"""Select to Speak is enabled only for a run and the user's settings always come back."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from accessibility import ENABLED_KEY, MARKER, SERVICE, SERVICES_KEY, expose_controls

OTHER = "com.example/.Reader"


class Phone:
    """The `pm` and `settings` commands the bridge uses, over an in-memory store."""

    def __init__(self, services=None, enabled=None, installed=True, fail_put=False):
        self.secure = {SERVICES_KEY: services, ENABLED_KEY: enabled}
        self.installed = installed
        self.fail_put = fail_put
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append(args)
        if args[:2] == ["pm", "path"]:
            if not self.installed:
                raise subprocess.CalledProcessError(1, args)
            return "package:/product/app/talkback/talkback.apk\n"
        command, _, key, *value = args[1:]
        if command == "get":
            stored = self.secure.get(key)
            return ("null" if stored is None else stored) + "\n"
        if self.fail_put:
            raise subprocess.CalledProcessError(255, args)
        self.secure[key] = value[0] if command == "put" else None
        return ""


class AccessibilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.events = []

    def tearDown(self):
        self.directory.cleanup()

    def run_bridge(self, phone, body=lambda exposed: None):
        with expose_controls(self.root, self.events.append, runner=phone, sleep=lambda _: None) as exposed:
            during = dict(phone.secure)
            body(exposed)
        return exposed, during

    def statuses(self):
        return [event["status"] for event in self.events]

    def test_enabled_for_the_run_and_restored_after(self):
        phone = Phone()
        exposed, during = self.run_bridge(phone)
        self.assertTrue(exposed)
        self.assertEqual(during, {SERVICES_KEY: SERVICE, ENABLED_KEY: "1"})
        self.assertEqual(phone.secure, {SERVICES_KEY: None, ENABLED_KEY: None})
        self.assertFalse((self.root / MARKER).exists())
        self.assertEqual(self.statuses(), ["enabled", "restored"])

    def test_services_the_user_enabled_are_kept(self):
        phone = Phone(services=OTHER, enabled="1")
        _, during = self.run_bridge(phone)
        self.assertEqual(during[SERVICES_KEY], OTHER + ":" + SERVICE)
        self.assertEqual(phone.secure, {SERVICES_KEY: OTHER, ENABLED_KEY: "1"})

    def test_select_to_speak_the_user_enabled_is_not_taken_over(self):
        phone = Phone(services=SERVICE, enabled="1")
        exposed, _ = self.run_bridge(phone)
        self.assertTrue(exposed)
        self.assertFalse(any(call[1] in ("put", "delete") for call in phone.calls if call[0] == "settings"))
        self.assertEqual(self.statuses(), ["already-enabled"])

    def test_missing_suite_leaves_settings_alone_and_falls_back_to_ocr(self):
        phone = Phone(installed=False)
        exposed, _ = self.run_bridge(phone)
        self.assertFalse(exposed)
        self.assertEqual([call[0] for call in phone.calls], ["pm"])
        self.assertEqual(self.statuses(), ["unavailable"])

    def test_failed_run_still_restores_settings(self):
        phone = Phone(services=OTHER, enabled="1")

        def fail(_):
            raise ValueError("collection failed")

        with self.assertRaisesRegex(ValueError, "collection failed"):
            self.run_bridge(phone, fail)
        self.assertEqual(phone.secure, {SERVICES_KEY: OTHER, ENABLED_KEY: "1"})
        self.assertFalse((self.root / MARKER).exists())

    def test_settings_left_by_a_killed_run_are_restored_first(self):
        # The killed run had enabled the service over the user's own reader.
        phone = Phone(services=OTHER + ":" + SERVICE, enabled="1")
        (self.root / MARKER).write_text(json.dumps({"services": OTHER, "enabled": "1"}))
        _, during = self.run_bridge(phone)
        self.assertEqual(during[SERVICES_KEY], OTHER + ":" + SERVICE)
        self.assertEqual(phone.secure, {SERVICES_KEY: OTHER, ENABLED_KEY: "1"})
        self.assertEqual(self.statuses(), ["recovered", "enabled", "restored"])

    def test_unreadable_marker_is_discarded(self):
        (self.root / MARKER).write_text("{")
        exposed, _ = self.run_bridge(Phone())
        self.assertTrue(exposed)
        self.assertFalse((self.root / MARKER).exists())

    def test_settings_failure_does_not_fail_the_run(self):
        phone = Phone(fail_put=True)
        exposed, _ = self.run_bridge(phone)
        self.assertFalse(exposed)
        self.assertEqual(self.statuses(), ["failed", "restore-failed"])
        # The marker lets the next run put the original settings back.
        self.assertEqual(json.loads((self.root / MARKER).read_text()), {"services": None, "enabled": None})


if __name__ == "__main__":
    unittest.main()
