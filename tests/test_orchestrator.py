import unittest
import tempfile
import os
import json
import time
import re
import io
import datetime

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from pathlib import Path
from orchestrator import (
    load_tasks,
    mark_complete,
    pick_next_provider,
    seconds_until_next_available,
    Provider,
    load_state,
    save_state,
    validate_config,
    load_config,
    git_commit,
    git_run,
    validate_git_working_tree,
    run_verification,
    lint_config,
    lint_todo,
    get_task_timeout,
    TAG_REGEX,
    STATE_PATH,
    PID_PATH,
    print_summary,
    run_provider_stats,
    count_total_tasks,
    count_completed_tasks,
    start_dashboard,
    update_dashboard_state,
    build_provider_status,
    _dashboard_state,
    _build_html,
    _write_pid_file,
    _remove_pid_file,
    _resolve_executable,
    _resolve_shell_python,
    html_escape,
    DashboardHandler,
    DashboardServer,
    _compute_success_streak,
    cmd_init,
    cmd_validate,
    _interpolate_env_vars,
    _deep_merge,
    _register_secrets,
    _mask_secrets,
    _secret_values,
    _sigterm_handler,
    GLOBAL_CONFIG_PATH,
)


class TestLoadTasks(unittest.TestCase):
    def test_load_tasks(self):
        content = "- [ ] Task one\n- [x] Task two\n- [ ] Task three\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            tasks = load_tasks(Path(path))
            self.assertEqual(tasks, ["Task one", "Task three"])
        finally:
            os.unlink(path)

    def test_no_tasks(self):
        content = "- [x] Task one\n- [x] Task two\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            tasks = load_tasks(Path(path))
            self.assertEqual(tasks, [])
        finally:
            os.unlink(path)


class TestMarkComplete(unittest.TestCase):
    def test_mark_complete(self):
        content = "- [ ] Task one\n- [ ] Task two\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            mark_complete(Path(path), "Task one")
            updated = Path(path).read_text()
            self.assertIn("- [x] Task one", updated)
            self.assertIn("- [ ] Task two", updated)
        finally:
            os.unlink(path)


class TestPickNextProvider(unittest.TestCase):
    def _make_provider(self, name, cooldown=0, priority=0):
        return Provider({
            "name": name,
            "command": "echo test",
            "env": {},
            "rate_limit_patterns": [],
            "cooldown_seconds": cooldown,
            "priority": priority,
        })

    def test_round_robin(self):
        p1 = self._make_provider("p1")
        p2 = self._make_provider("p2")
        state = {"provider_cooldowns": {}}
        prov, idx = pick_next_provider([p1, p2], state, 0)
        self.assertEqual(prov.name, "p1")
        self.assertEqual(idx, 0)
        prov, idx = pick_next_provider([p1, p2], state, 1)
        self.assertEqual(prov.name, "p2")
        self.assertEqual(idx, 1)

    def test_priority_order(self):
        p1 = self._make_provider("low", priority=1)
        p2 = self._make_provider("high", priority=10)
        state = {"provider_cooldowns": {}}
        prov, idx = pick_next_provider([p1, p2], state, 0)
        self.assertEqual(prov.name, "high")
        self.assertEqual(idx, 1)

    def test_skip_cooldown(self):
        p1 = self._make_provider("p1")
        p2 = self._make_provider("p2")
        state = {"provider_cooldowns": {"p1": time.time() + 9999}}
        prov, idx = pick_next_provider([p1, p2], state, 0)
        self.assertEqual(prov.name, "p2")
        self.assertEqual(idx, 1)

    def test_all_exhausted(self):
        p1 = self._make_provider("p1")
        p2 = self._make_provider("p2")
        state = {"provider_cooldowns": {"p1": time.time() + 9999, "p2": time.time() + 9999}}
        prov, idx = pick_next_provider([p1, p2], state, 0)
        self.assertIsNone(prov)
        self.assertIsNone(idx)


class TestSecondsUntilNextAvailable(unittest.TestCase):
    def test_seconds_until(self):
        p1 = Provider({"name": "p1", "command": "echo", "env": {}, "rate_limit_patterns": []})
        p2 = Provider({"name": "p2", "command": "echo", "env": {}, "rate_limit_patterns": []})
        soon = time.time() + 5
        state = {"provider_cooldowns": {"p1": soon, "p2": soon + 10}}
        wait = seconds_until_next_available([p1, p2], state)
        self.assertGreaterEqual(wait, 4)
        self.assertLessEqual(wait, 6)


class TestProviderAvailability(unittest.TestCase):
    def test_is_available(self):
        p = Provider({"name": "p", "command": "echo", "env": {}, "rate_limit_patterns": []})
        state = {"provider_cooldowns": {}}
        self.assertTrue(p.is_available(state))

    def test_mark_exhausted(self):
        # mark_exhausted() calls the real save_state(), which writes to
        # STATE_PATH -- without patching it, this test silently overwrites
        # the real project's live state.json with fake "p" fixture data
        # every time it runs, corrupting real provider cooldown tracking.
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            with patch('orchestrator.STATE_PATH', state_path):
                p = Provider({"name": "p", "command": "echo", "env": {}, "rate_limit_patterns": []})
                state = {"provider_cooldowns": {}}
                p.mark_exhausted(state)
                self.assertFalse(p.is_available(state))
                self.assertIn("p", state["provider_cooldowns"])


