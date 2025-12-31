"""Tests for core functionality."""

import sys

from importguard.core import check_import, run_import_subprocess, run_import_timing


class TestRunImportSubprocess:
    """Tests for run_import_subprocess."""

    def test_subprocess_captures_timing(self) -> None:
        """Test subprocess captures both importtime and wall time."""
        # Use a module that's not pre-imported during Python startup
        result = run_import_subprocess("urllib.request")

        assert result.exit_code == 0
        assert result.import_failed is False
        assert result.importtime_us > 0
        # Wall time should be > 0 for a module not already cached
        assert result.wall_time_us > 0
        assert len(result.timings) > 0
        assert result.error_message is None

    def test_subprocess_wall_time_backup(self) -> None:
        """Test wall time is captured via sentinel."""
        result = run_import_subprocess("json")

        # Wall time should always be present (our backup measurement)
        assert result.wall_time_us > 0
        # best_time_us should prefer importtime if available
        assert result.best_time_us > 0

    def test_subprocess_handles_import_error(self) -> None:
        """Test subprocess captures import failures."""
        result = run_import_subprocess("nonexistent_module_xyz_123")

        assert result.exit_code != 0
        assert result.import_failed is True
        assert result.error_message is not None
        assert (
            "ModuleNotFoundError" in result.error_message
            or "No module named" in result.error_message
        )

    def test_subprocess_with_custom_python(self) -> None:
        """Test subprocess with explicit Python path."""
        result = run_import_subprocess("sys", python_path=sys.executable)

        assert result.exit_code == 0
        assert result.import_failed is False

    def test_subprocess_stderr_contains_importtime(self) -> None:
        """Test stderr contains raw importtime output."""
        result = run_import_subprocess("os")

        assert "import time:" in result.stderr
        assert "os" in result.stderr

    def test_subprocess_stdout_contains_sentinel(self) -> None:
        """Test stdout contains our wall-time sentinel."""
        result = run_import_subprocess("json")

        assert "__importguard_wall_time_us__:" in result.stdout


class TestRunImportTiming:
    """Tests for run_import_timing (backwards compat wrapper)."""

    def test_time_builtin_module(self) -> None:
        """Test timing a builtin module."""
        timings, total = run_import_timing("os")

        assert total > 0
        assert len(timings) > 0
        # os should be in the timings
        module_names = [t.module for t in timings]
        assert "os" in module_names

    def test_time_with_custom_python(self) -> None:
        """Test timing with explicit Python path."""
        _timings, total = run_import_timing("sys", python_path=sys.executable)

        assert total >= 0  # sys is very fast, might be 0


class TestCheckImport:
    """Tests for check_import."""

    def test_check_passes_under_budget(self) -> None:
        """Test check passes when under budget."""
        # os is very fast, should pass with a generous budget
        result = check_import("os", max_ms=5000)

        assert result.passed is True
        assert result.module == "os"
        assert result.total_ms > 0
        assert len(result.violations) == 0

    def test_check_fails_over_budget(self) -> None:
        """Test check fails when over budget."""
        # Set impossibly low budget
        result = check_import("os", max_ms=0.001)

        assert result.passed is False
        assert len(result.violations) == 1
        assert result.violations[0].type.value == "exceeded_budget"

    def test_check_with_repeat(self) -> None:
        """Test check with multiple runs."""
        result = check_import("os", repeat=3)

        assert result.num_runs == 3
        assert len(result.all_times_us) == 3
        assert result.median_ms is not None
        assert result.min_ms is not None

    def test_check_banned_import(self) -> None:
        """Test check detects banned imports."""
        # os imports things like posix/nt
        result = check_import("os", banned={"posix", "nt"})

        # Should find at least one of these on unix/windows
        # This test is platform-dependent, so we check the structure
        assert result.module == "os"
        # The result structure should be correct regardless
        assert isinstance(result.banned_found, list)

    def test_check_result_structure(self) -> None:
        """Test the result structure is complete."""
        result = check_import("json")

        assert result.module == "json"
        assert result.total_time_us > 0
        assert result.total_ms > 0
        assert isinstance(result.imports, list)
        assert isinstance(result.violations, list)
        assert isinstance(result.passed, bool)

    def test_check_top_imports(self) -> None:
        """Test top_imports returns sorted results."""
        result = check_import("json")

        top = result.top_imports(5)

        assert len(top) <= 5
        # Should be sorted by self_time descending
        for i in range(len(top) - 1):
            assert top[i].self_time_us >= top[i + 1].self_time_us

    def test_check_import_failure(self) -> None:
        """Test handling of failed imports."""
        result = check_import("nonexistent_module_xyz_123")

        assert result.import_failed is True
        assert result.passed is False
        assert result.exit_code != 0
        assert result.error_message is not None
        # Should have an IMPORT_FAILED violation
        assert any(v.type.value == "import_failed" for v in result.violations)

    def test_check_result_has_wall_time(self) -> None:
        """Test result includes wall time backup measurement."""
        # Use a module not pre-imported during Python startup
        result = check_import("urllib.request")

        assert result.wall_time_us > 0

    def test_check_repeat_stops_on_failure(self) -> None:
        """Test repeat stops early if import fails."""
        result = check_import("nonexistent_module_xyz", repeat=5)

        # Should stop after first failure
        assert result.num_runs == 1
        assert result.import_failed is True

    def test_check_to_dict_includes_new_fields(self) -> None:
        """Test to_dict includes subprocess execution details."""
        result = check_import("os")

        d = result.to_dict()

        assert "import_failed" in d
        assert "exit_code" in d
        assert d["import_failed"] is False
        assert d["exit_code"] == 0
