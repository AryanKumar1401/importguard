"""Tests for data models."""

from importguard.models import (
    ImportResult,
    ImportTiming,
    Violation,
    ViolationType,
)


class TestImportTiming:
    """Tests for ImportTiming."""

    def test_time_conversion(self) -> None:
        """Test microseconds to milliseconds conversion."""
        timing = ImportTiming(
            module="test",
            self_time_us=1500,
            cumulative_time_us=3000,
        )

        assert timing.self_time_ms == 1.5
        assert timing.cumulative_time_ms == 3.0


class TestViolation:
    """Tests for Violation."""

    def test_str_without_details(self) -> None:
        """Test string representation without details."""
        violation = Violation(
            type=ViolationType.BANNED_IMPORT,
            message="test imports banned module: pandas",
            module="pandas",
        )

        assert str(violation) == "test imports banned module: pandas"

    def test_str_with_details(self) -> None:
        """Test string representation with details."""
        violation = Violation(
            type=ViolationType.EXCEEDED_BUDGET,
            message="test imported in 150ms (budget: 100ms)",
            module="test",
            details="exceeded by 50ms",
        )

        assert "exceeded by 50ms" in str(violation)


class TestImportResult:
    """Tests for ImportResult."""

    def test_passed_no_violations(self) -> None:
        """Test passed property with no violations."""
        result = ImportResult(module="test", total_time_us=100000)

        assert result.passed is True

    def test_passed_with_violations(self) -> None:
        """Test passed property with violations."""
        result = ImportResult(
            module="test",
            total_time_us=100000,
            violations=[
                Violation(
                    type=ViolationType.BANNED_IMPORT,
                    message="test",
                    module="pandas",
                )
            ],
        )

        assert result.passed is False

    def test_total_ms(self) -> None:
        """Test total_ms property."""
        result = ImportResult(module="test", total_time_us=150000)

        assert result.total_ms == 150.0

    def test_median_ms_odd(self) -> None:
        """Test median_ms with odd number of runs."""
        result = ImportResult(
            module="test",
            total_time_us=120000,
            all_times_us=[100000, 120000, 140000],
            num_runs=3,
        )

        assert result.median_ms == 120.0

    def test_median_ms_even(self) -> None:
        """Test median_ms with even number of runs."""
        result = ImportResult(
            module="test",
            total_time_us=115000,
            all_times_us=[100000, 110000, 120000, 140000],
            num_runs=4,
        )

        assert result.median_ms == 115.0

    def test_min_ms(self) -> None:
        """Test min_ms property."""
        result = ImportResult(
            module="test",
            total_time_us=120000,
            all_times_us=[100000, 120000, 140000],
            num_runs=3,
        )

        assert result.min_ms == 100.0

    def test_top_imports(self) -> None:
        """Test top_imports method."""
        result = ImportResult(
            module="test",
            total_time_us=100000,
            imports=[
                ImportTiming("a", 100, 100),
                ImportTiming("b", 300, 300),
                ImportTiming("c", 200, 200),
            ],
        )

        top = result.top_imports(2)

        assert len(top) == 2
        assert top[0].module == "b"
        assert top[1].module == "c"

    def test_to_dict(self) -> None:
        """Test to_dict method."""
        result = ImportResult(
            module="test",
            total_time_us=100000,
            all_times_us=[100000],
            num_runs=1,
        )

        d = result.to_dict()

        assert d["module"] == "test"
        assert d["total_ms"] == 100.0
        assert d["passed"] is True
        assert "violations" in d
        assert "top_imports" in d
