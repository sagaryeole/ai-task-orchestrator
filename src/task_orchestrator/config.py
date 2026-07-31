"""Configuration loading, validation, and linting for the orchestrator."""

import json
import os
import re
import shlex
import sys
from pathlib import Path

from .git import DEFAULT_CONFIG_FILENAME

CONFIG_PATH = Path(DEFAULT_CONFIG_FILENAME)
MAX_TASKS_PER_BATCH = 5  # hard cap -- one agent invocation covering too many
# tasks at once makes the all-or-nothing verify/commit gate too coarse (a
# single bad task in a big batch discards everything else alongside it).

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
        "message": (
            "GitHub Copilot CLI prompts for tool approval by default; "
            "use --allow-all-tools (or --yolo) for unattended runs."
        ),
    },
}

_ENV_VAR_PATTERN = re.compile(r"\$\{(\w+)\}|\$(\w+)")

GLOBAL_CONFIG_PATH = Path.home() / ".task-orchestrator" / "config.json"


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


def lint_config(config):
    """Warn about common config pitfalls that would silently break unattended runs."""
    from .orchestrator import log

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
    from .orchestrator import log

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
