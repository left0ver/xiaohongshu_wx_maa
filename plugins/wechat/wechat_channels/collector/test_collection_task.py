"""App task 1 (视频号采集): stored fields, filter words, share link and the miss limit."""

import contextlib
import errno
import io
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from collection import (
    LocalStore,
    Timings,
    UnclearedComposerLinkError,
    copy_share_link,
    filter_words,
    identity,
    matched_words,
    share_geometry,
)
from comments import filter_comments, save_comments
from modes import (
    collection_task_config,
    is_fatal_collection_error,
    is_transient_upload_error,
    run_collection_task,
    run_search_mode,
    run_tab_feed_mode,
)
from test_modes import (
    DETAILS_ONE,
    DETAILS_TWO,
    FEED_RECOMMEND,
    FEED_SECOND,
    INFO_ONE,
    INFO_TWO,
    PROFILE_ONE,
    PROFILE_TWO,
    SEARCH_VIDEO_TAB_LINES,
    FakeModeUi,
    line,
)

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

    def clear_text(self):
        self.cleared = getattr(self, "cleared", 0) + 1

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


class FeedAuthorTests(unittest.TestCase):
    def test_a_new_video_notice_is_not_part_of_the_name(self):
        from collection import author_in_feed
        for notice in ("6小时前有新视频", " 3天前有新视频", "有新视频"):
            feed = [line("王林的化凡生涯" + notice, 118, 1478, 300, 34)]
            self.assertEqual(author_in_feed(feed, 1605)["text"], "王林的化凡生涯")


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

    def test_published_reply_links_parent_event_id_and_backfills_repeat_commenter_profile(self):
        from comments import resolve_reply_targets, save_users
        from publisher import WechatPublisher

        sent = []

        def fake_transport(events, **_kwargs):
            sent.extend(events)
            return {"discoveryAckIds": [e["event_id"] for e in events], "pendingEventIds": [], "uploaded": True}

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "v.sqlite3"
            video, _ = LocalStore(path).save(payload())
            c1 = self.comment("平安健康（尚…", "太香了", commentLevel=1, ipLocation="河南")
            c2 = self.comment("岚夕", "我也要试试", commentLevel=2, parentCommentKey=c1["key"], replyToNickname="平安健康（尚…", ipLocation="湖南")
            c3 = self.comment("平安健康（尚…", "回复岚夕:很好做", commentLevel=2, parentCommentKey=c1["key"], replyToNickname="岚夕", ipLocation="河南")
            c4 = self.comment("岚夕", "回复平安健康（尚…:谢谢", commentLevel=2, parentCommentKey=c1["key"], replyToNickname="平安健康（尚…", ipLocation="湖南")
            c_other = self.comment("岚夕", "另一个同名用户", commentLevel=1, ipLocation="北京")

            profiles = [
                {
                    "commentKey": c1["key"],
                    "nickname": "平安健康（尚爱玲）",
                    "commentNickname": "平安健康（尚…",
                    "_commentIpLocation": "河南",
                    "douyinId": "sphTestChannels01",
                    "accountType": "channels",
                    "identityMethod": "wechat-channel-id",
                    "ipLocation": "河南",
                    "profileLocation": "河南 郑州",
                    "profileVisited": True,
                    "touchStatus": "visited",
                    "profileActionsPerformed": [],
                },
                {
                    "commentKey": c2["key"],
                    "nickname": "岚夕",
                    "commentNickname": "岚夕",
                    "_commentIpLocation": "湖南",
                    "douyinId": None,
                    "accountType": "wechat_user",
                    "identityMethod": "wechat-user-card",
                    "ipLocation": "湖南",
                    "profileLocation": "维也纳",
                    "profileVisited": True,
                    "touchStatus": "visited_no_channels_id",
                    "profileActionsPerformed": [],
                },
            ]

            all_comments = [c1, c2, c3, c4, c_other]
            # Simulate streaming per-comment publishing (as collect_visible_comments does).
            with WechatPublisher(
                path,
                records=Path(root) / "uploads",
                identity={"scope": "e" * 64, "machine": "dev-1"},
                transport=fake_transport,
            ) as pub:
                pub.enqueue([video])
                pub.flush()
                for c in all_comments:
                    resolve_reply_targets(all_comments, profiles)
                    c_events = save_comments(path, video, [c])
                    u_events = save_users(path, c_events, profiles)
                    pub.enqueue([*c_events, *u_events])
                    pub.flush()

        by_type = {}
        for ev in sent:
            by_type.setdefault(ev["event_type"], []).append(ev)
        self.assertEqual(len(by_type["VIDEO"]), 1)
        self.assertEqual(len(by_type["COMMENT"]), 5)
        self.assertEqual(len(by_type["USER"]), 2)

        video_ev = by_type["VIDEO"][0]
        c_by_content = {e["payload"]["content"]: e for e in by_type["COMMENT"]}
        u_by_nick = {e["payload"]["nickname"]: e for e in by_type["USER"]}

        # 1. parentCommentKey & replyToCommentKey equal the referenced COMMENT's event_id (not dedupe_key),
        # while parent_event_id remains the VIDEO's event_id.
        ev1 = c_by_content["太香了"]
        ev2 = c_by_content["我也要试试"]
        ev3 = c_by_content["回复岚夕:很好做"]
        ev4 = c_by_content["回复平安健康（尚…:谢谢"]
        ev_other = c_by_content["另一个同名用户"]

        for ce in (ev1, ev2, ev3, ev4, ev_other):
            self.assertEqual(ce["parent_event_id"], video_ev["event_id"])
        self.assertIsNone(ev1["payload"].get("parentCommentKey"))
        self.assertEqual(ev2["payload"]["parentCommentKey"], ev1["event_id"])
        self.assertEqual(ev2["payload"]["replyToCommentKey"], ev1["event_id"])
        self.assertEqual(ev3["payload"]["parentCommentKey"], ev1["event_id"])
        self.assertEqual(ev3["payload"]["replyToCommentKey"], ev2["event_id"])
        self.assertEqual(ev4["payload"]["parentCommentKey"], ev1["event_id"])
        self.assertNotEqual(ev2["payload"]["parentCommentKey"], ev1["dedupe_key"])

        # 2. Repeat comments from the same person inherit userProfileEventId, authorWechatChannelsId, and touchStatus
        # without creating duplicate USER events; a namesake in another region is NOT linked.
        u_channels = u_by_nick["平安健康（尚爱玲）"]
        u_wechat = u_by_nick["岚夕"]

        self.assertEqual(ev1["payload"]["userProfileEventId"], u_channels["event_id"])
        self.assertEqual(ev1["payload"]["authorWechatChannelsId"], "sphTestChannels01")
        self.assertEqual(ev1["payload"]["touchStatus"], "visited")

        self.assertEqual(ev3["payload"]["userProfileEventId"], u_channels["event_id"])
        self.assertEqual(ev3["payload"]["authorWechatChannelsId"], "sphTestChannels01")
        self.assertEqual(ev3["payload"]["touchStatus"], "visited")

        self.assertEqual(ev2["payload"]["userProfileEventId"], u_wechat["event_id"])
        self.assertEqual(ev2["payload"]["touchStatus"], "visited_no_channels_id")

        self.assertEqual(ev4["payload"]["userProfileEventId"], u_wechat["event_id"])
        self.assertEqual(ev4["payload"]["touchStatus"], "visited_no_channels_id")

        self.assertIsNone(ev_other["payload"].get("userProfileEventId"))
        self.assertIsNone(ev_other["payload"].get("touchStatus"))


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

    def test_without_a_search_button_the_comment_box_reads_the_link_and_is_emptied(self):
        drawer = [line("评论19", 27, 482, 110, 32), control("发表评论", 101, 1537, 458, 34, cls="android.widget.EditText")]
        pasted = [control(LINK, 101, 900, 458, 60, cls="android.widget.EditText")]
        emptied = [control("发表评论", 101, 900, 458, 60, cls="android.widget.EditText")]
        ui = ShareUi([self.SHEET_SCROLLED, FEED_SECOND, FEED_SECOND, drawer, pasted, emptied, drawer, FEED_SECOND])
        with patch("collection.time.sleep"):
            link = copy_share_link(ui, FEED_SECOND)
        self.assertEqual(link, LINK)
        self.assertEqual(ui.keys, [279])
        self.assertEqual(ui.cleared, 1)
        self.assertEqual([c["text"] for c in ui.clicks], ["share", "复制链接", "19", "发表评论"])
        self.assertEqual(ui.backs, 1)

    def test_a_link_that_stays_in_the_comment_box_stops_the_run(self):
        drawer = [line("评论19", 27, 482, 110, 32), control("发表评论", 101, 1537, 458, 34, cls="android.widget.EditText")]
        pasted = [control(LINK, 101, 900, 458, 60, cls="android.widget.EditText")]
        ui = ShareUi([self.SHEET_SCROLLED, FEED_SECOND, FEED_SECOND, drawer, pasted, pasted, pasted])
        with patch("collection.time.sleep"), self.assertRaisesRegex(RuntimeError, "could not be removed"):
            copy_share_link(ui, FEED_SECOND)
        self.assertEqual(ui.cleared, 2)

    def test_an_ocr_reading_or_a_stale_clipboard_is_not_saved(self):
        with self.assertRaisesRegex(ValueError, "could not be read"):
            self.run_copy([line(LINK, 72, 81, 440, 86)])
        pasted = [control(LINK, 72, 81, 440, 86, cls="android.widget.EditText")]
        with self.assertRaisesRegex(ValueError, "previous video"):
            self.run_copy(pasted, previous=LINK)

    def test_direct_clipboard_read_skips_search_and_composer_paste(self):
        ui = ShareUi([self.SHEET_SCROLLED, FEED_SECOND])
        ui.read_clipboard = lambda: {"text": f"看看这个视频 {LINK}", "timestamp": 9_999_999_999_999}
        with patch("collection.time.sleep"):
            link = copy_share_link(ui, FEED_SECOND)
        self.assertEqual(link, LINK)
        self.assertEqual([c["text"] for c in ui.clicks], ["share", "复制链接"])
        self.assertEqual(ui.keys, [])
        self.assertEqual(getattr(ui, "cleared", 0), 0)
        self.assertEqual(ui.backs, 0)


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

    def test_a_video_saved_by_an_earlier_run_is_skipped(self):
        # Huawei run 251ba8b0 collected 与琪说o again: saved videos are recorded
        # under their bare dedupe key, which the feed check did not look for.
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
            key = identity("阿星探店", "今天分享一道快手家常菜#美食")
            (Path(root) / "seen.json").write_text(json.dumps([f"collect:{key}", f"comments:{key}"]))
            ui = FakeModeUi([FEED_SECOND])
            with patch("modes.time.sleep"):
                report = run_tab_feed_mode(ui, Path(root), tab="recommend", count=1, max_swipes=0,
                                           inspect_comments=True)
            self.assertEqual(report["processedCount"], 0)
            self.assertEqual([s["kind"] for s in report["skippedItems"]], ["already_seen"])

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

    def test_error_in_recommend_feed_recovers_skips_broken_video_and_collects_next(self):
        broken_profile = [line("喜剧之王单口季", 192, 509, 210, 34)]
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
            timings = Timings(Path(root) / "timings.jsonl", "run-rec-skip")
            ui = FakeModeUi(
                [
                    FEED_RECOMMEND,  # switch_channels_top_tab resume
                    broken_profile,  # video 1 author-profile -> missing controls -> ValueError
                    PROFILE_ONE,     # recover-check-0 -> user_profile -> back()
                    FEED_RECOMMEND,  # recover-check-1 -> recommend_feed recovered
                    FEED_SECOND,     # recommend-feed-1 after scroll_feed_next()
                    PROFILE_TWO,     # video 2 author-profile
                    [line("更多信息", 280, 1400)],
                    INFO_TWO,        # video 2 basic-info
                    DETAILS_TWO,     # video 2 details
                ]
            )
            with patch("modes.time.sleep"), patch("collection.time.sleep"):
                report = run_tab_feed_mode(ui, Path(root), tab="recommend", count=1, max_swipes=5, timings=timings)

            self.assertEqual(report["processedCount"], 1)
            self.assertEqual(report["videos"][0]["author"], "阿星探店")
            self.assertEqual(len(report["failedItems"]), 1)
            self.assertEqual(report["failedItems"][0]["author"], "喜剧之王单口季")
            self.assertTrue(report["failedItems"][0]["recovered"])
            self.assertEqual(report["failedItems"][0]["recoverySteps"], 1)
            self.assertEqual(ui.swipes_next, 1)

            seen = json.loads((Path(root) / "seen.json").read_text(encoding="utf-8"))
            broken_key = identity("喜剧之王单口季", "凭什么脂肪伸缩自如，毛囊却自动退休？#脱口秀")
            saved_key = identity("阿星探店", "今天分享一道快手家常菜#美食")
            self.assertTrue(any(saved_key in item for item in seen))
            self.assertFalse(any(broken_key in item for item in seen))
            self.assertTrue(any(r.get("kind") == "videoSkippedOnError" for r in timings.rows)
                            or "videoSkippedOnError" in (Path(root) / "timings.jsonl").read_text(encoding="utf-8"))

    def test_error_in_search_mode_backs_out_to_grid_and_skips_failed_card(self):
        opened_search_video = [
            line("#徐志胜这段#脱口秀就该直接封神！", 29, 1369, 665, 28),
            line("腾讯娱乐", 119, 1477, 150, 34),
            line("转发", 441, 1533, 53, 23),
            line("288", 636, 1533, 47, 25),
        ]
        deleted_video = [line("该内容无法查看", 260, 760, 200, 34)]
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
            ui = FakeModeUi(
                [
                    SEARCH_VIDEO_TAB_LINES,  # search_keyword reuse check
                    SEARCH_VIDEO_TAB_LINES,  # apply_search_filters after sort click
                    deleted_video,           # card 0 opened -> not channels_feed
                    deleted_video,           # card 0 retry -> feed_video_preview raises ValueError
                    opened_search_video,     # recover-check-0 -> bare video -> back_from_search_video
                    SEARCH_VIDEO_TAB_LINES,  # recover-check-1 -> back on search_results!
                    SEARCH_VIDEO_TAB_LINES,  # idx=1 search-grid-2 -> skips card 0, selects card 1
                    FEED_SECOND,             # card 1 opened
                    PROFILE_TWO,             # card 1 author-profile
                    [line("更多信息", 280, 1400)],
                    INFO_TWO,                # card 1 basic-info
                    DETAILS_TWO,             # card 1 details
                    SEARCH_VIDEO_TAB_LINES,  # card 1 search-return-2
                ]
            )
            with patch("modes.time.sleep"), patch("collection.time.sleep"):
                report = run_search_mode(ui, Path(root), keyword="脱口秀", count=1, max_screens=2)

            self.assertEqual(report["processedCount"], 1)
            self.assertEqual(len(report["failedItems"]), 1)
            self.assertEqual(report["failedItems"][0]["author"], "腾讯娱乐")
            self.assertTrue(report["failedItems"][0]["recovered"])
            self.assertEqual(report["recoveryEvents"][0]["actions"], ["back_from_search_video"])
            self.assertEqual(ui.texts_set, [])
            seen = json.loads((Path(root) / "seen.json").read_text(encoding="utf-8"))
            self.assertFalse(any(report["failedItems"][0]["cardKey"] in item for item in seen))

    def test_consecutive_error_circuit_breaker_and_reset_on_success(self):
        feed_a = [*FEED_RECOMMEND[:4], line("作品A", 29, 1380, 200, 28), line("作者甲", 118, 1478, 120, 34), *FEED_RECOMMEND[6:]]
        feed_b = [*FEED_RECOMMEND[:4], line("作品B", 29, 1380, 200, 28), line("作者乙", 118, 1478, 120, 34), *FEED_RECOMMEND[6:]]
        feed_c = [*FEED_RECOMMEND[:4], line("作品C", 29, 1380, 200, 28), line("作者丙", 118, 1478, 120, 34), *FEED_RECOMMEND[6:]]
        feed_d = [*FEED_RECOMMEND[:4], line("作品D", 29, 1380, 200, 28), line("作者丁", 118, 1478, 120, 34), *FEED_RECOMMEND[6:]]

        # 3 errors in a row -> circuit breaker stops the run immediately.
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
            ui = FakeModeUi([feed_a, feed_a, feed_b, feed_b, feed_c])
            with patch("modes.time.sleep"), patch(
                "modes._process_open_video_item", side_effect=ValueError("Author profile controls are missing")
            ):
                with self.assertRaisesRegex(RuntimeError, "连续 3 个视频采集报错"):
                    run_tab_feed_mode(ui, Path(root), tab="recommend", count=2, max_swipes=10)
            self.assertEqual(ui.swipes_next, 2)

        # 2 errors -> 1 success -> 2 errors -> 1 success resets the counter and finishes cleanly.
        outcomes = [
            ValueError("broken 1"),
            ValueError("broken 2"),
            {
                "completedPurposes": ["collect"],
                "failedActions": [],
                "eventId": "ev-1",
                "dedupeKey": identity("作者丙", "作品C"),
                "sourceMode": "RECOMMEND",
                "author": "作者丙",
                "description": "作品C",
                "authorWechatChannelsId": "sphC000001",
                "videoUrl": None,
                "previewKey": "wxch-preview:" + identity("作者丙", "作品C"),
                "authorKey": "wxch-author:" + identity("作者丙"),
            },
            ValueError("broken 3"),
            ValueError("broken 4"),
            {
                "completedPurposes": ["collect"],
                "failedActions": [],
                "eventId": "ev-2",
                "dedupeKey": identity("阿星探店", "今天分享一道快手家常菜#美食"),
                "sourceMode": "RECOMMEND",
                "author": "阿星探店",
                "description": "今天分享一道快手家常菜#美食",
                "authorWechatChannelsId": "sphFoodStar888",
                "videoUrl": None,
                "previewKey": "wxch-preview:" + identity("阿星探店", "今天分享一道快手家常菜#美食"),
                "authorKey": "wxch-author:" + identity("阿星探店"),
            },
        ]
        feed_e = [*FEED_RECOMMEND[:4], line("作品E", 29, 1380, 200, 28), line("作者戊", 118, 1478, 120, 34), *FEED_RECOMMEND[6:]]
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
            ui = FakeModeUi([
                feed_a, feed_a,  # fail 1 + recover
                feed_b, feed_b,  # fail 2 + recover
                feed_c,          # success 1 (resets consecutive_errors to 0)
                feed_d, feed_d,  # fail 3 + recover
                feed_e, feed_e,  # fail 4 + recover
                FEED_SECOND,     # success 2
            ])
            with patch("modes.time.sleep"), patch("modes._process_open_video_item", side_effect=outcomes):
                report = run_tab_feed_mode(ui, Path(root), tab="recommend", count=2, max_swipes=10)
            self.assertEqual(report["processedCount"], 2)
            self.assertEqual(len(report["failedItems"]), 4)

    def test_fatal_errors_abort_immediately_without_skipping(self):
        fatal_cases = (
            InterruptedError("Collection was stopped"),
            RuntimeError("Collection was stopped"),
            RuntimeError("The pasted link could not be removed from the comment box"),
            RuntimeError("Verification timeout; manual intervention did not complete within limit"),
            PermissionError("授权码或绑定设备验证失败"),
        )
        for fatal in fatal_cases:
            with self.subTest(error=repr(fatal)), tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                ui = FakeModeUi([FEED_RECOMMEND, FEED_SECOND])
                with patch("modes.time.sleep"), patch("modes._process_open_video_item", side_effect=fatal):
                    with self.assertRaises(type(fatal)):
                        run_tab_feed_mode(ui, Path(root), tab="recommend", count=2, max_swipes=5)
                self.assertEqual(ui.swipes_next, 0)

    def test_copied_link_before_mid_video_failure_updates_previous_link_guard(self):
        broken_profile = [line("喜剧之王单口季", 192, 509, 210, 34)]
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
            ui = FakeModeUi(
                [
                    FEED_RECOMMEND,  # resume
                    broken_profile,  # video 1 author-profile -> raises ValueError after link was copied!
                    FEED_RECOMMEND,  # recover-check-0 -> already on recommend_feed
                    FEED_SECOND,     # video 2 after swipe
                    FEED_SECOND,     # video 2 share-recover back_to_video
                    PROFILE_TWO,     # video 2 author-profile
                    [line("更多信息", 280, 1400)],
                    INFO_TWO,        # video 2 basic-info
                    DETAILS_TWO,     # video 2 details
                ]
            )

            def fake_copy_link(_ui, _lines, previous_link=None):
                if previous_link == LINK:
                    raise ValueError("The clipboard still holds the previous video's link")
                return LINK

            with patch("modes.time.sleep"), patch("collection.time.sleep"), patch(
                "modes.copy_share_link", side_effect=fake_copy_link
            ):
                report = run_tab_feed_mode(
                    ui,
                    Path(root),
                    tab="recommend",
                    count=1,
                    max_swipes=5,
                    collect_options={"copyShareLink": True},
                )

            self.assertEqual(report["processedCount"], 1)
            self.assertIsNone(report["videos"][0]["videoUrl"])
            self.assertIn("videoUrl", report["videos"][0]["videoEvent"]["payload"]["missingFields"])

    def test_transient_upload_failure_defers_to_sqlite_and_flushes_on_recovery(self):
        drawer_one = [
            *DETAILS_ONE[:4],
            line("路人甲广东1天前", 99, 730, 260, 28),
            line("太真实了哈哈", 99, 770, 220, 28),
            line("12", 650, 730, 36, 28),
            line("已显示全部评论", 260, 860, 180, 28),
            line("发表评论", 101, 1537, 200, 34),
        ]
        drawer_two = [
            *DETAILS_TWO[:4],
            line("吃货乙北京2小时前", 99, 730, 260, 28),
            line("看着很有食欲", 99, 770, 220, 28),
            line("5", 650, 730, 36, 28),
            line("已显示全部评论", 260, 860, 180, 28),
            line("发表评论", 101, 1537, 200, 34),
        ]
        identity_cfg = {"scope": "a" * 64, "machine": "device-001"}
        calls = []

        def flaky_transport(events, **_):
            calls.append([e["event_type"] for e in events])
            # Fail on Video 1's streaming upload; succeed once Video 2 starts uploading.
            if len(calls) == 1:
                raise TimeoutError("等待 discoveryAckIds 超时")
            ids = [e["event_id"] for e in events]
            return {"discoveryAckIds": ids, "pendingEventIds": [], "uploaded": True}

        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
            timings = Timings(Path(root) / "runs/r1/timings.jsonl", "r1")
            ui = FakeModeUi(
                [
                    FEED_RECOMMEND,  # switch_channels_top_tab
                    PROFILE_ONE,     # video 1 author-profile
                    [line("更多信息", 280, 1400)],
                    INFO_ONE,        # video 1 basic-info
                    drawer_one,      # video 1 single-open drawer (details + comments page 1)
                    FEED_SECOND,     # video 2 recommend-feed-1
                    PROFILE_TWO,     # video 2 author-profile
                    [line("更多信息", 280, 1400)],
                    INFO_TWO,        # video 2 basic-info
                    drawer_two,      # video 2 single-open drawer (details + comments page 1)
                ]
            )
            link_seq = iter(["https://weixin.qq.com/sph/LinkOne01", "https://weixin.qq.com/sph/LinkTwo02"])
            with patch("modes.time.sleep"), patch("collection.time.sleep"), patch(
                "modes.copy_share_link", side_effect=lambda *_a, **_kw: next(link_seq)
            ):
                result = run_collection_task(
                    ui,
                    Path(root),
                    {"entry": "recommend", "count": 2, "commentOptions": {"visitProfiles": False, "maxPages": 1}},
                    timings=timings,
                    publisher_identity=identity_cfg,
                    upload_transport=flaky_transport,
                )

            self.assertEqual(result["processedCount"], 2)
            # Video 1's comment did NOT trigger a second failing network timeout while Video 1 upload was deferred!
            self.assertEqual(calls[0], ["VIDEO"])
            self.assertEqual(result["upload"]["pendingCount"], 0)
            self.assertEqual(result["upload"]["acknowledgedCount"], 4)  # 2 VIDEO + 2 COMMENT
            log_text = (Path(root) / "runs/r1/timings.jsonl").read_text(encoding="utf-8")
            self.assertIn("discoveryDeferred", log_text)

        # Unauthorized error is never treated as transient and stops immediately.
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
            ui = FakeModeUi(
                [
                    FEED_RECOMMEND,
                    PROFILE_ONE,
                    [line("更多信息", 280, 1400)],
                    INFO_ONE,
                    DETAILS_ONE,
                ]
            )
            with patch("modes.time.sleep"), patch("collection.time.sleep"), patch(
                "modes.copy_share_link", return_value="https://weixin.qq.com/sph/LinkAuth1"
            ):
                with self.assertRaises(PermissionError):
                    run_collection_task(
                        ui,
                        Path(root),
                        {"entry": "recommend", "count": 1, "commentOptions": {"visitProfiles": False, "maxPages": 1}},
                        publisher_identity=identity_cfg,
                        upload_transport=lambda *_a, **_kw: (_ for _ in ()).throw(
                            PermissionError("授权码或绑定设备验证失败")
                        ),
                    )
            # Open drawer was closed before propagating PermissionError.
            self.assertEqual(ui.backs, 3)

    def test_composer_clear_command_failure_raises_fatal_error_and_stops_run(self):
        drawer = [
            line("评论19", 27, 482, 110, 32),
            control("发表评论", 101, 1537, 458, 34, cls="android.widget.EditText"),
        ]
        pasted = [control(LINK, 101, 900, 458, 60, cls="android.widget.EditText")]
        sheet = [line("投诉", 18, 1365, 36, 49), line("复制链接", 611, 1365, 72, 49)]
        for clear_exc in (
            subprocess.TimeoutExpired(["input", "keyevent"], 30),
            subprocess.CalledProcessError(1, ["input", "keyevent"]),
        ):
            with self.subTest(exc=type(clear_exc).__name__), tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                ui = ShareUi(
                    [
                        FEED_SECOND,  # switch_channels_top_tab
                        sheet,        # share-sheet
                        FEED_SECOND,  # share-closed
                        FEED_SECOND,  # share-feed (no search button -> composer)
                        drawer,       # share-paste-drawer
                        pasted,       # share-link-paste-composer
                    ]
                )
                ui.clear_text = lambda exc=clear_exc: (_ for _ in ()).throw(exc)
                with patch("modes.time.sleep"), patch("collection.time.sleep"):
                    with self.assertRaises(UnclearedComposerLinkError) as ctx:
                        run_tab_feed_mode(
                            ui,
                            Path(root),
                            tab="recommend",
                            count=2,
                            max_swipes=5,
                            collect_options={"copyShareLink": True},
                        )
                self.assertTrue(is_fatal_collection_error(ui, ctx.exception))
                self.assertEqual(ui.swipes_next, 0)

    def test_non_transient_upload_and_enqueue_errors_propagate_through_video_loop(self):
        identity_cfg = {"scope": "b" * 64, "machine": "device-002"}
        for bad_exc in (
            ValueError("服务端确认了本次请求之外的事件"),
            sqlite3.OperationalError("database disk image is malformed"),
        ):
            with self.subTest(exc=repr(bad_exc)), tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                ui = FakeModeUi(
                    [
                        FEED_RECOMMEND,
                        PROFILE_ONE,
                        [line("更多信息", 280, 1400)],
                        INFO_ONE,
                        DETAILS_ONE,
                        FEED_SECOND,
                    ]
                )
                with patch("modes.time.sleep"), patch("collection.time.sleep"), patch(
                    "modes.copy_share_link", return_value="https://weixin.qq.com/sph/LinkFatal1"
                ):
                    with self.assertRaises(type(bad_exc)):
                        run_collection_task(
                            ui,
                            Path(root),
                            {"entry": "recommend", "count": 2, "commentOptions": {"visitProfiles": False, "maxPages": 1}},
                            publisher_identity=identity_cfg,
                            upload_transport=lambda *_a, exc=bad_exc, **_kw: (_ for _ in ()).throw(exc),
                        )
                self.assertEqual(ui.swipes_next, 0)

    def test_http_401_and_local_disk_errors_are_not_deferred_as_transient_upload(self):
        http_401 = urllib.error.HTTPError("http://127.0.0.1:9999/upload", 401, "Unauthorized", None, None)
        http_403 = urllib.error.HTTPError("http://127.0.0.1:9999/upload", 403, "Forbidden", None, None)
        http_500 = urllib.error.HTTPError("http://127.0.0.1:9999/upload", 500, "Internal Server Error", None, None)
        disk_full = OSError(errno.ENOSPC, "No space left on device")
        missing_file = FileNotFoundError(errno.ENOENT, "No such file or directory")
        net_down = OSError(errno.ENETUNREACH, "Network is unreachable")

        self.assertFalse(is_transient_upload_error(http_401))
        self.assertFalse(is_transient_upload_error(http_403))
        self.assertTrue(is_transient_upload_error(http_500))
        self.assertFalse(is_transient_upload_error(disk_full))
        self.assertFalse(is_transient_upload_error(missing_file))
        self.assertTrue(is_transient_upload_error(net_down))

        identity_cfg = {"scope": "c" * 64, "machine": "device-003"}
        # 1. HTTP 401 from RemoteBridge transport immediately aborts without swiping.
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
            ui = FakeModeUi(
                [
                    FEED_RECOMMEND,
                    PROFILE_ONE,
                    [line("更多信息", 280, 1400)],
                    INFO_ONE,
                    DETAILS_ONE,
                    FEED_SECOND,
                ]
            )
            with patch("modes.time.sleep"), patch("collection.time.sleep"), patch(
                "modes.copy_share_link", return_value="https://weixin.qq.com/sph/Link401"
            ):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    run_collection_task(
                        ui,
                        Path(root),
                        {"entry": "recommend", "count": 2, "commentOptions": {"visitProfiles": False, "maxPages": 1}},
                        publisher_identity=identity_cfg,
                        upload_transport=lambda *_a, **_kw: (_ for _ in ()).throw(
                            urllib.error.HTTPError("http://127.0.0.1:9999/upload", 401, "Unauthorized", None, None)
                        ),
                    )
            self.assertEqual(ctx.exception.code, 401)
            self.assertEqual(ui.swipes_next, 0)

        # 2. Local disk write failure in publisher.flush() (before transport) immediately aborts.
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
            ui = FakeModeUi(
                [
                    FEED_RECOMMEND,
                    PROFILE_ONE,
                    [line("更多信息", 280, 1400)],
                    INFO_ONE,
                    DETAILS_ONE,
                    FEED_SECOND,
                ]
            )
            with patch("modes.time.sleep"), patch("collection.time.sleep"), patch(
                "modes.copy_share_link", return_value="https://weixin.qq.com/sph/LinkDisk"
            ), patch.object(Path, "write_text", side_effect=OSError(errno.ENOSPC, "No space left on device")):
                with self.assertRaises(OSError) as disk_ctx:
                    run_collection_task(
                        ui,
                        Path(root),
                        {"entry": "recommend", "count": 2, "commentOptions": {"visitProfiles": False, "maxPages": 1}},
                        publisher_identity=identity_cfg,
                        upload_transport=lambda events, **_kw: {
                            "discoveryAckIds": [e["event_id"] for e in events],
                            "pendingEventIds": [],
                            "uploaded": True,
                        },
                    )
            self.assertEqual(disk_ctx.exception.errno, errno.ENOSPC)
            self.assertEqual(ui.swipes_next, 0)

    def test_single_drawer_uploads_video_first_then_streams_comments_one_by_one(self):
        drawer = [
            *DETAILS_ONE[:4],
            line("小王同学广东1天前", 99, 730, 260, 28),
            line("这段脱口秀太好笑了", 99, 770, 240, 28),
            line("12", 650, 730, 36, 28),
            line("美食阿强浙江2小时前", 99, 830, 260, 28),
            line("转发给朋友一起看", 99, 870, 240, 28),
            line("3", 650, 830, 36, 28),
            line("已显示全部评论", 260, 960, 180, 28),
            line("发表评论", 101, 1537, 200, 34),
        ]
        card_a = [
            line("小王同学", 180, 1180, 140, 32),
            line("地区：广东 深圳", 180, 1230, 220, 28),
            line("设置", 610, 1080, 56, 32),
        ]
        card_b = [
            line("美食阿强", 180, 1180, 140, 32),
            line("地区：浙江 杭州", 180, 1230, 220, 28),
            line("设置", 610, 1080, 56, 32),
        ]
        sheet = [line("投诉", 18, 1365, 36, 49), line("复制链接", 611, 1365, 72, 49)]
        uploaded_batches = []

        def record_transport(events, **_):
            uploaded_batches.append([(e["event_type"], e["payload"]) for e in events])
            return {
                "discoveryAckIds": [e["event_id"] for e in events],
                "pendingEventIds": [],
                "uploaded": True,
            }

        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
            ui = ShareUi(
                [
                    FEED_RECOMMEND,  # initial bare feed snapshot (ONLY feed snapshot!)
                    sheet,           # share-sheet
                    FEED_RECOMMEND,  # share-closed
                    PROFILE_ONE,     # author-profile
                    [line("更多信息", 280, 1400)],
                    INFO_ONE,        # author-basic-info-ready
                    drawer,          # comments-details (drawer opened ONCE!)
                    card_a,          # commenter 1 user card
                    drawer,          # comments-return after commenter 1
                    card_b,          # commenter 2 user card
                    drawer,          # comments-return after commenter 2
                ]
            )
            ui.read_clipboard = lambda: {"text": LINK, "timestamp": 9_999_999_999_999}
            with patch("modes.time.sleep"), patch("collection.time.sleep"):
                result = run_collection_task(
                    ui,
                    Path(root),
                    {
                        "entry": "recommend",
                        "count": 1,
                        "commentOptions": {"visitProfiles": True, "maxProfiles": 2, "maxPages": 1},
                    },
                    publisher_identity={"scope": "d" * 64, "machine": "device-004"},
                    upload_transport=record_transport,
                )

            self.assertEqual(result["processedCount"], 1)
            # Strict event upload order: VIDEO first, then each (COMMENT + USER) one by one!
            self.assertEqual(
                [[kind for kind, _ in batch] for batch in uploaded_batches],
                [["VIDEO"], ["COMMENT", "USER"], ["COMMENT", "USER"]],
            )
            self.assertEqual(uploaded_batches[0][0][1]["videoUrl"], LINK)
            self.assertEqual(uploaded_batches[0][0][1]["authorWechatChannelsId"], "sphComedyKing01")
            self.assertEqual(uploaded_batches[0][0][1]["description"], "凭什么脂肪伸缩自如，毛囊却自动退休？#脱口秀")
            self.assertEqual(uploaded_batches[1][0][1]["content"], "这段脱口秀太好笑了")
            self.assertEqual(uploaded_batches[2][0][1]["content"], "转发给朋友一起看")
            # Comment button ("288") was clicked ONLY ONCE; zero redundant "before-author" or "comments" open!
            self.assertEqual([c.get("text") for c in ui.clicks].count("288"), 1)
            self.assertNotIn("before-author", ui.labels)
            self.assertNotIn("comments", ui.labels)


if __name__ == "__main__":
    unittest.main()
