# importguard

**A pull-request report of what a change adds to your Python program's startup, and why.**

```bash
pip install importguardpy
importguard diff
```

One added `import`, three modules deep, can pull hundreds of modules into your CLI's or service's startup. It can also add a network call or a subprocess that now runs on every launch. None of that shows up in the diff a reviewer reads. importguard runs the import before and after the change and reports, on the pull request:

- every **new package at startup**, with the chain of imports that pulled it in and the line in *your* code to change;
- **forbidden modules** (`forbid = ["pandas"]`), even when they arrive through a dependency;
- **side effects at import time**: network, subprocess, file writes, environment changes, threads.

There's nothing to set up: no config, no lockfile, no baseline. Problems that already exist on your main branch aren't reported, so the first run is quiet.

---

## What a pull request sees

The GitHub Action keeps one comment on the PR up to date:

> ### :x: importguard diff
>
> Compared with `adb7d13f6c` (merge-base of HEAD and main).
>
> | entrypoint | status | modules | time |
> |---|---|---|---|
> | `demoapp.cli` | fail | 103 (base 77) | 32ms |
>
> :warning: **demoapp.cli**: new third-party package at startup: bigdep (+20 modules, ~9ms); also pulls in stdlib: sqlite3, json, csv, http, socket, …
> ```
> src/demoapp/cli.py:2 -> demoapp.report
> src/demoapp/report.py:5 -> bigdep
> ```
> - defer the import at src/demoapp/report.py:5 - mark it `lazy` (Python 3.15+), list it in `__lazy_modules__`, or move it into the function that uses it
>   `lazy import bigdep`
> - if this is intended, a maintainer approves it outside this change: label the pull request `importguard:allow:bigdep` …
>
> :x: **demoapp.cli**: network access during import in demoapp.telemetry (src/demoapp/telemetry.py:3): socket.getaddrinfo 127.0.0.1:9
> ```
> src/demoapp/cli.py:8 -> demoapp.telemetry
> ```
> - move this out of module scope in demoapp.telemetry (e.g. into a function called explicitly or on first use)

**By default it reports and does not gate.** A new startup package is a ⚠️ warning on the PR, so the reviewer answers one question: "this adds `bigdep` to CLI startup through `report.py:5`. Intended?" Only rules you set fail the check:

- `forbid` lists;
- network or subprocess calls at import time;
- `new_packages = "error"`, if you want new dependencies at startup to need explicit approval.

In practice it stays quiet. Replayed over the last 50 commits of httpie, black, mkdocs, pre-commit and cookiecutter (250 commits), it raised **no false alarms**, and runs took under a second each (median). Startup regressions are rare, so the report is meant to cost you almost nothing until the day one lands.

## Set it up on GitHub

```yaml
# .github/workflows/importguard.yml
on:
  pull_request:
    # labeled/unlabeled re-run the report when a maintainer adds an approval label
    types: [opened, synchronize, reopened, labeled, unlabeled]
permissions:
  contents: read
  pull-requests: write   # to keep the PR comment up to date
jobs:
  importguard:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }            # history for the merge-base
      - uses: actions/setup-python@v5
        with: { python-version: "3.13" }
      - run: pip install -e .               # your project, with its dependencies
      - uses: AryanKumar1401/importguard@v0.2.1
```

The action:
- compares the PR with its merge-base;
- writes the report to the job summary;
- keeps one PR comment updated (`comment: false` to turn that off);
- fails the job only on errors (`args: --strict` to fail on warnings too).

If it can't comment, it warns and carries on.

### Pull requests from forks

On a PR from a fork, GitHub gives the workflow a read-only token, so the action can't comment there. Instead it uploads the report, and a second workflow posts it:

```yaml
# .github/workflows/importguard-comment.yml
name: importguard comment
on:
  workflow_run:
    workflows: [importguard]          # the name of the workflow that runs the action
    types: [completed]
permissions:
  actions: read                       # to download the report
  pull-requests: write                # to comment
jobs:
  comment:
    if: github.event.workflow_run.event == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: AryanKumar1401/importguard/comment@v0.2.1
```

