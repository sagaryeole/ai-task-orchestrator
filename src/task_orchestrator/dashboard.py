import datetime
import json
import re
import string
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# --------------------------------------------------------------------------
# Dashboard state
# --------------------------------------------------------------------------

_dashboard_state = {
    "current_task": None,
    "current_provider": None,
    "providers": {},
    "history": [],
    "tasks": [],
    "todo_file": None,
    "start_time": None,
}

_DASHBOARD_HISTORY_MAX = 50
_CHECKBOX_TASK_RE = re.compile(r"^\s*[-*]\s\[([ xX])\]\s+(.+)$")
TEMPLATE_PATH = Path(__file__).resolve().parent.parent.parent / "dashboard" / "templates" / "index.html"
_dashboard_server_ref = None


def _shutdown_dashboard_server():
    global _dashboard_server_ref
    if _dashboard_server_ref is None:
        return
    try:
        _dashboard_server_ref.shutdown()
        _dashboard_server_ref.server_close()
    except Exception:
        pass
    _dashboard_server_ref = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _iso_now():
    return datetime.datetime.now().isoformat()


def _load_all_todo_tasks(todo_path: Path):
    """Return all checkbox tasks in file order, including checked and unchecked."""
    tasks = []
    if not todo_path.exists():
        return tasks
    for line in todo_path.read_text().splitlines():
        m = _CHECKBOX_TASK_RE.match(line)
        if not m:
            continue
        tasks.append({"title": m.group(2), "checked": m.group(1).lower() == "x"})
    return tasks


def _dashboard_task_id(title, index_for_title):
    return f"{title}#{index_for_title}"


def refresh_dashboard_tasks_from_todo(todo_path):
    """Sync dashboard task cards with Todo.md while preserving runtime fields."""
    state = _dashboard_state
    todo_entries = _load_all_todo_tasks(todo_path)
    previous = {t.get("id"): t for t in state.get("tasks", []) if isinstance(t, dict)}
    counters = {}
    merged = []
    for entry in todo_entries:
        title = entry["title"]
        counters[title] = counters.get(title, 0) + 1
        task_id = _dashboard_task_id(title, counters[title])
        prev = previous.get(task_id, {})
        status = prev.get("status")
        if status is None:
            status = "complete" if entry["checked"] else "pending"
        elif entry["checked"] and status in ("pending", "running", "skipped", "failed"):
            status = "complete"
        elif (not entry["checked"]) and status == "complete":
            status = "pending"
        merged.append({
            "id": task_id,
            "title": title,
            "status": status,
            "provider": prev.get("provider"),
            "started_at": prev.get("started_at"),
            "finished_at": prev.get("finished_at"),
            "duration_seconds": prev.get("duration_seconds"),
            "attempt": int(prev.get("attempt", 0)),
            "error_summary": prev.get("error_summary"),
            "exit_code": prev.get("exit_code"),
            "verification_passed": prev.get("verification_passed"),
        })
    state["tasks"] = merged
    state["todo_file"] = str(todo_path)


def _find_next_task_card(title, preferred_statuses=None):
    preferred_statuses = preferred_statuses or ("pending", "skipped", "failed", "running")
    cards = _dashboard_state.get("tasks", [])
    for card in cards:
        if card.get("title") == title and card.get("status") in preferred_statuses:
            return card
    for card in cards:
        if card.get("title") == title:
            return card
    return None


def mark_dashboard_tasks_running(task_titles, provider, attempt):
    now_iso = _iso_now()
    for title in task_titles:
        card = _find_next_task_card(title, preferred_statuses=("pending", "skipped", "failed", "running"))
        if not card:
            continue
        card["status"] = "running"
        card["provider"] = provider
        if not card.get("started_at") or card.get("finished_at"):
            card["started_at"] = now_iso
        card["finished_at"] = None
        card["duration_seconds"] = None
        card["attempt"] = max(int(card.get("attempt", 0)), int(attempt))
        card["error_summary"] = None
        card["exit_code"] = None
        card["verification_passed"] = None


def mark_dashboard_tasks_skipped(task_titles, provider=None):
    now_iso = _iso_now()
    for title in task_titles:
        card = _find_next_task_card(title)
        if not card:
            continue
        card["status"] = "skipped"
        if provider:
            card["provider"] = provider
        card["finished_at"] = now_iso
        if card.get("started_at") and card.get("duration_seconds") is None:
            try:
                started = datetime.datetime.fromisoformat(card["started_at"])
                card["duration_seconds"] = max(0.0, (datetime.datetime.now() - started).total_seconds())
            except Exception:
                pass
        card["verification_passed"] = None


def mark_dashboard_tasks_finished(task_titles, status, provider, duration_seconds, exit_code=None, verification_passed=None, error_summary=None):
    now_dt = datetime.datetime.now()
    now_iso = now_dt.isoformat()
    for title in task_titles:
        card = _find_next_task_card(title)
        if not card:
            continue
        card["status"] = status
        card["provider"] = provider
        if not card.get("started_at"):
            try:
                card["started_at"] = (now_dt - datetime.timedelta(seconds=float(duration_seconds))).isoformat()
            except Exception:
                card["started_at"] = now_iso
        card["finished_at"] = now_iso
        card["duration_seconds"] = float(duration_seconds) if duration_seconds is not None else None
        card["exit_code"] = exit_code
        card["verification_passed"] = verification_passed
        card["error_summary"] = error_summary


