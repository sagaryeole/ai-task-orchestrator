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
ruff check src/ orchestrator.py tests/
```

> **Where to make changes:** All production code lives in the installable
> package at `src/task_orchestrator/`. The root-level `orchestrator.py` is a
> backward-compatible shim only — do **not** add features there. Edit the
> appropriate module under `src/task_orchestrator/` instead. See
> [docs/contributing-architecture.md](../docs/contributing-architecture.md) for a
> full "where does X go?" mapping of feature areas to modules.

## Architecture

- **`orchestrator.py`** (repo root) — a thin backward-compatible shim. It inserts
  `src/` onto `sys.path`, imports `task_orchestrator.runner`, and replaces itself
  in `sys.modules` with that module so that `python orchestrator.py` and the
  installed `task-orchestrator` CLI run identical code. **Do not add features
  here** — it exists solely so the classic `python orchestrator.py` invocation
  keeps working. All logic lives in the package below.
- **`src/task_orchestrator/`** — the installable package (where you make changes):
  - `__init__.py` — package version
  - `__main__.py` — enables `python -m task_orchestrator`
  - `cli.py` — the `task-orchestrator` entry point; owns all `argparse` setup
    (subcommands `init`, `validate`, `run`, plus flags like `--once`,
    `--dry-run`, `--provider`, etc.) and delegates to `runner.main(args)`
  - `runner.py` — the main task loop, `Provider` class, config/state loading,
    git operations, todo manipulation, dashboard, notifications, and all
    supporting helpers

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
