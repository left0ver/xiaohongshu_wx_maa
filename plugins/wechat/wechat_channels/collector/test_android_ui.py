import sys
import types
import unittest
from unittest import mock

# android_ui needs MaaFramework and numpy only at runtime on the phone.
_STUBS = {name: types.ModuleType(name) for name in
          ("numpy", "maa", "maa.controller", "maa.library", "maa.pipeline", "maa.resource", "maa.tasker")}
_STUBS["maa.controller"].CustomController = object
_STUBS["maa.library"].Library = object
_STUBS["maa.resource"].Resource = object
_STUBS["maa.tasker"].Tasker = object
for _name in ("JAnd", "JOCR", "JTemplateMatch"):
    setattr(_STUBS["maa.pipeline"], _name, object)
with mock.patch.dict(sys.modules, {name: sys.modules.get(name, stub) for name, stub in _STUBS.items()}):
    import android_ui

HEART = {"text": "点赞", "box": [276, 96, 32, 29], "navigation": "message_like",
         "templateNode": "BatchMessagesLikeTab"}
BUBBLE = {"text": "评论", "box": [411, 96, 35, 29], "navigation": "message_comment",
          "templateNode": "BatchMessagesCommentTab"}


class MessageTabsTest(unittest.TestCase):
    def tabs(self, label, lines, hits):
        ui = android_ui.MaaUi.__new__(android_ui.MaaUi)
        calls = []

        def match(name):
            calls.append(name)
            return hits.get(name)

        ui.match_navigation = match
        return ui.message_tabs(label, lines, lambda _source, read: read()), calls

    def test_icon_only_tabs_are_added_on_message_pages(self):
        hits = {"BatchMessagesLikeTab": HEART, "BatchMessagesCommentTab": BUBBLE}
        tabs, calls = self.tabs("messages-center", [{"text": "林栖前天", "box": [123, 358, 116, 36]}], hits)
        self.assertEqual(tabs, [HEART, BUBBLE])
        self.assertEqual(calls, ["BatchMessagesLikeTab", "BatchMessagesCommentTab"])

    def test_heart_is_added_next_to_comment_text(self):
        lines = [{"text": "评论", "box": [408, 94, 58, 32]}]
        tabs, calls = self.tabs("comments-check", lines, {"BatchMessagesLikeTab": HEART})
        self.assertEqual(tabs, [HEART])
        self.assertEqual(calls, ["BatchMessagesLikeTab"])

    def test_lone_heart_is_not_a_message_center(self):
        tabs, calls = self.tabs("messages-start", [], {"BatchMessagesLikeTab": HEART})
        self.assertEqual(tabs, [])
        self.assertEqual(calls, ["BatchMessagesLikeTab", "BatchMessagesCommentTab"])

    def test_other_pages_skip_template_matching(self):
        tabs, calls = self.tabs("feed", [{"text": "推荐", "box": [437, 73, 70, 37]}], {})
        self.assertEqual((tabs, calls), ([], []))

    def test_text_tabs_need_no_templates(self):
        lines = [{"text": "点赞", "box": [196, 40, 50, 30]}, {"text": "评论", "box": [330, 40, 50, 30]}]
        self.assertEqual(self.tabs("messages-center", lines, {}), ([], []))

    def test_text_tab_with_unread_badge_needs_no_templates(self):
        lines = [{"text": "点赞+65", "box": [193, 78, 80, 30]}, {"text": "评论", "box": [328, 78, 50, 30]}]
        self.assertEqual(self.tabs("messages-center", lines, {}), ([], []))

    def test_comment_swipe_ignores_feed_bottom_bar_comment_icon(self):
        ui = android_ui.MaaUi.__new__(android_ui.MaaUi)
        ui.height = 1559
        feed_only = [
            {"text": "转发", "box": [458, 1481, 43, 29]},
            {"text": "评论", "box": [641, 1481, 43, 29]},
        ]
        with self.assertRaisesRegex(ValueError, "Cannot scroll an unclassified comment drawer"):
            ui._comment_swipe(feed_only)

        drawer = [
            {"text": "评论", "box": [100, 437, 54, 36]},
            {"text": "评论正文", "box": [100, 620, 300, 30]},
        ]
        start, end = ui._comment_swipe(drawer)
        self.assertGreater(start, end)

    def test_known_feed_and_personal_pages_skip_message_tab_templates_on_messages_start(self):
        feed_lines = [
            {"text": "推荐", "box": [437, 73, 70, 37]},
            {"text": "关注", "box": [192, 73, 70, 37]},
            {"text": "朋友", "box": [310, 73, 70, 37]},
        ]
        tabs, calls = self.tabs("messages-start", feed_lines, {"BatchMessagesLikeTab": HEART})
        self.assertEqual((tabs, calls), ([], []))

        personal_lines = [{"text": "视频号消息", "box": [120, 520, 180, 36]}]
        tabs2, calls2 = self.tabs("messages-return-1", personal_lines, {"BatchMessagesLikeTab": HEART})
        self.assertEqual((tabs2, calls2), ([], []))

    def test_click_reuses_fresh_controls_once_then_invalidates_after_input(self):
        import time as _time

        ui = android_ui.MaaUi.__new__(android_ui.MaaUi)
        ui.tasker = types.SimpleNamespace(stopping=False)
        ui.height = 1559
        ui.navigation_stage = None
        target = {
            "text": "展开",
            "box": [620, 660, 48, 34],
            "source": "hierarchy",
            "selector": {"attribute": "text", "value": "展开", "resource": "", "class": "android.widget.TextView"},
        }
        ui.controls = [target]
        ui.controls_at = _time.monotonic()
        dump_calls = []
        ui.hierarchy = lambda **_kw: (dump_calls.append(1) or [target])
        clicked = []

        class _Job:
            succeeded = True
            def wait(self):
                return self

        ui.controller = types.SimpleNamespace(post_click=lambda x, y: (clicked.append((x, y)) or _Job()))
        with mock.patch.object(android_ui, "foreground"), mock.patch.object(android_ui.time, "sleep"):
            ui.click(target)
            self.assertEqual(dump_calls, [])
            self.assertEqual(clicked, [(644, 677)])
            self.assertEqual(ui.controls, [])
            # A second click without an intervening snapshot must re-dump hierarchy.
            ui.click(target)
            self.assertEqual(len(dump_calls), 1)

    def test_comments_snapshot_captures_roi_baseline_before_hierarchy(self):
        import tempfile
        from pathlib import Path

        ui = android_ui.MaaUi.__new__(android_ui.MaaUi)
        ui.tasker = types.SimpleNamespace(stopping=False)
        ui.progress = None
        ui.height = 1605
        ui.sequence = 0
        ui.observation = 0
        ui.dump_frames = False
        ui.controls = []
        ui.controls_at = 0.0
        ui.controls_frame = None
        ui.controls_frame_file = None
        ui.snapshot_frame = None
        ui.snapshot_frame_file = None
        ui.navigation_stage = None
        with tempfile.TemporaryDirectory() as tmp:
            ui.evidence_dir = Path(tmp)
            order = []
            baseline = [["frame"]]

            def fake_capture():
                order.append("capture")
                return baseline

            drawer = [
                {"text": "评论 5", "box": [100, 437, 95, 37], "source": "hierarchy"},
                {"text": "林间微风", "box": [101, 753, 120, 34], "source": "hierarchy"},
                {"text": "发表评论:", "box": [101, 1537, 220, 34], "source": "hierarchy"},
            ]

            def fake_hierarchy():
                order.append("hierarchy")
                return drawer

            ui.capture_frame = fake_capture
            ui.hierarchy = fake_hierarchy
            ui._acknowledge_channels_intro = lambda lines: lines
            ui.message_tabs = lambda _l, _r, _m: []
            ui.snapshot("comments")
            self.assertEqual(order, ["capture", "hierarchy"])
            self.assertIs(ui.snapshot_frame, baseline)
            self.assertIs(ui.controls_frame, baseline)

            # Non-target snapshots (after-like / personal-page) skip extra baseline capture.
            order.clear()
            ui.controls_at = 0.0
            ui.snapshot("comments-after-like")
            self.assertEqual(order, ["hierarchy"])
            self.assertIsNone(ui.snapshot_frame)


if __name__ == "__main__":
    unittest.main()
