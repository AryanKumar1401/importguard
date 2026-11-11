from __future__ import annotations

import json

from importguard.findings import REPORT_SCHEMA
from importguard.lockfile import Lock, LockEntry, affected_entrypoints


def test_entrypoints_inferred_and_snapshot_written(project):
    code, report = project.run_json("snapshot")
    assert code == 0
    assert [e["name"] for e in report["entrypoints"]] == ["app", "app.cli"]
    lock = json.loads((project.root / "importguard.lock").read_text())
    assert lock["entrypoints"]["app.cli"]["third_party"] == ["lightdep"]
    assert lock["entrypoints"]["app.cli"]["first_party"]["app.core"] == "src/app/core.py"


def test_clean_check_passes(project):
    project.run("snapshot")
    code, report = project.run_json("check")
    assert code == 0
    assert report["ok"] is True
    assert report["summary"]["error"] == 0


def test_new_dependency_fails_with_chain_and_fix(project):
    project.run("snapshot")
    project.write("src/app/core.py", "import heavydep\n\ndef run():\n    return 1\n")
    code, report = project.run_json("check")
    assert code == 1
    [finding] = [f for f in report["findings"] if f["kind"] == "new_package"]
    assert finding["severity"] == "error"
    assert finding["id"] == "new_package:app.cli:heavydep"
    assert finding["data"]["packages"]["third_party"] == ["heavydep"]
    assert "json" in finding["data"]["packages"]["stdlib"]
    assert [s["module"] for s in finding["chain"]] == ["app.cli", "app.core", "heavydep"]
    fix = finding["remedies"][0]
    assert (fix["kind"], fix["file"], fix["line"]) == ("defer_import", "src/app/core.py", 1)
    assert fix["replacement"] == "lazy import heavydep"
    assert finding["remedies"][-1]["command"] == "importguard snapshot app.cli"
    # stdlib modules pulled in by heavydep are folded into the same finding
    assert not [f for f in report["findings"] if f["kind"] == "new_stdlib"]


def test_accepting_via_snapshot_clears_finding(project):
    project.run("snapshot")
    project.write("src/app/core.py", "import heavydep\n")
    project.run("snapshot", "app.cli")
    code, _ = project.run_json("check")
    assert code == 0


def test_removed_package_is_info(project):
    project.run("snapshot")
    project.write("src/app/__init__.py", "")
    code, report = project.run_json("check")
    assert code == 0
    assert any(f["kind"] == "removed_package" for f in report["findings"])


def test_forbidden_is_transitive(project):
    project.write(
        "pyproject.toml",
        """
        [project]
        name = "app"
        version = "0"
        [tool.importguard]
        entrypoints = ["app.cli"]
        forbid = ["decimal"]
        """,
    )
    project.write("src/app/core.py", "import heavydep\n")
    code, report = project.run_json("check")
    assert code == 1
    [f] = [f for f in report["findings"] if f["kind"] == "forbidden_import"]
    assert f["subject"] == "decimal"
    assert f["chain"][-1]["module"] == "decimal"
    assert f["remedies"][0]["file"] == "src/app/core.py"


def test_side_effect_policy(project):
    project.write(
        "src/app/core.py",
        """
        import os, subprocess, sys
        os.environ["X"] = "1"
        subprocess.run([sys.executable, "-c", "pass"])
        """,
    )
    code, report = project.run_json("check", "app.cli")
    assert code == 1
    by_cat = {f["data"]["category"]: f for f in report["findings"] if f["kind"] == "side_effect"}
    assert by_cat["subprocess"]["severity"] == "error"
    assert by_cat["env"]["severity"] == "warning"
    assert by_cat["subprocess"]["remedies"][0]["line"] == 3


def test_strict_turns_warnings_into_failure(project):
    project.write("src/app/core.py", "import os\nos.environ['X'] = '1'\n")
    assert project.run("check", "app.cli")[0] == 0
    assert project.run("check", "app.cli", "--strict")[0] == 1


def test_import_failure_is_reported(project):
    project.write("src/app/core.py", "1/0\n")
    code, report = project.run_json("check", "app.cli")
    assert code == 1
    [f] = report["findings"]
    assert f["kind"] == "import_failed"
    assert "ZeroDivisionError" in f["message"]


