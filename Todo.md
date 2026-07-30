# Todo

Tasks for developing the orchestrator itself (`orchestrator.py`, `config.json`, prompts, docs).
Grouped by priority — work top section first.

## Open-Source Architecture Improvements

Tasks to make the project easier to contribute to, easier to maintain, and friendlier to open-source contributors.

### CLI Module — argparse and entrypoint cleanup

- [ ] Move all `argparse` logic from `runner.py` (line ~2330) into `src/task_orchestrator/cli.py` so the entry point file actually owns the CLI surface. Today `cli.py` is a 21-line shim that only handles `--version`; the real parser is buried in a 2949-line god file.
- [ ] Replace the current `nargs="?"` positional-command trick (`init` / `validate` as optional positional args) with real `argparse` subparsers. This gives clean `--help` output per subcommand (`task-orchestrator init --help`, `task-orchestrator run --help`) instead of a single flat help screen.
- [ ] Add a `run` subcommand that owns all runtime flags (`--once`, `--dry-run`, `--concurrency`, `--provider`, `--task`, `--resume-from`, `--skip-section`, `--list-tasks`, `--summary`, `--json-logs`). This makes the CLI scannable: `task-orchestrator run --help` lists only run-time flags, not `init`/`validate` noise.
- [ ] Remove the manual `if "--version" in argv` check and use `parser.add_argument("--version", action="version", ...)` instead.
- [ ] Add `src/task_orchestrator/__main__.py` that calls `cli.main()`, so `python -m task_orchestrator` works (standard Python convention).
- [x] Keep `orchestrator.py` (repo root) as the backward-compatible shim for `python orchestrator.py`, but update `CONTRIBUTING.md` to point contributors to the package path.

### Test imports — migrate off the root shim

- [x] Change all test imports from `from orchestrator import main` to `from task_orchestrator.orchestrator import main` (or `from task_orchestrator.cli import main` once argparse moves). This removes the tests' dependency on the repo-root `sys.modules` self-replacement hack and makes them pass in a fresh `pip install` without the root shim present.
- [x] Update `Makefile` lint target: remove `orchestrator.py` from `ruff check` once it is confirmed to be trivial glue and tests no longer import through it.

### Dashboard Module — extract from `runner.py`

- [x] Create `src/task_orchestrator/dashboard.py` and move all dashboard-specific code into it: `_dashboard_state`, `_build_html`, `DashboardHandler`, `DashboardServer`, `start_dashboard`, `update_dashboard_state`, `refresh_dashboard_tasks_from_todo`, `mark_dashboard_tasks_running/skipped/finished`, `_build_run_summary`, `_load_all_todo_tasks`, `_find_next_task_card`, `_dashboard_task_id`, `_iso_now`, and the `_CHECKBOX_TASK_RE` regex. `runner.py` should call into this module, not define it.
- [ ] Serve static assets from `dashboard/static/` instead of inlining all CSS/JS in a Python string:
  - Split the current inline `<style>` block into `dashboard/static/styles.css`.
  - Split the current inline `<script>` block into `dashboard/static/app.js`.
  - Convert `_build_html` to a minimal template with only the 2–3 dynamic fields replaced (uptime, current task, current provider), using stdlib `string.Template` (no Jinja2 dep).
  - `DashboardHandler` should serve `/static/styles.css` and `/static/app.js` with `Cache-Control: no-cache` during development and a long max-age in production if desired.
- [x] Add a `dashboard/templates/index.html` file so the HTML is readable and editable by front-end contributors without opening `runner.py`.

### Dashboard robustness

- [ ] Add a `threading.Lock` around all mutations of `_dashboard_state`. The dashboard is mutated by the orchestrator main loop and read by `DashboardHandler` in a daemon thread; today this is "safe" only because the GIL serializes dict ops, which is an accident, not a guarantee.
- [x] Add `/api/version` endpoint that returns the current `__version__` from `task_orchestrator.__init__`, so the dashboard can display which version is running.
- [x] Make `open_dashboard_in_browser` state-aware: write a small sentinel file (e.g. `.dashboard_opened`) after the first auto-open so the browser tab is not re-opened on every `run_forever.sh` restart. Current behavior is annoying when the supervisor restarts the process after a transient crash.

### Package architecture — split `runner.py` into focused modules

`runner.py` is 2949 lines and owns config, git, state, logging, secrets, providers, subprocess lifecycle, stall detection, todo manipulation, dashboard, notifications, and the main loop. Split it into files under 400 lines each, mirroring the existing section comments:

- [ ] `src/task_orchestrator/config.py` — `load_config`, `validate_config`, `_interpolate_env_vars`, `_deep_merge`, `lint_config`, `GLOBAL_CONFIG_PATH`, `_INTERACTIVE_LAUNCHERS`.
- [x] `src/task_orchestrator/git.py` — `git_run`, `_git_dirty_count`, `validate_git_working_tree`, `_is_transient_git_error`, `_todo_lock`, and all Todo.md read/write/count helpers (`load_tasks`, `mark_complete`, `defer_task`, `count_total_tasks`, `count_completed_tasks`, `_get_section_for_line`, `_count_matching_lines`).
- [ ] `src/task_orchestrator/provider.py` — `Provider` class, `load_providers`, `pick_next_provider`, `seconds_until_next_available`, `print_provider_status`, `_resolve_executable`, `_resolve_shell_python`, `_PYTHON_ALIASES`.
- [x] `src/task_orchestrator/notify.py` — `notify`, `_play_audio_cue`, `_applescript_escape`, `_print_startup_banner`.
- [x] `src/task_orchestrator/orchestrator.py` — the main loop, `run_verification`, `build_prompt`, `build_retry_prompt`, `print_summary`, `print_progress`, `print_run_report_card`, `git_commit`, `run_provider_stats`, `_process_group_cpu_percent`, `_kill_process_tree`, `_sigint_handler`, `_sigterm_handler`, `_start_keyboard_listener`, `_interactive_options`, `_control_state`.
- [x] Keep `runner.py` as a compatibility shim that re-exports from the new modules, so `from orchestrator import main` still works for the runtime shim. Tests should import from the new modules directly.

### Open-repo and contributor experience

- [x] Update `README.md` line 90 ("single-file Python you can read end-to-end") to reflect the new module structure while preserving the stdlib-only promise. Suggested replacement: "Pure Python 3 standard library, split into focused modules you can read in under 5 minutes each."
- [x] Add `docs/architecture.md` with a one-paragraph purpose per module and a directory-tree map, so contributors landing from the README know where new code belongs without reading 2900-line files.
- [ ] Add a `## [Unreleased]` section at the top of `CHANGELOG.md` so contributors know where to put entry bullets.
- [ ] Add `__main__.py` coverage to `tests/` — a smoke test that `python -m task_orchestrator --help` exits 0 and prints the banner/help.
- [ ] Add `docs/contributing-architecture.md` (or expand `CONTRIBUTING.md`) with a concrete "where does X go?" table mapping feature areas to modules.

### Priority order

1. **Extract dashboard into `dashboard.py` + static files** — highest visibility, most painful to maintain inline, and the file is already ~400 lines of frontend mixed into orchestration logic.
2. **Move argparse into `cli.py` with real subparsers** — low risk, high clarity win for anyone reading the entry point.
3. **Migrate tests to package imports** — low risk, removes the `sys.modules` shim dependency in tests.
4. **Split `runner.py` into focused modules** — biggest long-term win but most churn; do it after the first three so conflicts are minimal.
