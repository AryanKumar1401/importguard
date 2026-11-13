---
tags: [skill-creator]
max_turns: 40
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

Product wants anonymous usage stats for notecli. Every time the CLI runs, send a ping to https://telemetry.example.com/ping with the notecli version (an HTTP GET with ?v=<version> is fine; use urllib, no new dependencies). If the ping fails (offline etc.) it must never break the command. The repo is in ./notecli.
