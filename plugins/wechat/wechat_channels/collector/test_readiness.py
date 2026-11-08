"""Readiness budgets and propagation of cancellation/foreground errors."""

import subprocess
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from readiness import framework_directory, wait_basic_info
from test_collection import INFO


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
        with patch("readiness.time.monotonic", return_value=100):
            self.assertEqual(wait_basic_info(lambda _: INFO, ocr), (INFO, "hierarchy"))
        ocr.assert_not_called()

    def test_empty_tree_gives_ocr_only_remaining_budget(self):
        ocr = Mock(return_value=INFO)
        with patch("readiness.time.monotonic", side_effect=[100, 100, 101.2, 101.2, 107.9]):
            self.assertEqual(wait_basic_info(lambda _: [], ocr), (INFO, "local-ocr"))
        self.assertAlmostEqual(ocr.call_args.args[0], 6.8)

    def test_semantic_timeout_can_use_remaining_budget(self):
        semantic = Mock(side_effect=subprocess.TimeoutExpired("dump", 1.2))
        with patch("readiness.time.monotonic", side_effect=[100, 100, 101.2, 104]):
            self.assertEqual(wait_basic_info(semantic, lambda _: INFO)[1], "local-ocr")

    def test_expired_budget_does_not_start_ocr(self):
        ocr = Mock()
        with patch("readiness.time.monotonic", side_effect=[100, 100, 108, 108]):
            with self.assertRaisesRegex(ValueError, "within 8 seconds"):
                wait_basic_info(lambda _: INFO, ocr)
        ocr.assert_not_called()

    def test_late_success_is_not_accepted(self):
        with patch("readiness.time.monotonic", side_effect=[100, 100, 101, 101, 109]):
            with self.assertRaisesRegex(ValueError, "within 8 seconds"):
                wait_basic_info(lambda _: [], lambda _: INFO)

    def test_ocr_timeout_is_not_retried_with_a_fresh_budget(self):
        ocr = Mock(side_effect=subprocess.TimeoutExpired("ocr", 3.8))
        with patch("readiness.time.monotonic", side_effect=[100, 100, 101.2, 101.2]):
            with self.assertRaisesRegex(ValueError, "within 8 seconds"):
                wait_basic_info(lambda _: [], ocr)
        self.assertEqual(ocr.call_count, 1)

    def test_cancellation_and_wrong_foreground_do_not_fall_back(self):
        for error in (InterruptedError("stopped"), RuntimeError("wrong foreground")):
            ocr = Mock()
            with self.subTest(error=error), self.assertRaises(type(error)):
                wait_basic_info(Mock(side_effect=error), ocr)
            ocr.assert_not_called()


if __name__ == "__main__":
    unittest.main()
