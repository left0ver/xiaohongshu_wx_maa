import sys
from pathlib import Path
import subprocess
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "xiaohongshu" / "agent"))
from commands import run


class CommandTests(unittest.TestCase):
    def test_output_and_literal_arguments(self):
        self.assertEqual(run(["echo", "$(no execution); literal"]), "$(no execution); literal\n")

    def test_failure_and_missing_command(self):
        with self.assertRaises(subprocess.CalledProcessError):
            run(["false"])
        with self.assertRaises((OSError, subprocess.CalledProcessError)):
            run(["xhs-nonexistent-command"])

    def test_timeout(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run(["sleep", "2"], timeout=0.1)

    @unittest.skipUnless(hasattr(sys, "getandroidapilevel"), "Android spawn cancellation")
    def test_cancel_and_repeated_spawn(self):
        with self.assertRaises(InterruptedError):
            run(["sleep", "2"], stopped=lambda: True)
        for _ in range(30):
            self.assertEqual(run(["echo", "ok"]), "ok\n")


if __name__ == "__main__":
    unittest.main()
