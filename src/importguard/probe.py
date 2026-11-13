"""Run the probe in a fresh interpreter and turn its output into an import graph."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_PROBE_SOURCE = (Path(__file__).parent / "_probe.py").read_text(encoding="utf-8")
_IMPORTTIME = re.compile(r"^import time:\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(.+?)\s*$")
_THIRD_PARTY_MARKERS = {"site-packages", "dist-packages"}

DEFAULT_TIMEOUT = 120.0


class ProbeError(Exception):
    """The probe could not run at all (bad interpreter, timeout, crash)."""


@dataclass
class Module:
    name: str
    kind: str  # "stdlib" | "first_party" | "third_party"
    parent: str | None
    file: str | None  # file that triggered the import (relative to root when inside it)
    line: int | None
    origin: str | None
    self_us: int = 0
    cumulative_us: int = 0
    # Distribution-level name: the shortest dotted prefix that is not a namespace
    # package, so google.cloud and google.protobuf are different packages.
    package: str = ""
    namespace: bool = False  # a namespace package: a directory, not a distribution
    synthetic: bool = False  # put in sys.modules without a spec (e.g. cython_runtime)

    def __post_init__(self) -> None:
        if not self.package:
            self.package = self.name.partition(".")[0]


@dataclass
class Effect:
    category: str
    event: str
    detail: str
    module: str | None
    file: str | None
    line: int | None


@dataclass
class ImportError_:
    type: str
    message: str
    traceback: str


@dataclass
class ProbeResult:
    target: str
    python: str
    executable: str
    modules: list[Module]
    effects: list[Effect]
    error: ImportError_ | None
    attempted: list[str] = field(default_factory=list)
    # Non-stdlib modules already imported when the interpreter started
    # (sitecustomize, usercustomize, .pth files); code the probe cannot observe.
    startup: list[Module] = field(default_factory=list)
    stubbed: list[str] = field(default_factory=list)  # missing modules replaced by stand-ins
    total_us: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def total_ms(self) -> float:
        return self.total_us / 1000.0

    def by_name(self) -> dict[str, Module]:
        return {m.name: m for m in self.modules}

    def packages(self, kind: str) -> set[str]:
        return {
            m.package
            for m in self.modules
            if m.kind == kind and not m.namespace and not m.synthetic
        }

    def chain(self, name: str) -> list[Module]:
        """Import chain from the target down to ``name`` (inclusive)."""
        index = self.by_name()
        out: list[Module] = []
        current = index.get(name)
        visited: set[str] = set()
        while current is not None and current.name not in visited:
            visited.add(current.name)
            out.append(current)
            current = index.get(current.parent) if current.parent else None
        out.reverse()
        return out

    def first_loaded(self, prefix: str) -> Module | None:
        """First module loaded that is ``prefix`` or inside it."""
        for m in self.modules:
            if m.name == prefix or m.name.startswith(prefix + "."):
                return m
        return None

    def subtree(self, name: str) -> list[Module]:
        """Modules whose import chain passes through ``name``."""
        children: dict[str, list[str]] = {}
        for m in self.modules:
            if m.parent:
                children.setdefault(m.parent, []).append(m.name)
        index = self.by_name()
        out: list[Module] = []
        stack = [name]
        seen: set[str] = set()
        while stack:
            n = stack.pop()
            if n in seen or n not in index:
                continue
            seen.add(n)
            out.append(index[n])
            stack.extend(children.get(n, []))
        return out


def _installed_dist_dir(path: Path, root: Path) -> bool:
    """True if ``path`` sits in a directory that holds installed distributions.

    Covers pip --target dirs, __pypackages__ and vendored installs inside the
    project root: any ancestor directory (below root) with a *.dist-info entry.
    """
    for parent in path.parents:
        if parent == root or root not in parent.parents:
            break
        try:
            if any(e.name.endswith(".dist-info") for e in parent.iterdir()):
                return True
        except OSError:
            return False
    return False


def classify(name: str, origin: str | None, stdlib: bool, root: Path) -> str:
    if stdlib:
        return "stdlib"
    if origin and origin not in ("built-in", "frozen"):
        try:
            path = Path(origin).resolve()
            rel = path.relative_to(root)
        except (ValueError, OSError):
            return "third_party"
        if not _THIRD_PARTY_MARKERS.intersection(rel.parts) and not _installed_dist_dir(path, root):
            return "first_party"
    return "third_party"


def _top_level_path(name: str, origin: str) -> Path:
    path = Path(origin)
    if path.name == "__init__.py":
        path = path.parent
    for _ in range(name.count(".")):
        path = path.parent
    return path


def _reclassify_ignored(modules: list[Module], root: Path) -> None:
    """Code inside the project whose top-level package git ignores is vendored
    or installed there (__pypackages__, pip --target), not first-party."""
    tops: dict[str, list[Module]] = {}
    for m in modules:
        if m.kind == "first_party" and m.origin:
            try:
                rel = _top_level_path(m.name, m.origin).resolve().relative_to(root)
            except (ValueError, OSError):
                continue
            tops.setdefault(rel.as_posix(), []).append(m)
    if not tops:
        return
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "--stdin"],
            cwd=root,
            input="\n".join(tops) + "\n",
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return
    for ignored in proc.stdout.splitlines():
        for m in tops.get(ignored.strip(), []):
            m.kind = "third_party"


def relpath(path: str | None, root: Path) -> str | None:
    if path is None or path.startswith("<"):
        return path
    try:
        return Path(path).resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return path


def probe_env(root: Path, extra_paths: list[str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    paths = [str((root / p).resolve()) for p in (extra_paths or [])]
    if not extra_paths and (root / "src").is_dir():
        paths.append(str((root / "src").resolve()))
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    if paths:
        env["PYTHONPATH"] = os.pathsep.join(paths)
    env.pop("PYTHON_LAZY_IMPORTS", None)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_probe(
    target: str,
    *,
    root: Path,
    python: str | None = None,
    pythonpath: list[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    stubs: list[str] | None = None,
) -> ProbeResult:
    """Import ``target`` in a fresh interpreter and return what happened.

    ``stubs``: missing modules to replace with inert stand-ins (base probes in
    ``diff`` whose dependencies are no longer installed).
    """
    root = root.resolve()
    interpreter = python or sys.executable
    with tempfile.TemporaryDirectory(prefix="importguard-") as tmp:
        out_path = Path(tmp) / "probe.json"
        cmd = [
            interpreter,
            "-X",
            "importtime",
            "-c",
            _PROBE_SOURCE,
            str(out_path),
            target,
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=root,
                env=probe_env(root, pythonpath) | {"IMPORTGUARD_STUBS": ",".join(stubs or [])},
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProbeError(f"importing {target} timed out after {timeout:.0f}s") from exc
        except OSError as exc:
            raise ProbeError(f"could not run interpreter {interpreter!r}: {exc}") from exc
        if not out_path.exists():
            tail = "\n".join(proc.stderr.splitlines()[-15:])
            raise ProbeError(
                f"probe for {target} produced no result (exit {proc.returncode}).\n{tail}"
            )
        data = json.loads(out_path.read_text(encoding="utf-8"))
    return _build_result(data, proc.stderr, root)


def _build_result(data: dict, stderr: str, root: Path) -> ProbeResult:  # type: ignore[type-arg]
    # Only importtime lines between the probe's markers belong to the target.
    times: dict[str, tuple[int, int]] = {}
    inside = "__importguard_begin__" not in stderr  # old probes: whole stream
    for line in stderr.splitlines():
        if line == "__importguard_begin__":
            inside = True
            continue
        if line == "__importguard_end__":
            inside = False
            continue
        m = _IMPORTTIME.match(line)
        if inside and m and m.group(3) not in times:
            times[m.group(3)] = (int(m.group(1)), int(m.group(2)))

    raws = list(data["modules"])
    kept = [r for r in raws if r.get("present", True)]
    attempted = [r["name"] for r in raws if not r.get("present", True)]
    namespaces = {r["name"] for r in kept if r.get("namespace")}

    def package_of(name: str) -> str:
        parts = name.split(".")
        i = 1
        while i < len(parts) and ".".join(parts[:i]) in namespaces:
            i += 1
        return ".".join(parts[:i])

    modules: list[Module] = []
    for raw in kept:
        self_us, cum_us = times.get(raw["name"], (0, 0))
        if raw["parent"] == "__main__":  # imported by the probe itself
            raw["parent"] = raw["file"] = raw["line"] = None
        modules.append(
            Module(
                name=raw["name"],
                kind=classify(raw["name"], raw["origin"], raw["stdlib"], root),
                parent=raw["parent"],
                file=relpath(raw["file"], root),
                line=raw["line"],
                origin=raw["origin"],
                self_us=self_us,
                cumulative_us=cum_us,
                package=package_of(raw["name"]),
                namespace=bool(raw.get("namespace")),
                synthetic=bool(raw.get("synthetic")),
            )
        )
    _reclassify_ignored(modules, root)
    startup = [
        Module(
            name=r["name"],
            kind=classify(r["name"], r["origin"], False, root),
            parent=None,
            file=None,
            line=None,
            origin=r["origin"],
        )
        for r in data.get("startup", [])
    ]
    target = data["target"]
    loaded = {m.name for m in modules}
    parts = target.split(".")
    roots = {".".join(parts[: i + 1]) for i in range(len(parts))}
    total = sum(times.get(n, (0, 0))[1] for n in roots if n in loaded)
    err = data.get("error")
    return ProbeResult(
        target=target,
        python=data["python"],
        executable=data["executable"],
        modules=modules,
        effects=[
            Effect(
                category=e["category"],
                event=e["event"],
                detail=e["detail"],
                module=e["module"],
                file=relpath(e["file"], root),
                line=e["line"],
            )
            for e in data["effects"]
        ],
        error=ImportError_(err["type"], err["message"], err["traceback"]) if err else None,
        attempted=attempted,
        startup=startup,
        stubbed=data.get("stubbed", []),
        total_us=total,
    )
