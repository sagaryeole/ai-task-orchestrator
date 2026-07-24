# Task Orchestrator

A small, AI-agnostic orchestrator that drives a coding-agent CLI (Kilo Code, Claude Code, Codex, or similar) through a backlog of tasks one at a time, with rate-limit-aware pacing and automatic fallback across multiple providers/models.

## Motivation

Free-tier and "auto" routed models (e.g. Kilo Code's Auto Free mode) stop responding after a burst of requests because the underlying provider rate-limits them. Running a long backlog of tasks by hand means babysitting the agent, watching for it to stall, and manually restarting it — which defeats the point of automation.

Two things were needed:

1. A way to space out requests so a single free provider doesn't get rate-limited as quickly.
2. A way to keep working even when a provider does get rate-limited, by switching to another available provider/model instead of sitting idle.

Prompting the agent itself to "wait 60 seconds and then continue" doesn't work — an LLM has no real clock or way to pause execution mid-conversation, and each task is typically a separate CLI invocation anyway. The delay and the failover both have to live outside the model, in a controlling process.

## Requirements

- Work through a checklist of tasks (`Todo.md`) one at a time, marking each complete as it finishes.
- Be agnostic to which coding agent CLI is actually doing the work, so the underlying tool can be swapped without rewriting the orchestrator.
- Support multiple providers/models (Anthropic, OpenRouter, Nvidia NIM, a local LM Studio server, etc.), each with its own credentials and launch command.
- Detect when a provider is rate-limited or exhausted, based on its output.
- When a provider is exhausted, automatically continue the same task on the next available provider instead of stopping or waiting for that specific provider to recover.
- If every configured provider is exhausted at the same time, wait until the soonest one becomes available again rather than failing outright.
- Apply a configurable delay between tasks even on success, to reduce the chance of re-triggering a limit.
- Optionally verify a task's result (build/lint/test commands) before marking it complete, and optionally auto-commit the result to git.
- Support both fully automatic operation and a manual-confirmation mode where a human reviews each task's output before it's marked done.
- Persist provider cooldown state across restarts, so relaunching the script doesn't immediately retry a provider that's still rate-limited.
- Log everything (per-attempt status, provider used, exit codes, verification results) to a file, not just the terminal.

## Tech Spec

- **Language:** Python 3, standard library only (`subprocess`, `json`, `re`, `time`, `pathlib`, `datetime`). No external dependencies to install.
- **Files:**
  - `orchestrator.py` — the orchestrator itself.
  - `config.json` — providers, delays, retry policy, verification commands.
  - `Todo.md` — the task backlog, using standard GitHub-flavored checkbox syntax (`- [ ]` / `- [x]`).
  - `prompts/task_prompt.txt` — template used to build the prompt sent to the agent for each task (`{{TASK}}` placeholder).
  - `state.json` — auto-created; records per-provider cooldown-until timestamps.
  - `logs/orchestrator.log` — auto-created; append-only human-readable run log.
  - `logs/orchestrator.jsonl` — auto-created when `--json-logs` (or `"json_logs": true` in `config.json`) is set; one JSON object per line, for downstream parsing/dashboards.
- **Providers:** each is a plain CLI launch — a command string plus an environment variable overlay (API keys, base URLs) plus a list of substrings/regex fragments that indicate a rate limit was hit. This makes a "provider" nothing more than "however you'd normally invoke your agent CLI with a specific model/backend," so it works with Anthropic's API, OpenRouter, Nvidia NIM's OpenAI-compatible endpoint, a local LM Studio server, or anything else reachable via a CLI flag or env var.
- **Process model:** each task attempt is a synchronous subprocess call — the prompt is piped to the agent CLI's stdin, and its combined stdout/stderr is captured for rate-limit detection and printed live to the terminal.
- **State machine per task:** pick provider → run → check for rate-limit pattern → (if limited) cooldown that provider and rotate to next → (if not limited) verify → confirm/auto-accept → mark complete or retry/give up → sleep `delay_seconds` → next task.

## Solution / How It Works

```
Todo.md ──► next unchecked task
               │
               ▼
      pick next available provider (round-robin, skipping any on cooldown)
               │
               ▼
      run agent CLI with that provider's env + command, prompt piped to stdin
               │
               ▼
      output matches a rate-limit pattern?
        │                          │
       yes                        no
        │                          │
  mark provider on           run verify_commands (optional)
  cooldown, rotate                 │
  to next provider           require_manual_confirmation?
  (no delay — retry                │            │
   immediately)                   yes           no
        │                          │            │
        │                    ask y/n/retry   exit_code==0
        │                          │         and verified?
        └──────────┐               ▼            │
                    │        mark complete   mark complete
                    │        + git commit    + git commit
                    ▼               │            │
        (loop: try next            └─────┬──────┘
         provider for                    ▼
         same task)              sleep delay_seconds
                                         │
                                         ▼
                                  next task in Todo.md
```

If every provider is on cooldown at once, the orchestrator sleeps until the earliest cooldown expires, then re-checks — it never gives up as long as at least one provider will eventually free up.

### Configuration reference (`config.json`)

| Field | Purpose |
|---|---|
| `todo_file` | Path to the checklist file |
| `working_directory` | Directory the agent CLI and verification commands run in |
| `prompt_template` | Path to the prompt template file (`{{TASK}}` is replaced with the task text) |
| `delay_seconds` | Pause after each successfully completed task |
| `max_retries_per_provider` | Retries on the same provider before treating it as a failure (not used for rate-limit hits — those rotate immediately) |
| `require_manual_confirmation` | If `true`, you approve each task result (`y`/`n`/`retry`/`skip-provider`) before it's marked complete |
| `continue_on_failure` | If `false`, the orchestrator stops entirely on the first task that can't be completed |
| `auto_commit` | If `true`, runs `git add -A && git commit` after each completed task |
| `verify_commands` | Shell commands (e.g. `pytest`, `npm test`) that must all exit 0 for a task to count as verified |
| `providers[]` | List of provider entries — see below |

Each entry in `providers`:

| Field | Purpose |
|---|---|
| `name` | Identifier used in logs and `state.json` |
| `enabled` | Set `false` to exclude a provider without deleting it |
| `command` | The CLI invocation for this provider (flags for model, base URL, etc.) |
| `env` | Environment variables merged on top of the current environment (API keys, base URLs) |
| `rate_limit_patterns` | Lowercase substrings checked against the CLI's combined stdout/stderr; a match marks the provider exhausted |
| `cooldown_seconds` | How long an exhausted provider is skipped before being retried |

The shipped `config.json` includes four example providers — `openrouter-free`, `anthropic-claude`, `nvidia-nim`, and `lmstudio-local` — as a starting template. The `command` strings and `rate_limit_patterns` are placeholders; they need to be adjusted to match your actual CLI's flags and the exact wording it prints on a rate limit, since that wording varies by tool and provider.

### Optional fields

| Field | Purpose |
| |--- |
| `priority` | Integer priority for provider selection (higher = preferred). When any provider has an explicit priority, the orchestrator picks the highest-priority available provider. Providers without a `priority` default to `0`. |

### Adding a new provider

A provider is anything the orchestrator can launch as a subprocess. To add one:

1. Choose a unique `name` for the provider.
2. Write the exact CLI command in `command`. The orchestrator uses `shlex.split()` to parse it, so you can safely include quoted arguments and paths with spaces.
3. Set any required credentials in `env`.
4. Provide `rate_limit_patterns` — lowercase substrings that appear in the CLI's output when the underlying API returns `429`, quota exhaustion, or similar limits.
5. Set `cooldown_seconds` to determine how long the provider is skipped after a rate-limit hit.

Example:

```json
{
  "name": "my-new-provider",
  "enabled": true,
  "command": "my-agent-cli --auto --model some-model",
  "env": { "MY_API_KEY": "REPLACE_ME" },
  "rate_limit_patterns": ["rate limit", "429", "quota exceeded"],
  "cooldown_seconds": 3600
}
```

## Usage

1. Fill in real API keys and correct CLI flags in `config.json` for each provider you want to use; set `enabled: false` on any you don't.
2. Edit `Todo.md` with your real task list.
3. Run:
   ```
   python orchestrator.py
   ```
4. Watch `logs/orchestrator.log` (or the terminal) for progress; if `require_manual_confirmation` is `true`, respond to the prompt after each attempt.

### CLI flags

- `--config <path>` — use a non-default config file
- `--dry-run` — print the next task and provider without executing anything
- `--once` — run a single task and exit
- `--json-logs` — append structured JSON log lines to `logs/orchestrator.jsonl` alongside normal logs
- `--summary` — print a summary of today's run statistics (tasks completed, success rate, total run time, average time per task) and exit

## Known Limitations / Follow-ups

- Rate-limit detection is text-pattern matching against CLI output, not a structured API response — patterns need to be tuned per tool/provider and may need updates if a CLI changes its error wording.
- Providers are tried round-robin by default, but you can set a `priority` field to prefer specific providers (higher = preferred).

## Dashboard

A minimal local dashboard is served via Python's stdlib `http.server` — no external dependencies required. It exposes the current run state (active task, provider status, recent history) as both JSON and an HTML page.

### Enabling

Set `dashboard_port` in `config.json` to the port you want the dashboard to listen on:

```json
{
  "dashboard_port": 8080
}
```

When `dashboard_port` is `null` (the default), the dashboard is disabled.

### Endpoints

| Path | Content-Type | Description |
|---|---|---|
| `/` | `text/html` | Human-readable dashboard with auto-refresh (5s) |
| `/api/state` | `application/json` | Machine-readable current run state |

### JSON response shape (`/api/state`)

```json
{
  "current_task": "Task one",
  "current_provider": "kilo",
  "providers": [
    {"name": "kilo", "available": true, "cooldown_until": null}
  ],
  "history": [
    {"task": "Task one", "provider": "kilo", "status": "complete", "timestamp": "2026-07-24T11:45:58"}
  ],
  "uptime_seconds": 123.4
}
```
