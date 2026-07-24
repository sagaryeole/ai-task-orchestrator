# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An AI-agnostic orchestrator (single file, `orchestrator.py`) that drives a coding-agent CLI (currently configured for Kilo Code; also works with Claude Code, Codex, or similar) through a `Todo.md` backlog, one task at a time, unattended. It rotates across configured providers when one gets rate-limited, restarts itself on crash, detects stalled/stuck tasks via activity monitoring (not just wall-clock timeout), and is built to run overnight without anyone watching it.

Pure Python 3 standard library only (`subprocess`, `json`, `re`, `time`, `threading`, `pathlib`, `datetime`, `argparse`) — no dependencies to install, no build step.

**This repo dogfoods itself**: a local `task-orchestrator.config.json` (gitignored — not checked in) targets this repo's own `Todo.md`, so `orchestrator.py` can be used to drive its own development. Be aware when editing `orchestrator.py` that a running instance may have this file open as its own target.

## Running it

```bash
python3 orchestrator.py                    # normal run, processes Todo.md top to bottom
python3 orchestrator.py --dry-run          # show the next task/provider without executing
python3 orchestrator.py --once             # run a single task and exit
python3 orchestrator.py --summary          # print today's run stats and exit
python3 orchestrator.py --json-logs        # also write logs/orchestrator.jsonl (structured)
python3 orchestrator.py --skip-section "Ideas / Backlog"   # exclude a Todo.md section; repeatable
./run_forever.sh                           # supervisor: restarts orchestrator.py on crash (exit 1),
                                            # does NOT restart on clean finish (0) or Ctrl+C (130)
```

### Tests

```bash
python3 -m unittest discover -s tests      # full suite (~53 tests, runs in well under 1s)
python3 -m unittest tests.test_orchestrator.TestProviderAvailability   # a single test class
python3 -m unittest tests.test_orchestrator.TestProviderAvailability.test_mark_exhausted  # a single test
```

This is also `verify_commands` in `task-orchestrator.config.json` — it runs after every task the agent completes, so a broken test blocks a task from being auto-accepted. Keep it fast; it's on the critical path of every single task.

Tests that touch persisted state must patch `orchestrator.STATE_PATH` to a tempdir (`unittest.mock.patch`) rather than let `save_state()` hit the real `state.json` — a test that doesn't do this will silently corrupt live provider-cooldown data on every run.

To exercise a change by hand without waiting on a real agent CLI: point one `providers[].command` at something fast and deterministic (`cat`, `echo`), set `require_manual_confirmation: false`, and run against a throwaway `Todo.md`. `state.json` and `logs/orchestrator.log` are auto-created on first run — delete `state.json` to reset provider cooldowns between test runs.

## Architecture

Everything lives in `orchestrator.py`; there's no package structure.