def test_time_budget_is_advisory_unless_strict(project):
    project.write(
        "pyproject.toml",
        """
        [project]
        name = "app"
        version = "0"
        [tool.importguard]
        entrypoints = ["app.cli"]
        max_ms = 0.0001
        """,
    )
    code, report = project.run_json("check")
    assert code == 0
    assert report["findings"][0]["kind"] == "slow_import"
    assert report["findings"][0]["severity"] == "warning"


def test_changed_skips_unaffected_entrypoints(project):
    project.run("snapshot")
    _, report = project.run_json("check", "--changed", "src/app/core.py")
    status = {e["name"]: e["status"] for e in report["entrypoints"]}
    assert status == {"app": "skipped", "app.cli": "pass"}
    _, report = project.run_json("check", "--changed", "pyproject.toml")
    assert all(e["status"] != "skipped" for e in report["entrypoints"])


def test_affected_entrypoints_logic():
    lock = Lock(
        {
            "a": LockEntry("3.12", 1, [], [], {"a": "src/a/__init__.py"}),
            "a.cli": LockEntry(
                "3.12", 2, [], [], {"a": "src/a/__init__.py", "a.cli": "src/a/cli.py"}
            ),
        }
    )
    eps = ["a", "a.cli", "b"]
    assert affected_entrypoints(lock, eps, ["src/a/cli.py"])[0] == ["a.cli", "b"]
    assert affected_entrypoints(lock, eps, ["./src/a/__init__.py"])[0] == eps
    assert affected_entrypoints(lock, eps, ["docs/x.md"])[0] == ["b"]
    assert affected_entrypoints(lock, eps, ["uv.lock"])[0] == eps
    assert affected_entrypoints(None, eps, [])[0] == eps


def test_why_and_show(project):
    project.write("src/app/core.py", "import heavydep\n")
    code, report = project.run_json("why", "app.cli", "decimal")
    assert code == 0 and report["data"]["found"]
    assert report["data"]["chain"][2] == {
        "module": "heavydep",
        "imported_by": "app.core",
        "file": "src/app/core.py",
        "line": 1,
    }
    code, report = project.run_json("why", "app.cli", "sqlite3")
    assert report["data"]["found"] is False
    code, report = project.run_json("show", "app.cli", "--kind", "third_party")
    assert {m["name"] for m in report["data"]["modules"]} == {
        "heavydep",
        "heavydep.inner",
        "lightdep",
    }


def test_text_and_markdown_render(project):
    project.run("snapshot")
    project.write("src/app/core.py", "import heavydep\n")
    code, text = project.run("check")
    assert code == 1
    assert "new third-party package at startup: heavydep" in text
    assert "src/app/core.py:1" in text
    code, md = project.run("check", "--format", "markdown")
    assert md.startswith("### :x: importguard check")
    assert "`importguard snapshot app.cli`" in md


def test_config_errors_exit_2(project):
    project.write("pyproject.toml", "[tool.importguard]\nbogus = 1\n")
    code, report = project.run_json("check", "app")
    assert code == 2
    assert "bogus" in report["tool_error"]


def test_bad_interpreter_exits_3(project):
    code, report = project.run_json("check", "app", "--python", "/nonexistent/python")
    assert code == 3
    assert report["tool_error"]


def test_report_matches_schema_shape(project):
    project.write("src/app/core.py", "import heavydep\n")
    _, report = project.run_json("check")
    assert set(REPORT_SCHEMA["required"]) <= set(report)
    kinds = REPORT_SCHEMA["properties"]["findings"]["items"]["properties"]["kind"]["enum"]
    remedy_kinds = REPORT_SCHEMA["properties"]["findings"]["items"]["properties"]["remedies"][
        "items"
    ]["properties"]["kind"]["enum"]
    for f in report["findings"]:
        assert f["kind"] in kinds
        assert all(r["kind"] in remedy_kinds for r in f["remedies"])


def test_schema_command(project):
    code, out = project.run("schema")
    assert code == 0
    assert json.loads(out)["title"] == "importguard report"
