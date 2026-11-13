"""Regression tests for findings from the adversarial review.

Each test reproduces a confirmed finding; the ids in the names (A1..A14 for the
engine/diff review, B1..B15 for the verify-lazy/CLI review) map to the review.
"""

from __future__ import annotations

import os
import shlex
import time
from pathlib import Path

import pytest

from importguard.lazy import Candidate, isolate_failures
from importguard.probe import Statement, run_probe
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
    repo.write("pyproject.toml", POLICY + f'python = "{fake}"\n')
    repo.write("src/app/core.py", "import heavydep\n")
    code, report = repo.run_json("diff")
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
    code, report = repo.run_json("diff", "app.cli")
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
    code, report = repo.run_json("diff", "app.cli")
    assert code == 1
    assert "new_package" in _kinds(report)


def test_a5_base_that_cannot_import_fails_closed(repo):
    repo.write("src/app/cli.py", "import heavydep\nraise RuntimeError('broken on base')\n")
    commit(repo, "broken base")
    repo.write("src/app/cli.py", "import heavydep\n")
    code, report = repo.run_json("diff", "app.cli")
    [e] = report["entrypoints"]
    assert "everything it loads counts as new" in e["note"]
    assert code == 1


def test_a6_namespace_packages_are_compared_per_distribution(repo):
    repo.dist("google/protobuf/__init__.py", "")
    repo.dist("google/cloud/storage/__init__.py", "")
    repo.write("src/app/core.py", "import google.protobuf\n")
    commit(repo, "uses protobuf")
    repo.write("src/app/core.py", "import google.protobuf\nimport google.cloud.storage\n")
    code, report = repo.run_json("diff", "app.cli")
    assert code == 1
    [f] = [f for f in report["findings"] if f["kind"] == "new_package"]
    assert f["data"]["packages"]["third_party"] == ["google.cloud.storage"]


def test_a7_new_file_shadowing_a_startup_module_rechecks(project):
    project.run("snapshot")
    project.write("src/lightdep.py", "import heavydep\n")
    code, report = project.run_json("check", "--changed", "src/lightdep.py")
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
    code, report = repo.run_json("diff", "app.cli")
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
    code, _ = project.run_json("check", "--changed", str(project.root / "src/app/core.py"))
    assert code == 1
    monkeypatch.chdir(project.root / "src" / "app")
    from importguard.cli import main

    assert main(["check", "--changed", "core.py", "-q"]) == 1


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


# --- verify-lazy -----------------------------------------------------------------


def _cands(n: int) -> list[Candidate]:
    return [Candidate(Statement("m", f"x{i}", "", "m.py", i), []) for i in range(n)]


def test_b5_interaction_blames_only_interacting_imports():
    found = isolate_failures(_cands(40), lambda g: {"x0", "x39"} <= {c.statement.name for c in g})
    assert sorted(c.statement.name for c in found) == ["x0", "x39"]


def test_b13_max_candidates_must_be_positive(project):
    with pytest.raises(SystemExit):
        project.run("verify-lazy", "app.cli", "--max-candidates", "-1")


@pytest.mark.skipif(os.name != "posix", reason="process groups")
def test_b15_timeout_kills_the_whole_command(tmp_path):
    from importguard.lazy import _run_shell

    marker = tmp_path / "late"
    start = time.monotonic()
    passed, out = _run_shell(
        f"sleep 1.5 && touch {shlex.quote(str(marker))}",
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=0.3,
    )
    assert not passed and "timed out" in out
    assert time.monotonic() - start < 1.4
    time.sleep(1.6)
    assert not marker.exists()


def _lazy_project(project, cli: str) -> None:
    project.write("src/app/__init__.py", "REGISTRY = []\n")
    project.write("src/app/cli.py", cli)
    project.write("src/app/plugins.py", "from app import REGISTRY\nREGISTRY.append('csv')\n")


def test_b1_unused_import_is_not_verified(project, python315):
    _lazy_project(project, "import heavydep\n\ndef main():\n    return heavydep\n")
    test_cmd = f"{shlex.quote(python315)} -c 'import app.cli'"  # never calls main()
    code, report = project.run_json(
        "verify-lazy", "app.cli", "--python", python315, "--test", test_cmd
    )
    assert code == 0, report
    [f] = [f for f in report["findings"] if f["subject"] == "heavydep"]
    assert f["kind"] == "lazy_unexercised"
    assert "never used heavydep" in f["data"]["reason"]


def test_b2_explicit_lazy_imports_stay_lazy(project, python315):
    project.write("src/cyc/__init__.py", "")
    project.write("src/cyc/a.py", "lazy import cyc.b\nX = 1\n\ndef f():\n    return cyc.b.Y\n")
    project.write("src/cyc/b.py", "from cyc.a import X\nY = X + 1\n")
    _lazy_project(project, "import heavydep\nimport cyc.a\n\ndef main():\n    return heavydep\n")
    check = "import app.cli, cyc.a; assert cyc.a.f() == 2; app.cli.main()"
    test_cmd = f"{shlex.quote(python315)} -c {shlex.quote(check)}"
    code, report = project.run_json(
        "verify-lazy", "app.cli", "--python", python315, "--test", test_cmd
    )
    assert code == 0, report["tool_error"]


def test_b3_python_dash_m_runs_are_keyed_by_module(project, python315):
    _lazy_project(
        project,
        "import app.plugins\nfrom app import REGISTRY\n\n"
        "if __name__ == '__main__':\n    print(REGISTRY)\n",
    )
    check = (
        "import subprocess, sys, app.cli; "
        "out = subprocess.run([sys.executable, '-m', 'app.cli'], capture_output=True, "
        "text=True).stdout; assert 'csv' in out, out"
    )
    test_cmd = f"{shlex.quote(python315)} -c {shlex.quote(check)}"
    _, report = project.run_json(
        "verify-lazy", "app.cli", "--python", python315, "--test", test_cmd
    )
    [f] = [f for f in report["findings"] if f["subject"] == "app.plugins"]
    assert f["kind"] == "lazy_unsafe"


def test_b12_finding_ids_are_unique(project, python315):
    # two statements on one line differ only by their fromlist
    _lazy_project(project, "from email import message; from email import charset\n")
    _, report = project.run_json("verify-lazy", "app.cli", "--python", python315)
    ids = [f["id"] for f in report["findings"]]
    assert len(ids) == len(set(ids))


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
    script = text.split("```bash\n", 1)[1].split("```", 1)[0]
    assert "1) # violations" in script and "*) # usage/config/tool error" in script
