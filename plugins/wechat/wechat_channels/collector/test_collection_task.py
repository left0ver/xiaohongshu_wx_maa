"""App task 1 (视频号采集): stored fields, filter words, share link and the miss limit."""

import contextlib
import io
import json
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from collection import LocalStore, copy_share_link, filter_words, identity, matched_words, share_geometry
from comments import filter_comments, save_comments
from modes import collection_task_config, run_tab_feed_mode
from test_modes import DETAILS_TWO, FEED_SECOND, FakeModeUi, line

LINK = "https://weixin.qq.com/sph/ASkxIf0OkN"


def control(text, x, y, w=80, h=30, cls="android.widget.TextView"):
    return {**line(text, x, y, w, h), "source": "hierarchy", "selector": {"class": cls, "attribute": "text"}}


class ShareUi(FakeModeUi):
    """Records key presses and swipes; controls come from a fixed list."""

    def __init__(self, snapshots, controls=()):
        super().__init__(snapshots)
        self.keys, self.swipes = [], []
        self.fixed_controls = list(controls)

    def controls_now(self):
        return self.fixed_controls

    def key(self, code):
        self.keys.append(code)

    def swipe(self, *args):
        self.swipes.append(args)


def payload(**extra):
    return {"authorNickname": "阿星探店", "description": "今天分享一道快手家常菜#美食",
            "authorDouyinId": "sphFoodStar888", "authorIdentityMethod": "wechat-channel-id",
            "publishTimeText": "11月7日", "publishLocation": "广东", **extra}


class FilterWordTests(unittest.TestCase):
    def test_words_split_on_spaces_commas_and_enumeration_marks(self):
        self.assertEqual(filter_words("创业, 副业、想学；创业  AI"), ["创业", "副业", "想学", "AI"])
        self.assertEqual(filter_words(""), [])
        self.assertEqual(filter_words(None), [])
        self.assertEqual(filter_words(["创业", "副业"]), ["创业", "副业"])

    def test_matching_ignores_case_width_and_spacing(self):
        self.assertEqual(matched_words("学ａｉ 做 副 业", ["AI", "副业", "创业"]), ["AI", "副业"])
        self.assertEqual(matched_words(None, ["AI"]), [])


class StoredVideoTests(unittest.TestCase):
    def test_video_is_saved_in_the_agreed_upload_shape(self):
        with tempfile.TemporaryDirectory() as root:
            event, duplicate = LocalStore(Path(root) / "v.sqlite3").save(
                payload(), source_mode="SEARCH", video_url=LINK, search_keyword=" 美食 ")
        self.assertFalse(duplicate)
        self.assertRegex(event["dedupe_key"], "^[0-9a-f]{64}$")
        self.assertEqual(event["dedupe_key"], identity("阿星探店", "今天分享一道快手家常菜#美食"))
        self.assertIsInstance(event["collected_at"], int)
        body = event["payload"]
        self.assertEqual((body["platform"], body["videoUrl"], body["awemeId"], body["authorDouyinId"]),
                         ("wechat", LINK, None, None))
        self.assertEqual(body["author_wechat_channels_id"], "sphFoodStar888")
        self.assertEqual((body["searchKeyword"], body["matchedKeyword"]), ("美食", "美食"))

    def test_a_fallback_author_key_is_not_a_channels_id(self):
        with tempfile.TemporaryDirectory() as root:
            event, _ = LocalStore(Path(root) / "v.sqlite3").save(
                payload(authorDouyinId="wxch_user_0123456789abcdef", authorIdentityMethod="wechat-author-fallback"))
        self.assertIsNone(event["payload"]["author_wechat_channels_id"])
        self.assertIsNone(event["payload"]["searchKeyword"])
        self.assertIsNone(event["payload"]["videoUrl"])

    def test_a_repeat_keeps_the_first_link_and_fills_a_missing_one(self):
        with tempfile.TemporaryDirectory() as root:
            store = LocalStore(Path(root) / "v.sqlite3")
            store.save(payload())
            second, duplicate = store.save(payload(), video_url=LINK)
            third, _ = store.save(payload(), video_url="https://weixin.qq.com/sph/Apk5qDYnkO")
        self.assertTrue(duplicate)
        self.assertEqual(second["payload"]["videoUrl"], LINK)
        self.assertEqual(third["payload"]["videoUrl"], LINK)


