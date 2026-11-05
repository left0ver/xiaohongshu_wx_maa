import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "xiaohongshu" / "agent"))
from collector import Device, collect_note, open_discover
from ui import Card, PageMismatch


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

    def test_video_pauses_before_author_navigation(self):
        video = Card("VIDEO", "标题", "作者", CARD.box, CARD.cover_box)
        detail = {**DETAIL, "noteType": "VIDEO"}
        driver = device()
        driver.page.return_value.cards.return_value = [video]
        driver.wait.side_effect = [[video], detail, "red123", detail, object(), detail, [video]]
        page = Mock()
        with tempfile.TemporaryDirectory() as directory:
            collect_note(driver, lambda: {"text": "https://xhslink.com/a/video",
                         "timestamp": time.time_ns() // 1_000_000}, directory)
        driver.click.call_args_list[0].args[0](page)
        page.unique.assert_called_once_with(description="暂停")

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


if __name__ == "__main__":
    unittest.main()
