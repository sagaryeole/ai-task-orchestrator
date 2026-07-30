"""Integration test: runs a real (mock) provider end-to-end and verifies Todo.md state."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestEndToEndMockProvider(unittest.TestCase):
    """End-to-end integration test using a mock provider (python one-liner that
    creates a file, proving it ran) and verifying the orchestrator marks the
    task complete and commits."""

    def test_single_task_completes_via_mock_provider(self):
        """A mock provider that touches a file should result in task marked [x]."""
        from orchestrator import main

        with tempfile.TemporaryDirectory() as tmpdir:
            # Set up git repo
            subprocess.run(["git", "init", "-q"], cwd=tmpdir)
            subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmpdir)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=tmpdir)

            # Create project files
            todo = Path(tmpdir) / "Todo.md"
            todo.write_text("- [ ] Create hello.txt\n")

            marker = Path(tmpdir) / "hello.txt"
            target = marker.as_posix()
            # Mock provider: a python command that creates the requested file
            mock_cmd = f"python -c \"open(r'{target}', 'w').write('done')\""

            cfg_path = Path(tmpdir) / "config.json"
            cfg_path.write_text(json.dumps({
                "todo_file": str(todo),
                "working_directory": tmpdir,
                "require_manual_confirmation": False,
                "auto_commit": False,
                "delay_seconds": 0,
                "verify_commands": [],
                "providers": [{
                    "name": "mock",
                    "command": mock_cmd,
                    "env": {},
                    "rate_limit_patterns": [],
                }],
            }))

            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"provider_cooldowns": {}}))
            pid_path = Path(tmpdir) / "orchestrator.pid"

            # Commit baseline so git status is clean before the task runs
            (Path(tmpdir) / ".gitignore").write_text("orchestrator.pid\nstate.json\n")
            subprocess.run(["git", "add", "-A"], cwd=tmpdir)
            subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=tmpdir)

            with patch.object(sys, "argv", ["orchestrator.py", "run", "--config", str(cfg_path), "--once"]):
                with patch("orchestrator.STATE_PATH", state_path):
                    with patch("orchestrator.PID_PATH", pid_path):
                        with patch("orchestrator.time.sleep"):
                            main()

            # Verify task was marked complete
            final = todo.read_text()
            self.assertIn("- [x] Create hello.txt", final)

            # Verify the provider actually ran (file exists)
            self.assertTrue(marker.exists())
            self.assertEqual(marker.read_text(), "done")

    def test_failing_provider_does_not_mark_complete(self):
        """A provider that exits non-zero should NOT mark the task complete."""
        from orchestrator import main

        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init", "-q"], cwd=tmpdir)
            subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmpdir)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=tmpdir)

            todo = Path(tmpdir) / "Todo.md"
            todo.write_text("- [ ] Impossible task\n")

            cfg_path = Path(tmpdir) / "config.json"
            cfg_path.write_text(json.dumps({
                "todo_file": str(todo),
                "working_directory": tmpdir,
                "require_manual_confirmation": False,
                "auto_commit": False,
                "continue_on_failure": True,
                "delay_seconds": 0,
                "verify_commands": [],
                "providers": [{
                    "name": "failing-mock",
                    "command": "python -c \"import sys; sys.exit(1)\"",
                    "env": {},
                    "rate_limit_patterns": [],
                }],
            }))

            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"provider_cooldowns": {}}))
            pid_path = Path(tmpdir) / "orchestrator.pid"

            (Path(tmpdir) / ".gitignore").write_text("orchestrator.pid\nstate.json\n")
            subprocess.run(["git", "add", "-A"], cwd=tmpdir)
            subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=tmpdir)

            with patch.object(sys, "argv", ["orchestrator.py", "run", "--config", str(cfg_path), "--once"]):
                with patch("orchestrator.STATE_PATH", state_path):
                    with patch("orchestrator.PID_PATH", pid_path):
                        with patch("orchestrator.time.sleep"):
                            main()

            final = todo.read_text()
            self.assertIn("- [ ] Impossible task", final)
            self.assertNotIn("- [x]", final)

    def test_rate_limited_provider_rotates(self):
        """A provider whose output matches rate_limit_patterns should be marked
        exhausted, and the orchestrator should rotate to the next provider."""
        from orchestrator import main

        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init", "-q"], cwd=tmpdir)
            subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmpdir)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=tmpdir)

            todo = Path(tmpdir) / "Todo.md"
            todo.write_text("- [ ] Do something\n")

            marker = Path(tmpdir) / "done.txt"
            target = marker.as_posix()

            cfg_path = Path(tmpdir) / "config.json"
            cfg_path.write_text(json.dumps({
                "todo_file": str(todo),
                "working_directory": tmpdir,
                "require_manual_confirmation": False,
                "auto_commit": False,
                "continue_on_failure": True,
                "delay_seconds": 0,
                "verify_commands": [],
                "providers": [
                    {
                        "name": "limited",
                        "command": "python -c \"print('429 rate limit exceeded')\"",
                        "env": {},
                        "rate_limit_patterns": ["rate limit"],
                        "cooldown_seconds": 9999,
                    },
                    {
                        "name": "working",
                        "command": f"python -c \"open(r'{target}', 'w').write('ok')\"",
                        "env": {},
                        "rate_limit_patterns": [],
                    },
                ],
            }))

            state_path = Path(tmpdir) / "state.json"
            state_path.write_text(json.dumps({"provider_cooldowns": {}}))
            pid_path = Path(tmpdir) / "orchestrator.pid"

            (Path(tmpdir) / ".gitignore").write_text("orchestrator.pid\nstate.json\n")
            subprocess.run(["git", "add", "-A"], cwd=tmpdir)
            subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=tmpdir)

            with patch.object(sys, "argv", ["orchestrator.py", "run", "--config", str(cfg_path), "--once"]):
                with patch("orchestrator.STATE_PATH", state_path):
                    with patch("orchestrator.PID_PATH", pid_path):
                        with patch("orchestrator.time.sleep"):
                            main()

            # The working provider should have run and completed the task
            final = todo.read_text()
            self.assertIn("- [x] Do something", final)
            self.assertTrue(marker.exists())

            # The limited provider should be on cooldown in state
            state = json.loads(state_path.read_text())
            self.assertIn("limited", state["provider_cooldowns"])


if __name__ == "__main__":
    unittest.main()
