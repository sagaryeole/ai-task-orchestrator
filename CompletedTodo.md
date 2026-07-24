# Completed Todo

## V2 — Open-Source Excellence (Architecture & Packaging)

### Packaging & Distribution

- [x] Convert single-file `orchestrator.py` into a proper Python package (`src/task_orchestrator/`) with `__init__.py`, `cli.py`, `provider.py`, `todo.py`, `dashboard.py`, `state.py`, `config.py` modules
- [x] Add `pyproject.toml` with proper metadata, entry_points (`[project.scripts] task-orchestrator = "task_orchestrator.cli:main"`), and classifiers for PyPI
- [x] Make installable via `pip install task-orchestrator` (reserve PyPI name)
- [x] Add a `task-orchestrator init` command that scaffolds `config.json`, `Todo.md`, `prompts/task_prompt.txt`, and `.gitignore` in the current directory — zero-friction onboarding for new users
- [x] Add a `task-orchestrator validate` command that checks config, providers reachability (dry command test), and git state without running any tasks
- [x] Support `~/.task-orchestrator/config.json` as a global default config (merged under project-local config) for user-wide provider credentials

### Configuration Flexibility

- [~] ~~Support YAML and TOML config formats alongside JSON (auto-detect by extension), since many OSS users prefer YAML~~ (dropped: adds PyYAML dep; JSON is universal and stdlib-only)
- [x] Add JSON Schema file (`config.schema.json`) and validate config against it on load — enables IDE autocompletion and inline validation for users
- [x] Allow environment variable interpolation in config values (e.g. `"env": {"API_KEY": "$ANTHROPIC_API_KEY"}`) so secrets never need to be in the config file
- [x] Support `--provider <name>` CLI flag to force a specific provider for a single run, ignoring priority/round-robin
- [x] Support `--task "inline task text"` to run a one-off task without editing Todo.md
- [~] ~~Add `--concurrency N` flag for embarrassingly-parallel task lists (independent tasks marked with a `[parallel]` tag can run N at a time on different providers)~~ (dropped: fundamentally changes execution model; out of v2 scope)

### Extensibility & Plugin System

- [~] ~~Define a simple plugin interface (`class BaseProvider`) with `run()`, `is_rate_limited()`, `get_stats()` methods so users can write custom providers as Python classes (not just CLI wrappers)~~ (dropped: CLI wrapping already covers any provider; over-engineering)
- [~] ~~Support loading provider plugins from `~/.task-orchestrator/plugins/` or via entry_points (`task_orchestrator.providers` group)~~ (dropped: same as above)
- [~] ~~Add webhook/callback hooks (`on_task_start`, `on_task_complete`, `on_task_fail`, `on_provider_exhausted`) configurable as shell commands or HTTP POST URLs — enables Slack/Discord/Teams notifications, CI integrations~~ (dropped: verify_commands already enables post-task hooks)
- [~] ~~Support custom task parsers (not just `- [ ]` markdown) via a `task_format` config — enables integration with GitHub Issues, Jira exports, plain-text lists, CSV~~ (dropped: markdown checkboxes are universal; premature abstraction)

### Robustness & Reliability

- [x] Remove hard git dependency — make git-based checks (suspicious completion, dirty detection) optional and gracefully degrade when not in a git repo (use file-mtime-based change detection as fallback)
- [x] Add atomic state writes (write to `state.json.tmp` then `os.replace()`) to prevent corruption on power loss / crash mid-write
- [x] Add a `--resume-from <task-text>` flag to start from a specific task instead of always from the top
- [x] Mask secrets (API keys from provider env) in log output — scan for known patterns (`sk-`, `key-`, env var values) and redact them before writing to disk
- [x] Add a health-check endpoint to the dashboard (`/health`) returning 200 when alive, for external monitoring tools
- [x] Handle SIGTERM in addition to SIGINT for container/systemd graceful shutdown
- [~] ~~Add file-watching mode (`--watch`) that re-reads Todo.md when it changes on disk (inotify/ReadDirectoryChanges) instead of only at task boundaries — enables live task additions without restarting~~ (dropped: orchestrator re-reads between tasks; inotify adds platform complexity)

