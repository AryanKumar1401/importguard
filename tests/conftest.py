"""Fixtures that build throwaway projects with first- and third-party code."""

from __future__ import annotations

import io
import json
import os
import subprocess
import textwrap
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class Project:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.site = root.parent / f"{root.name}-site" / "site-packages"

    def write(self, rel: str, body: str = "") -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
        return path

    def dist(self, rel: str, body: str = "") -> Path:
        """Write a file into the fake site-packages (third-party code)."""
        path = self.site / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
        return path

    def run(self, *args: str) -> tuple[int, str]:
        from importguard.cli import main

        buf = io.StringIO()
        old = os.getcwd()
        os.chdir(self.root)
        try:
            with redirect_stdout(buf):
                code = main(list(args))
        finally:
            os.chdir(old)
        return code, buf.getvalue()

    def run_json(self, *args: str) -> tuple[int, dict]:  # type: ignore[type-arg]
        code, out = self.run(*args, "--json")
        return code, json.loads(out)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Project:
    root = tmp_path / "proj"
    root.mkdir()
    proj = Project(root)
    proj.site.mkdir(parents=True)
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join([str(proj.site), os.environ.get("PYTHONPATH", "")])
    )
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.delenv("IMPORTGUARD_FORMAT", raising=False)
    proj.write(
        "pyproject.toml",
        """
        [project]
        name = "app"
        version = "0"
        [project.scripts]
        app = "app.cli:main"
        """,
    )
    proj.write("src/app/__init__.py", "import lightdep\n")
    proj.write(
        "src/app/cli.py",
        """
        import argparse
        from app import core

        def main():
            return core.run()
        """,
    )
    proj.write("src/app/core.py", "def run():\n    return 1\n")
    proj.dist("lightdep/__init__.py", "X = 1\n")
    proj.dist("heavydep/__init__.py", "import json\nfrom heavydep import inner\n")
    proj.dist("heavydep/inner.py", "import decimal\n")
    return proj


def git(project: Project, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=project.root, check=True, capture_output=True, text=True
    ).stdout


def commit(project: Project, msg: str) -> None:
    git(project, "add", "-A")
    git(project, "commit", "-qm", msg)


@pytest.fixture
def repo(project: Project, monkeypatch: pytest.MonkeyPatch) -> Project:
    for var, val in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }.items():
        monkeypatch.setenv(var, val)
    git(project, "init", "-q", "-b", "main")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "base")
    return project
