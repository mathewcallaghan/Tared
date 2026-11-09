"""Unit tests verifying CLI flags (--help, --version) execute cleanly without graphical subsystem."""

import subprocess
import sys
import unittest
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[2] / "src"


class TestCLISmoke(unittest.TestCase):
    def test_cli_help_flag_exits_cleanly(self) -> None:
        res = subprocess.run(
            [sys.executable, "-m", "tared", "--help"],
            cwd=str(SRC_DIR.parent),
            env={"PYTHONPATH": str(SRC_DIR)},
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"CLI --help failed:\n{res.stderr}")
        self.assertIn("Precision recipe formulation", res.stdout)
        self.assertIn("--help", res.stdout)

    def test_cli_version_flag_exits_cleanly(self) -> None:
        res = subprocess.run(
            [sys.executable, "-m", "tared", "--version"],
            cwd=str(SRC_DIR.parent),
            env={"PYTHONPATH": str(SRC_DIR)},
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"CLI --version failed:\n{res.stderr}")
        self.assertIn("tared", res.stdout)


if __name__ == "__main__":
    unittest.main()