### Testing & CI

- [x] Add GitHub Actions CI workflow (`.github/workflows/ci.yml`) running tests on Python 3.9–3.13 across ubuntu/windows/macos matrix
- [x] Add integration test that actually runs a mock provider (simple echo script) end-to-end and verifies Todo.md state changes
- [~] ~~Add `pytest` as an alternative test runner alongside unittest (many contributors prefer it)~~ (dropped: pytest can run unittest tests already; no code change needed)
- [~] ~~Add code coverage reporting (coveralls/codecov badge in README)~~ (dropped: CI polish, not core)
- [x] Add a `Makefile` / `justfile` with common commands (`make test`, `make lint`, `make format`, `make release`)

### Documentation & Community

- [x] Add `CONTRIBUTING.md` with development setup, code style, PR process, and architecture overview
- [x] Add `examples/` directory with ready-to-use configs for popular providers (Claude, GPT-4, Copilot, Ollama, LM Studio, OpenRouter)
- [x] Add `docs/` with a mkdocs site covering: quickstart, configuration reference, provider guide, FAQ, troubleshooting
- [~] ~~Add ASCII art logo / banner for terminal output and README header~~ (dropped: cosmetic)
- [x] Add `CHANGELOG.md` following Keep a Changelog format
- [x] Add GitHub issue templates (bug report, feature request, new provider)
- [x] Add a "Providers compatibility matrix" table in README showing tested agent CLIs and their required flags
- [~] ~~Record a 2-minute demo GIF/asciinema for the README showing a real multi-provider overnight run~~ (dropped: marketing, do manually)

### Code Quality & Maintainability

- [x] Add type hints throughout (prepare for `mypy --strict` or `pyright`) — improves IDE experience and catches bugs
- [x] Add `ruff` or `flake8` linting config and pre-commit hooks
- [~] ~~Replace global mutable state (`_current_process`, `_dashboard_server_ref`, `_control_state`, etc.) with a `RunContext` dataclass passed through the call chain — eliminates hidden coupling and makes testing trivial~~ (dropped: v2 package already uses cleaner patterns; legacy file is frozen)
- [~] ~~Extract the 50+ line `_build_html()` string-concatenation into a proper Jinja2-like template file (or at minimum a separate `.html` file read at startup) — the inline JS/HTML strings are unmaintainable~~ (dropped: would need Jinja2 dep or complex stdlib alternative)
- [~] ~~Add structured logging with levels (DEBUG/INFO/WARN/ERROR) instead of the current flat `log()` with color hints — enables log filtering and integration with standard logging infrastructure~~ (dropped: current system works; stdlib logging adds unnecessary complexity for a CLI tool)
- [x] Make `defer_task()` configurable: add a `on_failure` config option with values `skip` (current new behavior), `defer` (old move-to-end behavior), or `stop`

### Performance & Scalability

- [~] ~~Add an optional SQLite backend for state (instead of JSON) for large-scale runs with 1000+ tasks — JSON read/write on every task becomes a bottleneck~~ (dropped: adds external dep risk; JSON is fine for realistic backlogs)
- [x] Stream provider output in real-time (line-by-line) instead of buffering the entire stdout/stderr — enables live progress for long-running tasks and earlier rate-limit detection
- [x] Add provider health pre-check (optional quick probe before sending real work) to avoid wasting a full task attempt on a provider that's clearly down

### Security