- **`Provider`** (class) — wraps one entry from the config's `providers[]` list: CLI `command`, an `env` overlay merged onto the current process env, lowercase `rate_limit_patterns` matched against combined stdout/stderr, `cooldown_seconds`, an optional per-task `subprocess_timeout` override, and a `stall_timeout` (default 600s). `Provider.run()` launches the subprocess with `start_new_session=True` (its own process group) and hands off to `_wait_for_result()`.
- **`_wait_for_result()`** runs `process.communicate()` on a background thread and polls in the foreground so the terminal isn't silent for the whole task. Each poll tick (every 0.2s): refreshes CPU%/git-dirty-count every `heartbeat_interval` (3s), writes a heartbeat log line every `log_heartbeat_interval` (30s) to `logs/orchestrator.log` (and `logs/orchestrator.jsonl` if `--json-logs`), and — **only when `sys.stdout.isatty()` is true** — redraws a live `\r`-updating status line (spinner, elapsed time, cpu%, files changed, idle time). That live spinner is intentionally suppressed when stdout isn't a real terminal (piped, redirected, `nohup`, backgrounded) so log files never get raw escape/carriage-return bytes in them — the 30s heartbeat log line is the thing to tail (`tail -f logs/orchestrator.log`) for unattended/overnight runs, not the spinner.
- **Two independent timeout mechanisms, not one**: `subprocess_timeout` (wall-clock, can be `null` for "no limit" — needed for genuinely big tasks) vs. `stall_timeout_seconds` (activity-based — fires only when there's been no CPU activity above `STALL_CPU_THRESHOLD` *and* no change in git-dirty-file-count for that long, regardless of wall-clock elapsed). A big task that's actively working never trips the stall timeout even with `subprocess_timeout: null`; a small task that hangs trips the stall timeout long before any wall-clock limit would. `subprocess_timeout_overrides` applies a longer wall-clock limit to tasks tagged `[big]`/`[slow]` etc. in their Todo.md text (`get_task_timeout()`, matched via `TAG_REGEX`).
- **On timeout or detected stall**, `os.killpg(os.getpgid(pid), SIGKILL)` kills the whole process group, not just `Popen.kill()` — some agent CLIs (Kilo included) spawn a detached grandchild worker that `Popen.kill()` alone leaves orphaned.
- **`state.json`** — the only thing persisted between runs: `provider_cooldowns` (provider name → unix timestamp when available again) plus `completed_task_durations` (rolling window, feeds the ETA in `print_progress()`). This is what makes rate-limit backoff and ETA estimation survive process restarts.
- **Provider selection is round-robin with skip-on-cooldown** (`pick_next_provider`), cursor carried across tasks via `provider_idx`. If every provider is on cooldown, the loop sleeps until the soonest cooldown expires (`seconds_until_next_available`) and re-checks — it never gives up outright.
- **Rate-limit detection is text-pattern matching**: each provider's `rate_limit_patterns` are lowercase substrings checked against the CLI's combined output. A rate-limited provider stops retrying immediately and rotates to the next provider for the *same* task, no delay.
- **Per-task attempt loop** (in `main()`): pick provider → run → rate-limited? mark cooldown + rotate → else `run_verification()` (all `verify_commands` must exit 0) → check for a **suspicious completion** (`exit_code == 0` but `git diff --stat` is empty — a false-success signal, not proof of real work) → either prompt for manual y/n/retry/skip-provider/skip-task (`require_manual_confirmation: true`) or auto-accept only when `exit_code == 0 and verified and not suspicious`. A suspicious result is never silently auto-accepted; it falls through to the same retry/defer path as a real failure. Non-rate-limit failures retry the same provider up to `max_retries_per_provider`; a task that exhausts every provider without succeeding is moved to the end of `Todo.md` via `defer_task()` (still unchecked) rather than staying at index 0 forever and blocking every task behind it.
- **`Todo.md` is both the input and the mutable state of the backlog**: tasks parsed via `TASK_REGEX` over `- [ ] ...` lines. `mark_complete()` does a literal `- [ ] {task}` → `- [x] {task}` replace — it never deletes a task line. The orchestrator always re-reads the file and re-parses task 0 at the top of the main loop, so external edits between tasks are picked up naturally. `load_tasks()`/`count_total_tasks()`/`count_completed_tasks()` accept `skip_sections` to exclude whole markdown sections (backed by `--skip-section`).
- **`prompts/task_prompt.txt`** is the stdin template per task, `{{TASK}}` substituted; explicitly tells the agent this is unattended (make a judgment call, don't wait for input) and not to edit `Todo.md` itself — the orchestrator owns that file. Falls back to a hardcoded default if the file is missing.
- `auto_commit: true` runs `git add -A && git commit -m "Task: {task}"` after a task is marked complete, but only if `git status --porcelain` shows real changes first — it will not produce empty commits.
- `stats_command` (optional, per-provider) — if the agent CLI exposes a usage/cost stats subcommand (e.g. `kilo stats --json`), `run_provider_stats()` runs it after each task and logs the output (parsed as JSON when possible) via `log_json("provider_stats", ...)`. No-op if unset.
- **Exit codes are a deliberate contract with `run_forever.sh`**: `0` = clean/intentional finish (all tasks done, or normal exit) → don't restart; `130` = SIGINT (`_sigint_handler` also kills any in-flight agent subprocess group before exiting) → don't restart; anything else (unhandled crash) → restart after a 10s pause. Never change these without updating the supervisor script to match.
- `lint_config()` / `lint_todo()` run at startup and warn (not fail) on common misconfigurations — e.g. a provider `command` that looks like it launches an interactive TUI (`_INTERACTIVE_LAUNCHERS`) without the flags needed for non-interactive/stdin-driven use.

## Configuration (`task-orchestrator.config.json`)

See README's "Configuration reference" for the full field list. Non-obvious ones:

- `max_retries_per_provider` only applies to non-rate-limit failures; a rate-limit hit always rotates immediately regardless of this value.
- `providers[].cooldown_seconds` is per-provider, not global.
- `stall_timeout_seconds` is what actually catches a genuinely stuck task in practice — tune `STALL_CPU_THRESHOLD` (currently `12.0`, calibrated against real observed idle-vs-active CPU% from a live run: ~0-4% idle, ~27-49% active) if a different agent CLI has a different idle/active signature.
- `subprocess_timeout: null` means unbounded — safe to use because `stall_timeout_seconds` is the real backstop, not wall-clock time.
- `require_manual_confirmation: false` + `auto_commit: true` is the "sleep without worry" unattended configuration; it's only safe with a real `verify_commands` gate (this repo uses its own test suite) — without verification, an agent's exit-code-0 is not evidence of correctness.
