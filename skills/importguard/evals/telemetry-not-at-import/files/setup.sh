#!/usr/bin/env bash
# Creates the notecli git repo (base commit on main, work on a branch) and its
# .venv with the project, its test runner and the libraries it may use.
set -eu
cd "$(dirname "$0")/notecli"
export GIT_AUTHOR_NAME=dev GIT_AUTHOR_EMAIL=dev@example.com GIT_COMMITTER_NAME=dev GIT_COMMITTER_EMAIL=dev@example.com
git init -q -b main
git add -A
git commit -qm "notecli 1.4.0"
git checkout -qb feature
uv venv -q -p python3.12 .venv
uv pip install -q --python .venv/bin/python -e . pytest ../deps/tablekit ../deps/climate
