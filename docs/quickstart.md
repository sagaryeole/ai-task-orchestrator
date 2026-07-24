# Quickstart

Get running in under 2 minutes.

!!! success "100% Offline & Private"
    Task Orchestrator runs entirely on your machine. It never phones home, sends telemetry, or touches your API keys. Only your configured agent CLIs make outbound requests — the orchestrator itself has zero network activity.

## Install

```bash
pip install task-orchestrator
```

Or clone and install in development mode:

```bash
git clone https://github.com/sagaryeole/ai-task-orchestrator.git
cd ai-task-orchestrator
pip install -e .
```

## Initialize a Project

```bash
task-orchestrator init
```

This creates:

- `config.json` — provider configuration
- `Todo.md` — your task backlog
- `prompts/task_prompt.txt` — prompt template
- `.gitignore` — excludes runtime files

## Configure a Provider

Edit `config.json` and set your agent CLI command:

=== "GitHub Copilot"

    ```json
    {
      "providers": [{
        "name": "copilot",
        "command": "copilot --allow-all-tools --no-ask-user -s -p {{TASK}}",
        "env": {},
        "rate_limit_patterns": ["rate limit", "429", "too many requests"],
        "cooldown_seconds": 300
      }]
    }
    ```

=== "Claude Code"

    ```json
    {
      "providers": [{
        "name": "claude",
        "command": "claude --no-interactive --print",
        "env": {"ANTHROPIC_API_KEY": "$ANTHROPIC_API_KEY"},
        "rate_limit_patterns": ["rate limit", "429", "overloaded"],
        "cooldown_seconds": 600
      }]
    }
    ```

=== "Kilo Code"

    ```json
    {
      "providers": [{
        "name": "kilo",
        "command": "kilo run --auto",
        "env": {},
        "rate_limit_patterns": ["rate limit", "429", "quota exceeded"],
        "cooldown_seconds": 300
      }]
    }
    ```

## Add Tasks

Edit `Todo.md`:

```markdown
## Tasks

- [ ] Add input validation to the login form
- [ ] Write unit tests for the payment module
- [ ] Refactor the database connection pool
```

## Run

```bash
# Preview what would happen
task-orchestrator --dry-run

# Run one task
task-orchestrator --once

# Run all tasks continuously
task-orchestrator

# Run with JSON logs for debugging
task-orchestrator --json-logs
```

## What Happens

1. Orchestrator reads the first unchecked task from `Todo.md`
2. Builds a prompt from `prompts/task_prompt.txt`
3. Sends it to the next available provider
4. Checks if the provider was rate-limited (pattern matching on output)
5. If rate-limited → marks provider on cooldown, rotates to next
6. If successful → runs verification commands → marks task `[x]` → auto-commits
7. Waits `delay_seconds`, then picks up the next task

## Next Steps

- [Configuration Reference](configuration.md) — all config options explained
- [Provider Guide](providers.md) — detailed setup for each agent CLI
- [FAQ](faq.md) — common questions answered