class TestExponentialBackoff(unittest.TestCase):
    def test_first_rate_limit_uses_base_cooldown(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            with patch('orchestrator.STATE_PATH', state_path):
                p = Provider({"name": "p", "command": "echo", "env": {}, "rate_limit_patterns": [], "cooldown_seconds": 600})
                state = {"provider_cooldowns": {}}
                p.mark_exhausted(state, reason="rate_limited")
                until = state["provider_cooldowns"]["p"]
                expected = time.time() + 600
                self.assertAlmostEqual(until, expected, delta=1)

    def test_second_rate_limit_doubles_cooldown(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            with patch('orchestrator.STATE_PATH', state_path):
                p = Provider({"name": "p", "command": "echo", "env": {}, "rate_limit_patterns": [], "cooldown_seconds": 600})
                state = {"provider_cooldowns": {}}
                p.mark_exhausted(state, reason="rate_limited")
                p.mark_exhausted(state, reason="rate_limited")
                until = state["provider_cooldowns"]["p"]
                expected = time.time() + 1200
                self.assertAlmostEqual(until, expected, delta=1)

    def test_third_rate_limit_quadruples_cooldown(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            with patch('orchestrator.STATE_PATH', state_path):
                p = Provider({"name": "p", "command": "echo", "env": {}, "rate_limit_patterns": [], "cooldown_seconds": 600})
                state = {"provider_cooldowns": {}}
                for _ in range(3):
                    p.mark_exhausted(state, reason="rate_limited")
                until = state["provider_cooldowns"]["p"]
                expected = time.time() + 2400
                self.assertAlmostEqual(until, expected, delta=1)

    def test_backoff_capped_at_max(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            with patch('orchestrator.STATE_PATH', state_path):
                p = Provider({"name": "p", "command": "echo", "env": {}, "rate_limit_patterns": [], "cooldown_seconds": 600})
                state = {"provider_cooldowns": {}}
                for _ in range(10):
                    p.mark_exhausted(state, reason="rate_limited")
                until = state["provider_cooldowns"]["p"]
                max_expected = time.time() + 600 * 64
                self.assertLessEqual(until, max_expected + 1)

    def test_skip_provider_uses_fixed_cooldown(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            with patch('orchestrator.STATE_PATH', state_path):
                p = Provider({"name": "p", "command": "echo", "env": {}, "rate_limit_patterns": [], "cooldown_seconds": 600})
                state = {"provider_cooldowns": {}}
                p.mark_exhausted(state, reason="rate_limited")
                p.mark_exhausted(state, reason="rate_limited")
                p.mark_exhausted(state, reason="skip")
                until = state["provider_cooldowns"]["p"]
                # After 2 rate-limits (1200s) + skip (600s), should be the skip cooldown
                expected = time.time() + 600
                self.assertAlmostEqual(until, expected, delta=1)

    def test_reset_rate_limit_count(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            with patch('orchestrator.STATE_PATH', state_path):
                p = Provider({"name": "p", "command": "echo", "env": {}, "rate_limit_patterns": [], "cooldown_seconds": 600})
                state = {"provider_cooldowns": {}}
                p.mark_exhausted(state, reason="rate_limited")
                p.mark_exhausted(state, reason="rate_limited")
                self.assertEqual(p.get_rate_limit_count(state), 2)
                p.reset_rate_limit_count(state)
                self.assertEqual(p.get_rate_limit_count(state), 0)

    def test_rate_limit_count_persists_in_state(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            with patch('orchestrator.STATE_PATH', state_path):
                p = Provider({"name": "p", "command": "echo", "env": {}, "rate_limit_patterns": [], "cooldown_seconds": 600})
                state = {"provider_cooldowns": {}}
                p.mark_exhausted(state, reason="rate_limited")
                p.mark_exhausted(state, reason="rate_limited")
                self.assertEqual(state["provider_rate_limit_counts"]["p"], 2)

    def test_backward_compat_without_rate_limit_counts(self):
        p = Provider({"name": "p", "command": "echo", "env": {}, "rate_limit_patterns": [], "cooldown_seconds": 600})
        state = {"provider_cooldowns": {}}
        self.assertEqual(p.get_rate_limit_count(state), 0)
        p.mark_exhausted(state, reason="rate_limited")
        self.assertEqual(p.get_rate_limit_count(state), 1)


class TestValidateConfig(unittest.TestCase):
    def test_valid(self):
        cfg = {"todo_file": "Todo.md", "providers": [{"name": "a", "command": "cmd"}]}
        try:
            validate_config(cfg)
        except SystemExit:
            self.fail("Should not exit")

    def test_missing_todo_file(self):
        cfg = {"providers": [{"name": "a", "command": "cmd"}]}
        with self.assertRaises(SystemExit):
            validate_config(cfg)

    def test_empty_providers(self):
        cfg = {"todo_file": "Todo.md", "providers": []}
        with self.assertRaises(SystemExit):
            validate_config(cfg)

    def test_missing_name(self):
        cfg = {"todo_file": "Todo.md", "providers": [{"command": "cmd"}]}
        with self.assertRaises(SystemExit):
            validate_config(cfg)

    def test_load_config_with_path(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write('{"todo_file": "Todo.md", "providers": [{"name": "a", "command": "cmd"}]}')
            path = f.name
        try:
            cfg = load_config(Path(path))
            self.assertEqual(cfg["todo_file"], "Todo.md")
        finally:
            os.unlink(path)


class TestDryRun(unittest.TestCase):
    def test_dry_run_exits_cleanly(self):
        from unittest.mock import patch
        from orchestrator import main

        with tempfile.TemporaryDirectory() as tmpdir:
            todo = Path(tmpdir) / "Todo.md"
            todo.write_text("- [ ] Task one\n- [x] Task two\n")
            cfg_path = Path(tmpdir) / "config.json"
            dry_cmd = "python -c \"print('hello')\""
            cfg_path.write_text(json.dumps({
                "todo_file": str(todo),
                "providers": [{"name": "dryrun-p", "command": dry_cmd, "env": {}, "rate_limit_patterns": []}],
            }))
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"provider_cooldowns": {}}))
            pid_path = Path(tmpdir) / "orchestrator.pid"

            with patch.object(sys, 'argv', ['orchestrator.py', '--config', str(cfg_path), '--dry-run']):
                with patch('orchestrator.log') as mock_log:
                    with patch('orchestrator.STATE_PATH', state_path):
                        with patch('orchestrator.PID_PATH', pid_path):
                            main()
            log_output = ' '.join(str(call.args[0]) for call in mock_log.call_args_list)
            self.assertIn("Dry-run", log_output)
            self.assertIn("Task one", log_output)
            self.assertIn("dryrun-p", log_output)


class TestGitCommitGuard(unittest.TestCase):
    def test_git_commit_no_changes(self):
        import subprocess as sp
        with tempfile.TemporaryDirectory() as tmpdir:
            sp.run(["git", "init", "-q"], cwd=tmpdir)
            sp.run(["git", "config", "user.email", "tests@example.com"], cwd=tmpdir)
            sp.run(["git", "config", "user.name", "Tests"], cwd=tmpdir)
            (Path(tmpdir) / "a.txt").write_text("ok\n")
            sp.run(["git", "add", "-A"], cwd=tmpdir)
            sp.run(["git", "commit", "-q", "-m", "baseline"], cwd=tmpdir)
            cfg = {"auto_commit": True, "working_directory": tmpdir}
            result = git_commit(cfg, "dummy")
            self.assertIsNone(result)


class TestSuspiciousCompletion(unittest.TestCase):
    """A real incident: kilo reported exit code 0 on a task and had made zero
    edits, and the orchestrator marked it complete anyway since nothing
    checked whether the working tree actually changed. These tests cover
    the fix, in both confirmation modes."""

    def test_unattended_does_not_autocomplete_when_nothing_changed(self):
        from unittest.mock import patch
        from orchestrator import main
        import subprocess as sp

        with tempfile.TemporaryDirectory() as tmpdir:
            sp.run(["git", "init", "-q"], cwd=tmpdir)
            sp.run(["git", "config", "user.email", "tests@example.com"], cwd=tmpdir)
            sp.run(["git", "config", "user.name", "Tests"], cwd=tmpdir)
            # git status --porcelain (used to detect a real completion) sees
            # untracked files too, so the test's own scaffolding must be
            # committed as a clean baseline first, same as a real checkout
            # where Todo.md/config.json are already tracked -- otherwise it's
            # indistinguishable from the agent having "changed" something.
            (Path(tmpdir) / ".gitignore").write_text("orchestrator.pid\n")
            todo = Path(tmpdir) / "Todo.md"
            todo.write_text("- [ ] Task one\n")
            cfg_path = Path(tmpdir) / "config.json"
            no_change_cmd = "python -c \"print('hello')\""
            cfg_path.write_text(json.dumps({
                "todo_file": str(todo),
                "working_directory": tmpdir,
                "require_manual_confirmation": False,
                "continue_on_failure": True,
                "providers": [{"name": "p", "command": no_change_cmd, "env": {}, "rate_limit_patterns": []}],
            }))
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"provider_cooldowns": {}}))
            sp.run(["git", "add", "-A"], cwd=tmpdir)
            sp.run(["git", "commit", "-q", "-m", "baseline"], cwd=tmpdir)
            pid_path = Path(tmpdir) / "orchestrator.pid"

            with patch.object(sys, 'argv', ['orchestrator.py', '--config', str(cfg_path), '--once']):
                with patch('orchestrator.STATE_PATH', state_path):
                    with patch('orchestrator.time.sleep'):
                        with patch('orchestrator.log') as mock_log:
                            with patch('orchestrator.PID_PATH', pid_path):
                                main()

            log_output = ' '.join(str(call.args[0]) for call in mock_log.call_args_list)
            self.assertIn("SUSPICIOUS", log_output)
            final_text = todo.read_text()
            self.assertNotIn("- [x] Task one", final_text)
            self.assertIn("- [ ] Task one", final_text)

    def test_unattended_completes_normally_when_something_changed(self):
        from unittest.mock import patch
        from orchestrator import main
        import subprocess as sp

        with tempfile.TemporaryDirectory() as tmpdir:
            sp.run(["git", "init", "-q"], cwd=tmpdir)
            sp.run(["git", "config", "user.email", "tests@example.com"], cwd=tmpdir)
            sp.run(["git", "config", "user.name", "Tests"], cwd=tmpdir)
            (Path(tmpdir) / "existing.txt").write_text("baseline\n")
            sp.run(["git", "add", "-A"], cwd=tmpdir)
            sp.run(["git", "commit", "-q", "-m", "baseline"], cwd=tmpdir)

            todo = Path(tmpdir) / "Todo.md"
            todo.write_text("- [ ] Task one\n")
            cfg_path = Path(tmpdir) / "config.json"
            # Command actually modifies a tracked file, so git diff --stat is non-empty.
            target = (Path(tmpdir) / "existing.txt").as_posix()
            write_cmd = f"python -c \"open(r'{target}', 'w').write('changed')\""
            cfg_path.write_text(json.dumps({
                "todo_file": str(todo),
                "working_directory": tmpdir,
                "require_manual_confirmation": False,
                "providers": [{"name": "p", "command": write_cmd, "env": {}, "rate_limit_patterns": []}],
            }))
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"provider_cooldowns": {}}))
            pid_path = Path(tmpdir) / "orchestrator.pid"

            with patch.object(sys, 'argv', ['orchestrator.py', '--config', str(cfg_path), '--once']):
                with patch('orchestrator.STATE_PATH', state_path):
                    with patch('orchestrator.time.sleep'):
                        with patch('orchestrator.log') as mock_log:
                            with patch('orchestrator.PID_PATH', pid_path):
                                main()

            log_output = ' '.join(str(call.args[0]) for call in mock_log.call_args_list)
            self.assertNotIn("SUSPICIOUS", log_output)
            final_text = todo.read_text()
            self.assertIn("- [x] Task one", final_text)


