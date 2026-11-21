import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from hierarchy import text_lines
from links import (
    ALL_COMMENT_PAGES,
    LinkLedger,
    _locate,
    open_file_helper,
    private_message_state,
    require_private_message_available,
    resend_confirmation,
    share_link,
    validate_link_task,
    validate_pasted_link,
)

LINK = "https://weixin.qq.com/sph/A7uTTkNHLH"
FIXTURES = Path(__file__).parent / "testdata"


class LinkInputTests(unittest.TestCase):
    def test_share_link_from_user_text(self):
        self.assertEqual(share_link(f"复制这条视频 {LINK} 打开微信"), LINK)
        with self.assertRaisesRegex(ValueError, "视频链接无效"):
            share_link("wechat-channels://video/abc")

    def test_required_task_inputs(self):
        validate_link_task({"action": "likeVideo", "link": LINK})
        cases = (
            ({"action": "share", "link": LINK}, "Unsupported"),
            ({"action": "likeComment", "link": LINK}, "昵称"),
            ({"action": "replyComment", "link": LINK, "nickname": "俊国", "text": " "}, "内容"),
            ({"action": "messageAuthor", "link": LINK, "text": "字" * 501}, "500"),
            ({"action": "commentVideo", "link": "https://example.com", "text": "好"}, "视频链接无效"),
        )
        for config, message in cases:
            with self.subTest(config=config), self.assertRaisesRegex(ValueError, message):
                validate_link_task(config)

    def test_ledger_persists_on_disk(self):
        directory = Path(__file__).parent / "data" / "link-tests"
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as root:
            path = Path(root) / "link_actions.json"
            LinkLedger(path).add("dm:abc")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), ["dm:abc"])
            self.assertTrue(LinkLedger(path).has("dm:abc"))


class RecordedClipboardTests(unittest.TestCase):
    # 2026-11-12 vivo 实测输入框节点，保留原始文字和坐标。
    def test_pasted_link_matches_exactly(self):
        xml = (FIXTURES / "clipboard-input-vivo.xml").read_text(encoding="utf-8")
        validate_pasted_link(text_lines(xml, 1080, 2408), LINK)

    def test_rewritten_input_is_rejected_before_sending(self):
        xml = (FIXTURES / "rewritten-input-vivo.xml").read_text(encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "输入框内容与视频链接不一致"):
            validate_pasted_link(text_lines(xml, 1080, 2408), LINK)

    def test_different_link_is_rejected(self):
        xml = (FIXTURES / "clipboard-input-vivo.xml").read_text(encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "输入框内容与视频链接不一致"):
            validate_pasted_link(text_lines(xml, 1080, 2408), "https://weixin.qq.com/sph/A0uxjlm771")


