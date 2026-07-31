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

- `task-orchestrator.config.json` — provider configuration (gitignored by default — it commonly holds API keys)
- `Todo.md` — your task backlog
- `prompts/task_prompt.txt` — prompt template
- `.gitignore` — excludes runtime files

## Configure a Provider

Edit `task-orchestrator.config.json` and set your agent CLI command:

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
        "command": "claude -p --permission-mode bypassPermissions",
        "env": {"ANTHROPIC_API_KEY": "$ANTHROPIC_API_KEY"},
        "rate_limit_patterns": ["rate limit", "429", "overloaded"],
        "cooldown_seconds": 600
      }]
    }
    ```

=== "Claude Code"

    ```json
    {
      "providers": [{
        "name": "claude",
        "command": "claude -p --permission-mode bypassPermissions",
        "env": {},
        "rate_limit_patterns": ["rate limit", "429", "overloaded", "capacity"],
        "cooldown_seconds": 600
      }]
    }
    ```

=== "Codex CLI"

    ```json
    {
      "providers": [{
        "name": "codex",
        "command": "codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox",
        "env": {"OPENAI_API_KEY": "$OPENAI_API_KEY"},
        "rate_limit_patterns": ["rate limit", "429", "quota exceeded"],
        "cooldown_seconds": 300
      }]
    }
    ```

    Uses `codex exec` which reads the prompt from stdin. Requires the
    [codex CLI](https://github.com/openai/codex) and an `OPENAI_API_KEY`.
    For local models, add `--oss --local-provider lmstudio -m <model>`
    (or `ollama`) and drop the API key.

=== "Aider"

    ```json
    {
      "providers": [{
        "name": "aider",
        "command": "aider --yes-always --no-pretty --no-stream --no-auto-commits --message-file -",
        "env": {"OPENAI_API_KEY": "$OPENAI_API_KEY"},
        "rate_limit_patterns": ["rate limit", "429", "quota"],
        "cooldown_seconds": 300
      }]
    }
    ```

    Uses `--message-file -` to read the prompt from stdin. Requires
    [aider](https://aider.chat) and an LLM API key. For a local
    OpenAI-compatible server (LM Studio, Ollama), add
    `--model openai/<model> --openai-api-base <url> --api-key openai=<dummy>`
    and drop the real API key.

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

=== "Ollama (local)"

    ```json
    {
      "providers": [{
        "name": "ollama",
        "command": "ollama run codellama",
        "env": {},
        "rate_limit_patterns": [],
        "cooldown_seconds": 10,
        "priority": 2
      }]
    }
    ```

=== "Lm Studio (local)"

    ```json
    {
      "providers": [{
        "name": "lmstudio",
        "command": "python src/task_orchestrator/lmstudio_provider.py",
        "env": {
          "LM_STUDIO_URL": "http://127.0.0.1:1234/v1",
          "LM_STUDIO_MODEL": "qwen/qwen3.5-9b"
        },
        "rate_limit_patterns": [],
        "cooldown_seconds": 60,
        "priority": 1
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
