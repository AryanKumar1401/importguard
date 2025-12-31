"""Tests for configuration loading."""

import tempfile
from pathlib import Path

from importguard.config import ImportGuardConfig, ModuleConfig, load_config


class TestLoadConfig:
    """Tests for load_config."""

    def test_load_basic_config(self) -> None:
        """Test loading a basic config file."""
        config_content = """\
[importguard]
max_total_ms = 200

[importguard.budgets]
"mypkg" = 150
"mypkg.cli" = 100

[importguard.banned]
"mypkg.cli" = ["pandas", "torch"]
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(config_content)
            f.flush()
            config_path = Path(f.name)

        try:
            config = load_config(config_path)

            assert config.max_total_ms == 200
            assert "mypkg" in config.modules
            assert config.modules["mypkg"].max_ms == 150
            assert config.modules["mypkg.cli"].max_ms == 100
            assert "pandas" in config.modules["mypkg.cli"].banned
            assert "torch" in config.modules["mypkg.cli"].banned
        finally:
            config_path.unlink()

    def test_load_empty_config(self) -> None:
        """Test loading an empty config file."""
        config_content = "[importguard]\n"

        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write(config_content)
            f.flush()
            config_path = Path(f.name)

        try:
            config = load_config(config_path)

            assert config.max_total_ms is None
            assert len(config.modules) == 0
        finally:
            config_path.unlink()


class TestImportGuardConfig:
    """Tests for ImportGuardConfig."""

    def test_get_module_config_exact_match(self) -> None:
        """Test getting config for exact module match."""
        config = ImportGuardConfig(
            max_total_ms=200,
            modules={
                "mypkg": ModuleConfig(max_ms=150),
                "mypkg.cli": ModuleConfig(max_ms=100),
            },
        )

        module_config = config.get_module_config("mypkg.cli")

        assert module_config.max_ms == 100

    def test_get_module_config_parent_inheritance(self) -> None:
        """Test getting config inherits from parent module."""
        config = ImportGuardConfig(
            max_total_ms=200,
            modules={
                "mypkg": ModuleConfig(max_ms=150, banned={"pandas"}),
            },
        )

        module_config = config.get_module_config("mypkg.submodule")

        assert module_config.max_ms == 150
        assert "pandas" in module_config.banned

    def test_get_module_config_global_fallback(self) -> None:
        """Test getting config falls back to global settings."""
        config = ImportGuardConfig(max_total_ms=200)

        module_config = config.get_module_config("unknown")

        assert module_config.max_ms == 200

    def test_get_module_config_no_settings(self) -> None:
        """Test getting config with no settings."""
        config = ImportGuardConfig()

        module_config = config.get_module_config("unknown")

        assert module_config.max_ms is None
