# Todo

Tasks for developing the orchestrator itself (`orchestrator.py`, `config.json`, prompts, docs).
Grouped by priority — work top section first.

## Dashboard V2 — Task Card UI

Replace the current plain-log dashboard (`_build_html` table view) with a modern card-based task board. Still stdlib-only (no React/Vue/npm build step) — all HTML/CSS/JS inlined or served as static strings from `_build_html()`.

### Backend — `/api/state` payload expansion

- [x] Add a `tasks` array to the `/api/state` JSON response. Each entry: `{ "title": str, "status": "pending"|"running"|"complete"|"failed"|"skipped", "provider": str|null, "started_at": iso|null, "finished_at": iso|null, "duration_seconds": float|null, "attempt": int, "error_summary": str|null, "exit_code": int|null, "verification_passed": bool|null }`
- [x] Populate `tasks` from the merged view of Todo.md (all `- [ ]` and `- [x]` lines) plus in-memory run history, so every task — pending, active, and finished — appears in the list
- [x] Include a top-level `run_summary` object: `{ "total": int, "completed": int, "failed": int, "running": int, "pending": int, "elapsed_seconds": float, "estimated_remaining_seconds": float|null }`

### Frontend — Card grid layout

- [x] Render each task as a visual card (CSS grid/flexbox, 2–4 columns depending on viewport width, wrapping responsively) instead of a flat `<table>` row
- [x] Card shows: task title (truncated to ~80 chars with `title` tooltip for full text), provider name/icon, elapsed or final duration (`mm:ss` format), and attempt count badge if > 1
- [x] Color-code cards by status: green (`#22c55e`) background tint for complete, amber/yellow (`#f59e0b`) for running (with a subtle pulse animation), red (`#ef4444`) for failed, neutral gray (`#e5e7eb`) for pending, and slate (`#94a3b8`) for skipped
- [x] Running card shows a live elapsed-time counter (JS `setInterval` ticking every second, driven by `started_at`)

### Frontend — Click-to-expand detail popup

- [x] Clicking a card opens a modal/overlay with full task details: full task text (untruncated), provider used, start/end timestamps, duration, exit code, verification result, and error summary (if failed)
- [x] Modal is closeable by clicking outside, pressing Escape, or clicking an × button
- [x] Modal content is populated from the existing `/api/state` JSON — no new endpoint needed

### Frontend — Header bar with run summary

- [x] Top-of-page summary bar showing: total tasks, completed count, failed count, running count, overall progress bar (percentage fill), wall-clock elapsed, and ETA
- [x] Provider status chips (available / cooldown with countdown timer) rendered inline in the header, replacing the current separate provider table

### Frontend — Auto-refresh and live updates

- [x] Poll `/api/state` every 3 seconds (reduced from current 5s) and diff-update only changed cards (avoid full DOM rebuild on each tick to prevent flicker)
- [x] Newly completed/failed cards get a brief highlight animation (CSS transition) so the user's eye is drawn to state changes

### Constraints

- No external JS/CSS frameworks — keep the entire dashboard as a single inlined HTML string returned by `_build_html()`, same as today, so there's zero build tooling and zero extra files to serve
- Keep the existing `/api/state` backward-compatible — new fields are additive; any external tool parsing the current shape must still work
- Dark-mode aware: use a dark background (`#0f172a` / `#1e293b` card) with light text, consistent with the logo/demo SVG branding

### Configuration Flexibility

- [~] ~~Support YAML and TOML config formats alongside JSON (auto-detect by extension), since many OSS users prefer YAML~~ (dropped: adds PyYAML dep; JSON is universal and stdlib-only)
- [~] ~~Add `--concurrency N` flag for embarrassingly-parallel task lists (independent tasks marked with a `[parallel]` tag can run N at a time on different providers)~~ (dropped: fundamentally changes execution model; out of v2 scope)

### Extensibility & Plugin System

- [~] ~~Define a simple plugin interface (`class BaseProvider`) with `run()`, `is_rate_limited()`, `get_stats()` methods so users can write custom providers as Python classes (not just CLI wrappers)~~ (dropped: CLI wrapping already covers any provider; over-engineering)
- [~] ~~Support loading provider plugins from `~/.task-orchestrator/plugins/` or via entry_points (`task_orchestrator.providers` group)~~ (dropped: same as above)
- [~] ~~Add webhook/callback hooks (`on_task_start`, `on_task_complete`, `on_task_fail`, `on_provider_exhausted`) configurable as shell commands or HTTP POST URLs — enables Slack/Discord/Teams notifications, CI integrations~~ (dropped: verify_commands already enables post-task hooks)
- [~] ~~Support custom task parsers (not just `- [ ]` markdown) via a `task_format` config — enables integration with GitHub Issues, Jira exports, plain-text lists, CSV~~ (dropped: markdown checkboxes are universal; premature abstraction)

### Robustness & Reliability
- [~] ~~Add file-watching mode (`--watch`) that re-reads Todo.md when it changes on disk (inotify/ReadDirectoryChanges) instead of only at task boundaries — enables live task additions without restarting~~ (dropped: orchestrator re-reads between tasks; inotify adds platform complexity)

### Testing & CI

- [~] ~~Add `pytest` as an alternative test runner alongside unittest (many contributors prefer it)~~ (dropped: pytest can run unittest tests already; no code change needed)
- [~] ~~Add code coverage reporting (coveralls/codecov badge in README)~~ (dropped: CI polish, not core)

### Documentation & Community

- [~] ~~Add ASCII art logo / banner for terminal output and README header~~ (dropped: cosmetic)
- [~] ~~Record a 2-minute demo GIF/asciinema for the README showing a real multi-provider overnight run~~ (dropped: marketing, do manually)

### Code Quality & Maintainability

- [~] ~~Replace global mutable state (`_current_process`, `_dashboard_server_ref`, `_control_state`, etc.) with a `RunContext` dataclass passed through the call chain — eliminates hidden coupling and makes testing trivial~~ (dropped: v2 package already uses cleaner patterns; legacy file is frozen)
- [~] ~~Extract the 50+ line `_build_html()` string-concatenation into a proper Jinja2-like template file (or at minimum a separate `.html` file read at startup) — the inline JS/HTML strings are unmaintainable~~ (dropped: would need Jinja2 dep or complex stdlib alternative)
- [~] ~~Add structured logging with levels (DEBUG/INFO/WARN/ERROR) instead of the current flat `log()` with color hints — enables log filtering and integration with standard logging infrastructure~~ (dropped: current system works; stdlib logging adds unnecessary complexity for a CLI tool)

### Performance & Scalability

- [~] ~~Add an optional SQLite backend for state (instead of JSON) for large-scale runs with 1000+ tasks — JSON read/write on every task becomes a bottleneck~~ (dropped: adds external dep risk; JSON is fine for realistic backlogs)
