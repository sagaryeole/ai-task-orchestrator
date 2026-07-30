# Task Orchestrator

**AI-agnostic task orchestrator** — drives any coding-agent CLI through a backlog of tasks with automatic provider rotation, rate-limit handling, and unattended overnight execution.

## Why?

Free-tier and auto-routed AI models get rate-limited after a burst of requests. Running a long task backlog means babysitting the agent, watching for stalls, and manually restarting — defeating the point of automation.

Task Orchestrator solves this by:

1. **Spacing out requests** so providers don't get rate-limited as quickly
2. **Rotating to the next provider** when one gets exhausted, instead of sitting idle
3. **Verifying results** before marking tasks complete
4. **Running unattended** overnight with crash recovery

## Key Features

- **100% offline** — zero telemetry, no phone-home, your API keys never leave your machine
- **Zero dependencies** — Python 3.9+ stdlib only
- **Cross-platform** — Windows, macOS, Linux
- **Any agent CLI** — Copilot, Claude, Kilo, Codex, Ollama, or any CLI that accepts a prompt
- **Rate-limit rotation** — exponential backoff, automatic failover
- **Verification gates** — run tests/lints before accepting results
- **Live dashboard** — local HTTP dashboard with provider status
- **pip installable** — `pip install task-orchestrator`

!!! note "Your keys are safe"
    The orchestrator itself makes **zero network requests**. Only your configured provider CLIs talk to APIs. Secrets are auto-redacted from logs. Nothing is ever uploaded anywhere.

## Quick Links

- [Quickstart →](quickstart.md)
- [Dashboard V2 →](dashboard-v2.md)
- [Configuration Reference →](configuration.md)
- [Provider Guide →](providers.md)
- [FAQ →](faq.md)
- [Troubleshooting →](troubleshooting.md)
