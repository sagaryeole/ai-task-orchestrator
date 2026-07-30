# Architecture

<!-- markdown-toc start -->
- [Directory Tree](#directory-tree)
- [Module Purposes](#module-purposes)
<!-- markdown-toc end -->

## Directory Tree

```
ai-task-orchestrator/
├── orchestrator.py                # Backward-compatible shim → task_orchestrator.runner
├── src/task_orchestrator/
│   ├── __init__.py                # Package init; declares __version__
│   ├── __main__.py                # Enables `python -m task_orchestrator`
│   ├── cli.py                     # argparse CLI entry point (run / init / validate)
│   ├── orchestrator.py            # Core engine: Provider, task loop, rotation, verification
│   ├── runner.py                  # Backward shim re-exporting from submodules
│   ├── git.py                     # Git operations + Todo.md load/mark/defer/lock
│   ├── dashboard.py               # Local HTTP server for the live dashboard
│   └── notify.py                  # Desktop notifications, ANSI styling, audio cues, banner
├── dashboard/
│   └── templates/
│       └── index.html             # Dashboard HTML template (served by dashboard.py)
├── prompts/
│   └── task_prompt.txt            # Prompt template with {{TASK}} placeholder
├── scripts/
│   └── sync_todo.py               # Todo.md sync utility
├── tests/
│   ├── test_orchestrator.py       # Unit tests
│   └── test_integration.py        # Integration tests
├── docs/
│   ├── index.md                   # Docs landing page
│   ├── architecture.md            # This file
│   ├── quickstart.md              # First-time setup guide
│   ├── configuration.md           # Config reference
│   ├── providers.md               # Provider compatibility & setup
│   ├── dashboard-v2.md            # Dashboard documentation
│   ├── faq.md                     # Frequently asked questions
│   ├── troubleshooting.md         # Common problems & fixes
│   └── changelog.md               # Release notes
├── task-orchestrator.config.json  # Runtime config (providers, todo file, delays)
├── task-orchestrator.config.schema.json  # JSON schema for config validation
├── pyproject.toml                 # packaging metadata
├── Makefile                       # Shortcuts for test/lint/typecheck
└── README.md                      # Project overview and quickstart
```

## Module Purposes

### `orchestrator.py` (root)
A backward-compatible shim: it inserts `src/` onto `sys.path`, imports `task_orchestrator.runner`, and replaces itself in `sys.modules` so that `python orchestrator.py` and `from orchestrator import X` continue to work exactly as they did before the code was moved into the `src/task_orchestrator/` package. It also delegates `__main__` execution to `runner.main()`.

### `src/task_orchestrator/__init__.py`
Package init that declares the `__version__` string. Contributors should bump this version when packaging a new release.

### `src/task_orchestrator/__main__.py`
One-liner that enables `python -m task_orchestrator` by delegating to `cli.main()`.

### `src/task_orchestrator/cli.py`
The CLI entry point: parses `argparse` subcommands (`run`, `init`, `validate`) and all their flags, then forwards to `runner.main(args)`. Adding a new subcommand means adding a subparser here and the corresponding logic in `runner.py` / `orchestrator.py`.

### `src/task_orchestrator/orchestrator.py`
The core engine. Contains the `Provider` class (wraps a CLI command, env, rate-limit patterns, cooldown, stall timeout), the main task loop with provider rotation and cooldown tracking, subprocess launch with `start_new_session=True`, stall detection via CPU + git-dirty monitoring, per-task verification gates, exponential-backoff rate-limit handling, git commit automation, structured JSON logging, and the interactive keyboard listener (`p`/`r`/`s`/`q`). New orchestration logic (e.g. a new scheduling strategy) belongs here.

### `src/task_orchestrator/runner.py`
A backward-compatible shim that re-exports everything from `orchestrator.py`, `git.py`, `dashboard.py`, and `notify.py` so that existing `from task_orchestrator.runner import X` imports keep working after the refactor into focused submodules. The `__getattr__` fallback also forwards private-name lookups to the submodules.

### `src/task_orchestrator/git.py`
All git operations and Todo.md manipulation: `git_run` (with transient-error retry on lock contention), `load_tasks` (regex parse with section skipping), `mark_complete` (literal `- [ ]` → `- [x]` replacement), `defer_task` (move failed tasks to end of file to prevent starvation), advisory file-level locking via `fcntl`/`msvcrt`, and task-count utilities. New git or Todo.md logic goes here.

### `src/task_orchestrator/dashboard.py`
The local HTTP dashboard: an in-memory state dict, an `HTTPServer` subclass serving JSON (`/api/state`, `/api/version`, `/health`) and HTML (root path), and helper functions to update task/provider status in real time. The HTML is rendered from a `string.Template` loaded at `dashboard/templates/index.html`. Dashboard UI changes are in `dashboard/templates/index.html`; state/API additions go in this module.

### `src/task_orchestrator/notify.py`
Notification helpers: desktop notifications via `osascript` (macOS) or `notify-send` (Linux), ANSI terminal color/style wrapping that is a no-op when stdout is not a TTY, audio cues (system beep / `say` / `winsound`), and the startup banner rendered in interactive terminals. New notification channels or visual styling go here.

### `dashboard/templates/index.html`
The HTML template rendered by `dashboard.py` to serve the live dashboard page. It uses `string.Template` substitution for `uptime`, `current_task`, `current_provider`, and `history_items`.

### `prompts/task_prompt.txt`
The prompt template fed to agent CLIs on each task. Contains a `{{TASK}}` placeholder that `orchestrator.py:build_prompt()` replaces with the actual task text. Edit this to change the default agent instruction for all providers.

### `scripts/sync_todo.py`
A standalone utility script for synchronizing Todo.md with external tooling (e.g. GitHub Issues). Not loaded by the orchestrator at runtime.

### `tests/`
The test suite. `test_orchestrator.py` covers unit-level logic (provider cooldowns, config validation, task parsing, stall detection); `test_integration.py` covers end-to-end orchestration scenarios. Tests that touch persisted state must patch `orchestrator.STATE_PATH` to a tempdir to avoid corrupting `state.json` on the developer's machine.