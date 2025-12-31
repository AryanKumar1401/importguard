"""Command-line interface for importguard."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import ImportGuardConfig, find_config, load_config
from .core import check_import
from .models import ImportResult


def format_time(ms: float) -> str:
    """Format time in milliseconds for display."""
    if ms >= 1000:
        return f"{ms / 1000:.1f}s"
    return f"{ms:.0f}ms"


def print_result(
    result: ImportResult,
    max_ms: float | None = None,
    top_n: int = 10,
    quiet: bool = False,
) -> None:
    """Print the result of an import check to stdout."""
    if quiet and result.passed:
        return

    # Color codes
    green = "\033[32m"
    red = "\033[31m"
    reset = "\033[0m"

    # Handle import failure specially
    if result.import_failed:
        print(f"{red}✗ FAIL:{reset} {result.module} failed to import")
        if result.error_message:
            # Indent error message lines
            for line in result.error_message.splitlines():
                print(f"  {red}→{reset} {line}")
        return

    # Status line for successful import
    if result.passed:
        status = "✓"
        color = green
    else:
        status = "✗ FAIL:"
        color = red

    time_str = format_time(result.total_ms)

    # Build status message
    msg_parts = [f"{result.module} imported in {time_str}"]

    if max_ms is not None:
        msg_parts.append(f"(budget: {format_time(max_ms)})")

    if result.num_runs > 1 and result.median_ms is not None:
        msg_parts.append(f"[median of {result.num_runs} runs]")

    print(f"{color}{status}{reset} {' '.join(msg_parts)}")

    # Print violations
    for violation in result.violations:
        print(f"  {color}→{reset} {violation}")

    # Print top imports (only if showing details)
    if result.imports and not quiet:
        print()
        print(f"Top {min(top_n, len(result.imports))} slowest imports:")
        for i, timing in enumerate(result.top_imports(top_n), 1):
            print(f"  {i:2}. {timing.module:<40} {format_time(timing.self_time_ms)}")


def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(
        prog="importguard",
        description="Measure and enforce import-time behavior in Python projects",
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # check command
    check_parser = subparsers.add_parser(
        "check",
        help="Check import time and violations for a module",
    )
    check_parser.add_argument(
        "module",
        help="Python module to check (e.g., mypkg, mypkg.cli)",
    )
    check_parser.add_argument(
        "--max-ms",
        type=float,
        metavar="MS",
        help="Fail if import exceeds this threshold",
    )
    check_parser.add_argument(
        "--ban",
        action="append",
        dest="banned",
        metavar="MODULE",
        help="Ban a module from being imported (can repeat)",
    )
    check_parser.add_argument(
        "--config",
        type=Path,
        metavar="FILE",
        help="Path to .importguard.toml config file",
    )
    check_parser.add_argument(
        "--top",
        type=int,
        default=10,
        metavar="N",
        help="Show top N slowest imports (default: 10)",
    )
    check_parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON (for CI parsing)",
    )
    check_parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only output on failure",
    )
    check_parser.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="Exit non-zero on warnings, not just errors",
    )
    check_parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        metavar="K",
        help="Run K times in fresh subprocesses, report median (reduces noise)",
    )
    check_parser.add_argument(
        "--python",
        type=str,
        metavar="PATH",
        help="Use specific Python interpreter (e.g., /usr/bin/python3.11)",
    )

    return parser


def cmd_check(args: argparse.Namespace) -> int:
    """Execute the check command."""
    module: str = args.module
    max_ms: float | None = args.max_ms
    banned: list[str] = args.banned or []
    config_path: Path | None = args.config
    top_n: int = args.top
    output_json: bool = args.json
    quiet: bool = args.quiet
    repeat: int = args.repeat
    python_path: str | None = args.python

    # Load config if specified or auto-discover
    config: ImportGuardConfig | None = None
    if config_path:
        if not config_path.exists():
            print(f"Error: Config file not found: {config_path}", file=sys.stderr)
            return 1
        config = load_config(config_path)
    else:
        found_config = find_config()
        if found_config:
            config = load_config(found_config)

    # Merge config with CLI options
    if config:
        module_config = config.get_module_config(module)
        if max_ms is None:
            max_ms = module_config.max_ms
        banned = list(set(banned) | module_config.banned)

    # Run the check
    result = check_import(
        module=module,
        max_ms=max_ms,
        banned=set(banned) if banned else None,
        python_path=python_path,
        repeat=repeat,
    )

    # Output
    if output_json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print_result(result, max_ms=max_ms, top_n=top_n, quiet=quiet)

    # Exit code
    return 0 if result.passed else 1


def main(argv: list[str] | None = None) -> int:
    """Main entry point."""
    parser = create_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "check":
        return cmd_check(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
