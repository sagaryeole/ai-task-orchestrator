"""Notification, audio cue, and startup banner helpers.

Extracted from ``runner.py`` to keep the main orchestration module focused on
the task loop.  ``runner.py`` re-imports everything from here so existing
``from task_orchestrator.runner import notify`` style imports keep working.
"""

import os
import subprocess
import sys

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


def _applescript_escape(text):
    """Escape a string for safe interpolation into a double-quoted AppleScript
    literal. Without this, a title/message containing a double quote (e.g. a
    task description quoting something) breaks out of the string and the rest
    is parsed as AppleScript -- task text isn't trusted input, so this isn't
    just a cosmetic crash risk."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def notify(title, message):
    """Fire-and-forget desktop notification. Uses osascript on macOS,
    notify-send on Linux when available, otherwise silently does nothing."""
    try:
        if sys.platform == "darwin":
            safe_message = _applescript_escape(message)
            safe_title = _applescript_escape(title)
            subprocess.run(
                ["osascript", "-e", f'display notification "{safe_message}" with title "{safe_title}"'],
                capture_output=True, timeout=5,
            )
        elif sys.platform.startswith("linux"):
            subprocess.run(
                ["notify-send", title, message],
                capture_output=True, timeout=5,
            )
    except Exception:
        pass


def _play_audio_cue(config, cue_name):
    """Optional audio cue; disabled by default via config."""
    if not config.get("audio_notifications", False):
        return
    try:
        if os.name == "nt":
            import winsound
            freq = 880 if cue_name == "complete" else 600
            winsound.Beep(freq, 180)
        elif sys.platform == "darwin":
            phrase = "Task complete" if cue_name == "complete" else "Action needed"
            subprocess.run(["say", phrase], capture_output=True, timeout=3)
        elif sys.platform.startswith("linux"):
            subprocess.run(["printf", "\a"], capture_output=True, timeout=3)
    except Exception:
        pass


def _print_startup_banner(providers, dashboard_url, require_confirmation, enabled=True):
    """Render a fun startup banner once per run in interactive terminals.

    Kept off for non-TTY output so tests, pipes, and log redirection stay clean.
    """
    if not enabled or not sys.stdout.isatty():
        return

    provider_names = ", ".join(p.name for p in providers[:4])
    if len(providers) > 4:
        provider_names += ", ..."
    run_mode = "manual confirmation" if require_confirmation else "unattended"
    dashboard = dashboard_url or "disabled"

    art = [
        " _____         _     ___           _               _             _",
        "|_   _|_ _ ___| | __/ _ \\ _ __ ___| |__   ___  ___| |_ _ __ __ _| |_ ___  _ __",
        "  | |/ _` / __| |/ / | | | '__/ __| '_ \\ / _ \\/ __| __| '__/ _` | __/ _ \\| '__|",
        "  | | (_| \\__ \\   <| |_| | | | (__| | | |  __/\\__ \\ |_| | | (_| | || (_) | |",
        "  |_|\\__,|___/_|\\_\\\\___/|_|  \\___|_| |_|\\___||___/\\__|_|  \\__,_|\\__\\___/|_|",
    ]

    print(style("\n" + "\n".join(art), "bold_cyan"))
    print(style("+--------------------------------------------------------------------------+", "cyan"))
    print(style(f"| Providers : {len(providers)} [{provider_names}]", "cyan"))
    print(style(f"| Mode      : {run_mode}", "cyan"))
    print(style(f"| Dashboard : {dashboard}", "cyan"))
    print(style("+--------------------------------------------------------------------------+\n", "cyan"))
