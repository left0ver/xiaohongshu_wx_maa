"""Tasks on the video behind a share link: opening it, messaging, and the once-only ledger."""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from interactions import DailyQuotaStore
from links import (
    LinkLedger,
    open_video_by_link,
    run_link_task,
    send_private_message,
    share_link,
    validate_link_task,
)
from test_collection_task import ShareUi, control
from test_modes import FEED_SECOND, PROFILE_TWO, line

LINK = "https://weixin.qq.com/sph/ASkxIf0OkN"
HOME = [line("微信", 60, 1525, 43, 29), line("通讯录", 238, 1525, 65, 29), line("发现", 429, 1525, 43, 29),
        line("我", 620, 1525, 22, 29)]
SEARCH = [control("文件传输助手", 72, 81, 440, 86, cls="android.widget.EditText"),
          line("功能", 28, 212, 50, 30), line("文件传输助手", 128, 298, 185, 42),
          line("文件传输助手", 86, 465, 185, 42)]
CHAT = [control("可按住 转文字", 87, 1471, 474, 79, cls="android.widget.EditText")]
TYPED = [control(LINK, 87, 906, 435, 108, cls="android.widget.EditText"), line("发送", 613, 946, 90, 52)]
SENT = [line(LINK, 192, 267, 423, 111), control("可按住 转文字", 87, 1471, 474, 79, cls="android.widget.EditText")]


class LinkUi(ShareUi):
    def start_wechat(self):
        self.started = True


class AsciiUi(LinkUi):
    fail_set_text = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.typed = []

    def type_ascii(self, text):
        self.typed.append(text)

    def set_text(self, text):
        if self.fail_set_text:
            raise RuntimeError("set-text helper was killed")
        super().set_text(text)


class LinkInputTests(unittest.TestCase):
    def test_the_link_is_taken_from_pasted_share_text(self):
        self.assertEqual(share_link(f"复制这条视频 {LINK} 打开微信"), LINK)
        with self.assertRaisesRegex(ValueError, "视频链接无效"):
            share_link("wechat-channels://video/abc")

    def test_each_action_needs_its_own_inputs(self):
        validate_link_task({"action": "likeVideo", "link": LINK})
        for config, message in (
            ({"action": "share", "link": LINK}, "Unsupported"),
            ({"action": "likeComment", "link": LINK}, "昵称"),
            ({"action": "replyComment", "link": LINK, "nickname": "俊国", "text": " "}, "内容"),
            ({"action": "messageAuthor", "link": LINK, "text": "字" * 501}, "500"),
            ({"action": "commentVideo", "link": "https://example.com", "text": "好"}, "视频链接无效"),
        ):
            with self.assertRaisesRegex(ValueError, message):
                validate_link_task(config)

    def test_the_ledger_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as root:
            LinkLedger(Path(root) / "link_actions.json").add("dm:abc")
            self.assertTrue(LinkLedger(Path(root) / "link_actions.json").has("dm:abc"))
            self.assertFalse(LinkLedger(Path(root) / "missing.json").has("dm:abc"))


class OpenByLinkTests(unittest.TestCase):
    def test_the_link_is_sent_to_file_transfer_and_opened_from_the_newest_message(self):
        old_and_new = [line(LINK, 192, 120, 423, 111), *SENT]
        ui = LinkUi([[line("推荐", 441, 76, 64, 32)], HOME, HOME, SEARCH, CHAT, TYPED, old_and_new, FEED_SECOND],
                    controls=[control("搜索", 560, 80, 60, 60)])
        with patch("links.time.sleep"):
            lines = open_video_by_link(ui, LINK)
        self.assertIs(lines, FEED_SECOND)
        self.assertEqual(ui.backs, 1)
        self.assertEqual(ui.texts_set, ["文件传输助手", LINK])
        clicked = [c["text"] for c in ui.clicks]
        self.assertEqual(clicked, ["微信", "搜索", "文件传输助手", "可按住 转文字", "发送", LINK])
        # The contact under 功能 is opened, not a web-search suggestion below it.
        self.assertEqual(ui.clicks[2]["box"][1], 298)
        # The link text, not the gap between its two lines, is tapped.
        self.assertEqual(ui.clicks[-1]["box"], [287, 328, 20, 20])

    def test_a_slow_browser_is_waited_for_and_links_are_typed_as_ascii(self):
        browser = [line("X", 12, 86, 44, 45), line("···", 649, 100, 38, 18)]
        ui = AsciiUi([HOME, HOME, SEARCH, CHAT, TYPED, SENT, browser, browser, FEED_SECOND],
                     controls=[control("搜索", 560, 80, 60, 60)])
        with patch("links.time.sleep"):
            self.assertIs(open_video_by_link(ui, LINK), FEED_SECOND)
        self.assertEqual(ui.typed, [LINK])
        self.assertEqual(ui.texts_set, ["文件传输助手"])
        # One tap: the browser page was waited out, not tapped through.
        self.assertEqual([c["text"] for c in ui.clicks].count(LINK), 1)

    def test_search_falls_back_to_pinyin_when_set_text_hangs(self):
        ui = AsciiUi([HOME, HOME, SEARCH, CHAT, TYPED, SENT, FEED_SECOND], controls=[control("搜索", 560, 80, 60, 60)])
        ui.fail_set_text = True
        with patch("links.time.sleep"):
            open_video_by_link(ui, LINK)
        self.assertEqual(ui.typed, ["wenjianchuanshuzhushou", LINK])

    def test_an_ocr_read_chat_is_still_recognised(self):
        from links import _is_chat
        self.assertTrue(_is_chat([line("可按住 转文字", 253, 1492, 160, 31)]))
        self.assertTrue(_is_chat([line("对方回复前，仅可发1条消息。", 91, 190, 338, 34)]))
        self.assertFalse(_is_chat(FEED_SECOND))

    def test_a_missing_file_transfer_contact_stops_the_task(self):
        ui = LinkUi([HOME, HOME, SEARCH[:2]])
        with patch("links.time.sleep"), self.assertRaisesRegex(ValueError, "文件传输助手"):
            open_video_by_link(ui, LINK)