`workflow_run` always runs this file from your default branch, with a write token. It downloads the report the PR's run uploaded and never checks out or runs the PR's code. The report comes from a run the fork controls, so it is handled as untrusted data:
- the comment goes only to the PR whose head is the commit that run tested;
- the body is size-capped;
- `@mentions` are defused.

Don't use `pull_request_target` to get a write token instead. importguard executes the code it checks, so that would hand the token to whoever opened the PR.

Locally, or in any other CI, `importguard diff` does the same against `origin/HEAD`, `main` or `master`, or `--base REF`. `--format markdown` prints the comment and `--json` prints a versioned report.

## Who decides what is acceptable

When agents write much of the code, a check is only useful if the change it judges can't adjust the check. `diff` keeps everything that decides the verdict outside the change:

- **The baseline is the merge-base commit**, checked out into a temporary git worktree and probed with the same interpreter. Nothing is stored in the repository, so there is nothing to regenerate.
- **The policy comes from the base too.** That covers `forbid`, `allow`, severities, and the `python`, `pythonpath` and `timeout` used to run the probe. If the base has no `[tool.importguard]`, the defaults apply, never the change's own config. A change that edits the policy gets a `policy_changed` finding, and the edit takes effect after it's reviewed and merged.
- **Approval happens outside the diff.** It comes from a maintainer's PR label (`importguard:allow:<package>`, or `importguard:approved` for everything), `--allow` / `IMPORTGUARD_ALLOW` in CI, or `allow` on the base branch. A suggested fix never includes a command an agent could run to approve itself.
- **Entrypoints are the union of base and head**, so deleting one from the config doesn't hide it.
- **An entrypoint the base cannot import** (new in the change) is compared with what the base's other entrypoints load. If the base fails only because the change removed a third-party dependency, that module is stubbed out on the base and the report says so.

Labels are only as trustworthy as whoever can set them. If agents run with a token that can label PRs, put approvals in `allow` on the protected base branch instead.

**What it does not protect against:** the probe runs inside the interpreter it measures. Code written specifically to deceive it, such as rewriting the probe's state or faking its output, can succeed. Such code stands out in review. importguard guards against accidental regressions and casual workarounds; it is not a sandbox.

## Configuration

Everything is optional. Without configuration, entrypoints are inferred from `[project.scripts]` and your package name.

```toml
# pyproject.toml (diff reads this from the base commit)
[tool.importguard]
entrypoints = ["mypkg", "mypkg.cli"]
forbid = ["pandas", "torch"]                    # never at startup, even transitively
allow = ["rich"]                                # approved third-party packages
forbid_effects = ["network", "subprocess"]      # default; file_write, env and thread warn
new_packages = "warning"                        # default; "error" to require approval
new_stdlib = "info"
new_first_party = "info"
max_ms = 300                                    # advisory: a warning, never a failure...
strict_time = false                             # ...unless you opt in
# python = ".venv/bin/python"                   # interpreter to probe with
# pythonpath = ["src"]                          # default: src/ if it exists

[tool.importguard.entrypoint."mypkg.cli"]       # per-entrypoint overrides
forbid = ["boto3"]                              # added to the global list
```

**Why module sets and not milliseconds:** wall-clock import time on shared CI runners varies by tens of percent between identical runs, so a millisecond threshold either flakes or is too loose to catch anything. The set of modules an entrypoint loads is deterministic. importguard reports time as context and judges by what loads.

## Commands

