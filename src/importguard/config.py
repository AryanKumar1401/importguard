"""Configuration from ``[tool.importguard]`` in pyproject.toml."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

SEVERITIES = ("error", "warning", "info", "ignore")
EFFECT_CATEGORIES = ("network", "subprocess", "file_write", "env", "thread")


class ConfigError(Exception):
    pass


@dataclass
class Policy:
    """Rules applied to one entrypoint."""

    forbid: list[str] = field(default_factory=list)
    forbid_effects: list[str] = field(default_factory=lambda: ["network", "subprocess"])
    new_packages: str = "error"
    new_stdlib: str = "info"
    new_first_party: str = "info"
    max_ms: float | None = None
    strict_time: bool = False


@dataclass
class Config:
    root: Path
    entrypoints: list[str]
    lockfile: Path
    python: str | None = None
    pythonpath: list[str] = field(default_factory=list)
    timeout: float = 120.0
    defaults: Policy = field(default_factory=Policy)
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    source: Path | None = None

    def policy(self, entrypoint: str) -> Policy:
        raw = self.overrides.get(entrypoint)
        if not raw:
            return self.defaults
        merged = _policy_from(raw, base=self.defaults, where=f"entrypoint {entrypoint!r}")
        # forbid lists accumulate: global forbids still apply
        merged.forbid = sorted(set(self.defaults.forbid) | set(merged.forbid))
        return merged


def find_pyproject(start: Path) -> Path | None:
    for directory in [start, *start.parents]:
        candidate = directory / "pyproject.toml"
        if candidate.is_file():
            return candidate
    return None


def load_config(path: Path | None = None, cwd: Path | None = None) -> Config:
    cwd = (cwd or Path.cwd()).resolve()
    pyproject = path.resolve() if path else find_pyproject(cwd)
    data: dict[str, Any] = {}
    if pyproject is not None:
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read {pyproject}: {exc}") from exc
    root = pyproject.parent if pyproject else cwd
    tool = data.get("tool", {}).get("importguard", {})
    if not isinstance(tool, dict):
        raise ConfigError("[tool.importguard] must be a table")

    known = {
        "entrypoints",
        "lockfile",
        "python",
        "pythonpath",
        "timeout",
        "entrypoint",
        *Policy.__dataclass_fields__,
    }
    unknown = set(tool) - known
    if unknown:
        raise ConfigError(f"unknown [tool.importguard] keys: {', '.join(sorted(unknown))}")

    entrypoints = tool.get("entrypoints") or infer_entrypoints(data, root)
    overrides = tool.get("entrypoint", {})
    for name in overrides:
        if name not in entrypoints:
            entrypoints.append(name)

    return Config(
        root=root,
        entrypoints=list(entrypoints),
        lockfile=root / tool.get("lockfile", "importguard.lock"),
        python=tool.get("python"),
        pythonpath=list(tool.get("pythonpath", [])),
        timeout=float(tool.get("timeout", 120.0)),
        defaults=_policy_from(tool, base=Policy(), where="[tool.importguard]"),
        overrides=overrides,
        source=pyproject,
    )


def _policy_from(raw: dict[str, Any], *, base: Policy, where: str) -> Policy:
    policy = replace(base)
    for key in Policy.__dataclass_fields__:
        if key in raw:
            setattr(policy, key, raw[key])
    for key in ("new_packages", "new_stdlib", "new_first_party"):
        if getattr(policy, key) not in SEVERITIES:
            raise ConfigError(f"{where}: {key} must be one of {', '.join(SEVERITIES)}")
    bad = set(policy.forbid_effects) - set(EFFECT_CATEGORIES)
    if bad:
        raise ConfigError(
            f"{where}: unknown effect categories {sorted(bad)}; "
            f"choose from {', '.join(EFFECT_CATEGORIES)}"
        )
    unknown = set(raw) - set(Policy.__dataclass_fields__)
    if where.startswith("entrypoint") and unknown:
        raise ConfigError(f"{where}: unknown keys {', '.join(sorted(unknown))}")
    policy.forbid = list(policy.forbid)
    policy.forbid_effects = list(policy.forbid_effects)
    return policy


def infer_entrypoints(data: dict[str, Any], root: Path) -> list[str]:
    """Guess entrypoints: console-script modules plus the project's own package."""
    project = data.get("project", {})
    found: list[str] = []
    for target in project.get("scripts", {}).values():
        module = str(target).split(":", 1)[0].strip()
        if module and module not in found:
            found.append(module)
    name = project.get("name")
    if name:
        pkg = re.sub(r"[-.]+", "_", name).lower()
        for base in (root / "src", root):
            if (base / pkg / "__init__.py").is_file() or (base / f"{pkg}.py").is_file():
                if pkg not in found:
                    found.insert(0, pkg)
                break
    return found
