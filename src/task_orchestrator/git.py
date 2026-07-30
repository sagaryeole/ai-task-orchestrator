"""Git operations and Todo.md manipulation helpers.

Extracted from ``runner.py`` to keep the main orchestration module focused on
the task loop.  ``runner.py`` re-imports everything from here so existing
``from task_orchestrator.runner import git_run`` style imports keep working.
"""

import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # non-Windows
    msvcrt = None

DEFAULT_CONFIG_FILENAME = "task-orchestrator.config.json"
TASK_REGEX = r"- \[ \] (.+)"

_GIT_LOCK_PATTERNS = [
    "index.lock",
    "head.lock",
    "unable to create",
    "locked",
]


def _is_transient_git_error(result):
    """Return True if the git failure looks transient (e.g. lock contention)
    rather than a permanent user/configuration error."""
    if result.returncode == 0:
        return False
    combined = (result.stderr or "").lower() + (result.stdout or "").lower()
    return any(p in combined for p in _GIT_LOCK_PATTERNS)


def git_run(args, cwd=None, timeout=10):
    """Run a git command, retrying once on transient failures (e.g. index.lock
    contention). Returns the subprocess.CompletedProcess result."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=cwd, capture_output=True, text=True, errors="replace", timeout=timeout,
    )
    if _is_transient_git_error(result):
        time.sleep(0.5)
        result = subprocess.run(
            ["git"] + list(args),
            cwd=cwd, capture_output=True, text=True, errors="replace", timeout=timeout,
        )
    return result


def validate_git_working_tree(working_directory):
    """Fail fast if working_directory is not inside a git working tree."""
    result = git_run(["rev-parse", "--is-inside-work-tree"], cwd=working_directory)
    if result.returncode != 0 or result.stdout.strip().lower() != "true":
        sys.exit(
            f"Fatal: '{working_directory}' is not inside a git working tree. "
            f"Set a valid 'working_directory' in {DEFAULT_CONFIG_FILENAME} or run from inside a git repo."
        )


def _git_dirty_count(working_directory):
    """Count files with uncommitted changes. None if not a git repo / on failure."""
    try:
        result = git_run(["status", "--porcelain"], cwd=working_directory)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return len([line for line in result.stdout.splitlines() if line.strip()])


@contextmanager
def _todo_lock(todo_path: Path):
    """Advisory cross-process lock around Todo.md writes so two orchestrator
    instances pointed at the same file cannot interleave read-modify-write
    sequences and corrupt it."""
    lock_path = Path(str(todo_path) + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        elif msvcrt is not None:
            # msvcrt.locking() locks from the current file offset for n bytes.
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                os.write(fd, b"0")
            except OSError:
                pass
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif msvcrt is not None:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(fd)


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
    with _todo_lock(todo_path):
        text = todo_path.read_text()
        text = text.replace(f"- [ ] {task}", f"- [x] {task}", 1)
        todo_path.write_text(text)


def defer_task(todo_path: Path, task: str):
    """Move a task to the end of the file, still unchecked. Without this, a
    task that never succeeds (and is never explicitly marked complete) stays
    at index 0 forever -- load_tasks() always re-reads from the top, so it
    would be retried on every single loop iteration, permanently blocking
    every other task behind it."""
    with _todo_lock(todo_path):
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
