# Todo

Tasks for developing the orchestrator itself (`orchestrator.py`, `config.json`, prompts, docs).
Grouped by priority — work top section first.

## P0 — Critical / Correctness

- [x] Add a .gitignore for state.json, logs/, and any local .env — config.json currently has REPLACE_ME placeholders but real keys would otherwise be committable
- [x] Validate config.json on load (required fields, providers non-empty, types) and fail with a clear error instead of a raw KeyError/AttributeError
- [x] Fix provider.run()'s naive `command.split()` — breaks for commands with quoted arguments or spaces in a path; use shlex.split() instead
- [x] Add a subprocess timeout (or at least a documented rationale for timeout=None) so a hung agent CLI doesn't block the orchestrator forever
- [x] Handle Ctrl+C / SIGINT gracefully mid-task — save state and exit cleanly instead of leaving state.json or a running subprocess in a bad spot

## P1 — Important / Robustness

- [x] Write a test suite (pytest or unittest) covering: load_tasks/mark_complete regex+replace logic, pick_next_provider round-robin + cooldown skipping, seconds_until_next_available, and Provider.is_available/mark_exhausted
- [x] Add a --dry-run CLI flag that prints which provider/task would run next without executing anything
- [x] Add a --config <path> CLI flag instead of hardcoding CONFIG_PATH = Path("config.json")
- [x] Guard auto_commit against empty commits — check `git status --porcelain` before running git add/commit
- [x] Add a "skip-task" manual-confirmation option (currently only y/n/retry/skip-provider exist) so a human can defer a task without marking it complete or failing the run
- [x] Surface verify_commands failure output live (currently only written to the log file, not printed to terminal on failure)
- [x] Auto diff-preview before the "mark complete?" confirmation — run `git diff --stat` (working_directory) and print it inline right before the prompt, so a human doesn't have to manually check whether anything actually changed
- [x] Auto-flag "exit 0 but zero files changed" as suspicious instead of asking a normal "mark complete?" — a real incident: kilo reported success on a task and had done zero edits (confirmed via git status + no recent file mtimes); the confirm prompt gave no hint anything was off
- [x] Startup config linter: warn (don't just silently proceed) if `prompt_template` doesn't contain `{{TASK}}`, if a provider `command` looks like a bare/interactive launcher rather than a documented headless flag, or if `env` values still contain `REPLACE_ME`
- [x] Todo.md linter: detect duplicate section headers / duplicate task lines on startup and warn — found a byte-identical duplicated section in a real Todo.md that would have caused the same task to be attempted twice
- [x] Wrap main()'s loop in a top-level try/except so an unhandled exception saves state and logs the full traceback before exiting, instead of dying in a worse spot than a clean SIGINT does today — exits 1 on crash, 130 on SIGINT, 0 on normal/intentional stop, so a supervisor script can tell them apart
- [x] Activity-based stall detection (`stall_timeout_seconds`, default 600s) — separate from the wall-clock `subprocess_timeout`, kills and retries a task only if there's been genuinely zero CPU activity *and* zero file changes for that long, so a big-but-working task is never killed while a truly hung one still gets caught
- [x] `run_forever.sh` supervisor script — auto-restarts orchestrator.py on an unexpected crash (exit 1), but never on a clean finish (exit 0) or a manual Ctrl+C (exit 130); progress always resumes from Todo.md/state.json on disk, no separate resume logic needed
- [x] Support a per-task timeout override (e.g. a `[big]`/`[slow]` tag in the task text) instead of only a single global `subprocess_timeout` for every task regardless of size

## P2 — Nice-to-have / Polish

- [x] Support prioritized (non-round-robin) provider ordering, e.g. a `priority` field or "always prefer local before paid" mode
- [x] Add a --once flag to run a single task and exit, for easier manual testing/debugging
- [x] Emit structured (JSON) log lines alongside the current human-readable log, for easier downstream parsing/dashboards
- [x] Document, in README or a CONTRIBUTING note, how to add a new provider type (what fields are required, how rate_limit_patterns should be chosen)
- [x] Add a minimal live status view (e.g. print a one-line summary of provider cooldown states on startup and after each rotation)
- [x] Persistent one-line status footer (Claude Code style) showing provider, task N/total, elapsed, cpu%, files changed — kept separate from the scrolling task log rather than mixed into it
- [x] Progress + ETA: "Task 14/202 (7%), ~3h remaining" based on a rolling average of completed task durations
- [x] Desktop notification (e.g. `osascript -e 'display notification'` on macOS, no new deps) when a task needs manual confirmation or the run stalls/finishes, so long runs don't require babysitting the terminal
- [x] Add a `--summary` flag (rtk-gain style): tasks completed today, success/fail rate, total run time, average time per task
- [x] Add a `--skip-section "Section Name"` flag to exclude non-actionable sections (e.g. a manual QA/"Go-Live Verification Checklist") from being picked up as tasks at all

- [x] Minimal local dashboard via stdlib `http.server` serving current run state (current task, provider status, recent history) as JSON/HTML — replaces the "no live dashboard" limitation without adding a dependency
- [ ] Shell out to the provider's own stats command after each task (e.g. `kilo stats`) and log cost/token usage per task, where the CLI supports it