class TestRateLimitFalsePositive(unittest.TestCase):
    """Regression tests for the false-positive rate-limit detection fix.

    rate_limited was decided by a plain substring match over the whole CLI output,
    so task/code text mentioning "rate limit", "429", or "quota" (routine domain
    vocabulary in this repo) discarded genuinely finished work and re-sent the
    same task after a cooldown. The fix only trusts the match when git diff --stat
    shows no real changes happened.
    """

    def test_false_positive_rate_limit_not_exhausted_when_files_changed(self):
        from unittest.mock import patch
        from orchestrator import main
        import subprocess as sp

        with tempfile.TemporaryDirectory() as tmpdir:
            sp.run(["git", "init", "-q"], cwd=tmpdir)
            sp.run(["git", "config", "user.email", "tests@example.com"], cwd=tmpdir)
            sp.run(["git", "config", "user.name", "Tests"], cwd=tmpdir)
            (Path(tmpdir) / "existing.txt").write_text("baseline\n")
            sp.run(["git", "add", "-A"], cwd=tmpdir)
            sp.run(["git", "commit", "-q", "-m", "baseline"], cwd=tmpdir)

            todo = Path(tmpdir) / "Todo.md"
            todo.write_text("- [ ] Task one\n")
            cfg_path = Path(tmpdir) / "config.json"
            target = (Path(tmpdir) / "existing.txt").as_posix()
            write_cmd = (
                f"python -c \"import os; open(r'{target}', 'w').write('changed'); "
                "print('rate limit from model')\""
            )
            cfg_path.write_text(json.dumps({
                "todo_file": str(todo),
                "working_directory": tmpdir,
                "require_manual_confirmation": False,
                "providers": [{
                    "name": "p",
                    "command": write_cmd,
                    "env": {},
                    "rate_limit_patterns": ["rate limit"]
                }],
            }))
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"provider_cooldowns": {}}))
            pid_path = Path(tmpdir) / "orchestrator.pid"

            with patch.object(sys, 'argv', ['orchestrator.py', '--config', str(cfg_path), '--once']):
                with patch('orchestrator.STATE_PATH', state_path):
                    with patch('orchestrator.time.sleep'):
                        with patch('orchestrator.log') as mock_log:
                            with patch('orchestrator.PID_PATH', pid_path):
                                main()

            log_output = ' '.join(str(call.args[0]) for call in mock_log.call_args_list)
            self.assertNotIn("marked exhausted", log_output)
            self.assertIn("Task marked complete", log_output)
            final_text = todo.read_text()
            self.assertIn("- [x] Task one", final_text)
            state = json.loads(state_path.read_text())
            self.assertNotIn("p", state["provider_cooldowns"])

    def test_true_rate_limit_is_still_exhausted_when_no_files_changed(self):
        from unittest.mock import patch
        from orchestrator import main
        import subprocess as sp

        with tempfile.TemporaryDirectory() as tmpdir:
            sp.run(["git", "init", "-q"], cwd=tmpdir)
            sp.run(["git", "config", "user.email", "tests@example.com"], cwd=tmpdir)
            sp.run(["git", "config", "user.name", "Tests"], cwd=tmpdir)
            # git status --porcelain (used to confirm the rate-limit signal)
            # sees untracked files too, so commit the test's own scaffolding
            # as a clean baseline first -- same reasoning as the sibling
            # suspicious-completion test above.
            (Path(tmpdir) / ".gitignore").write_text("orchestrator.pid\n")

            todo = Path(tmpdir) / "Todo.md"
            todo.write_text("- [ ] Task one\n")
            cfg_path = Path(tmpdir) / "config.json"
            # Output contains "rate limit" but makes NO file changes.
            cmd = "python -c \"print('rate limit exceeded')\""
            cfg_path.write_text(json.dumps({
                "todo_file": str(todo),
                "working_directory": tmpdir,
                "require_manual_confirmation": False,
                "continue_on_failure": True,
                "providers": [
                    {
                        "name": "limited-p",
                        "command": cmd,
                        "env": {},
                        "rate_limit_patterns": ["rate limit"]
                    },
                    {
                        "name": "fail-p",
                        "command": "python -c \"import sys; sys.exit(1)\"",
                        "env": {},
                        "rate_limit_patterns": []
                    },
                ],
            }))
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"provider_cooldowns": {}}))
            sp.run(["git", "add", "-A"], cwd=tmpdir)
            sp.run(["git", "commit", "-q", "-m", "baseline"], cwd=tmpdir)
            pid_path = Path(tmpdir) / "orchestrator.pid"

            with patch.object(sys, 'argv', ['orchestrator.py', '--config', str(cfg_path), '--once']):
                with patch('orchestrator.STATE_PATH', state_path):
                    with patch('orchestrator.time.sleep'):
                        with patch('orchestrator.log') as mock_log:
                            with patch('orchestrator.PID_PATH', pid_path):
                                main()

            log_output = ' '.join(str(call.args[0]) for call in mock_log.call_args_list)
            self.assertIn("marked exhausted", log_output)
            self.assertIn("limited-p", log_output)
            state = json.loads(state_path.read_text())
            self.assertIn("limited-p", state["provider_cooldowns"])
            final_text = todo.read_text()
            self.assertIn("- [ ] Task one", final_text)


class TestVerifyLiveOutput(unittest.TestCase):
    def test_verify_prints_to_stderr(self):
        import io
        from unittest.mock import patch
        fail_cmd = "python -c \"import sys; sys.exit(1)\""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('sys.stderr', new=io.StringIO()) as fake_err:
                with patch('orchestrator.log') as mock_log:
                    run_verification({"verify_commands": [fail_cmd], "working_directory": tmpdir})
                stderr_output = fake_err.getvalue()
                self.assertIn("Verification FAILED", stderr_output)
                self.assertIn("sys.exit(1)", stderr_output)


class TestSkipTask(unittest.TestCase):
    def test_skip_task_defers(self):
        from unittest.mock import patch
        from orchestrator import main

        with tempfile.TemporaryDirectory() as tmpdir:
            todo = Path(tmpdir) / "Todo.md"
            todo.write_text("- [ ] Task one\n- [ ] Task two\n")
            cfg_path = Path(tmpdir) / "config.json"
            ok_cmd = "python -c \"print('ok')\""
            cfg_path.write_text(json.dumps({
                "todo_file": str(todo),
                "providers": [{"name": "p", "command": ok_cmd, "env": {}, "rate_limit_patterns": []}],
                "require_manual_confirmation": True,
            }))
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"provider_cooldowns": {}}))
            pid_path = Path(tmpdir) / "orchestrator.pid"

            inputs = ["skip-task", "y", "y"]
            with patch.object(sys, 'argv', ['orchestrator.py', '--config', str(cfg_path)]):
                with patch('orchestrator.STATE_PATH', state_path):
                    with patch('builtins.input', side_effect=inputs):
                        with patch('orchestrator.time.sleep'):
                            with patch('orchestrator.log') as mock_log:
                                with patch('orchestrator.PID_PATH', pid_path):
                                    main()
            log_output = ' '.join(str(call.args[0]) for call in mock_log.call_args_list)
            self.assertIn("skipped for this cycle", log_output)
            final_text = todo.read_text()
            self.assertIn("- [x] Task one", final_text)
            self.assertIn("- [x] Task two", final_text)


