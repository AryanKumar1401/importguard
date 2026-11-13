"""Turn a probe result plus policy and lock into findings."""

from __future__ import annotations

from pathlib import Path

from .config import Policy
from .findings import EntrypointSummary, Finding, Remedy, Step
from .lockfile import LockEntry
from .probe import Module, ProbeResult, relpath
from .sourcefix import lazy_rewrite

EFFECT_LABELS = {
    "network": "network access",
    "subprocess": "subprocess spawned",
    "file_write": "file written",
    "env": "environment modified",
    "thread": "thread started",
}


def source_line(root: Path, file: str | None, line: int | None) -> str | None:
    if not file or not line or file.startswith("<"):
        return None
    path = Path(file) if Path(file).is_absolute() else root / file
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    return lines[line - 1].strip() if 0 < line <= len(lines) else None


def chain_steps(chain: list[Module]) -> list[Step]:
    return [Step(m.name, m.parent, m.file, m.line) for m in chain]


def break_point(result: ProbeResult, chain: list[Module]) -> Module | None:
    """Deepest hop in ``chain`` whose import statement lives in first-party code.

    That is the line the project controls: deferring it removes the subject
    from startup (unless something else also imports it).
    """
    index = result.by_name()
    for m in reversed(chain):
        parent = index.get(m.parent) if m.parent else None
        if parent is not None and parent.kind == "first_party" and m.file:
            return m
    return None


def defer_remedy(result: ProbeResult, chain: list[Module], root: Path) -> Remedy | None:
    hop = break_point(result, chain)
    if hop is None:
        return None
    fix = lazy_rewrite(root, hop.file, hop.line, hop.name)
    summary = (
        f"defer the import at {hop.file}:{hop.line} - mark it `lazy` (Python 3.15+), "
        "list it in `__lazy_modules__`, or move it into the function that uses it"
    )
    if fix.note and fix.replacement is None and fix.source:
        summary += f" ({fix.note})"
    return Remedy(
        kind="defer_import",
        summary=summary,
        file=hop.file,
        line=hop.line,
        source=fix.source or source_line(root, hop.file, hop.line),
        replacement=fix.replacement,
    )


def accept_remedy(entrypoint: str) -> Remedy:
    return Remedy(
        kind="accept",
        summary="if this is intended, record it in the lockfile",
        command=f"importguard snapshot {entrypoint}",
    )


_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2, "ignore": 3}


def approval_remedy(packages: list[str]) -> Remedy:
    label = ", ".join(f"importguard:allow:{p}" for p in packages)
    return Remedy(
        kind="request_approval",
        summary=(
            "if this is intended, a maintainer approves it outside this change: "
            f"label the pull request `{label}` or add it to `allow` on the base branch. "
            "Agents should not approve their own changes."
        ),
    )


def _group_key(m: Module, index: dict[str, Module], force_package: bool = False) -> str:
    """One finding per place where new code enters startup.

    Modules loaded by the same import statement in first- or third-party code
    share a group (its chain shows the line to change). Stdlib modules pulled in by other stdlib or
    preloaded modules are grouped by package; modules with no recorded importer
    are grouped together (stdlib) or by package, instead of one finding each.
    """
    if m.parent and not force_package:
        importer = index.get(m.parent)
        if m.kind != "stdlib" or (importer is not None and importer.kind != "stdlib"):
            # One group per import statement (`import a.b` can load a, a.b, ...).
            return f"site:{m.parent}:{m.file}:{m.line}"
        return f"stdlib:{m.package}"
    if m.kind == "stdlib":
        return "stdlib:unattributed"
    return f"pkg:{m.package}"


