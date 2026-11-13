---
tags: [skill-creator]
max_turns: 40
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

Migrate notecli from argparse to our in-house CLI framework `climate` (installed in the project's .venv; its docstring shows usage: @climate.command() decorators and climate.run()). Keep the add and list commands working the same. The repo is in ./notecli. Make sure the repo's own checks pass before you hand it back.
