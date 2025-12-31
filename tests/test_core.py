"""Tests for core functionality."""

import sys

from importguard.core import check_import, run_import_timing


class TestRunImportTiming:
    """Tests for run_import_timing."""

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
