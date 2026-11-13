#!/usr/bin/env bash
# Create or update importguard's single comment on a pull request.
#
# env: GH_TOKEN, REPO (owner/name), PR (number), AUTHOR (login that owns the
#      comment), BODY_FILE (markdown report)
# Best effort: failing to comment warns and exits 0, so it never changes the
# check's verdict.
set -u
marker="<!-- importguard -->"
body="$(mktemp)"
{ echo "$marker"; cat "$BODY_FILE"; } > "$body"
hint="Give the job 'permissions: pull-requests: write'. On pull requests from forks the token is always read-only: add the importguard-comment workflow (see the README), and the report is in the job summary meanwhile."
id=$(gh api "repos/$REPO/issues/$PR/comments" --paginate \
  --jq ".[] | select(.user.login == \"$AUTHOR\" and (.body | startswith(\"$marker\"))) | .id" \
  2>/dev/null | head -n 1) || id=""
if [ -n "$id" ]; then
  gh api -X PATCH "repos/$REPO/issues/comments/$id" -F "body=@$body" > /dev/null \
    || echo "::warning::importguard could not update its PR comment. $hint"
else
  gh api "repos/$REPO/issues/$PR/comments" -F "body=@$body" > /dev/null \
    || echo "::warning::importguard could not post a PR comment. $hint"
fi
rm -f "$body"
exit 0
