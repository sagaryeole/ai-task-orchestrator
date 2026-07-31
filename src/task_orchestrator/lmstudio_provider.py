#!/usr/bin/env python3
"""LM Studio provider for task-orchestrator.

Reads a task prompt from stdin, sends it to LM Studio's OpenAI-compatible
API, executes the model's commands, and exits 0 on success.

Usage in config:
  "command": "python src/task_orchestrator/lmstudio_provider.py",
  "env": { "LM_STUDIO_URL": "http://127.0.0.1:1234/v1", "LM_STUDIO_MODEL": "qwen/qwen3.5-9b" }

Prompt mode: stdin
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib import request as urlrequest


BASE_URL = os.environ.get("LM_STUDIO_URL", "http://127.0.0.1:1234/v1")
MODEL = os.environ.get("LM_STUDIO_MODEL", "qwen/qwen3.5-9b")
WORKING_DIR = os.environ.get("WORKING_DIR", ".")


def chat(messages: list[dict]) -> str:
    payload = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": 4096,
    }
    data = json.dumps(payload).encode()
    req = urlrequest.Request(
        f"{BASE_URL.rstrip('/')}/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlrequest.urlopen(req, timeout=300) as resp:
            body = json.loads(resp.read().decode())
    except Exception as e:
        print(f"[lmstudio] API error: {e}", file=sys.stderr)
        sys.exit(1)

    choices = body.get("choices", [])
    if not choices:
        print(f"[lmstudio] No choices in response: {body}", file=sys.stderr)
        sys.exit(1)
    return choices[0]["message"]["content"]


def main() -> None:
    prompt = sys.stdin.read()

    system_msg = (
        "You are a coding agent. You operate inside a git repository. "
        "When you need to create or modify files, output the changes using "
        "the following format so they can be parsed and applied:\n"
        "===FILE: path/to/file===\n"
        "file contents here\n"
        "===END===\n"
        "You can create/modify multiple files in one response. "
        "Only use this format. Do not include any other text outside of file blocks. "
        "If you need to run shell commands, output: "
        "===CMD: command here===\n"
        "===CMD_END===\n"
        "You can include multiple file and command blocks in a single response."
    )

    messages: list[dict] = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": prompt},
    ]

    max_rounds = 10
    for _ in range(max_rounds):
        response = chat(messages)

        # Check if we're done (no file or command blocks)
        if "===FILE:" not in response and "===CMD:" not in response:
            messages.append({"role": "assistant", "content": response})
            messages.append({"role": "user", "content": "Done with changes. No more actions needed."})
            break

        messages.append({"role": "assistant", "content": response})

        # Parse and apply file blocks
        current = response
        while "===FILE:" in current:
            f_start = current.index("===FILE:") + len("===FILE:")
            f_end = current.index("===", f_start)
            filepath = current[f_start:f_end].strip()
            content_start = current.index("\n", f_end) + 1
            content_end = current.index("===END===", content_start)
            file_content = current[content_start:content_end]

            fpath = Path(WORKING_DIR) / filepath
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(file_content)
            print(f"[lmstudio] Wrote {filepath}", file=sys.stderr)

            current = current[content_end + len("===END==="):]

        # Parse and execute command blocks
        while "===CMD:" in current:
            start = current.index("===CMD:") + len("===CMD:")
            end = current.index("===CMD_END===")
            cmd = current[start:end].strip()

            print(f"[lmstudio] Running: {cmd}", file=sys.stderr)
            result = subprocess.run(cmd, shell=True, cwd=WORKING_DIR, capture_output=True, timeout=120)
            stdout = result.stdout.decode(errors="replace")
            stderr = result.stderr.decode(errors="replace")

            messages.append({"role": "assistant", "content": current[:start - len("===CMD:")]})
            msg = f"Command executed.\nstdout: {stdout}\nstderr: {stderr}\nreturn code: {result.returncode}"
            messages.append({"role": "tool", "content": msg})
            current = current[end + len("===CMD_END==="):]

    print("Task completed.", file=sys.stderr)


if __name__ == "__main__":
    main()
