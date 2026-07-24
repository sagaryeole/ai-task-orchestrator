import unittest
import tempfile
import os
import json
import time

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
    run_verification,
    STATE_PATH,
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
        p = Provider({"name": "p", "command": "echo", "env": {}, "rate_limit_patterns": []})
        state = {"provider_cooldowns": {}}
        p.mark_exhausted(state)
        self.assertFalse(p.is_available(state))
        self.assertIn("p", state["provider_cooldowns"])


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
            cfg_path.write_text(json.dumps({
                "todo_file": str(todo),
                "providers": [{"name": "dryrun-p", "command": "echo hello", "env": {}, "rate_limit_patterns": []}],
            }))
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"provider_cooldowns": {}}))

            with patch.object(sys, 'argv', ['orchestrator.py', '--config', str(cfg_path), '--dry-run']):
                with patch('orchestrator.log') as mock_log:
                    with patch('orchestrator.STATE_PATH', state_path):
                        main()
            log_output = ' '.join(str(call.args[0]) for call in mock_log.call_args_list)
            self.assertIn("Dry-run", log_output)
            self.assertIn("Task one", log_output)
            self.assertIn("dryrun-p", log_output)


class TestGitCommitGuard(unittest.TestCase):
    def test_git_commit_no_changes(self):
        cfg = {"auto_commit": True, "working_directory": "/tmp"}
        result = git_commit(cfg, "dummy")
        self.assertIsNone(result)


class TestVerifyLiveOutput(unittest.TestCase):
    def test_verify_prints_to_stderr(self):
        import io
        from unittest.mock import patch
        with patch('sys.stderr', new=io.StringIO()) as fake_err:
            with patch('orchestrator.log') as mock_log:
                run_verification({"verify_commands": ["false"], "working_directory": "/tmp"})
            stderr_output = fake_err.getvalue()
            self.assertIn("Verification FAILED", stderr_output)
            self.assertIn("false", stderr_output)


class TestSkipTask(unittest.TestCase):
    def test_skip_task_defers(self):
        from unittest.mock import patch
        from orchestrator import main

        with tempfile.TemporaryDirectory() as tmpdir:
            todo = Path(tmpdir) / "Todo.md"
            todo.write_text("- [ ] Task one\n- [ ] Task two\n")
            cfg_path = Path(tmpdir) / "config.json"
            cfg_path.write_text(json.dumps({
                "todo_file": str(todo),
                "providers": [{"name": "p", "command": "echo hello", "env": {}, "rate_limit_patterns": []}],
                "require_manual_confirmation": True,
            }))
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"provider_cooldowns": {}}))

            inputs = ["skip-task", "y", "y"]
            with patch.object(sys, 'argv', ['orchestrator.py', '--config', str(cfg_path)]):
                with patch('orchestrator.STATE_PATH', state_path):
                    with patch('builtins.input', side_effect=inputs):
                        with patch('orchestrator.time.sleep'):
                            with patch('orchestrator.log') as mock_log:
                                main()
            log_output = ' '.join(str(call.args[0]) for call in mock_log.call_args_list)
            self.assertIn("deferred", log_output)
            final_text = todo.read_text()
            self.assertIn("- [x] Task one", final_text)
            self.assertIn("- [x] Task two", final_text)


if __name__ == "__main__":
    unittest.main()