class RecordedPrivateMessageTests(unittest.TestCase):
    def test_huawei_resend_confirmation(self):
        lines = json.loads((FIXTURES / "dm-resend-confirm-huawei.json").read_text())["lines"]
        self.assertEqual(resend_confirmation(lines)["box"], [329, 1371, 62, 42])

    def test_selects_failure_button_for_latest_message(self):
        lines = json.loads((FIXTURES / "dm-failed-huawei.json").read_text())["lines"]
        message, button, pending = private_message_state(lines, "你好，做的菜看着好香")
        self.assertEqual(message["box"], [255, 653, 361, 34])
        self.assertEqual(button["box"], [179, 631, 79, 79])
        self.assertFalse(pending)
        self.assertIsNone(resend_confirmation(lines))

    def test_successful_message_has_no_retry_button(self):
        lines = json.loads((FIXTURES / "dm-sent-vivo.json").read_text())["lines"]
        message, button, pending = private_message_state(lines, "你好，看到你的评论很有共鸣")
        self.assertEqual(message["selector"]["value"], "你好，看到你的评论很有共鸣")
        self.assertIsNone(button)
        self.assertFalse(pending)

    def test_older_failed_message_does_not_trigger_resend(self):
        lines = json.loads((FIXTURES / "dm-latest-sent-huawei.json").read_text())["lines"]
        self.assertTrue(any(line["text"] == "重发" for line in lines))
        message, button, pending = private_message_state(lines, "你好，做的菜看着好香")
        self.assertEqual(message["selector"]["value"], "你好，做的菜看着好香")
        self.assertIsNone(button)
        self.assertFalse(pending)

    def test_pending_message_keeps_progress_indicator(self):
        xml = (FIXTURES / "dm-pending-vivo.xml").read_text()
        lines = text_lines(xml, 1080, 2408)
        message, button, pending = private_message_state(lines, "发送状态检查 1794462591546770899")
        self.assertTrue(pending)
        self.assertIsNone(button)
        self.assertGreater(message["controlBox"][3], message["box"][3])

    def test_system_notice_is_not_an_outgoing_message(self):
        lines = json.loads((FIXTURES / "dm-failed-huawei.json").read_text())["lines"]
        with self.assertRaisesRegex(ValueError, "无法从控件确认"):
            private_message_state(lines, "操作太频繁，请稍后再试。")

    def test_missing_message_is_not_confirmed(self):
        lines = json.loads((FIXTURES / "dm-sent-vivo.json").read_text())["lines"]
        with self.assertRaisesRegex(ValueError, "无法从控件确认"):
            private_message_state(lines, "另一条内容")

    def test_private_message_and_resend_work_when_all_resource_ids_are_obfuscated_or_empty(self):
        import re as _re

        def strip_ids(items):
            stripped = []
            for item in items:
                copy = dict(item)
                if "selector" in copy and copy["selector"]:
                    sel = dict(copy["selector"])
                    sel["resource"] = "com.tencent.mm:id/obf_999"
                    if sel.get("attribute") == "resource-id":
                        sel["value"] = sel["resource"]
                    copy["selector"] = sel
                stripped.append(copy)
            return stripped

        confirm_lines = strip_ids(json.loads((FIXTURES / "dm-resend-confirm-huawei.json").read_text())["lines"])
        self.assertEqual(resend_confirmation(confirm_lines)["box"], [329, 1371, 62, 42])

        failed_lines = strip_ids(json.loads((FIXTURES / "dm-failed-huawei.json").read_text())["lines"])
        message, button, pending = private_message_state(failed_lines, "你好，做的菜看着好香")
        self.assertEqual(message["box"], [255, 653, 361, 34])
        self.assertEqual(button["box"], [179, 631, 79, 79])
        self.assertFalse(pending)

        blocked_lines = strip_ids(json.loads((FIXTURES / "dm-recipient-blocked-huawei.json").read_text())["lines"])
        with self.assertRaisesRegex(RuntimeError, "对方暂无法接收私信"):
            require_private_message_available(blocked_lines)
        with self.assertRaisesRegex(RuntimeError, "微信限制了私信发送"):
            require_private_message_available(failed_lines)

        raw_xml = _re.sub(r'resource-id="[^"]*"', 'resource-id=""', (FIXTURES / "dm-pending-vivo.xml").read_text())
        pending_lines = text_lines(raw_xml, 1080, 2408)
        msg, btn, is_pending = private_message_state(pending_lines, "发送状态检查 1794462591546770899")
        self.assertTrue(is_pending)
        self.assertIsNone(btn)
        self.assertGreater(msg["controlBox"][3], msg["box"][3])


