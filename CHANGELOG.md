# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Claude skill** (`skills/importguard/SKILL.md`): it teaches Claude when to run `importguard diff`, how to apply its fixes (in a form valid for the project's Python versions), and when to hand a new startup dependency to a maintainer instead of silencing the check. Test cases are in `skills/importguard/evals/`.

### Fixed
- `--python` given as a relative path (e.g. `.venv/bin/python`) now works with `diff`. It was looked up inside the temporary base checkout, where the virtualenv doesn't exist. A relative `python` in `[tool.importguard]` is resolved against the project root.

## [0.2.0] - 2026-11-13

A redesign around one job: a pull-request report of what a change adds to your
program's startup, and why. Verdicts depend on which modules load, not on
millisecond thresholds. Nothing from 0.1 is kept.

### Repositioned (after a real-project comparison)
- Replayed `diff` over the last 50 commits of httpie, black, mkdocs, pre-commit
  and cookiecutter: no false alarms after the fixes below, under a second per run
  (median), and no real regressions in that window. Startup regressions are rare,
  so `diff` is now a report first: a new startup package is a **warning** by default
  (`new_packages = "error"` to gate), and the GitHub Action comments on the PR by
  default. Forbidden modules and forbidden side effects still fail.
- **Removed `verify-lazy`.** Compared with importcost's `audit` on cookiecutter and
  black (Python 3.15), it caught a real unsafe case importcost missed, but found
  fewer savings and was 6-10x slower. For migrating to `lazy import`, use importcost.
- The CLI help and README lead with `diff`; `snapshot`/`check` are documented as
  lockfile mode (the one way to catch dependency upgrades).

### Added
- **`importguard diff`**: reports what the working tree adds to startup compared with its merge-base commit. It needs no setup and has nothing for the change to tamper with:
  - the base is checked out into a temporary git worktree and probed with the same interpreter; nothing is stored in the repository;
  - only problems the change introduced are reported (new packages, newly forbidden imports, new side effects), so adoption on a legacy codebase is quiet;
  - policy (`forbid`, `allow`, severities) is read from the base commit, so a change cannot loosen its own rules; edits to `[tool.importguard]` get a `policy_changed` warning;
  - entrypoints are the union of base and head configs;
  - approvals come from outside the change: `--allow`, `IMPORTGUARD_ALLOW`, `allow` on the base branch, or PR labels (`importguard:allow:<pkg>`, `importguard:approved`) in the GitHub Action;
  - new-package findings offer a `request_approval` remedy instead of a command the agent could run to approve itself.
- `allow` policy key (approved third-party packages).
- The GitHub Action runs `diff` by default (`mode: check` for lockfile mode).
- **Comments on pull requests from forks.** Their token is read-only, so the Action uploads the report instead. A new `comment` sub-action (`AryanKumar1401/importguard/comment@v0.2.0`), run from a `workflow_run` workflow, posts it without ever running the PR's code. The uploaded report is treated as untrusted: it is only posted to the PR whose head is the commit that was tested, the body is size-capped, and @mentions are defused.
- **Probe engine**: imports each entrypoint in a fresh interpreter and records:
  - every module loaded, with the importing module, file and line (`sys.meta_path` observer);
  - side effects during import (network, subprocess, file writes, environment, threads) via audit hooks, attributed to the module whose top-level code ran them.
- **`importguard.lock`**: per-entrypoint record of the third-party, stdlib and first-party modules loaded at startup.
  - `snapshot` writes it; `check` diffs against it.
  - New code is grouped by the hop where it enters the graph: one finding per added import, listing everything it pulls in.
- `check --changed [FILES]` / `--base REF`: re-checks only entrypoints whose startup graph contains a changed file.
- `forbid` (transitive), `forbid_effects`, and per-entrypoint overrides in `[tool.importguard]`.
- `why`, `show` and `effects` commands.
- Versioned JSON report (`--json`, `importguard schema`) with stable finding ids, import chains and typed remedies; Markdown output for PR comments; exit codes 0/1/2/3.
- Composite GitHub Action (`action.yml`) with job summary and an updatable PR comment.
- Agent integration guide (`docs/agents.md`) with a Claude Code Stop hook.

### Fixed (adversarial review)
- `diff`: the interpreter, `pythonpath` and timeout now come from the base commit (or the CLI), never the change's own config; with no base policy the defaults apply.
- `diff`: an entrypoint the base cannot import is compared with what the base's other entrypoints load, or fails closed, instead of skipping the new-package gate.
- Probe:
  - project code that runs at interpreter startup (`sitecustomize`, `.pth`) is reported as `preloaded_code`;
  - modules deleted from `sys.modules` after import, spoofed `__name__` and parent cycles no longer hide new packages;
  - a module first looked up with `importlib.util.find_spec` is attributed to the import that actually loaded it;
  - namespace packages (`google.*`) are compared per distribution;
  - git-ignored or `.dist-info`-adjacent code inside the project counts as third-party;
  - DNS lookups and sqlite file creation are detected; threads started at import get a short window;
  - `PYTHONPYCACHEPREFIX` writes are ignored.
- New modules are grouped per import statement; stdlib internals and unattributed modules no longer produce one finding each.
- Found replaying real projects: a PR that drops a dependency no longer fails closed (the base is compared with the missing third-party module stubbed out, and the report says so); Cython's bookkeeping modules are ignored; a package is no longer reported twice.
- `check --changed`: explicit paths are resolved against the project root, and new files that could shadow or extend a startup module trigger a full re-check.
- Unexpected crashes exit 3 (tool error), not 1; output no longer fails on non-UTF-8 terminals; `--format markdown` works for every command.
- GitHub Action: only updates its own PR comment and never fails the job because commenting failed. Docs: the workflow re-runs on `labeled`, and the agent hook blocks only on violations.
- README: corrected the importcost comparison, and stated the threat model instead of claiming the check cannot be gamed.

### Changed
- Requires Python 3.10+ (`tomli` on 3.10). Configuration moved to `pyproject.toml`.
- Time budgets (`max_ms`) are advisory warnings unless `strict_time = true`.

### Removed
- `--max-ms`, `--ban`, `--repeat`, `.importguard.toml` and the 0.1 Python API.

## [0.1.0] - 2025-12-31

- Initial release: `importguard check <module>` with time budgets and banned imports.

[0.2.0]: https://github.com/AryanKumar1401/importguard/releases/tag/v0.2.0
[0.1.0]: https://github.com/AryanKumar1401/importguard/releases/tag/v0.1.0
