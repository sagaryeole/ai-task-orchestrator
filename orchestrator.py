#!/usr/bin/env python3
"""Backward-compatible shim for the package runner module."""

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