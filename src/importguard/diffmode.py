"""diff: what does this change add to startup, compared with its base commit?

The baseline is the merge-base commit, checked out into a temporary git
worktree and probed with the same interpreter as the working tree. Nothing
is stored in the repository, so:

- adoption needs no lockfile, no config and no baseline of existing problems:
  only what the change *adds* is reported;
- the change under review cannot move its own baseline. Policy (forbid,
  allow, severities) is read from the base commit too, so an edit that loosens
  the rules only takes effect after it has been reviewed and merged. Approvals
  come from outside the diff (``--allow``, e.g. from a pull-request label set
  by a maintainer, or ``allow`` on the base branch).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .checks import check_result
from .config import Config, ConfigError, load_config
from .findings import EntrypointSummary, Finding, Remedy, Report
from .lockfile import LockEntry
from .probe import ImportError_, ProbeResult, run_probe

DEFAULT_BASES = ("origin/HEAD", "origin/main", "origin/master", "main", "master")


@dataclass
class BaseRef:
    sha: str
    label: str
    note: str | None = None


def _git(cwd: Path, *args: str) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise ConfigError("importguard diff needs git on PATH") from exc
    return proc.stdout.strip()


def _resolves(root: Path, ref: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            cwd=root,
            capture_output=True,
        ).returncode
        == 0
    )


_FULL_SHA = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")


def _short(ref: str) -> str:
    """A ref for display: full commit SHAs (as CI passes them) shortened."""
    return ref[:10] if _FULL_SHA.fullmatch(ref) else ref


def resolve_base(root: Path, ref: str | None) -> BaseRef:
    try:
        _git(root, "rev-parse", "--verify", "HEAD")
    except subprocess.CalledProcessError as exc:
        raise ConfigError(
            "importguard diff needs a git repository with at least one commit"
        ) from exc
    candidates = [ref] if ref else list(DEFAULT_BASES)
    for cand in candidates:
        if not _resolves(root, cand):
            continue
        try:
            sha = _git(root, "merge-base", cand, "HEAD")
            return BaseRef(sha=sha, label=f"merge-base of HEAD and {_short(cand)}")
        except subprocess.CalledProcessError:
            sha = _git(root, "rev-parse", f"{cand}^{{commit}}")
            return BaseRef(
                sha=sha,
                label=_short(cand),
                note=(
                    f"no merge-base with {_short(cand)} (shallow clone?); "
                    "comparing against it directly"
                ),
            )
    if ref:
        raise ConfigError(f"--base {ref!r} is not a commit in this repository")
    raise ConfigError(
        "could not find a base branch (tried "
        + ", ".join(DEFAULT_BASES)
        + "); pass --base REF, e.g. --base origin/main"
    )


@contextmanager
def base_checkout(root: Path, sha: str) -> Iterator[Path]:
    """Check ``sha`` out into a temporary worktree; yield the path matching ``root``."""
    top = Path(_git(root, "rev-parse", "--show-toplevel")).resolve()
    rel = root.resolve().relative_to(top)
    tmp = Path(tempfile.mkdtemp(prefix="importguard-base-"))
    tree = tmp / "base"
    try:
        _git(top, "worktree", "add", "--detach", "--quiet", str(tree), sha)
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ConfigError(
            f"could not check out base commit {sha[:12]}: {exc.stderr.strip()}"
        ) from exc
    try:
        yield tree / rel
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(tree)], cwd=top, capture_output=True
        )
        shutil.rmtree(tmp, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=top, capture_output=True)


def _policy_changed(
    base: dict[str, object], head: dict[str, object], applied: str
) -> Finding | None:
    if base == head:
        return None
    keys = sorted(k for k in set(base) | set(head) if base.get(k) != head.get(k))
    if applied == "head":
        tail = "the edited policy is being applied (--policy-from head)"
    else:
        tail = "diff applies the base branch's policy, so the edit takes effect after merge"
    adopting = not base
    verb = "adds" if adopting else "edits"
    return Finding(
        id="policy_changed",
        kind="policy_changed",
        # Introducing a config for the first time is adoption, not loosening.
        severity="info" if adopting and applied != "head" else "warning",
        entrypoint="(config)",
        message=f"this change {verb} [tool.importguard] ({', '.join(keys)}); {tail}",
        data={"keys": keys},
        remedies=[
            Remedy(
                kind="request_approval",
                summary="policy changes need review by a maintainer",
            )
        ],
    )


def run_diff(
    config: Config,
    *,
    entrypoints: list[str],
    base_ref: str | None,
    allow: list[str],
    policy_from: str,
    python: str | None,
) -> Report:
    base = resolve_base(config.root, base_ref)
    report = Report(command="diff")
    report.data = {"base": {"sha": base.sha, "ref": base.label}}
    if base.note:
        report.data["note"] = base.note
    allowed = frozenset(a for a in allow if a)

    with base_checkout(config.root, base.sha) as base_root:
        base_cfg: Config | None = None
        if (base_root / "pyproject.toml").is_file():
            try:
                base_cfg = load_config(base_root / "pyproject.toml")
            except ConfigError as exc:
                report.data["base_config_error"] = str(exc)
        if policy_from == "head":
            policy_cfg, source = config, "head"
        elif base_cfg is not None and base_cfg.raw:
            policy_cfg, source = base_cfg, "base"
        else:
            # The base has no policy: apply the defaults, never the head's. Otherwise a
            # change could introduce a config that approves its own additions.
            policy_cfg = Config(
                root=base_root, entrypoints=[], lockfile=base_root / "importguard.lock"
            )
            source = "base (defaults)"
        report.data["policy_source"] = source

        if entrypoints:
            targets = list(entrypoints)
        else:
            # Union: removing an entrypoint from the config cannot hide it from review.
            targets = list(
                dict.fromkeys((base_cfg.entrypoints if base_cfg else []) + config.entrypoints)
            )
        if not targets:
            raise ConfigError(
                "no entrypoints: pass one (e.g. `importguard diff mypkg.cli`) or set "
                "`entrypoints = [...]` under [tool.importguard] in pyproject.toml"
            )
        changed = _policy_changed(base_cfg.raw if base_cfg else {}, config.raw, source)
        if changed is not None:
            report.findings.append(changed)

        # Runtime settings come from the same place as the policy: a change must not
        # be able to pick the interpreter or search path that judges it.
        runtime = policy_cfg
        interpreter = python or runtime.python

        def probe(ep: str, root: Path, stubs: list[str] | None = None) -> ProbeResult:
            return run_probe(
                ep,
                root=root,
                python=interpreter,
                pythonpath=runtime.pythonpath,
                timeout=runtime.timeout,
                stubs=stubs,
            )

        def probe_base(ep: str, head: ProbeResult) -> ProbeResult:
            # The environment has the change's dependencies. If the base imports a
            # module that is no longer installed (the change removed a dependency),
            # retry with inert stand-ins so the comparison can still be made.
            # Only third-party dependencies are stubbed: the project's own code
            # missing on base (e.g. a new entrypoint) stays a real failure.
            own = {ep.partition(".")[0]} | {
                m.name.partition(".")[0] for m in head.modules if m.kind == "first_party"
            }
            stubs: list[str] = []
            result = probe(ep, base_root)
            while result.error is not None and len(stubs) < 5:
                missing = _missing_module(result)
                if missing is None or missing in stubs or missing in own:
                    break
                stubs.append(missing)
                result = probe(ep, base_root, stubs)
            return result

        heads = {ep: probe(ep, config.root) for ep in targets}
        bases: dict[str, ProbeResult] = {}
        shadowed: dict[str, str] = {}
        for ep in targets:
            base_res = probe_base(ep, heads[ep])
            shadow = _shadowed(ep, base_res, base_root)
            if shadow and not _exists_in(shadow, config.root, base_root):
                # The entrypoint is new in this change; the base probe fell through
                # to an installed copy of the head. Treat the base as absent.
                base_res.error = base_res.error or ImportError_(
                    "ModuleNotFoundError", f"{ep} does not exist on base", ""
                )
                shadow = None
            if shadow:
                shadowed[ep] = shadow
            bases[ep] = base_res
        # For an entrypoint the base cannot import (new in this change, or broken by
        # the environment), compare against everything the base's other entrypoints
        # load. With nothing importable on base, everything counts as new: fail closed.
        fallback = _union_entry(
            [LockEntry.from_probe(b, base_root) for b in bases.values() if b.ok]
        )

        deltas: dict[str, dict[str, object]] = {}
        for ep in targets:
            head, base_res = heads[ep], bases[ep]
            report.python = head.python
            if ep in shadowed:
                report.entrypoints.append(
                    EntrypointSummary(
                        name=ep,
                        status="error",
                        note=(
                            f"base probe loaded {ep} from {shadowed[ep]}, not from the base "
                            "checkout (an installed copy shadows it); set `pythonpath` under "
                            "[tool.importguard] or install the project in editable mode"
                        ),
                    )
                )
                report.tool_error = report.tool_error or f"cannot compare {ep}: base is shadowed"
                continue
            policy = policy_cfg.policy(ep)
            base_entry = LockEntry.from_probe(base_res, base_root) if base_res.ok else fallback
            summary, findings = check_result(
                ep,
                head,
                policy,
                base_entry,
                config.root,
                lock_exists=False,
                mode="diff",
                allowed=allowed,
            )
            if base_res.ok:
                findings = _only_new(findings, base_res, policy.max_ms)
                summary.locked_modules = len(base_res.modules)
                if base_res.stubbed:
                    summary.note = (
                        "base imports "
                        + ", ".join(sorted({n.partition(".")[0] for n in base_res.stubbed}))
                        + ", not installed here (this change likely removed it); "
                        "compared with it stubbed out"
                    )
            elif head.ok:
                why = base_res.error.type if base_res.error else "?"
                summary.note = (
                    f"not importable on base ({why}); compared with what the base's other "
                    "entrypoints load"
                    if fallback.modules
                    else f"not importable on base ({why}); everything it loads counts as new"
                )
            else:
                findings = []
                summary.status, summary.note = "skipped", "fails to import on base and head"
            if summary.status != "skipped":
                summary.status = (
                    "error"
                    if head.error
                    else "fail"
                    if any(f.severity == "error" for f in findings)
                    else "pass"
                )
            report.entrypoints.append(summary)
            report.findings.extend(findings)
            deltas[ep] = {
                "base_modules": len(base_res.modules) if base_res.ok else None,
                "head_modules": len(head.modules),
                "base_ms": round(base_res.total_ms, 1) if base_res.ok else None,
                "head_ms": round(head.total_ms, 1),
            }
        report.data["entrypoints"] = deltas
    return report


_MISSING = re.compile(r"No module named '([^']+)'")


def _missing_module(result: ProbeResult) -> str | None:
    """Top-level name of the module whose absence made the import fail."""
    if result.error is None or result.error.type != "ModuleNotFoundError":
        return None
    m = _MISSING.search(result.error.message)
    return m.group(1).partition(".")[0] if m else None


def _union_entry(entries: list[LockEntry]) -> LockEntry:
    third: set[str] = set()
    std: set[str] = set()
    first: dict[str, str] = {}
    for e in entries:
        third.update(e.third_party)
        std.update(e.stdlib)
        first.update(e.first_party)
    return LockEntry(
        python=entries[0].python if entries else "",
        modules=sum(e.modules for e in entries),
        third_party=sorted(third),
        stdlib=sorted(std),
        first_party=first,
    )


def _shadowed(entrypoint: str, base: ProbeResult, base_root: Path) -> str | None:
    """Origin of the entrypoint if the base probe loaded it from outside the checkout."""
    mod = base.by_name().get(entrypoint)
    if mod is None or not mod.origin:
        return None
    try:
        Path(mod.origin).resolve().relative_to(base_root.resolve())
    except ValueError:
        return mod.origin
    return None


def _exists_in(origin: str, head_root: Path, base_root: Path) -> bool:
    try:
        rel = Path(origin).resolve().relative_to(head_root.resolve())
    except ValueError:
        return True  # outside the project entirely: a real installed copy
    return (base_root / rel).exists()


def _only_new(findings: list[Finding], base: ProbeResult, max_ms: float | None) -> list[Finding]:
    """Drop findings that already held on the base commit."""
    base_effects = {(e.category, e.module, e.event) for e in base.effects}
    keep = []
    for f in findings:
        if f.kind == "forbidden_import" and f.subject and base.first_loaded(f.subject):
            continue
        if (
            f.kind == "side_effect"
            and (
                f.data.get("category"),
                f.subject,
                f.data.get("event"),
            )
            in base_effects
        ):
            continue
        if f.kind == "slow_import" and max_ms is not None and base.total_ms > max_ms:
            continue
        keep.append(f)
    return keep


def allow_from_env() -> list[str]:
    raw = os.environ.get("IMPORTGUARD_ALLOW", "")
    return [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]
