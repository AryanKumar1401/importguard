# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - unreleased

A redesign. importguard now records runtime ground truth about imports, and its
verdicts no longer depend on millisecond thresholds. Nothing from 0.1 is kept.

### Added
- **Probe engine**: imports each entrypoint in a fresh interpreter and records:
  - every module loaded, with the importing module, file and line (`sys.meta_path` observer);
  - side effects during import (network, subprocess, file writes, environment, threads) via audit hooks, attributed to the module whose top-level code ran them.
- **`importguard.lock`**: per-entrypoint record of the third-party, stdlib and first-party modules loaded at startup.
  - `snapshot` writes it; `check` diffs against it.
  - New code is grouped by the hop where it enters the graph: one finding per added import, listing everything it pulls in.
- `check --changed [FILES]` / `--base REF`: re-checks only entrypoints whose startup graph contains a changed file.
- `forbid` (transitive), `forbid_effects`, and per-entrypoint overrides in `[tool.importguard]`.
- `why`, `show` and `effects` commands.
- **`verify-lazy`** (Python 3.15+, PEP 810):
  - discovers lazy-eligible import statements and measures what each saves;
  - runs your test command with them lazy via `sys.set_lazy_imports_filter`;
  - bisects test failures down to the imports that must stay eager.
- Versioned JSON report (`--json`, `importguard schema`) with stable finding ids, import chains and typed remedies; Markdown output for PR comments; exit codes 0/1/2/3.
- Composite GitHub Action (`action.yml`) with job summary and an updatable PR comment.
- Agent integration guide (`docs/agents.md`) with a Claude Code Stop hook.

### Changed
- Requires Python 3.10+ (`tomli` on 3.10). Configuration moved to `pyproject.toml`.
- Time budgets (`max_ms`) are advisory warnings unless `strict_time = true`.

### Removed
- `--max-ms`, `--ban`, `--repeat`, `.importguard.toml` and the 0.1 Python API.

## [0.1.0] - 2025-01-01

- Initial release: `importguard check <module>` with time budgets and banned imports.
