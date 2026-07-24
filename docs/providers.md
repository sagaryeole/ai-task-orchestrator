# Provider Guide

A **provider** is any agent CLI the orchestrator can launch as a subprocess. This guide covers setup for popular providers.

## How Providers Work

The orchestrator launches your provider's `command` as a subprocess, passes the task prompt (via stdin or `{{TASK}}` arg substitution), captures output, and checks for rate-limit patterns.

**Two prompt delivery modes:**

1. **Stdin (default)** — prompt is piped to the process's stdin
2. **Arg-based** — include `{{TASK}}` in your command; the full prompt replaces it as a single argument

## GitHub Copilot CLI

```json
{
  "name": "copilot",
  "command": "copilot --allow-all-tools --no-ask-user -s -p {{TASK}}",
  "env": {},
  "rate_limit_patterns": ["rate limit", "429", "too many requests"],
  "cooldown_seconds": 300
}
```

**Prerequisites:**

- Install: comes with VS Code or `npm install -g @githubnext/github-copilot-cli`
- Authenticate: `copilot login` (one-time interactive)
- Required flags: `--allow-all-tools` (no tool approval prompts), `--no-ask-user` (no clarifying questions), `-s` (silent output)

**Prompt mode:** Arg-based (`{{TASK}}`)

## Claude Code CLI

```json
{
  "name": "claude",
  "command": "claude --no-interactive --print",
  "env": {"ANTHROPIC_API_KEY": "$ANTHROPIC_API_KEY"},
  "rate_limit_patterns": ["rate limit", "429", "overloaded", "capacity"],
  "cooldown_seconds": 600
}
```

**Prerequisites:**

- Install: `npm install -g @anthropic-ai/claude-code`
- Set `ANTHROPIC_API_KEY` in your environment

**Prompt mode:** Stdin

## Kilo Code

```json
{
  "name": "kilo",
  "command": "kilo run --auto",
  "env": {},
  "rate_limit_patterns": ["rate limit", "429", "quota exceeded"],
  "cooldown_seconds": 300
}
```

**Prerequisites:**

- Install Kilo Code extension/CLI
- `--auto` flag for non-interactive mode

**Prompt mode:** Stdin

## Aider

```json
{
  "name": "aider",
  "command": "aider --yes --no-git --message {{TASK}}",
  "env": {"OPENAI_API_KEY": "$OPENAI_API_KEY"},
  "rate_limit_patterns": ["rate limit", "429", "quota"],
  "cooldown_seconds": 600
}
```

**Prompt mode:** Arg-based (`{{TASK}}`)

## Ollama (Local)

```json
{
  "name": "ollama",
  "command": "ollama run codellama",
  "env": {},
  "rate_limit_patterns": [],
  "cooldown_seconds": 10,
  "priority": 1
}
```

No rate limits (local), low priority as fallback.

**Prompt mode:** Stdin

## Multi-Provider Setup

Combine providers with priority for automatic failover:

```json
{
  "providers": [
    {"name": "claude", "priority": 10, "cooldown_seconds": 600, "...": "..."},
    {"name": "copilot", "priority": 5, "cooldown_seconds": 300, "...": "..."},
    {"name": "ollama", "priority": 1, "cooldown_seconds": 10, "...": "..."}
  ]
}
```

The orchestrator uses the highest-priority available provider. When Claude is rate-limited, it falls to Copilot. When both are exhausted, it uses the local Ollama model.

## Adding Your Own Provider

Any CLI that:

1. Accepts a prompt (via stdin or as a CLI argument)
2. Produces output on stdout/stderr
3. Exits with code 0 on success

...works as a provider. To add one:

1. Choose a unique `name`
2. Write the exact CLI command (the orchestrator uses `shlex.split()`)
3. Set any required credentials in `env` (use `$VAR` for secrets)
4. Identify what the CLI prints when rate-limited → add to `rate_limit_patterns`
5. Set `cooldown_seconds` for how long to wait after a hit

## Validating Your Setup

```bash
# Check if providers are reachable
task-orchestrator validate

# See what prompt would be sent
task-orchestrator --dry-run-prompt

# Force a specific provider
task-orchestrator --provider copilot --once
```
