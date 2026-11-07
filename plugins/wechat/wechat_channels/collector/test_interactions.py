"""Unit tests for WeChat Channels Batch 3 interactions, profile touch, message handling, and quotas."""

import tempfile
import unittest
from pathlib import Path

from interactions import (
     DailyQuotaStore,
    QuotaExceededError,
    classify_page_state,
    collect_work_action,
    ensure_clean_page_on_resume,
    follow_profile_user,
    inspect_and_handle_messages,
    interact_profile_first_work,
    interact_work_and_comments,
    is_red_active,
    like_comment,
    like_work,
    parse_comment_messages,
    post_work_comment,
    preemptive_reply_comment,
    reply_to_comment,
    visit_and_touch_commenter_profile,
)


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
