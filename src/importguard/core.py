"""Core subprocess runner for import timing."""

from __future__ import annotations

import subprocess
import sys

from .models import ImportResult, ImportTiming, Violation, ViolationType
from .parser import find_banned_imports, parse_importtime_output


def run_import_timing(
    module: str,
    python_path: str | None = None,
) -> tuple[list[ImportTiming], int]:
    """
    Run a subprocess to time the import of a module.

    Args:
        module: The module to import
        python_path: Path to Python interpreter (default: sys.executable)

    Returns:
        Tuple of (list of ImportTiming, total time in microseconds)
    """
    python = python_path or sys.executable

    result = subprocess.run(
        [python, "-X", "importtime", "-c", f"import {module}"],
        capture_output=True,
        text=True,
    )

    # importtime output goes to stderr
    timings, total_time_us = parse_importtime_output(result.stderr)

    return timings, total_time_us


def check_import(
    module: str,
    max_ms: float | None = None,
    banned: set[str] | None = None,
    python_path: str | None = None,
    repeat: int = 1,
) -> ImportResult:
    """
    Check the import time and violations for a module.

    Args:
        module: The module to check
        max_ms: Maximum allowed import time in milliseconds
        banned: Set of banned module names
        python_path: Path to Python interpreter
        repeat: Number of times to repeat the measurement

    Returns:
        ImportResult with timing and violation information
    """
    banned = banned or set()
    all_times_us: list[int] = []
    all_timings: list[ImportTiming] = []

    # Run measurements
    for _ in range(repeat):
        timings, total_time_us = run_import_timing(module, python_path)
        all_times_us.append(total_time_us)
        # Keep the timings from the last run for the detailed breakdown
        all_timings = timings

    # Use median for the reported time if multiple runs
    if repeat > 1:
        sorted_times = sorted(all_times_us)
        n = len(sorted_times)
        if n % 2 == 1:
            final_time_us = sorted_times[n // 2]
        else:
            final_time_us = (sorted_times[n // 2 - 1] + sorted_times[n // 2]) // 2
    else:
        final_time_us = all_times_us[0] if all_times_us else 0

    # Build result
    result = ImportResult(
        module=module,
        total_time_us=final_time_us,
        imports=all_timings,
        all_times_us=all_times_us,
        num_runs=repeat,
    )

    # Check for violations
    violations: list[Violation] = []

    # Check time budget
    if max_ms is not None:
        final_ms = final_time_us / 1000.0
        if final_ms > max_ms:
            violations.append(
                Violation(
                    type=ViolationType.EXCEEDED_BUDGET,
                    message=f"{module} imported in {final_ms:.0f}ms (budget: {max_ms:.0f}ms)",
                    module=module,
                    details=f"exceeded by {final_ms - max_ms:.0f}ms",
                )
            )

    # Check banned imports
    if banned:
        banned_found = find_banned_imports(all_timings, banned)
        result.banned_found = banned_found
        for banned_module in banned_found:
            violations.append(
                Violation(
                    type=ViolationType.BANNED_IMPORT,
                    message=f"{module} imports banned module: {banned_module}",
                    module=banned_module,
                )
            )

    result.violations = violations
    return result
