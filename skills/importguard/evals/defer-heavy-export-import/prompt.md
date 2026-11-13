---
tags: [skill-creator]
max_turns: 40
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

I want notecli to be able to export notes. Add an `export` subcommand that writes all notes to a CSV file (one note per row, with a header row "note"), e.g. `notecli export notes.csv`. Use the tablekit library for the CSV writing, it's already installed in the project's .venv. The repo is in ./notecli. Add a test too.
