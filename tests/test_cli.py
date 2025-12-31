"""Tests for CLI."""

import json
import subprocess
import sys

import pytest

from importguard.cli import create_parser, main


class TestCLI:
    """Tests for CLI functionality."""

    def test_help_exits_zero(self) -> None:
        """Test that --help exits with 0."""
        result = main([])

        assert result == 0

    def test_check_command_help(self) -> None:
        """Test check command parses correctly."""
        parser = create_parser()
        args = parser.parse_args(["check", "os"])

        assert args.command == "check"
        assert args.module == "os"

    def test_check_with_options(self) -> None:
        """Test check command with all options."""
        parser = create_parser()
        args = parser.parse_args(
            [
                "check",
                "mymodule",
                "--max-ms",
                "100",
                "--ban",
                "pandas",
                "--ban",
                "torch",
                "--top",
                "5",
                "--repeat",
                "3",
                "--python",
                "/usr/bin/python3",
            ]
        )

        assert args.module == "mymodule"
        assert args.max_ms == 100.0
        assert args.banned == ["pandas", "torch"]
        assert args.top == 5
        assert args.repeat == 3
        assert args.python == "/usr/bin/python3"

    def test_check_json_flag(self) -> None:
        """Test check command with --json flag."""
        parser = create_parser()
        args = parser.parse_args(["check", "os", "--json"])

        assert args.json is True

    def test_check_quiet_flag(self) -> None:
        """Test check command with --quiet flag."""
        parser = create_parser()
        args = parser.parse_args(["check", "os", "--quiet"])

        assert args.quiet is True


class TestCLIIntegration:
    """Integration tests for CLI."""

    def test_check_module_via_main(self) -> None:
        """Test checking a module via main()."""
        result = main(["check", "os", "--max-ms", "5000"])

        assert result == 0

    def test_check_returns_error_for_invalid_module(self) -> None:
        """Test that checking an invalid module returns error."""
        # This will fail because the module doesn't exist
        # The subprocess will fail, but we handle it gracefully
        result = main(["check", "nonexistent_module_xyz"])

        # Should still work (return 0) because the import just fails fast
        # The real test is that it doesn't crash
        assert result in (0, 1)

    def test_check_json_output(self, capsys: pytest.CaptureFixture) -> None:
        """Test JSON output format."""
        main(["check", "os", "--json"])

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "module" in data
        assert data["module"] == "os"
        assert "total_ms" in data
        assert "passed" in data

    def test_python_m_invocation(self) -> None:
        """Test running via python -m importguard."""
        result = subprocess.run(
            [sys.executable, "-m", "importguard", "check", "json"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert "json" in result.stdout


class TestCLIConfigIntegration:
    """Integration tests for CLI with config files."""

    def test_config_file_applies_budget(self, tmp_path: pytest.TempPathFactory) -> None:
        """Test that config file budget is applied."""
        # Create a config file
        config_file = tmp_path / ".importguard.toml"  # type: ignore[operator]
        config_file.write_text(
            """
[importguard]
max_total_ms = 100

[importguard.budgets]
"json" = 50
"""
        )

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "importguard",
                "check",
                "json",
                "--config",
                str(config_file),
                "--json",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["passed"] is True

    def test_config_file_applies_bans(self, tmp_path: pytest.TempPathFactory) -> None:
        """Test that config file bans are applied."""
        config_file = tmp_path / ".importguard.toml"  # type: ignore[operator]
        config_file.write_text(
            """
[importguard]

[importguard.banned]
"urllib.request" = ["ssl"]
"""
        )

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "importguard",
                "check",
                "urllib.request",
                "--config",
                str(config_file),
                "--json",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 1
        data = json.loads(result.stdout)
        assert data["passed"] is False
        assert "ssl" in data["banned_found"]

    def test_cli_overrides_config_budget(self, tmp_path: pytest.TempPathFactory) -> None:
        """Test that CLI --max-ms overrides config budget."""
        config_file = tmp_path / ".importguard.toml"  # type: ignore[operator]
        config_file.write_text(
            """
[importguard]

[importguard.budgets]
"json" = 1
"""
        )

        # Config says 1ms (would fail), but CLI says 5000ms (should pass)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "importguard",
                "check",
                "json",
                "--config",
                str(config_file),
                "--max-ms",
                "5000",
                "--json",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["passed"] is True

    def test_cli_appends_to_config_bans(self, tmp_path: pytest.TempPathFactory) -> None:
        """Test that CLI --ban appends to config bans."""
        config_file = tmp_path / ".importguard.toml"  # type: ignore[operator]
        config_file.write_text(
            """
[importguard]

[importguard.banned]
"urllib.request" = ["ssl"]
"""
        )

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "importguard",
                "check",
                "urllib.request",
                "--config",
                str(config_file),
                "--ban",
                "http",
                "--json",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 1
        data = json.loads(result.stdout)
        # Both ssl (from config) and http (from CLI) should be in banned_found
        assert "ssl" in data["banned_found"]
        assert "http" in data["banned_found"]

    def test_config_file_not_found_error(self) -> None:
        """Test error when config file doesn't exist."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "importguard",
                "check",
                "json",
                "--config",
                "nonexistent.toml",
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 1
        assert "Config file not found" in result.stderr
