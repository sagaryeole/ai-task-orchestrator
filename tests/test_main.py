import subprocess
import sys
import unittest


class TestMainSmoke(unittest.TestCase):
    """Smoke tests for `python -m task_orchestrator` entry point."""

    def test_help_exits_zero_and_prints_banner(self):
        result = subprocess.run(
            [sys.executable, "-m", "task_orchestrator", "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(
            "Task Orchestrator",
            result.stdout,
        )
        self.assertIn(
            "drives a coding-agent CLI through a task backlog.",
            result.stdout,
        )


if __name__ == "__main__":
    unittest.main()