| command | what it does |
|---|---|
| `importguard diff [ENTRY...]` | **The report.** What the working tree adds to startup vs. its merge-base (`--base REF`, `--allow PKG`, `--strict`) |
| `importguard why ENTRY MODULE` | The import chain from `ENTRY` to `MODULE`, with file:line for each hop |
| `importguard show ENTRY` | Modules an entrypoint loads, by cumulative cost (`--kind third_party`, `--all`) |
| `importguard effects [ENTRY...]` | Side effects performed during import |
| `importguard snapshot` / `check` | Lockfile mode (below) |
| `importguard schema` | JSON Schema of `--json` output |

Common options: `--json` / `--format {text,json,markdown}`, `--python PATH`, `--config PATH`, `-q`.

Exit codes: `0` ok · `1` errors (or warnings with `--strict`) · `2` usage/config error · `3` tool error.

### Lockfile mode

`diff` compares two commits in one environment, so it can't see a dependency *upgrade* that starts importing something new. To cover that, record what each entrypoint loads and compare against it:

```console
$ importguard snapshot     # writes importguard.lock; commit it
$ importguard check        # compares with the lock; check --changed re-probes only affected entrypoints
```

The lockfile lives in the repository, so a change can rewrite it. Protect it with CODEOWNERS and keep `diff` as the PR report.

## For coding agents

- `importguard diff --json` gives a versioned report (`importguard schema`). Each finding has:
  - a stable `id`;
  - the import `chain` with file:line for each hop;
  - typed `remedies` (`defer_import` with the exact `replacement`, `request_approval`, `investigate`).
- It's deterministic, needs no setup, and judges the change from outside it. An agent gets to green by fixing the import, not by editing the config.

**Claude skill.** [`skills/importguard`](skills/importguard/SKILL.md) teaches Claude when to run `importguard diff`, how to apply its fixes, and when to stop and ask you about a new dependency instead of silencing the check. To add it to a project for Claude Code:

```bash
mkdir -p .claude/skills/importguard
curl -fsSL https://raw.githubusercontent.com/AryanKumar1401/importguard/main/skills/importguard/SKILL.md \
  -o .claude/skills/importguard/SKILL.md
```

Use `~/.claude/skills/importguard/` instead to enable it for all your projects. See [docs/agents.md](docs/agents.md) for an `AGENTS.md` snippet and a Claude Code Stop hook that runs `importguard diff` before an agent finishes its turn.

## How it works

`importguard` launches your interpreter with `-X importtime` and a small stdlib-only probe script. Before importing the entrypoint, the probe installs:

- a `sys.meta_path` observer, which records each module the first time it is imported, together with the frame (file, line, importing module) that asked for it;
- a `sys.addaudithook` hook, which records network, subprocess, file-write, environment and thread events and attributes each to the module whose top-level code was running.

It also:
- flags project code that runs at interpreter startup (`sitecustomize`, `.pth` files);
- confirms which modules really loaded, so deleting one from `sys.modules` doesn't hide it;
- treats git-ignored or vendored code inside the project as third-party;
- compares namespace packages by distribution (`google.cloud.bigquery`, not `google`);
- ignores bytecode-cache writes and Cython's bookkeeping modules.

The probe executes your code. Treat untrusted repositories as you would when running their tests: in a sandbox.

## How it fits with other tools

| tool | relationship |
|---|---|
| Ruff (`TID251`/`TID253`, lazy-import rules), flake8-lazy | Static checks of your own source; use them alongside. importguard sees imports that arrive through dependencies, and runtime side effects. |
| [importcost](https://pypi.org/project/importcost/) | Measures import cost, and its `audit`/`apply` make imports lazy (PEP 810) by running your tests. **To migrate to `lazy import`, use importcost**; importguard reports what each PR adds. |
| tuna, importtime-waterfall | Visualise one `-X importtime` run. `importguard show` and `why` answer the same questions in CI or from an agent. |
| import-linter | Architecture contracts between your own packages, checked statically. |

## Development

```bash
git clone https://github.com/AryanKumar1401/importguard && cd importguard
pip install -e ".[dev]"
pytest
ruff check src tests && mypy
```

## License

MIT
