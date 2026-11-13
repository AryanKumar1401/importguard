"""Locate the exact import statement behind a finding and suggest a lazy rewrite.

A replacement is only offered when it changes exactly what was measured: one
statement, importing one module (or one ``from`` import), so prefixing it with
``lazy`` does not also make unrelated imports lazy.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass
class Rewrite:
    source: str | None  # the statement as written
    replacement: str | None  # the suggested lazy form, if safe to suggest
    note: str | None = None  # why no replacement is offered


def _parse(path: str) -> tuple[ast.Module | None, str]:
    try:
        st = Path(path).stat()
    except OSError:
        return None, ""
    return _parse_cached(path, st.st_mtime_ns, st.st_size)


@lru_cache(maxsize=256)
def _parse_cached(path: str, mtime_ns: int, size: int) -> tuple[ast.Module | None, str]:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, ""
    try:
        return ast.parse(text), text
    except SyntaxError:  # e.g. `lazy import` parsed by a host older than 3.15
        return None, text


def _simple_statements_on(tree: ast.Module, line: int) -> list[ast.stmt]:
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt) or hasattr(node, "body"):
            continue  # compound statements (if/def/with/...) span many lines
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        if node.lineno <= line <= end:
            out.append(node)
    return out


def _imports(node: ast.stmt, module: str | None) -> bool:
    if module is None:
        return True
    if isinstance(node, ast.Import):
        return any(
            a.name == module or module.startswith(a.name + ".") or a.name.startswith(module + ".")
            for a in node.names
        )
    if isinstance(node, ast.ImportFrom):
        base = node.module or ""
        if node.level:  # relative: compare by suffix
            return (
                not base
                or module.endswith(base)
                or any(module.endswith(f"{base}.{a.name}".lstrip(".")) for a in node.names)
            )
        return (
            module == base
            or module.startswith(base + ".")
            or any(module == f"{base}.{a.name}" for a in node.names)
        )
    return False


def lazy_rewrite(root: Path, file: str | None, line: int | None, module: str | None) -> Rewrite:
    if not file or not line or file.startswith("<"):
        return Rewrite(None, None)
    path = Path(file) if Path(file).is_absolute() else root / file
    tree, text = _parse(str(path))
    if tree is None:
        lines = text.splitlines()
        src = lines[line - 1].strip() if 0 < line <= len(lines) else None
        return Rewrite(src, None, "could not parse the file to locate the statement")
    stmts = _simple_statements_on(tree, line)
    imports = [s for s in stmts if isinstance(s, (ast.Import, ast.ImportFrom))]
    if len(imports) > 1:
        imports = [s for s in imports if _imports(s, module)]
    if len(imports) != 1:
        return Rewrite(None, None, "could not identify a single import statement")
    node = imports[0]
    segment = ast.get_source_segment(text, node)
    if segment is None:
        return Rewrite(None, None)
    if getattr(node, "is_lazy", 0):
        return Rewrite(segment, None, "already lazy")
    if isinstance(node, ast.Import) and len(node.names) > 1:
        return Rewrite(
            segment,
            None,
            "this statement imports several modules; split it so only this one is made lazy",
        )
    return Rewrite(segment, "lazy " + segment)
