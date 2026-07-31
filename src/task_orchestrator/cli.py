"""CLI entry point for task-orchestrator."""

from __future__ import annotations

import argparse

from . import __version__, runner


def main() -> None:
    """Parse CLI arguments and route to the shared runner implementation."""
    parser = argparse.ArgumentParser(
        description="Task Orchestrator — drives a coding-agent CLI through a task backlog."
    )
    parser.add_argument(
        "--version", "-V", action="version",
        version=f"task-orchestrator {__version__}"
    )
    parser.add_argument(
        "--config", default=runner.DEFAULT_CONFIG_FILENAME,
        help=f"Path to the config file (default: {runner.DEFAULT_CONFIG_FILENAME})"
    )
    subparsers = parser.add_subparsers(dest="command", title="subcommands")

    subparsers.add_parser("init", help="Scaffold a new project in the current directory")

    validate_parser = subparsers.add_parser(
        "validate",
        help="Check config/providers/git state without running anything",
    )
    validate_parser.add_argument(
        "--config", default=argparse.SUPPRESS,
        help=f"Path to the config file (default: {runner.DEFAULT_CONFIG_FILENAME})"
    )

    run_parser = subparsers.add_parser("run", help="Run the normal task loop")
    run_parser.add_argument(
        "--config", default=argparse.SUPPRESS,
        help=f"Path to the config file (default: {runner.DEFAULT_CONFIG_FILENAME})"
    )
    run_parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the next task and provider without executing anything"
    )
    run_parser.add_argument(
        "--dry-run-prompt", action="store_true",
        help="Print the exact prompt that would be sent for the next pending task, without executing anything."
    )
    run_parser.add_argument(
        "--once", action="store_true",
        help="Run only a single task and then exit"
    )
    run_parser.add_argument(
        "--json-logs", action="store_true",
        help="Append structured JSON log lines to logs/orchestrator.jsonl alongside normal logs"
    )
    run_parser.add_argument(
        "--skip-section", action="append", default=[],
        help="Exclude tasks under a Todo.md section (markdown header) from being processed. Repeatable."
    )
    run_parser.add_argument(
        "--summary", action="store_true",
        help="Print a summary of today's run statistics and exit"
    )
    run_parser.add_argument(
        "--list-tasks", "--peek", dest="list_tasks", nargs="?", const=10, type=int,
        help="Preview the next N pending tasks (default: 10) and which provider would run each."
    )
    run_parser.add_argument(
        "--concurrency", type=int, default=1,
        help="Run up to N tasks in parallel (only tasks tagged [parallel] are parallelized; others run sequentially)."
    )
    run_parser.add_argument(
        "--provider", default=None,
        help="Force a specific provider by name for this run, ignoring the "
             "others entirely (its own cooldown still applies)."
    )
    run_parser.add_argument(
        "--task", default=None,
        help="Run a single ad-hoc task immediately without reading or modifying Todo.md."
    )
    run_parser.add_argument(
        "--resume-from", default=None,
        help="Skip pending tasks in Todo.md until reaching the first one containing this text, "
             "then proceed normally from there for the rest of this run. Todo.md itself is not modified."
    )
    args, remaining = parser.parse_known_args()
    if args.command is None:
        run_args = run_parser.parse_args(remaining)
        for k, v in run_args.__dict__.items():
            setattr(args, k, v)
        args.command = "run"

    runner.main(args)


if __name__ == "__main__":
    main()
