#!/usr/bin/env python3
"""
Task Orchestrator (multi-provider)
-----------------------------------
Runs a coding-agent CLI (Kilo Code, Claude Code, Codex, etc.) one task at a time,
reading tasks from Todo.md, and rotates across a configurable pool of
providers/models (Anthropic, OpenRouter, Nvidia NIM, local LM Studio, ...).

When a provider is rate-limited / exhausted, the orchestrator marks it
"cooling down" and immediately retries the SAME task on the next provider
in the list -- no wasted wait. Only if every provider is exhausted does it
sleep and then start again from the top of the list. A normal per-task
delay is still applied between successful tasks to avoid re-triggering
limits.

Usage:
    python orchestrator.py

Edit config.json to configure your providers, Todo file, and delay.
"""

import subprocess
import threading
import time
import json
import re
import sys
import os
import shlex
import signal
import argparse
import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

CONFIG_PATH = Path("config.json")
STATE_PATH = Path("state.json")
LOG_DIR = Path("logs")
TASK_REGEX = r"- \[ \] (.+)"
TAG_REGEX = r"(\[\w+\])"
STALL_CPU_THRESHOLD = 12.0  # %cpu below this counts as "idle" for stall detection.
# Calibrated from real observed data: a genuinely stalled process read 0-4%
# CPU (event-loop/GC noise, not real work) and kept resetting the stall timer
# under the old 2.0 threshold, letting a task sit stuck for 7+ hours because
# it never accumulated enough idle time. A genuinely active process read
# 27-49%. 12.0 sits in the real gap between those two clusters.
_json_log_enabled = False
_current_process = None  # in-flight agent subprocess, so SIGINT can clean it up too


def log_json(event, **kwargs):
    if not _json_log_enabled:
        return
    record = {
        "ts": datetime.datetime.now().isoformat(),
        "event": event,
        **kwargs,
    }
    LOG_DIR.mkdir(exist_ok=True)
    with open(LOG_DIR / "orchestrator.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")


# --------------------------------------------------------------------------
# Config / state / logging
# --------------------------------------------------------------------------

def validate_config(config):
    errors = []

    if not isinstance(config, dict):
        sys.exit("Config validation failed:\n  - config.json must be a JSON object (dict).")

    required_top = ["todo_file", "providers"]
    for key in required_top:
        if key not in config:
            errors.append(f"Missing required config key: '{key}'")

    providers = config.get("providers")
    if providers is not None:
        if not isinstance(providers, list):
            errors.append("'providers' must be a list.")
        elif len(providers) == 0:
            errors.append("'providers' list must not be empty.")
        else:
            for i, p in enumerate(providers):
                if not isinstance(p, dict):
                    errors.append(f"Provider at index {i} is not a dict.")
                    continue
                if "name" not in p or not p["name"]:
                    errors.append(f"Provider at index {i} missing or empty 'name'.")
                if "command" not in p or not p["command"]:
                    errors.append(f"Provider at index {i} missing or empty 'command'.")

    if errors:
        sys.exit("Config validation failed:\n" + "\n".join(f"  - {e}" for e in errors))


_INTERACTIVE_LAUNCHERS = {
    "claude": {
        "headless_flags": ["--no-interactive", "--print", "-p"],
        "message": "Claude Code is interactive by default; use --no-interactive or --print for unattended runs.",
    },
    "codex": {
        "headless_flags": ["--quiet", "--no-interactive"],
        "message": "Codex CLI may be interactive; use --quiet for unattended runs.",
    },
    "kilo": {
        "headless_flags": ["--auto"],
        "message": "Kilo Code is interactive by default; use --auto for unattended runs.",
    },
    "cursor": {
        "headless_flags": [],
        "message": "Cursor is an editor, not a headless agent CLI.",
    },
}


def lint_todo(todo_path: Path):
    """Warn about duplicate section headers and duplicate task lines in Todo.md."""
    if not todo_path.exists():
        return
    text = todo_path.read_text()
    lines = text.splitlines()

    headers = []
    tasks = []
    for line in lines:
        stripped = line.strip()
        if re.match(r"^#{1,6}\s+.+", stripped):
            headers.append(stripped)
        elif re.match(r"^- \[ \] .+", stripped):
            tasks.append(stripped)

    seen = {}
    for header in headers:
        seen[header] = seen.get(header, 0) + 1
    for line, count in seen.items():
        if count > 1:
            log(f"Todo.md linter: duplicate section header ({count}x): {line}", color="yellow")

    seen = {}
    for task in tasks:
        seen[task] = seen.get(task, 0) + 1
    for line, count in seen.items():
        if count > 1:
            log(f"Todo.md linter: duplicate task line ({count}x): {line}", color="yellow")


def lint_config(config):
    """Warn about common config pitfalls that would silently break unattended runs."""
    warnings = []

    prompt_template_path = Path(config.get("prompt_template", "prompts/task_prompt.txt"))
    if prompt_template_path.exists():
        content = prompt_template_path.read_text()
        if "{{TASK}}" not in content:
            warnings.append(
                f"prompt_template ({prompt_template_path}) exists but does not contain '{{{{TASK}}}}'. "
                "The task will not be substituted into the prompt."
            )

    for p in config.get("providers", []):
        if not isinstance(p, dict):
            continue
        name = p.get("name", "unknown")
        cmd = p.get("command", "")
        if not cmd:
            continue
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            continue
        if not tokens:
            continue
        base = tokens[0].lower()
        if base in _INTERACTIVE_LAUNCHERS:
            info = _INTERACTIVE_LAUNCHERS[base]
            has_headless = any(flag in tokens for flag in info["headless_flags"])
            if not has_headless:
                warnings.append(
                    f"Provider '{name}' command '{cmd}' looks like a bare/interactive "
                    f"{base} launcher. {info['message']}"
                )

        env = p.get("env", {})
        for k, v in env.items():
            if isinstance(v, str) and "REPLACE_ME" in v:
                warnings.append(
                    f"Provider '{name}' env variable '{k}' still contains 'REPLACE_ME'. "
                    "Set a real value before running."
                )

    for w in warnings:
        log(w, color="yellow")


