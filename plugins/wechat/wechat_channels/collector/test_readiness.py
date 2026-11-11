"""Readiness budgets and propagation of cancellation/foreground errors."""

import subprocess
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from readiness import framework_directory, wait_basic_info
from test_collection import INFO


class Clock:
    """Monotonic time that only moves when the code sleeps or a reader takes time."""

    def __init__(self, now=100.0):
        self.now = now
        self.patches = [patch("readiness.time.monotonic", self.monotonic), patch("readiness.time.sleep", self.sleep)]

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def taking(self, seconds, read):
        def timed(*args):
            self.now += seconds
            return read(*args)
        return timed

    def __enter__(self):
        for item in self.patches:
            item.start()
        return self

    def __exit__(self, *exc):
        for item in self.patches:
            item.stop()


class ReadinessTests(unittest.TestCase):
    def test_framework_can_be_provided_by_host_instead_of_agent_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent, native = root / "agent", root / "native"
            agent.mkdir()
            native.mkdir()
            (agent / "libMaaAgentServer.so").touch()
            (native / "libMaaFramework.so").touch()
            # Android search paths use ':'. Relative test paths avoid Windows
            # drive letters being mistaken for an Android path separator.
            with patch("readiness.Path") as paths:
                paths.side_effect = lambda value: agent if value == "agent" else native
                self.assertEqual(framework_directory({"MAAFW_BINARY_PATH": "agent", "LD_LIBRARY_PATH": "agent:native"}), native)

    def test_missing_framework_fails_before_starting_recognition(self):
        with self.assertRaisesRegex(RuntimeError, "Native MaaFramework"):
            framework_directory({})

    def test_ready_semantics_skip_ocr(self):
        ocr = Mock()
        with Clock():
            self.assertEqual(wait_basic_info(lambda _: INFO, ocr), (INFO, "hierarchy"))
        ocr.assert_not_called()

    def test_panel_that_loads_after_the_click_is_read_from_controls(self):
        reads = iter([[], [], INFO])
        ocr = Mock()
        with Clock() as clock:
            self.assertEqual(wait_basic_info(clock.taking(0.5, lambda _: next(reads)), ocr), (INFO, "hierarchy"))
        ocr.assert_not_called()

    def test_empty_tree_gives_ocr_only_remaining_budget(self):
        ocr = Mock(return_value=INFO)
        with Clock() as clock:
            self.assertEqual(wait_basic_info(clock.taking(1.2, lambda _: []), ocr), (INFO, "local-ocr"))
        # Controls are re-read for at most half the budget: 3 reads and 2 pauses.
        self.assertAlmostEqual(ocr.call_args.args[0], 3.8)

    def test_skipped_probe_goes_straight_to_ocr(self):
        error = ValueError("probe skipped")
        error.skipped = True
        semantic, ocr = Mock(side_effect=error), Mock(return_value=INFO)
        with Clock():
            self.assertEqual(wait_basic_info(semantic, ocr)[1], "local-ocr")
        semantic.assert_called_once()
        self.assertAlmostEqual(ocr.call_args.args[0], 8)

    def test_semantic_timeout_can_use_remaining_budget(self):
        with Clock() as clock:
            semantic = clock.taking(1.2, Mock(side_effect=subprocess.TimeoutExpired("dump", 1.2)))
            ocr = Mock(return_value=INFO)
            self.assertEqual(wait_basic_info(semantic, ocr)[1], "local-ocr")
        self.assertAlmostEqual(ocr.call_args.args[0], 6.8)

    def test_expired_budget_does_not_start_ocr(self):
        ocr = Mock()
        with Clock() as clock:
            with self.assertRaisesRegex(ValueError, "within 8 seconds"):
                wait_basic_info(clock.taking(8, lambda _: INFO), ocr)
        ocr.assert_not_called()

    def test_late_success_is_not_accepted(self):
        error = ValueError("probe skipped")
        error.skipped = True
        with Clock() as clock:
            with self.assertRaisesRegex(ValueError, "within 8 seconds"):
                wait_basic_info(Mock(side_effect=error), clock.taking(9, lambda _: INFO))

    def test_ocr_timeout_is_not_retried_with_a_fresh_budget(self):
        ocr = Mock(side_effect=subprocess.TimeoutExpired("ocr", 3.8))
        with Clock() as clock:
            with self.assertRaisesRegex(ValueError, "within 8 seconds"):
                wait_basic_info(clock.taking(1.2, lambda _: []), ocr)
        self.assertEqual(ocr.call_count, 1)

    def test_cancellation_and_wrong_foreground_do_not_fall_back(self):
        for error in (InterruptedError("stopped"), RuntimeError("wrong foreground")):
            ocr = Mock()
            with self.subTest(error=error), self.assertRaises(type(error)):
                wait_basic_info(Mock(side_effect=error), ocr)
            ocr.assert_not_called()


if __name__ == "__main__":
    unittest.main()
