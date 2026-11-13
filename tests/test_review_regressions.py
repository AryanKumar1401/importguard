"""Regression tests for findings from the adversarial review.

Each test reproduces a confirmed finding; the ids in the names (A1..A14 for the
engine/diff review, B1..B15 for the CLI review; tests for the removed
verify-lazy command went with it) map to the review.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from importguard.probe import run_probe
from importguard.sourcefix import lazy_rewrite
from tests.conftest import commit

POLICY = '[project]\nname = "app"\nversion = "0"\n[tool.importguard]\nentrypoints = ["app.cli"]\n'


def _kinds(report):
    return {f["kind"] for f in report["findings"]}


# --- diff: the change must not choose what judges it ---------------------------


def test_a1_head_cannot_choose_the_interpreter(repo, tmp_path):
    repo.write("pyproject.toml", POLICY)
    commit(repo, "policy")
    fake = tmp_path / "fakepy"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    # TOML literal string: a Windows path has backslashes that a basic string
    # would read as escapes.
    repo.write("pyproject.toml", POLICY + f"python = '{fake}'\n")
    repo.write("src/app/core.py", "import heavydep\n")
    code, report = repo.run_json("diff", "--strict")
    assert code == 1
    assert "new_package" in _kinds(report)


def test_a2_sitecustomize_in_project_is_flagged(repo):
    repo.write("src/sitecustomize.py", "import heavydep\n")
    code, report = repo.run_json("check", "app.cli")
    assert code == 1
    [f] = [f for f in report["findings"] if f["kind"] == "preloaded_code"]
    assert f["data"]["files"] == ["src/sitecustomize.py"]


def test_a3_spoofed_name_does_not_hide_package(repo):
    repo.write(
        "src/app/core.py",
        '__name__ = "heavydep"\nimport heavydep\n__name__ = "app.core"\n',
    )
    code, report = repo.run_json("diff", "--strict", "app.cli")
    assert code == 1
    assert "heavydep" in {
        p
        for f in report["findings"]
        if f["kind"] == "new_package"
        for p in f["data"]["packages"]["third_party"]
    }


def test_a4_deleting_from_sys_modules_does_not_hide_package(repo):
    repo.dist("solodep/__init__.py", "X = 1\n")  # single module: nothing else reveals it
    repo.write("src/app/core.py", "import sys\nimport solodep\ndel sys.modules['solodep']\n")
    code, report = repo.run_json("diff", "--strict", "app.cli")
    assert code == 1
    assert "new_package" in _kinds(report)


def test_a5_base_that_cannot_import_fails_closed(repo):
    repo.write("src/app/cli.py", "import heavydep\nraise RuntimeError('broken on base')\n")
    commit(repo, "broken base")
    repo.write("src/app/cli.py", "import heavydep\n")
    code, report = repo.run_json("diff", "--strict", "app.cli")
    [e] = report["entrypoints"]
    assert "everything it loads counts as new" in e["note"]
    assert code == 1


def test_a6_namespace_packages_are_compared_per_distribution(repo):
    repo.dist("google/protobuf/__init__.py", "")
    repo.dist("google/cloud/storage/__init__.py", "")
    repo.write("src/app/core.py", "import google.protobuf\n")
    commit(repo, "uses protobuf")
    repo.write("src/app/core.py", "import google.protobuf\nimport google.cloud.storage\n")
    code, report = repo.run_json("diff", "--strict", "app.cli")
    assert code == 1
    [f] = [f for f in report["findings"] if f["kind"] == "new_package"]
    assert f["data"]["packages"]["third_party"] == ["google.cloud.storage"]


def test_a7_new_file_shadowing_a_startup_module_rechecks(project):
    project.run("snapshot")
    project.write("src/lightdep.py", "import heavydep\n")
    code, report = project.run_json("check", "--strict", "--changed", "src/lightdep.py")
    assert all(e["status"] != "skipped" for e in report["entrypoints"])
    assert code == 1


def test_a8_find_spec_lookup_does_not_steal_attribution(repo):
    repo.write(
        "src/app/core.py", "import importlib.util\nHAS = importlib.util.find_spec('heavydep')\n"
    )
    commit(repo, "probe for heavydep")
    repo.write("src/app/net.py", "import heavydep\n")
    repo.write(
        "src/app/cli.py",
        "import argparse\nfrom app import core\nfrom app import net\n\ndef main():\n    return 1\n",
    )
    _, report = repo.run_json("diff", "app.cli")
    [f] = [f for f in report["findings"] if f["kind"] == "new_package"]
    assert f["remedies"][0]["file"] == "src/app/cli.py"
    assert f["remedies"][0]["line"] == 3


def test_a9_gitignored_vendored_code_is_third_party(repo):
    repo.write(".gitignore", "vendor/\n")
    commit(repo, "ignore vendor")
    repo.write("vendor/vendeddep/__init__.py", "X = 1\n")
    repo.write(
        "pyproject.toml",
        POLICY + 'pythonpath = ["src", "vendor"]\n',
    )
    commit(repo, "vendor path")
    repo.write("src/app/core.py", "import vendeddep\n")
    code, report = repo.run_json("diff", "--strict", "app.cli")
    assert code == 1
    assert "new_package" in _kinds(report)


def test_a10_dns_and_sqlite_are_side_effects(project, tmp_path):
    db = tmp_path / "x.db"
    project.write(
        "src/app/core.py",
        f"import socket, sqlite3\nsocket.gethostbyname('localhost')\nsqlite3.connect({str(db)!r})\n",
    )
    result = run_probe("app.core", root=project.root)
    cats = {(e.category, e.event) for e in result.effects}
    assert ("network", "socket.gethostbyname") in cats
    assert ("file_write", "sqlite3.connect") in cats


def test_a11_thread_started_at_import_is_observed(project):
    project.write(
        "src/app/core.py",
        "import threading, time\n"
        "def work():\n    time.sleep(0.1)\n    import heavydep\n"
        "threading.Thread(target=work, daemon=True).start()\n",
    )
    result = run_probe("app.core", root=project.root)
    assert "heavydep" in result.by_name()


def test_a12_pycache_prefix_writes_are_ignored(project, monkeypatch, tmp_path):
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE")
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(tmp_path / "pyc"))
    project.write("src/app/core.py", "import heavydep\n")
    result = run_probe("app.core", root=project.root)
    assert not [e for e in result.effects if e.category == "file_write"]


def test_a13_one_finding_per_import_statement(repo):
    repo.write("src/app/core.py", "import importlib.util\n")
    _, report = repo.run_json("diff", "app.cli")
    stdlib = [f for f in report["findings"] if f["kind"] == "new_stdlib"]
    assert len(stdlib) <= 2  # the statement, plus at most one unattributed group
    assert {"importlib"} <= {p for f in stdlib for p in f["data"]["packages"]["stdlib"]}


# --- check --changed -----------------------------------------------------------


def test_b4_changed_accepts_absolute_and_cwd_relative_paths(project, monkeypatch):
    project.run("snapshot")
    project.write("src/app/core.py", "import heavydep\n")
    code, _ = project.run_json(
        "check", "--strict", "--changed", str(project.root / "src/app/core.py")
    )
    assert code == 1
    monkeypatch.chdir(project.root / "src" / "app")
    from importguard.cli import main

    assert main(["check", "--strict", "--changed", "core.py", "-q"]) == 1


def test_b4_changed_rejects_paths_outside_project(project, tmp_path):
    code, report = project.run_json("check", "--changed", str(tmp_path / "elsewhere.py"))
    assert code == 2
    assert "outside the project" in report["tool_error"]


# --- replacements ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "module", "replacement"),
    [
        (1, "json", "lazy import json"),
        (1, "decimal", "lazy import decimal"),
        (2, "json", None),  # import json, decimal: would make both lazy
        (3, "email", "lazy from email import (\n    message,\n)"),
        (6, "bigdep", "lazy import bigdep"),
    ],
)
def test_b6_replacement_targets_exactly_one_statement(tmp_path, line, module, replacement):
    (tmp_path / "m.py").write_text(
        "import decimal; import json\nimport json, decimal\nfrom email import (\n"
        "    message,\n)\nimport bigdep  # comment\n"
    )
    assert lazy_rewrite(tmp_path, "m.py", line, module).replacement == replacement


# --- CLI ---------------------------------------------------------------------------


def test_b9_internal_error_exits_3(project, monkeypatch):
    from importguard import cli

    def boom(*_a, **_k):
        raise RuntimeError("bug")

    monkeypatch.setitem(cli.COMMANDS, "show", boom)
    code, report = project.run_json("show", "app.cli")
    assert code == 3
    assert "internal error" in report["tool_error"]


def test_b11_markdown_for_informational_commands(project):
    project.write("src/app/core.py", "import heavydep\n")
    _, md = project.run("why", "app.cli", "decimal", "--format", "markdown")
    assert "src/app/core.py:1" in md


def test_hook_ignores_tool_errors(tmp_path):
    hook = Path(__file__).resolve().parents[1] / "docs" / "agents.md"
    text = hook.read_text()
    blocks = [b.split("```", 1)[0] for b in text.split("```bash\n")[1:]]
    [script] = [b for b in blocks if "stop_hook_active" in b]  # the Stop hook
    assert "1) # violations" in script and "*) # usage/config/tool error" in script


def test_base_needing_a_removed_dependency_is_compared_with_a_stub(repo):
    # Found replaying cookiecutter: a commit dropped typing_extensions; the
    # environment (head's deps) lacks it, so the base could not import and diff
    # failed closed with a false alarm.
    repo.dist("olddep/__init__.py", "class TypeAlias: pass\n")
    repo.write("src/app/core.py", "from olddep import TypeAlias\n")
    commit(repo, "uses olddep")
    (repo.site / "olddep" / "__init__.py").unlink()
    (repo.site / "olddep").rmdir()
    repo.write("src/app/core.py", "TypeAlias = object\n")
    code, report = repo.run_json("diff", "app.cli")
    assert code == 0, report["findings"]
    [e] = report["entrypoints"]
    assert "olddep" in e["note"] and "stubbed" in e["note"]


def test_submodule_registered_by_its_parent_joins_that_finding(repo):
    # Found in CI on GitHub's CPython 3.15.2 build: math.integer appeared in
    # sys.modules without an import statement (registered by C code during
    # math's init), so it had no recorded importer and was reported as a
    # separate "new stdlib module" instead of joining the package that pulled
    # math in. Simulated here with a stdlib-named child registered directly.
    repo.dist(
        "regdep/__init__.py",
        "import sys, json, importlib.machinery, types\n"
        "child = types.ModuleType('json.registered_child')\n"
        "child.__spec__ = importlib.machinery.ModuleSpec("
        "'json.registered_child', None, origin='built-in')\n"
        "sys.modules['json.registered_child'] = child\n",
    )
    repo.write("src/app/core.py", "import regdep\n")
    _, report = repo.run_json("diff", "app.cli")
    assert [f["kind"] for f in report["findings"]] == ["new_package"], report["findings"]
    [f] = report["findings"]
    assert "json" in f["data"]["packages"]["stdlib"]