class TestLintConfig(unittest.TestCase):
    def _run_lint(self, cfg):
        from unittest.mock import patch
        with patch('orchestrator.log') as mock_log:
            lint_config(cfg)
        return ' '.join(str(call.args[0]) for call in mock_log.call_args_list)

    def test_warns_on_missing_task_placeholder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            template = Path(tmpdir) / "bad_template.txt"
            template.write_text("Just some text without placeholder")
            output = self._run_lint({
                "prompt_template": str(template),
                "providers": [{"name": "p", "command": "echo", "env": {}, "rate_limit_patterns": []}],
            })
            self.assertIn("does not contain '{{TASK}}'", output)

    def test_no_warn_on_good_template(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            template = Path(tmpdir) / "good_template.txt"
            template.write_text("Do this: {{TASK}}")
            output = self._run_lint({
                "prompt_template": str(template),
                "providers": [{"name": "p", "command": "echo", "env": {}, "rate_limit_patterns": []}],
            })
            self.assertNotIn("does not contain '{{TASK}}'", output)

    def test_warns_on_bare_claude(self):
        output = self._run_lint({
            "providers": [{"name": "claude-p", "command": "claude", "env": {}, "rate_limit_patterns": []}],
        })
        self.assertIn("bare/interactive", output)
        self.assertIn("--no-interactive", output)

    def test_no_warn_on_claude_with_headless_flag(self):
        output = self._run_lint({
            "providers": [{"name": "claude-p", "command": "claude --no-interactive", "env": {}, "rate_limit_patterns": []}],
        })
        self.assertNotIn("bare/interactive", output)

    def test_warns_on_bare_kilo(self):
        output = self._run_lint({
            "providers": [{"name": "kilo-p", "command": "kilo", "env": {}, "rate_limit_patterns": []}],
        })
        self.assertIn("bare/interactive", output)
        self.assertIn("--auto", output)

    def test_warns_on_replace_me_env(self):
        output = self._run_lint({
            "providers": [{"name": "p", "command": "echo", "env": {"KEY": "REPLACE_ME"}, "rate_limit_patterns": []}],
        })
        self.assertIn("REPLACE_ME", output)

    def test_no_warn_on_good_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            template = Path(tmpdir) / "good.txt"
            template.write_text("{{TASK}}")
            output = self._run_lint({
                "prompt_template": str(template),
                "providers": [
                    {
                        "name": "p",
                        "command": "echo hello --auto",
                        "env": {"KEY": "real-key"},
                        "rate_limit_patterns": [],
                    }
                ],
            })
            self.assertEqual(output.strip(), "")


class TestLintTodo(unittest.TestCase):
    def _run_lint(self, content):
        from unittest.mock import patch
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            path = f.name
        try:
            with patch('orchestrator.log') as mock_log:
                lint_todo(Path(path))
            return ' '.join(str(call.args[0]) for call in mock_log.call_args_list)
        finally:
            os.unlink(path)

    def test_warns_on_duplicate_headers(self):
        output = self._run_lint("## Section\n## Section\n")
        self.assertIn("duplicate section header", output)
        self.assertIn("2x", output)

    def test_warns_on_duplicate_tasks(self):
        output = self._run_lint("- [ ] Task A\n- [ ] Task A\n")
        self.assertIn("duplicate task line", output)
        self.assertIn("2x", output)

    def test_no_warn_on_unique(self):
        output = self._run_lint("- [ ] Task A\n- [ ] Task B\n")
        self.assertNotIn("duplicate", output)

    def test_no_warn_on_missing_file(self):
        from unittest.mock import patch
        with patch('orchestrator.log') as mock_log:
            lint_todo(Path("/nonexistent/path/Todo.md"))
        self.assertEqual(mock_log.call_count, 0)


class TestCountTasksSkipSections(unittest.TestCase):
    def _write(self, content):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            return Path(f.name)

    def test_duplicate_text_within_skipped_section_all_excluded(self):
        # Regression test: a set-based skip/subtract implementation dedupes
        # by line text, so two identical lines in the same skipped section
        # only get subtracted once, undercounting by 1 for every extra
        # duplicate. Both occurrences must be excluded here.
        path = self._write(
            "## Section A (kept)\n"
            "- [ ] Unique kept task\n"
            "## Section B (skip this one)\n"
            "- [ ] Duplicate task text\n"
            "- [ ] Duplicate task text\n"
        )
        try:
            total = count_total_tasks(path, skip_sections=["Section B (skip this one)"])
            self.assertEqual(total, 1)
        finally:
            os.unlink(path)

    def test_duplicate_text_across_kept_and_skipped_sections(self):
        path = self._write(
            "## Section A (kept)\n"
            "- [x] Same text\n"
            "## Section B (skip this one)\n"
            "- [x] Same text\n"
        )
        try:
            completed = count_completed_tasks(path, skip_sections=["Section B (skip this one)"])
            self.assertEqual(completed, 1)
        finally:
            os.unlink(path)


class TestGetTaskTimeout(unittest.TestCase):
    def test_no_overrides_returns_global(self):
        self.assertEqual(get_task_timeout("do stuff", 180, {}), 180)

    def test_none_global_no_overrides(self):
        self.assertIsNone(get_task_timeout("do stuff", None, {}))

    def test_tag_matches_override(self):
        overrides = {"[big]": 900, "[slow]": 1800}
        self.assertEqual(get_task_timeout("[big] Refactor", 180, overrides), 900)

    def test_multiple_tags_picks_largest(self):
        overrides = {"[big]": 900, "[slow]": 1800}
        self.assertEqual(get_task_timeout("[big] [slow] Refactor", 180, overrides), 1800)

    def test_unmatched_tag_ignored(self):
        overrides = {"[big]": 900}
        self.assertEqual(get_task_timeout("[tiny] Do thing", 180, overrides), 180)

    def test_empty_overrides_dict(self):
        self.assertEqual(get_task_timeout("[big] Thing", 180, {}), 180)

    def test_none_overrides(self):
        self.assertEqual(get_task_timeout("[big] Thing", 180, None), 180)

    def test_case_sensitive_tags(self):
        overrides = {"[big]": 900}
        self.assertEqual(get_task_timeout("[BIG] Thing", 180, overrides), 180)


class TestTagRegex(unittest.TestCase):
    def test_single_tag(self):
        self.assertEqual(re.findall(TAG_REGEX, "[big] task"), ["[big]"])

    def test_multiple_tags(self):
        self.assertEqual(re.findall(TAG_REGEX, "[big] [slow] task"), ["[big]", "[slow]"])

    def test_no_tags(self):
        self.assertEqual(re.findall(TAG_REGEX, "normal task"), [])


class TestPrintSummary(unittest.TestCase):
    def test_summary_counts_today(self):
        from unittest.mock import patch
        today = datetime.date.today().isoformat()
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir) / "logs"
            log_dir.mkdir()
            log_file = log_dir / "orchestrator.log"
            log_file.write_text(
                f"[{today} 10:00:00] Starting task: A\n"
                f"[{today} 10:01:00] Task marked complete: A (provider: p)\n"
                f"[{today} 10:02:00] Starting task: B\n"
                f"[{today} 10:03:00] Task NOT completed: B\n"
            )
            todo = Path(tmpdir) / "Todo.md"
            todo.write_text("- [ ] C\n")
            state = {"completed_task_durations": [60.0, 120.0]}
            fake_stdout = io.StringIO()
            with patch('sys.stdout', fake_stdout):
                with patch('orchestrator.LOG_DIR', log_dir):
                    print_summary(state, todo, log_path=log_file)
            output = fake_stdout.getvalue()
            self.assertIn("Tasks completed today: 1", output)
            self.assertIn("Success rate: 1/2 (50%)", output)
            self.assertIn("Average time per task: 1m 30s", output)

    def test_summary_no_tasks_today(self):
        from unittest.mock import patch
        today = datetime.date.today().isoformat()
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir) / "logs"
            log_dir.mkdir()
            log_file = log_dir / "orchestrator.log"
            log_file.write_text(
                f"[{yesterday} 10:00:00] Task marked complete: old (provider: p)\n"
            )
            todo = Path(tmpdir) / "Todo.md"
            todo.write_text("- [ ] C\n")
            state = {"completed_task_durations": []}
            fake_stdout = io.StringIO()
            with patch('sys.stdout', fake_stdout):
                with patch('orchestrator.LOG_DIR', log_dir):
                    print_summary(state, todo, log_path=log_file)
            output = fake_stdout.getvalue()
            self.assertIn("Tasks completed today: 0", output)
            self.assertIn("Success rate: N/A", output)
            self.assertIn("Average time per task: N/A", output)


class TestSummaryFlag(unittest.TestCase):
    def test_summary_flag_exits_cleanly(self):
        from unittest.mock import patch
        from orchestrator import main

        today = datetime.date.today().isoformat()
        with tempfile.TemporaryDirectory() as tmpdir:
            todo = Path(tmpdir) / "Todo.md"
            todo.write_text("- [ ] Task one\n")
            cfg_path = Path(tmpdir) / "config.json"
            cfg_path.write_text(json.dumps({
                "todo_file": str(todo),
                "providers": [{"name": "p", "command": "echo hello", "env": {}, "rate_limit_patterns": []}],
            }))
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"provider_cooldowns": {}}))

            log_dir = Path(tmpdir) / "logs"
            log_dir.mkdir()
            log_file = log_dir / "orchestrator.log"
            log_file.write_text(
                f"[{today} 10:00:00] Starting task: Task one\n"
                f"[{today} 10:01:00] Task marked complete: Task one (provider: p)\n"
            )

            fake_stdout = io.StringIO()
            with patch.object(sys, 'argv', ['orchestrator.py', '--config', str(cfg_path), '--summary']):
                with patch('orchestrator.STATE_PATH', state_path):
                    with patch('orchestrator.LOG_DIR', log_dir):
                        with patch('sys.stdout', fake_stdout):
                            main()
            output = fake_stdout.getvalue()
            self.assertIn("Summary", output)
            self.assertIn("Tasks completed today: 1", output)


class TestSummaryEnhancements(unittest.TestCase):
    def test_summary_includes_streak_and_verdict(self):
        from unittest.mock import patch
        today = datetime.date.today().isoformat()
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir) / "logs"
            log_dir.mkdir()
            log_file = log_dir / "orchestrator.log"
            log_file.write_text(
                f"[{today} 10:00:00] Task marked complete: A (provider: p)\n"
                f"[{today} 10:01:00] Task marked complete: B (provider: p)\n"
            )
            todo = Path(tmpdir) / "Todo.md"
            todo.write_text("- [x] A\n")
            state = {"completed_task_durations": [10.0, 20.0]}
            fake_stdout = io.StringIO()
            with patch('sys.stdout', fake_stdout):
                with patch('orchestrator.LOG_DIR', log_dir):
                    print_summary(state, todo, log_path=log_file)
            output = fake_stdout.getvalue()
            self.assertIn("Current success streak:", output)
            self.assertIn("Verdict:", output)

    def test_compute_success_streak(self):
        today = datetime.date.today().isoformat()
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "orchestrator.log"
            log_file.write_text(
                f"[{today} 09:00:00] Task NOT completed: A\n"
                f"[{today} 09:05:00] Task marked complete: B\n"
                f"[{today} 09:10:00] Task marked complete: C\n"
            )
            streak = _compute_success_streak(log_file, today)
            self.assertEqual(streak, 2)


class TestListTasksFlag(unittest.TestCase):
    def test_list_tasks_prints_preview(self):
        from unittest.mock import patch
        from orchestrator import main

        with tempfile.TemporaryDirectory() as tmpdir:
            import subprocess as sp
            sp.run(["git", "init", "-q"], cwd=tmpdir)
            sp.run(["git", "config", "user.email", "tests@example.com"], cwd=tmpdir)
            sp.run(["git", "config", "user.name", "Tests"], cwd=tmpdir)

            todo = Path(tmpdir) / "Todo.md"
            todo.write_text("- [ ] Task one\n- [ ] Task two\n")
            cfg_path = Path(tmpdir) / "config.json"
            cfg_path.write_text(json.dumps({
                "todo_file": str(todo),
                "working_directory": tmpdir,
                "providers": [{"name": "p", "command": "python -c \"print('ok')\"", "env": {}, "rate_limit_patterns": []}],
            }))
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"provider_cooldowns": {}}))
            pid_path = Path(tmpdir) / "orchestrator.pid"

            with patch.object(sys, 'argv', ['orchestrator.py', '--config', str(cfg_path), '--list-tasks', '2']):
                with patch('orchestrator.STATE_PATH', state_path):
                    with patch('orchestrator.PID_PATH', pid_path):
                        with patch('orchestrator.log') as mock_log:
                            main()
            joined = " ".join(str(c.args[0]) for c in mock_log.call_args_list)
            self.assertIn("Next 2 pending tasks", joined)
            self.assertIn("Task one", joined)
            self.assertIn("Task two", joined)


