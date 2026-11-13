---
type: llm
focus: trace
weight: 1
---

Importing `notecli.cli` makes no network call: the ping happens inside main() or a function main() calls, not at module level.
