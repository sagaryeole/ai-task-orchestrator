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


BASE_URL = os.environ.get("LM_STUDIO_URL", os.environ.get("PROVIDER_URL", "http://127.0.0.1:1234/v1"))
MODEL = os.environ.get("LM_STUDIO_MODEL", os.environ.get("PROVIDER_MODEL", "qwen/qwen3.5-9b"))
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
    for round_num in range(max_rounds):
        response = chat(messages)

        # Check if we're done (no file or command blocks)
        if "===FILE:" not in response and "===CMD:" not in response:
            messages.append({"role": "assistant", "content": response})
            messages.append({"role": "user", "content": "Done with changes. No more actions needed."})
            break

        messages.append({"role": "assistant", "content": response})

        # Parse and apply file blocks. Use .find() (returns -1 on miss) instead
        # of .index() (raises ValueError) so a malformed block from a small model
        # is skipped gracefully instead of crashing the whole provider.
        current = response
        while "===FILE:" in current:
            f_start = current.find("===FILE:")
            if f_start == -1:
                break
            f_start += len("===FILE:")
            # The path ends at the first newline (the marker is ===FILE: path===).
            newline_after_path = current.find("\n", f_start)
            if newline_after_path == -1:
                print("[lmstudio] Malformed ===FILE: block (no newline after path); skipping.", file=sys.stderr)
                break
            filepath = current[f_start:newline_after_path].strip()
            # Strip a trailing === from the path line if present (===FILE: path===).
            filepath = filepath.rstrip("=").strip()
            if not filepath:
                print("[lmstudio] Malformed ===FILE: block (empty path); skipping.", file=sys.stderr)
                break

            content_start = newline_after_path + 1
            content_end = current.find("===END===", content_start)
            if content_end == -1:
                print(f"[lmstudio] Malformed ===FILE: {filepath} block (missing ===END===); skipping.", file=sys.stderr)
                break
            file_content = current[content_start:content_end]
            # Strip a single trailing newline so we don't add a blank line the
            # model didn't intend (the ===END=== marker sits on its own line).
            if file_content.endswith("\n"):
                file_content = file_content[:-1]

            fpath = Path(WORKING_DIR) / filepath
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(file_content)
            print(f"[lmstudio] Wrote {filepath}", file=sys.stderr)

            current = current[content_end + len("===END==="):]

        # Parse and execute command blocks. Same .find()-based approach so a
        # missing ===CMD_END=== marker is skipped instead of crashing.
        while "===CMD:" in current:
            start = current.find("===CMD:")
            if start == -1:
                break
            start += len("===CMD:")
            end = current.find("===CMD_END===", start)
            if end == -1:
                print("[lmstudio] Malformed ===CMD: block (missing ===CMD_END===); skipping.", file=sys.stderr)
                break
            cmd = current[start:end].strip()
            if not cmd:
                print("[lmstudio] Empty ===CMD: block; skipping.", file=sys.stderr)
                current = current[end + len("===CMD_END==="):]
                continue

            print(f"[lmstudio] Running: {cmd}", file=sys.stderr)
            result = subprocess.run(cmd, shell=True, cwd=WORKING_DIR, capture_output=True, timeout=120)
            stdout = result.stdout.decode(errors="replace")
            stderr = result.stderr.decode(errors="replace")

            # Use the "user" role for command feedback instead of "tool" — the
            # OpenAI tool-message format requires a tool_call_id, which we don't
            # track. LM Studio tolerates a bare tool role, but strict OpenAI
            # APIs (and Ollama's compat layer) may reject it. "user" is safe
            # everywhere and keeps the conversation coherent.
            msg = f"Command executed: {cmd}\nstdout: {stdout}\nstderr: {stderr}\nreturn code: {result.returncode}"
            messages.append({"role": "user", "content": msg})
            current = current[end + len("===CMD_END==="):]

    else:
        # Loop exhausted without a clean "done" break — the model kept producing
        # blocks for all max_rounds. Don't treat that as a silent success.
        print(f"[lmstudio] Reached max_rounds ({max_rounds}) without finishing.", file=sys.stderr)

    print("Task completed.", file=sys.stderr)


if __name__ == "__main__":
    main()
