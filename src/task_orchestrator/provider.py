"""Provider pool and executable resolution for task-orchestrator."""

import os
import sys
import time
import signal
import shlex
import shutil
import subprocess
import threading

from . import orchestrator
from .git import _git_dirty_count
from .notify import notify, style

_PYTHON_ALIASES = {"python": "python3", "python3": "python"}


def _resolve_executable(executable, env=None):
    """Resolve an executable path for subprocess launches.

    On Windows, CLIs often exist as .cmd/.bat/.exe shims, and PowerShell may
    expose script wrappers that are not directly discoverable by bare command
    name from subprocess without shell mediation.

    'python'/'python3' get an extra fallback: whichever name isn't on PATH
    falls back to the other, and finally to sys.executable (this process's
    own interpreter, which always exists).
    """
    if not executable:
        return executable

    env = env or os.environ

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
    string to whichever interpreter _resolve_executable finds available."""
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


def _progress_bar(completed, total, width=10):
    if total <= 0:
        return "[----------]"
    fill = int(round((completed / total) * width))
    fill = max(0, min(width, fill))
    return "[" + ("#" * fill) + ("-" * (width - fill)) + "]"


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


STALL_CPU_THRESHOLD = 12.0


class Provider:
    """
    A provider is any way of launching a coding agent: a CLI command with its
    own env vars (API key, model name, base URL). Examples:

      - Anthropic via Claude Code CLI
      - OpenRouter free model via Kilo Code CLI (different model flag)
      - Nvidia NIM endpoint via Kilo Code CLI (OpenAI-compatible base URL)
      - Local LM Studio server (OpenAI-compatible base URL, no API key)
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
            orchestrator.log(f"Provider '{self.name}' marked exhausted. Cooling down for {backoff}s (consecutive rate-limit hits: {count}).", color="yellow")
        else:
            if self.name in counts:
                counts[self.name] = 0
            state["provider_cooldowns"][self.name] = time.time() + self.cooldown_seconds
            orchestrator.log(f"Provider '{self.name}' marked exhausted. Cooling down for {self.cooldown_seconds}s.", color="yellow")
        orchestrator.save_state(state)

    def reset_rate_limit_count(self, state):
        counts = state.setdefault("provider_rate_limit_counts", {})
        if self.name in counts and counts[self.name] != 0:
            counts[self.name] = 0
            orchestrator.save_state(state)
            orchestrator.log(f"Provider '{self.name}' rate-limit counter reset (provider responded without rate-limiting).", color="dim")

    def run(self, prompt: str, working_directory: str, task_timeout=None):
        """Run this provider's command with the prompt on stdin, unless the
        command contains a literal ``{{TASK}}`` token -- some agent CLIs take
        the prompt as an argv element rather than reading stdin, so that token
        is substituted with the full prompt as a single argument instead, and
        stdin is left empty.
        Returns (exit_code, combined_output, looked_rate_limited: bool)."""
        env = os.environ.copy()
        env.update(self.env)
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
            orchestrator.log(f"Provider '{self.name}' command not found: {e}", color="bold_red")
            return 127, str(e), False

        orchestrator._current_process = process
        try:
            return self._wait_for_result(process, stdin_input, working_directory, task_timeout)
        finally:
            orchestrator._current_process = None

    def _wait_for_result(self, process, prompt, working_directory, task_timeout=None):
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
        heartbeat_interval = 3
        log_heartbeat_interval = 30
        last_heartbeat_check = 0
        last_logged_heartbeat = start
        cpu_pct = None
        dirty_count = None
        last_dirty_count = None
        last_activity_time = start
        stalled = False
        effective_timeout = task_timeout if task_timeout is not None else self.subprocess_timeout
        while comm_thread.is_alive():
            elapsed = time.time() - start
            if effective_timeout is not None and elapsed >= effective_timeout:
                break

            now = time.time()
            if now - last_heartbeat_check >= heartbeat_interval:
                cpu_pct = _process_group_cpu_percent(process.pid)
                dirty_count = _git_dirty_count(working_directory)
                had_cpu = cpu_pct is not None and cpu_pct > STALL_CPU_THRESHOLD
                if os.name == "nt" and cpu_pct is None:
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
                if orchestrator._interactive_options.get("ascii_progress", False) and orchestrator._run_progress.get("total", 0) > 0:
                    stats.append(_progress_bar(orchestrator._run_progress.get("completed", 0), orchestrator._run_progress.get("total", 0)))
                stats_str = (" " + " ".join(stats)) if stats else ""
                label = "working"
                if orchestrator._interactive_options.get("flair_mode", False):
                    label = flavor[frame % len(flavor)]
                    if elapsed >= 900:
                        label = "this one is taking a while"
                    elif elapsed >= 300:
                        label = "still going, might be worth a coffee"
                sys.stdout.write("\r" + style(f"[{self.name}] {label} {spinner[frame % len(spinner)]} ({int(elapsed)}s){stats_str} ", "cyan"))
                sys.stdout.flush()
                frame += 1

            if now - last_logged_heartbeat >= log_heartbeat_interval:
                orchestrator.log(f"[{self.name}] still working... {int(elapsed)}s elapsed"
                    + (f", cpu {cpu_pct:.0f}%" if cpu_pct is not None else "")
                    + (f", {dirty_count} files changed" if dirty_count is not None else "")
                    + f", idle {int(now - last_activity_time)}s",
                    color="dim")
                orchestrator.log_json("heartbeat", provider=self.name, elapsed=int(elapsed), cpu_pct=cpu_pct,
                          files_changed=dirty_count, idle_seconds=int(now - last_activity_time))
                last_logged_heartbeat = now

            time.sleep(0.2)

        if interactive:
            sys.stdout.write("\r" + " " * 60 + "\r")
            sys.stdout.flush()

        if comm_thread.is_alive():
            orchestrator._kill_process_tree(process)
            comm_thread.join(timeout=5)
            if stalled:
                orchestrator.log(f"Provider '{self.name}' looks stalled -- no CPU activity or file changes for "
                    f"{self.stall_timeout}s despite still running. Killing and treating as failed.", color="bold_red")
                notify("Provider stalled", f"{self.name} stalled after {self.stall_timeout}s of inactivity")
                orchestrator.log_json("provider_stalled", provider=self.name, stall_timeout=self.stall_timeout)
                return 124, f"Stalled: no activity for {self.stall_timeout}s", False
            label = f"{effective_timeout}s" if effective_timeout is not None else "the configured limit"
            orchestrator.log(f"Provider '{self.name}' timed out after {label}.", color="bold_red")
            return 124, f"Timed out after {label}", False

        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        output = (stdout or "") + "\n" + (stderr or "")
        orchestrator.log_file_only(f"[{self.name}] full output:\n{output}")

        looked_rate_limited = any(p in output.lower() for p in self.rate_limit_patterns)
        return process.returncode, output, looked_rate_limited


def load_providers(config, subprocess_timeout=None, stall_timeout=600):
    providers = [
        Provider(p, subprocess_timeout=subprocess_timeout, stall_timeout=stall_timeout)
        for p in config.get("providers", []) if p.get("enabled", True)
    ]
    if not providers:
        from .git import DEFAULT_CONFIG_FILENAME
        orchestrator.log(f"No enabled providers configured in {DEFAULT_CONFIG_FILENAME}.", color="bold_red")
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
    use_glyphs = orchestrator._interactive_options.get("provider_glyphs", False) and sys.stdout.isatty()
    for p in providers:
        until = state["provider_cooldowns"].get(p.name, 0)
        if until > now:
            label = f"{p.name}=cooldown({int(until - now)}s)"
            parts.append(("🌙 " + label) if use_glyphs else label)
        else:
            label = f"{p.name}=available"
            parts.append(("✅ " + label) if use_glyphs else label)
    return " | ".join(parts)
