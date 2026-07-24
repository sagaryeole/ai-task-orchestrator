# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **BREAKING: default config filename renamed** from `config.json` to `task-orchestrator.config.json` — `config.json` is a generic name likely to collide with another tool's config in the same repo, and its genericness made it easy to gitignore-miss. Existing users must rename their file (`mv config.json task-orchestrator.config.json`) or pass `--config config.json` explicitly; there is no automatic fallback.
- `task-orchestrator init` now gitignores the config file it scaffolds by default — provider `env` blocks commonly hold API keys (directly or via `$VAR` interpolation), so the config should not be committed by default.

## [2.0.0] - 2026-07-24

### Added
- **Package structure**: installable via `pip install task-orchestrator` with CLI entry point
- **`task-orchestrator init`**: scaffolds config, Todo.md, prompt template, and .gitignore
- **`task-orchestrator validate`**: checks config, provider reachability, and git state
- **`--provider <name>`**: force a specific provider for a single run
- **`--task "text"`**: run a one-off task without editing Todo.md
- **`--dry-run-prompt`**: show the exact prompt that would be sent without executing
- **`--resume-from <text>`**: start from a specific task instead of the top
- **`--list-tasks / --peek`**: preview next N tasks with provider assignment
- **Environment variable interpolation**: `$VAR` and `${VAR}` in config values
- **Atomic state writes**: write-then-rename prevents corruption on crash
- **Secret masking**: API keys redacted from log output
- **Global config**: `~/.task-orchestrator/config.json` merged under project config
- **JSON Schema**: `config.schema.json` for IDE autocompletion
- **`on_failure` config**: choose `skip`, `defer`, or `stop` behavior
- **Dashboard `/health` endpoint**: returns 200 for external monitoring
- **Dashboard auto-refresh**: JavaScript polling replaces meta-refresh
- **Dashboard port fallback**: retries next port if configured port is busy
- **Graceful dashboard shutdown**: explicit `server.shutdown()` on exit
- **SIGTERM handling**: graceful shutdown for containers/systemd
- **Cross-platform**: Windows file locking, process termination, executable resolution
- **PowerShell supervisor**: `run_forever.ps1` equivalent to `run_forever.sh`
- **Interactive commands**: p(ause), s(kip), q(uit), r(esume) during runs
- **Fun stats in summary**: longest/shortest task, success streak, verdict
- **Milestone celebrations**: notification every 10th task
- **Run report card**: printed on natural completion
- **GitHub Actions CI**: Python 3.9–3.13 on ubuntu/windows/macos
- **CONTRIBUTING.md**: development setup, architecture, PR process
- **Example configs**: copilot.json, claude.json, multi-provider.json

### Changed
- Main execution loop now lives in `src/task_orchestrator/runner.py`; top-level `orchestrator.py` is a compatibility shim
- Provider command resolution (and `verify_commands`/`stats_command`) now falls back between `python`/`python3`/`sys.executable`, whichever is actually available, instead of assuming one literal name is on `PATH`
- Failed tasks are skipped in-memory by default instead of reordered in Todo.md
- Provider command resolution uses `shutil.which` + Windows suffix fallback
- `.ps1` provider commands auto-wrapped through pwsh

### Fixed
- `fcntl` import crash on Windows
- False stall detection on Windows (CPU sampling unavailable)
- Tests using `/tmp` and `python3` now cross-platform
- `verify_commands`/`stats_command` failing outright on systems where only one of `python`/`python3` exists on `PATH`
- `run_forever.sh` losing its executable bit

### Known Gaps
- `working_directory` must be inside a git working tree — the orchestrator fails fast at startup if it isn't (`validate_git_working_tree`), it does not run without git
- Type hints are not yet applied across the whole codebase — `pyproject.toml`'s `mypy strict` config is aspirational, not currently enforced by CI or `make lint`
- `runner.py` is still one file, not split into the `config.py`/`provider.py`/`git.py`/etc. modules described in earlier drafts of this project — see `CONTRIBUTING.md`

## [1.0.0] - 2026-07-01

### Added
- Initial single-file orchestrator with multi-provider rotation
- Rate-limit detection and exponential backoff
- Activity-based stall detection
- Dashboard via stdlib http.server
- Verification gate and auto-commit
- Manual confirmation mode
