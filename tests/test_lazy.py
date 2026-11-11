from __future__ import annotations

import shlex
import sys

import pytest

from importguard.lazy import Candidate, failure_excerpt, isolate_failures
from importguard.probe import Statement


def _cands(n: int) -> list[Candidate]:
    return [Candidate(Statement("m", f"x{i}", "", "m.py", i), []) for i in range(n)]


def test_isolate_single_culprit():
    cands = _cands(8)
    calls = []

    def fails(group):
        calls.append(len(group))
        return any(c.statement.name == "x5" for c in group)

    assert [c.statement.name for c in isolate_failures(cands, fails)] == ["x5"]
    assert len(calls) <= 2 * 3  # two probes per bisection level


def test_isolate_multiple_culprits():
    bad = {"x1", "x6"}
    found = isolate_failures(_cands(8), lambda g: any(c.statement.name in bad for c in g))
    assert {c.statement.name for c in found} == bad


def test_isolate_interaction_blames_group():
    # fails only when x0 and x3 are lazy together
    def fails(group):
        names = {c.statement.name for c in group}
        return {"x0", "x3"} <= names

    found = isolate_failures(_cands(4), fails)
    assert {"x0", "x3"} <= {c.statement.name for c in found}


def test_failure_excerpt_prefers_pytest_summary():
    out = "noise\n" * 50 + "==== short test summary info ====\nFAILED t.py::test_a - boom\n"
    assert failure_excerpt(out) == "FAILED t.py::test_a - boom"


def _plugin_project(project):
    project.write(
        "src/app/__init__.py",
        """
        REGISTRY = []
        """,
    )
    project.write(
        "src/app/cli.py",
        """
        import heavydep
        import app.plugins

        def main():
            return heavydep
        """,
    )
    project.write("src/app/plugins.py", "from app import REGISTRY\nREGISTRY.append('csv')\n")


def test_verify_lazy_finds_safe_and_unsafe(project, python315):
    _plugin_project(project)
    check = "import app, app.cli; assert app.REGISTRY == ['csv']; app.cli.main()"
    test_cmd = f"{shlex.quote(python315)} -c {shlex.quote(check)}"
    code, report = project.run_json(
        "verify-lazy", "app.cli", "--python", python315, "--test", test_cmd
    )
    assert code == 0, report
    by_subject = {f["subject"]: f for f in report["findings"]}
    assert by_subject["app.plugins"]["kind"] == "lazy_unsafe"
    safe = by_subject["heavydep"]
    assert safe["kind"] == "lazy_safe" and safe["confidence"] == "verified"
    assert safe["data"]["saves_modules"] >= 3
    assert safe["remedies"][0]["replacement"] == "lazy import heavydep"
    assert report["data"]["lazy"]["modules"] < report["data"]["eager"]["modules"]


def test_verify_lazy_without_tests_is_unverified(project, python315):
    _plugin_project(project)
    code, report = project.run_json("verify-lazy", "app.cli", "--python", python315)
    assert code == 0
    assert {f["kind"] for f in report["findings"]} == {"lazy_unexercised"}
    assert all(f["confidence"] == "unexercised" for f in report["findings"])


@pytest.mark.skipif(sys.version_info >= (3, 15), reason="host supports lazy imports")
def test_verify_lazy_rejects_old_interpreter(project):
    code, report = project.run_json("verify-lazy", "app.cli")
    assert code == 3
    assert "3.15" in report["tool_error"]


def test_verify_lazy_rejects_test_command_on_old_python(project, python315):
    _plugin_project(project)
    code, report = project.run_json(
        "verify-lazy", "app.cli", "--python", python315, "--test", "python3.11 -c pass"
    )
    assert code == 3
    assert "did not run under a Python with lazy imports" in report["tool_error"]