class TestDashboardPortFallback(unittest.TestCase):
    def _find_free_port(self):
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def test_dashboard_retries_to_next_port(self):
        import socket
        busy_port = self._find_free_port()
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", busy_port))
        blocker.listen(1)
        try:
            server = start_dashboard(busy_port, retry_on_port_in_use=True, max_attempts=3)
            self.assertIsNotNone(server)
            self.assertNotEqual(server.server_port, busy_port)
            server.shutdown()
            server.server_close()
        finally:
            blocker.close()



class TestProviderStats(unittest.TestCase):
    def test_stats_logs_output(self):
        from unittest.mock import patch
        cfg = {
            "name": "p",
            "command": "echo hello",
            "env": {},
            "rate_limit_patterns": [],
            "stats_command": "echo '{\"tokens\": 100}'",
        }
        with patch('orchestrator.log_json') as mock_json:
            p = Provider(cfg)
            run_provider_stats(p, ".", "Test task")
        logged = [str(c.args[0]) for c in mock_json.call_args_list]
        self.assertTrue(any("provider_stats" in c for c in logged))

    def test_stats_no_command(self):
        from unittest.mock import patch
        cfg = {
            "name": "p",
            "command": "echo hello",
            "env": {},
            "rate_limit_patterns": [],
        }
        with patch('orchestrator.log') as mock_log:
            p = Provider(cfg)
            run_provider_stats(p, ".", "Test task")
        self.assertEqual(mock_log.call_count, 0)

    def test_stats_command_not_found(self):
        from unittest.mock import patch
        cfg = {
            "name": "p",
            "command": "echo hello",
            "env": {},
            "rate_limit_patterns": [],
            "stats_command": "/nonexistent/stats-binary",
        }
        with patch('orchestrator.log') as mock_log:
            with patch('orchestrator.log_json') as mock_json:
                p = Provider(cfg)
                run_provider_stats(p, ".", "Test task")
        log_msgs = [str(c.args[0]) for c in mock_log.call_args_list]
        self.assertTrue(any("not found" in m for m in log_msgs))

    def test_stats_timeout(self):
        # Simulate the timeout directly rather than actually running a
        # subprocess and waiting for run_provider_stats()'s real 30s timeout
        # to elapse -- the previous version used "stats_command": "sleep 100"
        # and genuinely blocked for 30 real seconds. Since verify_commands
        # runs the whole test suite after every task, that added a 30s tax
        # to every single task completion.
        import subprocess
        from unittest.mock import patch
        cfg = {
            "name": "p",
            "command": "echo hello",
            "env": {},
            "rate_limit_patterns": [],
            "stats_command": "stats-binary",
        }
        with patch('orchestrator.log') as mock_log:
            with patch('orchestrator.log_json') as mock_json:
                with patch('orchestrator.subprocess.run', side_effect=subprocess.TimeoutExpired(cmd="stats-binary", timeout=30)):
                    p = Provider(cfg)
                    run_provider_stats(p, ".", "Test task")
        log_msgs = [str(c.args[0]) for c in mock_log.call_args_list]
        self.assertTrue(any("timed out" in m for m in log_msgs))

    def test_stats_logs_json_payload(self):
        from unittest.mock import patch
        cfg = {
            "name": "p",
            "command": "echo hello",
            "env": {},
            "rate_limit_patterns": [],
            "stats_command": "python -c \"import json; print(json.dumps({'tokens': 50, 'cost': 0.01}))\"",
        }
        with patch('orchestrator.log_json') as mock_json:
            p = Provider(cfg)
            run_provider_stats(p, ".", "Test task")
        args_list = [c.args for c in mock_json.call_args_list]
        event_names = [a[0] for a in args_list if a]
        self.assertIn("provider_stats", event_names)
        kwargs_list = [c.kwargs for c in mock_json.call_args_list]
        self.assertTrue(any(k.get("task") == "Test task" for k in kwargs_list))


class TestHtmlEscape(unittest.TestCase):
    def test_escapes_ampersand(self):
        self.assertEqual(html_escape("&"), "&amp;")

    def test_escapes_lt_gt(self):
        self.assertEqual(html_escape("<script>"), "&lt;script&gt;")

    def test_escapes_quotes(self):
        self.assertEqual(html_escape('"hello"'), "&quot;hello&quot;")

    def test_no_escape_needed(self):
        self.assertEqual(html_escape("plain text"), "plain text")


class TestBuildProviderStatus(unittest.TestCase):
    def _make_provider(self, name, cooldown=0):
        return Provider({
            "name": name,
            "command": "echo test",
            "env": {},
            "rate_limit_patterns": [],
            "cooldown_seconds": cooldown,
        })

    def test_available_provider(self):
        p = self._make_provider("p1")
        state = {"provider_cooldowns": {}}
        status = build_provider_status([p], state)
        self.assertTrue(status["p1"]["available"])
        self.assertIsNone(status["p1"]["cooldown_until"])

    def test_cooldown_provider(self):
        p = self._make_provider("p1")
        soon = time.time() + 300
        state = {"provider_cooldowns": {"p1": soon}}
        status = build_provider_status([p], state)
        self.assertFalse(status["p1"]["available"])
        self.assertIsNotNone(status["p1"]["cooldown_until"])


class TestUpdateDashboardState(unittest.TestCase):
    def setUp(self):
        _dashboard_state.clear()
        _dashboard_state["start_time"] = None

    def test_set_current_task(self):
        update_dashboard_state(current_task="my task")
        self.assertEqual(_dashboard_state["current_task"], "my task")

    def test_set_current_provider(self):
        update_dashboard_state(current_provider="my-provider")
        self.assertEqual(_dashboard_state["current_provider"], "my-provider")

    def test_set_provider_status(self):
        status = {"p1": {"available": True, "cooldown_until": None}}
        update_dashboard_state(provider_status=status)
        self.assertEqual(_dashboard_state["providers"], status)

    def test_append_history_entry(self):
        entry = {"task": "T", "provider": "p", "status": "complete", "timestamp": "2026-01-01T00:00:00"}
        update_dashboard_state(history_entry=entry)
        self.assertEqual(len(_dashboard_state["history"]), 1)
        self.assertEqual(_dashboard_state["history"][0], entry)

    def test_history_capped_at_max(self):
        for i in range(60):
            update_dashboard_state(history_entry={"task": f"T{i}", "status": "complete"})
        self.assertEqual(len(_dashboard_state["history"]), 50)
        self.assertEqual(_dashboard_state["history"][0]["task"], "T10")
        self.assertEqual(_dashboard_state["history"][-1]["task"], "T59")

    def test_start_time_set_once(self):
        update_dashboard_state(current_task="T")
        first = _dashboard_state["start_time"]
        self.assertIsNotNone(first)
        update_dashboard_state(current_task="T2")
        self.assertEqual(_dashboard_state["start_time"], first)


class TestBuildHtml(unittest.TestCase):
    def setUp(self):
        _dashboard_state.clear()
        _dashboard_state["start_time"] = time.time()

    def test_idle_state(self):
        html = _build_html(_dashboard_state)
        self.assertIn("Orchestrator Dashboard", html)
        self.assertIn("idle", html)
        self.assertIn("No history yet", html)

    def test_shows_current_task(self):
        update_dashboard_state(current_task="Build feature X")
        html = _build_html(_dashboard_state)
        self.assertIn("Build feature X", html)

    def test_shows_current_provider(self):
        update_dashboard_state(current_provider="kilo")
        html = _build_html(_dashboard_state)
        self.assertIn("kilo", html)

    def test_shows_provider_status(self):
        status = {"kilo": {"available": True, "cooldown_until": None}}
        update_dashboard_state(provider_status=status)
        html = _build_html(_dashboard_state)
        self.assertIn("available", html)

    def test_shows_cooldown_status(self):
        soon = time.time() + 300
        status = {"kilo": {"available": False, "cooldown_until": soon}}
        update_dashboard_state(provider_status=status)
        html = _build_html(_dashboard_state)
        self.assertIn("cooldown", html)

    def test_shows_history(self):
        entry = {"task": "Test task", "provider": "kilo", "status": "complete", "timestamp": "2026-01-01T00:00:00"}
        update_dashboard_state(history_entry=entry)
        html = _build_html(_dashboard_state)
        self.assertIn("Test task", html)
        self.assertIn("complete", html)

    def test_failed_history_entry(self):
        entry = {"task": "Failing task", "provider": "kilo", "status": "failed", "timestamp": "2026-01-01T00:00:00"}
        update_dashboard_state(history_entry=entry)
        html = _build_html(_dashboard_state)
        self.assertIn("Failing task", html)
        self.assertIn("failed", html)

    def test_uptime_displayed(self):
        html = _build_html(_dashboard_state)
        self.assertIn("Uptime:", html)


