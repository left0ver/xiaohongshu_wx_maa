import unittest
from types import SimpleNamespace
from unittest import mock

import foreground as fg


class TestForegroundGuard(unittest.TestCase):
    def setUp(self):
        fg.invalidate_foreground_cache()

    def tearDown(self):
        fg.invalidate_foreground_cache()

    def test_force_bypasses_cache_and_is_wechat_foreground_checks_live_state(self):
        with mock.patch.object(
            fg,
            "run",
            side_effect=[
                "mResumedActivity: ActivityRecord{1 u0 com.tencent.mm/.ui.LauncherUI t1}",
                "mResumedActivity: ActivityRecord{2 u0 com.android.incallui/.InCallActivity t2}",
            ],
        ) as mocked_run:
            # First call populates cache.
            fg.foreground(timeout=1)
            # Cached call does not re-run dumpsys.
            fg.foreground(timeout=1)
            self.assertEqual(mocked_run.call_count, 1)
            # force=True bypasses cache and observes InCallActivity.
            self.assertFalse(fg.is_wechat_foreground(timeout=1, force=True))
            self.assertEqual(mocked_run.call_count, 2)

    def test_ensure_wechat_foreground_skips_launch_when_already_in_front(self):
        try:
            import android_ui
        except ImportError:
            self.skipTest("maa runtime not installed on host")

        ui = android_ui.MaaUi.__new__(android_ui.MaaUi)
        ui.tasker = SimpleNamespace(stopping=False)
        ui.controller = mock.MagicMock()
        emitted = []

        with mock.patch.object(android_ui, "is_wechat_foreground", return_value=True), mock.patch.object(
            android_ui, "command"
        ) as mocked_cmd:
            self.assertTrue(ui.ensure_wechat_foreground(reset_to_home=False, emit=emitted.append))
            mocked_cmd.assert_not_called()
            self.assertEqual(len(emitted), 1)
            self.assertTrue(emitted[0]["wasInForeground"])
            self.assertFalse(emitted[0]["launched"])

    def test_ensure_wechat_foreground_auto_launches_from_other_app_and_wakes_screen(self):
        try:
            import android_ui
        except ImportError:
            self.skipTest("maa runtime not installed on host")

        ui = android_ui.MaaUi.__new__(android_ui.MaaUi)
        ui.tasker = SimpleNamespace(stopping=False)
        ui.controller = mock.MagicMock()
        ui.controls = []
        ui.controls_at = 0.0
        ui.controls_frame_file = None
        ui.hierarchy_unavailable = True
        ui.navigation_stage = "old"
        emitted = []

        fg_calls = iter(
            [
                RuntimeError("微信不在前台；未执行操作：mResumedActivity: ActivityRecord{1 u0 com.android.incallui/.InCallActivity t1}"),
                None,
            ]
        )

        def fake_foreground(timeout=15, force=False):
            result = next(fg_calls)
            if isinstance(result, Exception):
                raise result

        def fake_command(*args, **kwargs):
            if args[:2] == ("dumpsys", "power"):
                return b"mWakefulness=Asleep"
            return b""

        with mock.patch.object(android_ui, "is_wechat_foreground", return_value=False), mock.patch.object(
            android_ui, "foreground", side_effect=fake_foreground
        ), mock.patch.object(android_ui, "command", side_effect=fake_command) as mocked_cmd, mock.patch.object(
            android_ui.time, "sleep"
        ):
            self.assertTrue(ui.ensure_wechat_foreground(reset_to_home=False, timeout=5.0, emit=emitted.append))

        calls = [c.args for c in mocked_cmd.call_args_list]
        self.assertIn(("input", "keyevent", "224"), calls)
        self.assertIn(("am", "start", "-n", "com.tencent.mm/.ui.LauncherUI"), calls)
        self.assertEqual(len(emitted), 1)
        self.assertFalse(emitted[0]["wasInForeground"])
        self.assertTrue(emitted[0]["launched"])
        self.assertFalse(ui.hierarchy_unavailable)

    def test_ensure_wechat_foreground_escalates_and_reports_blocking_activity_on_timeout(self):
        try:
            import android_ui
        except ImportError:
            self.skipTest("maa runtime not installed on host")

        ui = android_ui.MaaUi.__new__(android_ui.MaaUi)
        ui.tasker = SimpleNamespace(stopping=False)
        job = mock.MagicMock()
        job.wait.return_value = SimpleNamespace(succeeded=True)
        ui.controller = mock.MagicMock()
        ui.controller.post_start_app.return_value = job
        ui.controls = []
        ui.controls_at = 0.0
        ui.controls_frame_file = None

        clock = [0.0]

        def fake_monotonic():
            return clock[0]

        def fake_sleep(seconds):
            clock[0] += max(seconds, 1.0)

        with mock.patch.object(android_ui, "is_wechat_foreground", return_value=False), mock.patch.object(
            android_ui,
            "foreground",
            side_effect=RuntimeError("微信不在前台；未执行操作：mResumedActivity: ActivityRecord{7 u0 com.android.incallui/.InCallActivity t895}"),
        ), mock.patch.object(android_ui, "command", return_value=b"mWakefulness=Awake"), mock.patch.object(
            android_ui.time, "monotonic", side_effect=fake_monotonic
        ), mock.patch.object(
            android_ui.time, "sleep", side_effect=fake_sleep
        ):
            with self.assertRaises(RuntimeError) as ctx:
                ui.ensure_wechat_foreground(reset_to_home=True, timeout=10.0)
            self.assertIn("自动打开微信超时", str(ctx.exception))
            self.assertIn("InCallActivity", str(ctx.exception))
            ui.controller.post_start_app.assert_called_with("com.tencent.mm")


if __name__ == "__main__":
    unittest.main()
