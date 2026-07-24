# Troubleshooting

## Startup Errors

### "Config file not found"

Run `task-orchestrator init` to create starter files, or specify a path with `--config path/to/task-orchestrator.config.json`.

### "No enabled providers configured"

At least one provider in `providers[]` must have `"enabled": true` (or omit the field — it defaults to true).

### "Not inside a git working tree"

The orchestrator uses git for change detection (suspicious-completion checks, rate-limit false-positive checks) and auto-commit — this is a hard requirement, not optional. `working_directory` must be inside a git working tree or the orchestrator exits immediately at startup with this error; there is no degraded/git-disabled mode.

Fix: `git init && git add -A && git commit -m "initial"` inside `working_directory` before starting the orchestrator.

### "Provider command not found"

Run `task-orchestrator validate` to see which providers are resolvable. Common fixes:

- Ensure the CLI is installed and on your PATH
- On Windows: the CLI might be a `.cmd`, `.ps1`, or `.exe` — the orchestrator tries all suffixes automatically
- Check that any required `env` variables are set

## Runtime Issues

### Task marked "SUSPICIOUS" despite succeeding

This means exit code was 0 but `git status --porcelain` showed no file changes. Common causes:

1. The agent said "done" but didn't actually modify any files
2. The only changes were to files in `.gitignore` (state.json, logs/, etc.)
3. The agent's working directory doesn't match `working_directory` in config

Fix: ensure `.gitignore` covers runtime files (state.json, orchestrator.pid, logs/, Todo.md.lock).

### False rate-limit detection

If your task text or generated code contains words like "rate limit" or "429", the orchestrator might incorrectly flag a successful run as rate-limited. The fix is already built in: rate-limit detection is only trusted when `git status` shows no file changes actually happened.

### Provider keeps getting rate-limited

The orchestrator uses exponential backoff (doubles each consecutive hit, capped at 64x base cooldown). If a provider is consistently exhausted:

1. Increase `cooldown_seconds` for that provider
2. Add more providers for automatic failover
3. Reduce task frequency with higher `delay_seconds`

### Stall detected (task killed after 600s)

The stall detector kills tasks with no CPU activity AND no file changes for `stall_timeout_seconds`. If your task legitimately needs idle time (e.g., waiting for an API response):

- Increase `stall_timeout_seconds`
- Or tag the task `[slow]` with a timeout override in config

### Dashboard not accessible

- Check `dashboard_port` in config is set to a number (not null)
- If the port is busy, the orchestrator auto-retries on the next port — check the log for the actual URL
- Dashboard is only accessible from localhost (127.0.0.1)

## Windows-Specific

### "No module named 'fcntl'"

This is already handled — the orchestrator uses `msvcrt` on Windows instead. If you see this error, you're running an older version; update to v2.0+.

### Copilot CLI resolves to a .ps1 script

Handled automatically. The orchestrator detects `.ps1` commands and wraps them through `pwsh`/`powershell` with `-NoProfile -ExecutionPolicy Bypass`.

### "python: command not found" / "python3: command not found"

Provider commands, `verify_commands`, and `stats_command` all auto-resolve between `python`, `python3`, and finally this process's own interpreter — whichever is actually on `PATH`. You shouldn't need to edit `task-orchestrator.config.json` to match your OS's naming convention; if you still see this error, the command you configured uses a different interpreter name entirely (e.g. a venv-specific path) that isn't resolvable at all.

## Getting Help

1. Check `logs/orchestrator.log` for detailed error messages
2. Run with `--json-logs` for structured output
3. Use `validate` to check your setup
4. Use `--dry-run-prompt` to inspect what's being sent
5. File an issue: [GitHub Issues](https://github.com/sagaryeole/ai-task-orchestrator/issues)
