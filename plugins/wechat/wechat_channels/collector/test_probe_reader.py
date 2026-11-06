"""Read-only worker lifetime and deadline handling without Android or Maa."""

import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from probe_reader import prepared_reader

WORKER = '''import json, sys, time
from pathlib import Path
output = Path(sys.argv[1])
(output.parent / "prepared").touch()
while not (output.parent / "request").exists():
    time.sleep(0.005)
{action}
'''


class ProbeReaderTests(unittest.TestCase):
    def script(self, root, action):
        path = Path(root) / "worker.py"
        path.write_text(WORKER.format(action=action), encoding="utf-8")
        return path

    def test_prepared_reader_returns_one_atomic_result(self):
        with tempfile.TemporaryDirectory() as directory:
            script = self.script(directory, 'output.write_text(json.dumps({"lines": [], "source": "test"}))')
            with prepared_reader(script) as read:
                self.assertEqual(read(2), {"lines": [], "source": "test"})

    def test_unresponsive_reader_is_terminated(self):
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as directory:
            script = self.script(directory, 'time.sleep(30)')
            with self.assertRaises(subprocess.TimeoutExpired):
                with prepared_reader(script) as read:
                    read(0.05)
        self.assertLess(time.monotonic() - started, 3)

    def test_cancelled_collection_does_not_start_a_capture_request(self):
        with tempfile.TemporaryDirectory() as directory:
            script = self.script(directory, 'time.sleep(30)')
            cancelled = [False]
            with self.assertRaises(InterruptedError):
                with prepared_reader(script, stopped=lambda: cancelled[0]) as read:
                    cancelled[0] = True
                    read(2)

    def test_crashed_reader_is_reported_without_waiting_for_page_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            script = self.script(directory, 'raise RuntimeError("reader failed")')
            with self.assertRaises(subprocess.CalledProcessError):
                with prepared_reader(script) as read:
                    read(2)


if __name__ == "__main__":
    unittest.main()
