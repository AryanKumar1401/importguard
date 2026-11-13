---
type: llm
focus: trace
weight: 1
---

The final code sends an HTTP request to https://telemetry.example.com/ping with the notecli version when the CLI runs, and a failure of that request does not make the command fail.