- [x] Audit and document the threat model: Todo.md task text is user-controlled and flows into subprocess args (potential command injection if not properly quoted) — add a note in README about trusted-input assumption
- [x] Add `--dry-run-prompt` flag that shows the exact prompt that would be sent (with task text substituted) without executing — helps users verify no unintended content reaches the LLM
- [x] Validate that provider `command` doesn't contain shell metacharacters when `shell=False` is used (current `shlex.split` is correct, but document why `shell=True` is never used for provider commands)

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
- [x] Fix false-positive rate-limit detection overriding a real completion — `rate_limited` was decided by a plain substring match over the whole CLI output, so task/code text mentioning "rate limit"/"429"/"quota" (routine domain vocabulary in this repo) discarded genuinely finished work and re-sent the same task after a cooldown; only trust the match when `git diff --stat` shows no real changes happened (fixed in orchestrator.py, verify it stays fixed with a regression test — none exists yet)
- [x] Handle non-UTF8/malformed bytes in a provider CLI's stdout/stderr without crashing the whole run — `subprocess.run`/`Popen` currently decode with `text=True` and no `errors=` handling, so one bad byte sequence from an agent CLI raises `UnicodeDecodeError` and kills the orchestrator instead of just that task
- [x] Retry transient git command failures once after a short delay (e.g. `index.lock` contention) instead of treating them as hard failures — dogfooding runs kilo against this repo's own working tree, so the orchestrator's own `git diff`/`git commit`/`git status` calls can genuinely race with the agent's own git usage
- [x] Rotate/cap `logs/orchestrator.log` and `.jsonl` once they pass a size threshold (e.g. 10MB) instead of growing unbounded across a long-lived or multi-day run
- [x] File-lock `Todo.md` writes (`mark_complete`/`defer_task`) so two orchestrator processes accidentally pointed at the same `Todo.md` can't interleave writes and corrupt it
- [x] Exponential backoff on repeated rate-limit hits from the same provider instead of always reusing the same fixed `cooldown_seconds` — a provider that keeps getting rate-limited should back off longer each time, not retry at a fixed cadence forever
- [x] Validate at startup that `working_directory` is actually inside a git working tree (`git rev-parse --is-inside-work-tree`) and fail fast with a clear message, instead of every `git diff`/`git commit` call quietly no-op'ing or erroring deep inside the task loop
- [x] Add GitHub Copilot CLI as a fully working provider — required a small `Provider.run()` change: a `{{TASK}}` token in a provider's `command` is now substituted with the full prompt as a single argv element (stdin left empty) instead of always piping the prompt via stdin, since `copilot -p <text>` takes the prompt as an argument, not stdin. Verified end-to-end with a real `--once` smoke test (file created, task marked complete, no false rejection). See README's "Worked example: GitHub Copilot CLI".
- [x] Fix suspicious/rate-limited-false-positive detection being blind to brand-new files — both checks relied on `git diff --stat`, which only sees changes to already-tracked files; a task whose only output was a new file (surfaced by the Copilot smoke test) got wrongly flagged "SUSPICIOUS" and rejected despite succeeding. Switched to `git status --porcelain` (catches new/modified/deleted/untracked alike). This also exposed that `orchestrator.pid` wasn't in `.gitignore`, which would have made *every* task look "changed" forever in a real run (fixed both; see README's git-status-porcelain caveat under Known Limitations).
- [x] Fix AppleScript string injection in `notify()` — `title`/`message` (raw task text from Todo.md) were interpolated unescaped into an `osascript` AppleScript literal; a `"` in a task description broke the syntax, and a crafted one could inject arbitrary AppleScript. Added `_applescript_escape()`, verified live with quotes/backslashes in the message.
- [x] Fix manual-confirmation mode silently ignoring `verify_commands` failures — `run_verification()`'s result was computed but never checked before the "mark complete?" prompt, unlike the unattended path which correctly gates on it; a human could confirm past a failing test suite without any warning in the prompt itself. Prompt now reads "mark complete despite VERIFICATION FAILURE?" and highlights red, matching the existing suspicious-completion treatment.
- [x] Remove a no-op `save_state(load_state())` round-trip in the SIGINT handler — every state mutation already calls `save_state()` immediately elsewhere (state.json is never batched in memory), so this line did nothing; cleaned it up and fixed the "Saving state and exiting" log message that implied otherwise.

