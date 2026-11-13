---
name: importguard
description: Check what a code change adds to a Python program's startup imports, and fix it before finishing. Use this whenever you add or change import statements, add a dependency, or add module-level code in a Python project that has a CLI, a server, a serverless handler or a library entrypoint. Also use it when an importguard comment or check appears on a pull request, when the user asks why a module gets imported or why startup is slow, and before you finish any Python task that touched imports, even if nobody mentioned importguard. Runs `importguard diff`, which works with no setup in any git repository.
---

# importguard: keep what you add to startup intentional

An `import` added three modules deep can pull hundreds of modules into a program's startup. It can also add a network call or a subprocess that now runs on every launch. Reviewers rarely see this in a diff. `importguard diff` imports each entrypoint at the base commit and at your working tree, and reports what your change added, with the chain of imports that pulled it in.

Your job: run it after you change imports, fix what you introduced, and hand the decisions that are not yours to the user.

## Run it

From the repository root, after your edits (uncommitted changes are included):

```bash
importguard diff --json --strict
```

If `importguard` isn't installed, run it without installing:

```bash
uvx --from importguardpy importguard diff --json --strict
```

Things that matter for a correct result:

- **Use the project's environment.** The probe imports the project's code, so it needs the project's dependencies. If the project has its own virtualenv, pass its interpreter as an absolute path, for example `--python "$PWD/.venv/bin/python"` (importguard 0.2.0 needs it absolute; 0.2.1 and later also accept a relative path). If you see `import_failed` with `ModuleNotFoundError` for a dependency the project declares, that's this problem, not a bug in the change.
- **Entrypoints** are inferred from `[project.scripts]` and the package name. If none are found (exit code 2, "no entrypoints"), pass the modules that matter, for example `importguard diff mypkg mypkg.cli --json --strict`.
- **The base** is the merge-base with `origin/HEAD`, `main` or `master`. Pass `--base REF` if the branch is based on something else.
- **`--strict`** makes warnings fail too. A new startup package is only a warning on PRs, but you should still deal with it before finishing.

Exit codes: `0` clean · `1` findings to address · `2` usage or config problem · `3` the tool couldn't run. Exit codes 2 and 3 are not findings about the change. Fix the invocation if you can (entrypoints, `--base`, `--python`), and otherwise tell the user it couldn't run and why. Don't report the change as clean.

## Read the report

`--json` prints a report. The parts you need are in `findings`:

```json
{
  "kind": "new_package",
  "severity": "warning",
  "message": "new third-party package at startup: pandas (+188 modules, ~310ms)",
  "chain": [
    {"module": "mypkg.cli", "imported_by": null, "file": null, "line": null},
    {"module": "mypkg.report", "imported_by": "mypkg.cli", "file": "src/mypkg/cli.py", "line": 4},
    {"module": "pandas", "imported_by": "mypkg.report", "file": "src/mypkg/report.py", "line": 2}
  ],
  "remedies": [
    {"kind": "defer_import", "file": "src/mypkg/report.py", "line": 2,
     "source": "import pandas as pd", "replacement": "lazy import pandas as pd"},
    {"kind": "request_approval", "summary": "if this is intended, a maintainer approves it outside this change ..."}
  ]
}
```

The `chain` is how the module reached startup. The `defer_import` remedy points at the last line in the project's own code, which is the line you can change. For more detail, `importguard why <entrypoint> <module>` prints the chain for any module.

## Fix what you introduced

Work through each finding, from errors to warnings:

**`new_package` or `forbidden_import`: defer the import.** Usually the package is only needed by one function, so it shouldn't load at startup. Choose the form by the project's supported Python versions (`requires-python` in `pyproject.toml`):

- **Supports anything older than 3.15** (most projects): move the import into the function that uses it. The `replacement` field suggests `lazy import …`, which is a SyntaxError before Python 3.15, so don't paste it into such a project. If the name is only used in type annotations, put the import under `if TYPE_CHECKING:` and quote the annotations, or use `from __future__ import annotations`.
- **Requires Python 3.15 or later:** the `replacement` (`lazy import …`) is the one-line fix. Use it as given; it targets exactly that statement.

Deferring changes when the module runs, so check for code that depends on it running at import time. Common cases: a module that registers plugins or handlers when imported, monkey-patching, and tests that mock the imported name (`mock.patch("mypkg.report.pd")`). Run the project's tests after deferring.

**`side_effect`** (network, subprocess, file write, environment change, thread at import time): move that code out of module scope, into a function called when it's needed or a lazily initialised helper. The `investigate` remedy gives the file and line. Startup code that phones home or spawns processes is almost always a bug, and the default policy fails the check for network and subprocess activity.

**`preloaded_code`:** project code runs at interpreter startup through a `sitecustomize.py` or a `.pth` file, where importguard can't observe it. If your change added that file, remove it. If not, tell the user.

**`import_failed`:** the entrypoint raises on import. Fix the error, unless it's the environment problem described under "Run it".

**Info findings** (`new_first_party`, `new_stdlib`, `removed_package`) are context. Mention them if they're large; they don't need fixing.

Then run `importguard diff --json --strict` again, and repeat until it exits 0 or only `request_approval` items remain.

## Hand the decision to the user when the dependency is needed

Sometimes the new startup dependency is the point of the change. A CLI that now formats all its output with `rich`, for example, can't defer `rich`. In that case:

- **Stop and tell the user.** Name the package and the entrypoint, give the chain and roughly how many modules it adds, and say why it can't be deferred. Approving it is a maintainer's call: the PR label `importguard:allow:<package>`, or adding it to `allow` under `[tool.importguard]` on the base branch.
- **Don't approve it yourself.** That means not editing `[tool.importguard]` (`allow`, `forbid`, severities, `python`, `pythonpath`), not running `importguard snapshot` or editing `importguard.lock`, not adding approval labels, and not passing `--allow`, `--policy-from head` or a different `--base` to make the check pass. Why: the check exists to show the people reviewing your change what it adds to startup. Silencing it hides exactly that. In diff mode most of these don't work anyway, because policy and baseline come from the base commit, and a policy edit shows up as a `policy_changed` finding.
- **If the user asks you to change the policy**, do it as its own clearly described change. It takes effect once that change is reviewed and merged.

## Report back

When you finish, say in a few lines what importguard found and what you did. For example: "importguard: deferred `pandas` in `report.py` (it was adding 188 modules to `mycli` startup); moved the version check out of import time; `rich` is a new startup dependency that needs your approval." If it couldn't run, say so and why.
