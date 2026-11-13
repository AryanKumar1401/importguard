"""verify-lazy: find imports that are safe to make lazy, proven by running your tests.

On Python 3.15+ (PEP 810) any set of import statements can be made lazy at
runtime with ``sys.set_lazy_imports("all")`` plus a filter. We use that to:

1. discover every import statement in first-party code that loads something
   at startup (``discover`` probe),
2. measure what making each one lazy actually saves (``lazy`` probes),
3. run the project's test command with the candidates lazy and, if it fails,
   bisect down to the statements that break it.

Static linters can say an import *could* be lazy; this says it *is* safe under
your tests and how much startup it saves.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .checks import source_line
from .findings import Finding, Remedy
from .probe import ProbeResult, Statement, probe_env, run_probe
from .sourcefix import lazy_rewrite

Key = tuple[str, str, str]

_BOOTSTRAP = r"""
import os as _os
import sys as _sys


def _importguard_lazy():
    spec = _os.environ.get("IMPORTGUARD_LAZY_SPEC")
    hits_path = _os.environ.get("IMPORTGUARD_LAZY_HITS")
    if not spec or not hits_path or not hasattr(_sys, "set_lazy_imports"):
        return
    keys = set()
    with open(spec, encoding="utf-8") as fh:
        for raw in fh.read().splitlines():
            if raw:
                keys.add(tuple(raw.split("\t")))
    hits = {}  # key -> {(file, line)}
    sources = {}

    def _line(filename, lineno):
        # Plain open(): importing linecache/tokenize here would recurse into imports.
        lines = sources.get(filename)
        if lines is None:
            try:
                with open(filename, "rb") as fh:
                    lines = fh.read().decode("utf-8", "replace").splitlines()
            except Exception:
                lines = []
            sources[filename] = lines
        return lines[lineno - 1] if 0 < lineno <= len(lines) else ""

    def _filter(importer, name, fromlist):
        frame = _sys._getframe(1)
        if importer == "__main__":
            # `python -m pkg.mod`: key statements by the module's real name.
            main_spec = getattr(_sys.modules.get("__main__"), "__spec__", None)
            if main_spec is not None and main_spec.name:
                importer = main_spec.name
        key = (importer, name, ",".join(fromlist) if fromlist else "")
        if key in keys:
            hits.setdefault(key, set()).add((frame.f_code.co_filename, frame.f_lineno))
            return True
        # Everything else keeps the laziness its source asked for.
        listed = frame.f_globals.get("__lazy_modules__")
        try:
            if listed is not None and name in listed:
                return True
        except TypeError:
            pass
        text = _line(frame.f_code.co_filename, frame.f_lineno).lstrip()
        return text.startswith("lazy ") or text.startswith("lazy\t")

    _sys.set_lazy_imports_filter(_filter)
    _sys.set_lazy_imports("all")
    with open(hits_path, "a", encoding="utf-8") as fh:
        fh.write("#active\n")

    def _bound_names(filename, lineno):
        import ast

        try:
            with open(filename, "rb") as fh:
                tree = ast.parse(fh.read())
        except Exception:
            return []
        names = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and node.lineno == lineno:
                for alias in node.names:
                    if alias.asname:
                        names.append(alias.asname)
                    elif isinstance(node, ast.Import):
                        names.append(alias.name.partition(".")[0])
                    else:
                        names.append(alias.name)
        return names

    def _flush():
        lines = []
        for key, sites in hits.items():
            lines.append("hit\t" + "\t".join(key))
            module = _sys.modules.get(key[0])
            if key[0] == _getattr_main_name():
                module = module or _sys.modules.get("__main__")
            namespace = getattr(module, "__dict__", {})
            used = False
            for filename, lineno in sites:
                for bound in _bound_names(filename, lineno):
                    value = namespace.get(bound)
                    if value is not None and type(value).__name__ != "lazy_import":
                        used = True
            if used:
                lines.append("used\t" + "\t".join(key))
        with open(hits_path, "a", encoding="utf-8") as fh:
            fh.write("".join(line + "\n" for line in lines))

    def _getattr_main_name():
        main_spec = getattr(_sys.modules.get("__main__"), "__spec__", None)
        return getattr(main_spec, "name", None)

    import atexit

    atexit.register(_flush)


_importguard_lazy()

# Chain to the sitecustomize this one shadows, if any.
_here = _os.path.dirname(_os.path.abspath(__file__))
_saved_path = list(_sys.path)
_sys.path[:] = [p for p in _sys.path if _os.path.abspath(p or ".") != _here]
_me = _sys.modules.pop("sitecustomize", None)
try:
    import sitecustomize  # noqa: F401
