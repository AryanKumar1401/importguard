from __future__ import annotations

import pytest

from tests.conftest import commit, git


def test_clean_tree_passes_and_needs_no_setup(repo):
    code, report = repo.run_json("diff")
    assert code == 0
    assert report["data"]["policy_source"] == "base (defaults)"  # no [tool.importguard] on base
    assert {e["name"]: e["status"] for e in report["entrypoints"]} == {
        "app": "pass",
        "app.cli": "pass",
    }
    assert not (repo.root / "importguard.lock").exists()


def test_uncommitted_dependency_is_reported_with_approval_not_accept(repo):
    repo.write("src/app/core.py", "import heavydep\n")
    code, report = repo.run_json("diff", "--strict", "app.cli")
    assert code == 1
    [f] = [f for f in report["findings"] if f["kind"] == "new_package"]
    assert f["chain"][-1] == {
        "module": "heavydep",
        "imported_by": "app.core",
        "file": "src/app/core.py",
        "line": 1,
    }
    kinds = [r["kind"] for r in f["remedies"]]
    assert kinds == ["defer_import", "request_approval"]
    assert all("command" not in r for r in f["remedies"])  # nothing the agent can run
    assert report["entrypoints"][0]["locked_modules"] < report["entrypoints"][0]["modules"]


