#!/usr/bin/env python3
"""Backward-compatible shim for the package runner module.

Inserts ``src/`` onto ``sys.path``, imports :mod:`task_orchestrator.runner`,
and replaces this module in :data:`sys.modules` so that
``python orchestrator.py`` and the installed ``task-orchestrator`` CLI run
identical code.  The self-replacement also means ``from orchestrator import X``
and ``unittest.mock.patch('orchestrator.X')`` work exactly as they did when
all code lived in this file.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent
_SRC_DIR = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

_runner = importlib.import_module("task_orchestrator.runner")
sys.modules[__name__] = _runner


if __name__ == "__main__":
    raise SystemExit(_runner.main())
