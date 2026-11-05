"""Command boundary: binary capture, failed probes and bounded termination."""

import subprocess
import sys
import unittest

from commands import run


class CommandTests(unittest.TestCase):
    def test_binary_output_is_not_decoded(self):
        self.assertEqual(run([sys.executable, "-c", "import sys; sys.stdout.buffer.write(bytes([0, 255, 10]))"],
                             binary=True), bytes([0, 255, 10]))

    def test_failed_probe_keeps_diagnostic_stderr(self):
        with self.assertRaises(subprocess.CalledProcessError) as error:
            run([sys.executable, "-c", "import sys; sys.stderr.write('probe failed'); sys.exit(2)"])
        stderr = error.exception.stderr
        self.assertIn("probe failed", stderr.decode() if isinstance(stderr, bytes) else stderr)

    def test_expired_and_slow_probe_are_terminated(self):
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        for budget in (0, 0.05):
            with self.subTest(budget=budget), self.assertRaises(subprocess.TimeoutExpired):
                run(command, timeout=budget)


if __name__ == "__main__":
    unittest.main()
