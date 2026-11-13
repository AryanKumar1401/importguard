"""Human-readable renderings of a Report (text for terminals, markdown for PRs)."""

from __future__ import annotations

import os
import sys
from typing import Any

from .findings import Finding, Report


def _unicode_ok() -> bool:
    enc = (getattr(sys.stdout, "encoding", None) or "").lower()
    return "utf" in enc


class _Style:
    def __init__(self, color: bool, uni: bool) -> None:
        self.color = color
        self.ok = "✓" if uni else "OK"
        self.bad = "✗" if uni else "X"
        self.skip = "·" if uni else "-"
        self.arrow = "→" if uni else "->"
        self.dot = "·" if uni else "|"

    def c(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def sev(self, severity: str) -> str:
        codes = {"error": "31;1", "warning": "33;1", "info": "36"}
        return self.c(f"{severity:<7}", codes.get(severity, "0"))


def _style() -> _Style:
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    return _Style(color, _unicode_ok())


def _finding_text(f: Finding, s: _Style, multi: bool) -> list[str]:
    head = f"{s.sev(f.severity)}"
    if multi:
        head += f" {s.c(f.entrypoint, '1')}"
    lines = [f"{head}  {f.message}"]
    pad = " " * 9
    hops = [st for st in f.chain if st.file]
    if hops:
        width = max(len(f"{st.file}:{st.line}") for st in hops)
        for st in hops:
            loc = f"{st.file}:{st.line}"
            lines.append(f"{pad}{loc:<{width}} {s.arrow} {st.module}")
    tb = f.data.get("traceback") if f.data else None
    if tb:
        lines.extend(pad + ln for ln in str(tb).splitlines())
    failure = f.data.get("failure") if f.data else None
    if failure:
        lines.extend(pad + "| " + ln for ln in str(failure).splitlines()[-6:])
    for i, r in enumerate(f.remedies):
        label = "fix:" if i == 0 else "or: "
        if r.command and not r.file:
            lines.append(f"{pad}{label} {r.command}")
            continue
        lines.append(f"{pad}{label} {r.summary}")
        if r.replacement:
            lines.append(f"{pad}      {s.c(r.replacement, '32')}")
    return lines


def render_text(report: Report, *, quiet: bool = False) -> str:
    s = _style()
    out: list[str] = []
    title = f"importguard {report.command}"
    if report.python:
        title += f" {s.dot} Python {report.python}"
    base = report.data.get("base") if report.command == "diff" else None
    if base:
        title += f" {s.dot} vs {base['sha'][:10]} ({base['ref']})"
        if report.data.get("policy_source"):
            title += f" {s.dot} policy from {report.data['policy_source']}"
    if report.tool_error:
        return f"{s.c('importguard: error', '31;1')}: {report.tool_error}\n"
    if not quiet:
        out.append(s.c(title, "1"))
        out.append("")

    renderer = _COMMAND_RENDERERS.get(report.command)
    if renderer is not None:
        out.extend(renderer(report, s))
    elif report.entrypoints and not quiet:
        width = max(len(e.name) for e in report.entrypoints)
        for e in report.entrypoints:
            if e.status == "skipped":
                out.append(f"{s.skip} {e.name:<{width}}  skipped ({e.note})")
                continue
            mark = s.c(s.ok, "32") if e.status == "pass" else s.c(s.bad, "31")
            word = "base" if report.command == "diff" else "locked"
            locked = f" ({word} {e.locked_modules})" if e.locked_modules is not None else ""
            out.append(
                f"{mark} {e.name:<{width}}  {e.modules} modules{locked} {s.dot} {e.total_ms:.0f}ms"
            )
            if e.note:
                out.append(f"  {' ' * width}  {e.note}")
        out.append("")

    multi = len({f.entrypoint for f in report.findings}) > 1
    shown = [
        f
        for f in report.findings
        if not (quiet and f.severity == "info" and not f.kind.startswith("lazy_"))
    ]
    order = {"error": 0, "warning": 1, "info": 2}
    for f in sorted(shown, key=lambda f: order.get(f.severity, 3)):
        out.extend(_finding_text(f, s, multi))
        out.append("")

    counts = [
        f"{report.count(k)} {k}{'s' if report.count(k) != 1 else ''}" for k in ("error", "warning")
    ]
    informational = report.command in ("why", "show", "snapshot") and not report.findings
    if (not quiet or not report.ok) and not informational:
        out.append(f" {s.dot} ".join(counts) + f" {s.dot} {report.count('info')} info")
    return "\n".join(out).rstrip() + "\n"


def _render_why(report: Report, s: _Style) -> list[str]:
    d = report.data
    if not d.get("found"):
        return [f"{d['module']} is not imported when importing {d['entrypoint']}", ""]
    lines = [f"{d['entrypoint']} imports {d['module']} via:"]
    locs = [
        f"{step['file']}:{step['line']}" if step.get("file") else "(entrypoint)"
        for step in d["chain"]
    ]
    width = max(len(loc) for loc in locs)
    for loc, step in zip(locs, d["chain"], strict=True):
        lines.append(f"  {loc:<{width}} {s.arrow} {step['module']}")
    lines.append("")
    if d.get("subtree_modules"):
        lines.append(
            f"{d['module']} brings {d['subtree_modules']} module(s) "
            f"(~{d['cumulative_ms']:.0f}ms cumulative)"
        )
        lines.append("")
    return lines


def _render_show(report: Report, s: _Style) -> list[str]:
    d = report.data
    lines = [
        f"{d['entrypoint']}: {d['total_modules']} modules {s.dot} {d['total_ms']:.0f}ms "
        f"{s.dot} {d['counts']['first_party']} first-party, "
        f"{d['counts']['third_party']} third-party, {d['counts']['stdlib']} stdlib",
        "",
        f"  {'cumulative':>10}  {'self':>7}  module",
    ]
    for m in d["modules"]:
        lines.append(
            f"  {m['cumulative_ms']:>8.1f}ms {m['self_ms']:>6.1f}ms  {m['name']}  [{m['kind']}]"
        )
    if d.get("truncated"):
        lines.append(f"  ... {d['truncated']} more (use --all)")
    lines.append("")
    return lines


def _render_lazy(report: Report, s: _Style) -> list[str]:
    d = report.data
    if not d:
        return []
    lines = [
        f"{d['entrypoint']}: {d['eager']['modules']} modules eager",
    ]
    if d.get("lazy"):
        which = "the safe set" if d.get("tested") else "all candidates (untested)"
        lines[0] += (
            f" {s.arrow} {d['lazy']['modules']} with {which} lazy "
            f"(-{d['eager']['modules'] - d['lazy']['modules']} modules, "
            f"~{d['eager']['ms'] - d['lazy']['ms']:.0f}ms)"
        )
    lines.append(
        f"{d['candidates']} candidate(s) {s.dot} {d['test_runs']} test run(s)"
        + (f" {s.dot} {'; '.join(d['notes'])}" if d.get("notes") else "")
    )
    lines.append("")
    return lines


def _render_snapshot(report: Report, s: _Style) -> list[str]:
    lines = []
    for e in report.entrypoints:
        mark = s.c(s.ok, "32") if e.status == "pass" else s.c(s.bad, "31")
        pkgs = ", ".join(e.third_party) or "no third-party packages"
        lines.append(f"{mark} {e.name}: {e.modules} modules {s.dot} {pkgs}")
    if report.data.get("written"):
        lines.append("")
        lines.append(f"wrote {report.data['written']}")
    lines.append("")
    return lines


_COMMAND_RENDERERS: dict[str, Any] = {
    "why": _render_why,
    "show": _render_show,
    "verify-lazy": _render_lazy,
    "snapshot": _render_snapshot,
}


def render_markdown(report: Report) -> str:
    """Compact markdown for PR comments and GitHub step summaries."""
    if report.tool_error:
        return f"### importguard\n\n:x: {report.tool_error}\n"
    status = ":white_check_mark:" if report.ok else ":x:"
    out = [f"### {status} importguard {report.command}", ""]
    if report.command in _COMMAND_RENDERERS:
        # Informational commands: their text rendering, verbatim.
        text = render_text(report).rstrip()
        fence = "~~~~" if "```" in text else "```"
        return "\n".join([*out, fence, text, fence, ""])
    base = report.data.get("base") if report.command == "diff" else None
    if base:
        out += [f"Compared with `{base['sha'][:10]}` ({base['ref']}).", ""]
    if report.entrypoints:
        out += ["| entrypoint | status | modules | time |", "|---|---|---|---|"]
        for e in report.entrypoints:
            word = "base" if report.command == "diff" else "locked"
            locked = f" ({word} {e.locked_modules})" if e.locked_modules is not None else ""
            mods = "" if e.status == "skipped" else f"{e.modules}{locked}"
            ms = "" if e.status == "skipped" else f"{e.total_ms:.0f}ms"
            out.append(f"| `{e.name}` | {e.status} | {mods} | {ms} |")
        out.append("")
    icons = {"error": ":x:", "warning": ":warning:", "info": ":information_source:"}
    for f in report.findings:
        approved = bool(f.data.get("approved"))
        if (
            f.severity == "info"
            and not approved
            and f.kind not in ("lazy_safe", "lazy_unexercised")
        ):
            continue
        icon = ":white_check_mark:" if approved else icons.get(f.severity, "")
        out.append(f"{icon} **{f.entrypoint}**: {f.message}")
        hops = [st for st in f.chain if st.file]
        if hops:
            out.append("")
            out.append("```")
            for st in hops:
                out.append(f"{st.file}:{st.line} -> {st.module}")
            out.append("```")
        for r in f.remedies[:2]:
            if r.command and not r.file:
                out.append(f"- `{r.command}`")
            else:
                out.append(f"- {r.summary}")
                if r.replacement:
                    out.append(f"  `{r.replacement}`")
        out.append("")
    out.append(
        f"<sub>{report.count('error')} error(s), {report.count('warning')} warning(s). "
        "Timing is advisory; module sets are deterministic.</sub>"
    )
    return "\n".join(out) + "\n"