class OpenFileHelperRecoveryTests(unittest.TestCase):
    def _line(self, text, x, y, w=120, h=34, cls="android.widget.TextView", selected=None):
        item = {
            "text": text,
            "box": [x, y, w, h],
            "source": "hierarchy",
            "selector": {"attribute": "text", "value": text, "resource": "", "class": cls},
        }
        if selected is not None:
            item["selected"] = selected
        return item

    def _home(self, helper_y=168):
        items = [
            self._line("微信", 69, 1566, 43, 29, selected=True),
            self._line("通讯录", 238, 1566, 64, 29, selected=False),
        ]
        if helper_y is not None:
            items.append(self._line("文件传输助手", 137, helper_y, 472, 41, cls="android.view.View"))
        return items

    def _chat(self):
        return [
            self._line("文件传输助手", 120, 80, 200, 40),
            self._line("", 100, 1490, 450, 50, cls="android.widget.EditText"),
        ]

    def test_recovers_and_retries_direct_tap_when_push_shifts_chat_list(self):
        from unittest.mock import patch

        class FakeUi:
            height = 1605
            controls_exposed = False

            def __init__(self, snapshots):
                self.snapshots = list(snapshots)
                self.clicks = []
                self.backs = 0
                self.typed = []

            def snapshot(self, label):
                return self.snapshots.pop(0)

            def click(self, target):
                self.clicks.append((target["text"], target["box"]))

            def back(self):
                self.backs += 1

            def set_text(self, text):
                self.typed.append(text)

            def controls_now(self):
                return []

        official_accounts_page = [self._line("订阅号消息", 120, 80)]
        home_shifted = self._home(helper_y=297)
        ui = FakeUi([official_accounts_page, home_shifted, self._chat()])
        with patch("links.time.sleep"):
            chat = open_file_helper(ui, home_lines=self._home(helper_y=168))
        self.assertTrue(any(l["selector"]["class"].endswith("EditText") for l in chat))
        self.assertEqual(ui.backs, 1)
        self.assertEqual(ui.typed, [])
        self.assertEqual(ui.clicks, [("文件传输助手", [137, 168, 472, 41]), ("文件传输助手", [137, 297, 472, 41])])

    def test_recovers_when_search_accidentally_opens_web_search_page(self):
        from unittest.mock import patch

        class FakeUi:
            height = 1605
            controls_exposed = False

            def __init__(self, snapshots):
                self.snapshots = list(snapshots)
                self.clicks = []
                self.backs = 0
                self.typed = []

            def snapshot(self, label):
                return self.snapshots.pop(0)

            def click(self, target):
                self.clicks.append(target["text"])

            def back(self):
                self.backs += 1

            def set_text(self, text):
                self.typed.append(text)

            def controls_now(self):
                return []

        web_search = [
            self._line("文件传输助手", 72, 96, 442, 34, cls="android.widget.EditText"),
            self._line("搜索", 593, 96, 97, 34, cls="android.widget.TextView"),
            self._line("文\u200b件\u200b传\u200b输\u200b助\u200b手", 0, 171, 720, 101, cls="android.view.View"),
        ]
        contact_search = [
            self._line("文件传输助手", 72, 96, 442, 34, cls="android.widget.EditText"),
            self._line("取消", 610, 96, 80, 34, cls="android.widget.TextView"),
            self._line("文件传输助手", 140, 260, 240, 40, cls="android.widget.TextView"),
        ]
        ui = FakeUi([web_search, self._home(helper_y=None), contact_search, self._chat()])
        with patch("links.time.sleep"):
            chat = open_file_helper(ui, home_lines=self._home(helper_y=None))
        self.assertTrue(any(l["selector"]["class"].endswith("EditText") for l in chat))
        self.assertEqual(ui.backs, 1)
        self.assertEqual(ui.clicks, ["搜索", "搜索", "文件传输助手"])

    def test_locate_uses_500_pages_and_expands_replies(self):
        from unittest.mock import patch

        self.assertEqual(ALL_COMMENT_PAGES, 500)
        expected = {"found": True, "comment": {"nickname": "爱吃汉堡🍔的…"}, "lines": []}
        with patch("links.locate_target_comment", return_value=expected) as mock_locate:
            got = _locate(object(), None, "wxch-video:1", "爱吃汉堡🍔的…", [])
        self.assertIs(got, expected)
        _, kwargs = mock_locate.call_args
        self.assertEqual(kwargs["max_pages"], 500)
        self.assertTrue(kwargs["expand_replies"])


if __name__ == "__main__":
    unittest.main()