def load_config(config_path=None):
    path = config_path or CONFIG_PATH
    if not path.exists():
        sys.exit("config.json not found.")
    config = json.loads(path.read_text())
    validate_config(config)
    return config


def load_state():
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text())
    else:
        state = {"provider_cooldowns": {}}
    state.setdefault("completed_task_durations", [])
    return state


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2))


_ANSI_CODES = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "bold_red": "1;31",
    "bold_green": "1;32",
    "bold_yellow": "1;33",
    "bold_cyan": "1;36",
}


def style(text, name):
    """Wrap text in an ANSI color/style code. No-op when stdout isn't a real
    terminal, so redirected output and log files never get escape codes."""
    code = _ANSI_CODES.get(name)
    if not code or not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def log(msg, color=None):
    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(style(line, color) if color else line)
    with open(LOG_DIR / "orchestrator.log", "a") as f:
        f.write(line + "\n")  # plain text on disk -- no escape codes in the log file


def notify(title, message):
    """Fire-and-forget desktop notification. Uses osascript on macOS,
    notify-send on Linux when available, otherwise silently does nothing."""
    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
                capture_output=True, timeout=5,
            )
        elif sys.platform.startswith("linux"):
            subprocess.run(
                ["notify-send", title, message],
                capture_output=True, timeout=5,
            )
    except Exception:
        pass


# --------------------------------------------------------------------------
# Todo handling
# --------------------------------------------------------------------------

def _get_section_for_line(text: str, target_line: str) -> str:
    """Return the header text for the section containing the given task line,
    or '' if it is not under any header. Headers are lines matching ^#{1,6} .
    """
    current_header = ""
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^#{1,6}\s+.+", stripped):
            current_header = stripped.lstrip("#").strip()
        elif stripped == target_line.strip():
            return current_header
    return ""


def load_tasks(todo_path: Path, skip_sections=None):
    skip_sections = [s.lower() for s in (skip_sections or [])]
    if not skip_sections:
        return re.findall(TASK_REGEX, todo_path.read_text())
    text = todo_path.read_text()
    all_tasks = re.findall(TASK_REGEX, text)
    return [
        t for t in all_tasks
        if _get_section_for_line(text, f"- [ ] {t}").lower() not in skip_sections
    ]


def mark_complete(todo_path: Path, task: str):
    text = todo_path.read_text()
    text = text.replace(f"- [ ] {task}", f"- [x] {task}", 1)
    todo_path.write_text(text)


def defer_task(todo_path: Path, task: str):
    """Move a task to the end of the file, still unchecked. Without this, a
    task that never succeeds (and is never explicitly marked complete) stays
    at index 0 forever -- load_tasks() always re-reads from the top, so it
    would be retried on every single loop iteration, permanently blocking
    every other task behind it."""
    text = todo_path.read_text()
    text = text.replace(f"- [ ] {task}", "", 1)
    if text.endswith("\n"):
        text = text.rstrip("\n") + f"\n- [ ] {task}\n"
    else:
        text = text + f"\n- [ ] {task}\n"
    todo_path.write_text(text)


def _count_matching_lines(text, line_pattern, skip_sections):
    """Count lines matching line_pattern, excluding any under a section whose
    header is in skip_sections. Counts occurrences directly rather than
    de-duplicating by line text -- a set-based diff here would undercount
    whenever two different sections happen to contain byte-identical task
    text (a real thing we found in an actual Todo.md), since a set can't
    tell two identical lines in different sections apart."""
    if not skip_sections:
        return len(re.findall(line_pattern, text, re.MULTILINE))
    count = 0
    current_header = ""
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^#{1,6}\s+.+", stripped):
            current_header = stripped.lstrip("#").strip()
        elif re.match(line_pattern, stripped):
            if current_header.lower() not in skip_sections:
                count += 1
    return count


def count_total_tasks(todo_path: Path, skip_sections=None):
    text = todo_path.read_text()
    skip_sections = [s.lower() for s in (skip_sections or [])]
    return _count_matching_lines(text, r"^- \[.\] .+$", skip_sections)


def count_completed_tasks(todo_path: Path, skip_sections=None):
    text = todo_path.read_text()
    skip_sections = [s.lower() for s in (skip_sections or [])]
    return _count_matching_lines(text, r"^- \[x\] .+$", skip_sections)


def format_duration(seconds):
    if seconds is None:
        return "unknown"
    seconds = int(round(seconds))
    if seconds < 0:
        return "0s"
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s"
    else:
        h, rem = divmod(seconds, 3600)
        m, _ = divmod(rem, 60)
        if m == 0:
            return f"{h}h"
        return f"{h}h {m}m"


