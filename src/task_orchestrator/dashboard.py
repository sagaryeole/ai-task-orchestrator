import datetime
import json
import re
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
    history_hint = "No history yet" if not history else ""
    history_items = []
    if history:
        for entry in history[-12:][::-1]:
            status = html_escape(entry.get("status", ""))
            provider = html_escape(entry.get("provider", "none"))
            task_name = html_escape(entry.get("task", ""))
            history_items.append(f"<li>{status} • {provider} • {task_name}</li>")
    history_items_html = "".join(history_items)

    parts = [
        '<!DOCTYPE html><html><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<title>Orchestrator Dashboard</title>',
        '<style>',
        ':root{--bg:#0f172a;--panel:#1e293b;--panel2:#0b1220;--text:#e2e8f0;--muted:#94a3b8;--ok:#22c55e;--warn:#f59e0b;--bad:#ef4444;--pending:#64748b;--skipped:#94a3b8;--border:#334155}',
        '*{box-sizing:border-box}',
        'body{margin:0;background:radial-gradient(circle at 20% 0%,#1e293b 0,#0f172a 55%);color:var(--text);font-family:Segoe UI,Arial,sans-serif}',
        '.wrap{padding:20px;max-width:1400px;margin:0 auto}',
        '.top{background:rgba(15,23,42,.7);border:1px solid var(--border);backdrop-filter:blur(6px);padding:16px;border-radius:14px;position:sticky;top:10px;z-index:10}',
        '.title{font-size:28px;font-weight:700;margin:0 0 6px 0}',
        '.meta{display:flex;flex-wrap:wrap;gap:14px;color:var(--muted);font-size:14px}',
        '.summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-top:14px}',
        '.metric{background:var(--panel2);border:1px solid var(--border);border-radius:10px;padding:10px}',
        '.metric .k{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}',
        '.metric .v{font-size:20px;font-weight:700;margin-top:3px}',
        '.progress{height:10px;border-radius:999px;background:#0b1220;border:1px solid var(--border);overflow:hidden;margin-top:12px}',
        '.progress > i{display:block;height:100%;background:linear-gradient(90deg,var(--ok),#14b8a6);width:0}',
        '.providers{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}',
        '.chip{font-size:12px;border-radius:999px;padding:4px 10px;border:1px solid var(--border);background:#0b1220;color:var(--muted)}',
        '.chip.available{color:var(--ok);border-color:#166534}',
        '.chip.cooldown{color:var(--warn);border-color:#92400e}',
        '.board{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;margin-top:18px}',
        '.task{border:1px solid var(--border);border-left-width:5px;border-radius:12px;padding:12px;background:linear-gradient(180deg,rgba(30,41,59,.95),rgba(15,23,42,.95));cursor:pointer;transition:transform .15s ease,border-color .2s ease,box-shadow .2s ease}',
        '.task:hover{transform:translateY(-2px);box-shadow:0 10px 22px rgba(0,0,0,.28)}',
        '.task .t{font-weight:650;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}',
        '.task .sub{margin-top:10px;font-size:12px;color:var(--muted);display:flex;justify-content:space-between;gap:8px;align-items:center}',
        '.provider-badge{display:inline-block;border:1px solid var(--border);border-radius:999px;padding:2px 8px;background:#0b1220;color:#cbd5e1;font-family:Consolas,Monaco,monospace}',
        '.attempt-badge{display:inline-block;margin-left:6px;border:1px solid #475569;border-radius:999px;padding:2px 7px;color:#cbd5e1;background:rgba(15,23,42,.65)}',
        '.task .status{font-size:12px;text-transform:uppercase;letter-spacing:.07em;font-weight:700;margin-top:8px}',
        '.task.pending{border-left-color:var(--pending);background:linear-gradient(180deg,rgba(100,116,139,.18),rgba(15,23,42,.95))}',
        '.task.pending .status{color:#cbd5e1}',
        '.task.running{border-left-color:var(--warn);background:linear-gradient(180deg,rgba(245,158,11,.20),rgba(15,23,42,.95));animation:pulse 1.5s infinite}',
        '.task.running .status{color:#fde68a}',
        '.task.complete{border-left-color:var(--ok);background:linear-gradient(180deg,rgba(34,197,94,.18),rgba(15,23,42,.95))}',
        '.task.complete .status{color:#86efac}',
        '.task.failed{border-left-color:var(--bad);background:linear-gradient(180deg,rgba(239,68,68,.20),rgba(15,23,42,.95))}',
        '.task.failed .status{color:#fecaca}',
        '.task.skipped{border-left-color:var(--skipped);background:linear-gradient(180deg,rgba(148,163,184,.16),rgba(15,23,42,.95))}',
        '.task.skipped .status{color:#cbd5e1}',
        '@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(245,158,11,.0)}50%{box-shadow:0 0 0 4px rgba(245,158,11,.18)}}',
        '.events{margin-top:14px;background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:12px}',
        '.events h3{margin:0 0 10px 0;font-size:16px}',
        '.events-list{margin:0;padding:0;list-style:none;font-size:13px;color:var(--muted)}',
        '.events-list li{padding:5px 0;border-top:1px solid rgba(148,163,184,.15)}',
        '.events-list li:first-child{border-top:none}',
        '.modal{position:fixed;inset:0;background:rgba(2,6,23,.72);display:none;align-items:center;justify-content:center;padding:18px;z-index:30}',
        '.modal.open{display:flex}',
        '.sheet{width:min(760px,96vw);max-height:86vh;overflow:auto;background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:16px}',
        '.sheet h3{margin:0 0 10px 0;font-size:20px}',
        '.sheet .row{display:grid;grid-template-columns:180px 1fr;gap:8px;font-size:14px;padding:5px 0;border-top:1px solid rgba(148,163,184,.17)}',
        '.sheet .row:first-of-type{border-top:none}',
        '.close{float:right;background:#0b1220;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:4px 9px;cursor:pointer}',
        '.hint{color:var(--muted);font-size:12px;margin-top:6px}',
        '@media (max-width:640px){.wrap{padding:12px}.top{top:6px;padding:12px}.title{font-size:22px}.sheet{padding:12px}.sheet .row{grid-template-columns:1fr;gap:3px}}',
        '</style></head><body><div class="wrap">',
        '<section class="top">',
        '<h1 class="title">Orchestrator Dashboard</h1>',
        '<div class="meta">',
        f'<span id="uptime">Uptime: {uptime}s</span>',
        f'<span id="current-task">Task: {current_task}</span>',
        f'<span id="current-provider">Provider: {current_provider}</span>',
        '</div>',
        '<div class="summary">',
        '<div class="metric"><div class="k">Total</div><div class="v" id="m-total">0</div></div>',
        '<div class="metric"><div class="k">Completed</div><div class="v" id="m-completed">0</div></div>',
        '<div class="metric"><div class="k">Failed</div><div class="v" id="m-failed">0</div></div>',
        '<div class="metric"><div class="k">Running</div><div class="v" id="m-running">0</div></div>',
        '<div class="metric"><div class="k">Pending</div><div class="v" id="m-pending">0</div></div>',
        '<div class="metric"><div class="k">ETA</div><div class="v" id="m-eta">--</div></div>',
        '</div>',
        '<div class="progress"><i id="progress-fill"></i></div>',
        '<div class="providers" id="providers-chips"></div>',
        '</section>',
        '<section class="board" id="task-board"></section>',
        '<section class="events"><h3>Recent Events</h3><ul class="events-list" id="events-list">{0}</ul></section>'.format(
            history_items_html if history_items_html else f"<li>{html_escape(history_hint or 'Loading...')}</li>"
        ),
        '<p class="hint">Click a card for full task details. Status colors: green complete, yellow running, red failed.</p>',
        '</div>',
        '<div class="modal" id="task-modal"><div class="sheet">',
        '<button class="close" id="modal-close">×</button>',
        '<h3 id="modal-title">Task</h3>',
        '<div class="row"><strong>Status</strong><span id="modal-status"></span></div>',
        '<div class="row"><strong>Provider</strong><span id="modal-provider"></span></div>',
        '<div class="row"><strong>Attempt</strong><span id="modal-attempt"></span></div>',
        '<div class="row"><strong>Started</strong><span id="modal-started"></span></div>',
        '<div class="row"><strong>Finished</strong><span id="modal-finished"></span></div>',
        '<div class="row"><strong>Duration</strong><span id="modal-duration"></span></div>',
        '<div class="row"><strong>Exit Code</strong><span id="modal-exit"></span></div>',
        '<div class="row"><strong>Verification</strong><span id="modal-verify"></span></div>',
        '<div class="row"><strong>Error</strong><span id="modal-error"></span></div>',
        '</div></div>',
        '<script>',
        'const board=document.getElementById("task-board");',
        'const modal=document.getElementById("task-modal");',
        'const closeBtn=document.getElementById("modal-close");',
        'let taskMap=new Map();',
        'let latestData={tasks:[]};',
        'function esc(s){return (s ?? "").toString();}',
        'function providerLabel(name){const n=esc(name)||"none"; const first=(n.trim()[0]||"?").toUpperCase(); return `[${first}] ${n}`;}',
        'function fmtDur(v){if(v===null||v===undefined||v==="") return "--"; const n=Math.max(0,Math.floor(Number(v))); const m=Math.floor(n/60); const s=n%60; return `${m}:${String(s).padStart(2,"0")}`;}',
        'function fmtEta(v){if(v===null||v===undefined) return "--"; const n=Math.max(0,Math.floor(Number(v))); if(n<60) return `${n}s`; const m=Math.floor(n/60); const s=n%60; if(m<60) return `${m}m ${s}s`; const h=Math.floor(m/60); const rm=m%60; return `${h}h ${rm}m`;}',
        'function openModal(task){',
        '  document.getElementById("modal-title").textContent=esc(task.title);',
        '  document.getElementById("modal-status").textContent=esc(task.status);',
        '  document.getElementById("modal-provider").textContent=esc(task.provider)||"none";',
        '  document.getElementById("modal-attempt").textContent=String(task.attempt||0);',
        '  document.getElementById("modal-started").textContent=esc(task.started_at)||"--";',
        '  document.getElementById("modal-finished").textContent=esc(task.finished_at)||"--";',
        '  document.getElementById("modal-duration").textContent=fmtDur(task.duration_seconds);',
        '  document.getElementById("modal-exit").textContent=(task.exit_code===null||task.exit_code===undefined)?"--":String(task.exit_code);',
        '  document.getElementById("modal-verify").textContent=(task.verification_passed===null||task.verification_passed===undefined)?"--":String(task.verification_passed);',
        '  document.getElementById("modal-error").textContent=esc(task.error_summary)||"--";',
        '  modal.classList.add("open");',
        '}',
        'closeBtn.addEventListener("click",()=>modal.classList.remove("open"));',
        'modal.addEventListener("click",(e)=>{if(e.target===modal){modal.classList.remove("open")}});',
        'window.addEventListener("keydown",(e)=>{if(e.key==="Escape") modal.classList.remove("open")});',
        'function taskHash(t){return JSON.stringify([t.status,t.provider,t.started_at,t.finished_at,t.duration_seconds,t.attempt,t.exit_code,t.verification_passed,t.error_summary]);}',
        'function renderTaskCard(task){',
        '  const el=document.createElement("article");',
        '  el.className=`task ${task.status||"pending"}`;',
        '  el.dataset.id=task.id;',
        '  el.dataset.startedAt=task.started_at||"";',
        '  el.dataset.status=task.status||"pending";',
        '  const attempts=(task.attempt&&task.attempt>1)?`<span class="attempt-badge">attempt ${task.attempt}</span>`:"";',
        '  el.innerHTML=`<div class="t" title="${esc(task.title)}">${esc(task.title)}</div><div class="status">${esc(task.status||"pending")}</div><div class="sub"><span><span class="provider-badge">${providerLabel(task.provider)}</span>${attempts}</span><span class="dur">${fmtDur(task.duration_seconds)}</span></div>`;',
        '  el.addEventListener("click",()=>openModal(task));',
        '  return el;',
        '}',
        'function syncTaskCards(tasks){',
        '  const nextIds=new Set();',
        '  for(const t of tasks){',
        '    const id=t.id||t.title;',
        '    nextIds.add(id);',
        '    const hash=taskHash(t);',
        '    const existing=taskMap.get(id);',
        '    if(!existing){',
        '      const card=renderTaskCard(t);',
        '      card.dataset.hash=hash;',
        '      board.appendChild(card);',
        '      taskMap.set(id, card);',
        '      continue;',
        '    }',
        '    if(existing.dataset.hash!==hash){',
        '      const card=renderTaskCard(t);',
        '      card.dataset.hash=hash;',
        '      existing.replaceWith(card);',
        '      taskMap.set(id, card);',
        '      if(t.status==="complete"||t.status==="failed"){',
        '        const glow=t.status==="failed"?"rgba(239,68,68,.35)":"rgba(34,197,94,.35)";',
        '        card.style.boxShadow=`0 0 0 3px ${glow}`;',
        '        setTimeout(()=>{card.style.boxShadow=""},900);',
        '      }',
        '    }',
        '  }',
        '  for(const [id,el] of taskMap.entries()){',
        '    if(!nextIds.has(id)){',
        '      el.remove();',
        '      taskMap.delete(id);',
        '    }',
        '  }',
        '}',
        'function tickRunningDurations(){',
        '  const now=Date.now();',
        '  board.querySelectorAll(".task.running").forEach((card)=>{',
        '    const started=card.dataset.startedAt;',
        '    if(!started) return;',
        '    const ms=Date.parse(started);',
        '    if(Number.isNaN(ms)) return;',
        '    const dur=Math.max(0,Math.floor((now-ms)/1000));',
        '    const durEl=card.querySelector(".dur");',
        '    if(durEl) durEl.textContent=fmtDur(dur);',
        '  });',
        '}',
        'function renderProviders(list){',
        '  const root=document.getElementById("providers-chips");',
        '  root.innerHTML="";',
        '  const now=Date.now()/1000;',
        '  for(const p of list||[]){',
        '    const el=document.createElement("span");',
        '    if(p.available){',
        '      el.className="chip available";',
        '      el.textContent=`available • ${p.name}`;',
        '    } else if(p.cooldown_until){',
        '      el.className="chip cooldown";',
        '      el.textContent=`cooldown • ${p.name} (${Math.max(0,Math.floor(p.cooldown_until-now))}s)`;',
        '    } else {',
        '      el.className="chip cooldown";',
        '      el.textContent=`cooldown • ${p.name}`;',
        '    }',
        '    root.appendChild(el);',
        '  }',
        '}',
        'function renderHistory(hist){',
        '  const root=document.getElementById("events-list");',
        '  root.innerHTML="";',
        '  if(!hist||hist.length===0){',
        '    const li=document.createElement("li");li.textContent="No history yet";root.appendChild(li);return;',
        '  }',
        '  for(const h of hist.slice(-12).reverse()){',
        '    const li=document.createElement("li");',
        '    li.textContent=`${h.status||""} • ${h.provider||"none"} • ${h.task||""}`;',
        '    root.appendChild(li);',
        '  }',
        '}',
        'function renderSummary(sum){',
        '  const total=Number(sum?.total||0);',
        '  const completed=Number(sum?.completed||0);',
        '  const failed=Number(sum?.failed||0);',
        '  const running=Number(sum?.running||0);',
        '  const pending=Number(sum?.pending||0);',
        '  document.getElementById("m-total").textContent=String(total);',
        '  document.getElementById("m-completed").textContent=String(completed);',
        '  document.getElementById("m-failed").textContent=String(failed);',
        '  document.getElementById("m-running").textContent=String(running);',
        '  document.getElementById("m-pending").textContent=String(pending);',
        '  document.getElementById("m-eta").textContent=fmtEta(sum?.estimated_remaining_seconds);',
        '  const pct=(total>0)?Math.floor((completed/total)*100):0;',
        '  document.getElementById("progress-fill").style.width=`${pct}%`;',
        '}',
        'async function refreshState(){',
        '  try {',
        '    const r=await fetch("/api/state", {cache:"no-store"});',
        '    if(!r.ok) return;',
        '    const d=await r.json();',
        '    latestData=d;',
        '    document.getElementById("uptime").textContent=`Uptime: ${d.uptime_seconds}s`;',
        '    document.getElementById("current-task").textContent=`Task: ${d.current_task||"idle"}`;',
        '    document.getElementById("current-provider").textContent=`Provider: ${d.current_provider||"none"}`;',
        '    renderSummary(d.run_summary||{});',
        '    renderProviders(d.providers||[]);',
        '    syncTaskCards(d.tasks||[]);',
        '    renderHistory(d.history||[]);',
        '  } catch (_) {}',
        '}',
        'setInterval(refreshState, 3000);',
        'setInterval(tickRunningDurations, 1000);',
        'refreshState();',
        '</script>',
        '</body></html>',
    ]
    return "".join(parts)
