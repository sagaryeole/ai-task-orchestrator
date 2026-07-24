#!/bin/bash
# Supervisor for orchestrator.py: restarts it automatically if it crashes,
# but respects an intentional stop and does NOT restart in that case.
# orchestrator.py always writes progress (Todo.md checkboxes, state.json)
# immediately as it happens, so a restart just resumes from wherever the
# last run left off -- no separate "resume" logic needed.
#
# Usage: ./run_forever.sh [any orchestrator.py args, e.g. --config path.json]

cd "$(dirname "$0")"

while true; do
    python3 orchestrator.py "$@"
    code=$?

    if [ "$code" -eq 0 ]; then
        echo "[supervisor] Orchestrator finished normally (all tasks done, or stopped on purpose). Not restarting."
        break
    elif [ "$code" -eq 130 ]; then
        echo "[supervisor] Interrupted by Ctrl+C. Not restarting."
        break
    else
        echo "[supervisor] Orchestrator exited unexpectedly (code $code). Restarting in 10s..."
        sleep 10
    fi
done
