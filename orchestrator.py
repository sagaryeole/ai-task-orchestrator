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

CONFIG_PATH = Path("config.json")
STATE_PATH = Path("state.json")
LOG_DIR = Path("logs")
TASK_REGEX = r"- \[ \] (.+)"
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
        errors.append("config.json must be a JSON object (dict).")
        _fail(errors)

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
        return json.loads(STATE_PATH.read_text())
    return {"provider_cooldowns": {}}  # provider_name -> unix timestamp when available again


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


# --------------------------------------------------------------------------
# Todo handling
# --------------------------------------------------------------------------

def load_tasks(todo_path: Path):
    return re.findall(TASK_REGEX, todo_path.read_text())


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
      "cooldown_seconds": 3600
    }
    """

    def __init__(self, cfg, subprocess_timeout=None, stall_timeout=600):
        self.name = cfg["name"]
        self.command = cfg["command"]
        self.env = cfg.get("env", {})
        self.rate_limit_patterns = [p.lower() for p in cfg.get("rate_limit_patterns", [])]
        self.cooldown_seconds = cfg.get("cooldown_seconds", 3600)
        self.subprocess_timeout = subprocess_timeout
        # Unlike subprocess_timeout (a hard wall-clock cap), stall_timeout is
        # activity-based: it only fires if there's been no CPU usage AND no
        # file changes for this long, so a genuinely big/slow task that's
        # still working is never killed, but a task that's alive yet
        # producing nothing (hung, or silently waiting on input that will
        # never come) gets caught and retried/deferred instead of blocking
        # the whole overnight run forever.
        self.stall_timeout = stall_timeout
        self.priority = int(cfg.get("priority", 0))

    def is_available(self, state):
        until = state["provider_cooldowns"].get(self.name, 0)
        return time.time() >= until

    def mark_exhausted(self, state):
        state["provider_cooldowns"][self.name] = time.time() + self.cooldown_seconds
        save_state(state)
        log(f"Provider '{self.name}' marked exhausted. Cooling down for {self.cooldown_seconds}s.", color="yellow")

    def run(self, prompt: str, working_directory: str):
        """Run this provider's command with the prompt on stdin.
        Returns (exit_code, combined_output, looked_rate_limited: bool)."""
        global _current_process
        env = os.environ.copy()
        env.update(self.env)
        cmd = shlex.split(self.command)
        try:
            # Run in its own process group (not just its own process) so that on
            # timeout -- or SIGINT -- we can kill any descendants it spawns too.
            # Some agent CLIs are launcher scripts that spawn the real worker as
            # a child of their own rather than exec'ing into it -- killing only
            # the direct child leaves that worker running, orphaned.
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
            return self._wait_for_result(process, prompt, working_directory)
        finally:
            _current_process = None

    def _wait_for_result(self, process, prompt, working_directory):
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
        while comm_thread.is_alive():
            elapsed = time.time() - start
            # subprocess_timeout of None means "no limit" -- just keep polling
            # and showing the spinner for as long as the task takes.
            if self.subprocess_timeout is not None and elapsed >= self.subprocess_timeout:
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
                log_json("provider_stalled", provider=self.name, stall_timeout=self.stall_timeout)
                return 124, f"Stalled: no activity for {self.stall_timeout}s", False
            log(f"Provider '{self.name}' timed out after {self.subprocess_timeout}s.", color="bold_red")
            return 124, f"Timed out after {self.subprocess_timeout}s", False

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
# Verification / git
# --------------------------------------------------------------------------

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
    args = parser.parse_args()

    config = load_config(Path(args.config))
    lint_config(config)
    global _json_log_enabled
    _json_log_enabled = args.json_logs or config.get("json_logs", False)

    state = load_state()
    subprocess_timeout = config.get("subprocess_timeout", 180)
    stall_timeout = config.get("stall_timeout_seconds", 600)
    providers = load_providers(config, subprocess_timeout=subprocess_timeout, stall_timeout=stall_timeout)
    log(f"Providers: {print_provider_status(providers, state)}", color="blue")

    todo_path = Path(config["todo_file"])
    prompt_template = Path(config.get("prompt_template", "prompts/task_prompt.txt"))
    delay = config.get("delay_seconds", 60)
    max_retries_per_provider = config.get("max_retries_per_provider", 1)
    require_confirmation = config.get("require_manual_confirmation", True)
    working_directory = config.get("working_directory", ".")

    if not todo_path.exists():
        sys.exit(f"Todo file not found: {todo_path}")

    if args.dry_run:
        tasks = load_tasks(todo_path)
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

    provider_idx = 0  # round-robin cursor across tasks

    while True:
        tasks = load_tasks(todo_path)
        if not tasks:
            log("All tasks completed. Exiting.", color="bold_green")
            break

        task = tasks[0]
        log("=" * 60, color="dim")
        log(f"Starting task: {task}", color="bold_green")
        log_json("task_start", task=task, provider_idx=provider_idx)

        prompt = build_prompt(task, prompt_template)
        task_done = False

        while not task_done:
            provider, idx = pick_next_provider(providers, state, provider_idx)

            if provider is None:
                wait_s = seconds_until_next_available(providers, state)
                log(f"All providers exhausted. Sleeping {wait_s}s until one frees up...", color="yellow")
                time.sleep(wait_s + 1)
                continue  # re-check availability

            provider_idx = idx  # remember where we are for next round
            log(f"Using provider: {provider.name}", color="cyan")
            log_json("provider_selected", provider=provider.name, index=idx)

            attempt_success = False
            for attempt in range(1, max_retries_per_provider + 1):
                log(f"[{provider.name}] attempt {attempt}/{max_retries_per_provider}", color="dim")
                exit_code, output, rate_limited = provider.run(prompt, working_directory)
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

                if require_confirmation:
                    diff_stat = subprocess.run(
                        ["git", "diff", "--stat"],
                        cwd=working_directory, capture_output=True, text=True, timeout=2,
                    )
                    if diff_stat.returncode == 0:
                        stat_output = diff_stat.stdout.strip()
                        if stat_output:
                            log("Working tree changes (git diff --stat):\n" + stat_output, color="yellow")
                    answer = input(
                        style(f"\nTask '{task}' via '{provider.name}' — mark complete? (y/n/retry/skip-provider/skip-task): ", "bold_cyan")
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
                    if exit_code == 0 and verified:
                        attempt_success = True
                        break
                    # non-rate-limit failure: retry same provider up to max_retries_per_provider

            if attempt_success:
                mark_complete(todo_path, task)
                git_commit(config, task)
                log(f"Task marked complete: {task} (provider: {provider.name})", color="bold_green")
                log_json("task_complete", task=task, provider=provider.name)
                task_done = True
                provider_idx = (idx + 1) % len(providers)  # rotate for load balancing
                log(f"Providers: {print_provider_status(providers, state)}", color="blue")
            elif provider.is_available(state):
                # Failed for a non-rate-limit reason and user didn't want a retry -> give up on task
                log(f"Task NOT completed: {task}", color="bold_red")
                log_json("task_failed", task=task, provider=provider.name)
                if not config.get("continue_on_failure", True):
                    log("Stopping (continue_on_failure=false).", color="bold_red")
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
