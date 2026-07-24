# Todo

Tasks for developing the orchestrator itself (`orchestrator.py`, `config.json`, prompts, docs).
Grouped by priority — work top section first.


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
