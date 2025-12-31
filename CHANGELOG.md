# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2025-01-01

### Added

- **CLI**: `importguard check <module>` command with full option support
  - `--max-ms MS` — Fail if import exceeds time budget
  - `--ban MODULE` — Ban modules from being imported (repeatable)
  - `--config FILE` — Load `.importguard.toml` configuration
  - `--top N` — Show top N slowest imports
  - `--json` — Machine-readable JSON output for CI
  - `--quiet` — Only output on failure
  - `--repeat K` — Run K times, report median (reduces noise)
  - `--python PATH` — Use specific Python interpreter

- **Configuration**: `.importguard.toml` support
  - Global `max_total_ms` budget
  - Per-module budgets via `[importguard.budgets]`
  - Per-module banned imports via `[importguard.banned]`
  - Auto-discovery walking up directory tree

- **Python API**: `check_import()` function
  - Returns `ImportResult` with full timing data
  - Supports `max_ms`, `banned`, `repeat`, `python_path` parameters
  - Accepts both `list` and `set` for banned modules

- **Core Engine**
  - Uses Python's `-X importtime` for accurate timing
  - Wall-clock backup measurement via `time.perf_counter_ns()`
  - Runs in isolated subprocess (no interpreter pollution)
  - Handles import failures gracefully

- **CI Integration**
  - GitHub Actions workflow example
  - Pre-commit hook example
  - JSON output for machine parsing
  - Warnings for near-budget imports

- **Documentation**
  - Comprehensive README with examples
  - "Why Results Differ on CI?" troubleshooting guide
  - "How to Keep Imports Fast" best practices

### Technical Details

- Zero runtime dependencies
- Python 3.9+ support
- Full type annotations (mypy strict)
- 64+ test cases

[0.1.0]: https://github.com/AryanKumar1401/importguard/releases/tag/v0.1.0