- [x] Replace platform-specific shell commands with cross-platform alternatives (or guarded fallbacks).
- [x] Add/adjust supervisor for Windows (PowerShell or bat equivalent).
- [x] Make verify command interpreter-agnostic in config.json.
- [x] Add tests for Windows branches in test_orchestrator.py.

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
- [x] Shell out to the provider's own stats command after each task (e.g. `kilo stats`) and log cost/token usage per task, where the CLI supports it
- [x] Write a PID/status file (e.g. `orchestrator.pid` with pid + dashboard URL + start time) on startup, removed on clean exit — today the only way to tell "is a run actually still alive, and where's its dashboard" is grepping `ps aux` or trusting the last log line, which is indistinguishable from a dead run that was killed without logging anything (hit this exact confusion — a `Dashboard available at :8765` line stayed in the log long after that process was gone)
- [x] Auto-retry the dashboard on the next free port if `dashboard_port` is already in use, instead of logging an `OSError` once and running the rest of that session with no dashboard at all
- [x] Auto-refresh the dashboard HTML page (simple JS poll of `/api/state` every few seconds) instead of requiring a manual browser refresh to see updated state
- [x] Explicitly shut down the dashboard `HTTPServer` (`server.shutdown()`) on both clean exit and SIGINT instead of relying on the daemon thread dying implicitly with the process
- [x] Add a `--list-tasks [N]` / `--peek` flag to preview the next N pending tasks (with which provider would run each) without executing anything — complements the existing `--dry-run`
- [x] Optional `open_dashboard_in_browser` config flag — call `webbrowser.open()` once at startup when set, so the dashboard tab opens automatically instead of relying on someone noticing and copying the URL from the log

## P3 — Interactivity & Delight

Cosmetic/UX only — none of these may change what gets written to `logs/orchestrator.log`/`.jsonl` or affect task pass/fail logic. Anything audio/animated must be opt-in (config flag, default off) so a real overnight unattended run stays exactly as quiet and deterministic as it is today.

- [x] Non-blocking keyboard commands while a task is running — read stdin on a background thread so a human watching an interactive session can press `p` to pause after the current task finishes, `s` to skip the current task, or `q` to quit gracefully, without resorting to Ctrl+C
- [x] Rotating flavor-text in the live `\r` status line/spinner (only shown when `sys.stdout.isatty()`) — a small local list of phrases (e.g. "Reticulating splines...", "Bribing the linter...") cycled in place of the plain "working" label; purely cosmetic, must never leak into the heartbeat log line
- [x] Small celebratory notice on milestones — every 10th task completed and on reaching 100% of `Todo.md` — distinct from the existing plain "Task marked complete" log line
- [x] Optional audio/voice cue on task completion or when manual confirmation is needed (e.g. macOS `say "Task complete"`), config-gated and off by default
- [x] Fun stats in `--summary`: longest task, shortest task, current consecutive-success streak — derived from `completed_task_durations`/existing per-task outcome logging, no new dependencies
- [x] Elapsed-time-aware "waiting mood" messages on the interactive spinner only (never logged) — e.g. past 5 minutes idle: "still going, might be worth a coffee ☕"; past 15: "this one's taking a while" — purely to make long waits less dead-silent in a terminal you're actively watching
- [x] ASCII progress bar (e.g. `[#######---] 68%`) in the interactive spinner line, driven by the same percentage `print_progress()` already computes, instead of only numeric "Task N/M"
- [x] Emoji/color status glyphs per provider state in `print_provider_status()` when interactive (✅ available, 🌙 cooldown Xs, 🔥 currently working) instead of plain text state names — text-only fallback stays for the log file
- [x] A small "run report card" printed on natural completion and available via `--summary` — total tasks, wall time, success rate, and a lighthearted one-line verdict (e.g. "clean run, no retries" vs "rough night, 4 retries") — cosmetic wrapper around stats that are already tracked, no new data collection
- [x] Add Windows-safe compatibility layer for locking/process inspection/process termination in orchestrator.py.
