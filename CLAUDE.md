# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A small, AI-agnostic orchestrator (single file, `orchestrator.py`) that drives a coding-agent CLI (Kilo Code, Claude Code, Codex, or similar) through a backlog of tasks one at a time. It rotates across multiple configured providers/models when one gets rate-limited, so a long backlog can run unattended instead of stalling on a single free-tier provider.

Pure Python 3 standard library only (`subprocess`, `json`, `re`, `time`, `pathlib`, `datetime`) — no dependencies to install, no build step.

## Running it

```
python orchestrator.py
```

There is no test suite, linter, or build process in this repo currently.

To exercise a change by hand: put a fake/short-lived command in one `providers[].command` entry in `config.json` (e.g. `cat` or `echo`), set `require_manual_confirmation: false`, and run against a throwaway `Todo.md`. `state.json` and `logs/orchestrator.log` are auto-created on first run — delete `state.json` to reset provider cooldowns between test runs.

## Architecture

Everything lives in `orchestrator.py`; there's no package structure. The core objects:

- **`Provider`** (class) — wraps one entry from `config.json`'s `providers[]` list: a CLI `command` string, an `env` overlay merged onto the current process env, a list of lowercase `rate_limit_patterns` to match against combined stdout/stderr, and a `cooldown_seconds`. `Provider.run()` shells out synchronously (`subprocess.run`, prompt piped via `stdin`, `capture_output=True`) and returns `(exit_code, output, looked_rate_limited)`.
- **`state.json`** — the only thing persisted between runs: a `provider_cooldowns` map of provider name → unix timestamp when it becomes available again. This is what makes rate-limit backoff survive process restarts.
- **Provider selection is round-robin with skip-on-cooldown** (`pick_next_provider`), cursor carried across tasks via `provider_idx` so load balances across successful runs rather than always starting from provider 0. If every provider is on cooldown, the loop sleeps until the soonest cooldown expires (`seconds_until_next_available`) and re-checks — it never gives up outright.
- **Rate-limit detection is text-pattern matching**, not structured API errors: each provider's `rate_limit_patterns` are lowercase substrings checked against the CLI's combined output. When a provider looks rate-limited, retries for that provider stop immediately and the state machine rotates to the next provider for the *same* task — no delay, since the point is to keep making progress across providers rather than waiting out any one of them.
- **Per-task attempt loop** (in `main()`): pick provider → run → rate-limited? mark cooldown + rotate, else → `run_verification()` (optional shell commands from `verify_commands`, all must exit 0) → either prompt for manual y/n/retry/skip-provider (`require_manual_confirmation: true`) or auto-accept on `exit_code == 0 and verified`. Non-rate-limit failures retry the *same* provider up to `max_retries_per_provider` before the task is abandoned (or the whole run stops, if `continue_on_failure: false`).
- **`Todo.md` is both the input and the mutable state of the backlog**: tasks are parsed with a regex over `- [ ] ...` lines (`TASK_REGEX`), and `mark_complete()` does a literal string replace of `- [ ] {task}` → `- [x] {task}`. The orchestrator always re-reads the file and re-parses task 0 on the top of the main loop, so external edits to `Todo.md` between tasks are picked up naturally.
- **`prompts/task_prompt.txt`** is the template sent to the agent CLI on stdin per task, with `{{TASK}}` substituted; if the file is missing, `build_prompt()` falls back to a hardcoded default prompt.
- `auto_commit: true` in `config.json` runs `git add -A && git commit -m "Task: {task}"` in `working_directory` after each task is marked complete — there's no check that anything actually changed, so this can produce empty commits.

## Configuration (`config.json`)

The provider list ships with four placeholder examples (`openrouter-free`, `anthropic-claude`, `nvidia-nim`, `lmstudio-local`) with `REPLACE_ME` API keys and placeholder `command`/`rate_limit_patterns` — these need real values before running against an actual provider, and the exact rate-limit wording varies per CLI tool and must be tuned by hand. See the README's "Configuration reference" table for the full field list; the non-obvious ones:

- `max_retries_per_provider` only applies to non-rate-limit failures (wrong-answer/error exits); a rate-limit hit always rotates immediately regardless of this value.
- `providers[].cooldown_seconds` is per-provider, not global — a provider likely to recover quickly (e.g. a local LM Studio server) should have a short cooldown even if remote paid providers have long ones.
- Provider order in the list is the tie-break/preference order for round-robin; there's no separate priority field.
