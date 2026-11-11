"""Unit tests for WeChat Channels Batch 4 modes, search, filters, live/ad skipping,
captcha pause, and bounded recovery."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))

from collection import LocalStore, identity
from interactions import DailyQuotaStore
from modes import (
    _process_open_video_item,
    already_processed,
    mark_processed,
    mode_comment_limits,
    mode_failures,
    validate_mode_config,
    parse_search_video_cards,
    is_search_results_page,
    active_top_tab,
    apply_search_filters,
    bounded_recover_to_target,
    call_with_captcha_pause,
    classify_mode_page,
    detect_abnormal_state,
    detect_feed_skip_item,
    dismiss_location_permission_modal,
    feed_video_preview,
    find_search_filter_option,
    find_top_tabs,
    is_captcha_lines,
    is_search_input_page,
    is_search_results_page,
    load_seen_keys,
    parse_search_video_cards,
    run_auto_mode,
    run_mode,
    run_recommend_mode,
    run_search_mode,
    run_tab_feed_mode,
    save_seen_keys,
    search_keyword,
    select_search_card,
    switch_channels_top_tab,
    verify_stage_page,
    wait_for_captcha_resolution,
)


def line(text, x, y, w=80, h=30, **extra):
    return {"text": text, "box": [x, y, w, h], "score": 0.99, **extra}


def make_frame(width=720, height=1605, default_bgr=(30, 30, 30), patches=()):
    frame = [[list(default_bgr) for _ in range(width)] for _ in range(height)]
    for box, bgr in patches:
        x, y, w, h = box
        for py in range(max(0, y), min(height, y + h)):
            row = frame[py]
            for px in range(max(0, x), min(width, x + w)):
                row[px] = list(bgr)
    return frame


FEED_RECOMMEND = [
    line("关注", 192, 76, 67, 32),
    line("朋友♡", 302, 78, 96, 30),
    line("推荐", 441, 76, 64, 32, selected=True),
    line("凭什么脂肪伸缩自如，毛囊却自动退休？#脱口秀", 29, 1369, 665, 28),
    line("喜剧之王单口季", 119, 1477, 194, 34),
    line("4738", 344, 1533, 59, 23),
    line("7597", 441, 1533, 53, 23),
    line("2497", 534, 1533, 57, 25),
    line("288", 636, 1533, 47, 25),
]

FEED_SECOND = [
    line("关注", 192, 76, 67, 32),
    line("朋友♡", 302, 78, 96, 30),
    line("推荐", 441, 76, 64, 32, selected=True),
    line("今天分享一道快手家常菜#美食", 29, 1380, 520, 28),
    line("阿星探店", 118, 1478, 150, 34),
    line("120", 344, 1533, 55, 23),
    line("45", 441, 1533, 45, 23),
    line("88", 534, 1533, 45, 25),
    line("19", 636, 1533, 42, 25),
]

DETAILS_ONE = [
    line("评论288", 27, 482, 120, 32),
    line("喜剧之王单口季作者上海11月6日", 99, 568, 380, 28),
    line("凭什么脂肪伸缩自如，毛囊却自动退休？#脱口秀", 98, 608, 590, 28),
    line("原声·喜剧之王单口季", 102, 680, 260, 28),
    line("暂无评论", 280, 780, 120, 28),
]

PROFILE_ONE = [
    line("喜剧之王单口季", 192, 509, 210, 34),
    line("上海", 192, 552, 80, 26),
    line("官方喜剧节目账号", 35, 644, 260, 28),
    line("115条原创内容", 35, 682, 180, 28),
    line("关注", 162, 760, 56, 32),
    line("私信", 496, 760, 56, 32),
]

INFO_ONE = [
    line("基础信息", 40, 479, 120, 32),
    line("IP归属地", 40, 608, 125, 28),
    line("上海", 250, 607, 68, 28),
    line("资料所在地", 40, 670, 140, 28),
    line("上海", 252, 673, 80, 28),
    line("视频号ID", 41, 737, 126, 28),
    line("sphComedyKing01", 250, 735, 278, 28),
]

DETAILS_TWO = [
    line("评论19", 27, 482, 110, 32),
    line("阿星探店作者广东11月7日", 99, 568, 340, 28),
    line("今天分享一道快手家常菜#美食", 98, 608, 520, 28),
    line("原声·阿星探店", 102, 680, 240, 28),
    line("暂无评论", 280, 780, 120, 28),
]

PROFILE_TWO = [
    line("阿星探店", 192, 509, 160, 34),
    line("广东广州", 192, 552, 120, 26),
    line("每天探访街头美味", 35, 644, 260, 28),
    line("52条原创内容", 35, 682, 180, 28),
    line("关注", 162, 760, 56, 32),
    line("私信", 496, 760, 56, 32),
]

INFO_TWO = [
    line("基础信息", 40, 479, 120, 32),
    line("IP归属地", 40, 608, 125, 28),
    line("广东", 250, 607, 68, 28),
    line("资料所在地", 40, 670, 140, 28),
    line("广东广州", 252, 673, 110, 28),
    line("视频号ID", 41, 737, 126, 28),
    line("sphFoodStar888", 250, 735, 278, 28),
]

# Real V2131A OCR snapshot of Search Video Tab ("脱口秀")
SEARCH_VIDEO_TAB_LINES = [
    line("脱口秀", 89, 96, 97, 37),
    line("X", 467, 108, 15, 10),
    line("搜索", 526, 95, 65, 38),
    line("取消", 642, 98, 64, 34),
    line("全部", 24, 185, 60, 32),
    line("视频", 131, 185, 62, 32),
    line("账号", 240, 185, 61, 32),
    line("直播", 347, 185, 63, 32),
    line("商品", 456, 185, 59, 34),
    line("不限", 46, 272, 63, 32),
    line("最新", 143, 272, 62, 32),
    line("最热", 242, 272, 59, 32),
    line("时长〈", 351, 270, 95, 35),
    line("范围", 501, 272, 61, 32),
    # Left card 1
    line("05:48", 25, 769, 84, 23),
    line("1.5万", 263, 772, 77, 23),
    line("#徐志胜这段#脱口秀就该", 25, 822, 303, 28),
    line("直接封神！", 25, 849, 116, 30),
    line("腾讯娱乐", 64, 891, 136, 28),
    line("3个月前", 254, 894, 88, 27),
    # Right card 1
    line("04:33", 370, 769, 73, 23),
    line("凸2770", 621, 772, 73, 23),
    line("这应该是“军旅脱口秀”的", 379, 820, 308, 29),
    line("天花板了吧！#脱口秀#···", 379, 850, 296, 29),
    line("幽默的小伍狼", 380, 891, 198, 33),
    line("17小时前", 574, 896, 116, 23),
    # Non-video inline section
    line("脱口秀－视频号", 41, 969, 220, 34),
    line("更多>", 598, 969, 85, 30),
    line("黄西脱口秀", 179, 1053, 153, 30),
    line("已关注", 610, 1053, 68, 25),
    # Left card 2
    line("06:41", 25, 1372, 85, 24),
    line("凸10万+", 256, 1372, 81, 25),
    line("小罗不愧是文本型选手，梗", 27, 1422, 307, 29),
    line("都是又高级又幽默！#小·…", 27, 1452, 298, 29),
    line("腾讯娱乐", 65, 1498, 135, 23),
    line("1年前", 278, 1496, 64, 25),
    # Right card 2
    line("02:00", 375, 1370, 68, 26),
    line("10万+", 609, 1372, 83, 25),
    line("我真的太快乐了，不信你点", 377, 1421, 308, 31),
    line("进我主页看看#脱口秀和T··", 380, 1454, 308, 23),
    line("李琪宝没···", 379, 1496, 190, 28),
    line("4个月前", 569, 1498, 121, 23),
]

SEARCH_FILTER_PANEL_LINES = [
    line("脱口秀", 89, 96, 97, 37),
    line("搜索", 609, 95, 66, 38),
    line("全部", 22, 183, 64, 34),
    line("视频", 131, 183, 62, 34),
    line("三筛选", 598, 187, 96, 30),
    line("排序", 24, 274, 62, 38),
    line("不限", 167, 277, 59, 32),
    line("最新", 368, 277, 59, 32),
    line("最热", 571, 277, 55, 33),
    line("范围", 25, 364, 61, 32),
    line("不限", 167, 364, 59, 32),
    line("已关注", 358, 364, 81, 32),
    line("最近看过", 543, 364, 106, 30),
    line("时长", 25, 451, 61, 32),
    line("不限", 167, 451, 59, 32),
    line("5分钟以下", 337, 451, 119, 30),
    line("5-20分钟", 540, 451, 114, 30),
    line("20分钟以上", 129, 523, 135, 30),
    line("收起", 25, 626, 89, 31),
    line("重置", 635, 625, 59, 32),
]


class FakeModeUi:
    height = 1605

    def __init__(self, snapshots, frames=None):
        self.snapshots = list(snapshots)
        self._snap_idx = 0
        self.frames = list(frames or [])
        self._frame_idx = 0
        self.clicks = []
        self.backs = 0
        self.swipes_next = 0
        self.swipes_prev = 0
        self.texts_set = []
        self.labels = []
        self._stopped = False

    def stopped(self):
        return self._stopped

    def snapshot(self, label, author=None):
        self.labels.append(label)
        if self._snap_idx < len(self.snapshots):
            item = self.snapshots[self._snap_idx]
            self._snap_idx += 1
            return item
        return self.snapshots[-1] if self.snapshots else []

    def capture_frame(self):
        if not self.frames:
            return None
        if self._frame_idx < len(self.frames):
            f = self.frames[self._frame_idx]
            self._frame_idx += 1
            return f
        return self.frames[-1]

    def click(self, target):
        self.clicks.append(target)

    def back(self):
        self.backs += 1

    def set_text(self, text):
        self.texts_set.append(text)

    def scroll_feed_next(self):
        self.swipes_next += 1

    def scroll_feed_prev(self):
        self.swipes_prev += 1

    def profile_more(self, name):
        self.clicks.append({"text": "profile_more", "box": name["box"]})

    @contextlib.contextmanager
    def prepare_basic_info(self):
        yield None

    def wait_basic_info(self, timeout=8, reader=None):
        from layout import basic_info_ready

        lines = self.snapshot("author-basic-info-ready")
        if not basic_info_ready(lines):
            raise ValueError("Author basic information was not ready within 8 seconds")
        return lines


class ProcessedRecordTests(unittest.TestCase):
    def test_collected_video_is_still_offered_to_interactions(self):
        seen = set()
        mark_processed(seen, ["collect"], ["wxch-video:a"])
        self.assertTrue(already_processed(seen, ("collect",), ["wxch-video:a"]))
        self.assertFalse(already_processed(seen, ("interact",), ["wxch-video:a"]))
        self.assertFalse(already_processed(seen, ("collect", "comments"), ["wxch-video:a"]))

    def test_author_records_never_skip_another_video(self):
        seen = {"wxch-author:x", "wxch-preview:old"}
        self.assertFalse(already_processed(seen, ("collect",), ["wxch-author:x", "wxch-preview:new"]))
        # A record from before purposes were kept is a collected video.
        self.assertTrue(already_processed(seen, ("collect",), ["wxch-preview:old"]))
        self.assertFalse(already_processed(seen, ("interact",), ["wxch-preview:old"]))

    def test_failed_interaction_is_not_recorded_and_fails_the_run(self):
        report = {"workLike": {"status": "performed"}, "workComment": {"status": "failed", "error": "x"}}
        ui = Mock(height=1605)
        with patch("modes.feed_video_preview", return_value={"author": "A", "captionPreview": "T", "previewKey": "p",
                                                             "authorKey": "wxch-author:A"}), \
                patch("modes.interact_work_and_comments", return_value=report), \
                patch("modes.check_stopped"):
            item = _process_open_video_item(ui, Path("."), lines=[], source_mode="RECOMMEND", store=Mock(),
                                            timings=None, quota_store=Mock(), collect_video=False,
                                            interaction_options={"likeWork": True, "commentText": "x"})
        self.assertEqual(item["completedPurposes"], [])
        self.assertEqual(item["failedActions"], ["workComment"])
        failures = mode_failures({"stages": [{"videos": [item]}, {"mode": "messages"}]})
        self.assertEqual(len(failures), 1)

    def test_comment_limits_reach_the_comment_collector(self):
        self.assertEqual(mode_comment_limits({"maxReplyExpansions": "3"}),
                         {"max_pages": 2, "max_reply_expansions": 3, "max_profiles": 1})
        self.assertEqual(mode_comment_limits({"unlimited": True}),
                         {"max_pages": None, "max_reply_expansions": None, "max_profiles": None})
        with patch("modes.collect_visible_comments") as collect, patch("modes.check_stopped"), \
                patch("modes.feed_video_preview", return_value={"author": "A", "captionPreview": "T",
                                                                "previewKey": "p", "authorKey": "k"}), \
                patch("modes.collect_video_in_mode", return_value={"event_id": "e", "dedupe_key": "d", "payload": {}}):
            _process_open_video_item(Mock(height=1605), Path("."), lines=[], source_mode="RECOMMEND",
                                     store=Mock(path="db"), timings=None, quota_store=Mock(),
                                     inspect_comments=True, comment_options={"maxReplyExpansions": 4})
        self.assertEqual(collect.call_args.kwargs["max_reply_expansions"], 4)

    def test_unusable_mode_configuration_is_rejected_before_the_phone(self):
        for config in ({"collectVideo": False, "inspectComments": True}, {"collectVideo": False},
                       {"inspectComments": True, "commentOptions": {"maxPages": "0"}},
                       {"mode": "auto", "stages": [{"stage": "nowhere"}]}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                validate_mode_config(config)
        validate_mode_config({"mode": "auto", "stages": [{"stage": "messages"}, {"stage": "search", "count": 1}]})

    def test_search_card_selection_uses_the_callers_record(self):
        card = {"cardKey": "c1", "author": "A", "title": "T", "clickBox": [0, 0, 1, 1]}
        ui = Mock(height=1605)
        with patch("modes.parse_search_video_cards", return_value=[card]), patch("modes.check_stopped"):
            self.assertIs(select_search_card(ui, lambda item: False, lines=[])[0], card)
            with self.assertRaises(RuntimeError):
                select_search_card(ui, lambda item: True, max_screens=1, lines=[])


class ControlSearchTests(unittest.TestCase):
    @staticmethod
    def desc(text, x, y, w, h):
        return {"text": text, "box": [x, y, w, h], "score": 1.0, "source": "hierarchy",
                "selector": {"attribute": "content-desc", "value": text, "resource": "", "class": "android.view.View"}}

    def test_tab_descriptions_identify_the_results_page(self):
        lines = [self.desc("全部\n第 1 个标签，共 5 个", 0, 182, 108, 68), self.desc("视频\n第 2 个标签，共 5 个", 108, 182, 108, 68)]
        self.assertTrue(is_search_results_page(lines))

    def test_card_descriptions_are_parsed_as_reported(self):
        card = self.desc("10万+\n“人群太吵了 我想去见山”\n小\u200b白\u200b呦\u2006y\n2\u2006个\u200b月\u200b前", 15, 351, 339, 610)
        truncated = self.desc("720\n...全景展现亚马逊河热带雨林风光", 367, 351, 339, 610)
        cards = parse_search_video_cards([card, truncated], 1559)
        self.assertEqual(len(cards), 1)
        self.assertEqual((cards[0]["author"], cards[0]["likeCountText"], cards[0]["publishTime"]),
                         ("小\u200b白\u200b呦\u2006y", "10万+", "2\u2006个\u200b月\u200b前"))
        x, y, w, h = cards[0]["clickBox"]
        self.assertTrue(15 <= x and x + w <= 354 and 351 <= y and y + h <= 961)


class ModesUnitTests(unittest.TestCase):
    def test_top_tabs_detection_and_merged_follow_box(self):
        merged = [
            line("<关注", 55, 76, 200, 32),
            line("朋友♡", 302, 78, 96, 30),
            line("推荐", 441, 76, 64, 32),
        ]
        tabs = find_top_tabs(merged)
        self.assertEqual(set(tabs.keys()), {"follow", "friends", "recommend"})
        self.assertEqual(tabs["follow"]["box"], [187, 76, 68, 32])

        # Verify active_top_tab via underline + text brightness on frame
        frame_friends = make_frame(
            patches=[
                ([302, 78, 96, 30], (215, 215, 215)),
                ([302, 116, 96, 12], (202, 202, 202)),
            ]
        )
        tab_name, metrics = active_top_tab(merged, frame=frame_friends)
        self.assertEqual(tab_name, "friends")
        self.assertGreater(metrics["friends"]["score"], metrics["recommend"]["score"])

    def test_switch_channels_top_tab_and_missing_city_handling(self):
        friends_active = [
            line("关注", 192, 76, 67, 32),
            line("朋友♡", 302, 78, 96, 30, selected=True),
            line("推荐", 441, 76, 64, 32),
            line("好友分享日常", 29, 1380, 300, 28),
            line("山山6342", 119, 1477, 160, 34),
            line("2.6万", 344, 1533, 59, 23),
        ]
        ui = FakeModeUi([FEED_RECOMMEND, friends_active])
        with patch("modes.time.sleep"):
            res = switch_channels_top_tab(ui, "friends")
        self.assertTrue(res["available"])
        self.assertTrue(res["clicked"])
        self.assertEqual(res["activeTab"], "friends")
        self.assertEqual(ui.clicks[-1]["text"], "朋友♡")

        # Missing city tab returns entry_unavailable cleanly
        ui_city = FakeModeUi([FEED_RECOMMEND, FEED_RECOMMEND])
        with patch("modes.time.sleep"):
            city_res = switch_channels_top_tab(ui_city, "city")
        self.assertFalse(city_res["available"])
        self.assertEqual(city_res["status"], "entry_unavailable")
        self.assertEqual(city_res["reason"], "city_tab_missing")

    def test_dismiss_location_permission_modal(self):
        modal_lines = [
            line("定位权限未开启", 180, 680, 360, 36),
            line("开启定位权限以发现同城内容", 140, 740, 440, 28),
            line("暂时不了", 160, 860, 140, 40),
            line("去设置", 420, 860, 140, 40),
        ]
        ui = FakeModeUi([modal_lines])
        self.assertTrue(dismiss_location_permission_modal(ui, modal_lines))
        self.assertEqual(ui.clicks[-1]["text"], "暂时不了")

    def test_detect_feed_skip_item_for_live_and_ads(self):
        live_preview = [
            line("关注", 192, 76, 67, 32),
            line("朋友", 302, 78, 96, 30),
            line("推荐", 441, 76, 64, 32),
            line("轻触进入直播间", 240, 820, 240, 36),
            line("主播小美", 119, 1477, 150, 34),
        ]
        self.assertEqual(detect_feed_skip_item(live_preview)["kind"], "live_preview")

        live_interior = [
            line("主播小美", 90, 75, 140, 30),
            line("1258人看过", 520, 75, 140, 28),
            line("聊聊吧...", 40, 1510, 200, 36),
        ]
        self.assertEqual(detect_feed_skip_item(live_interior)["kind"], "live_room")

        ad_feed = [
            *FEED_RECOMMEND[:5],
            line("广告", 620, 1475, 56, 26),
            line("了解详情", 80, 1425, 160, 34),
        ]
        self.assertEqual(detect_feed_skip_item(ad_feed)["kind"], "ad_card")
        self.assertFalse(detect_feed_skip_item(FEED_RECOMMEND)["skip"])

    def test_parse_search_video_cards_from_real_v2131a_snapshot(self):
        self.assertTrue(is_search_results_page(SEARCH_VIDEO_TAB_LINES, expected_keyword="脱口秀"))
        cards = parse_search_video_cards(SEARCH_VIDEO_TAB_LINES, height=1605)
        self.assertEqual(len(cards), 4)
        self.assertEqual(cards[0]["author"], "腾讯娱乐")
        self.assertEqual(cards[0]["title"], "#徐志胜这段#脱口秀就该直接封神！")
        self.assertEqual(cards[0]["publishTime"], "3个月前")
        self.assertEqual(cards[0]["duration"], "05:48")
        self.assertEqual(cards[1]["author"], "幽默的小伍狼")
        self.assertEqual(cards[1]["publishTime"], "17小时前")
        self.assertEqual(cards[2]["author"], "腾讯娱乐")
        self.assertEqual(cards[2]["publishTime"], "1年前")
        self.assertEqual(cards[3]["author"], "李琪宝没")
        self.assertEqual(cards[3]["publishTime"], "4个月前")
        # Ensure non-video account row ("黄西脱口秀") was excluded
        self.assertFalse(any("黄西" in c["author"] for c in cards))

    def test_search_filter_panel_and_inline_video_bar(self):
        opt = find_search_filter_option(SEARCH_FILTER_PANEL_LINES, "排序", "最新")
        self.assertEqual(opt["box"], [368, 277, 59, 32])
        dur_opt = find_search_filter_option(SEARCH_FILTER_PANEL_LINES, "时长", "5分钟以下")
        self.assertEqual(dur_opt["box"], [337, 451, 119, 30])
        scope_opt = find_search_filter_option(SEARCH_FILTER_PANEL_LINES, "范围", "已关注")
        self.assertEqual(scope_opt["box"], [358, 364, 81, 32])

        # Test inline filter bar on 视频 tab (selecting 最新 + 5分钟以下)
        duration_sheet = [
            *SEARCH_VIDEO_TAB_LINES[:14],
            line("时长", 330, 1280, 60, 32),
            line("不限", 160, 1370, 60, 32),
            line("5分钟以下", 450, 1370, 120, 32),
            line("5-20分钟", 140, 1460, 120, 32),
            line("20分钟以上", 450, 1460, 130, 32),
        ]
        after_duration = [
            *SEARCH_VIDEO_TAB_LINES[:12],
            line("5分钟以下 v", 351, 270, 125, 35),
            *SEARCH_VIDEO_TAB_LINES[13:],
        ]
        frame_before = make_frame(width=720, height=1605, default_bgr=(245, 245, 245))
        frame_after = make_frame(
            width=720,
            height=1605,
            default_bgr=(245, 245, 245),
            patches=[([143, 272, 62, 32], (20, 20, 20))],
        )
        ui = FakeModeUi(
            [SEARCH_VIDEO_TAB_LINES, duration_sheet, after_duration],
            frames=[frame_before, frame_after],
        )
        with patch("modes.time.sleep"):
            res = apply_search_filters(
                ui,
                sort_by="最新",
                duration="5分钟以下",
                lines=SEARCH_VIDEO_TAB_LINES,
            )
        self.assertEqual(res["status"], "search_filters_applied")
        self.assertEqual(res["sortBy"], "最新")
        self.assertEqual(res["duration"], "5分钟以下")
        self.assertEqual([c["text"] for c in ui.clicks], ["最新", "时长〈", "5分钟以下"])

    def test_captcha_zero_click_pause_and_resume(self):
        captcha_screen = [
            line("安全验证", 280, 220, 160, 38),
            line("请拖动滑块完成拼图", 200, 420, 320, 32),
        ]
        self.assertTrue(is_captcha_lines(captcha_screen))
        ui = FakeModeUi([captcha_screen, FEED_RECOMMEND, FEED_RECOMMEND])
        paused_called = []
        with patch("modes.time.sleep"), contextlib.redirect_stdout(io.StringIO()):
            res = wait_for_captcha_resolution(
                ui,
                poll_interval=0.05,
                max_wait_seconds=5.0,
                on_pause=lambda: paused_called.append(True),
            )
        self.assertEqual(res["status"], "resumed_after_verification")
        self.assertEqual(res["waitedPolls"], 2)
        self.assertEqual(ui.clicks, [])
        self.assertEqual(len(paused_called), 1)

    def test_bounded_recover_to_target_handles_popups_live_and_search(self):
        teen_modal = [
            line("青少年模式", 260, 620, 180, 36),
            line("为呵护未成年人健康成长，微信推出青少年模式", 100, 700, 520, 28),
            line("我知道了", 280, 880, 160, 42),
        ]
        live_room = [
            line("主播小美", 90, 75, 140, 30),
            line("在线观众", 520, 75, 140, 28),
            line("聊聊吧...", 40, 1510, 200, 36),
        ]
        ui = FakeModeUi([teen_modal, live_room, FEED_RECOMMEND])
        with patch("modes.time.sleep"):
            rec = bounded_recover_to_target(ui, target="recommend", max_steps=4)
        self.assertTrue(rec["recovered"])
        self.assertEqual(rec["stepsUsed"], 2)
        self.assertEqual(rec["actions"], ["dismiss:teenager_mode_modal", "back_from:live_room"])
        self.assertEqual(ui.clicks[0]["text"], "我知道了")
        self.assertEqual(ui.backs, 1)

    def test_run_recommend_mode_skips_seen_and_live_and_collects_multiple(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            seen_file, seen = load_seen_keys(root)
            pre_seen_preview = feed_video_preview(FEED_RECOMMEND)
            seen.add(pre_seen_preview["previewKey"])
            save_seen_keys(seen_file, seen)

            live_card = [
                line("关注", 192, 76, 67, 32),
                line("朋友♡", 302, 78, 96, 30),
                line("推荐", 441, 76, 64, 32, selected=True),
                line("轻触进入直播间", 240, 820, 240, 36),
            ]
            ui = FakeModeUi(
                [
                    FEED_RECOMMEND,  # resume-check (already seen -> swipe 1)
                    live_card,       # swipe 1 lands on live preview -> swipe 2
                    FEED_SECOND,     # swipe 2 lands on unseen video 2
                    DETAILS_TWO,     # video-details
                    FEED_SECOND,     # before-author
                    PROFILE_TWO,     # author-profile
                    [line("更多信息", 280, 1400)],
                    INFO_TWO,        # author-basic-info-ready
                ]
            )
            with patch("modes.time.sleep"):
                report = run_recommend_mode(ui, root, count=1, max_swipes=5)

            self.assertEqual(report["status"], "recommend_mode_completed")
            self.assertEqual(report["processedCount"], 1)
            self.assertEqual(report["swipesUsed"], 2)
            self.assertEqual([s["kind"] for s in report["skippedItems"]], ["already_seen", "live_preview"])
            self.assertEqual(report["videos"][0]["author"], "阿星探店")
            self.assertEqual(report["videos"][0]["authorWechatChannelsId"], "sphFoodStar888")
            self.assertEqual(report["videos"][0]["sourceMode"], "RECOMMEND")

            store = LocalStore(root / "videos.sqlite3")
            with store.connect() as db:
                saved = json.loads(db.execute("SELECT event_json FROM video_drafts").fetchone()[0])
            self.assertEqual(saved["source_mode"], "RECOMMEND")
            self.assertIsNone(saved["payload"]["authorDouyinId"])
            self.assertEqual(saved["payload"]["author_wechat_channels_id"], "sphFoodStar888")
            self.assertEqual(saved["payload"]["platform"], "wechat")

    def test_run_search_mode_and_auto_mode_stage_switching(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            search_input_lines = [
                line("搜索", 609, 95, 66, 38),
                line("最近在搜", 35, 220, 120, 30),
                line("搜索发现", 35, 420, 120, 30),
            ]
            ui = FakeModeUi(
                [
                    # Stage 1: recommend (count=1, collectVideo=False for fast stage test)
                    FEED_RECOMMEND,
                    FEED_RECOMMEND,
                    # Stage 2: search ("脱口秀", count=1, collectVideo=False)
                    FEED_RECOMMEND,             # resume-check in search_keyword
                    search_input_lines,         # search-input
                    search_input_lines,         # search-typed
                    SEARCH_VIDEO_TAB_LINES,     # search-results
                    SEARCH_VIDEO_TAB_LINES,     # search-video-tab
                    SEARCH_VIDEO_TAB_LINES,     # search-after-sort
                    FEED_RECOMMEND,             # search-opened-video-1
                    SEARCH_VIDEO_TAB_LINES,     # search-return-1
                    SEARCH_VIDEO_TAB_LINES,     # verify-stage-search
                    SEARCH_VIDEO_TAB_LINES,     # recover-check-0 (exit search via 取消)
                    FEED_RECOMMEND,             # recover-check-1 (on recommend_feed)
                ]
            )
            stages = [
                {"stage": "recommend", "count": 1, "collectVideo": False},
                {"stage": "search", "keyword": "脱口秀", "sortBy": "最新", "count": 1, "collectVideo": False},
            ]
            with patch("modes.time.sleep"):
                auto_report = run_auto_mode(ui, root, stages=stages)

            self.assertEqual(auto_report["status"], "auto_mode_completed")
            self.assertEqual(auto_report["stagesCompleted"], 2)
            self.assertTrue(auto_report["stages"][0]["stageSwitchVerified"])
            self.assertEqual(auto_report["stages"][0]["sourceMode"], "AUTO_RECOMMEND")
            self.assertTrue(auto_report["stages"][1]["stageSwitchVerified"])
            self.assertEqual(auto_report["stages"][1]["sourceMode"], "AUTO_SEARCH")
            self.assertTrue(auto_report["stages"][1]["videos"][0]["returnedToSearchResults"])
            self.assertEqual(ui.texts_set, ["脱口秀"])


if __name__ == "__main__":
    unittest.main()