except ImportError:
    pass
finally:
    _sys.path[:] = _saved_path
    if "sitecustomize" not in _sys.modules and _me is not None:
        _sys.modules["sitecustomize"] = _me
"""


class LazyError(Exception):
    pass


@dataclass
class Candidate:
    statement: Statement
    eager_modules: list[str]  # modules this statement first-loaded (with their subtrees)
    saves_modules: int = 0
    saves_ms: float = 0.0
    status: str = "untested"  # safe | unsafe | untested
    exercised: bool = False  # tests used the lazily imported name (it was reified)
    executed: bool = False  # tests ran the import statement at all
    reason: str | None = None  # why it is not verified
    failure: str | None = None
    has_effects: bool = False

    @property
    def key(self) -> Key:
        return self.statement.key


@dataclass
class TestRun:
    passed: bool
    hits: set[Key]  # statements executed (lazily) during the run
    active: bool
    output: str
    used: set[Key] = field(default_factory=set)  # lazily imported names then used


@dataclass
class LazyOutcome:
    candidates: list[Candidate]
    eager: ProbeResult
    lazy: ProbeResult | None
    test_runs: int = 0
    notes: list[str] = field(default_factory=list)


def find_candidates(eager: ProbeResult) -> list[Candidate]:
    """Import statements in first-party code that loaded at least one new module."""
    index = eager.by_name()
    by_site: dict[tuple[str | None, str | None, int | None], list[str]] = {}
    for m in eager.modules:
        if m.parent:
            by_site.setdefault((m.parent, m.file, m.line), []).append(m.name)
    effect_modules = {e.module for e in eager.effects if e.module}
    out: dict[Key, Candidate] = {}
    for s in eager.statements:
        importer = index.get(s.importer)
        if importer is None or importer.kind != "first_party" or s.key in out:
            continue
        loaded = by_site.get((s.importer, s.file, s.line), [])
        if not loaded:
            continue
        subtree: list[str] = []
        for name in loaded:
            subtree.extend(m.name for m in eager.subtree(name))
        subtree = sorted(set(subtree))
        out[s.key] = Candidate(
            statement=s,
            eager_modules=subtree,
            has_effects=bool(effect_modules.intersection(subtree)),
        )
    return sorted(out.values(), key=lambda c: len(c.eager_modules), reverse=True)


def run_tests(
    command: str,
    keys: list[Key],
    *,
    root: Path,
    pythonpath: list[str],
    timeout: float,
) -> TestRun:
    with tempfile.TemporaryDirectory(prefix="importguard-lazy-") as tmp:
        boot = Path(tmp) / "boot"
        boot.mkdir()
        (boot / "sitecustomize.py").write_text(_BOOTSTRAP, encoding="utf-8")
        spec = Path(tmp) / "spec.tsv"
        spec.write_text("\n".join("\t".join(k) for k in keys) + "\n", encoding="utf-8")
        hits_path = Path(tmp) / "hits.tsv"
        env = probe_env(root, pythonpath)
        env["PYTHONPATH"] = os.pathsep.join(p for p in (str(boot), env.get("PYTHONPATH", "")) if p)
        env["IMPORTGUARD_LAZY_SPEC"] = str(spec)
        env["IMPORTGUARD_LAZY_HITS"] = str(hits_path)
        passed, output = _run_shell(command, cwd=root, env=env, timeout=timeout)
        hits: set[Key] = set()
        used: set[Key] = set()
        active = False
        if hits_path.exists():
            for raw in hits_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if raw == "#active":
                    active = True
                    continue
                parts = raw.split("\t")
                if len(parts) != 4 or parts[0] not in ("hit", "used"):
                    continue  # tolerate interleaved writes from parallel workers
                key = (parts[1], parts[2], parts[3])
                (hits if parts[0] == "hit" else used).add(key)
    return TestRun(passed=passed, hits=hits, active=active, output=output, used=used)


def _run_shell(command: str, *, cwd: Path, env: dict[str, str], timeout: float) -> tuple[bool, str]:
    """Run a shell command; on timeout kill its whole process group."""
    posix = os.name == "posix"
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=posix,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if posix:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        else:  # pragma: no cover
            proc.kill()
        proc.communicate()
        return False, f"test command timed out after {timeout:.0f}s"
    return proc.returncode == 0, out


def failure_excerpt(output: str, limit: int = 12) -> str:
    """The most informative lines of a failing test run (pytest summary if present)."""
    lines = output.strip().splitlines()
    for i, line in enumerate(lines):
        if "short test summary info" in line:
            return "\n".join(lines[i + 1 : i + 1 + limit])
    picked = [ln for ln in lines if ln.startswith(("FAILED", "ERROR", "E ")) or "Error" in ln]
    return "\n".join((picked or lines)[-limit:])


def isolate_failures(
    candidates: list[Candidate],
    fails: Callable[[list[Candidate]], bool],
    keep: list[Candidate] | None = None,
) -> list[Candidate]:
    """Adaptive group testing: the candidates that make ``fails`` true.

    ``keep + candidates`` must be known to fail and ``keep`` alone to pass. When
    neither half fails on its own, the failure needs members of both halves:
    each half is searched with the other kept lazy, so only the interacting
    imports are blamed, not the whole group.
    """
    keep = keep or []
    if len(candidates) <= 1:
        return list(candidates)
    mid = len(candidates) // 2
    left, right = candidates[:mid], candidates[mid:]
    left_fails, right_fails = fails(keep + left), fails(keep + right)
    culprits: list[Candidate] = []
    if left_fails:
        culprits += isolate_failures(left, fails, keep)
    if right_fails:
        culprits += isolate_failures(right, fails, keep)
    if not left_fails and not right_fails:
        culprits += isolate_failures(left, fails, keep + right)
        culprits += isolate_failures(right, fails, keep + left)
    return culprits


def verify_lazy(
    entrypoint: str,
    *,
    root: Path,
    python: str | None,
    pythonpath: list[str],
    test_command: str | None,
    max_candidates: int = 40,
    probe_timeout: float = 120.0,
    test_timeout: float = 900.0,
    progress: Callable[[str], None] | None = None,
) -> LazyOutcome:
    say = progress or (lambda _msg: None)
    say(f"discovering import statements in {entrypoint}")
    eager = run_probe(
        entrypoint,
        root=root,
        python=python,
        mode="discover",
        pythonpath=pythonpath,
        timeout=probe_timeout,
    )
    if eager.error is not None:
        raise LazyError(
            f"importing {entrypoint} fails even eagerly: {eager.error.type}: {eager.error.message}"
        )
    candidates = find_candidates(eager)
    notes: list[str] = []
    if len(candidates) > max_candidates:
        notes.append(f"limited to the {max_candidates} heaviest of {len(candidates)} candidates")
        candidates = candidates[:max_candidates]

    def probe_lazy(keys: list[Key]) -> ProbeResult:
        return run_probe(
            entrypoint,
            root=root,
            python=python,
            mode="lazy",
            lazy_keys=keys,
            pythonpath=pythonpath,
            timeout=probe_timeout,
        )

    eager_names = {m.name for m in eager.modules}
    say(f"measuring {len(candidates)} candidate(s)")
    all_lazy = probe_lazy([c.key for c in candidates]) if candidates else None
    combined_saved = eager_names - {m.name for m in all_lazy.modules} if all_lazy else set()
    eager_index = eager.by_name()
    for c in candidates:
        solo = probe_lazy([c.key])
        if solo.error is not None:
            c.status = "unsafe"
            c.failure = f"import fails when lazy: {solo.error.type}: {solo.error.message}"
            continue
        saved = eager_names - {m.name for m in solo.modules}
        c.saves_modules = len(saved)
        c.saves_ms = sum(eager_index[n].self_us for n in saved if n in eager_index) / 1000.0
    candidates = [
        c
        for c in candidates
        if c.status == "unsafe" or c.saves_modules or combined_saved.intersection(c.eager_modules)
    ]

    runs = 0
    if test_command and candidates:

        def execute(keys: list[Key]) -> TestRun:
            nonlocal runs
            runs += 1
            return run_tests(
                test_command, keys, root=root, pythonpath=pythonpath, timeout=test_timeout
            )

        say("running tests eagerly (baseline)")
        baseline = execute([])
        if not baseline.active:
            raise LazyError(
                "the test command did not run under a Python with lazy imports (3.15+); "
                "invoke that interpreter explicitly, e.g. --test 'python3.15 -m pytest -x -q'"
            )
        if not baseline.passed:
            tail = "\n".join(baseline.output.strip().splitlines()[-15:])
            raise LazyError(f"tests fail before any import is made lazy; fix them first:\n{tail}")

        pending = [c for c in candidates if c.status != "unsafe"]
        outputs: dict[Key, str] = {}
        last_pass: TestRun | None = None
        for _round in range(8):
            if not pending:
                break
            say(f"running tests with {len(pending)} import(s) lazy")
            run = execute([c.key for c in pending])
            if run.passed:
                last_pass = run
                break

            def fails(group: list[Candidate]) -> bool:
                say(f"bisecting: {len(group)} import(s) lazy")
                result = execute([c.key for c in group])
                if not result.passed:
                    for c in group:
                        outputs.setdefault(c.key, result.output)
                return not result.passed

            for c in pending:
                outputs.setdefault(c.key, run.output)
            culprits = isolate_failures(pending, fails)
            for c in culprits:
                c.status = "unsafe"
                c.failure = failure_excerpt(outputs.get(c.key, ""))
            pending = [c for c in pending if c.status != "unsafe"]
        if last_pass is not None:
            for c in pending:
                c.status = "safe"
                c.executed = c.key in last_pass.hits
                c.exercised = c.key in last_pass.used
                if not c.executed:
                    c.reason = f"tests never imported {c.statement.importer}"
                elif not c.exercised:
                    c.reason = (
                        f"tests imported {c.statement.importer} but never used {c.statement.name}"
                    )
        elif pending:
            notes.append("could not converge on a passing set; remaining candidates untested")
            for c in pending:
                c.reason = "tests did not converge on a passing set"
    else:
        for c in candidates:
            if c.status != "unsafe":
                c.reason = "no --test command given" if not test_command else None

    safe_keys = [c.key for c in candidates if c.status != "unsafe"]
    final = probe_lazy(safe_keys) if safe_keys else None
    return LazyOutcome(candidates=candidates, eager=eager, lazy=final, test_runs=runs, notes=notes)


def lazy_findings(entrypoint: str, outcome: LazyOutcome, root: Path, tested: bool) -> list[Finding]:
    findings: list[Finding] = []
    ordered = sorted(outcome.candidates, key=lambda c: (c.status != "unsafe", -c.saves_modules))
    for c in ordered:
        s = c.statement
        fix = lazy_rewrite(root, s.file, s.line, s.name)
        src = fix.source or source_line(root, s.file, s.line)
        where = f"{s.file}:{s.line}"
        what = f"{s.name}" + (f" ({s.fromlist})" if s.fromlist else "")
        ident = f"{entrypoint}:{s.importer}:{s.name}:{s.fromlist}:{s.line}"
        data = {
            "statement": {"importer": s.importer, "module": s.name, "fromlist": s.fromlist},
            "saves_modules": c.saves_modules,
            "saves_ms": round(c.saves_ms, 1),
            "has_import_side_effects": c.has_effects,
            "lazy_modules_entry": s.name,
        }
        if c.status == "unsafe":
            data["failure"] = c.failure
            findings.append(
                Finding(
                    id=f"lazy_unsafe:{ident}",
                    kind="lazy_unsafe",
                    severity="warning",
                    entrypoint=entrypoint,
                    message=f"keep eager: making {what} lazy at {where} breaks the tests",
                    subject=s.name,
                    data=data,
                    remedies=[
                        Remedy(
                            kind="keep_eager",
                            summary="this import must run at import time (side effects, "
                            "registration or import-order dependence)",
                            file=s.file,
                            line=s.line,
                            source=src,
                        )
                    ],
                )
            )
            continue
        verified = c.status == "safe" and c.exercised
        replacement = fix.replacement
        effects_note = (
            " (it has import-time side effects; check they can run later)" if c.has_effects else ""
        )
        if c.saves_modules:
            gain = f"saves {c.saves_modules} modules (~{c.saves_ms:.0f}ms)"
        else:
            gain = "saves nothing alone, but is needed for the combined saving"
        if verified:
            kind, confidence = "lazy_safe", "verified"
            msg = (
                f"make lazy: {what} at {where} {gain}; tests pass with it lazy and use it"
                f"{effects_note}"
            )
        else:
            kind, confidence = "lazy_unexercised", "unexercised"
            why = c.reason or ("no --test command given" if not tested else "not exercised")
            msg = f"could be lazy: {what} at {where} {gain}; not verified ({why}){effects_note}"
        data["reason"] = None if verified else c.reason
        findings.append(
            Finding(
                id=f"{kind}:{ident}",
                kind=kind,
                severity="info",
                entrypoint=entrypoint,
                message=msg,
                subject=s.name,
                confidence=confidence,
                data=data,
                remedies=[
                    Remedy(
                        kind="make_lazy",
                        summary=(
                            "prefix the statement with `lazy` (3.15+) or add "
                            f"{s.name!r} to `__lazy_modules__` in {s.importer}"
                            + (f"; {fix.note}" if fix.note and not replacement else "")
                        ),
                        file=s.file,
                        line=s.line,
                        source=src,
                        replacement=replacement,
                    )
                ],
            )
        )
    return findings
