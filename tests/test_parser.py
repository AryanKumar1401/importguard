"""Tests for the importtime output parser."""

from importguard.parser import find_banned_imports, parse_importtime_output


class TestParseImporttimeOutput:
    """Tests for parse_importtime_output."""

    def test_parse_simple_output(self) -> None:
        """Test parsing a simple importtime output."""
        stderr = """\
import time: self [us] | cumulative | imported package
import time:       123 |        123 |   _frozen_importlib_external
import time:       456 |        789 | os
"""
        timings, total = parse_importtime_output(stderr)

        assert len(timings) == 2
        assert timings[0].module == "_frozen_importlib_external"
        assert timings[0].self_time_us == 123
        assert timings[0].cumulative_time_us == 123
        assert timings[1].module == "os"
        assert timings[1].self_time_us == 456
        assert total == 789

    def test_parse_nested_imports(self) -> None:
        """Test parsing nested import output."""
        stderr = """\
import time: self [us] | cumulative | imported package
import time:       100 |        100 |     encodings.aliases
import time:       200 |        300 |   encodings
import time:       150 |        150 |   encodings.utf_8
import time:       500 |        950 | requests
"""
        timings, total = parse_importtime_output(stderr)

        assert len(timings) == 4
        assert total == 950  # The root import's cumulative time

    def test_parse_empty_output(self) -> None:
        """Test parsing empty output."""
        timings, total = parse_importtime_output("")

        assert len(timings) == 0
        assert total == 0

    def test_parse_with_noise(self) -> None:
        """Test parsing with non-importtime lines."""
        stderr = """\
Some warning message
import time:       100 |        200 | mymodule
Another message
"""
        timings, _total = parse_importtime_output(stderr)

        assert len(timings) == 1
        assert timings[0].module == "mymodule"

    def test_timing_properties(self) -> None:
        """Test ImportTiming properties."""
        stderr = "import time:      1000 |       2000 | mymodule"
        timings, _ = parse_importtime_output(stderr)

        assert timings[0].self_time_ms == 1.0
        assert timings[0].cumulative_time_ms == 2.0


class TestFindBannedImports:
    """Tests for find_banned_imports."""

    def test_find_exact_match(self) -> None:
        """Test finding exact module match."""
        stderr = "import time:       100 |        100 | pandas"
        timings, _ = parse_importtime_output(stderr)

        found = find_banned_imports(timings, {"pandas"})

        assert found == ["pandas"]

    def test_find_submodule(self) -> None:
        """Test finding when submodule is imported."""
        stderr = """\
import time:       100 |        100 | pandas.core
import time:       100 |        200 | pandas.io
"""
        timings, _ = parse_importtime_output(stderr)

        found = find_banned_imports(timings, {"pandas"})

        assert found == ["pandas"]

    def test_no_match(self) -> None:
        """Test when no banned modules are found."""
        stderr = "import time:       100 |        100 | requests"
        timings, _ = parse_importtime_output(stderr)

        found = find_banned_imports(timings, {"pandas", "torch"})

        assert found == []

    def test_multiple_banned(self) -> None:
        """Test finding multiple banned modules."""
        stderr = """\
import time:       100 |        100 | pandas
import time:       100 |        200 | torch.nn
"""
        timings, _ = parse_importtime_output(stderr)

        found = find_banned_imports(timings, {"pandas", "torch"})

        assert set(found) == {"pandas", "torch"}

    def test_no_false_positives(self) -> None:
        """Test that partial name matches don't trigger."""
        stderr = "import time:       100 |        100 | pandasthing"
        timings, _ = parse_importtime_output(stderr)

        found = find_banned_imports(timings, {"pandas"})

        assert found == []