def print_summary(state, todo_path, log_path=None):
    if log_path is None:
        log_path = LOG_DIR / "orchestrator.log"
    today_str = datetime.date.today().isoformat()

    completed_today = 0
    failed_today = 0
    first_ts = None
    last_ts = None

    if log_path.exists():
        for line in log_path.read_text().splitlines():
            if not line.startswith(f"[{today_str}"):
                continue
            ts_str, _, msg = line.partition("] ")
            ts_str = ts_str.lstrip("[")
            try:
                ts = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
            if "Task marked complete:" in msg:
                completed_today += 1
            elif "Task NOT completed:" in msg:
                failed_today += 1

    total_attempts = completed_today + failed_today
    if first_ts and last_ts:
        total_run_seconds = (last_ts - first_ts).total_seconds()
    else:
        total_run_seconds = 0

    durations = state.get("completed_task_durations", [])
    avg_duration = sum(durations) / len(durations) if durations else None

    print("=" * 60)
    print(f"Summary ({today_str})")
    print("=" * 60)
    print(f"Tasks completed today: {completed_today}")
    if total_attempts > 0:
        pct = (completed_today / total_attempts) * 100
        print(f"Success rate: {completed_today}/{total_attempts} ({pct:.0f}%)")
    else:
        print("Success rate: N/A (no tasks attempted today)")
    print(f"Total run time: {format_duration(total_run_seconds)}")
    if avg_duration is not None:
        print(f"Average time per task: {format_duration(avg_duration)}")
    else:
        print("Average time per task: N/A")
    print("=" * 60)


def print_progress(todo_path: Path, state, skip_sections=None):
    completed = count_completed_tasks(todo_path, skip_sections=skip_sections)
    total = count_total_tasks(todo_path, skip_sections=skip_sections)
    if total == 0:
        return

    pct = (completed / total) * 100
    remaining = total - completed

    durations = state.get("completed_task_durations", [])
    durations = durations[-50:]
    if durations and remaining > 0:
        avg = sum(durations) / len(durations)
        eta_seconds = remaining * avg
        eta_str = format_duration(eta_seconds)
    else:
        eta_str = "unknown"

    line = f"Progress + ETA: Task {completed}/{total} ({pct:.0f}%), ~{eta_str} remaining"
    log(line, color="blue")


def build_prompt(task: str, template_path: Path):
    if template_path.exists():
        return template_path.read_text().replace("{{TASK}}", task)
    return (
        f"Complete ONLY this task:\n{task}\n\n"
        "Rules:\n"
        "- Modify code as needed.\n"
        "- Run tests if applicable.\n"
        "- Fix any errors you introduce.\n"
        "- When finished, stop and exit. Do not start another task.\n"
    )


def get_task_timeout(task_text: str, global_timeout, overrides: dict):
    """Return the effective subprocess timeout for a task.

    Looks for bracket tags like [big] or [slow] in the task text; if any
    match keys in ``overrides``, the largest matching timeout wins.
    Falls back to ``global_timeout`` when there are no matches.
    A None ``global_timeout`` means \"no limit\" and is propagated as-is.
    """
    if not overrides:
        return global_timeout
    tags = re.findall(TAG_REGEX, task_text)
    matching = [overrides[t] for t in tags if t in overrides]
    if not matching:
        return global_timeout
    return max(matching)


# --------------------------------------------------------------------------
# Liveness heartbeat
# --------------------------------------------------------------------------
# A subprocess that's captured (not streamed) gives no visible sign of life
# for the whole task -- these two cheap, stdlib-only checks (shelling out to
# `ps` and `git`, same as the rest of the codebase already does) are what
# distinguish "still genuinely working" from "silently stuck": real work
# shows up as CPU time and/or a growing uncommitted diff; a stall shows
# neither, which is the actual signature we used to diagnose a stuck task by
# hand before this existed.

