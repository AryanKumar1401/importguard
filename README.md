# importguard

**Runtime ground truth for Python imports.** See exactly what your entrypoints load at startup and why, block unreviewed dependencies and import-time side effects in CI, and prove which imports are safe to make lazy with Python 3.15's `lazy import`.

```bash
pip install importguardpy
```

Linters read your source. importguard *runs* the import in a fresh interpreter and records every module that loads, the file and line that pulled it in, and every network call, subprocess, file write, environment change and thread started on the way. Its verdicts depend on module sets, not milliseconds, so they don't flake. That makes importguard useful as a check in CI and inside coding-agent loops.

---

## Why

- **Startup regressions hide in transitive imports.** One `import` added three modules deep pulls in hundreds of others. Reviewers never see it in the diff.
- **Coding agents add dependencies freely.** A [study of 300 agent-generated projects](https://arxiv.org/abs/2512.22387v1) found 13.5x more runtime dependencies than they declared. Someone has to check what they pull into startup.
- **Python 3.15 ships `lazy import` ([PEP 810](https://peps.python.org/pep-0810)).** Making an import lazy is a one-word change, but it is only safe if nothing depends on that module running at import time (plugin registration, monkey-patching, import order). Linters can tell you an import *could* be lazy. importguard tells you whether your tests still pass when it is.

## 60-second tour

No config, no lockfile, no setup. In any git checkout, ask what your uncommitted or branch changes add to startup:

```console
$ importguard diff
importguard diff · Python 3.13 · vs 221a634b07 (merge-base of HEAD and main) · policy from base

✓ demoapp      2 modules (base 2) · 0ms
✗ demoapp.cli  103 modules (base 77) · 34ms

error    new third-party package at startup: bigdep (+20 modules, ~11ms); also pulls in stdlib: sqlite3, json, csv, http, socket, ...
         src/demoapp/cli.py:2    → demoapp.report
         src/demoapp/report.py:5 → bigdep
         fix: defer the import at src/demoapp/report.py:5 - mark it `lazy` (Python 3.15+), list it in `__lazy_modules__`, or move it into the function that uses it
               lazy import bigdep
         or:  if this is intended, a maintainer approves it outside this change: label the pull request `importguard:allow:bigdep` or add it to `allow` on the base branch. Agents should not approve their own changes.

error    network access during import in demoapp.telemetry (src/demoapp/telemetry.py:3): socket.getaddrinfo 127.0.0.1:9
         src/demoapp/cli.py:8 → demoapp.telemetry
         fix: move this out of module scope in demoapp.telemetry (e.g. into a function called explicitly or on first use)
```

Each finding is one place where new code enters the import graph. It shows the full chain, the line you control, and the exact fix. Problems that already existed on the base commit are not reported, so the first run on a legacy codebase is quiet and only new regressions fail.

### The baseline lives outside the change

When agents write most of the code, any baseline stored in the change can be moved by the change. An agent that hits a red check can regenerate a snapshot, add an ignore entry or loosen a config to turn it green. `diff` closes that route:

- **The baseline is the merge-base commit.** It is checked out into a temporary git worktree and probed with the same interpreter. Nothing is stored in the repository, so there is nothing to regenerate.
- **The policy comes from the base too.** `forbid`, `allow`, severities, and the `python`, `pythonpath` and `timeout` used to run the probe are all read from the base commit's `pyproject.toml`. If the base has no `[tool.importguard]`, the defaults apply, never the change's own config. A change that loosens them still fails, gets a `policy_changed` finding, and takes effect only after a human reviews and merges it.
- **An entrypoint the base cannot import** (new in the change, or broken by the environment) is compared with what the base's other entrypoints load. With nothing importable on the base, everything counts as new: the check fails closed.
- **Entrypoints are the union of base and head**, so deleting one from the config doesn't hide it.
- **Approval happens outside the diff.** It comes from a pull-request label applied by a maintainer (`importguard:allow:<package>`, or `importguard:approved` for all), from `--allow` / `IMPORTGUARD_ALLOW` in CI, or from `allow` on the base branch. The fix offered for a new package never includes a command the agent can run to approve itself.

The reviewer is left with one question per finding: "this PR adds `bigdep` to CLI startup through `report.py:5`. Intended?"

What this does and does not protect against: it stops a change from moving the baseline, the rules or the approval that judge it, and from hiding imports through startup hooks (`sitecustomize`, `.pth`) or by deleting modules from `sys.modules`. The probe runs inside the interpreter it measures, so code written specifically to deceive it (rewriting the probe's state, faking its output) can still succeed. Such code is conspicuous in review; importguard is a check against accidental regressions and casual workarounds, not a sandbox.

### Lockfile mode

To also catch changes that don't come from your code, such as a dependency upgrade that starts importing something new, record a baseline with `importguard snapshot` and gate on it with `importguard check`:

```console
$ importguard snapshot            # record what each entrypoint loads -> importguard.lock
✓ demoapp: 2 modules · lightlib
✓ demoapp.cli: 77 modules · heavylib, lightlib
```

Commit `importguard.lock`. `importguard check` then fails on anything new, with the same chains and fixes. Because `snapshot` accepts changes, the lockfile is something the change itself can rewrite. Where agents push code, protect it with CODEOWNERS, or use `diff` as the gate and keep the lockfile as an extra check.

### Prove what can be lazy (Python 3.15+)

```console
$ importguard verify-lazy demoapp.cli --python python3.15 --test "python3.15 -m pytest -x -q"
demoapp.cli: 78 modules eager → 3 with the safe set lazy (-75 modules, ~36ms)
6 candidate(s) · 9 test run(s)

warning  keep eager: making demoapp.plugins lazy at src/demoapp/cli.py:3 breaks the tests
         | FAILED tests/test_app.py::test_plugins_registered - AssertionError: assert []...
         fix: this import must run at import time (side effects, registration or import-order dependence)

info     make lazy: heavylib at src/demoapp/utils/frames.py:1 saves 51 modules (~38ms); tests pass with it lazy
         fix: prefix the statement with `lazy` (3.15+) or add 'heavylib' to `__lazy_modules__` in demoapp.utils.frames
               lazy import heavylib
...
```

How it works:
1. It finds every import statement in your code that loads something at startup.
2. It measures what making each one lazy actually saves.
3. It runs your tests with all of them lazy, using `sys.set_lazy_imports_filter`, so no source is edited.
4. If the tests fail, it bisects down to the imports that break them.

Each candidate is labelled:
- **verified**: tests passed with the import lazy, and they actually used the lazily imported name (it was loaded on first use during the tests).
- **unexercised**: not proven either way. The finding says why: the tests never imported the importing module, imported it but never used the name, did not converge, or no `--test` command was given.

Imports your code already marks `lazy`, or lists in `__lazy_modules__`, keep that behaviour in every run, including the eager baseline.
- **unsafe**: making it lazy breaks the tests. The finding includes the failing test.

---

## Commands

| command | what it does |
|---|---|
| `importguard diff [ENTRY...]` | Report what the working tree adds to startup vs. its merge-base (`--base REF`, `--allow PKG`, `--policy-from base\|head`). No setup |
| `importguard snapshot [ENTRY...]` | Record what each entrypoint imports into `importguard.lock` (accepts changes) |
| `importguard check [ENTRY...]` | Fail on new packages vs. the lock, forbidden modules, and forbidden side effects |
| `importguard check --changed [FILE...]` | Only re-check entrypoints whose startup graph contains a changed file (default: ask git). Fast enough for every agent turn |
| `importguard why ENTRY MODULE` | Print the import chain from `ENTRY` to `MODULE`, with file:line for each hop |
| `importguard show ENTRY` | List loaded modules by cumulative cost (`--kind third_party`, `--all`) |
| `importguard effects [ENTRY...]` | List side effects performed during import |
| `importguard verify-lazy ENTRY --test CMD` | Find imports that are safe to make lazy, verified by your tests (needs 3.15+) |
| `importguard schema` | JSON Schema for `--json` output |

Common options: `--json` / `--format {text,json,markdown}`, `--python PATH`, `--config PATH`, `-q`.

Exit codes: `0` ok · `1` policy violations · `2` usage/config error · `3` tool error (e.g. interpreter not found).

## Configuration

Everything is optional. Without configuration, entrypoints are inferred from `[project.scripts]` and your package name.

```toml
# pyproject.toml
[tool.importguard]
entrypoints = ["mypkg", "mypkg.cli"]
forbid = ["pandas", "torch"]                    # never at startup, even transitively
allow = ["rich"]                                # approved third-party packages (diff reads this from the base commit)
forbid_effects = ["network", "subprocess"]      # default; others (file_write, env, thread) warn
new_packages = "error"                          # new third-party package vs base/lock: error|warning|info|ignore
new_stdlib = "info"
new_first_party = "info"
max_ms = 300                                    # advisory: a warning, never a failure...
strict_time = false                             # ...unless you opt in
# python = ".venv/bin/python"                   # interpreter to probe with
# pythonpath = ["src"]                          # default: src/ if it exists
# lockfile = "importguard.lock"

[tool.importguard.entrypoint."mypkg.cli"]       # per-entrypoint overrides
forbid = ["boto3"]                              # added to the global list
max_ms = 100
```

### Why the lock records packages, not milliseconds

Wall-clock import time on shared CI runners varies by tens of percent between identical runs, so a millisecond threshold either flakes or is too loose to catch anything. The set of modules an entrypoint loads is deterministic. importguard gates on the module set and reports time as context. The lock stores package names (plus first-party module files for `--changed`), so it changes only when startup really changes. Dependency version bumps that shuffle a library's internal submodules don't touch it.

---

## For coding agents

importguard is designed to be called by agents as well as people:

- **Structured output:** `--json` (or `IMPORTGUARD_FORMAT=json`) emits a versioned report, described by `importguard schema`. Every finding has:
  - a stable `id`;
  - a `kind`;
  - an import `chain` with file:line for each hop;
  - a `confidence` (`verified` means it was observed at runtime);
  - typed `remedies`: `defer_import`, `make_lazy`, `keep_eager`, `accept`, ..., with `file`, `line`, the current `source` line and a suggested `replacement`.
- **Deterministic verdicts:** an agent loop won't chase timing noise.
- **Zero setup:** `importguard diff` works in any git checkout, so an agent can run it in a repository that has never heard of importguard (`uvx --from importguardpy importguard diff --json`, once 0.2 is released on PyPI).
- **A check the change cannot reconfigure:** in diff mode the baseline, policy and interpreter come from the base commit, and approval needs a human (see above). Editing the lockfile or config does not turn it green; fixing the import does. (See the threat model above for what it does not cover.)
- **Cheap incremental checks:** `check --changed` re-probes only the entrypoints the edit can affect.

See [docs/agents.md](docs/agents.md) for an `AGENTS.md` snippet and a Claude Code hook that runs `importguard diff` before an agent finishes its turn.

## CI

```yaml
# .github/workflows/importguard.yml
on:
  pull_request:
    # labeled/unlabeled re-run the check when a maintainer adds an approval label
    types: [opened, synchronize, reopened, labeled, unlabeled]
permissions:
  contents: read
  pull-requests: write   # only for comment: true
jobs:
  importguard:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }            # history for the merge-base
      - uses: actions/setup-python@v5
        with: { python-version: "3.13" }
      - run: pip install -e .
      - uses: AryanKumar1401/importguard@main
        with:
          comment: true                     # keeps one PR comment updated with the report
          # mode: check                     # gate on importguard.lock instead of the base commit
```

By default the action runs `importguard diff` against the PR's base. It writes a Markdown report to the job summary, optionally keeps a PR comment updated, and fails the job on violations. A maintainer approves an intended new startup dependency by adding the label `importguard:allow:<package>` to the PR.

Labels are only as trustworthy as the people and tokens that can set them. If agents run with a token that can label pull requests, rely on `allow` in the base branch's `pyproject.toml` instead, protected by branch protection or CODEOWNERS.

With pre-commit:

```yaml
- repo: local
  hooks:
    - id: importguard
      name: importguard
      entry: importguard diff --base HEAD
      language: system
      pass_filenames: false
```

## How it works

`importguard` launches the target interpreter with `-X importtime` and a small stdlib-only probe script. Before importing your entrypoint, the probe installs:

- a `sys.meta_path` observer, which records each module the first time it is requested, together with the frame (file, line, importing module) that asked for it;
- a `sys.addaudithook` hook, which records network, subprocess, file-write, environment and thread events and attributes each to the module whose top-level code was running.

Bytecode-cache writes are ignored. On 3.15 the probe can also run with `sys.set_lazy_imports("all")` and a filter, either to list lazy-eligible statements or to make a chosen set of them lazy. `verify-lazy` puts the same filter into your test process through a temporary `sitecustomize` that chains to any existing one.

The probe also flags project code that runs at interpreter startup (`sitecustomize`, `usercustomize`, `.pth` files), since that executes before it can observe anything, and cross-checks which modules really loaded, so deleting a module from `sys.modules` does not hide it. Code inside the project whose top-level package is git-ignored or sits next to a `*.dist-info` directory (`__pypackages__`, `pip install --target`) counts as third-party. Namespace packages are compared by distribution (`google.cloud.bigquery`, not `google`).

The probe executes your code. Treat untrusted repositories as you would when running their tests: in a sandbox.

## How it fits with other tools

| tool | relationship |
|---|---|
| Ruff (`TID251`/`TID253`, lazy-import rules), flake8-lazy | Static checks of your own source. Use them alongside importguard. importguard sees transitive imports through dependencies and checks laziness against runtime behaviour. |
| [importcost](https://pypi.org/project/importcost/) | The closest tool. It measures import cost, has an `audit` that classifies imports as safe or unsafe to make lazy by running your tests, an `apply` that rewrites them, and an `import.lock`. importguard's differences: the import chain with the first-party line for every new package, import-time side-effect detection, and `diff` against the base commit with policy and approval outside the change. |
| tuna, importtime-waterfall | Visualise one `-X importtime` run. Use `importguard show`/`why` for the same questions in CI or from an agent. |
| import-linter | Architecture contracts between your own packages, checked statically. |

## Development

```bash
git clone https://github.com/AryanKumar1401/importguard && cd importguard
pip install -e ".[dev]"
pytest                                # set IMPORTGUARD_TEST_PYTHON315 to also run verify-lazy tests
ruff check src tests && mypy
```

## License

MIT