class TestDashboardServer(unittest.TestCase):
    def setUp(self):
        _dashboard_state.clear()
        _dashboard_state["start_time"] = time.time()

    def _find_free_port(self):
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def test_server_starts_and_serves_html(self):
        port = self._find_free_port()
        server = start_dashboard(port)
        self.assertIsNotNone(server)
        server.shutdown()
        server.server_close()

    def test_json_endpoint_returns_valid_json(self):
        import urllib.request
        port = self._find_free_port()
        server = start_dashboard(port)
        self.assertIsNotNone(server)
        try:
            url = f"http://127.0.0.1:{port}/api/state"
            req = urllib.request.urlopen(url, timeout=5)
            data = json.loads(req.read().decode("utf-8"))
            self.assertIn("current_task", data)
            self.assertIn("providers", data)
            self.assertIn("history", data)
            self.assertIn("uptime_seconds", data)
        finally:
            server.shutdown()
            server.server_close()

    def test_html_endpoint_returns_html(self):
        import urllib.request
        port = self._find_free_port()
        server = start_dashboard(port)
        self.assertIsNotNone(server)
        try:
            url = f"http://127.0.0.1:{port}/"
            req = urllib.request.urlopen(url, timeout=5)
            content = req.read().decode("utf-8")
            self.assertIn("text/html", req.headers.get("Content-Type", ""))
            self.assertIn("Orchestrator Dashboard", content)
        finally:
            server.shutdown()
            server.server_close()

    def test_json_endpoint_with_state(self):
        import urllib.request
        update_dashboard_state(current_task="Test task", current_provider="kilo")
        port = self._find_free_port()
        server = start_dashboard(port)
        self.assertIsNotNone(server)
        try:
            url = f"http://127.0.0.1:{port}/api/state"
            req = urllib.request.urlopen(url, timeout=5)
            data = json.loads(req.read().decode("utf-8"))
            self.assertEqual(data["current_task"], "Test task")
            self.assertEqual(data["current_provider"], "kilo")
        finally:
            server.shutdown()
            server.server_close()


class TestNonUTF8Output(unittest.TestCase):
    """Non-UTF-8 bytes in a provider CLI's stdout/stderr should not crash
    the orchestrator — errors="replace" on subprocess calls prevents
    UnicodeDecodeError from propagating."""

    def test_provider_run_with_non_utf8_bytes(self):
        """A provider command that emits non-UTF-8 bytes should return
        a valid result rather than raising UnicodeDecodeError."""
        from unittest.mock import patch
        from orchestrator import Provider

        # A command that writes raw non-UTF-8 bytes to stdout
        cmd = "python -c \"import sys; sys.stdout.buffer.write(b'\\xff\\xfe hello')\""
        cfg = {
            "name": "nonutf8-p",
            "command": cmd,
            "env": {},
            "rate_limit_patterns": [],
        }
        p = Provider(cfg)
        with tempfile.TemporaryDirectory() as tmpdir:
            exit_code, output, rate_limited = p.run("test prompt", tmpdir)
        # Should not crash — the bad bytes are replaced, not raised
        self.assertIsInstance(output, str)
        self.assertIn("hello", output)

    def test_provider_run_with_non_utf8_stderr(self):
        """Non-UTF-8 bytes on stderr should also be handled gracefully."""
        from unittest.mock import patch
        from orchestrator import Provider

        cmd = "python -c \"import sys; sys.stderr.buffer.write(b'\\xff malformed')\""
        cfg = {
            "name": "nonutf8-stderr",
            "command": cmd,
            "env": {},
            "rate_limit_patterns": [],
        }
        p = Provider(cfg)
        with tempfile.TemporaryDirectory() as tmpdir:
            exit_code, output, rate_limited = p.run("test prompt", tmpdir)
        self.assertIsInstance(output, str)
        self.assertIn("malformed", output)


class TestGitRunRetry(unittest.TestCase):
    """git_run() should retry once on transient git failures (e.g. index.lock
    contention) instead of treating them as hard failures."""

    def _mock_result(self, returncode, stderr="", stdout=""):
        r = unittest.mock.Mock()
        r.returncode = returncode
        r.stdout = stdout
        r.stderr = stderr
        return r

    def test_git_run_success_no_retry(self):
        from unittest.mock import patch
        with patch('orchestrator.subprocess.run') as mock_run:
            mock_run.return_value = self._mock_result(0, stdout="")
            result = git_run(["status", "--porcelain"], cwd="/tmp")
            self.assertEqual(result.returncode, 0)
            self.assertEqual(mock_run.call_count, 1)

    def test_git_run_retries_on_index_lock(self):
        from unittest.mock import patch
        with patch('orchestrator.subprocess.run') as mock_run:
            mock_run.side_effect = [
                self._mock_result(128, stderr="fatal: Unable to create '/tmp/.git/index.lock': File exists."),
                self._mock_result(0, stdout=""),
            ]
            result = git_run(["status", "--porcelain"], cwd="/tmp")
            self.assertEqual(result.returncode, 0)
            self.assertEqual(mock_run.call_count, 2)

    def test_git_run_retries_on_head_lock(self):
        from unittest.mock import patch
        with patch('orchestrator.subprocess.run') as mock_run:
            mock_run.side_effect = [
                self._mock_result(128, stderr="fatal: Unable to create '/tmp/.git/HEAD.lock': File exists."),
                self._mock_result(0, stdout=""),
            ]
            result = git_run(["status", "--porcelain"], cwd="/tmp")
            self.assertEqual(result.returncode, 0)
            self.assertEqual(mock_run.call_count, 2)

    def test_git_run_retries_once_then_fails(self):
        from unittest.mock import patch
        with patch('orchestrator.subprocess.run') as mock_run:
            mock_run.side_effect = [
                self._mock_result(128, stderr="fatal: index.lock: File exists."),
                self._mock_result(128, stderr="fatal: index.lock: File exists."),
            ]
            result = git_run(["status", "--porcelain"], cwd="/tmp")
            self.assertEqual(result.returncode, 128)
            self.assertEqual(mock_run.call_count, 2)

    def test_git_run_no_retry_on_non_transient_error(self):
        from unittest.mock import patch
        with patch('orchestrator.subprocess.run') as mock_run:
            mock_run.return_value = self._mock_result(128, stderr="fatal: not a git repository (or any of the parent directories): .git")
            result = git_run(["status", "--porcelain"], cwd="/tmp")
            self.assertEqual(result.returncode, 128)
            self.assertEqual(mock_run.call_count, 1)

    def test_git_run_no_retry_on_other_error(self):
        from unittest.mock import patch
        with patch('orchestrator.subprocess.run') as mock_run:
            mock_run.return_value = self._mock_result(1, stderr="some other git error")
            result = git_run(["status", "--porcelain"], cwd="/tmp")
            self.assertEqual(result.returncode, 1)
            self.assertEqual(mock_run.call_count, 1)

    def test_git_run_passes_cwd(self):
        from unittest.mock import patch
        with patch('orchestrator.subprocess.run') as mock_run:
            mock_run.return_value = self._mock_result(0, stdout="")
            git_run(["status", "--porcelain"], cwd="/some/path")
            call_kwargs = mock_run.call_args
            self.assertEqual(call_kwargs.kwargs.get("cwd"), "/some/path")

    def test_git_run_passes_timeout(self):
        from unittest.mock import patch
        with patch('orchestrator.subprocess.run') as mock_run:
            mock_run.return_value = self._mock_result(0, stdout="")
            git_run(["status", "--porcelain"], cwd="/tmp", timeout=5)
            call_kwargs = mock_run.call_args
            self.assertEqual(call_kwargs.kwargs.get("timeout"), 5)


class TestValidateGitWorkingTree(unittest.TestCase):
    def test_passes_inside_git_repo(self):
        import subprocess as sp
        with tempfile.TemporaryDirectory() as tmpdir:
            sp.run(["git", "init", "-q"], cwd=tmpdir)
            validate_git_working_tree(tmpdir)

    def test_exits_when_not_inside_git_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                validate_git_working_tree(tmpdir)

    def test_exits_with_clear_message_for_non_git_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit) as cm:
                validate_git_working_tree(tmpdir)
            self.assertIn("not inside a git working tree", str(cm.exception))


class TestResolveExecutable(unittest.TestCase):
    def test_returns_direct_which_match(self):
        from unittest.mock import patch
        with patch("orchestrator.shutil.which", return_value="/usr/bin/copilot"):
            resolved = _resolve_executable("copilot")
        self.assertEqual(resolved, "/usr/bin/copilot")

    def test_windows_falls_back_to_cmd_suffix(self):
        from unittest.mock import patch

        def fake_which(name, path=None):
            if name == "copilot":
                return None
            if name == "copilot.cmd":
                return r"C:\tools\copilot.cmd"
            return None

        with patch("orchestrator.shutil.which", side_effect=fake_which):
            with patch("orchestrator.os.name", "nt"):
                resolved = _resolve_executable("copilot")
        self.assertEqual(resolved, r"C:\tools\copilot.cmd")

    def test_returns_original_when_unresolved(self):
        from unittest.mock import patch
        with patch("orchestrator.shutil.which", return_value=None):
            resolved = _resolve_executable("missing-cli")
        self.assertEqual(resolved, "missing-cli")