def _process_group_cpu_percent(pgid):
    """Sum %CPU across every process in the given process group. None on failure."""
    try:
        result = subprocess.run(
            ["ps", "-A", "-o", "pid=,pgid=,%cpu="],
            capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    total = 0.0
    found = False
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            line_pgid, cpu = int(parts[1]), float(parts[2])
        except ValueError:
            continue
        if line_pgid == pgid:
            total += cpu
            found = True
    return total if found else None


def _git_dirty_count(working_directory):
    """Count files with uncommitted changes. None if not a git repo / on failure."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=working_directory, capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return len([line for line in result.stdout.splitlines() if line.strip()])


# --------------------------------------------------------------------------
# Provider pool
# --------------------------------------------------------------------------

class Provider:
    """
    A provider is any way of launching a coding agent: a CLI command with its
    own env vars (API key, model name, base URL). Examples:

      - Anthropic via Claude Code CLI
      - OpenRouter free model via Kilo Code CLI (different model flag)
      - Nvidia NIM endpoint via Kilo Code CLI (OpenAI-compatible base URL)
      - Local LM Studio server (OpenAI-compatible base URL, no API key)

    Config shape (see config.json "providers" list):
    {
      "name": "openrouter-free",
      "command": "kilo --auto --model deepseek/deepseek-chat-v3-0324:free",
      "env": {"OPENROUTER_API_KEY": "sk-..."},
      "rate_limit_patterns": ["rate limit", "429", "quota exceeded"],
      "cooldown_seconds": 3600,
      "stats_command": "kilo stats"
    }
    """

    def __init__(self, cfg, subprocess_timeout=None, stall_timeout=600):
        self.name = cfg["name"]
        self.command = cfg["command"]
        self.env = cfg.get("env", {})
        self.rate_limit_patterns = [p.lower() for p in cfg.get("rate_limit_patterns", [])]
        self.cooldown_seconds = cfg.get("cooldown_seconds", 600)
        self.subprocess_timeout = subprocess_timeout
        self.stall_timeout = stall_timeout
        self.priority = int(cfg.get("priority", 0))
        self.stats_command = cfg.get("stats_command")

    def is_available(self, state):
        until = state["provider_cooldowns"].get(self.name, 0)
        return time.time() >= until

    def mark_exhausted(self, state):
        state["provider_cooldowns"][self.name] = time.time() + self.cooldown_seconds
        save_state(state)
        log(f"Provider '{self.name}' marked exhausted. Cooling down for {self.cooldown_seconds}s.", color="yellow")

    def run(self, prompt: str, working_directory: str, task_timeout=None):
        """Run this provider's command with the prompt on stdin.
        Returns (exit_code, combined_output, looked_rate_limited: bool).
        ``task_timeout`` overrides ``self.subprocess_timeout`` for this single
        run when provided."""
        global _current_process
        env = os.environ.copy()
        env.update(self.env)
        cmd = shlex.split(self.command)
        try:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=working_directory,
                env=env,
                start_new_session=True,
            )
        except FileNotFoundError as e:
            log(f"Provider '{self.name}' command not found: {e}", color="bold_red")
            return 127, str(e), False

        _current_process = process
        try:
            return self._wait_for_result(process, prompt, working_directory, task_timeout)
        finally:
            _current_process = None

    def _wait_for_result(self, process, prompt, working_directory, task_timeout=None):
        # provider.run() blocks for the whole task (the agent CLI's own output
        # is captured, not streamed), so without this the terminal would sit
        # silent for the entire run. Do the actual communicate() on a
        # background thread and spin in the foreground so there's visible
        # proof of life while waiting.
        result = {}

        def _communicate():
            try:
                result["stdout"], result["stderr"] = process.communicate(input=prompt)
            except Exception as e:
                result["exc"] = e

        comm_thread = threading.Thread(target=_communicate, daemon=True)
        start = time.time()
        comm_thread.start()

        spinner = "|/-\\"
        frame = 0
        interactive = sys.stdout.isatty()
        heartbeat_interval = 3  # how often to refresh cpu%/file-change stats (cheap but not free)
        log_heartbeat_interval = 30  # how often to write a heartbeat line to the log/JSON log
        last_heartbeat_check = 0
        last_logged_heartbeat = start
        cpu_pct = None
        dirty_count = None
        last_dirty_count = None
        last_activity_time = start  # last time we saw real CPU or a file-change, for stall detection
        stalled = False
        effective_timeout = task_timeout if task_timeout is not None else self.subprocess_timeout
        while comm_thread.is_alive():
            elapsed = time.time() - start
            # effective_timeout of None means "no limit" -- just keep polling
            # and showing the spinner for as long as the task takes.
            if effective_timeout is not None and elapsed >= effective_timeout:
                break

            now = time.time()
            if now - last_heartbeat_check >= heartbeat_interval:
                cpu_pct = _process_group_cpu_percent(process.pid)
                dirty_count = _git_dirty_count(working_directory)
                had_cpu = cpu_pct is not None and cpu_pct > STALL_CPU_THRESHOLD
                made_changes = (
                    dirty_count is not None
                    and last_dirty_count is not None
                    and dirty_count != last_dirty_count
                )
                if had_cpu or made_changes:
                    last_activity_time = now
                last_dirty_count = dirty_count
                last_heartbeat_check = now

            if self.stall_timeout is not None and (now - last_activity_time) >= self.stall_timeout:
                stalled = True
                break

            if interactive:
                stats = []
                if cpu_pct is not None:
                    stats.append(f"cpu:{cpu_pct:.0f}%")
                if dirty_count is not None:
                    stats.append(f"files changed:{dirty_count}")
                idle_for = int(now - last_activity_time)
                if idle_for >= heartbeat_interval:
                    stats.append(f"idle:{idle_for}s")
                stats_str = (" " + " ".join(stats)) if stats else ""
                sys.stdout.write("\r" + style(f"[{self.name}] working {spinner[frame % len(spinner)]} ({int(elapsed)}s){stats_str} ", "cyan"))
                sys.stdout.flush()
                frame += 1

            if now - last_logged_heartbeat >= log_heartbeat_interval:
                log(f"[{self.name}] still working... {int(elapsed)}s elapsed"
                    + (f", cpu {cpu_pct:.0f}%" if cpu_pct is not None else "")
                    + (f", {dirty_count} files changed" if dirty_count is not None else "")
                    + f", idle {int(now - last_activity_time)}s",
                    color="dim")
                log_json("heartbeat", provider=self.name, elapsed=int(elapsed), cpu_pct=cpu_pct,
                          files_changed=dirty_count, idle_seconds=int(now - last_activity_time))
                last_logged_heartbeat = now

            time.sleep(0.2)

        if interactive:
            sys.stdout.write("\r" + " " * 60 + "\r")
            sys.stdout.flush()

        if comm_thread.is_alive():
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            comm_thread.join(timeout=5)
            if stalled:
                log(f"Provider '{self.name}' looks stalled -- no CPU activity or file changes for "
                    f"{self.stall_timeout}s despite still running. Killing and treating as failed.", color="bold_red")
                notify("Provider stalled", f"{self.name} stalled after {self.stall_timeout}s of inactivity")
                log_json("provider_stalled", provider=self.name, stall_timeout=self.stall_timeout)
                return 124, f"Stalled: no activity for {self.stall_timeout}s", False
            label = f"{effective_timeout}s" if effective_timeout is not None else "the configured limit"
            log(f"Provider '{self.name}' timed out after {label}.", color="bold_red")
            return 124, f"Timed out after {label}", False

        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        output = (stdout or "") + "\n" + (stderr or "")
        print(output)  # still show live output in the terminal

        looked_rate_limited = any(p in output.lower() for p in self.rate_limit_patterns)
        return process.returncode, output, looked_rate_limited


def load_providers(config, subprocess_timeout=None, stall_timeout=600):
    providers = [
        Provider(p, subprocess_timeout=subprocess_timeout, stall_timeout=stall_timeout)
        for p in config.get("providers", []) if p.get("enabled", True)
    ]
    if not providers:
        sys.exit("No enabled providers configured in config.json.")
    return providers


def pick_next_provider(providers, state, start_index):
    """Return (provider, index) for the next available provider.
    If any provider has an explicit priority, use priority ordering (highest first, ties broken by original index).
    Otherwise use round-robin from start_index.
    Returns (None, None) if all are on cooldown."""
    if any(p.priority != 0 for p in providers):
        candidates = [(p, i) for i, p in enumerate(providers) if p.is_available(state)]
        if not candidates:
            return None, None
        candidates.sort(key=lambda x: (-x[0].priority, x[1]))
        return candidates[0]

    n = len(providers)
    for offset in range(n):
        idx = (start_index + offset) % n
        if providers[idx].is_available(state):
            return providers[idx], idx
    return None, None


def seconds_until_next_available(providers, state):
    times = [state["provider_cooldowns"].get(p.name, 0) for p in providers]
    soonest = min(times) if times else time.time()
    return max(0, int(soonest - time.time()))


def print_provider_status(providers, state):
    """Print a one-line summary of each provider's cooldown state."""
    now = time.time()
    parts = []
    for p in providers:
        until = state["provider_cooldowns"].get(p.name, 0)
        if until > now:
            parts.append(f"{p.name}=cooldown({int(until - now)}s)")
        else:
            parts.append(f"{p.name}=available")
    return " | ".join(parts)


# --------------------------------------------------------------------------
# Dashboard (stdlib http.server)
# --------------------------------------------------------------------------

_dashboard_state = {
    "current_task": None,
    "current_provider": None,
    "providers": {},
    "history": [],
    "start_time": None,
}

_DASHBOARD_HISTORY_MAX = 50


class DashboardHandler(BaseHTTPRequestHandler):
    """Serves JSON and HTML for the local dashboard."""

    def do_GET(self):
        if self.path == "/api/state":
            self._serve_json()
        else:
            self._serve_html()

    def _serve_json(self):
        state = _dashboard_state
        now = time.time()
        provider_list = []
        for name, info in state.get("providers", {}).items():
            provider_list.append({
                "name": name,
                "available": info.get("available", False),
                "cooldown_until": info.get("cooldown_until"),
            })
        payload = {
            "current_task": state.get("current_task"),
            "current_provider": state.get("current_provider"),
            "providers": provider_list,
            "history": state.get("history", []),
            "uptime_seconds": round(now - state.get("start_time", now), 1) if state.get("start_time") else 0,
        }
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self):
        state = _dashboard_state
        body = _build_html(state).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # suppress request logging — dashboard traffic is noise in the orchestrator log