def _lock_diff(
    entrypoint: str,
    result: ProbeResult,
    policy: Policy,
    lock_entry: LockEntry,
    root: Path,
    forbidden_hit: set[str],
    *,
    mode: str = "lock",
    allowed: frozenset[str] = frozenset(),
) -> list[Finding]:
    """New modules, grouped by the hop where each new subgraph enters startup.

    A "root" is a newly loaded module whose importer was already part of the
    locked startup graph. Everything reachable from it that is also new is
    attributed to it, so one added import yields one finding listing all it
    drags in, rather than one finding per transitive package.
    """
    findings: list[Finding] = []
    version_note = None
    if (
        mode == "lock"
        and lock_entry.python
        and lock_entry.python != ".".join(result.python.split(".")[:2])
    ):
        version_note = f"lock was made with Python {lock_entry.python}, now {result.python}"

    def is_new(m: Module) -> bool:
        if m.namespace or m.synthetic:
            return False  # a shared directory such as google/, or bookkeeping, not a package
        if m.kind == "first_party":
            return m.name not in lock_entry.first_party
        if m.kind == "third_party":
            return m.package not in lock_entry.third_party
        return m.package not in lock_entry.stdlib

    new = {m.name: m for m in result.modules if is_new(m)}
    claimed: set[str] = set()
    order = {x.name: i for i, x in enumerate(result.modules)}
    index = result.by_name()
    groups: dict[str, tuple[Module, list[Module]]] = {}

    def take(root_mod: Module, key: str) -> None:
        group = [
            x for x in result.subtree(root_mod.name) if x.name in new and x.name not in claimed
        ]
        if root_mod.name in new and root_mod.name not in claimed and root_mod not in group:
            group.append(root_mod)
        claimed.update(x.name for x in group)
        if key in groups:
            groups[key][1].extend(group)
        else:
            groups[key] = (root_mod, group)

    for m in result.modules:
        if m.name not in new or m.name in claimed or (m.parent and m.parent in new):
            continue
        take(m, _group_key(m, index))
    for m in result.modules:
        # Never reachable from a root (e.g. parents forming a cycle): still report.
        if m.name in new and m.name not in claimed:
            take(m, _group_key(m, index, force_package=True))

    # A group of modules with no recorded importer whose packages another group
    # already reports is part of that addition, not a separate one.
    for key in [k for k in groups if k.startswith("pkg:")]:
        pkg = key[4:]
        home = next(
            (k for k, (_, ms) in groups.items() if k != key and any(x.package == pkg for x in ms)),
            None,
        )
        if home is not None:
            groups[home][1].extend(groups.pop(key)[1])

    for m, members in groups.values():
        group = sorted({x.name: x for x in members}.values(), key=lambda x: order[x.name])
        pkgs: dict[str, list[str]] = {"third_party": [], "stdlib": [], "first_party": []}
        for x in group:
            bucket = pkgs[x.kind]
            label = x.name if x.kind == "first_party" else x.package
            if label not in bucket:
                bucket.append(label)
        if pkgs["third_party"]:
            kind, severity = "new_package", policy.new_packages
            headline = ", ".join(pkgs["third_party"])
            label = "new third-party package" + ("s" if len(pkgs["third_party"]) > 1 else "")
        elif pkgs["stdlib"]:
            kind, severity = "new_stdlib", policy.new_stdlib
            if version_note:
                severity = "info"
            headline = ", ".join(pkgs["stdlib"])
            label = "new stdlib module" + ("s" if len(pkgs["stdlib"]) > 1 else "")
        else:
            kind, severity = "new_first_party", policy.new_first_party
            headline = ", ".join(pkgs["first_party"])
            label = "new first-party module" + ("s" if len(pkgs["first_party"]) > 1 else "")
        if severity == "ignore":
            continue
        if kind == "new_package" and set(pkgs["third_party"]) <= forbidden_hit:
            continue  # already reported as forbidden
        approved = kind == "new_package" and ("*" in allowed or set(pkgs["third_party"]) <= allowed)
        if approved:
            severity = "info"
        extras = []
        public_std = [p for p in pkgs["stdlib"] if not p.startswith("_")]
        if kind == "new_package" and public_std:
            extras.append(
                "stdlib: "
                + ", ".join(public_std[:8])
                + (f" (+{len(public_std) - 8} more)" if len(public_std) > 8 else "")
            )
        if kind != "new_first_party" and pkgs["first_party"]:
            extras.append("first-party: " + ", ".join(pkgs["first_party"][:5]))
        chain = result.chain(m.name)
        ms = m.cumulative_us / 1000.0
        message = f"{label} at startup: {headline} (+{len(group)} modules, ~{ms:.0f}ms)"
        if approved:
            message = "approved: " + message
        if extras:
            message += "; also pulls in " + "; ".join(extras)
        remedies = [r for r in [defer_remedy(result, chain, root)] if r]
        if mode == "diff":
            if kind == "new_package" and not approved:
                remedies.append(approval_remedy(pkgs["third_party"]))
        else:
            remedies.append(accept_remedy(entrypoint))
        data: dict[str, object] = {
            "entry_module": m.name,
            "modules_added": len(group),
            "est_ms": round(ms, 1),
            "packages": pkgs,
            "approved": approved,
        }
        if version_note:
            data["note"] = version_note
        findings.append(
            Finding(
                id=f"{kind}:{entrypoint}:{m.name}",
                kind=kind,
                severity=severity,
                entrypoint=entrypoint,
                message=message,
                subject=m.name,
                chain=chain_steps(chain),
                data=data,
                remedies=remedies,
            )
        )

    current = {"third_party": result.packages("third_party"), "stdlib": result.packages("stdlib")}
    gone = sorted(set(lock_entry.third_party) - current["third_party"])
    if gone:
        findings.append(
            Finding(
                id=f"removed_package:{entrypoint}",
                kind="removed_package",
                severity="info",
                entrypoint=entrypoint,
                message=f"no longer imported at startup: {', '.join(gone)}",
                data={"packages": gone},
                remedies=[]
                if mode == "diff"
                else [
                    Remedy(
                        kind="accept",
                        summary="tighten the lockfile so they cannot silently come back",
                        command=f"importguard snapshot {entrypoint}",
                    )
                ],
            )
        )
    findings.sort(key=lambda f: _SEVERITY_RANK.get(f.severity, 3))
    return findings


