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

Edit task-orchestrator.config.json to configure your providers, Todo file, and delay.
"""

import os
import re
import sys
import json
import time
import shlex
import signal
import atexit
import shutil
import subprocess
import datetime
import threading
import webbrowser
from pathlib import Path

from .dashboard import (
    _dashboard_state,
    _dashboard_server_ref,  # noqa: F401 — re-exported for tests and signal handlers
    _shutdown_dashboard_server,
    start_dashboard,
    update_dashboard_state,
    build_provider_status,
    refresh_dashboard_tasks_from_todo,
    mark_dashboard_tasks_running,
    mark_dashboard_tasks_skipped,
    mark_dashboard_tasks_finished,
    DashboardHandler,  # noqa: F401 — re-exported for tests
    DashboardServer,  # noqa: F401 — re-exported for tests
    _build_html,  # noqa: F401 — re-exported for tests
    html_escape,  # noqa: F401 — re-exported for tests
)
from .git import (
    DEFAULT_CONFIG_FILENAME,  # noqa: F401 — re-exported for tests and config code
    TASK_REGEX,  # noqa: F401 — re-exported for tests
    _GIT_LOCK_PATTERNS,  # noqa: F401 — re-exported for tests
    _count_matching_lines,  # noqa: F401 — re-exported for tests
    _get_section_for_line,  # noqa: F401 — re-exported for tests
    _git_dirty_count,  # noqa: F401 — re-exported for tests
    _is_transient_git_error,  # noqa: F401 — re-exported for tests
    _todo_lock,  # noqa: F401 — re-exported for tests
    count_completed_tasks,  # noqa: F401 — re-exported for tests
    count_total_tasks,  # noqa: F401 — re-exported for tests
    defer_task,  # noqa: F401 — re-exported for tests
    git_run,  # noqa: F401 — re-exported for tests
    load_tasks,  # noqa: F401 — re-exported for tests
    mark_complete,  # noqa: F401 — re-exported for tests
    validate_git_working_tree,  # noqa: F401 — re-exported for tests
)
from .notify import (
    _ANSI_CODES,  # noqa: F401 — re-exported for tests
    _applescript_escape,  # noqa: F401 — re-exported for tests
    _play_audio_cue,  # noqa: F401 — re-exported for tests
    _print_startup_banner,  # noqa: F401 — re-exported for tests
    notify,  # noqa: F401 — re-exported for tests
    style,  # noqa: F401 — re-exported for tests
)

CONFIG_PATH = Path(DEFAULT_CONFIG_FILENAME)
STATE_PATH = Path("state.json")
PID_PATH = Path("orchestrator.pid")
DASHBOARD_OPENED_SENTINEL = Path(".dashboard_opened")
LOG_DIR = Path("logs")
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per file
LOG_BACKUP_COUNT = 5
TAG_REGEX = r"(\[\w+\])"
MAX_TASKS_PER_BATCH = 5  # hard cap -- one agent invocation covering too many
# tasks at once makes the all-or-nothing verify/commit gate too coarse (a
# single bad task in a big batch discards everything else alongside it).
STALL_CPU_THRESHOLD = 12.0  # %cpu below this counts as "idle" for stall detection.
# Calibrated from real observed data: a genuinely stalled process read 0-4%
# CPU (event-loop/GC noise, not real work) and kept resetting the stall timer
# under the old 2.0 threshold, letting a task sit stuck for 7+ hours because
# it never accumulated enough idle time. A genuinely active process read
# 27-49%. 12.0 sits in the real gap between those two clusters.
_json_log_enabled = False
_current_process = None  # in-flight agent subprocess, so SIGINT can clean it up too
_log_lock = threading.Lock()
_run_progress = {"completed": 0, "total": 0}
_interactive_options = {
    "flair_mode": False,
    "ascii_progress": False,
    "provider_glyphs": False,
}
_control_state = {
    "pause_after_task": False,
    "skip_current_task": False,
    "quit_requested": False,
    "current_task": None,
}
_control_lock = threading.Lock()

_SECRET_KEY_PATTERN = re.compile(r"(API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|_KEY$|_PWD$)", re.IGNORECASE)
_secret_values = set()


def _register_secrets(providers):
    """Record env values whose key looks like a credential, so log()/log_json()
    can redact them. Matched by key name (API_KEY, TOKEN, SECRET, ...), not by
    guessing at value shape -- a provider's env is the only place real secrets
    enter this process, since the config file itself is never logged wholesale."""
    for p in providers:
        for k, v in p.env.items():
            if isinstance(v, str) and v and _SECRET_KEY_PATTERN.search(k):
                _secret_values.add(v)


def _mask_secrets(text):
    if not _secret_values or not text:
        return text
    for secret in _secret_values:
        if secret:
            text = text.replace(secret, "***REDACTED***")
    return text


def log_json(event, **kwargs):
    if not _json_log_enabled:
        return
    record = {
        "ts": datetime.datetime.now().isoformat(),
        "event": event,
        **kwargs,
    }
    serialized = _mask_secrets(json.dumps(record))
    LOG_DIR.mkdir(exist_ok=True)
    with _log_lock:
        _rotate_log_file(LOG_DIR / "orchestrator.jsonl")
        with open(LOG_DIR / "orchestrator.jsonl", "a") as f:
            f.write(serialized + "\n")


def _rotate_log_file(log_path):
    if not log_path.exists() or log_path.stat().st_size < LOG_MAX_BYTES:
        return
    oldest = Path(str(log_path) + f".{LOG_BACKUP_COUNT}")
    if oldest.exists():
        oldest.unlink()
    for i in range(LOG_BACKUP_COUNT - 1, 0, -1):
        src = Path(str(log_path) + f".{i}")
        dst = Path(str(log_path) + f".{i + 1}")
        if src.exists():
            src.rename(dst)
    log_path.rename(Path(str(log_path) + ".1"))


def _write_pid_file(dashboard_url):
    record = {
        "pid": os.getpid(),
        "dashboard_url": dashboard_url,
        "start_time": datetime.datetime.now().isoformat(),
    }
    PID_PATH.write_text(json.dumps(record, indent=2) + "\n")


def _remove_pid_file():
    try:
        PID_PATH.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------
# Config / state / logging
# --------------------------------------------------------------------------

def validate_config(config):
    errors = []

    if not isinstance(config, dict):
        sys.exit(f"Config validation failed:\n  - {DEFAULT_CONFIG_FILENAME} must be a JSON object (dict).")

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

    if "tasks_per_batch" in config:
        tpb = config["tasks_per_batch"]
        if not isinstance(tpb, int) or isinstance(tpb, bool) or not (1 <= tpb <= MAX_TASKS_PER_BATCH):
            errors.append(f"'tasks_per_batch' must be an integer between 1 and {MAX_TASKS_PER_BATCH} (got {tpb!r}).")

    if errors:
        sys.exit("Config validation failed:\n" + "\n".join(f"  - {e}" for e in errors))


_INTERACTIVE_LAUNCHERS = {
    "claude": {
        "headless_flags": ["--print", "-p"],
        "message": "Claude Code is interactive by default; use --print/-p for unattended runs "
                   "(and --permission-mode bypassPermissions so it doesn't hang waiting for tool-use approval).",
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
    "copilot": {
        "headless_flags": ["--allow-all-tools", "--yolo", "--allow-all"],
        "message": "GitHub Copilot CLI prompts for tool approval by default; use --allow-all-tools (or --yolo) for unattended runs.",
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
            tokens = shlex.split(cmd, posix=(os.name != "nt"))
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

    if (
        not config.get("require_manual_confirmation", True)
        and config.get("auto_commit", False)
        and not config.get("verify_commands")
    ):
        warnings.append(
            "require_manual_confirmation is false and auto_commit is true, but "
            "verify_commands is empty -- every task where the agent exits 0 and "
            "touches a file will be auto-accepted and committed with no correctness "
            "check at all. Add a real verify_commands gate (e.g. a test suite) or "
            "set require_manual_confirmation back to true."
        )

    for w in warnings:
        log(w, color="yellow")


_ENV_VAR_PATTERN = re.compile(r"\$\{(\w+)\}|\$(\w+)")

GLOBAL_CONFIG_PATH = Path.home() / ".task-orchestrator" / "config.json"


def _interpolate_env_vars(value):
    """Recursively substitute $VAR / ${VAR} in string config values from
    os.environ. A var with no match in the environment is left as literal
    text (not blanked) so a misconfigured env fails loudly downstream
    instead of silently turning into an empty string/command."""
    if isinstance(value, str):
        def _replace(m):
            name = m.group(1) or m.group(2)
            return os.environ.get(name, m.group(0))
        return _ENV_VAR_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _interpolate_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env_vars(v) for v in value]
    return value


def _deep_merge(base, override):
    """Merge dicts recursively; override wins on conflicts. Lists (e.g.
    'providers') are replaced wholesale by override rather than merged
    item-wise -- a global provider list and a project provider list aren't
    meaningfully mergeable entry-by-entry."""
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for k, v in override.items():
            merged[k] = _deep_merge(merged[k], v) if k in merged else v
        return merged
    return override


def load_config(config_path=None):
    path = config_path or CONFIG_PATH
    if not path.exists():
        sys.exit(f"Config file not found: {path}")
    config = json.loads(path.read_text())

    if GLOBAL_CONFIG_PATH.exists():
        try:
            global_config = json.loads(GLOBAL_CONFIG_PATH.read_text())
            config = _deep_merge(global_config, config)
        except (json.JSONDecodeError, OSError) as e:
            log(f"Warning: could not read global config {GLOBAL_CONFIG_PATH}: {e}", color="yellow")

    config = _interpolate_env_vars(config)
    validate_config(config)
    return config


def load_state():
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text())
    else:
        state = {"provider_cooldowns": {}}
    state.setdefault("completed_task_durations", [])
    state.setdefault("provider_rate_limit_counts", {})
    return state


def save_state(state):
    """Write via temp-file + os.replace so a crash mid-write can never leave
    state.json truncated/corrupted -- os.replace is atomic on both POSIX and
    Windows (unlike os.rename, which raises on Windows if the destination
    already exists)."""
    tmp_path = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, indent=2))
    os.replace(tmp_path, STATE_PATH)


def log(msg, color=None):
    with _log_lock:
        _rotate_log_file(LOG_DIR / "orchestrator.log")
        LOG_DIR.mkdir(exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {_mask_secrets(msg)}"
        print(style(line, color) if color else line)
        with open(LOG_DIR / "orchestrator.log", "a") as f:
            f.write(line + "\n")  # plain text on disk -- no escape codes in the log file


def log_file_only(msg):
    """Same as log(), but never printed to the terminal -- for high-volume
    content (a full agent transcript, a full verify_commands failure dump)
    that belongs in logs/orchestrator.log, not scrolling past every short
    status line a human watching the terminal actually wants to see."""
    with _log_lock:
        _rotate_log_file(LOG_DIR / "orchestrator.log")
        LOG_DIR.mkdir(exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {_mask_secrets(msg)}"
        with open(LOG_DIR / "orchestrator.log", "a") as f:
            f.write(line + "\n")


def _set_control_state(key, value):
    with _control_lock:
        _control_state[key] = value


def _get_control_state(key, default=None):
    with _control_lock:
        return _control_state.get(key, default)


def _start_keyboard_listener(enabled):
    """Listen for p/s/q/r commands without blocking the orchestrator loop."""
    if not enabled or not sys.stdin.isatty() or not sys.stdout.isatty():
        return

    def _worker():
        while True:
            try:
                line = sys.stdin.readline()
            except Exception:
                return
            if not line:
                return
            cmd = line.strip().lower()
            if cmd == "p":
                _set_control_state("pause_after_task", True)
                log("Interactive command: pause requested after current task (use 'r' to resume).", color="yellow")
            elif cmd == "r":
                _set_control_state("pause_after_task", False)
                log("Interactive command: resumed.", color="green")
            elif cmd == "s":
                _set_control_state("skip_current_task", True)
                log("Interactive command: skip current task requested.", color="yellow")
            elif cmd == "q":
                _set_control_state("quit_requested", True)
                log("Interactive command: graceful quit requested.", color="yellow")

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


def _progress_bar(completed, total, width=10):
    if total <= 0:
        return "[----------]"
    fill = int(round((completed / total) * width))
    fill = max(0, min(width, fill))
    return "[" + ("#" * fill) + ("-" * (width - fill)) + "]"


def _compute_success_streak(log_path, today_str):
    streak = 0
    paths_to_read = [log_path]
    for i in range(1, LOG_BACKUP_COUNT + 1):
        paths_to_read.append(Path(str(log_path) + f".{i}"))

    events = []
    for current_path in paths_to_read:
        if not current_path.exists():
            continue
        for line in current_path.read_text().splitlines():
            if not line.startswith(f"[{today_str}"):
                continue
            if "Task marked complete:" in line:
                events.append("ok")
            elif "Task NOT completed:" in line:
                events.append("fail")
    for e in reversed(events):
        if e == "ok":
            streak += 1
        else:
            break
    return streak


def _kill_process_tree(process):
    """Best-effort kill of a provider process and its children across OSes."""
    try:
        if os.name == "nt":
            # /T kills child processes; /F forces termination.
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, subprocess.SubprocessError, OSError):
        try:
            process.kill()
        except Exception:
            pass


_PYTHON_ALIASES = {"python": "python3", "python3": "python"}


def _resolve_executable(executable, env=None):
    """Resolve an executable path for subprocess launches.

    On Windows, CLIs often exist as .cmd/.bat/.exe shims, and PowerShell may
    expose script wrappers that are not directly discoverable by bare command
    name from subprocess without shell mediation.

    'python'/'python3' get an extra fallback: whichever name isn't on PATH
    falls back to the other, and finally to sys.executable (this process's
    own interpreter, which always exists) -- the default config's
    verify_commands and stats_command entries are plain 'python ...' strings,
    and only one of 'python'/'python3' is guaranteed to exist on any given
    OS/distro, so a literal name alone isn't portable.
    """
    if not executable:
        return executable

    env = env or os.environ

    # If a path is already provided, trust it.
    if os.path.sep in executable or (os.path.altsep and os.path.altsep in executable):
        return executable

    found = shutil.which(executable, path=env.get("PATH"))
    if found:
        return found

    if os.name == "nt":
        for suffix in (".cmd", ".exe", ".bat", ".ps1"):
            candidate = executable + suffix
            found = shutil.which(candidate, path=env.get("PATH"))
            if found:
                return found

    alias = _PYTHON_ALIASES.get(executable)
    if alias:
        found = shutil.which(alias, path=env.get("PATH"))
        if found:
            return found
        return sys.executable

    return executable


def _resolve_shell_python(cmd: str) -> str:
    """Rewrite a leading bare 'python'/'python3' token in a shell command
    string to whichever interpreter _resolve_executable finds available.
    Used for verify_commands/stats_command, which run as shell strings
    rather than argv lists.

    These strings are executed with shell=True, which on Windows means
    cmd.exe (not a POSIX shell). shlex only understands POSIX quoting, so
    re-serializing the whole command with shlex.join() after splitting can
    produce single-quoted output cmd.exe can't parse. Instead, substitute
    just the leading token in the original string and leave everything
    else -- including its original quoting -- untouched."""
    try:
        tokens = shlex.split(cmd, posix=(os.name != "nt"))
    except ValueError:
        return cmd
    if not tokens or tokens[0] not in _PYTHON_ALIASES:
        return cmd
    resolved = _resolve_executable(tokens[0])
    if resolved == tokens[0]:
        return cmd
    stripped = cmd.lstrip()
    prefix_len = len(cmd) - len(stripped)
    return cmd[:prefix_len] + resolved + stripped[len(tokens[0]):]


# --------------------------------------------------------------------------
# Todo handling (moved to task_orchestrator.git)
# --------------------------------------------------------------------------

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

    paths_to_read = [log_path]
    for i in range(1, LOG_BACKUP_COUNT + 1):
        paths_to_read.append(Path(str(log_path) + f".{i}"))

    for current_path in paths_to_read:
        if not current_path.exists():
            continue
        for line in current_path.read_text().splitlines():
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
    longest = max(durations) if durations else None
    shortest = min(durations) if durations else None
    streak = _compute_success_streak(log_path, today_str)

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
    if longest is not None:
        print(f"Longest task: {format_duration(longest)}")
        print(f"Shortest task: {format_duration(shortest)}")
    print(f"Current success streak: {streak}")
    verdict = "clean run, no retries" if failed_today == 0 and completed_today > 0 else "rough run, review failed tasks"
    print(f"Verdict: {verdict}")
    print("=" * 60)


def print_run_report_card(state, todo_path):
    completed = count_completed_tasks(todo_path)
    total = count_total_tasks(todo_path)
    durations = state.get("completed_task_durations", [])
    avg = (sum(durations) / len(durations)) if durations else None
    log("Run report card:", color="bold_cyan")
    log(f"  Total tasks: {completed}/{total}")
    if avg is not None:
        log(f"  Average task time: {format_duration(avg)}")
    verdict = "clean run, no retries" if completed == total and total > 0 else "partial run, continue backlog"
    log(f"  Verdict: {verdict}")


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


# Cap on how much of a verify_commands failure gets appended to a retry
# prompt. verify output is a full build/test log and can be huge; the goal is
# just enough for the agent to see *what* broke, not to hand it the entire
# transcript and burn tokens re-reading a wall of stack traces.
VERIFY_FEEDBACK_MAX_CHARS = 4000


def build_retry_prompt(base_prompt: str, verify_failure: str) -> str:
    """Append a prior verify_commands failure to a task prompt.

    Without this, a retry re-sends the exact same prompt the previous attempt
    already saw, so the agent has no way to know verification failed or why --
    it just repeats whatever it did before against unchanged code.
    """
    truncated = verify_failure[:VERIFY_FEEDBACK_MAX_CHARS]
    if len(verify_failure) > VERIFY_FEEDBACK_MAX_CHARS:
        truncated += "\n... (truncated)"
    return (
        f"{base_prompt}\n\n---\n"
        "Your previous attempt at this task failed verification. Fix the "
        "following issue(s) in addition to completing the task above, then "
        f"stop:\n\n{truncated}"
    )


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
    if os.name == "nt":
        return None
    try:
        result = subprocess.run(
            ["ps", "-A", "-o", "pid=,pgid=,%cpu="],
            capture_output=True, text=True, errors="replace", timeout=2,
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


# --------------------------------------------------------------------------
# Git operations (moved to task_orchestrator.git)
# --------------------------------------------------------------------------

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

    Config shape (see task-orchestrator.config.json "providers" list):
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

    def get_rate_limit_count(self, state):
        return state.get("provider_rate_limit_counts", {}).get(self.name, 0)

    def mark_exhausted(self, state, reason="rate_limited"):
        counts = state.setdefault("provider_rate_limit_counts", {})
        if reason == "rate_limited":
            count = counts.get(self.name, 0) + 1
            counts[self.name] = count
            max_cooldown = self.cooldown_seconds * 64
            backoff = min(self.cooldown_seconds * (2 ** (count - 1)), max_cooldown)
            state["provider_cooldowns"][self.name] = time.time() + backoff
            log(f"Provider '{self.name}' marked exhausted. Cooling down for {backoff}s (consecutive rate-limit hits: {count}).", color="yellow")
        else:
            if self.name in counts:
                counts[self.name] = 0
            state["provider_cooldowns"][self.name] = time.time() + self.cooldown_seconds
            log(f"Provider '{self.name}' marked exhausted. Cooling down for {self.cooldown_seconds}s.", color="yellow")
        save_state(state)

    def reset_rate_limit_count(self, state):
        counts = state.setdefault("provider_rate_limit_counts", {})
        if self.name in counts and counts[self.name] != 0:
            counts[self.name] = 0
            save_state(state)
            log(f"Provider '{self.name}' rate-limit counter reset (provider responded without rate-limiting).", color="dim")

    def run(self, prompt: str, working_directory: str, task_timeout=None):
        """Run this provider's command with the prompt on stdin, unless the
        command contains a literal ``{{TASK}}`` token -- some agent CLIs (e.g.
        GitHub Copilot CLI's ``-p <text>``) take the prompt as an argv element
        rather than reading stdin, so that token is substituted with the full
        prompt as a single argument instead, and stdin is left empty.
        Returns (exit_code, combined_output, looked_rate_limited: bool).
        ``task_timeout`` overrides ``self.subprocess_timeout`` for this single
        run when provided."""
        global _current_process
        env = os.environ.copy()
        env.update(self.env)
        # Use POSIX splitting first even on Windows so quoted -c payloads
        # become a single argument without embedded quote characters.
        # Fall back to Windows-style tokenization for odd shell-style inputs.
        try:
            cmd = shlex.split(self.command, posix=True)
        except ValueError:
            cmd = shlex.split(self.command, posix=(os.name != "nt"))
        if cmd:
            cmd[0] = _resolve_executable(cmd[0], env=env)
            if os.name == "nt" and cmd[0].lower().endswith(".ps1"):
                shell_host = shutil.which("pwsh", path=env.get("PATH")) or shutil.which("powershell", path=env.get("PATH"))
                if shell_host:
                    cmd = [shell_host, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", cmd[0]] + cmd[1:]
        if "{{TASK}}" in cmd:
            cmd = [prompt if tok == "{{TASK}}" else tok for tok in cmd]
            stdin_input = ""
        else:
            stdin_input = prompt
        try:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                cwd=working_directory,
                env=env,
                start_new_session=True,
            )
        except FileNotFoundError as e:
            log(f"Provider '{self.name}' command not found: {e}", color="bold_red")
            return 127, str(e), False

        _current_process = process
        try:
            return self._wait_for_result(process, stdin_input, working_directory, task_timeout)
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
        flavor = [
            "working",
            "reticulating splines",
            "bribing the linter",
            "indexing intent",
        ]
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
                if os.name == "nt" and cpu_pct is None:
                    # Process-group CPU sampling is Unix-only; keep Windows
                    # runs from being falsely marked stalled.
                    had_cpu = True
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
                if _interactive_options.get("ascii_progress", False) and _run_progress.get("total", 0) > 0:
                    stats.append(_progress_bar(_run_progress.get("completed", 0), _run_progress.get("total", 0)))
                stats_str = (" " + " ".join(stats)) if stats else ""
                label = "working"
                if _interactive_options.get("flair_mode", False):
                    label = flavor[frame % len(flavor)]
                    if elapsed >= 900:
                        label = "this one is taking a while"
                    elif elapsed >= 300:
                        label = "still going, might be worth a coffee"
                sys.stdout.write("\r" + style(f"[{self.name}] {label} {spinner[frame % len(spinner)]} ({int(elapsed)}s){stats_str} ", "cyan"))
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
            _kill_process_tree(process)
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
        # The agent's full transcript (tool calls, file reads, reasoning) can be
        # thousands of lines -- that belongs in the log file, not scrolling past
        # the short per-task status lines a human watching the terminal wants.
        log_file_only(f"[{self.name}] full output:\n{output}")

        looked_rate_limited = any(p in output.lower() for p in self.rate_limit_patterns)
        return process.returncode, output, looked_rate_limited


def load_providers(config, subprocess_timeout=None, stall_timeout=600):
    providers = [
        Provider(p, subprocess_timeout=subprocess_timeout, stall_timeout=stall_timeout)
        for p in config.get("providers", []) if p.get("enabled", True)
    ]
    if not providers:
        sys.exit(f"No enabled providers configured in {DEFAULT_CONFIG_FILENAME}.")
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
    use_glyphs = _interactive_options.get("provider_glyphs", False) and sys.stdout.isatty()
    for p in providers:
        until = state["provider_cooldowns"].get(p.name, 0)
        if until > now:
            label = f"{p.name}=cooldown({int(until - now)}s)"
            parts.append(("🌙 " + label) if use_glyphs else label)
        else:
            label = f"{p.name}=available"
            parts.append(("✅ " + label) if use_glyphs else label)
    return " | ".join(parts)


def run_verification(config):
    """Returns (verified: bool, failure_output: str | None).

    failure_output is the command + its stdout/stderr for the first check that
    failed (or timed out), so a caller can hand it back to the agent on retry
    instead of just re-running the identical original prompt against code that
    hasn't changed. None when every check passed.
    """
    checks = config.get("verify_commands", [])
    if not checks:
        return True, None
    # None means unbounded, matching subprocess_timeout's convention elsewhere --
    # but unlike subprocess_timeout, there's no stall-detection backstop here
    # (verify_commands runs after the agent's own subprocess has already
    # exited), so a hanging build/test command would otherwise block the
    # orchestrator forever with no way to recover. 1800s (30 min) is a
    # generous default for a real build+test pass; override per-project via
    # 'verify_timeout_seconds' if that's genuinely not enough.
    timeout = config.get("verify_timeout_seconds", 1800)
    for cmd in checks:
        resolved_cmd = _resolve_shell_python(cmd)
        log(f"Verifying: {cmd}", color="cyan")
        try:
            result = subprocess.run(
                resolved_cmd, shell=True, cwd=config.get("working_directory", "."),
                capture_output=True, text=True, errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            failure_msg = (
                f"Verification TIMED OUT after {timeout}s: {cmd}\n"
                f"{e.stdout or ''}\n{e.stderr or ''}"
            )
            # Full output (a build/test log can be huge) goes to the log file
            # only; the terminal gets a one-line pointer, not the dump.
            log_file_only(failure_msg)
            log(f"Verification TIMED OUT after {timeout}s: {cmd} (see logs/orchestrator.log)", color="bold_red")
            print(f"Verification TIMED OUT after {timeout}s: {cmd}", file=sys.stderr)
            return False, failure_msg
        if result.returncode != 0:
            failure_msg = f"Verification FAILED: {cmd}\n{result.stdout}\n{result.stderr}"
            log_file_only(failure_msg)
            log(f"Verification FAILED: {cmd} (see logs/orchestrator.log)", color="bold_red")
            print(f"Verification FAILED: {cmd}", file=sys.stderr)
            return False, failure_msg
    return True, None


def git_commit(config, task: str):
    if not config.get("auto_commit", False):
        return
    wd = config.get("working_directory", ".")
    check = git_run(["status", "--porcelain"], cwd=wd)
    if not check.stdout.strip():
        log("No changes to commit. Skipping git commit.", color="dim")
        return
    git_run(["add", "-A"], cwd=wd)
    git_run(["commit", "-m", f"Task: {task}"], cwd=wd)


def run_provider_stats(provider, working_directory: str, task: str):
    """Collect usage/cost stats from the provider CLI if it supports it."""
    stats_cmd = getattr(provider, "stats_command", None)
    if not stats_cmd:
        return
    log(f"[{provider.name}] collecting usage stats...", color="cyan")
    stats_env = {**os.environ, **provider.env}
    tokens = shlex.split(stats_cmd, posix=(os.name != "nt"))
    if tokens:
        tokens[0] = _resolve_executable(tokens[0], env=stats_env)
    try:
        result = subprocess.run(
            tokens,
            cwd=working_directory,
            env=stats_env,
            capture_output=True,
            text=True,
            errors="replace",
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
        log_file_only(stdout)
    if stderr:
        log_file_only(stderr)

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
# Parallel execution
# --------------------------------------------------------------------------

PARALLEL_TAG = "[parallel]"


def _is_parallel_task(task: str) -> bool:
    return PARALLEL_TAG in task.lower() or PARALLEL_TAG in task


def _run_single_task_for_parallel(task, providers, state, config, working_directory, prompt_template, subprocess_timeout, timeout_overrides, stall_timeout):
    """Run one task to completion (used by the parallel executor).
    Returns (task, success: bool)."""
    from concurrent.futures import ThreadPoolExecutor  # noqa: F401 (already at module scope)

    prompt = build_prompt(task, prompt_template)
    task_timeout = get_task_timeout(task, subprocess_timeout, timeout_overrides)
    max_retries = config.get("max_retries_per_provider", 1)

    # Try each available provider
    tried_providers = set()
    while True:
        provider, idx = pick_next_provider(providers, state, 0)
        if provider is None or provider.name in tried_providers:
            log(f"[parallel] Task failed (all providers exhausted): {task[:60]}", color="bold_red")
            return task, False
        tried_providers.add(provider.name)

        log(f"[parallel] [{provider.name}] Running: {task[:60]}...", color="cyan")
        exit_code, output, rate_limited = provider.run(prompt, working_directory, task_timeout)

        # Check for real changes
        diff_stat = git_run(["status", "--porcelain"], cwd=working_directory)
        stat_output = diff_stat.stdout.strip() if diff_stat.returncode == 0 else ""
        rate_limited = rate_limited and not stat_output

        if rate_limited:
            provider.mark_exhausted(state, reason="rate_limited")
            continue

        provider.reset_rate_limit_count(state)

        if exit_code == 0 and stat_output:
            verified, _ = run_verification(config)
            if verified:
                mark_complete(Path(config["todo_file"]), task)
                if config.get("auto_commit", False):
                    git_commit(config, task)
                log(f"[parallel] Task complete: {task[:60]}", color="bold_green")
                return task, True

        log(f"[parallel] [{provider.name}] Task failed (exit {exit_code}): {task[:60]}", color="red")
        return task, False


# --------------------------------------------------------------------------
# init / validate subcommands
# --------------------------------------------------------------------------

_INIT_DEFAULT_CONFIG = {
    "todo_file": "Todo.md",
    "working_directory": ".",
    "prompt_template": "prompts/task_prompt.txt",
    "delay_seconds": 60,
    "subprocess_timeout": 180,
    "stall_timeout_seconds": 600,
    "max_retries_per_provider": 3,
    "tasks_per_batch": 1,
    "require_manual_confirmation": True,
    "continue_on_failure": True,
    "auto_commit": False,
    "verify_commands": [],
    "providers": [
        {
            "name": "example",
            "command": "REPLACE_ME_WITH_YOUR_AGENT_CLI",
            "env": {},
            "rate_limit_patterns": ["rate limit", "429", "quota exceeded"],
            "cooldown_seconds": 600,
        }
    ],
}

_INIT_DEFAULT_TODO = "# Todo\n\n## Backlog\n\n- [ ] Example task: replace this with your first real task\n"

_INIT_DEFAULT_PROMPT = (
    "Complete ONLY the task(s) below (there may be one, or a numbered batch of a few):\n{{TASK}}\n\n"
    "Rules:\n"
    "- If more than one task is listed, complete all of them.\n"
    "- Modify code as needed.\n"
    "- Run tests if applicable.\n"
    "- Fix any errors you introduce.\n"
    "- When finished, stop and exit. Do not start another task beyond what's listed above.\n"
)

_INIT_GITIGNORE_LINES = [
    # The config file commonly holds provider API keys (directly or via env
    # interpolation) -- gitignored by default so a real project's secrets
    # can never land in git just because someone ran `init` and committed
    # everything without thinking about it.
    DEFAULT_CONFIG_FILENAME,
    "state.json", "orchestrator.pid", "logs/", "*.env", ".env",
    "__pycache__/", "*.pyc", "*.lock",
]


def cmd_init(target_dir="."):
    """Scaffold a new project: task-orchestrator.config.json, Todo.md,
    prompts/task_prompt.txt, .gitignore. Never overwrites an existing file --
    reports what it created vs. what it left alone."""
    target = Path(target_dir)
    created, skipped = [], []

    files = {
        target / DEFAULT_CONFIG_FILENAME: json.dumps(_INIT_DEFAULT_CONFIG, indent=2) + "\n",
        target / "Todo.md": _INIT_DEFAULT_TODO,
        target / "prompts" / "task_prompt.txt": _INIT_DEFAULT_PROMPT,
    }
    for path, content in files.items():
        if path.exists():
            skipped.append(str(path))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        created.append(str(path))

    gitignore_path = target / ".gitignore"
    if gitignore_path.exists():
        existing = gitignore_path.read_text()
        missing = [line for line in _INIT_GITIGNORE_LINES if line not in existing]
        if missing:
            with open(gitignore_path, "a") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write("\n".join(missing) + "\n")
            created.append(f"{gitignore_path} (appended {len(missing)} entries)")
        else:
            skipped.append(str(gitignore_path))
    else:
        gitignore_path.write_text("\n".join(_INIT_GITIGNORE_LINES) + "\n")
        created.append(str(gitignore_path))

    for c in created:
        log(f"init: created {c}", color="green")
    for s in skipped:
        log(f"init: skipped {s} (already exists)", color="dim")
    log(f"init: done. Edit {DEFAULT_CONFIG_FILENAME} to set your provider command(s), then run the orchestrator.", color="bold_green")
    return 0


def cmd_validate(config_path):
    """Check config structure, provider executables, git working tree,
    and todo_file -- without running anything. Returns 0 if everything
    checks out, 1 otherwise."""
    if not config_path.exists():
        log(f"validate: config not found at {config_path}", color="bold_red")
        return 1

    try:
        config = load_config(config_path)
    except SystemExit as e:
        log(f"validate: {e}", color="bold_red")
        return 1

    log("validate: config structure OK", color="green")
    ok = True

    lint_config(config)
    lint_todo(Path(config.get("todo_file", "Todo.md")))

    for p in config.get("providers", []):
        if not isinstance(p, dict):
            continue
        name = p.get("name", "unknown")
        if not p.get("enabled", True):
            log(f"validate: provider '{name}' disabled, skipping reachability check", color="dim")
            continue
        cmd = p.get("command", "")
        try:
            tokens = shlex.split(cmd, posix=(os.name != "nt"))
        except ValueError:
            log(f"validate: provider '{name}' command could not be parsed: {cmd}", color="bold_red")
            ok = False
            continue
        if not tokens:
            log(f"validate: provider '{name}' has an empty command", color="bold_red")
            ok = False
            continue
        first = tokens[0]
        resolvable = (
            os.path.sep in first
            or (os.path.altsep and os.path.altsep in first)
            or shutil.which(first) is not None
            or _resolve_executable(first) != first
        )
        if resolvable:
            log(f"validate: provider '{name}' executable '{first}' resolves OK", color="green")
        else:
            log(f"validate: provider '{name}' executable '{first}' not found on PATH", color="bold_red")
            ok = False

    working_directory = config.get("working_directory", ".")
    result = git_run(["rev-parse", "--is-inside-work-tree"], cwd=working_directory)
    if result.returncode != 0 or result.stdout.strip().lower() != "true":
        log(f"validate: working_directory '{working_directory}' is not inside a git working tree", color="bold_red")
        ok = False
    else:
        log("validate: working_directory is a valid git working tree", color="green")

    todo_path = Path(config.get("todo_file", "Todo.md"))
    if not todo_path.exists():
        log(f"validate: todo_file '{todo_path}' does not exist", color="bold_red")
        ok = False
    else:
        total = count_total_tasks(todo_path)
        pending = len(load_tasks(todo_path))
        log(f"validate: todo_file OK ({pending} pending / {total} total tasks)", color="green")

    if ok:
        log("validate: all checks passed", color="bold_green")
        return 0
    log("validate: one or more checks failed", color="bold_red")
    return 1


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def _sigint_handler(signum, frame):
    # state.json/Todo.md are already written to disk immediately on every
    # change (see the crash handler below), never batched in memory -- so
    # there's nothing left to flush here. This just needs to stop the
    # in-flight subprocess and clean up before exiting.
    log("Interrupted by user (SIGINT). Exiting...", color="yellow")
    if _current_process is not None:
        _kill_process_tree(_current_process)
        log(f"Killed in-flight agent subprocess group (pid {_current_process.pid}).", color="yellow")
    _set_control_state("quit_requested", True)
    _shutdown_dashboard_server()
    _remove_pid_file()
    # 130 is the conventional exit code for a SIGINT-terminated process. A
    # supervisor script uses this to tell "user asked to stop" apart from
    # "crashed" -- it must never auto-restart after an intentional interrupt.
    sys.exit(130)


def _sigterm_handler(signum, frame):
    # SIGTERM is what containers/systemd/process managers send for a
    # requested graceful shutdown -- an operator or orchestrator asking to
    # stop, not a crash. Same cleanup as SIGINT, but exit(0): run_forever.sh's
    # contract treats 0 as "clean/intentional finish, don't restart", which
    # is exactly the right behavior for an operator-requested stop.
    log("Received SIGTERM (graceful shutdown requested). Exiting...", color="yellow")
    if _current_process is not None:
        _kill_process_tree(_current_process)
        log(f"Killed in-flight agent subprocess group (pid {_current_process.pid}).", color="yellow")
    _set_control_state("quit_requested", True)
    _shutdown_dashboard_server()
    _remove_pid_file()
    sys.exit(0)


def main(args=None):
    if args is None:
        from .cli import main as _cli_main
        _cli_main()
        return
    signal.signal(signal.SIGINT, _sigint_handler)
    try:
        signal.signal(signal.SIGTERM, _sigterm_handler)
    except (ValueError, AttributeError, OSError):
        pass  # some platforms/threads don't support installing a SIGTERM handler

    # main() may be invoked multiple times in a single Python process (unit
    # tests/import callers). Ensure stale control flags from a prior run do
    # not short-circuit the next run.
    _set_control_state("pause_after_task", False)
    _set_control_state("skip_current_task", False)
    _set_control_state("quit_requested", False)
    _set_control_state("current_task", None)

    if args.command == "init":
        sys.exit(cmd_init())
    if args.command == "validate":
        sys.exit(cmd_validate(Path(args.config)))

    config = load_config(Path(args.config))
    lint_config(config)
    lint_todo(Path(config["todo_file"]))
    global _json_log_enabled
    _json_log_enabled = args.json_logs or config.get("json_logs", False)
    global LOG_MAX_BYTES, LOG_BACKUP_COUNT
    LOG_MAX_BYTES = config.get("log_max_bytes", 10 * 1024 * 1024)
    LOG_BACKUP_COUNT = config.get("log_backup_count", 5)

    state = load_state()
    atexit.register(_remove_pid_file)

    subprocess_timeout = config.get("subprocess_timeout", 180)
    stall_timeout = config.get("stall_timeout_seconds", 600)
    timeout_overrides = config.get("subprocess_timeout_overrides", {})
    providers = load_providers(config, subprocess_timeout=subprocess_timeout, stall_timeout=stall_timeout)
    if args.provider:
        matching = [p for p in providers if p.name == args.provider]
        if not matching:
            sys.exit(f"--provider '{args.provider}' not found among enabled providers: "
                      f"{[p.name for p in providers]}")
        providers = matching
        log(f"Forcing provider: {args.provider}", color="cyan")
    _register_secrets(providers)
    _interactive_options["flair_mode"] = bool(config.get("flair_mode", False))
    _interactive_options["ascii_progress"] = bool(config.get("ascii_progress", False))
    _interactive_options["provider_glyphs"] = bool(config.get("provider_glyphs", False))
    _start_keyboard_listener(bool(config.get("interactive_commands", False)))
    log(f"Providers: {print_provider_status(providers, state)}", color="blue")

    dashboard_port = config.get("dashboard_port")
    dashboard_server = start_dashboard(
        dashboard_port,
        retry_on_port_in_use=bool(config.get("dashboard_retry_on_port_in_use", True)),
    )
    global _dashboard_server_ref
    _dashboard_server_ref = dashboard_server
    active_dashboard_port = dashboard_server.server_port if dashboard_server is not None else None
    dashboard_url = f"http://127.0.0.1:{active_dashboard_port}" if active_dashboard_port else None
    _print_startup_banner(
        providers=providers,
        dashboard_url=dashboard_url,
        require_confirmation=bool(config.get("require_manual_confirmation", True)),
        enabled=bool(config.get("startup_banner", True)),
    )
    if dashboard_url:
        # Plain stdout line keeps the URL easy to click/copy in cmd/powershell.
        print(f"Dashboard URL: {dashboard_url}", flush=True)
    _write_pid_file(dashboard_url)
    if dashboard_url and config.get("open_dashboard_in_browser", False):
        if not DASHBOARD_OPENED_SENTINEL.exists():
            try:
                webbrowser.open(dashboard_url)
            except Exception:
                pass
            else:
                DASHBOARD_OPENED_SENTINEL.write_text("")
    _dashboard_state["start_time"] = time.time()

    todo_path = Path(config["todo_file"])
    prompt_template = Path(config.get("prompt_template", "prompts/task_prompt.txt"))
    delay = config.get("delay_seconds", 60)
    max_retries_per_provider = config.get("max_retries_per_provider", 1)
    require_confirmation = config.get("require_manual_confirmation", True)
    working_directory = config.get("working_directory", ".")
    validate_git_working_tree(working_directory)

    if not todo_path.exists():
        sys.exit(f"Todo file not found: {todo_path}")

    try:
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

        if args.dry_run_prompt:
            tasks = load_tasks(todo_path, skip_sections=args.skip_section)
            if not tasks:
                log("Dry-run-prompt: no pending tasks.")
                return
            print(build_prompt(tasks[0], prompt_template))
            return

        if args.task:
            provider, idx = pick_next_provider(providers, state, 0)
            if provider is None:
                wait_s = seconds_until_next_available(providers, state)
                sys.exit(f"All providers exhausted. Next available in {wait_s}s.")
            prompt = build_prompt(args.task, prompt_template)
            task_timeout = get_task_timeout(args.task, subprocess_timeout, timeout_overrides)
            log(f"Running ad-hoc task via provider '{provider.name}': {args.task}", color="bold_green")
            exit_code, output, rate_limited = provider.run(prompt, working_directory, task_timeout)
            diff_stat = git_run(["status", "--porcelain"], cwd=working_directory)
            stat_output = diff_stat.stdout.strip() if diff_stat.returncode == 0 else ""
            if rate_limited and not stat_output:
                provider.mark_exhausted(state, reason="rate_limited")
                sys.exit(f"Provider '{provider.name}' rate-limited; ad-hoc task not completed. Re-run to try the next provider.")
            provider.reset_rate_limit_count(state)
            verified, _ = run_verification(config)
            if exit_code == 0 and verified:
                log(f"Ad-hoc task finished successfully via '{provider.name}'.", color="bold_green")
                if config.get("auto_commit", False):
                    git_commit(config, args.task)
                return
            sys.exit(f"Ad-hoc task did not complete cleanly (exit {exit_code}, verified={verified}).")

        if args.summary:
            print_summary(state, todo_path)
            return

        if args.list_tasks is not None:
            n = max(1, int(args.list_tasks))
            pending = load_tasks(todo_path, skip_sections=args.skip_section)
            if not pending:
                log("No pending tasks.")
                return
            cursor = 0
            limit = min(n, len(pending))
            log(f"Next {limit} pending tasks:", color="cyan")
            for i, task_preview in enumerate(pending[:limit], start=1):
                provider, pidx = pick_next_provider(providers, state, cursor)
                provider_name = provider.name if provider else "<all exhausted>"
                log(f"  {i}. [{provider_name}] {task_preview}")
                if provider is not None:
                    cursor = (pidx + 1) % len(providers)
            return

        if args.skip_section:
            log(f"Skipping sections: {', '.join(args.skip_section)}", color="yellow")

        resume_skip_tasks = set()
        if args.resume_from:
            pending = load_tasks(todo_path, skip_sections=args.skip_section)
            match_idx = next((i for i, t in enumerate(pending) if args.resume_from.lower() in t.lower()), None)
            if match_idx is None:
                sys.exit(f"--resume-from: no pending task matches '{args.resume_from}'.")
            resume_skip_tasks = set(pending[:match_idx])
            log(f"Resuming from task matching '{args.resume_from}' -- skipping {len(resume_skip_tasks)} "
                "earlier pending task(s) for this run (Todo.md itself is unchanged).", color="yellow")

        provider_idx = 0  # round-robin cursor across tasks
        skipped_tasks = set()
        concurrency = max(1, args.concurrency)

        while True:
            if _get_control_state("quit_requested", False):
                log("Graceful quit requested. Exiting after current boundary.", color="yellow")
                break

            all_pending_tasks = load_tasks(todo_path, skip_sections=args.skip_section)
            refresh_dashboard_tasks_from_todo(todo_path)
            # resume_skip_tasks is excluded for the lifetime of this run (never
            # re-included when skipped_tasks resets below) -- those tasks are
            # simply out of scope for this invocation, not "retry later".
            resumable_pending = [t for t in all_pending_tasks if t not in resume_skip_tasks]
            tasks = [t for t in resumable_pending if t not in skipped_tasks] if skipped_tasks else resumable_pending
            if not tasks and resumable_pending:
                skipped_tasks.clear()
                tasks = resumable_pending
            if not tasks:
                print_progress(todo_path, state, skip_sections=args.skip_section)
                log("All tasks completed. Exiting.", color="bold_green")
                print_run_report_card(state, todo_path)
                notify("All tasks completed", "All tasks in Todo.md are done")
                _play_audio_cue(config, "complete")
                break

            # --- Parallel batch: run [parallel]-tagged tasks concurrently ---
            if concurrency > 1:
                parallel_tasks = [t for t in tasks if _is_parallel_task(t)]
                if parallel_tasks:
                    from concurrent.futures import ThreadPoolExecutor, as_completed
                    batch = parallel_tasks[:concurrency]
                    log(f"Running {len(batch)} parallel tasks (concurrency={concurrency})...", color="bold_cyan")
                    log_json("parallel_batch_start", count=len(batch), concurrency=concurrency)

                    with ThreadPoolExecutor(max_workers=concurrency) as executor:
                        futures = {
                            executor.submit(
                                _run_single_task_for_parallel,
                                t, providers, state, config, working_directory,
                                prompt_template, subprocess_timeout, timeout_overrides, stall_timeout,
                            ): t
                            for t in batch
                        }
                        for future in as_completed(futures):
                            task_text = futures[future]
                            try:
                                _, success = future.result()
                                if not success:
                                    on_failure = config.get("on_failure", "skip")
                                    if on_failure == "skip":
                                        skipped_tasks.add(task_text)
                                    elif on_failure == "defer":
                                        defer_task(todo_path, task_text)
                            except Exception as e:
                                log(f"[parallel] Exception on task: {e}", color="bold_red")
                                skipped_tasks.add(task_text)

                    duration = time.time() - time.time()  # batch timing logged via individual tasks
                    log(f"Parallel batch done.", color="bold_green")
                    time.sleep(delay)
                    continue  # re-read Todo.md for next iteration

            # tasks_per_batch (default 1, hard-capped at MAX_TASKS_PER_BATCH) bundles
            # up to N pending tasks into a single agent invocation and a single
            # verify_commands run, amortizing per-task build/test overhead across
            # them. Completion is all-or-nothing: the batch shares one exit code,
            # one git-diff check, and one verify_commands result, so there's no
            # reliable way to attribute a mixed outcome to individual tasks --
            # either every task in the batch is marked complete, or none are (same
            # retry/defer/skip path a single failed task already takes).
            batch_size = min(max(1, int(config.get("tasks_per_batch", 1))), MAX_TASKS_PER_BATCH)
            batch_tasks = tasks[:batch_size]
            if len(batch_tasks) == 1:
                task = batch_tasks[0]
            else:
                task = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(batch_tasks))
            _set_control_state("current_task", task)
            _set_control_state("skip_current_task", False)
            task_start_time = time.time()
            log("=" * 60, color="dim")
            if len(batch_tasks) > 1:
                log(f"Starting batch of {len(batch_tasks)} tasks:\n{task}", color="bold_green")
            else:
                log(f"Starting task: {task}", color="bold_green")
            print_progress(todo_path, state, skip_sections=args.skip_section)
            log_json("task_start", task=task, provider_idx=provider_idx)
            update_dashboard_state(
                current_task=task,
                current_provider=None,
                provider_status=build_provider_status(providers, state),
                todo_path=todo_path,
            )

            prompt = build_prompt(task, prompt_template)
            task_timeout = get_task_timeout(task, subprocess_timeout, timeout_overrides)
            if task_timeout != subprocess_timeout:
                log(f"Task timeout override: {task_timeout}s (global: {subprocess_timeout}s)", color="magenta")
            task_done = False
            # Set once a verify_commands failure is seen for this task, then
            # carried into every subsequent attempt's prompt (same provider or
            # after rotating to the next one) until either it passes or the
            # task is deferred/skipped -- see build_retry_prompt().
            verify_failure_context = None
            _run_progress["completed"] = count_completed_tasks(todo_path, skip_sections=args.skip_section)
            _run_progress["total"] = count_total_tasks(todo_path, skip_sections=args.skip_section)

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
                    todo_path=todo_path,
                )

                attempt_success = False
                last_exit_code = None
                last_verified = None
                last_error_summary = None
                last_provider_name = provider.name
                for attempt in range(1, max_retries_per_provider + 1):
                    if _get_control_state("skip_current_task", False):
                        log("Current task was skipped by interactive command; leaving it unchecked.", color="yellow")
                        skipped_tasks.update(batch_tasks)
                        mark_dashboard_tasks_skipped(batch_tasks, provider=provider.name)
                        task_done = True
                        _set_control_state("skip_current_task", False)
                        break

                    log(f"[{provider.name}] attempt {attempt}/{max_retries_per_provider}", color="dim")
                    mark_dashboard_tasks_running(batch_tasks, provider.name, attempt)
                    attempt_prompt = (
                        build_retry_prompt(prompt, verify_failure_context)
                        if verify_failure_context else prompt
                    )
                    exit_code, output, rate_limited = provider.run(attempt_prompt, working_directory, task_timeout)
                    last_exit_code = exit_code
                    last_provider_name = provider.name

                    # rate_limited is just a substring match over the CLI's combined
                    # output -- in this repo specifically, task text and generated code
                    # routinely contain "rate limit", "429", "quota" etc. as *domain
                    # vocabulary*, not as a real rate-limit error. Confirm it against
                    # the working tree before trusting it: a real rate-limit hit means
                    # the agent didn't get to do anything, so if files actually changed,
                    # this was a false positive on a genuine completion, not a real
                    # exhaustion event.
                    # `git diff --stat` only sees changes to already-tracked files --
                    # it's blind to brand-new files, which is exactly what a task like
                    # "create X" produces. `git status --porcelain` catches new/modified/
                    # deleted/untracked alike, so it's the only reliable "did anything
                    # actually happen" signal here.
                    diff_stat = git_run(["status", "--porcelain"], cwd=working_directory)
                    stat_output = diff_stat.stdout.strip() if diff_stat.returncode == 0 else ""
                    rate_limited = rate_limited and not stat_output

                    exit_color = "yellow" if rate_limited else ("green" if exit_code == 0 else "red")
                    log(f"[{provider.name}] exit code {exit_code}"
                        + (" (looked rate-limited)" if rate_limited else ""), color=exit_color)

                    if rate_limited:
                        provider.mark_exhausted(state, reason="rate_limited")
                        log_json("provider_exhausted", provider=provider.name, reason="rate_limited")
                        log(f"Providers: {print_provider_status(providers, state)}", color="blue")
                        break  # stop retrying this provider, rotate to next
                    else:
                        provider.reset_rate_limit_count(state)

                    if exit_code == 124:
                        # Timed out -- we already know it didn't finish, so there's
                        # nothing meaningful to confirm. Treat it like any other
                        # failed attempt instead of asking "mark complete?".
                        last_error_summary = f"timed out after {task_timeout if task_timeout is not None else 'configured limit'}"
                        log(f"[{provider.name}] timed out before finishing -- treating as a failed attempt.", color="bold_red")
                        continue

                    verified, verify_output = run_verification(config)
                    last_verified = verified
                    # Carried into attempt_prompt on the next loop iteration (same
                    # provider, or after rotating to the next one) so the agent
                    # actually sees what broke instead of blindly repeating this
                    # attempt. Cleared once verification passes.
                    verify_failure_context = verify_output if not verified else None
                    if not verified and verify_output:
                        last_error_summary = verify_output.splitlines()[0][:240]

                    # exit_code == 0 alone isn't proof a task actually did anything --
                    # a real incident: kilo reported success on a task and had made
                    # zero edits. Treat a "success" with no changes as suspicious rather
                    # than trusting it at face value, in both confirmation modes.
                    suspicious = exit_code == 0 and diff_stat.returncode == 0 and not stat_output
                    if suspicious:
                        last_error_summary = "suspicious completion: exit 0 with no file changes"
                        log(f"[{provider.name}] SUSPICIOUS: exit code 0 but no files changed -- "
                            "this looks like a false success, not a real completion.", color="bold_red")
                        log_json("suspicious_completion", provider=provider.name, task=task)

                    if require_confirmation:
                        notify("Task needs confirmation", f"Task: {task}\nProvider: {provider.name}")
                        if stat_output:
                            log("Working tree changes (git status --porcelain):\n" + stat_output, color="yellow")
                        # verify_commands failing was previously silent here --
                        # a human could hit 'y' out of habit past the log output
                        # and mark a task complete despite failing tests, even
                        # though the unattended path below correctly blocks on
                        # this. Surface it in the prompt itself, same as the
                        # existing suspicious-completion warning.
                        if not verified:
                            prompt_label = "mark complete despite VERIFICATION FAILURE?"
                        elif suspicious:
                            prompt_label = "mark complete despite NO changes detected?"
                        else:
                            prompt_label = "mark complete?"
                        answer = input(
                            style(f"\nTask '{task}' via '{provider.name}' — {prompt_label} "
                                  "(y/n/retry/skip-provider/skip-task): ",
                                  "bold_red" if (suspicious or not verified) else "bold_cyan")
                        ).strip().lower()
                        if answer == "y":
                            attempt_success = True
                            break
                        elif answer == "retry":
                            continue
                        elif answer == "skip-provider":
                            provider.mark_exhausted(state, reason="skip")
                            log(f"Providers: {print_provider_status(providers, state)}", color="blue")
                            break
                        elif answer == "skip-task":
                            skipped_tasks.update(batch_tasks)
                            mark_dashboard_tasks_skipped(batch_tasks, provider=provider.name)
                            log(f"Task left unchecked and skipped for this cycle: {task}", color="yellow")
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
                    provider.reset_rate_limit_count(state)
                    for t in batch_tasks:
                        mark_complete(todo_path, t)
                    skipped_tasks.difference_update(batch_tasks)
                    # duration is for the whole batch -- split evenly so the rolling
                    # window stays one sample per completed task, keeping ETA/avg/
                    # longest/shortest meaningful regardless of batch size.
                    duration = (time.time() - task_start_time) / len(batch_tasks)
                    mark_dashboard_tasks_finished(
                        batch_tasks,
                        status="complete",
                        provider=provider.name,
                        duration_seconds=duration,
                        exit_code=0,
                        verification_passed=True,
                        error_summary=None,
                    )
                    durations = state.get("completed_task_durations", [])
                    durations.extend([duration] * len(batch_tasks))
                    state["completed_task_durations"] = durations[-200:]
                    save_state(state)
                    git_commit(config, task)
                    run_provider_stats(provider, working_directory, task)
                    log(f"Task marked complete: {task} (provider: {provider.name})", color="bold_green")
                    if _interactive_options.get("flair_mode", False):
                        done_count = count_completed_tasks(todo_path, skip_sections=args.skip_section)
                        if done_count > 0 and done_count % 10 == 0:
                            log(f"Milestone reached: {done_count} tasks completed.", color="bold_cyan")
                            notify("Milestone reached", f"{done_count} tasks completed")
                            _play_audio_cue(config, "complete")
                    print_progress(todo_path, state, skip_sections=args.skip_section)
                    log_json("task_complete", task=task, provider=provider.name)
                    update_dashboard_state(
                        current_task=None,
                        current_provider=None,
                        provider_status=build_provider_status(providers, state),
                        todo_path=todo_path,
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
                    per_task_duration = (time.time() - task_start_time) / max(1, len(batch_tasks))
                    mark_dashboard_tasks_finished(
                        batch_tasks,
                        status="failed",
                        provider=last_provider_name,
                        duration_seconds=per_task_duration,
                        exit_code=last_exit_code,
                        verification_passed=last_verified,
                        error_summary=last_error_summary,
                    )
                    log(f"Task NOT completed: {task}", color="bold_red")
                    log_json("task_failed", task=task, provider=provider.name)
                    update_dashboard_state(
                        current_task=None,
                        current_provider=None,
                        provider_status=build_provider_status(providers, state),
                        todo_path=todo_path,
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
                    # Configurable failure handling via on_failure setting
                    on_failure = config.get("on_failure", "skip")
                    if on_failure == "stop":
                        log("Stopping (on_failure=stop).", color="bold_red")
                        return
                    elif on_failure == "defer":
                        for t in batch_tasks:
                            defer_task(todo_path, t)
                        mark_dashboard_tasks_skipped(batch_tasks, provider=provider.name)
                        log(f"Task deferred to end of file: {task}", color="yellow")
                    else:  # "skip" (default)
                        skipped_tasks.update(batch_tasks)
                        mark_dashboard_tasks_skipped(batch_tasks, provider=provider.name)
                    task_done = True  # move on to next task in Todo.md
                else:
                    # Provider just got marked exhausted -> loop again to pick the next one immediately
                    log(f"Rotating away from exhausted provider '{provider.name}'...", color="yellow")
                    log_json("provider_exhausted", provider=provider.name)
                    provider_idx = (idx + 1) % len(providers)
                    update_dashboard_state(
                        provider_status=build_provider_status(providers, state),
                        todo_path=todo_path,
                    )
                    log(f"Providers: {print_provider_status(providers, state)}", color="blue")
                    continue

            log(f"Waiting {delay} seconds before next task...", color="dim")
            while _get_control_state("pause_after_task", False) and not _get_control_state("quit_requested", False):
                log("Paused (interactive command). Type 'r' then Enter to resume.", color="yellow")
                time.sleep(2)
            time.sleep(delay)

            if args.once:
                log("--once flag set. Exiting after one task.", color="dim")
                break
    finally:
        _shutdown_dashboard_server()
        _remove_pid_file()


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
