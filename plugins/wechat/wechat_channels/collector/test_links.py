import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from hierarchy import text_lines
from links import LinkLedger, share_link, validate_link_task, validate_pasted_link

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


if __name__ == "__main__":
    unittest.main()