class DashboardServer(HTTPServer):
    """Minimal HTTP server for the local dashboard."""
    allow_reuse_address = True


def start_dashboard(port):
    """Start the dashboard server on 127.0.0.1:{port} in a background thread.
    Returns the server instance, or None if the port is not a positive integer."""
    if not isinstance(port, int) or port <= 0:
        return None
    try:
        server = DashboardServer(("127.0.0.1", port), DashboardHandler)
    except OSError as e:
        log(f"Dashboard server could not start on port {port}: {e}", color="bold_red")
        return None
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log(f"Dashboard available at http://127.0.0.1:{port}", color="green")
    return server


def update_dashboard_state(current_task=None, current_provider=None, provider_status=None, history_entry=None):
    """Update the shared dashboard state. Called from the orchestrator main loop."""
    state = _dashboard_state
    if current_task is not None:
        state["current_task"] = current_task
    if current_provider is not None:
        state["current_provider"] = current_provider
    if provider_status is not None:
        state["providers"] = provider_status
    if history_entry is not None:
        state.setdefault("history", [])
        state["history"].append(history_entry)
        if len(state["history"]) > _DASHBOARD_HISTORY_MAX:
            state["history"] = state["history"][-_DASHBOARD_HISTORY_MAX:]
    if state.get("start_time") is None:
        state["start_time"] = time.time()


def build_provider_status(providers, state):
    """Build a provider-status dict for the dashboard."""
    now = time.time()
    status = {}
    for p in providers:
        until = state["provider_cooldowns"].get(p.name, 0)
        status[p.name] = {
            "available": until <= now,
            "cooldown_until": until if until > now else None,
        }
    return status


