# FAQ

## General

### Is this safe to use with my API keys?

Yes. The orchestrator itself makes **zero network requests** — it's a pure local process manager. Your API keys live in environment variables on your machine and are passed to provider CLIs via the OS process environment. They are:

- Never logged in plain text (auto-redacted from log output)
- Never uploaded anywhere
- Never stored in config files (use `$VAR` interpolation instead)
- Never sent to any telemetry/analytics endpoint (there are none)

The orchestrator is fully offline. Only your provider CLIs (Copilot, Claude, etc.) make outbound API calls — and only to the endpoints you configured them to talk to.

### What AI models/providers does this work with?

Any CLI that accepts a text prompt and exits when done. It's not tied to any specific AI provider — if you can invoke it from a terminal, it works. Tested with: GitHub Copilot CLI, Claude Code, Kilo Code, Aider, and Ollama.

### Does this require an internet connection?

Only if your providers need one. A local Ollama setup works fully offline.

### Is this just for coding tasks?

The orchestrator doesn't care what the tasks are — it sends text to a CLI and checks exit codes. It's designed for coding agents but works for any CLI-driven automation (writing, data processing, etc.).

### Why not just use a shell script loop?

A shell loop doesn't handle: rate-limit detection, automatic provider rotation, exponential backoff, verification gates, stall detection, state persistence across crashes, or a live dashboard. This tool exists because those things matter for overnight unattended runs.

## Configuration

### How do I keep API keys out of task-orchestrator.config.json?

Use environment variable interpolation:

```json
"env": {"ANTHROPIC_API_KEY": "$ANTHROPIC_API_KEY"}
```

Set the variable in your shell profile or `.env` file (which should be in `.gitignore`). Separately, `task-orchestrator init` already gitignores `task-orchestrator.config.json` itself by default, since even with interpolation some users paste literal keys in — if you're hand-editing an existing `.gitignore`, make sure that entry is still there.

### Can I use different configs for different projects?

Yes. Each project has its own `task-orchestrator.config.json`. Use `--config path/to/other.json` to point at a specific one. Global credentials in `~/.task-orchestrator/config.json` are automatically merged.

### What's the difference between `subprocess_timeout` and `stall_timeout_seconds`?

- **`subprocess_timeout`** — hard wall-clock limit. Task is killed after this many seconds regardless of activity.
- **`stall_timeout_seconds`** — activity-based. Only kills if there's been zero CPU activity AND zero file changes for this long. A task actively working will never trip this even if it takes hours.

Use `subprocess_timeout: null` (no wall-clock limit) with `stall_timeout_seconds: 600` for best results.

## Providers

### My provider shows "command not found" — what's wrong?

The orchestrator resolves commands via `shutil.which()`. Common causes:

1. The CLI isn't installed or isn't on PATH
2. On Windows, the CLI is a `.ps1` script (automatically handled) or needs a different PATH entry
3. Run `task-orchestrator validate` to check reachability

### How do I know what `rate_limit_patterns` to use?

Run your agent CLI manually until it hits a rate limit, then look at the exact error text. Common patterns: `"rate limit"`, `"429"`, `"quota exceeded"`, `"too many requests"`, `"overloaded"`.

### What if my provider doesn't print rate-limit errors?

Set `rate_limit_patterns: []`. The orchestrator will still handle non-zero exit codes as failures and retry according to `max_retries_per_provider`.

## Tasks

### Can I add tasks while the orchestrator is running?

Yes. The orchestrator re-reads `Todo.md` before each task. Just add new `- [ ]` lines and they'll be picked up on the next cycle.

### What happens if a task fails?

Depends on `on_failure` config:

- `"skip"` (default) — task stays unchecked, orchestrator moves to the next one
- `"defer"` — task is moved to the end of `Todo.md`
- `"stop"` — orchestrator halts

### Can I skip a task that keeps failing?

In manual confirmation mode: type `skip-task` at the prompt.
With interactive commands enabled: press `s` + Enter during execution.
Or just mark it `[x]` manually in `Todo.md`.

## Reliability

### What happens if the orchestrator crashes?

All state is persisted to disk immediately (state.json, Todo.md checkboxes). Restart with `run_forever.sh` (Linux/Mac) or `run_forever.ps1` (Windows) — it auto-restarts on crashes and resumes from where it left off.

### Is it safe to run overnight?

Yes, that's the primary use case. With `require_manual_confirmation: false`, `auto_commit: true`, and `verify_commands` set to your test suite, it runs fully unattended.

### Can two orchestrators run against the same Todo.md?

Technically yes — there's file locking to prevent corruption. But it's not designed for this; you'll get confusing task assignment behavior.
