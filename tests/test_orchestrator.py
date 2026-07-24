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
    run_verification,
    lint_config,
    lint_todo,
    get_task_timeout,
    TAG_REGEX,
    STATE_PATH,
    print_summary,
    run_provider_stats,
    count_total_tasks,
    count_completed_tasks,
    start_dashboard,
    update_dashboard_state,
    build_provider_status,
    _dashboard_state,
    _build_html,
    html_escape,
    DashboardHandler,
    DashboardServer,
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
            todo = Path(tmpdir) / "Todo.md"
            todo.write_text("- [ ] Task one\n")
            cfg_path = Path(tmpdir) / "config.json"
            cfg_path.write_text(json.dumps({
                "todo_file": str(todo),
                "working_directory": tmpdir,
                "require_manual_confirmation": False,
                "continue_on_failure": True,
                "providers": [{"name": "p", "command": "echo hello", "env": {}, "rate_limit_patterns": []}],
            }))
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"provider_cooldowns": {}}))

            with patch.object(sys, 'argv', ['orchestrator.py', '--config', str(cfg_path), '--once']):
                with patch('orchestrator.STATE_PATH', state_path):
                    with patch('orchestrator.time.sleep'):
                        with patch('orchestrator.log') as mock_log:
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
            (Path(tmpdir) / "existing.txt").write_text("baseline\n")
            sp.run(["git", "add", "-A"], cwd=tmpdir)
            sp.run(["git", "commit", "-q", "-m", "baseline"], cwd=tmpdir)

            todo = Path(tmpdir) / "Todo.md"
            todo.write_text("- [ ] Task one\n")
            cfg_path = Path(tmpdir) / "config.json"
            # Command actually modifies a tracked file, so git diff --stat is non-empty.
            write_cmd = f"python3 -c \"open('{tmpdir}/existing.txt', 'w').write('changed')\""
            cfg_path.write_text(json.dumps({
                "todo_file": str(todo),
                "working_directory": tmpdir,
                "require_manual_confirmation": False,
                "providers": [{"name": "p", "command": write_cmd, "env": {}, "rate_limit_patterns": []}],
            }))
            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"provider_cooldowns": {}}))

            with patch.object(sys, 'argv', ['orchestrator.py', '--config', str(cfg_path), '--once']):
                with patch('orchestrator.STATE_PATH', state_path):
                    with patch('orchestrator.time.sleep'):
                        with patch('orchestrator.log') as mock_log:
                            main()

            log_output = ' '.join(str(call.args[0]) for call in mock_log.call_args_list)
            self.assertNotIn("SUSPICIOUS", log_output)
            final_text = todo.read_text()
            self.assertIn("- [x] Task one", final_text)


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
            run_provider_stats(p, "/tmp", "Test task")
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
            run_provider_stats(p, "/tmp", "Test task")
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
                run_provider_stats(p, "/tmp", "Test task")
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
                    run_provider_stats(p, "/tmp", "Test task")
        log_msgs = [str(c.args[0]) for c in mock_log.call_args_list]
        self.assertTrue(any("timed out" in m for m in log_msgs))

    def test_stats_logs_json_payload(self):
        from unittest.mock import patch
        cfg = {
            "name": "p",
            "command": "echo hello",
            "env": {},
            "rate_limit_patterns": [],
            "stats_command": "printf '{\"tokens\": 50, \"cost\": 0.01}'",
        }
        with patch('orchestrator.log_json') as mock_json:
            p = Provider(cfg)
            run_provider_stats(p, "/tmp", "Test task")
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


if __name__ == "__main__":
    unittest.main()