def test_existing_problems_are_not_reported(repo):
    repo.write(
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
    repo.write(
        "src/app/core.py",
        "import heavydep, subprocess, sys\nsubprocess.run([sys.executable, '-c', 'pass'])\n",
    )
    commit(repo, "legacy problems")
    code, report = repo.run_json("diff")
    assert code == 0, report["findings"]
    assert report["findings"] == []
    # ...while `check` (no baseline) does report them
    assert repo.run("check")[0] == 1


def test_new_side_effect_is_reported(repo):
    repo.write(
        "src/app/core.py",
        "import os, subprocess, sys\nsubprocess.run([sys.executable, '-c', 'pass'])\n",
    )
    code, report = repo.run_json("diff", "app.cli")
    assert code == 1
    assert [f["data"]["category"] for f in report["findings"] if f["kind"] == "side_effect"] == [
        "subprocess"
    ]


def _with_policy(repo):
    repo.write(
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
    commit(repo, "policy")


def test_change_cannot_loosen_its_own_policy(repo):
    _with_policy(repo)
    repo.write("src/app/core.py", "import heavydep\n")
    repo.write(
        "pyproject.toml",
        """
        [project]
        name = "app"
        version = "0"
        [tool.importguard]
        entrypoints = ["app.cli"]
        allow = ["heavydep"]
        """,
    )
    code, report = repo.run_json("diff", "--strict")
    assert code == 1
    kinds = {f["kind"]: f for f in report["findings"]}
    assert kinds["forbidden_import"]["severity"] == "error"
    assert kinds["new_package"]["severity"] == "warning"
    assert kinds["policy_changed"]["data"]["keys"] == ["allow", "forbid"]
    assert report["data"]["policy_source"] == "base"
    # opting into the head policy is explicit and visible
    code, report = repo.run_json("diff", "--policy-from", "head")
    assert code == 0
    assert report["data"]["policy_source"] == "head"


def test_removing_entrypoint_from_config_does_not_hide_it(repo):
    _with_policy(repo)
    repo.write("src/app/core.py", "import heavydep\n")
    repo.write(
        "pyproject.toml",
        '[project]\nname = "app"\nversion = "0"\n[tool.importguard]\nentrypoints = ["app"]\n',
    )
    code, report = repo.run_json("diff")
    assert code == 1
    assert "app.cli" in {e["name"] for e in report["entrypoints"]}


@pytest.mark.parametrize("how", ["flag", "env", "wildcard", "base-config"])
def test_approval_from_outside_the_change(repo, monkeypatch, how):
    args = ["diff", "app.cli"]
    if how == "flag":
        args += ["--allow", "heavydep"]
    elif how == "env":
        monkeypatch.setenv("IMPORTGUARD_ALLOW", "other, heavydep")
    elif how == "wildcard":
        args += ["--allow", "*"]
    else:
        repo.write(
            "pyproject.toml",
            '[project]\nname = "app"\nversion = "0"\n'
            '[tool.importguard]\nentrypoints = ["app.cli"]\nallow = ["heavydep"]\n',
        )
        commit(repo, "approve heavydep")
    repo.write("src/app/core.py", "import heavydep\n")
    code, report = repo.run_json(*args)
    assert code == 0
    [f] = [f for f in report["findings"] if f["kind"] == "new_package"]
    assert f["severity"] == "info" and f["data"]["approved"] is True
    assert f["message"].startswith("approved: ")


def test_uses_merge_base_not_base_tip(repo):
    git(repo, "checkout", "-q", "-b", "feature")
    repo.write("src/app/core.py", "import heavydep\n")
    commit(repo, "feature adds heavydep")
    git(repo, "checkout", "-q", "main")
    repo.dist("otherdep/__init__.py", "")
    repo.write(
        "src/app/cli.py", "import otherdep\nfrom app import core\n\ndef main():\n    return core\n"
    )
    commit(repo, "main moves on")
    git(repo, "checkout", "-q", "feature")
    _, report = repo.run_json("diff", "app.cli")  # default base: main
    subjects = {f["subject"] for f in report["findings"]}
    assert "heavydep" in subjects
    assert "otherdep" not in subjects
    assert not any(f["kind"] == "removed_package" for f in report["findings"])


def test_new_entrypoint_is_compared_with_other_entrypoints(repo):
    # app.tool does not exist on base; its packages are new unless another base
    # entrypoint already loads them (fail closed rather than skipping the gate).
    repo.write("src/app/tool.py", "import heavydep, lightdep\n")
    code, report = repo.run_json("diff", "--strict", "app.cli", "app.tool")
    tool = next(e for e in report["entrypoints"] if e["name"] == "app.tool")
    assert "not importable on base" in tool["note"]
    assert code == 1
    [f] = [f for f in report["findings"] if f["kind"] == "new_package"]
    # lightdep is already loaded by app.cli on base, so only heavydep is new
    assert f["data"]["packages"]["third_party"] == ["heavydep"]


def test_worktree_is_cleaned_up(repo):
    repo.write("src/app/core.py", "import heavydep\n")
    repo.run("diff")
    assert len(git(repo, "worktree", "list").strip().splitlines()) == 1


def test_markdown_mentions_base(repo):
    repo.write("src/app/core.py", "import heavydep\n")
    _, md = repo.run("diff", "app.cli", "--format", "markdown")
    assert "Compared with `" in md
    assert "importguard:allow:heavydep" in md


def test_not_a_git_repo(project):
    code, report = project.run_json("diff")
    assert code == 2
    assert "git" in report["tool_error"]


def test_unknown_base_ref(repo):
    code, report = repo.run_json("diff", "--base", "nope")
    assert code == 2
    assert "nope" in report["tool_error"]


def test_new_config_cannot_approve_itself(repo):
    # The base has no [tool.importguard]; the change adds one that allows everything.
    repo.write("src/app/core.py", "import heavydep\n")
    repo.write(
        "pyproject.toml",
        '[project]\nname = "app"\nversion = "0"\n'
        '[tool.importguard]\nentrypoints = ["app.cli"]\nallow = ["*"]\n',
    )
    code, report = repo.run_json("diff", "--strict")
    assert code == 1
    kinds = {f["kind"]: f for f in report["findings"]}
    assert kinds["new_package"]["severity"] == "warning"
    assert kinds["policy_changed"]["severity"] == "info"  # adoption, applies after merge
    assert report["data"]["policy_source"] == "base (defaults)"


def test_full_sha_base_is_shortened_in_the_label(repo):
    sha = git(repo, "rev-parse", "HEAD").strip()
    _, report = repo.run_json("diff", "--base", sha)
    assert report["data"]["base"]["ref"] == f"merge-base of HEAD and {sha[:10]}"


def test_relative_python_path_is_used_for_base_and_head(repo):
    # Found building the agent skill: `--python .venv/bin/python` resolved
    # inside the temporary base checkout, where the (git-ignored) venv does
    # not exist, so the base probe could not start.
    import os
    import sys

    repo.write(".gitignore", ".venv/\n")
    commit(repo, "ignore venv")
    (repo.root / ".venv" / "bin").mkdir(parents=True)
    os.symlink(sys.executable, repo.root / ".venv" / "bin" / "python")
    code, report = repo.run_json("diff", "app.cli", "--python", ".venv/bin/python")
    assert code == 0, report["tool_error"]
    assert report["entrypoints"][0]["status"] == "pass"
