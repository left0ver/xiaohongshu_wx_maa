"""Unit tests for WeChat Channels Batch 3 interactions, profile touch, message handling, and quotas."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from interactions import (
    _work_matches_spec,
    DailyQuotaStore,
    QuotaExceededError,
    classify_page_state,
    failed_interactions,
    collect_work_action,
    commenter_target,
    ensure_clean_page_on_resume,
    follow_profile_user,
    inspect_and_handle_messages,
    interact_profile_first_work,
    interact_work_and_comments,
    is_red_active,
    like_comment,
    like_work,
    locate_target_comment,
    ocr_text_matches,
    open_personal_messages,
    parse_comment_messages,
    post_work_comment,
    preemptive_reply_comment,
    reply_to_comment,
    validate_interaction_config,
    visit_and_touch_commenter_profile,
)
from collection import identity
from comments import parse_comment_drawer


def make_frame(width=720, height=1605, red_boxes=()):
    """Create a lightweight pure-Python BGR frame with optional red patches."""
    bg = (40, 40, 40)
    red = (45, 45, 235)
    frame = [[bg] * width for _ in range(height)]
    for box in red_boxes:
        bx, by, bw, bh = box
        for y in range(max(0, by), min(height, by + bh)):
            row = list(frame[y])
            for x in range(max(0, bx), min(width, bx + bw)):
                row[x] = red
            frame[y] = row
    return frame


def feed_lines(author="新华社", like_count="120", collect_count="85", comment_count="16"):
    return [
        {"text": "关注", "box": [230, 78, 52, 32]},
        {"text": "朋友", "box": [334, 78, 52, 32]},
        {"text": "推荐", "box": [438, 78, 52, 32]},
        {"text": author, "box": [80, 1495, 110, 34]},
        {"text": "关注", "box": [240, 1495, 56, 32]},
        {"text": str(like_count), "box": [352, 1530, 42, 26]},
        {"text": "转发", "box": [448, 1530, 42, 26]},
        {"text": str(collect_count), "box": [542, 1530, 42, 26]},
        {"text": str(comment_count), "box": [638, 1530, 42, 26]},
    ]


def drawer_lines(total=5, include_self=False, liked_first=False, with_reply_Text=None):
    lines = [
        {"text": f"评论 {total}", "box": [315, 520, 90, 32]},
        {"text": "新华社 作者 2小时前", "box": [98, 580, 210, 28]},
        {"text": "春日航拍美景合集", "box": [98, 616, 240, 30]},
        {"text": "林间微风 广东 1小时前", "box": [98, 690, 220, 28]},
        {"text": "太美了，请问这是哪里拍的？", "box": [98, 728, 310, 30]},
    ]
    if liked_first:
        lines.append({"text": "4凸", "box": [565, 690, 44, 28]})
        lines.append({"text": "推荐♡", "box": [625, 690, 62, 28]})
    else:
        lines.append({"text": "3凸", "box": [640, 690, 44, 28]})

    if with_reply_Text:
        lines.extend([
            {"text": "AI吕哥说 我 刚刚", "box": [150, 775, 190, 26]},
            {"text": f"回复 林间微风：{with_reply_Text}", "box": [150, 808, 280, 28]},
            {"text": "凸", "box": [655, 775, 30, 26]},
        ])

    if include_self:
        lines.extend([
            {"text": "AI吕哥说 我 1分钟前", "box": [98, 860, 210, 28]},
            {"text": "拍得真好", "box": [98, 898, 140, 30]},
            {"text": "凸", "box": [655, 860, 30, 28]},
        ])

    lines.extend([
        {"text": "远山如黛 浙江 30分钟前", "box": [98, 960, 230, 28]},
        {"text": "构图和色调都很舒服", "box": [98, 998, 240, 30]},
        {"text": "1凸", "box": [645, 960, 40, 28]},
        {"text": "AI吕哥说 发表评论：", "box": [95, 1525, 220, 32]},
    ])
    return lines


def composer_lines(prompt="AI吕哥说 发表评论：", btn="发送"):
    return [
        {"text": "评论 5", "box": [315, 520, 90, 32]},
        {"text": prompt, "box": [45, 820, 260, 34]},
        {"text": btn, "box": [592, 816, 106, 52]},
    ]


def profile_lines(nickname="林间微风", followed=False, works_count=6):
    follow_text = "已关注" if followed else "关注"
    lines = [
        {"text": nickname, "box": [182, 412, 160, 38]},
        {"text": "广东 深圳", "box": [182, 462, 120, 26]},
        {"text": "IP属地：广东", "box": [182, 495, 130, 26]},
        {"text": "热爱自然风光与旅行摄影", "box": [52, 550, 320, 30]},
        {"text": f"{works_count}条原创内容", "box": [52, 600, 140, 26]},
        {"text": follow_text, "box": [52, 656, 290, 56]},
        {"text": "私信", "box": [368, 656, 290, 56]},
    ]
    if works_count > 0:
        lines.extend([
            {"text": "视频", "box": [70, 740, 60, 30]},
            {"text": "128", "box": [60, 960, 48, 24]},
        ])
    else:
        lines.append({"text": "暂无内容", "box": [300, 860, 120, 30]})
    return lines


def message_center_comment_lines():
    return [
        {"text": "点赞", "box": [163, 78, 52, 30]},
        {"text": "评论", "box": [333, 78, 52, 30]},
        {"text": "关注", "box": [503, 78, 52, 30]},
        {"text": "吕说AI W 作者 4天前", "box": [136, 179, 250, 29]},
        {"text": "回复 AI吕哥说：测试测试", "box": [136, 224, 297, 30]},
        {"text": "AI吕哥说：测试", "box": [151, 275, 166, 26]},
        {"text": "回复", "box": [144, 336, 51, 29]},
        {"text": "凸", "box": [254, 339, 32, 23]},
        {"text": "书山有鹿 3月29日", "box": [134, 487, 505, 31]},
        {"text": "这是AI生成的吗？", "box": [136, 535, 220, 27]},
        {"text": "回复", "box": [145, 596, 49, 27]},
        {"text": "凸", "box": [255, 598, 32, 23]},
    ]


def ln(text, y, x, w, h):
    return {"text": text, "box": [x, y, w, h]}


# Huawei Hebe-BD00 runs on 2026-11-08: the same comment before and after posting our own comment.
HUAWEI_DRAWER_BEFORE_POST = [
    ln("评论3.8万", 477, 25, 129, 34),
    ln("Q都在搜:齐天大圣孙悟空后续德云社", 482, 276, 410, 23),
    ln("王子不会拍作者广东7天前", 566, 98, 390, 29),
    ln("“大圣不会输你也不会”#短剧满贯#接住你的专", 604, 106, 583, 29),
    ln("属时刻#轻松真有料#悟空#齐天大圣", 643, 101, 432, 27),
    ln("♪相关音乐·新·与天齐", 690, 102, 288, 31),
    ln("Q帮他上热门", 764, 120, 167, 33),
    ln("等1413人帮过", 766, 527, 162, 28),
    ln("9.1万凸", 867, 585, 106, 35),
    ln("窗下有清风o广东7天前", 870, 101, 308, 28),
    ln("换一份机缘❤天道出手了！", 909, 98, 315, 32),
    ln("顺便告诉大家，小伙子已经顺利通过，成为腾字科", 948, 101, 586, 27),
    ln("a一员了🎉", 975, 96, 141, 43),
    ln("2.5万", 1034, 583, 108, 39),
    ln("丹若森林湖北7天前", 1040, 159, 268, 28),
    ln("好消息，太好了，", 1083, 157, 219, 27),
    ln("祝福！祝所有的好人一生", 1083, 368, 305, 27),
    ln("平安🔥🔥🔥", 1117, 152, 159, 39),
    ln("1831条回复", 1177, 99, 166, 32),
    ln("佳一讲商业发表评论：", 1494, 115, 253, 27),
]
HUAWEI_DRAWER_AFTER_POST = [
    ln("评论3.8万", 480, 27, 125, 29),
    ln("Q都在搜：齐天大圣孙悟空后续德云社", 483, 274, 412, 22),
    ln("王子不会拍作者广东7天前", 566, 98, 390, 29),
    ln("“大圣不会输你也不会”#短剧大满贯#接住你的专", 604, 106, 583, 29),
    ln("属时刻#轻松真有料#悟空#齐天大圣", 643, 101, 432, 27),
    ln("♪相关音乐·新·与天齐", 690, 102, 288, 31),
    ln("Q帮他上热门", 764, 120, 167, 33),
    ln("等1413人帮过", 766, 528, 161, 28),
    ln("佳一讲商业我1分钟前", 868, 98, 263, 31),
    ln("这份坚持太让人佩服了", 912, 99, 267, 28),
    ln("9.1万凸", 1001, 585, 107, 35),
    ln("窗下有清风o广东7天前", 1005, 101, 308, 27),
    ln("换一份机缘❤天道出手了！", 1042, 98, 315, 34),
    ln("顺便告诉大家，小伙子已经顺利通过，成为腾字科", 1084, 101, 585, 23),
    ln("a一员了", 1112, 98, 136, 39),
    ln("2.5万", 1170, 586, 103, 34),
    ln("丹若森林湖北7天前", 1177, 159, 266, 23),
    ln("好消息，太好了，税福！祝所有的好人一", 1214, 157, 516, 31),
    ln("平安🔥🔥🙏", 1252, 154, 157, 37),
    ln("1831条回复", 1313, 101, 162, 28),
    ln("佳一讲商业发表评论", 1494, 115, 251, 27),
]
# vivo V2131A run on 2026-11-08: our reply to 俊国 is already visible in the thread.
VIVO_DRAWER_WITH_OWN_REPLY = [
    ln("评论5357", 439, 98, 130, 30),
    ln("Q都在搜：海龟成活率", 439, 446, 244, 29),
    ln(" 问小微16人问过", 479, 98, 227, 34),
    ln("老王的动物园", 575, 100, 216, 28),
    ln("已声明原创>", 578, 541, 153, 28),
    ln("作者江苏9月30日", 610, 98, 220, 28),
    ln("视死如“龟”#搞笑科普#动物世界", 660, 100, 424, 28),
    ln("♪乐·OnMyOwn", 707, 103, 263, 31),
    ln("3人帮过", 775, 590, 105, 40),
    ln("帮他上热门", 782, 122, 163, 30),
    ln("AI吕哥说我1分钟前", 884, 98, 270, 28),
    ln("小海龟加油，太可爱了", 927, 100, 272, 29),
    ln("龍龍ｉ飛麗山西9月30日", 1013, 95, 334, 40),
    ln("龍", 1014, 24, 59, 62),
    ln("1.1万凸", 1016, 586, 108, 37),
    ln("配乐一个比一个燃，结局一个比一个惨经", 1059, 96, 473, 32),
    ln("4157", 1108, 583, 112, 47),
    ln("俊国河南10月1日", 1120, 155, 217, 28),
    ln("众生皆苦，却皆苦苦相逼。", 1158, 155, 320, 32),
    ln("AI吕哥说我1分钟前", 1217, 157, 272, 28),
    ln("回复俊国：哈哈确实，看得我也着急", 1260, 159, 413, 24),
    ln("更多回复", 1315, 98, 139, 30),
    ln("AI吕哥说发表评论：", 1539, 110, 262, 29),
]

# vivo V2131A runs on 2026-11-08: the feed shows a hot comment where the caption was.
VIVO_FEED_WITH_HOT_COMMENT = [
    ln("关注", 71, 190, 71, 37),
    ln("朋友", 75, 297, 102, 35),
    ln("推荐", 75, 439, 64, 32),
    ln("可以在这放一个桌子", 1044, 192, 335, 46),
    ln("这个房子最大的优点就是：方便时很方便", 1364, 98, 561, 28),
    ln("热评", 1367, 36, 50, 25),
    ln("137条回复", 1414, 27, 101, 23),
    ln("+关注", 1494, 252, 66, 32),
    ln("小琦受不了", 1496, 117, 101, 28),
    ln("2.1万", 1529, 340, 61, 30),
    ln("8.0万", 1529, 434, 64, 30),
    ln("1.0万", 1531, 533, 57, 25),
    ln("1.1万", 1531, 631, 54, 27),
]
VIVO_DRAWER_HEADER = [
    ln("评论1.1万", 438, 96, 130, 35),
    ln("Q都在搜：上海老破小热销原因", 444, 347, 341, 22),
    ln(" 问小微10人问过", 479, 98, 227, 34),
    ln("小琦受不了作者上海6月6日", 575, 98, 422, 28),
    ln("已声明原创>", 578, 536, 154, 23),
    ln("总裁，欢迎回到沪上1800的家@乔百万上海", 611, 98, 589, 29),
    ln("破小", 643, 93, 69, 39),
    ln("♪原声", 693, 98, 95, 37),
    ln("・上海市", 737, 96, 123, 40),
    ln("AI吕哥说发表评论：", 1539, 110, 262, 29),
]


class FakeUi:
    def __init__(self, snapshots, frames=None, height=1605):
        self.snapshots = list(snapshots)
        self.frames = list(frames or [])
        self.height = height
        self.clicks = []
        self.backs = 0
        self.typed = []
        self.feed_next_scrolls = 0
        self.feed_prev_scrolls = 0
        self.comment_scrolls = 0
        self.header_scrolls = 0
        self.is_stopped = False

    def stopped(self):
        return self.is_stopped

    def snapshot(self, label, author=None):
        if self.is_stopped:
            raise InterruptedError("Interaction was stopped")
        if not self.snapshots:
            raise AssertionError(f"No more snapshots left for label={label}")
        item = self.snapshots.pop(0)
        return item() if callable(item) else item

    def capture_frame(self):
        if self.is_stopped:
            raise InterruptedError("Interaction was stopped")
        if self.frames:
            item = self.frames.pop(0)
            return item() if callable(item) else item
        return make_frame(720, self.height)

    def click(self, line):
        if self.is_stopped:
            raise InterruptedError("Interaction was stopped")
        self.clicks.append(line)

    def back(self):
        if self.is_stopped:
            raise InterruptedError("Interaction was stopped")
        self.backs += 1

    def set_text(self, text, timeout=5):
        if self.is_stopped:
            raise InterruptedError("Interaction was stopped")
        self.typed.append(text)

    def scroll_comments(self, lines):
        if self.is_stopped:
            raise InterruptedError("Interaction was stopped")
        self.comment_scrolls += 1

    def scroll_comments_back(self, lines):
        if self.is_stopped:
            raise InterruptedError("Interaction was stopped")
        self.comment_back_scrolls = getattr(self, "comment_back_scrolls", 0) + 1

    def restore_work_header(self, lines):
        if self.is_stopped:
            raise InterruptedError("Interaction was stopped")
        self.header_scrolls += 1

    def scroll_feed_next(self):
        if self.is_stopped:
            raise InterruptedError("Interaction was stopped")
        self.feed_next_scrolls += 1

    def scroll_feed_prev(self):
        if self.is_stopped:
            raise InterruptedError("Interaction was stopped")
        self.feed_prev_scrolls += 1


class WechatInteractionsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.quota_path = Path(self.temp_dir.name) / "daily_quotas.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_daily_quota_persistence_rollover_and_exhaustion(self):
        day = ["2026-11-07"]
        store = DailyQuotaStore(
            self.quota_path,
            limits={"like": 2, "collect": 1, "comment": 1, "follow": 1, "dm": 0},
            today_fn=lambda: day[0],
        )
        self.assertEqual(store.remaining("like"), 2)
        store.record("like", "work_like", "wxch-video:1", extra_records=[("self_comment", "sc:1")])
        self.assertTrue(store.has_acted("work_like", "wxch-video:1"))
        self.assertTrue(store.has_acted("self_comment", "sc:1"))
        self.assertEqual(store.remaining("like"), 1)

        # Reload from disk on the same day
        reloaded = DailyQuotaStore(self.quota_path, today_fn=lambda: day[0])
        self.assertEqual(reloaded.remaining("like"), 1)
        self.assertTrue(reloaded.has_acted("work_like", "wxch-video:1"))
        reloaded.record("like", "work_like", "wxch-video:2")
        self.assertEqual(reloaded.remaining("like"), 0)
        with self.assertRaises(QuotaExceededError):
            reloaded.require_quota("like")

        # Next day resets daily counters but preserves self_comment anti-loop history
        day[0] = "2026-11-08"
        next_day_store = DailyQuotaStore(self.quota_path, today_fn=lambda: day[0])
        self.assertEqual(next_day_store.remaining("like"), 2)
        self.assertFalse(next_day_store.has_acted("work_like", "wxch-video:1"))
        self.assertTrue(next_day_store.has_acted("self_comment", "sc:1"))

    def test_ocr_variants_of_a_caption_share_one_work_key(self):
        day = ["2026-11-08"]
        store = DailyQuotaStore(self.quota_path, today_fn=lambda: day[0])
        author = "喜剧之王单口季"
        caption = "【东北风格特辑】#翟佳宁是不是背着我们追星？#喜单3高光盘点#喜剧之王单口季"
        key = store.resolve_work_key(author, caption)
        # Same work read again with OCR slips: a missing char, a swapped char, half-width punctuation.
        self.assertEqual(store.resolve_work_key(author, "【东北风格特辑】#翟佳宁是不是背着我门追星?#喜单3高光盘点#喜剧之王单口"), key)
        # Another work of the same author is a different work.
        other = store.resolve_work_key(author, "#张兴朝 讲述自己的北漂故事#喜单3高光盘点")
        self.assertNotEqual(other, key)
        # Another author with the same caption is a different work too.
        self.assertNotEqual(store.resolve_work_key("综艺大表姐", caption), key)

        # Known works survive a reload and a day rollover, unlike the daily counters.
        day[0] = "2026-11-09"
        next_day = DailyQuotaStore(self.quota_path, today_fn=lambda: day[0])
        self.assertEqual(next_day.resolve_work_key(author, caption), key)

    def test_interaction_reads_feed_caption_to_skip_an_already_commented_work(self):
        store = DailyQuotaStore(self.quota_path)
        text = "笑到停不下来，这段太绝了"
        key = store.resolve_work_key("新华社", "春日航拍美景合集，带你看遍江南")
        store.record("comment", "work_comment", f"{key}:comment:{identity(text)}")
        feed = feed_lines() + [{"text": "春日航拍美景合集，带你看遍江男", "box": [30, 1420, 560, 30]}]
        ui = FakeUi(snapshots=[feed])

        result = interact_work_and_comments(ui, store, {"commentText": text})

        self.assertEqual(result["workKey"], key)
        self.assertEqual(result["work"]["author"], "新华社")
        self.assertEqual(result["workComment"]["status"], "already_commented")
        self.assertEqual(ui.clicks, [])

    def test_interaction_refuses_work_actions_when_the_work_is_unknown(self):
        store = DailyQuotaStore(self.quota_path)
        ui = FakeUi(snapshots=[drawer_lines(total=5)])

        with mock.patch("interactions.inspect_and_handle_messages", return_value={"messagesHandled": []}) as inspect:
            result = interact_work_and_comments(ui, store, {"commentText": "笑到停不下来，这段太绝了", "replyText": "同感",
                                                            "targetComment": {"nickname": "俊国"},
                                                            "inspectMessages": {"maxMessages": 1}})

        for key in ("workComment", "commentReply"):
            self.assertEqual(result[key]["status"], "failed")
            self.assertIn("Cannot identify the current work", result[key]["error"])
        # The message inspection does not depend on the work, so it still runs.
        inspect.assert_called_once()
        self.assertEqual(failed_interactions(result), ["workComment", "commentReply"])
        self.assertEqual(ui.clicks, [])
        self.assertEqual(store.summary()["used"]["comment"], 0)

    def test_a_failed_action_is_recorded_and_the_next_actions_still_run(self):
        store = DailyQuotaStore(self.quota_path)
        profile = [{"text": "关注", "box": [157, 723, 64, 39]}, {"text": "私信", "box": [493, 727, 60, 32]}]
        # resume check, then the recovery: still on the author page, back once, feed.
        ui = FakeUi(snapshots=[feed_lines(), profile, feed_lines()])
        config = {"workKey": "wxch-video:k", "likeWork": True, "collectWork": True}

        with mock.patch("interactions.like_work", side_effect=RuntimeError("Like state did not change")), \
                mock.patch("interactions.collect_work_action", return_value={"status": "performed"}) as collect:
            result = interact_work_and_comments(ui, store, config)

        self.assertEqual(result["workLike"], {"status": "failed", "error": "Like state did not change",
                                              "recoveredState": "channels_feed"})
        self.assertEqual(ui.backs, 1)
        collect.assert_called_once()
        self.assertEqual(result["workCollect"]["status"], "performed")
        self.assertEqual(failed_interactions(result), ["workLike"])

    def test_a_stop_or_a_verification_page_still_ends_the_whole_run(self):
        store = DailyQuotaStore(self.quota_path)
        for error, expected in ((InterruptedError("Interaction was stopped"), InterruptedError),
                                (RuntimeError("Verification detected; manual intervention required"), RuntimeError)):
            ui = FakeUi(snapshots=[feed_lines()])
            with mock.patch("interactions.like_work", side_effect=error), \
                    mock.patch("interactions.collect_work_action") as collect:
                with self.assertRaises(expected):
                    interact_work_and_comments(ui, store, {"workKey": "wxch-video:k", "likeWork": True,
                                                           "collectWork": True})
            collect.assert_not_called()
            self.assertEqual(ui.backs, 0)

    def test_comment_actions_without_a_target_are_refused_before_touching_the_phone(self):
        # An empty target matches the first visible comment, so it would act on a stranger.
        store = DailyQuotaStore(self.quota_path)
        blank = {"nickname": "", "content": "  "}
        for config in (
            {"likeComment": True},
            {"likeComment": True, "targetComment": blank},
            {"replyText": "同感", "targetComment": blank},
            {"touchCommenterProfile": {"follow": True}, "targetComment": blank},
            {"touchCommenterProfile": {"follow": True, "targetComment": blank}},
        ):
            ui = FakeUi(snapshots=[])
            with self.assertRaisesRegex(ValueError, "targetComment"):
                interact_work_and_comments(ui, store, config)
            self.assertEqual(ui.clicks, [])

    def test_enabled_but_blank_texts_are_refused(self):
        for key in ("commentText", "replyText"):
            with self.assertRaisesRegex(ValueError, f"{key} is enabled but empty"):
                validate_interaction_config({key: " ", "targetComment": {"nickname": "俊国"}})
        # A missing key is simply not enabled.
        validate_interaction_config({"likeWork": True})

    def test_commenter_visit_uses_its_own_target_then_the_shared_one(self):
        shared = {"nickname": "俊国", "content": ""}
        own = {"content": "众生皆苦"}
        self.assertEqual(commenter_target({"touchCommenterProfile": {"follow": True}, "targetComment": shared}), shared)
        self.assertEqual(commenter_target({"touchCommenterProfile": {"targetComment": own}, "targetComment": shared}), own)
        self.assertEqual(
            commenter_target({"touchCommenterProfile": {"targetComment": {"nickname": ""}}, "targetComment": shared}),
            shared,
        )
        validate_interaction_config({"touchCommenterProfile": {"follow": True}, "targetComment": shared})

    def test_reply_is_not_repeated_when_ocr_reads_the_target_comment_differently(self):
        store = DailyQuotaStore(self.quota_path)
        work_key = "wxch-video:huawei-dasheng"
        text = "说得好，给大圣点赞"
        first_read = next(
            c for c in parse_comment_drawer(HUAWEI_DRAWER_AFTER_POST, 1559, parent_event_id=work_key)["comments"]
            if c["nickname"] == "窗下有清风o"
        )
        store.remember_reply(work_key, first_read["nickname"], first_read["content"], text)
        ui = FakeUi(snapshots=[], height=1559)

        result = reply_to_comment(
            ui, store, work_key, text, target_comment={"nickname": "窗下有清风o"}, lines=HUAWEI_DRAWER_BEFORE_POST
        )

        self.assertNotEqual(result["commentKey"], first_read["key"])
        self.assertEqual(result["status"], "already_replied")
        self.assertEqual(result["reason"], "ledger")
        self.assertEqual(ui.clicks, [])

    def test_reply_already_visible_in_the_thread_is_not_sent_again(self):
        store = DailyQuotaStore(self.quota_path)
        ui = FakeUi(snapshots=[])

        result = reply_to_comment(
            ui, store, "wxch-video:vivo-turtle", "哈哈确实，看得我也着急",
            target_comment={"nickname": "俊国"}, lines=VIVO_DRAWER_WITH_OWN_REPLY,
        )

        self.assertEqual(result["status"], "already_replied")
        self.assertEqual(result["reason"], "visible_in_drawer")
        self.assertEqual(ui.clicks, [])
        self.assertTrue(store.has_replied("wxch-video:vivo-turtle", "俊国", "众生皆苦，却皆苦苦相逼。", "哈哈确实，看得我也着急"))

    def test_a_different_reply_text_to_the_same_comment_is_still_allowed(self):
        store = DailyQuotaStore(self.quota_path)
        store.remember_reply("wxch-video:1", "俊国", "众生皆苦，却皆苦苦相逼。", "哈哈确实，看得我也着急")
        self.assertFalse(store.has_replied("wxch-video:1", "俊国", "众生皆苦，却皆苦苦相逼", "另一句回复"))
        self.assertFalse(store.has_replied("wxch-video:2", "俊国", "众生皆苦，却皆苦苦相逼", "哈哈确实，看得我也着急"))
        self.assertTrue(store.has_replied("wxch-video:1", "俊国", "众生皆苦，却皆苦苦相逼", "哈哈确实，看得我也着急"))

    def test_feed_hot_comment_falls_back_to_the_drawer_caption(self):
        store = DailyQuotaStore(self.quota_path)
        text = "小空间也能住得这么开心，佩服"
        # First run read the caption on the feed (and cut the author's last character).
        key = store.resolve_work_key("小琦受不", "总裁，欢迎回到沪上1800米的家@乔百万上海老破小")
        store.record("comment", "work_comment", f"{key}:comment:{identity(text)}")
        ui = FakeUi(snapshots=[VIVO_FEED_WITH_HOT_COMMENT, VIVO_DRAWER_HEADER])

        result = interact_work_and_comments(ui, store, {"commentText": text})

        self.assertEqual(result["work"]["caption"], "总裁，欢迎回到沪上1800的家@乔百万上海破小")
        self.assertEqual(result["workKey"], key)
        self.assertEqual(result["workComment"]["status"], "already_commented")
        self.assertEqual(ui.backs, 1)

    def test_a_feed_cut_caption_matches_the_full_drawer_caption(self):
        full = "“某天你想起我我就永远在你的十七岁”#青春#校园生活#合照#毕业季#回不去的夏天"
        self.assertTrue(ocr_text_matches("“某天你想起我我就永远在你的十七岁”#青春#校园…", full, allow_truncation=True))
        self.assertFalse(ocr_text_matches("“某天你想起我我就永远在你的十七岁”#青春#校园…", full))
        # A fragment too short to identify a work is not matched as a prefix.
        self.assertFalse(ocr_text_matches("“某天你想", full, allow_truncation=True))

    def test_stop_guard_and_dirty_resume_cleanup(self):
        ui = FakeUi(
            snapshots=[
                composer_lines("AI吕哥说 发表评论：", "发送"),
                drawer_lines(total=5),
            ]
        )
        res = ensure_clean_page_on_resume(ui)
        self.assertEqual(res["initialState"], "composer_overlay")
        self.assertEqual(res["finalState"], "comment_drawer")
        self.assertEqual(res["dismissedCount"], 1)
        self.assertTrue(res["clean"])
        self.assertEqual(ui.backs, 1)

        ui.is_stopped = True
        store = DailyQuotaStore(self.quota_path)
        with self.assertRaises(InterruptedError):
            like_work(ui, store, "wxch-video:stop")

    def test_like_and_collect_work_with_anti_toggle_protection(self):
        store = DailyQuotaStore(self.quota_path)
        like_roi = [340, 1478, 56, 48]
        collect_roi = [530, 1478, 56, 48]
        ui = FakeUi(
            snapshots=[
                feed_lines(like_count="120", collect_count="85"),
                feed_lines(like_count="121", collect_count="85"),
                feed_lines(like_count="121", collect_count="85"),
                feed_lines(like_count="121", collect_count="86"),
                feed_lines(like_count="121", collect_count="86"),
                feed_lines(like_count="121", collect_count="86"),
            ],
            frames=[
                make_frame(),
                make_frame(red_boxes=[like_roi]),
                make_frame(red_boxes=[like_roi]),
                make_frame(red_boxes=[like_roi, collect_roi]),
                make_frame(red_boxes=[like_roi, collect_roi]),
                make_frame(red_boxes=[like_roi, collect_roi]),
            ],
        )

        like_res = like_work(ui, store, "wxch-video:abc")
        self.assertEqual(like_res["status"], "performed")
        self.assertTrue(like_res["clicked"])
        self.assertEqual(like_res["afterCount"], 121)

        collect_res = collect_work_action(ui, store, "wxch-video:abc")
        self.assertEqual(collect_res["status"], "performed")
        self.assertTrue(collect_res["clicked"])
        self.assertEqual(collect_res["afterCount"], 86)

        # Second invocation on the same work must NOT click again
        clicks_before = len(ui.clicks)
        like_again = like_work(ui, store, "wxch-video:abc")
        collect_again = collect_work_action(ui, store, "wxch-video:abc")
        self.assertEqual(like_again["status"], "already_liked")
        self.assertFalse(like_again["clicked"])
        self.assertEqual(collect_again["status"], "already_collected")
        self.assertFalse(collect_again["clicked"])
        self.assertEqual(len(ui.clicks), clicks_before)

    def test_post_work_comment_and_self_comment_recording(self):
        store = DailyQuotaStore(self.quota_path)
        ui = FakeUi(
            snapshots=[
                drawer_lines(total=5, include_self=False),
                composer_lines("AI吕哥说 发表评论：", "发送"),
                composer_lines("拍得真好", "发送"),
                drawer_lines(total=6, include_self=True),
            ]
        )
        res = post_work_comment(ui, store, "wxch-video:comment-test", "拍得真好")
        self.assertEqual(res["status"], "performed")
        self.assertTrue(res["submitted"])
        self.assertEqual(res["selfNickname"], "AI吕哥说")
        self.assertEqual(ui.typed, ["拍得真好"])
        self.assertEqual(store.summary()["used"]["comment"], 1)

        # Duplicate comment text on the same work is skipped idempotently
        dup = post_work_comment(ui, store, "wxch-video:comment-test", "拍得真好")
        self.assertEqual(dup["status"], "already_commented")
        self.assertFalse(dup["clicked"])

    def test_like_comment_and_already_liked_badge_detection(self):
        store = DailyQuotaStore(self.quota_path)
        ui = FakeUi(
            snapshots=[
                drawer_lines(total=5, liked_first=False),
                drawer_lines(total=5, liked_first=True),
                drawer_lines(total=5, liked_first=True),
            ],
            frames=[
                make_frame(),
                make_frame(red_boxes=[[590, 690, 36, 28]]),
                make_frame(red_boxes=[[590, 690, 36, 28]]),
            ],
        )
        res = like_comment(
            ui,
            store,
            "wxch-video:1",
            target_comment={"nickname": "林间微风"},
        )
        self.assertEqual(res["status"], "performed")
        self.assertTrue(res["clicked"])
        self.assertEqual(res["afterCount"], 4)

        # Already liked comment ('推荐♡' badge visible) is not clicked again
        clicks_before = len(ui.clicks)
        res2 = like_comment(
            ui,
            store,
            "wxch-video:1",
            target_comment={"nickname": "林间微风"},
        )
        self.assertEqual(res2["status"], "already_liked")
        self.assertFalse(res2["clicked"])
        self.assertEqual(len(ui.clicks), clicks_before)

    def test_reply_to_comment_and_three_layer_self_comment_protection(self):
        store = DailyQuotaStore(self.quota_path)
        # 1. Layer 1 & 2: Explicitly targeting our own comment ('AI吕哥说 我 1分钟前') is refused before clicking
        ui_self = FakeUi(snapshots=[drawer_lines(total=6, include_self=True)])
        self_res = reply_to_comment(
            ui_self,
            store,
            "wxch-video:1",
            "自我回复测试",
            target_comment={"nickname": "AI吕哥说"},
        )
        self.assertEqual(self_res["status"], "self_comment_skipped")
        self.assertFalse(self_res["clicked"])
        self.assertEqual(len(ui_self.clicks), 0)

        # 2. Layer 3: If a comment is not labeled '我' in OCR, but tapping it does NOT open a reply composer,
        # reply_to_comment aborts without typing/submitting and marks it as self_comment.
        ui_layer3 = FakeUi(
            snapshots=[
                drawer_lines(total=5, include_self=False),
                drawer_lines(total=5, include_self=False),
            ]
        )
        layer3_res = reply_to_comment(
            ui_layer3,
            store,
            "wxch-video:layer3",
            "不会发送",
            target_comment={"nickname": "远山如黛"},
        )
        self.assertEqual(layer3_res["status"], "self_comment_skipped")
        self.assertFalse(layer3_res["submitted"])
        self.assertEqual(ui_layer3.typed, [])

        # 3. Normal reply to another user's comment succeeds
        ui_normal = FakeUi(
            snapshots=[
                drawer_lines(total=5, include_self=False),
                composer_lines("AI吕哥说 回复 林间微风：", "回复"),
                composer_lines("这是在新疆喀纳斯拍的", "回复"),
                drawer_lines(total=6, include_self=False, with_reply_Text="这是在新疆喀纳斯拍的"),
            ]
        )
        ok_res = reply_to_comment(
            ui_normal,
            store,
            "wxch-video:1",
            "这是在新疆喀纳斯拍的",
            target_comment={"nickname": "林间微风"},
        )
        self.assertEqual(ok_res["status"], "performed")
        self.assertTrue(ok_res["submitted"])
        self.assertEqual(ui_normal.typed, ["这是在新疆喀纳斯拍的"])
        self.assertEqual(ui_normal.clicks[0]["box"][2], 32)

        ui_search = FakeUi([drawer_lines(), [
            {"text": "无人岛", "box": [75, 480, 100, 41]},
            {"text": "搜索", "box": [610, 482, 63, 37]},
        ]])
        before = store.state["actions"]["self_comment"].copy()
        with self.assertRaisesRegex(RuntimeError, "unexpected page"):
            reply_to_comment(ui_search, store, "wxch-video:search-link", "不会发送",
                             target_comment={"nickname": "林间微风"})
        self.assertEqual(store.state["actions"]["self_comment"], before)
        self.assertEqual(ui_search.typed, [])

    def test_preemptive_reply_positive_and_negative_restoration(self):
        store = DailyQuotaStore(self.quota_path)
        # Positive case: current feed matches author, opens drawer, replies to target comment, closes drawer
        ui_pos = FakeUi(
            snapshots=[
                feed_lines(author="新华社"),
                drawer_lines(total=5),
                composer_lines("AI吕哥说 回复 林间微风：", "回复"),
                composer_lines("感谢支持", "回复"),
                drawer_lines(total=6, with_reply_Text="感谢支持"),
                drawer_lines(total=6, with_reply_Text="感谢支持"),
            ]
        )
        pos = preemptive_reply_comment(
            ui_pos,
            store,
            target_work={"author": "新华社", "title": "春日航拍"},
            target_comment={"nickname": "林间微风", "content": "太美了"},
            reply_text="感谢支持",
        )
        self.assertEqual(pos["status"], "performed")
        self.assertTrue(pos["replied"])
        self.assertTrue(pos["restoredToOriginalTask"])
        self.assertGreaterEqual(ui_pos.backs, 1)

        # Negative case: target work is not found within scroll budget -> scrolls back and refuses to reply
        ui_neg = FakeUi(
            snapshots=[
                feed_lines(author="新华社"),
                feed_lines(author="央视新闻"),
            ]
        )
        neg = preemptive_reply_comment(
            ui_neg,
            store,
            target_work={"author": "不存在的博主"},
            target_comment={"nickname": "林间微风"},
            reply_text="绝不误发",
            max_work_scrolls=1,
        )
        self.assertEqual(neg["status"], "target_work_not_found")
        self.assertFalse(neg["replied"])
        self.assertTrue(neg["restoredToOriginalTask"])
        self.assertEqual(ui_neg.feed_next_scrolls, 1)
        self.assertEqual(ui_neg.feed_prev_scrolls, 1)
        self.assertEqual(ui_neg.typed, [])

    def test_preemptive_recovers_recorded_scrolled_work_header(self):
        fixture = json.loads((Path(__file__).parent / "testdata/preemptive-scrolled-drawer.json").read_text(encoding="utf-8"))
        ui = FakeUi([fixture["scrolled"], fixture["top"]], height=fixture["height"])
        matched, _, lines = _work_matches_spec(
            ui, fixture["scrolled"], {"author": "郁WT", "title": "爱一个人要学会放下"},
        )
        self.assertTrue(matched)
        self.assertEqual(ui.header_scrolls, 2)
        self.assertEqual(lines, fixture["top"])
        self.assertEqual(ui.feed_next_scrolls, 0)

    def test_locate_rereads_when_a_scroll_skips_comments(self):
        # Hebe-BD00 measured ~1.25x overshoot: consecutive pages shared no comment,
        # so a target between them was never visible.
        def page(names, top=600):
            lines = [{"text": "评论 9", "box": [315, 520, 90, 32]}]
            for index, name in enumerate(names):
                y = top + index * 130
                lines += [{"text": f"{name} 广东 1小时前", "box": [98, y, 220, 28]},
                          {"text": f"{name}的评论内容", "box": [98, y + 38, 300, 30]},
                          {"text": "1凸", "box": [645, y, 40, 28]}]
            return lines + [{"text": "AI吕哥说 发表评论：", "box": [95, 1525, 220, 32]}]
        ui = FakeUi([page(["星河长明", "海棠依旧"]), page(["远山如黛", "晚风轻拂", "星河长明"])])
        located = locate_target_comment(ui, target_spec={"nickname": "晚风轻拂"}, lines=page(["林间微风", "远山如黛"]))
        self.assertTrue(located["found"])
        self.assertEqual(located["comment"]["nickname"], "晚风轻拂")
        self.assertEqual(ui.comment_scrolls, 1)
        self.assertEqual(ui.comment_back_scrolls, 1)

    def test_work_header_recovery_reopens_a_drawer_dismissed_by_the_pull(self):
        # Hebe-BD00 run 4675d961: WeChat reopened the drawer deep in the list and the
        # second header-recovery pull dismissed the whole sheet back to the feed.
        top = drawer_lines()
        scrolled = [line for line in top if line["box"][1] >= 690 or line["text"].startswith("评论")]
        ui = FakeUi([scrolled, feed_lines(), top])
        matched, _, lines = _work_matches_spec(ui, feed_lines(), {"author": "新华社"})
        self.assertTrue(matched)
        self.assertEqual(lines, top)
        self.assertEqual(ui.header_scrolls, 1)
        self.assertEqual([click["text"] for click in ui.clicks], ["16", "16"])

    def test_preemptive_recovers_top_then_scrolls_down_to_reply(self):
        # Start deep in comments; the target is below the restored first page.
        top = drawer_lines()
        target_page = [line for line in top if line["box"][1] >= 690 or line["text"].startswith("评论")]
        scrolled = [line for line in target_page if "林间微风" not in line["text"]]
        first_page = [line for line in top if not 690 <= line["box"][1] <= 758]
        ui = FakeUi([
            scrolled, first_page, target_page,
            composer_lines("AI吕哥说 回复 林间微风：", "回复"),
            composer_lines("感谢支持", "回复"),
            drawer_lines(with_reply_Text="感谢支持"), drawer_lines(),
        ])
        result = preemptive_reply_comment(
            ui, DailyQuotaStore(self.quota_path),
            {"author": "新华社", "title": "春日航拍"},
            {"nickname": "林间微风", "content": "太美了"}, "感谢支持",
        )
        self.assertEqual(result["status"], "performed")
        self.assertEqual(ui.header_scrolls, 1)
        self.assertEqual(ui.comment_scrolls, 1)
        self.assertEqual(ui.feed_next_scrolls, 0)
        self.assertEqual(ui.typed, ["感谢支持"])

    def test_preemptive_recovers_header_for_each_candidate_work(self):
        top = drawer_lines()
        scrolled = [line for line in top if line["box"][1] >= 690 or line["text"].startswith("评论")]
        wrong_top = [{**line, "text": "另一条作品"} if line["text"] == "春日航拍美景合集" else line for line in top]
        ui = FakeUi([wrong_top, top])
        target = {"author": "新华社", "title": "春日航拍"}
        self.assertFalse(_work_matches_spec(ui, scrolled, target)[0])
        self.assertTrue(_work_matches_spec(ui, scrolled, target)[0])
        self.assertEqual(ui.header_scrolls, 2)

    def test_preemptive_never_uses_comment_text_as_work_title(self):
        top = drawer_lines()
        top = [{**line, "text": "另一条作品"} if line["text"] == "春日航拍美景合集" else line for line in top]
        top = [{**line, "text": "春日航拍美景合集"} if line["text"] == "太美了，请问这是哪里拍的？" else line for line in top]
        ui = FakeUi([])
        self.assertFalse(_work_matches_spec(ui, top, {"author": "新华社", "title": "春日航拍"})[0])
        scrolled = [line for line in top if line["box"][1] >= 690 or line["text"].startswith("评论")]
        ui = FakeUi([scrolled] * 7)
        self.assertFalse(_work_matches_spec(ui, scrolled, {"author": "新华社", "title": "春日航拍"})[0])
        self.assertEqual(ui.header_scrolls, 7)
        self.assertEqual(ui.typed, [])

    def test_profile_follow_first_work_touch_and_comment_drawer_return(self):
        store = DailyQuotaStore(self.quota_path)
        like_roi = [340, 1478, 56, 48]
        collect_roi = [530, 1478, 56, 48]
        ui = FakeUi(
            snapshots=[
                # 1. Click commenter name -> commenter profile (unfollowed, 6 works)
                profile_lines(nickname="林间微风", followed=False, works_count=6),
                # 2. After clicking Follow -> 已关注
                profile_lines(nickname="林间微风", followed=True, works_count=6),
                # 3. Snapshot before first work card
                profile_lines(nickname="林间微风", followed=True, works_count=6),
                # 4. Open first work -> video feed page
                feed_lines(author="林间微风", like_count="10", collect_count="5", comment_count="2"),
                # 5. After like_work
                feed_lines(author="林间微风", like_count="11", collect_count="5", comment_count="2"),
                # 6. Snapshot for collect_work_action
                feed_lines(author="林间微风", like_count="11", collect_count="5", comment_count="2"),
                # 7. After collect_work_action
                feed_lines(author="林间微风", like_count="11", collect_count="6", comment_count="2"),
                # 8. Back to profile after first work
                profile_lines(nickname="林间微风", followed=True, works_count=6),
                # 9. Back to comment drawer after profile touch
                drawer_lines(total=5),
            ],
            frames=[
                make_frame(),
                make_frame(red_boxes=[like_roi]),
                make_frame(red_boxes=[like_roi]),
                make_frame(red_boxes=[like_roi, collect_roi]),
            ],
        )
        comment = {
            "key": "wxch-comment:test-1",
            "nickname": "林间微风",
            "_nameBox": [98, 690, 32, 28],
        }
        res = visit_and_touch_commenter_profile(
            ui,
            store,
            comment=comment,
            work_key="wxch-video:1",
            do_follow=True,
            do_like_first_work=True,
            do_collect_first_work=True,
        )
        self.assertEqual(res["accountType"], "channels")
        self.assertEqual(res["touchStatus"], "TOUCHED")
        self.assertEqual(
            res["profileActionsPerformed"],
            ["FOLLOW", "LIKE_FIRST_WORK", "COLLECT_FIRST_WORK"],
        )
        self.assertTrue(res["returnedToCommentPosition"])
        self.assertTrue(res["touchDetail"]["dmSkipped"])

        # Empty profile safely skips first-work opening
        ui_empty = FakeUi(snapshots=[profile_lines(nickname="空作品用户", followed=True, works_count=0)])
        empty_res = interact_profile_first_work(ui_empty, store, "wxch-user:empty")
        self.assertEqual(empty_res["status"], "no_work_found")
        self.assertFalse(empty_res["opened"])
        self.assertTrue(empty_res["returnedToProfile"])

    def test_message_center_does_not_require_follow_tab(self):
        lines = [{"text": "点赞", "box": [270, 98, 55, 30]},
                 {"text": "评论", "box": [410, 98, 55, 30]}]
        self.assertEqual(classify_page_state(lines), "channels_message_center")
        self.assertNotEqual(classify_page_state(lines[1:]), "channels_message_center")

    def test_personal_page_prefers_creator_messages_over_viewer_messages(self):
        # vivo V2131A WeChat 8.0.79 lists viewer 消息 above creator 视频号消息; the
        # viewer page has icon tabs and is not the Channels message center.
        personal = [{"text": "赞、收藏和最近看过", "box": [20, 120, 260, 30]},
                    {"text": "关注", "box": [20, 178, 50, 30]},
                    {"text": "消息", "box": [20, 236, 50, 30]},
                    {"text": "我的视频号", "box": [14, 300, 130, 28]},
                    {"text": "视频号消息", "box": [20, 490, 136, 30]},
                    {"text": "视频号私信", "box": [20, 548, 136, 30]}]
        center = [{"text": "点赞", "box": [196, 40, 50, 30]}, {"text": "评论", "box": [330, 40, 50, 30]},
                  {"text": "关注", "box": [462, 40, 50, 30]}]
        ui = FakeUi([feed_lines(), personal, center, center])
        open_personal_messages(ui)
        self.assertEqual([c["text"] for c in ui.clicks], ["personal_icon", "视频号消息", "评论"])

    def test_recorded_message_center_with_heart_template_opens_comments(self):
        lines = json.loads((Path(__file__).parent / "testdata/message-center-heart.json").read_text(encoding="utf-8"))
        self.assertNotEqual(classify_page_state(lines), "channels_message_center")
        lines.append({"text": "点赞", "box": [276, 96, 32, 29], "source": "template",
                      "templateNode": "BatchMessagesLikeTab"})
        ui = FakeUi(snapshots=[lines, lines])
        self.assertEqual(open_personal_messages(ui), lines)
        self.assertEqual([target["text"] for target in ui.clicks], ["评论"])

    def test_message_center_inspection_like_reply_and_return(self):
        store = DailyQuotaStore(self.quota_path)
        parsed = parse_comment_messages(message_center_comment_lines(), height=1605)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["nickname"], "吕说AI")
        self.assertTrue(parsed[0]["hasChannelsBadge"])
        self.assertEqual(parsed[0]["content"], "回复 AI吕哥说：测试测试")
        self.assertEqual(parsed[0]["quotedContent"], "AI吕哥说：测试")
        self.assertEqual(parsed[1]["nickname"], "书山有鹿")
        self.assertEqual(parsed[1]["content"], "这是AI生成的吗？")

        msg_like_box = parsed[0]["likeBox"]
        ui = FakeUi(
            snapshots=[
                # 1. Start on channels_feed
                feed_lines(author="新华社"),
                # 2. Click PersonalIcon -> personal_page
                [
                    {"text": "赞、收藏和最近看过", "box": [45, 220, 260, 32]},
                    {"text": "视频号消息", "box": [84, 597, 136, 30]},
                ],
                # 3. Click 视频号消息 -> message center
                message_center_comment_lines(),
                # 4. Click 评论 tab -> comment messages list
                message_center_comment_lines(),
                # 5. Click message thumb like -> after like
                message_center_comment_lines(),
                # 6. Click 回复 -> composer
                composer_lines("回复 吕说AI：", "回复"),
                # 7. After set_text
                composer_lines("收到啦，谢谢！", "回复"),
                # 8. After clicking 回复 send button
                message_center_comment_lines(),
                # 9. Return check (in message center)
                message_center_comment_lines(),
                # 10. Back 1 -> personal_page
                [{"text": "视频号消息", "box": [84, 597, 136, 30]}],
                # 11. Back 2 -> channels_feed
                feed_lines(author="新华社"),
            ],
            frames=[
                make_frame(),
                make_frame(red_boxes=[msg_like_box]),
            ],
        )

        result = inspect_and_handle_messages(
            ui,
            store,
            max_messages=1,
            do_like=True,
            reply_text="收到啦，谢谢！",
        )
        self.assertEqual(result["messagesFound"], 2)
        self.assertEqual(len(result["messagesHandled"]), 1)
        handled = result["messagesHandled"][0]
        self.assertEqual(handled["nickname"], "吕说AI")
        self.assertTrue(handled["liked"])
        self.assertTrue(handled["replied"])
        self.assertTrue(result["returnedToTask"])
        self.assertEqual(ui.typed, ["收到啦，谢谢！"])


if __name__ == "__main__":
    unittest.main()