class StoredCommentTests(unittest.TestCase):
    def comment(self, nickname, content, **extra):
        return {"nickname": nickname, "content": content, "commentTime": "1天前", "ipLocation": "广东",
                "key": "wxch-comment:" + identity(nickname, content), **extra}

    def test_filter_keeps_matching_comments_and_records_the_words(self):
        comments = [self.comment("甲", "想学做副业"), self.comment("乙", "拍得真好")]
        kept = filter_comments(comments, ["副业", "创业"], complete=True)
        self.assertEqual([c["nickname"] for c in kept], ["甲"])
        self.assertEqual((kept[0]["contentMatched"], kept[0]["commentMatchedKeywords"], kept[0]["collectionComplete"]),
                         (True, ["副业"], True))
        everything = filter_comments(comments, [], complete=False)
        self.assertEqual(len(everything), 2)
        self.assertEqual({(c["contentMatched"], c["commentMatchedKeywords"]) for c in everything}, {(None, None)})

    def test_comment_drafts_use_hex_keys_and_millisecond_times(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "v.sqlite3"
            video, _ = LocalStore(path).save(payload())
            parent = self.comment("甲", "想学做副业")
            reply = self.comment("乙", "回复甲：一起", commentLevel=2, parentCommentKey=parent["key"])
            events = save_comments(path, video, [parent, reply])
        self.assertTrue(all(re.fullmatch("[0-9a-f]{64}", e["dedupe_key"]) for e in events))
        self.assertTrue(all(isinstance(e["collected_at"], int) for e in events))
        self.assertEqual(events[1]["payload"]["parentCommentKey"], events[0]["dedupe_key"])
        self.assertEqual((events[0]["payload"]["commentId"], events[0]["payload"]["platform"]), (None, "wechat"))


class ShareLinkTests(unittest.TestCase):
    SHEET_FIRST = [line("转发给朋友", 27, 1170, 90, 49), line("浮窗", 54, 1365, 36, 49),
                   line("投诉", 421, 1365, 36, 49)]
    SHEET_SCROLLED = [line("投诉", 18, 1365, 36, 49), line("复制链接", 611, 1365, 72, 49)]

    def test_the_share_button_sits_above_its_count(self):
        self.assertEqual(share_geometry(FEED_SECOND, 1605)["box"], [439, 1485, 48, 44])
        self.assertEqual(share_geometry([], 1605)["box"], [442, 1473, 48, 48])

    def run_copy(self, pasted, previous=None):
        ui = ShareUi([self.SHEET_FIRST, self.SHEET_SCROLLED, FEED_SECOND, pasted, FEED_SECOND, FEED_SECOND],
                     controls=[control("搜索", 560, 80, 60, 60), control("清除搜索词", 525, 85, 60, 60)])
        with patch("collection.time.sleep"):
            return ui, copy_share_link(ui, FEED_SECOND, previous_link=previous)

    def test_the_link_is_copied_pasted_into_search_and_read_from_the_controls(self):
        pasted = [control(LINK, 72, 81, 440, 86, cls="android.widget.EditText")]
        ui, link = self.run_copy(pasted)
        self.assertEqual(link, LINK)
        self.assertEqual(len(ui.swipes), 1)
        self.assertEqual(ui.keys, [279])
        self.assertEqual([c["text"] for c in ui.clicks], ["share", "复制链接", "搜索", "清除搜索词"])

    def test_an_ocr_reading_or_a_stale_clipboard_is_not_saved(self):
        with self.assertRaisesRegex(ValueError, "could not be read"):
            self.run_copy([line(LINK, 72, 81, 440, 86)])
        pasted = [control(LINK, 72, 81, 440, 86, cls="android.widget.EditText")]
        with self.assertRaisesRegex(ValueError, "previous video"):
            self.run_copy(pasted, previous=LINK)


class CollectionTaskTests(unittest.TestCase):
    def test_settings_are_checked_before_the_phone_is_touched(self):
        self.assertEqual(collection_task_config({"entry": "搜索", "keyword": "美食", "count": "3",
                                                 "videoFilter": "家常菜 美食"})["videoFilterWords"],
                         ["家常菜", "美食"])
        for config, message in (({"entry": "search"}, "关键词"), ({"entry": "city"}, "同城"),
                                ({"count": 0}, "1 到 500"), ({"count": "x"}, "1 到 500"),
                                ({"entry": "friends"}, "Unsupported")):
            with self.assertRaisesRegex(ValueError, message):
                collection_task_config(config)

    def test_filtered_videos_do_not_count_and_misses_end_the_run(self):
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
            ui = FakeModeUi([FEED_SECOND, DETAILS_TWO, FEED_SECOND, DETAILS_TWO])
            with patch("modes.time.sleep"), patch("collection.time.sleep"):
                report = run_tab_feed_mode(ui, Path(root), tab="recommend", count=1, max_swipes=100,
                                           collect_options={"videoFilterWords": ["健身"]}, max_misses=2)
            self.assertEqual(report["processedCount"], 0)
            self.assertEqual(report["endReason"], "miss_limit")
            self.assertEqual(len(report["filteredItems"]), 2)
            self.assertEqual(ui.swipes_next, 1)
            # Nothing was saved, so a later run with other words still offers the video.
            self.assertFalse(json.loads((Path(root) / "seen.json").read_text()) if (Path(root) / "seen.json").exists()
                             else [])
            with contextlib.closing(sqlite3.connect(Path(root) / "videos.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM video_drafts").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
