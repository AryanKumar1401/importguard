"""Parse Python's -X importtime output."""

from __future__ import annotations

import re

from .models import ImportTiming

# Pattern to match importtime output lines
# Example: "import time:       234 |        567 | module.name"
IMPORTTIME_PATTERN = re.compile(r"^import time:\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\s*)(.+)$")


def parse_importtime_output(stderr: str) -> tuple[list[ImportTiming], int]:
    """
    Parse the stderr output from `python -X importtime -c "import module"`.

    Returns:
        Tuple of (list of ImportTiming objects, total import time in microseconds)
    """
    timings: list[ImportTiming] = []
    total_time_us = 0

    for line in stderr.splitlines():
        match = IMPORTTIME_PATTERN.match(line)
        if match:
            self_time_us = int(match.group(1))
            cumulative_time_us = int(match.group(2))
            # indent = match.group(3)  # Can be used to build import tree
            module_name = match.group(4).strip()

            timing = ImportTiming(
                module=module_name,
                self_time_us=self_time_us,
                cumulative_time_us=cumulative_time_us,
            )
            timings.append(timing)

            # The root import(s) will have the highest cumulative time
            # We track the max as the total time
            if cumulative_time_us > total_time_us:
                total_time_us = cumulative_time_us

    return timings, total_time_us


def find_banned_imports(timings: list[ImportTiming], banned: set[str]) -> list[str]:
    """
    Find any banned modules in the import timings.

    Args:
        timings: List of import timings
        banned: Set of banned module names (can be prefixes like 'pandas')

    Returns:
        List of banned modules that were imported
    """
    found: list[str] = []

    for timing in timings:
        module = timing.module
        # Check exact match or if module starts with banned prefix
        for banned_module in banned:
            if module == banned_module or module.startswith(f"{banned_module}."):
                if banned_module not in found:
                    found.append(banned_module)
                break

    return found
