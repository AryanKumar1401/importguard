"""Configuration loading from .importguard.toml."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    # For Python 3.9 and 3.10, we need to handle TOML differently
    # We'll use a simple fallback that works without external deps
    tomllib = None


@dataclass
class ModuleConfig:
    """Configuration for a specific module."""

    max_ms: float | None = None
    banned: set[str] = field(default_factory=set)


@dataclass
class ImportGuardConfig:
    """Full importguard configuration."""

    max_total_ms: float | None = None
    modules: dict[str, ModuleConfig] = field(default_factory=dict)

    def get_module_config(self, module: str) -> ModuleConfig:
        """Get configuration for a specific module, with inheritance."""
        # Try exact match first
        if module in self.modules:
            config = self.modules[module]
            # Apply global max if module doesn't have one
            if config.max_ms is None and self.max_total_ms is not None:
                config.max_ms = self.max_total_ms
            return config

        # Try parent modules
        parts = module.split(".")
        for i in range(len(parts) - 1, 0, -1):
            parent = ".".join(parts[:i])
            if parent in self.modules:
                config = self.modules[parent]
                if config.max_ms is None and self.max_total_ms is not None:
                    config.max_ms = self.max_total_ms
                return config

        # Return default with global settings
        return ModuleConfig(max_ms=self.max_total_ms)


def _parse_toml_simple(content: str) -> dict[str, Any]:
    """
    Simple TOML parser for basic configs (fallback for Python < 3.11).

    This handles only the subset of TOML we need:
    - [section] headers
    - key = value pairs (strings, numbers, arrays)
    """
    result: dict[str, Any] = {}
    current_section: list[str] | None = None

    for line in content.splitlines():
        line = line.strip()

        # Skip empty lines and comments
        if not line or line.startswith("#"):
            continue


        # Section header
        if line.startswith("[") and line.endswith("]"):
            section_name = line[1:-1].strip()
            current_section = section_name.split(".")
            # Create nested structure
            current: dict[str, Any] = result
            for part in current_section:
                part = part.strip('"')
                if part not in current:
                    current[part] = {}
                current = current[part]
            continue

        # Key = value
        if "=" in line:
            key, value = line.split("=", 1)
            key = key.strip().strip('"')
            value = value.strip()

            # Parse value
            parsed_value: Any
            if value.startswith('"') and value.endswith('"'):
                parsed_value = value[1:-1]
            elif value.startswith("[") and value.endswith("]"):
                # Simple array parsing
                array_content = value[1:-1]
                if array_content.strip():
                    items: list[str] = []
                    for item in array_content.split(","):
                        item = item.strip().strip('"')
                        if item:
                            items.append(item)
                    parsed_value = items
                else:
                    parsed_value = []
            elif value.isdigit():
                parsed_value = int(value)
            elif value.replace(".", "").isdigit():
                parsed_value = float(value)
            else:
                parsed_value = value

            # Set value in nested structure
            if current_section:
                current = result
                for part in current_section:
                    part = part.strip('"')
                    current = current[part]
                current[key] = parsed_value
            else:
                result[key] = parsed_value

    return result


def load_config(path: Path) -> ImportGuardConfig:
    """
    Load configuration from a .importguard.toml file.

    Args:
        path: Path to the config file

    Returns:
        ImportGuardConfig object
    """
    content = path.read_text()

    data: dict[str, Any]
    if tomllib is not None:
        data = tomllib.loads(content)
    else:
        data = _parse_toml_simple(content)

    config = ImportGuardConfig()

    ig_data: dict[str, Any] = data.get("importguard", {})

    # Global settings
    if "max_total_ms" in ig_data:
        config.max_total_ms = float(ig_data["max_total_ms"])

    # Per-module budgets
    budgets = ig_data.get("budgets", {})
    for module, max_ms in budgets.items():
        if module not in config.modules:
            config.modules[module] = ModuleConfig()
        config.modules[module].max_ms = float(max_ms)

    # Per-module banned imports
    banned = ig_data.get("banned", {})
    for module, banned_list in banned.items():
        if module not in config.modules:
            config.modules[module] = ModuleConfig()
        config.modules[module].banned = set(banned_list)

    return config


def find_config(start_path: Path | None = None) -> Path | None:
    """
    Find .importguard.toml by walking up from start_path.

    Args:
        start_path: Starting directory (default: current directory)

    Returns:
        Path to config file if found, None otherwise
    """
    if start_path is None:
        start_path = Path.cwd()

    current = start_path.resolve()

    while current != current.parent:
        config_path = current / ".importguard.toml"
        if config_path.exists():
            return config_path
        current = current.parent

    # Check root
    config_path = current / ".importguard.toml"
    if config_path.exists():
        return config_path

    return None
