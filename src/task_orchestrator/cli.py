"""CLI entry point for task-orchestrator."""

from __future__ import annotations

import sys

from . import __version__
from . import runner


def main() -> None:
    """Route package CLI calls to the shared runner implementation."""
    argv = sys.argv[1:]
    if "--version" in argv or "-V" in argv:
        print(f"task-orchestrator {__version__}")
        return
    runner.main()


if __name__ == "__main__":
    main()