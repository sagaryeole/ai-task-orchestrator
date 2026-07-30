"""Backward-compatible shim for the package orchestrator module.

Inserts ``src/`` onto ``sys.path`` (when executed directly), then imports
:mod:`task_orchestrator.orchestrator` and replaces this module in
:data:`sys.modules` so that

* ``python orchestrator.py`` and the installed ``task-orchestrator`` CLI run
  identical code, and
* ``from task_orchestrator.runner import X`` continues to work for every
  name that was ever exported from here.

The actual implementations live in :mod:`task_orchestrator.orchestrator`,
:mod:`task_orchestrator.provider`, :mod:`task_orchestrator.config`,
:mod:`task_orchestrator.git`, :mod:`task_orchestrator.dashboard`, and
:mod:`task_orchestrator.notify`.
"""

from __future__ import annotations

from . import (
    dashboard,  # noqa: F401
    git,  # noqa: F401
    notify,  # noqa: F401
)

# Re-export public names from the new focused modules so existing
# ``from task_orchestrator.runner import X`` imports keep working.
from .orchestrator import *  # noqa: F401,F403


def __getattr__(name: str) -> object:
    """Forward private/underscore-prefixed lookups to the submodules."""
    from importlib import import_module
    for mod_name in ("orchestrator", "git", "dashboard", "notify"):
        try:
            mod = import_module(f".{mod_name}", __package__ or __name__)
        except ImportError:
            continue
        if hasattr(mod, name):
            return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