class PrivateMessageTests(unittest.TestCase):
    DM_TYPED = [control("你好", 87, 906, 435, 108, cls="android.widget.EditText"), line("发送", 613, 946, 90, 52)]

    def test_the_message_is_sent_and_the_profile_is_back(self):
        sent = [line("你好", 420, 300, 100, 60), *CHAT]
        ui = LinkUi([CHAT, self.DM_TYPED, sent, CHAT, PROFILE_TWO])
        with patch("links.time.sleep"):
            lines = send_private_message(ui, PROFILE_TWO, "你好")
        self.assertIs(lines, PROFILE_TWO)
        self.assertEqual([c["text"] for c in ui.clicks], ["私信", "可按住 转文字", "发送"])
        self.assertEqual(ui.backs, 2)

    def test_the_first_message_notice_is_acknowledged(self):
        notice = [line("视频号私信消息安全保护提醒", 157, 1113, 403, 33), line("我知道了", 292, 1369, 128, 37)]
        sent = [line("你好", 420, 300, 100, 60), *CHAT]
        ui = LinkUi([CHAT, notice, CHAT, self.DM_TYPED, sent, PROFILE_TWO])
        with patch("links.time.sleep"):
            send_private_message(ui, PROFILE_TWO, "你好")
        self.assertEqual([c["text"] for c in ui.clicks], ["私信", "可按住 转文字", "我知道了", "可按住 转文字", "发送"])
        self.assertEqual(ui.texts_set, ["你好", "你好"])

    def test_a_restricted_chat_names_the_restriction(self):
        restricted = [line("作者已设置关注7天后才能发消息", 199, 1477, 365, 34),
                      line("对方回复前，仅可发1条消息。", 101, 190, 340, 34)]
        ui = LinkUi([restricted])
        with patch("links.time.sleep"), self.assertRaisesRegex(ValueError, "对方限制了私信：作者已设置关注7天后才能发消息"):
            send_private_message(ui, PROFILE_TWO, "你好")
        self.assertEqual(ui.texts_set, [])

    def test_a_message_that_does_not_appear_fails(self):
        sent = [line("对方回复前，仅可发1条消息。", 100, 190, 400, 40), *CHAT]
        ui = LinkUi([CHAT, self.DM_TYPED, sent])
        with patch("links.time.sleep"), self.assertRaisesRegex(RuntimeError, "仅可发1条消息"):
            send_private_message(ui, PROFILE_TWO, "你好")


class LinkTaskTests(unittest.TestCase):
    def run_task(self, root, config, snapshots=(), ledger=None):
        ui = LinkUi(list(snapshots) or [FEED_SECOND])
        quota = DailyQuotaStore(Path(root) / "quota.json")
        ledger = ledger or LinkLedger(Path(root) / "link_actions.json")
        with patch("links.open_video_by_link", return_value=FEED_SECOND), patch("links.time.sleep"), \
                contextlib.redirect_stdout(io.StringIO()):
            return ui, run_link_task(ui, quota, ledger, config)

    def test_an_author_is_messaged_only_once(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = LinkLedger(Path(root) / "link_actions.json")
            config = {"action": "messageAuthor", "link": LINK, "text": "你好"}
            with patch("links._open_author_profile", return_value=("阿星探店", PROFILE_TWO)), \
                    patch("links.send_private_message") as send, patch("links._back_to_video"):
                _, first = self.run_task(root, config, ledger=ledger)
                ui, second = self.run_task(root, config, ledger=ledger)
            self.assertEqual((first["status"], second["status"]), ("sent", "already_messaged"))
            self.assertEqual(send.call_count, 1)
            self.assertEqual(first["author"], "阿星探店")

    def test_a_commenter_without_channels_cannot_be_messaged(self):
        comment = {"nickname": "俊国", "content": "好", "_nameBox": [100, 600, 80, 30]}
        card = [line("俊国", 150, 900, 80, 30), line("设置", 600, 1300, 60, 30)]
        with tempfile.TemporaryDirectory() as root, \
                patch("links.locate_target_comment", return_value={"found": True, "comment": comment, "lines": []}):
            with self.assertRaisesRegex(ValueError, "没有开通视频号"):
                self.run_task(root, {"action": "messageCommenter", "link": LINK, "nickname": "俊国", "text": "你好"},
                              snapshots=[card])
            self.assertFalse(LinkLedger(Path(root) / "link_actions.json").has("dm:"))

    def test_an_unfound_comment_names_the_nickname(self):
        with tempfile.TemporaryDirectory() as root, \
                patch("links.locate_target_comment", return_value={"found": False, "comment": None, "lines": []}):
            with self.assertRaisesRegex(ValueError, "俊国.*翻完评论区"):
                self.run_task(root, {"action": "likeComment", "link": LINK, "nickname": "俊国"})

    def test_a_video_comment_is_posted_once(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = LinkLedger(Path(root) / "link_actions.json")
            config = {"action": "commentVideo", "link": LINK, "text": "拍得真好"}
            with patch("links.post_work_comment", return_value={"status": "performed"}) as post:
                _, first = self.run_task(root, config, ledger=ledger)
                _, second = self.run_task(root, config, ledger=ledger)
            self.assertEqual((first["status"], second["status"]), ("sent", "already_commented"))
            self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
