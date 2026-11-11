from __future__ import annotations

from importguard.probe import run_probe


def test_graph_records_kinds_and_provenance(project):
    project.write("src/app/core.py", "import heavydep\n")
    result = run_probe("app.cli", root=project.root)
    assert result.ok
    mods = result.by_name()
    assert mods["app.cli"].kind == "first_party"
    assert mods["heavydep"].kind == "third_party"
    assert mods["argparse"].kind == "stdlib"
    assert mods["heavydep"].parent == "app.core"
    assert (mods["heavydep"].file, mods["heavydep"].line) == ("src/app/core.py", 1)
    assert result.packages("third_party") == {"heavydep", "lightdep"}


def test_chain_walks_from_entrypoint(project):
    project.write("src/app/core.py", "import heavydep\n")
    chain = [m.name for m in run_probe("app.cli", root=project.root).chain("decimal")]
    assert chain[:4] == ["app.cli", "app.core", "heavydep", "heavydep.inner"]
    assert chain[-1] == "decimal"


def test_optional_import_attempts_are_not_loaded(project):
    project.write(
        "src/app/core.py",
        """
        try:
            import not_installed_anywhere
        except ImportError:
            pass
        """,
    )
    result = run_probe("app.core", root=project.root)
    assert "not_installed_anywhere" in result.attempted
    assert "not_installed_anywhere" not in result.by_name()


def test_import_error_is_captured(project):
    project.write("src/app/core.py", "raise RuntimeError('boom at import')\n")
    result = run_probe("app.core", root=project.root)
    assert not result.ok
    assert result.error is not None
    assert result.error.type == "RuntimeError"
    assert "boom at import" in result.error.message


def test_side_effects_are_attributed(project):
    project.write(
        "src/app/core.py",
        """
        import os, socket, subprocess, sys
        os.environ["APP_FLAG"] = "1"
        open("written.txt", "w").close()
        subprocess.run([sys.executable, "-c", "pass"])
        try:
            socket.create_connection(("127.0.0.1", 9), timeout=0.05)
        except OSError:
            pass
        """,
    )
    result = run_probe("app.core", root=project.root)
    cats = {(e.category, e.module, e.line) for e in result.effects}
    assert ("env", "app.core", 2) in cats
    assert ("file_write", "app.core", 3) in cats
    assert ("subprocess", "app.core", 4) in cats
    assert any(c == "network" and m == "app.core" for c, m, _ in cats)


def test_bytecode_cache_writes_are_not_side_effects(project, monkeypatch):
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE")
    project.write("src/app/core.py", "import heavydep\n")
    result = run_probe("app.core", root=project.root)
    assert not [e for e in result.effects if e.category == "file_write"]
