"""Core subprocess runner for import timing."""

from __future__ import annotations

import subprocess
import sys
import textwrap

from .models import ImportResult, ImportTiming, SubprocessResult, Violation, ViolationType
from .parser import find_banned_imports, parse_importtime_output, parse_wall_time_sentinel

# Timeout for subprocess execution (seconds)
DEFAULT_TIMEOUT = 60


def _build_import_script(module: str) -> str:
    """
    Build a Python script that imports a module and prints wall-time measurement.

    The script:
    1. Records start time with perf_counter_ns()
    2. Imports the target module
    3. Records end time
    4. Prints a sentinel line with wall time in microseconds

    This provides a backup timing measurement when -X importtime parsing fails
    or for sanity checking.
    """
    # Use raw string and textwrap.dedent for clean multi-line script
    script = textwrap.dedent(f"""\
        import time
        _start = time.perf_counter_ns()
        try:
            import {module}
            _end = time.perf_counter_ns()
            _wall_us = (_end - _start) // 1000
            print(f"__importguard_wall_time_us__:{{_wall_us}}")
        except Exception as e:
            _end = time.perf_counter_ns()
            _wall_us = (_end - _start) // 1000
            print(f"__importguard_wall_time_us__:{{_wall_us}}")
            print(f"__importguard_error__:{{type(e).__name__}}: {{e}}")
            raise
    """)
    return script


def run_import_subprocess(
    module: str,
    python_path: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> SubprocessResult:
    """
    Run a subprocess to time the import of a module.

    Uses Python's -X importtime flag to capture detailed timing, plus a
    wall-clock backup measurement using time.perf_counter_ns().

    Args:
        module: The module to import
        python_path: Path to Python interpreter (default: sys.executable)
        timeout: Maximum time to wait for subprocess (seconds)

    Returns:
        SubprocessResult with timing data, exit code, and any error info
    """
    python = python_path or sys.executable
    script = _build_import_script(module)

    try:
        result = subprocess.run(
            [python, "-X", "importtime", "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return SubprocessResult(
            exit_code=-1,
            stderr="",
            stdout="",
            importtime_us=0,
            wall_time_us=0,
            timings=[],
            error_message=f"Import timed out after {timeout}s",
            import_failed=True,
        )
    except OSError as e:
        return SubprocessResult(
            exit_code=-1,
            stderr="",
            stdout="",
            importtime_us=0,
            wall_time_us=0,
            timings=[],
            error_message=f"Failed to run Python interpreter: {e}",
            import_failed=True,
        )

    # Parse importtime output from stderr
    timings, importtime_us = parse_importtime_output(result.stderr)

    # Parse wall-time sentinel from stdout
    wall_time_us = parse_wall_time_sentinel(result.stdout)

    # Check for error sentinel in stdout
    error_message: str | None = None
    import_failed = result.returncode != 0

    for line in result.stdout.splitlines():
        if line.startswith("__importguard_error__:"):
            error_message = line[len("__importguard_error__:") :]
            import_failed = True
            break

    # If no sentinel error but returncode != 0, extract from stderr
    if import_failed and not error_message:
        # Look for traceback in stderr (after importtime lines)
        stderr_lines = result.stderr.splitlines()
        error_lines = []
        in_traceback = False
        for line in stderr_lines:
            if line.startswith("Traceback") or in_traceback:
                in_traceback = True
                error_lines.append(line)
        if error_lines:
            # Get the last few lines which typically contain the error
            error_message = "\n".join(error_lines[-5:])

    return SubprocessResult(
        exit_code=result.returncode,
        stderr=result.stderr,
        stdout=result.stdout,
        importtime_us=importtime_us,
        wall_time_us=wall_time_us,
        timings=timings,
        error_message=error_message,
        import_failed=import_failed,
    )


def run_import_timing(
    module: str,
    python_path: str | None = None,
) -> tuple[list[ImportTiming], int]:
    """
    Run a subprocess to time the import of a module.

    This is a simplified wrapper around run_import_subprocess for
    backwards compatibility.

    Args:
        module: The module to import
        python_path: Path to Python interpreter (default: sys.executable)

    Returns:
        Tuple of (list of ImportTiming, total time in microseconds)
    """
    result = run_import_subprocess(module, python_path)
    return result.timings, result.best_time_us


def check_import(
    module: str,
    max_ms: float | None = None,
    banned: set[str] | list[str] | None = None,
    python_path: str | None = None,
    repeat: int = 1,
) -> ImportResult:
    """
    Check the import time and violations for a module.

    Args:
        module: The module to check
        max_ms: Maximum allowed import time in milliseconds
        banned: Set or list of banned module names
        python_path: Path to Python interpreter
        repeat: Number of times to repeat the measurement

    Returns:
        ImportResult with timing and violation information

    Example:
        >>> from importguard import check_import
        >>> result = check_import("json", max_ms=100)
        >>> print(f"Import took {result.total_ms:.1f}ms")
        >>> if not result.passed:
        ...     for v in result.violations:
        ...         print(f"FAIL: {v}")
    """
    # Convert list to set if needed
    banned_set: set[str] = set(banned) if banned else set()
    all_times_us: list[int] = []
    all_timings: list[ImportTiming] = []
    last_subprocess_result: SubprocessResult | None = None

    # Run measurements
    for _ in range(repeat):
        subprocess_result = run_import_subprocess(module, python_path)
        last_subprocess_result = subprocess_result

        # Use best available time (importtime preferred, wall time as backup)
        all_times_us.append(subprocess_result.best_time_us)

        # Keep the timings from the last run for the detailed breakdown
        all_timings = subprocess_result.timings

        # If import failed, no point continuing
        if subprocess_result.import_failed:
            break

    # Use median for the reported time if multiple runs
    if len(all_times_us) > 1:
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
        num_runs=len(all_times_us),
        exit_code=last_subprocess_result.exit_code if last_subprocess_result else 0,
        error_message=last_subprocess_result.error_message if last_subprocess_result else None,
        import_failed=last_subprocess_result.import_failed if last_subprocess_result else False,
        wall_time_us=last_subprocess_result.wall_time_us if last_subprocess_result else 0,
    )

    # Check for violations
    violations: list[Violation] = []

    # Check if import failed
    if result.import_failed:
        violations.append(
            Violation(
                type=ViolationType.IMPORT_FAILED,
                message=f"Failed to import {module}",
                module=module,
                details=result.error_message,
            )
        )

    # Check time budget
    if max_ms is not None and not result.import_failed:
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
    if banned_set and not result.import_failed:
        banned_found = find_banned_imports(all_timings, banned_set)
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

    # Generate warnings (non-fatal issues)
    warnings: list[str] = []

    # Warn if using wall-time fallback (importtime failed to parse)
    if last_subprocess_result:
        if last_subprocess_result.importtime_us == 0 and last_subprocess_result.wall_time_us > 0:
            warnings.append("Using wall-clock time (importtime parsing failed)")

    # Warn if import time is close to budget (within 20%)
    if max_ms is not None and not result.import_failed:
        final_ms = final_time_us / 1000.0
        if final_ms <= max_ms and final_ms > max_ms * 0.8:
            headroom = max_ms - final_ms
            warnings.append(f"Import time is close to budget ({headroom:.0f}ms headroom)")

    result.warnings = warnings
    return result
