#!/usr/bin/env bash
# Post the report a fork PR's run uploaded, from a trusted workflow_run job.
#
# The artifact was produced by a run the fork controls, so its contents are
# untrusted data: nothing in it is executed, the PR number must belong to the
# commit the run tested, and the body is size-capped with @mentions defused.
#
# env: GH_TOKEN, REPO, AUTHOR, REPORT_DIR (downloaded artifact),
#      RUN_HEAD_SHA (github.event.workflow_run.head_sha)
set -u
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pr_file="$REPORT_DIR/pr.txt"
report="$REPORT_DIR/report.md"
if [ ! -f "$pr_file" ] || [ ! -f "$report" ]; then
  echo "No importguard report in this run (not a fork PR, or the check did not run)."
  exit 0
fi
pr="$(head -c 20 "$pr_file" | tr -d '[:space:]')"
if ! [[ "$pr" =~ ^[0-9]+$ ]]; then
  echo "::warning::importguard: the uploaded PR number is not a number; not commenting."
  exit 0
fi
head_sha=$(gh api "repos/$REPO/pulls/$pr" --jq .head.sha 2>/dev/null) || head_sha=""
if [ -z "$head_sha" ] || [ "$head_sha" != "$RUN_HEAD_SHA" ]; then
  echo "::warning::importguard: PR #$pr is not at the commit this run tested; not commenting."
  exit 0
fi
safe="$(mktemp)"
# Cap the size (GitHub's limit is 65536 characters) and defuse @mentions with a
# zero-width space so the bot cannot be used to notify people.
head -c 60000 "$report" | sed 's/@/@\xe2\x80\x8b/g' > "$safe"
PR="$pr" BODY_FILE="$safe" bash "$here/post-comment.sh"
rm -f "$safe"
