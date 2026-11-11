# Using importguard with coding agents

Coding agents add imports and dependencies quickly, and the startup cost hides in transitive imports that never show up in a diff. importguard gives an agent a deterministic, runtime-verified check, plus exact fixes it can apply. In `diff` mode it also gives the agent no way to wave a finding through itself.

## What an agent gets

- `importguard diff --json`: compares the working tree, including uncommitted edits, with the merge-base of the branch. It needs no lockfile or config, so it works in any repository. Only problems the change *introduced* are reported. Exit code `1` means the findings need attention.
- Every finding carries a stable `id`, the import `chain` (file:line per hop), and typed `remedies`:
  - `defer_import`: the first-party `file`/`line` to change, its current `source` and a suggested `replacement` (e.g. `lazy import pandas`).
  - `request_approval`: the change may be intended, but approving it is a human's call (a PR label or `allow` on the base branch). There is deliberately no command to run.
  - `accept` (lockfile mode only): `importguard snapshot <entrypoint>` records an intended change. Agents should only run it when the user explicitly asks.
  - `investigate`: a side effect in first-party code; move it out of module scope.
  - `make_lazy` / `keep_eager`: from `verify-lazy`, backed by test runs.
- `importguard schema` prints the JSON Schema of the report.
- `importguard why <entrypoint> <module>` answers "how did this get imported?" in one call.

## AGENTS.md / CLAUDE.md snippet

```markdown
## Startup imports
This project gates what its entrypoints import at startup with importguard.
- After changing imports or dependencies, run `importguard diff --json`.
- On a `new_package` or `forbidden_import` finding, apply the `defer_import` remedy
  (make the import lazy or move it into the function that needs it).
- If the new startup dependency is genuinely needed, do not try to make the check pass:
  leave it, and tell the user it needs approval (`importguard:allow:<package>` label).
  Never edit [tool.importguard] or importguard.lock to silence a finding.
- Never leave network calls, subprocesses or file writes at module import time.
- To make imports lazy, use `importguard verify-lazy <entrypoint> --python python3.15
  --test "python3.15 -m pytest -x -q"` and apply only `lazy_safe` findings.
```

## Claude Code: check before the agent finishes

A [Stop hook](https://code.claude.com/docs/en/hooks) runs when Claude finishes responding. If it exits with code `2`, Claude keeps working and receives the hook's stderr as the reason. Save this as `.claude/hooks/importguard.sh` and make it executable:

```bash
#!/usr/bin/env bash
# Block the end of a turn while this branch's changes add startup problems.
input=$(cat)
# Already continuing because of this hook: let Claude stop rather than loop.
if [ "$(printf '%s' "$input" | jq -r '.stop_hook_active')" = "true" ]; then
  exit 0
fi
cd "$CLAUDE_PROJECT_DIR" || exit 0
if ! report=$(importguard diff -q 2>&1); then
  printf 'importguard found startup import problems in your changes:\n%s\n' "$report" >&2
  exit 2
fi
exit 0
```

Register it in `.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/importguard.sh" }
        ]
      }
    ]
  }
}
```

`diff` needs no setup in the repository, so the hook works on day one. Each run probes every entrypoint twice (base and working tree), typically well under a second per entrypoint. If that is too slow for a large project, use `importguard check --changed` with a lockfile, which skips entrypoints the edit cannot affect.

The `stop_hook_active` guard lets Claude stop on its second attempt. That matters when a finding is intended and needs a human decision: Claude reports that the new dependency needs approval and stops, instead of looping or trying to silence the check. Because diff mode reads policy from the base commit, editing `pyproject.toml` to silence the check does not work.

## Other agents and CI

Any agent that can run a shell command can use the same contract: `--json` output, exit codes `0/1/2/3`, and remedies with exact file and line. In CI, the [GitHub Action](../README.md#ci) runs `diff` against the PR's base and posts a Markdown report, so reviewers of agent-authored PRs see the full import chain for each new dependency. A maintainer approves an intended one by labelling the PR `importguard:allow:<package>`. Give agents tokens that cannot add labels, or use `allow` on the protected base branch instead.
