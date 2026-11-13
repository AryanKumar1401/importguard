"""``importguard.lock``: the reviewed set of what each entrypoint loads at startup.

The lock deliberately records *packages*, not timings or line numbers, so it
only changes when startup really changes (a new dependency enters the import
graph), never because of timing noise or unrelated edits.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .probe import ProbeResult, relpath

LOCK_VERSION = 1


class LockError(Exception):
    pass


@dataclass
class LockEntry:
    python: str
    modules: int
    third_party: list[str]
    stdlib: list[str]
    first_party: dict[str, str]  # module -> file relative to root

    @classmethod
    def from_probe(cls, result: ProbeResult, root: Path) -> LockEntry:
        first_party = {
            m.name: relpath(m.origin, root) or "" for m in result.modules if m.kind == "first_party"
        }
        return cls(
            python=".".join(result.python.split(".")[:2]),
            modules=len(result.modules),
            third_party=sorted(result.packages("third_party")),
            stdlib=sorted(result.packages("stdlib")),
            first_party=dict(sorted(first_party.items())),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "python": self.python,
            "modules": self.modules,
            "third_party": self.third_party,
            "stdlib": self.stdlib,
            "first_party": self.first_party,
        }


@dataclass
class Lock:
    entrypoints: dict[str, LockEntry] = field(default_factory=dict)

    def to_json(self) -> str:
        payload = {
            "version": LOCK_VERSION,
            "generated_by": f"importguard {__version__}",
            "entrypoints": {
                name: entry.to_dict() for name, entry in sorted(self.entrypoints.items())
            },
        }
        return json.dumps(payload, indent=2) + "\n"

    def save(self, path: Path) -> None:
        path.write_text(self.to_json(), encoding="utf-8")


def load_lock(path: Path) -> Lock | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LockError(f"cannot read {path}: {exc}") from exc
    if data.get("version") != LOCK_VERSION:
        raise LockError(
            f"{path} has lock version {data.get('version')!r}; expected {LOCK_VERSION}. "
            "Regenerate it with `importguard snapshot`."
        )
    entries = {}
    for name, raw in data.get("entrypoints", {}).items():
        entries[name] = LockEntry(
            python=raw.get("python", ""),
            modules=int(raw.get("modules", 0)),
            third_party=list(raw.get("third_party", [])),
            stdlib=list(raw.get("stdlib", [])),
            first_party=dict(raw.get("first_party", {})),
        )
    return Lock(entries)


def affected_entrypoints(
    lock: Lock | None, entrypoints: list[str], changed: list[str]
) -> tuple[list[str], str]:
    """Entrypoints whose startup could be affected by ``changed`` files.

    Returns (entrypoints, reason). Any change to dependency manifests, the lock
    or config re-checks everything; otherwise an entrypoint is re-checked only
    if one of its locked first-party files changed. A file outside every
    startup graph cannot change startup without also editing a file inside it.
    """
    if lock is None:
        return entrypoints, "no lockfile"
    global_triggers = (
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "importguard.lock",
        "uv.lock",
        "poetry.lock",
        "pdm.lock",
        "Pipfile.lock",
    )
    norm = [c.replace("\\", "/") for c in changed]
    norm = [c[2:] if c.startswith("./") else c for c in norm]
    for c in norm:
        name = c.rsplit("/", 1)[-1]
        if name in global_triggers or (name.startswith("requirements") and name.endswith(".txt")):
            return entrypoints, f"{c} changed"
        if name.endswith(".pth") or name in ("sitecustomize.py", "usercustomize.py"):
            return entrypoints, f"{c} changed"
    # A .py file outside every locked graph can still change startup if it adds
    # to or shadows a module that is loaded: a new submodule of a startup package
    # (picked up by a try/except import), a package dir shadowing a module, or a
    # top-level file shadowing a dependency or stdlib module.
    known_files = {f for e in lock.entrypoints.values() for f in e.first_party.values()}
    loaded_tops: set[str] = set()
    for e in lock.entrypoints.values():
        loaded_tops.update(m.partition(".")[0] for m in e.first_party)
        loaded_tops.update(p.partition(".")[0] for p in e.third_party)
        loaded_tops.update(p.partition(".")[0] for p in e.stdlib)
    for c in norm:
        if not c.endswith(".py") or c in known_files:
            continue
        parts = c.split("/")
        if parts[0] == "src" and len(parts) > 1:
            parts = parts[1:]
        top = parts[0].removesuffix(".py")
        if top in loaded_tops:
            return entrypoints, f"{c} may add to or shadow {top!r}"
    selected = []
    for ep in entrypoints:
        entry = lock.entrypoints.get(ep)
        if entry is None or set(entry.first_party.values()).intersection(norm):
            selected.append(ep)
    return selected, "changed files"
