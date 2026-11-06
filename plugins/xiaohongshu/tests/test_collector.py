import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "xiaohongshu" / "agent"))
from collector import (Device, collect_author_red_id, collect_note, collect_notes,
                       copy_note_share_link, open_discover, read_note_detail)
from ui import Card, Page, PageMismatch


CARD = Card("IMAGE_TEXT", "标题", "作者", (10, 60, 190, 400), (10, 60, 190, 340))
DETAIL = dict(noteType="IMAGE_TEXT", title="标题", description="正文", authorNickname="作者")


def device():
    driver = Mock()
    driver.wait.side_effect = [[CARD], DETAIL.copy(), "red123", DETAIL.copy(), object(), DETAIL.copy(), [CARD]]
    driver.page.return_value.cards.return_value = [CARD]
    return driver


class CollectorTests(unittest.TestCase):
    def test_missing_snapshot_is_retryable_and_never_uses_old_data(self):
        driver = Device()
        driver.command = Mock(return_value="")
        with self.assertRaisesRegex(PageMismatch, "hierarchy was unavailable"):
            driver.page()

    def test_unstable_bounds_never_dispatch_input(self):
        import xml.etree.ElementTree as ET
        driver = Device()
        driver.page = Mock(side_effect=[ET.Element("node", bounds=f"[{x},10][{x + 20},30]") for x in range(4)])
        driver.command = Mock()
        with self.assertRaises(PageMismatch):
            driver.click(lambda page: page)
        driver.command.assert_not_called()

    def test_commit_only_after_verified_return_and_skip_seen(self):
        driver = device()
        with tempfile.TemporaryDirectory() as directory:
            report = collect_note(driver, lambda: {"text": "https://xhslink.com/a/example",
                                  "timestamp": time.time_ns() // 1_000_000}, directory)
            self.assertFalse(report["uploaded"])
            self.assertEqual(report["payload"]["authorDouyinId"], "red123")
            self.assertEqual(report["payload"]["awemeId"], "")
            self.assertIn(CARD.key, json.loads((Path(directory) / "seen.json").read_text()))
            second = device()
            with self.assertRaises(PageMismatch):
                collect_note(second, Mock(), directory)
            second.command.assert_not_called()

    def test_video_does_not_pause_and_expands_caption_from_drawer_first_item(self):
        video = Card("VIDEO", "标题", "作者", CARD.box, CARD.cover_box)
        collapsed = {**DETAIL, "noteType": "VIDEO", "description": "标题... 展开", "_incomplete": "truncated"}
        expanded = {**DETAIL, "noteType": "VIDEO", "description": "标题\n第一条就是完整视频文案", "_drawer_close": object()}
        clean_expanded = {**DETAIL, "noteType": "VIDEO", "description": "标题\n第一条就是完整视频文案"}
        driver = device()
        driver.page.return_value.cards.return_value = [video]
        driver.wait.side_effect = [
            [video], collapsed, expanded, clean_expanded,
            "red123", clean_expanded, object(), clean_expanded, [video],
        ]
        page = Mock()
        with tempfile.TemporaryDirectory() as directory:
            report = collect_note(driver, lambda: {"text": "https://xhslink.com/a/video",
                                  "timestamp": time.time_ns() // 1_000_000}, directory)
        self.assertEqual(report["payload"]["description"], "标题\n第一条就是完整视频文案")
        driver.click.call_args_list[0].args[0](page)
        page.note_expand_control.assert_called_once_with()
        driver.click.call_args_list[1].args[0](page)
        page.video_drawer_close_button.assert_called_once_with()
        driver.click.call_args_list[2].args[0](page)
        page.unique.assert_called_once_with(resource="matrixNickNameView")

    def test_type_filter_never_opens_an_image_in_video_task(self):
        driver = device()
        driver.wait.side_effect = [[CARD]] * 6
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PageMismatch):
                collect_note(driver, Mock(), directory, "VIDEO")
        self.assertEqual(driver.command.call_count, 5)
        self.assertTrue(all(call.args[:2] == ("input", "swipe") for call in driver.command.call_args_list))
        driver.click.assert_not_called()

    def test_share_retry_requires_same_work(self):
        driver = device()
        driver.wait.side_effect = [[CARD], DETAIL.copy(), "red123", DETAIL.copy(),
                                  PageMismatch("share did not open"), {**DETAIL, "title": "other"}]
        clipboard = Mock()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PageMismatch):
                collect_note(driver, clipboard, directory)
        clipboard.assert_not_called()
        self.assertEqual(driver.click.call_count, 2)

    def test_stale_clipboard_never_commits(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PageMismatch):
                collect_note(device(), lambda: {"text": "https://xhslink.com/a/stale", "timestamp": 1}, directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_changed_feed_before_click_is_not_clicked(self):
        driver = device()
        driver.page.return_value.cards.return_value = []
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PageMismatch):
                collect_note(driver, Mock(), directory)
        driver.command.assert_not_called()

    def test_wrong_returned_author_never_opens_share(self):
        driver = device()
        driver.wait.side_effect = [[CARD], DETAIL.copy(), "red123", {**DETAIL, "authorNickname": "其他人"}]
        clipboard = Mock()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PageMismatch):
                collect_note(driver, clipboard, directory)
            self.assertEqual(list(Path(directory).iterdir()), [])
        clipboard.assert_not_called()
        self.assertEqual(driver.click.call_count, 1)

    def test_unknown_page_does_not_back_out_blindly(self):
        driver = Mock()
        page = driver.page.return_value
        driver.wait.return_value = page
        page.cards.side_effect = PageMismatch("not feed")
        page.note.side_effect = PageMismatch("not note")
        page.nodes = []
        with self.assertRaises(PageMismatch):
            open_discover(driver)
        driver.back.assert_not_called()

    def test_read_note_detail_expands_and_scrolls_long_image_note(self):
        import xml.etree.ElementTree as ET

        def make_page(*, text, bottom, with_picture=True, expand_button=False):
            root = ET.Element("hierarchy")
            ET.SubElement(root, "node", {"package": "com.xingin.xhs", "bounds": "[20,20][100,50]",
                                         "text": "作者", "resource-id": "com.xingin.xhs:id/nickNameTV",
                                         "class": "android.widget.TextView"})
            ET.SubElement(root, "node", {"package": "com.xingin.xhs", "bounds": "[150,20][190,50]",
                                         "resource-id": "com.xingin.xhs:id/moreOperateIV",
                                         "class": "android.widget.ImageView"})
            if with_picture:
                ET.SubElement(root, "node", {"package": "com.xingin.xhs", "bounds": "[0,60][200,380]",
                                             "content-desc": "图片,第1张,共2张", "class": "android.widget.FrameLayout"})
            group = ET.SubElement(root, "node", {"package": "com.xingin.xhs",
                                                 "bounds": f"[0,390][200,{bottom}]",
                                                 "class": "android.widget.LinearLayout"})
            if with_picture:
                ET.SubElement(group, "node", {"package": "com.xingin.xhs", "bounds": "[10,395][190,430]",
                                              "text": "标题", "class": "android.widget.TextView"})
            ET.SubElement(group, "node", {"package": "com.xingin.xhs",
                                          "bounds": f"[10,435][190,{bottom}]",
                                          "text": text, "class": "android.widget.TextView",
                                          "clickable": "true" if expand_button else "false"})
            ET.SubElement(root, "node", {"package": "com.xingin.xhs", "bounds": "[10,700][100,750]",
                                         "content-desc": "评论框", "class": "android.widget.TextView"})
            return Page(ET.tostring(root, encoding="unicode"))

        p1 = make_page(text="第一段… 展开", bottom=720, with_picture=True, expand_button=True)
        p2 = make_page(text="第一段完整内容", bottom=720, with_picture=True)
        p3 = make_page(text="第二段结尾内容", bottom=580, with_picture=False)
        state = {"page": p1}
        driver = Mock()
        driver.page.side_effect = lambda: state["page"]

        def click(selector):
            target = selector(state["page"])
            self.assertIn("展开", target.get("text", ""))
            state["page"] = p2

        def command(*args):
            if args[:2] == ("input", "swipe"):
                state["page"] = p3
            return ""

        def wait(parse, **kwargs):
            for _ in range(3):
                try:
                    return parse(state["page"])
                except PageMismatch:
                    pass
            return parse(state["page"])

        driver.click.side_effect = click
        driver.command.side_effect = command
        driver.wait.side_effect = wait
        detail = read_note_detail(driver, expected_card=CARD, max_scrolls=4)
        self.assertEqual(detail, dict(noteType="IMAGE_TEXT", authorNickname="作者",
                                      title="标题", description="第一段完整内容\n第二段结尾内容"))
        self.assertEqual(driver.click.call_count, 1)
        self.assertEqual(driver.command.call_count, 1)

    def test_share_panel_closes_when_panel_remains_open_after_copy(self):
        driver = Mock()
        driver.wait.side_effect = [
            object(),
            PageMismatch("A profile or share panel still covers the note"),
            DETAIL.copy(),
        ]
        url, note_id = copy_note_share_link(
            driver,
            DETAIL.copy(),
            lambda: {"text": "https://www.xiaohongshu.com/explore/66b1c2d3e4f5a6b7c8d9e0f1",
                     "timestamp": time.time_ns() // 1_000_000 + 1000},
        )
        self.assertEqual(note_id, "66b1c2d3e4f5a6b7c8d9e0f1")
        driver.back.assert_called_once_with()

    def test_collect_notes_continuously_processes_and_scrolls_feed(self):
        card1 = Card("IMAGE_TEXT", "标题一", "作者甲", (10, 60, 190, 400), (10, 60, 190, 340))
        card2 = Card("IMAGE_TEXT", "标题二", "作者乙", (10, 120, 190, 460), (10, 120, 190, 400))
        detail1 = dict(noteType="IMAGE_TEXT", title="标题一", description="正文一", authorNickname="作者甲")
        detail2 = dict(noteType="IMAGE_TEXT", title="标题二", description="正文二", authorNickname="作者乙")
        driver = Mock()
        driver.wait.side_effect = [
            [card1], detail1.copy(), "red_a", detail1.copy(), object(), detail1.copy(), [card1],
            [card1], [card2], detail2.copy(), "red_b", detail2.copy(), object(), detail2.copy(), [card2],
        ]
        driver.page.return_value.cards.side_effect = [[card1], [card2]]
        with tempfile.TemporaryDirectory() as directory:
            batch = collect_notes(
                driver,
                lambda: {"text": "https://xhslink.com/a/batch", "timestamp": time.time_ns() // 1_000_000 + 1000},
                directory,
                count=2,
                note_type="IMAGE_TEXT",
                max_screens=3,
            )
            self.assertEqual(batch["count"], 2)
            self.assertEqual([r["payload"]["authorDouyinId"] for r in batch["reports"]], ["red_a", "red_b"])
            seen = json.loads((Path(directory) / "seen.json").read_text())
            self.assertEqual(set(seen), {card1.key, card2.key})


if __name__ == "__main__":
    unittest.main()
