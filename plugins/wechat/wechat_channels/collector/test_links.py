import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from hierarchy import text_lines
from links import LinkLedger, private_message_state, resend_confirmation, share_link, validate_link_task, validate_pasted_link

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


if __name__ == "__main__":
    unittest.main()