def _build_run_summary(state):
    tasks = state.get("tasks", [])
    total = len(tasks)
    completed = sum(1 for t in tasks if t.get("status") == "complete")
    failed = sum(1 for t in tasks if t.get("status") == "failed")
    running = sum(1 for t in tasks if t.get("status") == "running")
    pending = sum(1 for t in tasks if t.get("status") == "pending")
    elapsed_seconds = 0.0
    if state.get("start_time"):
        elapsed_seconds = max(0.0, time.time() - state.get("start_time"))
    completed_durations = [t.get("duration_seconds") for t in tasks if t.get("status") == "complete" and isinstance(t.get("duration_seconds"), (int, float))]
    estimated_remaining_seconds = None
    if completed_durations and pending > 0:
        avg = sum(completed_durations) / len(completed_durations)
        estimated_remaining_seconds = max(0.0, avg * pending)
    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "running": running,
        "pending": pending,
        "elapsed_seconds": round(elapsed_seconds, 1),
        "estimated_remaining_seconds": round(estimated_remaining_seconds, 1) if estimated_remaining_seconds is not None else None,
    }


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

class DashboardHandler(BaseHTTPRequestHandler):
    """Serves JSON and HTML for the local dashboard."""

    def do_GET(self):
        if self.path == "/api/state":
            self._serve_json()
        elif self.path == "/health":
            self._serve_health()
        else:
            self._serve_html()

    def _serve_health(self):
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_json(self):
        state = _dashboard_state
        now = time.time()
        provider_list = []
        for name, info in state.get("providers", {}).items():
            provider_list.append({
                "name": name,
                "available": info.get("available", False),
                "cooldown_until": info.get("cooldown_until"),
            })
        payload = {
            "current_task": state.get("current_task"),
            "current_provider": state.get("current_provider"),
            "providers": provider_list,
            "history": state.get("history", []),
            "tasks": state.get("tasks", []),
            "run_summary": _build_run_summary(state),
            "uptime_seconds": round(now - state.get("start_time", now), 1) if state.get("start_time") else 0,
        }
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self):
        state = _dashboard_state
        body = _build_html(state).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # suppress request logging — dashboard traffic is noise in the orchestrator log


class DashboardServer(HTTPServer):
    """Minimal HTTP server for the local dashboard."""
    allow_reuse_address = True


def start_dashboard(port, retry_on_port_in_use=False, max_attempts=20):
    """Start the dashboard server on 127.0.0.1:{port} in a background thread.
    Returns the server instance, or None if disabled/unavailable."""
    if not isinstance(port, int) or port <= 0:
        return None

    from .runner import log
    attempts = max_attempts if retry_on_port_in_use else 1
    active_port = port
    for _ in range(attempts):
        try:
            server = DashboardServer(("127.0.0.1", active_port), DashboardHandler)
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            log(f"Dashboard available at http://127.0.0.1:{active_port}", color="green")
            return server
        except OSError as e:
            if not retry_on_port_in_use:
                log(f"Dashboard server could not start on port {active_port}: {e}", color="bold_red")
                return None
            active_port += 1
    log(f"Dashboard server could not start after trying ports {port}-{active_port - 1}.", color="bold_red")
    return None


def update_dashboard_state(current_task=None, current_provider=None, provider_status=None, history_entry=None, todo_path=None):
    """Update the shared dashboard state. Called from the orchestrator main loop."""
    state = _dashboard_state
    if current_task is not None:
        state["current_task"] = current_task
    if current_provider is not None:
        state["current_provider"] = current_provider
    if provider_status is not None:
        state["providers"] = provider_status
    if history_entry is not None:
        state.setdefault("history", [])
        state["history"].append(history_entry)
        if len(state["history"]) > _DASHBOARD_HISTORY_MAX:
            state["history"] = state["history"][-_DASHBOARD_HISTORY_MAX:]
    if todo_path is not None:
        refresh_dashboard_tasks_from_todo(Path(todo_path))
    if state.get("start_time") is None:
        state["start_time"] = time.time()


def build_provider_status(providers, state):
    """Build a provider-status dict for the dashboard."""
    now = time.time()
    status = {}
    for p in providers:
        until = state["provider_cooldowns"].get(p.name, 0)
        status[p.name] = {
            "available": until <= now,
            "cooldown_until": until if until > now else None,
        }
    return status


def html_escape(text):
    """Minimal HTML escaping for dashboard output."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_html(state):
    now = time.time()
    uptime = round(now - state.get("start_time", now), 1) if state.get("start_time") else 0
    current_task = html_escape(state.get("current_task") or "idle")
    current_provider = html_escape(state.get("current_provider") or "none")
    history = state.get("history", [])
    history_items = []
    if history:
        for entry in history[-12:][::-1]:
            status = html_escape(entry.get("status", ""))
            provider = html_escape(entry.get("provider", "none"))
            task_name = html_escape(entry.get("task", ""))
            history_items.append(f"<li>{status} \u2022 {provider} \u2022 {task_name}</li>")
    history_items_html = "".join(history_items) if history_items else "<li>No history yet</li>"
    template = string.Template(TEMPLATE_PATH.read_text(encoding="utf-8"))
    return template.safe_substitute(
        uptime=uptime,
        current_task=current_task,
        current_provider=current_provider,
        history_items=history_items_html,
    )
