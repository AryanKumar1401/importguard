"""Data models for importguard."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ViolationType(Enum):
    """Type of import violation."""

    EXCEEDED_BUDGET = "exceeded_budget"
    BANNED_IMPORT = "banned_import"


@dataclass
class ImportTiming:
    """Timing information for a single imported module."""

    module: str
    self_time_us: int  # microseconds spent in this module (excluding children)
    cumulative_time_us: int  # total time including children

    @property
    def self_time_ms(self) -> float:
        """Self time in milliseconds."""
        return self.self_time_us / 1000.0

    @property
    def cumulative_time_ms(self) -> float:
        """Cumulative time in milliseconds."""
        return self.cumulative_time_us / 1000.0


@dataclass
class Violation:
    """Represents a single import violation."""

    type: ViolationType
    message: str
    module: str
    details: str | None = None

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} ({self.details})"
        return self.message


@dataclass
class ImportResult:
    """Result of an import check."""

    module: str
    total_time_us: int  # total import time in microseconds
    imports: list[ImportTiming] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    banned_found: list[str] = field(default_factory=list)

    # For --repeat functionality
    all_times_us: list[int] = field(default_factory=list)
    num_runs: int = 1

    @property
    def total_ms(self) -> float:
        """Total import time in milliseconds."""
        return self.total_time_us / 1000.0

    @property
    def median_ms(self) -> float | None:
        """Median import time in milliseconds (when multiple runs)."""
        if not self.all_times_us:
            return None
        sorted_times = sorted(self.all_times_us)
        n = len(sorted_times)
        if n % 2 == 1:
            return sorted_times[n // 2] / 1000.0
        return (sorted_times[n // 2 - 1] + sorted_times[n // 2]) / 2000.0

    @property
    def min_ms(self) -> float | None:
        """Minimum import time in milliseconds (when multiple runs)."""
        if not self.all_times_us:
            return None
        return min(self.all_times_us) / 1000.0

    @property
    def passed(self) -> bool:
        """Whether the import check passed (no violations)."""
        return len(self.violations) == 0

    def top_imports(self, n: int = 10) -> list[ImportTiming]:
        """Return the top N slowest imports by self time."""
        return sorted(self.imports, key=lambda x: x.self_time_us, reverse=True)[:n]

    def to_dict(self) -> dict[str, object]:
        """Convert to dictionary for JSON output."""
        return {
            "module": self.module,
            "total_ms": self.total_ms,
            "median_ms": self.median_ms,
            "min_ms": self.min_ms,
            "num_runs": self.num_runs,
            "passed": self.passed,
            "violations": [
                {"type": v.type.value, "message": v.message, "module": v.module}
                for v in self.violations
            ],
            "banned_found": self.banned_found,
            "top_imports": [
                {
                    "module": i.module,
                    "self_ms": i.self_time_ms,
                    "cumulative_ms": i.cumulative_time_ms,
                }
                for i in self.top_imports()
            ],
        }