class TestWindowsPs1ProviderLaunch(unittest.TestCase):
    def test_provider_wraps_ps1_with_pwsh(self):
        from unittest.mock import patch, Mock

        cfg = {
            "name": "copilot",
            "command": "copilot --allow-all-tools -p {{TASK}}",
            "env": {},
            "rate_limit_patterns": [],
        }
        provider = Provider(cfg)

        fake_process = Mock()
        fake_process.pid = 12345

        def fake_which(name, path=None):
            if name == "copilot":
                return r"C:\tools\copilot.ps1"
            if name == "pwsh":
                return r"C:\Program Files\PowerShell\7\pwsh.exe"
            return None

        with patch("orchestrator.os.name", "nt"):
            with patch("orchestrator.shutil.which", side_effect=fake_which):
                with patch("orchestrator.subprocess.Popen", return_value=fake_process) as mock_popen:
                    with patch.object(Provider, "_wait_for_result", return_value=(0, "", False)):
                        provider.run("task prompt", ".")

        launch_cmd = mock_popen.call_args.args[0]
        self.assertEqual(launch_cmd[0], r"C:\Program Files\PowerShell\7\pwsh.exe")
        self.assertEqual(launch_cmd[1:5], ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"])
        self.assertEqual(launch_cmd[5], r"C:\tools\copilot.ps1")


class TestPythonAliasResolution(unittest.TestCase):
    """_resolve_executable should fall back between 'python'/'python3' (and
    finally to sys.executable) since only one name is guaranteed to exist
    on any given OS."""

    def test_python_falls_back_to_python3(self):
        from unittest.mock import patch

        def fake_which(name, path=None):
            return "/usr/bin/python3" if name == "python3" else None

        with patch("orchestrator.shutil.which", side_effect=fake_which):
            resolved = _resolve_executable("python")
        self.assertEqual(resolved, "/usr/bin/python3")

    def test_python3_falls_back_to_python(self):
        from unittest.mock import patch

        def fake_which(name, path=None):
            return "/usr/bin/python" if name == "python" else None

        with patch("orchestrator.shutil.which", side_effect=fake_which):
            resolved = _resolve_executable("python3")
        self.assertEqual(resolved, "/usr/bin/python")

    def test_falls_back_to_sys_executable_when_neither_on_path(self):
        from unittest.mock import patch

        with patch("orchestrator.shutil.which", return_value=None):
            resolved = _resolve_executable("python")
        self.assertEqual(resolved, sys.executable)

    def test_non_python_executable_unaffected(self):
        from unittest.mock import patch
        with patch("orchestrator.shutil.which", return_value=None):
            resolved = _resolve_executable("kilo")
        self.assertEqual(resolved, "kilo")

    def test_resolve_shell_python_rewrites_leading_token(self):
        from unittest.mock import patch

        def fake_which(name, path=None):
            return "/usr/bin/python3" if name == "python3" else None

        with patch("orchestrator.shutil.which", side_effect=fake_which):
            rewritten = _resolve_shell_python("python -m unittest discover -s tests")
        self.assertEqual(rewritten, "/usr/bin/python3 -m unittest discover -s tests")

    def test_resolve_shell_python_leaves_non_python_commands_alone(self):
        rewritten = _resolve_shell_python("make test")
        self.assertEqual(rewritten, "make test")


class TestInitCommand(unittest.TestCase):
    def test_init_creates_expected_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = cmd_init(tmpdir)
            self.assertEqual(result, 0)
            self.assertTrue((Path(tmpdir) / "config.json").exists())
            self.assertTrue((Path(tmpdir) / "Todo.md").exists())
            self.assertTrue((Path(tmpdir) / "prompts" / "task_prompt.txt").exists())
            self.assertTrue((Path(tmpdir) / ".gitignore").exists())

            config = json.loads((Path(tmpdir) / "config.json").read_text())
            self.assertIn("providers", config)
            self.assertIn("verify_commands", config)

    def test_init_does_not_overwrite_existing_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            todo_path = Path(tmpdir) / "Todo.md"
            todo_path.write_text("- [ ] My existing task\n")

            cmd_init(tmpdir)

            self.assertEqual(todo_path.read_text(), "- [ ] My existing task\n")

    def test_init_appends_missing_gitignore_entries_without_duplicating(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            gitignore_path = Path(tmpdir) / ".gitignore"
            gitignore_path.write_text("state.json\n")

            cmd_init(tmpdir)

            content = gitignore_path.read_text()
            self.assertEqual(content.count("state.json"), 1)
            self.assertIn("*.lock", content)


class TestValidateCommand(unittest.TestCase):
    def test_validate_missing_config_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = cmd_validate(Path(tmpdir) / "config.json")
        self.assertEqual(result, 1)

    def test_validate_passes_for_well_formed_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import subprocess as sp
            sp.run(["git", "init", "-q"], cwd=tmpdir)
            sp.run(["git", "config", "user.email", "tests@example.com"], cwd=tmpdir)
            sp.run(["git", "config", "user.name", "Tests"], cwd=tmpdir)

            todo_path = Path(tmpdir) / "Todo.md"
            todo_path.write_text("- [ ] A task\n")
            cfg_path = Path(tmpdir) / "config.json"
            cfg_path.write_text(json.dumps({
                "todo_file": str(todo_path),
                "working_directory": tmpdir,
                "providers": [{"name": "p", "command": "python3 -c \"print(1)\"", "env": {}, "rate_limit_patterns": []}],
            }))

            result = cmd_validate(cfg_path)
        self.assertEqual(result, 0)

    def test_validate_flags_unresolvable_provider_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import subprocess as sp
            sp.run(["git", "init", "-q"], cwd=tmpdir)

            todo_path = Path(tmpdir) / "Todo.md"
            todo_path.write_text("- [ ] A task\n")
            cfg_path = Path(tmpdir) / "config.json"
            cfg_path.write_text(json.dumps({
                "todo_file": str(todo_path),
                "working_directory": tmpdir,
                "providers": [{"name": "p", "command": "definitely-not-a-real-binary-xyz", "env": {}, "rate_limit_patterns": []}],
            }))

            result = cmd_validate(cfg_path)
        self.assertEqual(result, 1)


class TestEnvVarInterpolation(unittest.TestCase):
    def test_dollar_brace_form_substituted(self):
        os.environ["ORCH_TEST_VAR_A"] = "hello"
        try:
            self.assertEqual(_interpolate_env_vars("value: ${ORCH_TEST_VAR_A}"), "value: hello")
        finally:
            del os.environ["ORCH_TEST_VAR_A"]

    def test_bare_dollar_form_substituted(self):
        os.environ["ORCH_TEST_VAR_B"] = "world"
        try:
            self.assertEqual(_interpolate_env_vars("value: $ORCH_TEST_VAR_B"), "value: world")
        finally:
            del os.environ["ORCH_TEST_VAR_B"]

    def test_unresolved_var_left_literal(self):
        self.assertNotIn("ORCH_TEST_VAR_UNSET_XYZ", os.environ)
        result = _interpolate_env_vars("value: ${ORCH_TEST_VAR_UNSET_XYZ}")
        self.assertEqual(result, "value: ${ORCH_TEST_VAR_UNSET_XYZ}")

    def test_recurses_into_nested_dicts_and_lists(self):
        os.environ["ORCH_TEST_VAR_C"] = "secretvalue"
        try:
            config = {"providers": [{"env": {"KEY": "$ORCH_TEST_VAR_C"}}]}
            result = _interpolate_env_vars(config)
            self.assertEqual(result["providers"][0]["env"]["KEY"], "secretvalue")
        finally:
            del os.environ["ORCH_TEST_VAR_C"]


class TestDeepMerge(unittest.TestCase):
    def test_override_wins_on_scalar_conflict(self):
        self.assertEqual(_deep_merge({"a": 1}, {"a": 2}), {"a": 2})

    def test_nested_dicts_merge_recursively(self):
        base = {"nested": {"x": 1, "y": 2}}
        override = {"nested": {"y": 99}}
        self.assertEqual(_deep_merge(base, override), {"nested": {"x": 1, "y": 99}})

    def test_lists_are_replaced_not_merged(self):
        base = {"providers": ["global-provider"]}
        override = {"providers": ["project-provider"]}
        self.assertEqual(_deep_merge(base, override), {"providers": ["project-provider"]})

    def test_keys_only_in_base_are_kept(self):
        self.assertEqual(_deep_merge({"a": 1}, {}), {"a": 1})


class TestGlobalConfigMerge(unittest.TestCase):
    def test_load_config_merges_global_config_under_project_config(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmpdir:
            global_cfg_path = Path(tmpdir) / "global-config.json"
            global_cfg_path.write_text(json.dumps({
                "delay_seconds": 999,
                "providers": [{"name": "global-p", "command": "echo hi", "env": {}, "rate_limit_patterns": []}],
            }))
            project_cfg_path = Path(tmpdir) / "config.json"
            project_cfg_path.write_text(json.dumps({
                "todo_file": "Todo.md",
                "providers": [{"name": "project-p", "command": "echo hi", "env": {}, "rate_limit_patterns": []}],
            }))

            with patch("orchestrator.GLOBAL_CONFIG_PATH", global_cfg_path):
                config = load_config(project_cfg_path)

        self.assertEqual(config["delay_seconds"], 999)  # only in global -> inherited
        self.assertEqual(config["providers"][0]["name"], "project-p")  # project replaces list wholesale

    def test_load_config_without_global_config_present(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_global = Path(tmpdir) / "does-not-exist.json"
            project_cfg_path = Path(tmpdir) / "config.json"
            project_cfg_path.write_text(json.dumps({
                "todo_file": "Todo.md",
                "providers": [{"name": "p", "command": "echo hi", "env": {}, "rate_limit_patterns": []}],
            }))
            with patch("orchestrator.GLOBAL_CONFIG_PATH", missing_global):
                config = load_config(project_cfg_path)
        self.assertEqual(config["providers"][0]["name"], "p")


class TestSecretMasking(unittest.TestCase):
    def setUp(self):
        _secret_values.clear()

    def tearDown(self):
        _secret_values.clear()

    def test_register_secrets_picks_up_key_like_env_vars(self):
        provider = Provider({
            "name": "p", "command": "echo hi",
            "env": {"MY_API_KEY": "topsecret123", "HARMLESS_FLAG": "true"},
            "rate_limit_patterns": [],
        })
        _register_secrets([provider])
        self.assertIn("topsecret123", _secret_values)
        self.assertNotIn("true", _secret_values)

    def test_mask_secrets_redacts_registered_values(self):
        _secret_values.add("topsecret123")
        masked = _mask_secrets("the response included topsecret123 in plain text")
        self.assertNotIn("topsecret123", masked)
        self.assertIn("REDACTED", masked)

    def test_mask_secrets_noop_when_nothing_registered(self):
        self.assertEqual(_mask_secrets("nothing sensitive here"), "nothing sensitive here")

    def test_log_redacts_secret_in_message(self):
        from unittest.mock import patch
        provider = Provider({
            "name": "p", "command": "echo hi",
            "env": {"SOME_TOKEN": "shh-dont-tell"},
            "rate_limit_patterns": [],
        })
        _register_secrets([provider])
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("orchestrator.LOG_DIR", Path(tmpdir)):
                from orchestrator import log
                log("leaked value: shh-dont-tell")
                logged = (Path(tmpdir) / "orchestrator.log").read_text()
        self.assertNotIn("shh-dont-tell", logged)
        self.assertIn("REDACTED", logged)


class TestAtomicStateWrite(unittest.TestCase):
    def test_save_state_writes_via_temp_file_and_replace(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            with patch("orchestrator.STATE_PATH", state_path):
                save_state({"provider_cooldowns": {"p": 123}})
            self.assertTrue(state_path.exists())
            self.assertEqual(json.loads(state_path.read_text())["provider_cooldowns"]["p"], 123)
            # no leftover temp file
            self.assertFalse((Path(tmpdir) / "state.json.tmp").exists())

    def test_save_state_overwrites_existing_file_atomically(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"provider_cooldowns": {"old": 1}}))
            with patch("orchestrator.STATE_PATH", state_path):
                save_state({"provider_cooldowns": {"new": 2}})
            data = json.loads(state_path.read_text())
        self.assertEqual(data["provider_cooldowns"], {"new": 2})


class TestNewCliFlags(unittest.TestCase):
    def test_provider_flag_restricts_to_named_provider(self):
        from unittest.mock import patch
        from orchestrator import main

        with tempfile.TemporaryDirectory() as tmpdir:
            todo = Path(tmpdir) / "Todo.md"
            todo.write_text("- [ ] Task one\n")
            cfg_path = Path(tmpdir) / "config.json"
            cfg_path.write_text(json.dumps({
                "todo_file": str(todo),
                "providers": [
                    {"name": "p1", "command": "python3 -c \"print(1)\"", "env": {}, "rate_limit_patterns": []},
                    {"name": "p2", "command": "python3 -c \"print(2)\"", "env": {}, "rate_limit_patterns": []},
                ],
            }))
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"provider_cooldowns": {}}))
            pid_path = Path(tmpdir) / "orchestrator.pid"

            argv = ["orchestrator.py", "--config", str(cfg_path), "--dry-run", "--provider", "p2"]
            with patch.object(sys, "argv", argv):
                with patch("orchestrator.log") as mock_log:
                    with patch("orchestrator.STATE_PATH", state_path):
                        with patch("orchestrator.PID_PATH", pid_path):
                            main()
            log_output = " ".join(str(call.args[0]) for call in mock_log.call_args_list)
            self.assertIn("Forcing provider: p2", log_output)
            self.assertIn("p2", log_output)
            self.assertNotIn("would run task via provider 'p1'", log_output)

    def test_provider_flag_exits_when_name_not_found(self):
        from unittest.mock import patch
        from orchestrator import main

        with tempfile.TemporaryDirectory() as tmpdir:
            todo = Path(tmpdir) / "Todo.md"
            todo.write_text("- [ ] Task one\n")
            cfg_path = Path(tmpdir) / "config.json"
            cfg_path.write_text(json.dumps({
                "todo_file": str(todo),
                "providers": [{"name": "p1", "command": "python3 -c \"print(1)\"", "env": {}, "rate_limit_patterns": []}],
            }))
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"provider_cooldowns": {}}))
            pid_path = Path(tmpdir) / "orchestrator.pid"

            argv = ["orchestrator.py", "--config", str(cfg_path), "--dry-run", "--provider", "does-not-exist"]
            with patch.object(sys, "argv", argv):
                with patch("orchestrator.STATE_PATH", state_path):
                    with patch("orchestrator.PID_PATH", pid_path):
                        with self.assertRaises(SystemExit):
                            main()

    def test_dry_run_prompt_prints_full_prompt_without_executing(self):
        from unittest.mock import patch
        from orchestrator import main

        with tempfile.TemporaryDirectory() as tmpdir:
            todo = Path(tmpdir) / "Todo.md"
            todo.write_text("- [ ] Build the widget\n")
            prompt_path = Path(tmpdir) / "task_prompt.txt"
            prompt_path.write_text("Do this: {{TASK}}\n")
            cfg_path = Path(tmpdir) / "config.json"
            cfg_path.write_text(json.dumps({
                "todo_file": str(todo),
                "prompt_template": str(prompt_path),
                "providers": [{"name": "p", "command": "python3 -c \"print(1)\"", "env": {}, "rate_limit_patterns": []}],
            }))
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"provider_cooldowns": {}}))
            pid_path = Path(tmpdir) / "orchestrator.pid"

            argv = ["orchestrator.py", "--config", str(cfg_path), "--dry-run-prompt"]
            with patch.object(sys, "argv", argv):
                with patch("orchestrator.STATE_PATH", state_path):
                    with patch("orchestrator.PID_PATH", pid_path):
                        with patch("sys.stdout", new_callable=io.StringIO) as fake_stdout:
                            main()
            self.assertIn("Do this: Build the widget", fake_stdout.getvalue())

    def test_resume_from_skips_earlier_tasks_without_modifying_todo(self):
        from unittest.mock import patch
        from orchestrator import main

        with tempfile.TemporaryDirectory() as tmpdir:
            todo = Path(tmpdir) / "Todo.md"
            todo.write_text("- [ ] First task\n- [ ] Second task\n- [ ] Third task\n")
            cfg_path = Path(tmpdir) / "config.json"
            cfg_path.write_text(json.dumps({
                "todo_file": str(todo),
                "providers": [{"name": "p", "command": "python3 -c \"import sys; sys.exit(1)\"", "env": {}, "rate_limit_patterns": []}],
                "max_retries_per_provider": 1,
                "require_manual_confirmation": False,
                "continue_on_failure": True,
                "delay_seconds": 0,
            }))
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"provider_cooldowns": {}}))
            pid_path = Path(tmpdir) / "orchestrator.pid"

            argv = ["orchestrator.py", "--config", str(cfg_path), "--once", "--resume-from", "Second"]
            with patch.object(sys, "argv", argv):
                with patch("orchestrator.log") as mock_log:
                    with patch("orchestrator.STATE_PATH", state_path):
                        with patch("orchestrator.PID_PATH", pid_path):
                            main()

            log_output = " ".join(str(call.args[0]) for call in mock_log.call_args_list)
            self.assertIn("Resuming from task matching 'Second'", log_output)
            self.assertIn("Starting task: Second task", log_output)
            self.assertNotIn("Starting task: First task", log_output)
            # Todo.md content itself must be untouched by --resume-from
            self.assertEqual(todo.read_text(), "- [ ] First task\n- [ ] Second task\n- [ ] Third task\n")

    def test_resume_from_exits_when_no_task_matches(self):
        from unittest.mock import patch
        from orchestrator import main

        with tempfile.TemporaryDirectory() as tmpdir:
            todo = Path(tmpdir) / "Todo.md"
            todo.write_text("- [ ] First task\n")
            cfg_path = Path(tmpdir) / "config.json"
            cfg_path.write_text(json.dumps({
                "todo_file": str(todo),
                "providers": [{"name": "p", "command": "python3 -c \"print(1)\"", "env": {}, "rate_limit_patterns": []}],
            }))
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"provider_cooldowns": {}}))
            pid_path = Path(tmpdir) / "orchestrator.pid"

            argv = ["orchestrator.py", "--config", str(cfg_path), "--resume-from", "Nonexistent task text"]
            with patch.object(sys, "argv", argv):
                with patch("orchestrator.STATE_PATH", state_path):
                    with patch("orchestrator.PID_PATH", pid_path):
                        with self.assertRaises(SystemExit):
                            main()


class TestSigtermHandler(unittest.TestCase):
    def test_sigterm_handler_cleans_up_and_exits_zero(self):
        from unittest.mock import patch
        # _set_control_state is mocked too -- calling the real one would flip
        # the module-global _control_state["quit_requested"] permanently for
        # the rest of the test run, breaking every later test that calls
        # main() and checks that flag at the top of its loop.
        with patch("orchestrator._kill_process_tree") as mock_kill:
            with patch("orchestrator._shutdown_dashboard_server") as mock_shutdown:
                with patch("orchestrator._remove_pid_file") as mock_remove_pid:
                    with patch("orchestrator._set_control_state") as mock_set_control:
                        with patch("orchestrator._current_process", None):
                            with self.assertRaises(SystemExit) as ctx:
                                _sigterm_handler(15, None)
        self.assertEqual(ctx.exception.code, 0)
        mock_shutdown.assert_called_once()
        mock_remove_pid.assert_called_once()
        mock_kill.assert_not_called()
        mock_set_control.assert_called_once_with("quit_requested", True)


if __name__ == "__main__":
    unittest.main()