def html_escape(text):
    """Minimal HTML escaping for dashboard output."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_html(state):
    now = time.time()
    uptime = round(now - state.get("start_time", now), 1) if state.get("start_time") else 0
    current_task = state.get("current_task") or "idle"
    current_provider = state.get("current_provider") or "none"

    provider_rows = ""
    for name, info in state.get("providers", {}).items():
        available = info.get("available", False)
        cooldown_until = info.get("cooldown_until")
        if available:
            status_class = "available"
            status_text = "available"
        elif cooldown_until:
            remaining = max(0, int(cooldown_until - now))
            status_class = "cooldown"
            status_text = "cooldown ({0}s)".format(remaining)
        else:
            status_class = "cooldown"
            status_text = "unknown"
        provider_rows += (
            '<tr><td>{0}</td>'
            '<td class="{1}">{2}</td></tr>\n'
        ).format(html_escape(name), status_class, status_text)

    history_rows = ""
    for entry in state.get("history", []):
        status_class = "complete" if entry.get("status") == "complete" else "failed"
        history_rows += (
            '<tr><td>{0}</td>'
            '<td>{1}</td>'
            '<td class="{2}">{3}</td>'
            '<td>{4}</td></tr>\n'
        ).format(
            html_escape(entry.get("task", "")),
            html_escape(entry.get("provider", "")),
            status_class,
            entry.get("status", ""),
            html_escape(entry.get("timestamp", "")),
        )

    if not history_rows:
        history_rows = '<tr><td colspan="4">No history yet</td></tr>\n'

    parts = [
        '<!DOCTYPE html><html><head><meta charset="utf-8">',
        '<meta http-equiv="refresh" content="5">',
        '<title>Orchestrator Dashboard</title>',
        '<style>',
        'body{font-family:sans-serif;margin:2em;background:#f5f5f5;color:#333}',
        'h1{color:#333}',
        '.card{background:#fff;padding:1em;margin:1em 0;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1)}',
        'table{border-collapse:collapse;width:100%}',
        'th,td{padding:0.5em;text-align:left;border-bottom:1px solid #eee}',
        '.available{color:green;font-weight:bold}',
        '.cooldown{color:orange;font-weight:bold}',
        '.complete{color:green}',
        '.failed{color:red}',
        '.uptime{color:#666;font-size:0.9em}',
        '</style></head><body>',
        '<h1>Orchestrator Dashboard</h1>',
        '<p class="uptime">Uptime: {0}s</p>'.format(uptime),
        '<div class="card"><h2>Current Task</h2>',
        '<p><strong>Task:</strong> {0}</p>'.format(html_escape(current_task)),
        '<p><strong>Provider:</strong> {0}</p></div>'.format(html_escape(current_provider)),
        '<div class="card"><h2>Providers</h2>',
        '<table><tr><th>Provider</th><th>Status</th></tr>',
        provider_rows,
        '</table></div>',
        '<div class="card"><h2>Recent History</h2>',
        '<table><tr><th>Task</th><th>Provider</th><th>Status</th><th>Time</th></tr>',
        history_rows,
        '</table></div>',
        '</body></html>',
    ]
    return "".join(parts)

def run_verification(config):
    checks = config.get("verify_commands", [])
    if not checks:
        return True
    for cmd in checks:
        log(f"Verifying: {cmd}", color="cyan")
        result = subprocess.run(
            cmd, shell=True, cwd=config.get("working_directory", "."),
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            failure_msg = f"Verification FAILED: {cmd}\n{result.stdout}\n{result.stderr}"
            log(failure_msg, color="bold_red")
            print(failure_msg, file=sys.stderr)
            return False
    return True


def git_commit(config, task: str):
    if not config.get("auto_commit", False):
        return
    wd = config.get("working_directory", ".")
    check = subprocess.run(
        ["git", "status", "--porcelain"], cwd=wd,
        capture_output=True, text=True,
    )
    if not check.stdout.strip():
        log("No changes to commit. Skipping git commit.", color="dim")
        return
    subprocess.run(["git", "add", "-A"], cwd=wd)
    subprocess.run(["git", "commit", "-m", f"Task: {task}"], cwd=wd)


def run_provider_stats(provider, working_directory: str, task: str):
    """Collect usage/cost stats from the provider CLI if it supports it."""
    stats_cmd = getattr(provider, "stats_command", None)
    if not stats_cmd:
        return
    log(f"[{provider.name}] collecting usage stats...", color="cyan")
    try:
        result = subprocess.run(
            shlex.split(stats_cmd),
            cwd=working_directory,
            env={**os.environ, **provider.env},
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        log(f"[{provider.name}] stats command not found: {stats_cmd}", color="yellow")
        log_json("provider_stats_error", provider=provider.name, error="command_not_found", task=task)
        return
    except subprocess.TimeoutExpired:
        log(f"[{provider.name}] stats command timed out after 30s", color="yellow")
        log_json("provider_stats_error", provider=provider.name, error="timeout", task=task)
        return
    except Exception as e:
        log(f"[{provider.name}] stats command failed: {e}", color="yellow")
        log_json("provider_stats_error", provider=provider.name, error=str(e), task=task)
        return

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    log(f"[{provider.name}] stats output (exit {result.returncode}):", color="dim")
    if stdout:
        log(stdout)
    if stderr:
        log(stderr, color="yellow")

    stats_payload = {"provider": provider.name, "task": task, "exit_code": result.returncode}
    if stdout:
        try:
            stats_payload["data"] = json.loads(stdout)
        except json.JSONDecodeError:
            stats_payload["raw_output"] = stdout
    if stderr:
        stats_payload["stderr"] = stderr

    log_json("provider_stats", **stats_payload)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def _sigint_handler(signum, frame):
    log("Interrupted by user (SIGINT). Saving state and exiting...", color="yellow")
    if _current_process is not None:
        try:
            os.killpg(os.getpgid(_current_process.pid), signal.SIGKILL)
            log(f"Killed in-flight agent subprocess group (pid {_current_process.pid}).", color="yellow")
        except (ProcessLookupError, PermissionError):
            pass
    save_state(load_state())
    # 130 is the conventional exit code for a SIGINT-terminated process. A
    # supervisor script uses this to tell "user asked to stop" apart from
    # "crashed" -- it must never auto-restart after an intentional interrupt.
    sys.exit(130)


def main():
    signal.signal(signal.SIGINT, _sigint_handler)

    parser = argparse.ArgumentParser(
        description="Task Orchestrator — drives a coding-agent CLI through a task backlog."
    )
    parser.add_argument(
        "--config", default="config.json",
        help="Path to config.json (default: config.json)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the next task and provider without executing anything"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run only a single task and then exit"
    )
    parser.add_argument(
        "--json-logs", action="store_true",
        help="Append structured JSON log lines to logs/orchestrator.jsonl alongside normal logs"
    )
    parser.add_argument(
        "--skip-section", action="append", default=[],
        help="Exclude tasks under a Todo.md section (markdown header) from being processed. Repeatable."
    )
    parser.add_argument(
        "--summary", action="store_true",
        help="Print a summary of today's run statistics and exit"
    )
    args = parser.parse_args()

    config = load_config(Path(args.config))
    lint_config(config)
    lint_todo(Path(config["todo_file"]))
    global _json_log_enabled
    _json_log_enabled = args.json_logs or config.get("json_logs", False)

    state = load_state()
    subprocess_timeout = config.get("subprocess_timeout", 180)
    stall_timeout = config.get("stall_timeout_seconds", 600)
    timeout_overrides = config.get("subprocess_timeout_overrides", {})
    providers = load_providers(config, subprocess_timeout=subprocess_timeout, stall_timeout=stall_timeout)
    log(f"Providers: {print_provider_status(providers, state)}", color="blue")

    dashboard_port = config.get("dashboard_port")
    dashboard_server = start_dashboard(dashboard_port)
    _dashboard_state["start_time"] = time.time()

    todo_path = Path(config["todo_file"])
    prompt_template = Path(config.get("prompt_template", "prompts/task_prompt.txt"))
    delay = config.get("delay_seconds", 60)
    max_retries_per_provider = config.get("max_retries_per_provider", 1)
    require_confirmation = config.get("require_manual_confirmation", True)
    working_directory = config.get("working_directory", ".")

    if not todo_path.exists():
        sys.exit(f"Todo file not found: {todo_path}")

    if args.dry_run:
        tasks = load_tasks(todo_path, skip_sections=args.skip_section)
        if not tasks:
            log("Dry-run: no pending tasks.")
            log_json("dry_run", no_tasks=True)
            return

        task = tasks[0]
        provider, idx = pick_next_provider(providers, state, 0)
        if provider is None:
            wait_s = seconds_until_next_available(providers, state)
            log(f"Dry-run: all providers exhausted. Next available in {wait_s}s.")
            log_json("dry_run", all_exhausted=True, wait_seconds=wait_s)
        else:
            log(f"Dry-run: would run task via provider '{provider.name}'.")
            log(f"  Task   : {task}")
            log(f"  Command: {provider.command}")
            log(f"  Cooldown remaining: {max(0, int(state['provider_cooldowns'].get(provider.name, 0) - time.time()))}s")
            log_json("dry_run", provider=provider.name, task=task, wait_seconds=max(0, int(state['provider_cooldowns'].get(provider.name, 0) - time.time())))
        return

    if args.summary:
        print_summary(state, todo_path)
        return

    if args.skip_section:
        log(f"Skipping sections: {', '.join(args.skip_section)}", color="yellow")

    provider_idx = 0  # round-robin cursor across tasks

    while True:
        tasks = load_tasks(todo_path, skip_sections=args.skip_section)
        if not tasks:
            print_progress(todo_path, state, skip_sections=args.skip_section)
            log("All tasks completed. Exiting.", color="bold_green")
            notify("All tasks completed", "All tasks in Todo.md are done")
            break

        task = tasks[0]
        task_start_time = time.time()
        log("=" * 60, color="dim")
        log(f"Starting task: {task}", color="bold_green")
        print_progress(todo_path, state, skip_sections=args.skip_section)
        log_json("task_start", task=task, provider_idx=provider_idx)
        update_dashboard_state(
            current_task=task,
            current_provider=None,
            provider_status=build_provider_status(providers, state),
        )

        prompt = build_prompt(task, prompt_template)
        task_timeout = get_task_timeout(task, subprocess_timeout, timeout_overrides)
        if task_timeout != subprocess_timeout:
            log(f"Task timeout override: {task_timeout}s (global: {subprocess_timeout}s)", color="magenta")
        task_done = False

        while not task_done:
            provider, idx = pick_next_provider(providers, state, provider_idx)

            if provider is None:
                wait_s = seconds_until_next_available(providers, state)
                log(f"All providers exhausted. Sleeping {wait_s}s until one frees up...", color="yellow")
                notify("All providers exhausted", f"Sleeping {wait_s}s until a provider is available")
                time.sleep(wait_s + 1)
                continue  # re-check availability

            provider_idx = idx  # remember where we are for next round
            log(f"Using provider: {provider.name}", color="cyan")
            log_json("provider_selected", provider=provider.name, index=idx)
            update_dashboard_state(
                current_provider=provider.name,
                provider_status=build_provider_status(providers, state),
            )

            attempt_success = False
            for attempt in range(1, max_retries_per_provider + 1):
                log(f"[{provider.name}] attempt {attempt}/{max_retries_per_provider}", color="dim")
                exit_code, output, rate_limited = provider.run(prompt, working_directory, task_timeout)

                # rate_limited is just a substring match over the CLI's combined
                # output -- in this repo specifically, task text and generated code
                # routinely contain "rate limit", "429", "quota" etc. as *domain
                # vocabulary*, not as a real rate-limit error. Confirm it against
                # the working tree before trusting it: a real rate-limit hit means
                # the agent didn't get to do anything, so if files actually changed,
                # this was a false positive on a genuine completion, not a real
                # exhaustion event.
                diff_stat = subprocess.run(
                    ["git", "diff", "--stat"],
                    cwd=working_directory, capture_output=True, text=True, timeout=2,
                )
                stat_output = diff_stat.stdout.strip() if diff_stat.returncode == 0 else ""
                rate_limited = rate_limited and not stat_output

                exit_color = "yellow" if rate_limited else ("green" if exit_code == 0 else "red")
                log(f"[{provider.name}] exit code {exit_code}"
                    + (" (looked rate-limited)" if rate_limited else ""), color=exit_color)

                if rate_limited:
                    provider.mark_exhausted(state)
                    log_json("provider_exhausted", provider=provider.name, reason="rate_limited")
                    log(f"Providers: {print_provider_status(providers, state)}", color="blue")
                    break  # stop retrying this provider, rotate to next

                if exit_code == 124:
                    # Timed out -- we already know it didn't finish, so there's
                    # nothing meaningful to confirm. Treat it like any other
                    # failed attempt instead of asking "mark complete?".
                    log(f"[{provider.name}] timed out before finishing -- treating as a failed attempt.", color="bold_red")
                    continue

                verified = run_verification(config)

                # exit_code == 0 alone isn't proof a task actually did anything --
                # a real incident: kilo reported success on a task and had made
                # zero edits. Treat a "success" with no changes as suspicious rather
                # than trusting it at face value, in both confirmation modes.
                suspicious = exit_code == 0 and diff_stat.returncode == 0 and not stat_output
                if suspicious:
                    log(f"[{provider.name}] SUSPICIOUS: exit code 0 but no files changed -- "
                        "this looks like a false success, not a real completion.", color="bold_red")
                    log_json("suspicious_completion", provider=provider.name, task=task)

                if require_confirmation:
                    notify("Task needs confirmation", f"Task: {task}\nProvider: {provider.name}")
                    if stat_output:
                        log("Working tree changes (git diff --stat):\n" + stat_output, color="yellow")
                    prompt_label = (
                        "mark complete despite NO changes detected?" if suspicious else "mark complete?"
                    )
                    answer = input(
                        style(f"\nTask '{task}' via '{provider.name}' — {prompt_label} "
                              "(y/n/retry/skip-provider/skip-task): ",
                              "bold_red" if suspicious else "bold_cyan")
                    ).strip().lower()
                    if answer == "y":
                        attempt_success = True
                        break
                    elif answer == "retry":
                        continue
                    elif answer == "skip-provider":
                        provider.mark_exhausted(state)
                        log(f"Providers: {print_provider_status(providers, state)}", color="blue")
                        break
                    elif answer == "skip-task":
                        defer_task(todo_path, task)
                        log(f"Task deferred to end of todo list: {task}", color="yellow")
                        task_done = True
                        break
                    else:
                        log("Task left pending by user choice.", color="yellow")
                        break
                else:
                    if exit_code == 0 and verified and not suspicious:
                        attempt_success = True
                        break
                    elif suspicious:
                        log(f"[{provider.name}] Not auto-completing a suspicious result -- "
                            "treating as a failed attempt.", color="bold_red")
                        # falls through to the same retry/defer path as any other failure
                    # non-rate-limit failure: retry same provider up to max_retries_per_provider

            if attempt_success:
                mark_complete(todo_path, task)
                duration = time.time() - task_start_time
                durations = state.get("completed_task_durations", [])
                durations.append(duration)
                state["completed_task_durations"] = durations[-200:]
                save_state(state)
                git_commit(config, task)
                run_provider_stats(provider, working_directory, task)
                log(f"Task marked complete: {task} (provider: {provider.name})", color="bold_green")
                print_progress(todo_path, state, skip_sections=args.skip_section)
                log_json("task_complete", task=task, provider=provider.name)
                update_dashboard_state(
                    current_task=None,
                    current_provider=None,
                    provider_status=build_provider_status(providers, state),
                    history_entry={
                        "task": task,
                        "provider": provider.name,
                        "status": "complete",
                        "timestamp": datetime.datetime.now().isoformat(),
                    },
                )
                task_done = True
                provider_idx = (idx + 1) % len(providers)  # rotate for load balancing
                log(f"Providers: {print_provider_status(providers, state)}", color="blue")
            elif provider.is_available(state):
                # Failed for a non-rate-limit reason and user didn't want a retry -> give up on task
                log(f"Task NOT completed: {task}", color="bold_red")
                log_json("task_failed", task=task, provider=provider.name)
                update_dashboard_state(
                    current_task=None,
                    current_provider=None,
                    provider_status=build_provider_status(providers, state),
                    history_entry={
                        "task": task,
                        "provider": provider.name,
                        "status": "failed",
                        "timestamp": datetime.datetime.now().isoformat(),
                    },
                )
                if not config.get("continue_on_failure", True):
                    log("Stopping (continue_on_failure=false).", color="bold_red")
                    notify("Orchestrator stopped", f"Task failed: {task}\ncontinue_on_failure=false")
                    log_json("stop")
                    return
                # Defer it to the end, still unchecked, rather than leaving it at
                # index 0 -- otherwise this exact task gets retried forever on
                # every loop iteration and nothing else in the backlog ever runs.
                defer_task(todo_path, task)
                task_done = True  # move on to next task in Todo.md
            else:
                # Provider just got marked exhausted -> loop again to pick the next one immediately
                log(f"Rotating away from exhausted provider '{provider.name}'...", color="yellow")
                log_json("provider_exhausted", provider=provider.name)
                provider_idx = (idx + 1) % len(providers)
                update_dashboard_state(
                    provider_status=build_provider_status(providers, state),
                )
                log(f"Providers: {print_provider_status(providers, state)}", color="blue")
                continue

        log(f"Waiting {delay} seconds before next task...", color="dim")
        time.sleep(delay)

        if args.once:
            log("--once flag set. Exiting after one task.", color="dim")
            break


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise  # explicit sys.exit() calls (0, 130, ...) pass through unchanged
    except Exception:
        import traceback
        tb = traceback.format_exc()
        # Any state that matters (Todo.md checkboxes, state.json cooldowns) is
        # already written to disk immediately as it changes, not batched --
        # so a crash here doesn't lose progress, it just needs to be visible
        # and exit with a code a supervisor script knows means "restart me".
        log("Unhandled exception -- exiting so a supervisor can restart. Progress on disk is intact.", color="bold_red")
        log(tb, color="bold_red")
        log_json("crash", traceback=tb)
        sys.exit(1)