def check_result(
    entrypoint: str,
    result: ProbeResult,
    policy: Policy,
    lock_entry: LockEntry | None,
    root: Path,
    *,
    lock_exists: bool,
    mode: str = "lock",
    allowed: frozenset[str] = frozenset(),
) -> tuple[EntrypointSummary, list[Finding]]:
    findings: list[Finding] = []
    summary = EntrypointSummary(
        name=entrypoint,
        status="pass",
        modules=len(result.modules),
        total_ms=round(result.total_ms, 1),
        third_party=sorted(result.packages("third_party")),
        locked_modules=lock_entry.modules if lock_entry else None,
    )

    if result.error is not None:
        tb_tail = "\n".join(result.error.traceback.strip().splitlines()[-6:])
        findings.append(
            Finding(
                id=f"import_failed:{entrypoint}",
                kind="import_failed",
                severity="error",
                entrypoint=entrypoint,
                message=f"importing {entrypoint} raised {result.error.type}: {result.error.message}",
                subject=entrypoint,
                data={"traceback": tb_tail},
                remedies=[Remedy(kind="fix_error", summary="fix the exception raised at import")],
            )
        )

    # Project code that ran at interpreter startup (sitecustomize, usercustomize,
    # .pth files) executes before the probe and is invisible to it.
    hooks = [m for m in result.startup if m.kind == "first_party"]
    if hooks:
        names = ", ".join(m.name for m in hooks)
        files = [relpath(m.origin, root) for m in hooks if m.origin]
        findings.append(
            Finding(
                id=f"preloaded_code:{entrypoint}",
                kind="preloaded_code",
                severity="error",
                entrypoint=entrypoint,
                message=(
                    f"project code runs at interpreter startup ({names}); importguard "
                    "cannot observe what it imports or does"
                ),
                subject=hooks[0].name,
                data={"modules": [m.name for m in hooks], "files": files},
                remedies=[
                    Remedy(
                        kind="investigate",
                        summary="remove the startup hook from the import path, or move "
                        "that code into an explicit initialisation step",
                        file=files[0] if files else None,
                    )
                ],
            )
        )

    # Forbidden modules (transitive).
    forbidden_hit: set[str] = set()
    for banned in policy.forbid:
        first = result.first_loaded(banned)
        if first is None:
            continue
        forbidden_hit.add(banned.partition(".")[0])
        chain = result.chain(first.name)
        remedies = [r for r in [defer_remedy(result, chain, root)] if r]
        findings.append(
            Finding(
                id=f"forbidden_import:{entrypoint}:{banned}",
                kind="forbidden_import",
                severity="error",
                entrypoint=entrypoint,
                message=f"{banned} is forbidden at startup but gets imported",
                subject=banned,
                chain=chain_steps(chain),
                data={
                    "modules": len(
                        [m for m in result.modules if m.name.partition(".")[0] == first.package]
                    ),
                    "est_ms": round(first.cumulative_us / 1000.0, 1),
                },
                remedies=remedies,
            )
        )

    # Lockfile diff, grouped by where new code enters the import graph.
    if lock_entry is not None and result.ok:
        findings.extend(
            _lock_diff(
                entrypoint,
                result,
                policy,
                lock_entry,
                root,
                forbidden_hit,
                mode=mode,
                allowed=allowed | frozenset(policy.allow),
            )
        )
    elif lock_entry is None and lock_exists and result.ok:
        findings.append(
            Finding(
                id=f"not_locked:{entrypoint}",
                kind="not_locked",
                severity="warning",
                entrypoint=entrypoint,
                message=f"{entrypoint} is not in the lockfile, so new imports are not gated",
                remedies=[accept_remedy(entrypoint)],
            )
        )

    # Side effects during import.
    seen_effects: set[tuple[str, str | None, str | None, int | None]] = set()
    for eff in result.effects:
        key = (eff.category, eff.module, eff.file, eff.line)
        if key in seen_effects:
            continue
        seen_effects.add(key)
        severity = "error" if eff.category in policy.forbid_effects else "warning"
        where = f"{eff.file}:{eff.line}" if eff.file else "unknown location"
        chain = result.chain(eff.module) if eff.module else []
        mod = result.by_name().get(eff.module or "")
        remedies = []
        if mod is not None and mod.kind == "first_party":
            remedies.append(
                Remedy(
                    kind="investigate",
                    summary=(
                        f"move this out of module scope in {eff.module} "
                        "(e.g. into a function called explicitly or on first use)"
                    ),
                    file=eff.file,
                    line=eff.line,
                    source=source_line(root, eff.file, eff.line),
                )
            )
        else:
            fix = defer_remedy(result, chain, root)
            if fix:
                remedies.append(fix)
        findings.append(
            Finding(
                id=f"side_effect:{entrypoint}:{eff.category}:{eff.module}:{eff.line}",
                kind="side_effect",
                severity=severity,
                entrypoint=entrypoint,
                message=(
                    f"{EFFECT_LABELS.get(eff.category, eff.category)} during import "
                    f"in {eff.module or '?'} ({where}): {eff.event} {eff.detail}"
                ),
                subject=eff.module,
                chain=chain_steps(chain),
                data={"category": eff.category, "event": eff.event, "detail": eff.detail},
                remedies=remedies,
            )
        )

    # Advisory time budget (timing is noisy; only an error when strict_time).
    if policy.max_ms is not None and result.ok and result.total_ms > policy.max_ms:
        heaviest = sorted(
            (m for m in result.modules if m.parent and result.by_name().get(m.parent)),
            key=lambda m: m.cumulative_us,
            reverse=True,
        )[:5]
        findings.append(
            Finding(
                id=f"slow_import:{entrypoint}",
                kind="slow_import",
                severity="error" if policy.strict_time else "warning",
                entrypoint=entrypoint,
                message=(
                    f"import took {result.total_ms:.0f}ms (budget {policy.max_ms:.0f}ms; "
                    "timing is advisory and varies between machines)"
                ),
                data={
                    "total_ms": round(result.total_ms, 1),
                    "budget_ms": policy.max_ms,
                    "heaviest": [
                        {"module": m.name, "cumulative_ms": round(m.cumulative_us / 1000, 1)}
                        for m in heaviest
                    ],
                },
            )
        )

    if result.error is not None:
        summary.status = "error"
    elif any(f.severity == "error" for f in findings):
        summary.status = "fail"
    return summary, findings
