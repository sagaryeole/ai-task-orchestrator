# Where Does X Go? — Contributing Architecture

This page answers the question "where do I put my change?" for every major
feature area in the project. Use it as a lookup when adding new functionality.

## Quick Reference Table

| Feature Area | Module / File | What to Edit |
|---|---|---|
| CLI subcommands (`run`, `init`, `validate`) and flags (`--once`, `--dry-run`, `--provider`, etc.) | `src/task_orchestrator/cli.py` | Add a subparser in `main()` and delegate to `runner.main()` |
| Main task loop (picking tasks, driving provider rotation, overall orchestration flow) | `src/task_orchestrator/orchestrator.py` | Edit `main()` and `_run_single_task()` |
| Provider model (subprocess launch, rate-limit pattern matching, cooldown tracking) | `src/task_orchestrator/orchestrator.py` | Edit the `Provider` class |
| Stall detection (CPU + git-dirty monitoring) | `src/task_orchestrator/orchestrator.py` | Edit `_process_group_cpu_percent()` and stall-check logic in the task loop |
| Rate-limit detection + exponential backoff rotation | `src/task_orchestrator/orchestrator.py` | Edit `pick_next_provider()`, `seconds_until_next_available()`, and `Provider` cooldown logic |
| Verification gates (`verify_commands`) | `src/task_orchestrator/orchestrator.py` | Edit `run_verification()` |
| Git operations (commit, status, diff checks) | `src/task_orchestrator/git.py` | Edit `git_run()` or add new helpers here |
| Todo.md loading, marking complete, deferring tasks, file locking | `src/task_orchestrator/git.py` | Edit `load_tasks()`, `mark_complete()`, `defer_task()`, or add new helpers |
| Task timeout overrides (`[big]` / `[slow]` tags) | `src/task_orchestrator/orchestrator.py` | Edit `get_task_timeout()` and `TAG_REGEX` |
| Config loading, validation, and state persistence (`state.json`) | `src/task_orchestrator/orchestrator.py` | Edit `load_config()`, `validate_config()`, `load_state()`, `save_state()` |
| Structured JSON logging + secret masking | `src/task_orchestrator/orchestrator.py` | Edit `log_json()`, `_mask_secrets()`, `_register_secrets()` |
| Prompt template building (`{{TASK}}` substitution) | `src/task_orchestrator/orchestrator.py` | Edit `build_prompt()` |
| Retry prompt construction (feeding verification failures back) | `src/task_orchestrator/orchestrator.py` | Edit `build_retry_prompt()` |
| Interactive keyboard listener (`p`/`r`/`s`/`q`) | `src/task_orchestrator/orchestrator.py` | Edit `_start_keyboard_listener()` |
| Auto-commit after task completion | `src/task_orchestrator/orchestrator.py` | Edit `git_commit()` |
| Provider stats collection (`stats_command`) | `src/task_orchestrator/orchestrator.py` | Edit `run_provider_stats()` |
| Progress display (spinner, elapsed time, CPU%, file changes) and summary reports | `src/task_orchestrator/orchestrator.py` | Edit `print_progress()`, `print_summary()`, `print_run_report_card()` |
| Backward-compatible shim (`python orchestrator.py` and `from orchestrator import X`) | `orchestrator.py` (repo root) | **Do not add features here.** Edit only when preserving shim behavior. |
| Backward shim re-exporting from submodules (`from task_orchestrator.runner import X`) | `src/task_orchestrator/runner.py` | Update `__getattr__` forwarding if you add new public names |
| Package version (`__version__`) | `src/task_orchestrator/__init__.py` | Bump when releasing |
| `python -m task_orchestrator` entry point | `src/task_orchestrator/__main__.py` | Update if you change the delegation target |
| Live dashboard (HTTP server, JSON API endpoints, state management) | `src/task_orchestrator/dashboard.py` | Add endpoints to `DashboardHandler` and state-update helpers |
| Dashboard HTML template | `dashboard/templates/index.html` | Edit for UI/layout changes |
| Desktop notifications (macOS `osascript`, Linux `notify-send`) | `src/task_orchestrator/notify.py` | Add new notification channels here |
| ANSI terminal styling and startup banner | `src/task_orchestrator/notify.py` | Edit `style()` or `_print_startup_banner()` |
| Audio cues (system beep, `say`, `winsound`) | `src/task_orchestrator/notify.py` | Edit `_play_audio_cue()` |
| Prompt template file (default agent instructions) | `prompts/task_prompt.txt` | Edit the `{{TASK}}` placeholder template |
| Unit tests | `tests/test_orchestrator.py` | Add test classes matching the feature area |
| Integration tests | `tests/test_integration.py` | Add end-to-end scenarios here |
| Example provider configs | `examples/` | Add a JSON file with `command`, `rate_limit_patterns`, `env`, etc. |
| Standalone Todo sync utility | `scripts/sync_todo.py` | Edit for external tooling integration |
| Project documentation | `docs/` | Edit the relevant `.md` file |

## Module Dependency Flow

```
cli.py (argparse)
  └─► orchestrator.py:main()
        ├─► orchestrator.py:Provider  (subprocess launch, rate-limit detection)
        ├─► orchestrator.py:run_verification()
        ├─► orchestrator.py:git_commit()
        ├─► orchestrator.py:print_progress() / print_summary()
        ├─► orchestrator.py:run_provider_stats()
        ├─► git.py  (load_tasks, mark_complete, defer_task, git_run, file locking)
        ├─► dashboard.py  (start_dashboard, update_dashboard_state, HTTP API)
        └─► notify.py  (desktop notifications, ANSI styling, audio, banner)
```

## Adding a New Feature — Checklist

1. **Identify the right module** using the table above.
2. **Add or update tests** in `tests/test_orchestrator.py` (unit) or `tests/test_integration.py` (e2e).
3. **Run the test suite**: `python -m unittest discover -s tests`
4. **Lint**: `ruff check src/ orchestrator.py tests/`
5. **Update this document** if a new module or feature area is introduced.
6. **Update `docs/architecture.md`** if the module structure changes (new file, rename, etc.).

## Backward-Compatibility Shim

The root-level `orchestrator.py` exists solely so that `python orchestrator.py` and
`from orchestrator import X` continue to work after the code was moved into
`src/task_orchestrator/`. It inserts `src/` onto `sys.path`, imports
`task_orchestrator.runner`, and replaces itself in `sys.modules`. **Do not add
features here** — all logic belongs in the package below. The shim should only be
updated when its import path or self-replacement mechanism needs to change.