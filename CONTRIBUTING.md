# Contributing to Task Orchestrator

Thank you for considering contributing! This project aims to be the best AI-agnostic task orchestrator — simple, robust, and cross-platform.

## Development Setup

```bash
# Clone the repository
git clone https://github.com/sagaryeole/ai-task-orchestrator.git
cd ai-task-orchestrator

# Install in development mode
pip install -e .

# Run tests
python -m unittest discover -s tests

# Run linting (optional, requires ruff)
pip install ruff
ruff check src/
```

## Architecture

- **`orchestrator.py`** — a thin backward-compatible shim. It inserts `src/` onto `sys.path`, imports `task_orchestrator.runner`, and replaces itself in `sys.modules` with that module, so `python orchestrator.py` and the installed `task-orchestrator` CLI run identical code.
- **`src/task_orchestrator/`** — the installable package:
  - `__init__.py` — package version
  - `cli.py` — the `task-orchestrator` entry point; handles `--version` and otherwise delegates to `runner.main()`
  - `runner.py` — everything else: config/state loading, the `Provider` class, the `init`/`validate` subcommands, the dashboard, and the main task loop

`runner.py` is intentionally one file rather than split into per-concern modules — the functions inside it (config loading, provider handling, git operations, logging/secret-masking, etc.) are still logically separated by the section comments (`# ---- Config / state / logging ----`, `# ---- Provider pool ----`, and so on), but there's no hard module boundary yet. A future PR splitting it into `config.py`/`provider.py`/`git.py`/etc. is a reasonable idea, not something already done — if you take that on, keep `orchestrator.py`'s shim behavior working and update this section to match.

## Pull Request Process

1. Fork the repo and create a feature branch from `main`
2. Write tests for new functionality
3. Ensure `python -m unittest discover -s tests` passes on your platform
4. Keep changes focused — one feature or fix per PR
5. Update README/docs if you're adding user-facing features

## Code Style

- Python 3.9+ compatible (no walrus operator in hot paths, use `from __future__ import annotations`)
- Type hints on all public functions
- No external dependencies in core (stdlib only) — optional extras are fine for plugins/dev tools
- Use `ruff` for linting (config in `pyproject.toml`)

## Adding a New Provider Example

Add a JSON file to `examples/` with your provider config. Include:
- The exact `command` invocation
- Which `rate_limit_patterns` to use
- Any required `env` variables (use `$VAR_NAME` interpolation syntax)
- A brief comment explaining the setup

## Reporting Issues

- Include your OS, Python version, and agent CLI version
- Paste the relevant section of `logs/orchestrator.log`
- For rate-limit detection issues, include the CLI output that was/wasn't matched
