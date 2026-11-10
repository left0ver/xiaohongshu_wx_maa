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


if __name__ == "__main__":
    unittest.main()
