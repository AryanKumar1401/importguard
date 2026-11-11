"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .checks import check_result
from .config import Config, ConfigError, load_config
from .findings import (
    EXIT_TOOL_ERROR,
    EXIT_USAGE,
    REPORT_SCHEMA,
    EntrypointSummary,
    Report,
)
from .lazy import LazyError, lazy_findings, verify_lazy
from .lockfile import Lock, LockEntry, LockError, affected_entrypoints, load_lock
from .probe import ProbeError, run_probe
from .render import render_markdown, render_text

EPILOG = """\
exit codes: 0 ok, 1 policy violations, 2 usage/config error, 3 tool error.
Use --format json (or IMPORTGUARD_FORMAT=json) for machine-readable output;
`importguard schema` prints its JSON Schema."""


def _common() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument(
        "--format",
        choices=["text", "json", "markdown"],
        default=None,
        help="output format (default: text, or $IMPORTGUARD_FORMAT)",
    )
    p.add_argument("--json", action="store_true", help="shorthand for --format json")
    p.add_argument("--config", type=Path, help="path to pyproject.toml (default: search upward)")
    p.add_argument("--python", help="interpreter to probe with (default: config or current)")
    p.add_argument("-q", "--quiet", action="store_true", help="only print problems")
    return p


def build_parser() -> argparse.ArgumentParser:
    common = _common()
    parser = argparse.ArgumentParser(
        prog="importguard",
        description="Runtime ground truth for Python imports: see what loads at startup and "
        "why, gate new imports and import-time side effects, and prove which imports are "
        "safe to make lazy.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"importguard {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser(
        "snapshot",
        parents=[common],
        help="record what each entrypoint imports into importguard.lock",
    )
    p.add_argument("entrypoints", nargs="*", help="entrypoints to (re)record (default: all)")

    p = sub.add_parser(
        "check",
        parents=[common],
        help="fail on forbidden or unreviewed imports and import-time side effects",
    )
    p.add_argument("entrypoints", nargs="*", help="entrypoints to check (default: all)")
    p.add_argument(
        "--changed",
        nargs="*",
        metavar="FILE",
        help="only check entrypoints whose startup graph includes these files "
        "(no files: ask git for uncommitted changes)",
    )
    p.add_argument("--base", help="with --changed: also include changes since this git ref")
    p.add_argument("--strict", action="store_true", help="exit non-zero on warnings too")

    p = sub.add_parser("why", parents=[common], help="show the import chain to a module")
    p.add_argument("entrypoint")
    p.add_argument("module")

    p = sub.add_parser("show", parents=[common], help="list modules an entrypoint loads")
    p.add_argument("entrypoint")
    p.add_argument("--limit", type=int, default=25, help="rows to show (default 25)")
    p.add_argument("--all", action="store_true", help="show every module")
    p.add_argument(
        "--kind", choices=["first_party", "third_party", "stdlib"], help="filter by kind"
    )

    p = sub.add_parser(
        "effects", parents=[common], help="list side effects performed during import"
    )
    p.add_argument("entrypoints", nargs="*")

    p = sub.add_parser(
        "verify-lazy",
        parents=[common],
        help="find imports that are safe to make lazy (Python 3.15+), verified by your tests",
    )
    p.add_argument("entrypoint")
    p.add_argument(
        "--test",
        metavar="CMD",
        help="test command run under a 3.15+ interpreter, e.g. 'python3.15 -m pytest -x -q'",
    )
    p.add_argument("--max-candidates", type=int, default=40)
    p.add_argument("--test-timeout", type=float, default=900.0, help="seconds per test run")

    sub.add_parser("schema", help="print the JSON Schema of --format json output")
    return parser


def _fmt(args: argparse.Namespace) -> str:
    if getattr(args, "json", False):
        return "json"
    return getattr(args, "format", None) or os.environ.get("IMPORTGUARD_FORMAT") or "text"


def emit(report: Report, args: argparse.Namespace) -> None:
    fmt = _fmt(args)
    if fmt == "json":
        sys.stdout.write(json.dumps(report.to_dict(), indent=2) + "\n")
    elif fmt == "markdown":
        sys.stdout.write(render_markdown(report))
    else:
        sys.stdout.write(render_text(report, quiet=getattr(args, "quiet", False)))


def _select(config: Config, requested: Sequence[str]) -> list[str]:
    entrypoints = list(requested) or config.entrypoints
    if not entrypoints:
        raise ConfigError(
            "no entrypoints: pass one (e.g. `importguard check mypkg.cli`) or set "
            "`entrypoints = [...]` under [tool.importguard] in pyproject.toml"
        )
    return entrypoints


def git_changed(root: Path, base: str | None) -> list[str]:
    def git(*cmd: str) -> list[str]:
        out = subprocess.run(
            ["git", *cmd], cwd=root, capture_output=True, text=True, check=True
        ).stdout
        return [line for line in out.splitlines() if line]

    try:
        top = Path(git("rev-parse", "--show-toplevel")[0])
        files = git("diff", "--name-only", "HEAD") + git(
            "ls-files", "--others", "--exclude-standard"
        )
        if base:
            files += git("diff", "--name-only", f"{base}...HEAD")
    except (subprocess.CalledProcessError, OSError, IndexError) as exc:
        raise ConfigError(f"--changed needs a git checkout (or explicit files): {exc}") from exc
    out = []
    for f in dict.fromkeys(files):
        try:
            out.append((top / f).resolve().relative_to(root.resolve()).as_posix())
        except ValueError:
            continue
    return out


def cmd_snapshot(args: argparse.Namespace, config: Config, python: str | None) -> Report:
    report = Report(command="snapshot")
    lock = load_lock(config.lockfile) or Lock()
    targets = _select(config, args.entrypoints)
    failed = False
    for ep in targets:
        result = run_probe(
            ep,
            root=config.root,
            python=python,
            pythonpath=config.pythonpath,
            timeout=config.timeout,
        )
        report.python = result.python
        summary, findings = check_result(
            ep, result, config.policy(ep), None, config.root, lock_exists=False
        )
        report.entrypoints.append(summary)
        if result.error is not None:
            failed = True
            report.findings.extend(f for f in findings if f.kind == "import_failed")
            continue
        lock.entrypoints[ep] = LockEntry.from_probe(result, config.root)
    if not failed:
        lock.save(config.lockfile)
        report.data["written"] = str(config.lockfile)
    return report


def cmd_check(args: argparse.Namespace, config: Config, python: str | None) -> Report:
    report = Report(command="check")
    lock = load_lock(config.lockfile)
    targets = _select(config, args.entrypoints)
    selected, reason = targets, None
    if args.changed is not None:
        changed = args.changed or git_changed(config.root, args.base)
        selected, reason = affected_entrypoints(lock, targets, changed)
        report.data["changed_files"] = changed
    for ep in targets:
        if ep not in selected:
            report.entrypoints.append(
                EntrypointSummary(
                    name=ep, status="skipped", note="no changed file is in its startup graph"
                )
            )
            continue
        result = run_probe(
            ep,
            root=config.root,
            python=python,
            pythonpath=config.pythonpath,
            timeout=config.timeout,
        )
        report.python = result.python
        entry = lock.entrypoints.get(ep) if lock else None
        summary, findings = check_result(
            ep, result, config.policy(ep), entry, config.root, lock_exists=lock is not None
        )
        report.entrypoints.append(summary)
        report.findings.extend(findings)
    if lock is None:
        report.data["note"] = "no lockfile: run `importguard snapshot` to gate new imports"
    if reason:
        report.data["selection"] = reason
    return report


def cmd_why(args: argparse.Namespace, config: Config, python: str | None) -> Report:
    result = run_probe(
        args.entrypoint,
        root=config.root,
        python=python,
        pythonpath=config.pythonpath,
        timeout=config.timeout,
    )
    report = Report(command="why", python=result.python)
    target = result.first_loaded(args.module)
    data: dict[str, object] = {
        "entrypoint": args.entrypoint,
        "module": args.module,
        "found": target is not None,
    }
    if target is not None:
        data["chain"] = [
            {"module": m.name, "imported_by": m.parent, "file": m.file, "line": m.line}
            for m in result.chain(target.name)
        ]
        data["subtree_modules"] = len(result.subtree(target.name))
        data["cumulative_ms"] = round(target.cumulative_us / 1000, 1)
    report.data = data
    if result.error is not None:
        report.tool_error = (
            f"importing {args.entrypoint} failed: {result.error.type}: {result.error.message}"
        )
    return report


def cmd_show(args: argparse.Namespace, config: Config, python: str | None) -> Report:
    result = run_probe(
        args.entrypoint,
        root=config.root,
        python=python,
        pythonpath=config.pythonpath,
        timeout=config.timeout,
    )
    report = Report(command="show", python=result.python)
    modules = [m for m in result.modules if not args.kind or m.kind == args.kind]
    modules.sort(key=lambda m: m.cumulative_us, reverse=True)
    limit = len(modules) if args.all else max(args.limit, 0)
    report.data = {
        "entrypoint": args.entrypoint,
        "total_modules": len(result.modules),
        "total_ms": round(result.total_ms, 1),
        "counts": {
            k: sum(1 for m in result.modules if m.kind == k)
            for k in ("first_party", "third_party", "stdlib")
        },
        "modules": [
            {
                "name": m.name,
                "kind": m.kind,
                "imported_by": m.parent,
                "file": m.file,
                "line": m.line,
                "self_ms": round(m.self_us / 1000, 1),
                "cumulative_ms": round(m.cumulative_us / 1000, 1),
            }
            for m in modules[:limit]
        ],
        "truncated": max(len(modules) - limit, 0),
    }
    if result.error is not None:
        report.tool_error = (
            f"importing {args.entrypoint} failed: {result.error.type}: {result.error.message}"
        )
    return report


def cmd_effects(args: argparse.Namespace, config: Config, python: str | None) -> Report:
    report = Report(command="effects")
    for ep in _select(config, args.entrypoints):
        result = run_probe(
            ep,
            root=config.root,
            python=python,
            pythonpath=config.pythonpath,
            timeout=config.timeout,
        )
        report.python = result.python
        summary, findings = check_result(
            ep, result, config.policy(ep), None, config.root, lock_exists=False
        )
        report.entrypoints.append(summary)
        report.findings.extend(f for f in findings if f.kind in ("side_effect", "import_failed"))
        summary.status = (
            "error"
            if result.error
            else "fail"
            if any(f.severity == "error" for f in report.findings if f.entrypoint == ep)
            else "pass"
        )
    return report


def cmd_verify_lazy(args: argparse.Namespace, config: Config, python: str | None) -> Report:
    progress = None
    if _fmt(args) == "text" and not args.quiet:

        def progress(msg: str) -> None:
            sys.stderr.write(f"  ... {msg}\n")
            sys.stderr.flush()

    try:
        outcome = verify_lazy(
            args.entrypoint,
            root=config.root,
            python=python,
            pythonpath=config.pythonpath,
            test_command=args.test,
            max_candidates=args.max_candidates,
            probe_timeout=config.timeout,
            test_timeout=args.test_timeout,
            progress=progress,
        )
    except ProbeError as exc:
        if "does not support lazy imports" in str(exc):
            raise LazyError(
                "verify-lazy needs a Python 3.15+ interpreter (PEP 810). "
                "Point --python at one, e.g. --python python3.15"
            ) from exc
        raise
    report = Report(command="verify-lazy", python=outcome.eager.python)
    report.findings = lazy_findings(args.entrypoint, outcome, config.root, bool(args.test))
    eager_ms = outcome.eager.total_ms
    report.data = {
        "entrypoint": args.entrypoint,
        "eager": {"modules": len(outcome.eager.modules), "ms": round(eager_ms, 1)},
        "lazy": (
            {"modules": len(outcome.lazy.modules), "ms": round(outcome.lazy.total_ms, 1)}
            if outcome.lazy
            else None
        ),
        "candidates": len(outcome.candidates),
        "test_runs": outcome.test_runs,
        "notes": outcome.notes,
    }
    return report


COMMANDS = {
    "snapshot": cmd_snapshot,
    "check": cmd_check,
    "why": cmd_why,
    "show": cmd_show,
    "effects": cmd_effects,
    "verify-lazy": cmd_verify_lazy,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "schema":
        sys.stdout.write(json.dumps(REPORT_SCHEMA, indent=2) + "\n")
        return 0
    try:
        config = load_config(args.config)
        python = args.python or config.python
        report = COMMANDS[args.command](args, config, python)
    except (ConfigError, LockError) as exc:
        emit(Report(command=args.command, tool_error=str(exc)), args)
        return EXIT_USAGE
    except (ProbeError, LazyError) as exc:
        emit(Report(command=args.command, tool_error=str(exc)), args)
        return EXIT_TOOL_ERROR
    emit(report, args)
    return report.exit_code(strict=getattr(args, "strict", False))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
