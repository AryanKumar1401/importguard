"""Fixtures that build throwaway projects with first- and third-party code."""

from __future__ import annotations

import io
import json
import os
import shutil
import textwrap
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def find_python315() -> str | None:
    candidates = [
        os.environ.get("IMPORTGUARD_TEST_PYTHON315"),
        shutil.which("python3.15"),
        str(Path.home() / ".local/bin/python3.15"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


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


@pytest.fixture
def python315() -> str:
    py = find_python315()
    if py is None:
        pytest.skip("needs a Python 3.15 interpreter (set IMPORTGUARD_TEST_PYTHON315)")
    return py
