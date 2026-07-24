# Configuration Reference

Configuration is stored in `config.json` (or any path passed via `--config`).

!!! tip "IDE Autocompletion"
    Add `"$schema": "./config.schema.json"` at the top of your config file for inline validation and autocompletion in VS Code / JetBrains.

## Top-Level Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `todo_file` | string | *required* | Path to the markdown task list |
| `working_directory` | string | `"."` | Working directory for agent CLI and verify commands |
| `prompt_template` | string | `"prompts/task_prompt.txt"` | Path to prompt template (must contain `{{TASK}}`) |
| `delay_seconds` | int | `60` | Pause between completed tasks |
| `subprocess_timeout` | int \| null | `null` | Wall-clock timeout per task (null = no limit) |
| `stall_timeout_seconds` | int | `600` | Kill task if no CPU/file activity for this long |
| `max_retries_per_provider` | int | `1` | Retry attempts on non-rate-limit failures |
| `require_manual_confirmation` | bool | `false` | Prompt for approval after each task |
| `continue_on_failure` | bool | `true` | If false, stop on first failed task |
| `on_failure` | string | `"skip"` | Behavior on failure: `skip`, `defer`, or `stop` |
| `auto_commit` | bool | `true` | Git commit after each completed task |
| `verify_commands` | string[] | `[]` | Commands that must exit 0 for task verification |
| `json_logs` | bool | `false` | Enable structured JSON logging |
| `dashboard_port` | int \| null | `null` | Local dashboard HTTP port (null = disabled) |

## Provider Fields

Each entry in the `providers[]` array:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | *required* | Unique identifier (used in logs and state) |
| `enabled` | bool | `true` | Set false to disable without deleting |
| `command` | string | *required* | CLI invocation. Use `{{TASK}}` for arg-based prompt |
| `env` | object | `{}` | Environment variables (supports `$VAR` interpolation) |
| `rate_limit_patterns` | string[] | `[]` | Lowercase substrings indicating rate-limit in output |
| `cooldown_seconds` | int | `600` | How long to skip after rate-limit hit |
| `priority` | int | `0` | Higher = preferred (0 = round-robin) |
| `stats_command` | string \| null | `null` | Command to collect usage stats after each task |

## Environment Variable Interpolation

Config values support `$VAR_NAME` and `${VAR_NAME}` syntax:

```json
{
  "env": {
    "ANTHROPIC_API_KEY": "$ANTHROPIC_API_KEY",
    "CUSTOM_URL": "${MY_BASE_URL}/v1"
  }
}
```

Variables are resolved from the process environment at startup. Unset variables trigger a warning but are left as-is.

## Global Config

`~/.task-orchestrator/config.json` is loaded as a base config. Project-local config values override global ones. Useful for storing provider credentials once across all projects.

## Per-Task Timeout Overrides

Tag tasks with `[big]` or `[slow]` to apply longer timeouts:

```json
{
  "subprocess_timeout_overrides": {
    "[big]": 900,
    "[slow]": 1800
  }
}
```

```markdown
- [ ] [big] Refactor the entire authentication system
- [ ] [slow] Run full integration test suite
```

## On-Failure Behavior

The `on_failure` field controls what happens when a task fails all retries:

| Value | Behavior |
|-------|----------|
| `"skip"` | Leave task unchecked, skip it this cycle, move to next (default) |
| `"defer"` | Move task to end of Todo.md (legacy behavior) |
| `"stop"` | Halt the orchestrator immediately |

## Full Example

```json
{
  "$schema": "./config.schema.json",
  "todo_file": "Todo.md",
  "working_directory": ".",
  "prompt_template": "prompts/task_prompt.txt",
  "delay_seconds": 45,
  "subprocess_timeout": null,
  "stall_timeout_seconds": 600,
  "max_retries_per_provider": 2,
  "require_manual_confirmation": false,
  "continue_on_failure": true,
  "on_failure": "skip",
  "auto_commit": true,
  "verify_commands": ["python -m pytest tests/ -x -q"],
  "dashboard_port": 8765,
  "providers": [
    {
      "name": "claude",
      "command": "claude --no-interactive --print",
      "env": {"ANTHROPIC_API_KEY": "$ANTHROPIC_API_KEY"},
      "rate_limit_patterns": ["rate limit", "429", "overloaded"],
      "cooldown_seconds": 600,
      "priority": 10
    },
    {
      "name": "copilot",
      "command": "copilot --allow-all-tools --no-ask-user -s -p {{TASK}}",
      "env": {},
      "rate_limit_patterns": ["rate limit", "429", "too many requests"],
      "cooldown_seconds": 300,
      "priority": 5
    }
  ]
}
```
