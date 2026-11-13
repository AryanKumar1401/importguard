"""Report model shared by every command. Stable, versioned JSON for agents and CI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import __version__

SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_USAGE = 2
EXIT_TOOL_ERROR = 3


@dataclass
class Step:
    """One hop in an import chain: ``module`` was imported by ``imported_by`` at file:line."""

    module: str
    imported_by: str | None
    file: str | None
    line: int | None


@dataclass
class Remedy:
    # defer_import | accept | request_approval | fix_error | investigate | make_lazy | keep_eager
    kind: str
    summary: str
    file: str | None = None
    line: int | None = None
    command: str | None = None
    source: str | None = None
    replacement: str | None = None


@dataclass
class Finding:
    id: str
    kind: str
    severity: str  # error | warning | info
    entrypoint: str
    message: str
    subject: str | None = None
    confidence: str = "verified"  # verified (observed at runtime) | unexercised | static
    chain: list[Step] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    remedies: list[Remedy] = field(default_factory=list)


@dataclass
class EntrypointSummary:
    name: str
    status: str  # pass | fail | skipped | error
    modules: int = 0
    total_ms: float = 0.0
    third_party: list[str] = field(default_factory=list)
    locked_modules: int | None = None
    note: str | None = None


@dataclass
class Report:
    command: str
    python: str | None = None
    entrypoints: list[EntrypointSummary] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    tool_error: str | None = None

    def count(self, severity: str) -> int:
        return sum(1 for f in self.findings if f.severity == severity)

    @property
    def ok(self) -> bool:
        return self.tool_error is None and self.count("error") == 0

    def exit_code(self, strict: bool = False) -> int:
        if self.tool_error is not None:
            return EXIT_TOOL_ERROR
        if self.count("error") or (strict and self.count("warning")):
            return EXIT_VIOLATION
        return EXIT_OK

    def to_dict(self) -> dict[str, Any]:
        order = {"error": 0, "warning": 1, "info": 2}
        findings = sorted(self.findings, key=lambda f: (order.get(f.severity, 3), f.entrypoint))
        return {
            "schema_version": SCHEMA_VERSION,
            "tool": f"importguard {__version__}",
            "command": self.command,
            "ok": self.ok,
            "python": self.python,
            "summary": {s: self.count(s) for s in ("error", "warning", "info")},
            "entrypoints": [asdict(e) for e in self.entrypoints],
            "findings": [_finding_dict(f) for f in findings],
            "data": self.data,
            "tool_error": self.tool_error,
        }


def _finding_dict(f: Finding) -> dict[str, Any]:
    d = asdict(f)
    d["remedies"] = [{k: v for k, v in r.items() if v is not None} for r in d["remedies"]]
    return d


REPORT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "importguard report",
    "type": "object",
    "required": ["schema_version", "command", "ok", "summary", "entrypoints", "findings"],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "tool": {"type": "string"},
        "command": {"type": "string"},
        "ok": {"type": "boolean", "description": "true when there are no error findings"},
        "python": {"type": ["string", "null"]},
        "summary": {
            "type": "object",
            "properties": {s: {"type": "integer"} for s in ("error", "warning", "info")},
        },
        "entrypoints": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "status": {"enum": ["pass", "fail", "skipped", "error"]},
                    "modules": {"type": "integer"},
                    "total_ms": {"type": "number", "description": "advisory; timing is noisy"},
                    "third_party": {"type": "array", "items": {"type": "string"}},
                    "locked_modules": {"type": ["integer", "null"]},
                    "note": {"type": ["string", "null"]},
                },
            },
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "kind", "severity", "entrypoint", "message"],
                "properties": {
                    "id": {"type": "string", "description": "stable across runs"},
                    "kind": {
                        "enum": [
                            "import_failed",
                            "forbidden_import",
                            "new_package",
                            "new_stdlib",
                            "new_first_party",
                            "removed_package",
                            "side_effect",
                            "slow_import",
                            "not_locked",
                            "lazy_safe",
                            "lazy_unsafe",
                            "lazy_unexercised",
                            "policy_changed",
                            "preloaded_code",
                        ]
                    },
                    "severity": {"enum": ["error", "warning", "info"]},
                    "entrypoint": {"type": "string"},
                    "message": {"type": "string"},
                    "subject": {"type": ["string", "null"]},
                    "confidence": {"enum": ["verified", "unexercised", "static"]},
                    "chain": {
                        "type": "array",
                        "description": "import chain from the entrypoint down to subject",
                        "items": {
                            "type": "object",
                            "properties": {
                                "module": {"type": "string"},
                                "imported_by": {"type": ["string", "null"]},
                                "file": {"type": ["string", "null"]},
                                "line": {"type": ["integer", "null"]},
                            },
                        },
                    },
                    "data": {"type": "object"},
                    "remedies": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["kind", "summary"],
                            "properties": {
                                "kind": {
                                    "enum": [
                                        "defer_import",
                                        "accept",
                                        "request_approval",
                                        "fix_error",
                                        "investigate",
                                        "make_lazy",
                                        "keep_eager",
                                    ]
                                },
                                "summary": {"type": "string"},
                                "file": {"type": "string"},
                                "line": {"type": "integer"},
                                "command": {"type": "string"},
                                "source": {"type": "string"},
                                "replacement": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        "data": {"type": "object"},
        "tool_error": {"type": ["string", "null"]},
    },
}
