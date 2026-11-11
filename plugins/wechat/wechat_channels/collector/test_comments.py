import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from collection import LocalStore, Timings
import comments as comments_module
from comments import (
    _collect_comment,
    CommenterProfileSkipped,
    comment_limits,
    inspect_commenter_profile,
    same_person,
    user_card_open,
    clean_comment_nickname,
    collect_visible_comments,
    parse_comment_drawer,
    parse_comments,
    parse_wechat_user_card,
    reply_target_name,
    resolve_reply_targets,
    save_comments,
    save_users,
)


def line(text, y, x=99, w=300, h=25):
    return {"text": text, "box": [x, y, w, h]}


class FakeCommentUi:
    height = 1605

    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.clicks = []
        self.backs = 0
        self.scrolls = 0
        self.labels = []

    def snapshot(self, label, author=None):
        self.labels.append(label)
        if not self.snapshots:
            raise AssertionError(f"Unexpected snapshot({label!r})")
        return self.snapshots.pop(0)

    def click(self, target):
        self.clicks.append(target.get("text", "box"))

    def back(self):
        self.backs += 1

    def scroll_comments(self, lines):
        self.scrolls += 1

    def profile_more(self, name):
        self.clicks.append("profile-more")

    @contextlib.contextmanager
    def prepare_basic_info(self):
        yield None

    def wait_basic_info(self, timeout=8, reader=None):
        return self.snapshot("author-basic-info-ready")


class CommentTests(unittest.TestCase):
    def test_work_and_reply_controls_are_not_comment_content(self):
        lines = [line("评论12", 400, 25), line("作品作者广东8月1日", 470),
                 line("视频文案", 510), line("音乐·歌曲", 550),
                 line("甲广东8月2日", 630), line("正文第一行", 670), line("正文第二行", 705),
                 line("作者赞过", 740), line("3条回复", 780),
                 line("乙河南昨天", 850), line("第二条评论", 900), line("发表评论", 1460)]
        comments = parse_comments(lines, 1559)
        self.assertEqual([c["nickname"] for c in comments], ["甲", "乙"])
        self.assertEqual(comments[0]["content"], "正文第一行\n正文第二行")
        self.assertEqual(comments[0]["replyCount"], 3)
        self.assertFalse(comments[0]["repliesCollected"])

    def test_unbounded_or_truncated_comment_is_skipped(self):
        self.assertEqual(parse_comments([line("评论12", 400), line("甲广东昨天", 630), line("未读完…", 680)], 1559), [])
        with self.assertRaises(ValueError):
            parse_comments([], 1559)

    def test_persistence_links_parent_and_dedupes_within_each_work(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.sqlite3"
            store = LocalStore(path)
            first, _ = store.save({"authorNickname": "作者", "description": "作品一", "authorDouyinId": "sphExample123"})
            second, _ = store.save({"authorNickname": "作者", "description": "作品二", "authorDouyinId": "sphExample123"})
            comment = {"nickname": "甲", "content": "评论", "commentTime": "昨天", "ipLocation": "广东"}
            a = save_comments(path, first, [comment])[0]
            self.assertEqual(save_comments(path, first, [comment])[0], a)
            b = save_comments(path, second, [comment])[0]
            self.assertNotEqual(a["event_id"], b["event_id"])
            self.assertEqual(a["parent_event_id"], first["event_id"])
            with self.assertRaises(ValueError):
                save_comments(path, {"event_type": "VIDEO", "event_id": "missing"}, [comment])
            with self.assertRaises(ValueError):
                save_comments(path, first, [{"nickname": "甲"}])

    def test_recorded_comment_with_literal_ellipsis_is_not_discarded(self):
        fixture = json.loads((Path(__file__).parent / "testdata/preemptive-scrolled-drawer.json").read_text(encoding="utf-8"))
        comments = parse_comments(fixture["top"], fixture["height"])
        target = next(c for c in comments if c["nickname"] == "恒萌琪铭")
        self.assertEqual(target["content"], "两位无人岛还去吗....")
        lines = [line("评论12", 400), line("甲广东昨天", 630),
                 line("尚未展开...展开", 680), line("乙广东昨天", 750),
                 line("另一条完整评论", 790), line("发表评论", 1460)]
        self.assertNotIn("甲", [c["nickname"] for c in parse_comments(lines, 1559)])

    def test_first_comment_tag_and_in_drawer_ad_are_excluded(self):
        lines = [
            line("评论1", 491, 26, 70, 29),
            line("月井羊一瘦至对象作者浙江2天前", 564, 96, 405, 32),
            line("这一集，我拍了四个月。", 608, 96, 295, 29),
            line("」原声", 780, 127, 68, 25),
            line("Sunburst...海南2天前", 928, 99, 259, 27),
            line("你确实是会吃懂吃", 969, 98, 219, 27),
            line("首评", 1011, 102, 41, 24),
            line("超自然新手小黄", 1171, 96, 208, 27),
            line("广告", 1172, 602, 48, 25),
            line("来<超自然行动组>看最新内容", 1216, 98, 463, 27),
            line("了解详情", 1296, 134, 97, 27),
        ]
        comments = parse_comments(lines, 1605)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["nickname"], "Sunburst...")
        self.assertEqual(comments[0]["content"], "你确实是会吃懂吃")
        self.assertEqual(comments[0]["ipLocation"], "海南")
        self.assertEqual(comments[0]["commentTime"], "2天前")

    def test_channels_butterfly_badge_and_ellipsis_cleanup(self):
        self.assertEqual(clean_comment_nickname("2020幸福来敲.··W"), ("2020幸福来敲...", True))
        self.assertEqual(clean_comment_nickname("馨大宝爸爸W"), ("馨大宝爸爸", True))
        self.assertEqual(clean_comment_nickname("小憩memories W"), ("小憩memories", True))
        self.assertEqual(clean_comment_nickname("Shadow"), ("Shadow", False))

    def test_inline_replies_and_thumb_like_badge_preserve_parent_reply_count(self):
        lines = [
            line("评论485", 491, 26, 111, 29),
            line("喜剧之王单口季W作者上海2025年7月30日", 558, 99, 495, 28),
            line("@庞博谈脱口秀演员现状", 600, 96, 560, 29),
            line("原声", 776, 127, 50, 27),
            line("2020幸福来敲.··W广东2天前", 935, 98, 344, 27),
            line("173", 936, 591, 44, 24),
            line("总决赛应该取消复活赛", 980, 96, 502, 27),
            line("阿凡提新疆2025年8月2日", 1111, 164, 300, 26),
            line("9凸", 1106, 633, 52, 34),
            line("是的，待定池里捞人全凭主观", 1157, 160, 522, 29),
            line("北京8月9日", 1249, 164, 150, 26),
            line("11", 1252, 619, 25, 23),
            line("回复阿凡提:对，晋级赛淘汰了", 1295, 160, 480, 27),
            line("61条回复", 1340, 96, 128, 29),
            line("馨大宝爸爸W北京2025年7月31日", 1419, 98, 376, 26),
            line("老选手不想讲段子了", 1463, 98, 420, 27),
            line("23条回复", 1508, 96, 128, 29),
        ]
        drawer = parse_comment_drawer(lines, 1605, parent_event_id="vid-1")
        comments = [c for c in drawer["comments"]]
        self.assertEqual([c["nickname"] for c in comments], ["2020幸福来敲...", "阿凡提", "[表情昵称]", "馨大宝爸爸"])
        self.assertEqual(comments[0]["commentLevel"], 1)
        self.assertEqual(comments[0]["replyCount"], 61)
        self.assertEqual(comments[1]["commentLevel"], 2)
        self.assertEqual(comments[1]["parentCommentKey"], comments[0]["key"])
        self.assertEqual(comments[1]["replyToNickname"], "2020幸福来敲...")
        self.assertEqual(comments[2]["commentLevel"], 2)
        self.assertEqual(comments[2]["parentCommentKey"], comments[0]["key"])
        self.assertEqual(comments[2]["replyToNickname"], "阿凡提")
        self.assertEqual(comments[3]["commentLevel"], 1)
        self.assertEqual(comments[3]["replyCount"], 23)
        resolve_reply_targets(comments, [{"commentKey": comments[1]["key"], "profileVisited": True, "douyinId": "sphAfanti888"}])
        self.assertEqual(comments[2]["replyToCommentKey"], comments[1]["key"])
        self.assertEqual(comments[2]["replyToDouyinId"], "sphAfanti888")

    def test_overlapping_pages_do_not_collect_one_comment_twice(self):
        # Huawei Hebe-BD00 run 49c170b4: pages 3 and 4 read the same comment with ".." and "...".
        page_3 = [
            line("评论196", 477, 24, 114, 34),
            line("佳一讲商业 我 浙江40分钟前", 1211, 99, 338, 28),
            line("一下子回到了学生时代", 1252, 101, 267, 27),
            line("2636", 1344, 588, 101, 29),
            line("你的眼淚是零.福建9天前", 1347, 101, 310, 23),
            line("十七岁就是很美好啊..比十六岁成熟 比十八岁稚嫩", 1386, 101, 590, 28),
            line("1001", 1435, 589, 100, 26),
            line("佳一讲商业发表评论：", 1494, 114, 254, 27),
        ]
        page_4 = [
            line("评论196", 477, 24, 114, 34),
            line("佳一讲商业我浙江40分钟前", 711, 99, 339, 27),
            line("一下子回到了学生时代", 750, 99, 272, 32),
            line("2636", 841, 586, 105, 35),
            line("你的眼淚是零..福建9天前", 846, 99, 314, 27),
            line("十七岁就是很美好啊...比十六岁成熟 比十八岁稚嫩", 886, 102, 589, 28),
            line("1084", 935, 586, 106, 39),
            line("讨厌啊讨厌讨...江西8天前", 943, 157, 346, 28),
            line("十八我只能说高考加油", 982, 157, 268, 31),
            line("16条回复", 1042, 99, 137, 29),
            line("226", 1128, 599, 95, 41),
            line("sayOh江苏8天前", 1133, 94, 222, 33),
            line("这张完全初恋感", 1174, 98, 192, 32),
            line("佳一讲商业发表评论：", 1494, 114, 254, 27),
        ]
        collected, aliases = {}, {}
        first = parse_comment_drawer(page_3, 1559, parent_event_id="vid")["comments"]
        second = parse_comment_drawer(page_4, 1559, parent_event_id="vid")["comments"]
        for item in first + second:
            _collect_comment(collected, aliases, item)

        names = [c["nickname"] for c in collected.values()]
        self.assertEqual(sum("眼淚" in n for n in names), 1, names)
        self.assertEqual(sum("佳一讲商业" in n for n in names), 1, names)
        reply = next(c for c in collected.values() if c["nickname"].startswith("讨厌啊"))
        kept = next(c for c in collected.values() if "眼淚" in c["nickname"])
        # The reply under the merged comment points at the comment that was kept.
        self.assertEqual(reply["parentCommentKey"], kept["key"])
        self.assertIn("sayOh", names)

    def test_recommend_badge_with_merged_count_keeps_the_liked_comment(self):
        # vivo V2131A run f5625b81: after liking, OCR read the count and badge as one box "5推荐".
        lines = [
            line("评论2", 436, 95, 90, 37),
            line("Q都在搜：王楚然说山东话表白", 444, 347, 340, 22),
            line("综艺大表姐作者 江西1天前", 575, 100, 388, 28),
            line("王楚然用方言表白，给黄sir整娇羞了#现在就出", 616, 102, 567, 24),
            line("发#现在就出发第4季", 652, 102, 256, 23),
            line("AI吕哥说我1分钟前", 921, 98, 270, 28),
            line("这段太有意思了，看完心情都变好了", 961, 96, 438, 30),
            line("5推荐", 1053, 526, 166, 32),
            line("嫣然含笑河南1天前", 1058, 100, 268, 23),
            line("王楚然好可爱啊，表情好丰富，演技真好", 1098, 100, 588, 28),
            line("首评", 1175, 96, 58, 32),
        ]
        drawer = parse_comment_drawer(lines, 1605, parent_event_id="vid-1")
        liked = next(c for c in drawer["comments"] if c["nickname"] == "嫣然含笑")
        self.assertTrue(liked["_liked"])
        self.assertEqual(liked["_likeCountText"], "5")
        self.assertEqual(liked["content"], "王楚然好可爱啊，表情好丰富，演技真好")

    def test_reply_target_name_requires_single_colon_on_reply(self):
        self.assertEqual(reply_target_name("回复 阿凡提:对，很有道理", is_reply=True), "阿凡提")
        self.assertEqual(reply_target_name("回复  样本用户：好的", is_reply=True), "样本用户")
        self.assertIsNone(reply_target_name("回复  名称：后缀：内容", is_reply=True))
        self.assertIsNone(reply_target_name("回复 阿凡提:这是一级评论", is_reply=False))

    def test_collect_expands_replies_visits_channels_and_wechat_profiles_and_persists_users(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            path = Path(directory) / "videos.sqlite3"
            store = LocalStore(path)
            video_event, _ = store.save({
                "authorNickname": "喜剧之王单口季",
                "description": "@庞博谈脱口秀演员现状",
                "authorDouyinId": "sphAuthor001",
            })
            feed = [line("485", 1555, 622, 42, 23)]
            drawer_initial = [
                line("评论2", 491, 26, 90, 29),
                line("Sunburst...海南2天前", 928, 99, 259, 27),
                line("折叠长评...展开", 969, 98, 260, 27),
                line("首评", 1011, 102, 41, 24),
                line("馨大宝爸爸W北京7月31日", 1120, 98, 320, 26),
                line("老选手不想讲段子了", 1165, 98, 360, 27),
                line("1条回复", 1210, 96, 110, 27),
            ]
            drawer_after_comment_expand = [
                drawer_initial[0],
                drawer_initial[1],
                line("折叠长评完整内容已展开", 969, 98, 320, 27),
                drawer_initial[3],
                *drawer_initial[4:],
            ]
            drawer_after_reply_expand = [
                *drawer_after_comment_expand[:-1],
                line("豪豪看吧W四川8月1日", 1215, 164, 280, 26),
                line("12", 1216, 619, 28, 24),
                line("回复馨大宝爸爸:哈哈一语中的", 1255, 160, 360, 27),
                line("没有更多评论", 1340, 280, 160, 25),
            ]
            channels_profile = [
                line("馨大宝爸爸", 509, 192, 180, 32),
                line("北京男", 552, 192, 90, 26),
                line("热爱生活", 644, 35, 160, 28),
                line("关注", 760, 162, 56, 28),
                line("私信", 760, 496, 56, 28),
            ]
            profile_menu = [line("更多信息", 1400, 280, 140, 30)]
            basic_info = [
                line("基础信息", 479, 40, 120, 30),
                line("IP归属地", 608, 40, 125, 28),
                line("北京", 608, 250, 68, 28),
                line("视频号ID", 737, 41, 126, 28),
                line("sphXinDad12345", 737, 250, 240, 28),
            ]
            wechat_user_card = [
                line("评论2", 491, 26, 90, 29),
                line("设置", 1206, 619, 55, 29),
                line("Sunburst", 1344, 182, 159, 32),
                line("海南 三亚 女", 1396, 182, 144, 30),
            ]
            ui = FakeCommentUi([
                feed,
                drawer_initial,
                drawer_after_comment_expand,
                drawer_after_reply_expand,
                channels_profile,
                profile_menu,
                basic_info,
                drawer_after_reply_expand,
                wechat_user_card,
                drawer_after_reply_expand,
            ])
            timings = Timings(Path(directory) / "timings.jsonl", "run-comments")
            result = collect_visible_comments(
                ui,
                path,
                video_event,
                timings,
                max_pages=2,
                max_reply_expansions=1,
                max_profiles=2,
            )
            self.assertTrue(result["complete"])
            self.assertEqual(result["endReason"], "end_of_list")
            self.assertEqual(result["replyExpansions"], 1)
            self.assertEqual(len(result["events"]), 3)
            self.assertEqual(len(result["userEvents"]), 2)
            self.assertEqual(result["skippedProfiles"], [])

            by_nick = {e["payload"]["nickname"]: e["payload"] for e in result["events"]}
            self.assertEqual(by_nick["Sunburst..."]["content"], "折叠长评完整内容已展开")
            self.assertIsNone(by_nick["Sunburst..."]["authorDouyinId"])
            self.assertEqual(by_nick["Sunburst..."]["touchStatus"], "visited_no_channels_id")
            self.assertEqual(by_nick["馨大宝爸爸"]["authorDouyinId"], "sphXinDad12345")
            self.assertTrue(by_nick["馨大宝爸爸"]["repliesCollected"])
            self.assertEqual(by_nick["豪豪看吧"]["commentLevel"], 2)
            self.assertEqual(by_nick["豪豪看吧"]["replyToNickname"], "馨大宝爸爸")
            self.assertEqual(by_nick["豪豪看吧"]["replyToDouyinId"], "sphXinDad12345")

            with contextlib.closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM comment_drafts").fetchone()[0], 3)
                self.assertEqual(db.execute("SELECT count(*) FROM user_drafts").fetchone()[0], 2)


def fixture(name):
    return json.loads((Path(__file__).parent / "testdata" / name).read_text(encoding="utf-8"))


class ControlTextTests(unittest.TestCase):
    def test_badge_placeholder_from_input_bar_control_is_not_part_of_the_nickname(self):
        self.assertEqual(clean_comment_nickname("AI吕哥说#"), ("AI吕哥说", True))
        self.assertEqual(clean_comment_nickname("AI吕哥说W"), ("AI吕哥说", True))
        self.assertEqual(clean_comment_nickname("佳一讲商业"), ("佳一讲商业", False))

    def test_user_card_location_includes_the_separate_gender_label(self):
        lines = [line("设置", 1150, 600, 60, 34), line("Mr. 陈", 1180, 187, 87, 41),
                 line("陕西 安康", 1228, 187, 108, 34), line("男", 1228, 303, 25, 34),
                 line("财富是对认知的补偿，而不是对勤奋的嘉奖！", 1269, 187, 460, 34)]
        card = parse_wechat_user_card(lines, "Mr. 陈", 1605)
        self.assertEqual((card["nickname"], card["profileLocation"]), ("Mr. 陈", "陕西 安康 男"))


class CommentPagingTests(unittest.TestCase):
    def setUp(self):
        self._settle = comments_module.REPLY_SETTLE_SECONDS
        comments_module.REPLY_SETTLE_SECONDS = 0

    def tearDown(self):
        comments_module.REPLY_SETTLE_SECONDS = self._settle

    def test_replies_on_a_page_without_their_thread_header_keep_the_parent(self):
        data = fixture("reply-thread-pages.json")
        parent = {"nickname": "櫂ToT", "key": "wxch-comment:parent", "commentLevel": 1}
        orphan = parse_comment_drawer(data["pages"]["page2"], data["height"], "video")
        self.assertEqual(orphan["comments"], [])
        page = parse_comment_drawer(data["pages"]["page2"], data["height"], "video",
                                    resolve_parent=lambda reply: parent)
        names = [c["nickname"] for c in page["comments"]]
        self.assertEqual(names[:6], ["急需2778个板车", "欣林林", "恋", "昕妤", "灭霸", "梧桐吟夏"])
        self.assertTrue(all(c["commentLevel"] == 2 for c in page["comments"]))
        self.assertTrue(all(c["parentCommentKey"] == "wxch-comment:parent" for c in page["comments"]))
        self.assertEqual(page["comments"][3]["replyToNickname"], "恋")
        self.assertEqual(page["comments"][1]["replyToNickname"], "急需2778个...")

    def test_avatar_glyph_does_not_turn_a_reply_into_a_top_level_comment(self):
        data = fixture("reply-thread-pages.json")
        parent = {"nickname": "櫂ToT", "key": "wxch-comment:parent"}
        page = parse_comment_drawer(data["pages"]["page3"], data["height"], "video",
                                    resolve_parent=lambda reply: parent)
        by_name = {c["nickname"]: c for c in page["comments"]}
        self.assertEqual(by_name["7Y2o"]["commentLevel"], 2)
        self.assertEqual(len(page["comments"]), 7)

    def test_reading_cut_off_by_the_input_bar_is_replaced_by_the_full_one(self):
        collected, aliases = {}, {}
        parent = {"nickname": "甲", "key": "wxch-comment:parent"}
        data = fixture("reply-thread-pages.json")
        for name in ("page2", "page3"):
            page = parse_comment_drawer(data["pages"][name], data["height"], "video",
                                        resolve_parent=lambda reply: parent)
            for item in page["comments"]:
                _collect_comment(collected, aliases, item)
        readings = [c for c in collected.values() if c["nickname"] == "7Y2o"]
        self.assertEqual(len(readings), 1)
        self.assertIn("所以我还", readings[0]["content"])

    def test_same_person_ignores_truncation_dots_but_not_other_regions(self):
        self.assertTrue(same_person({"nickname": "基石施工围挡...", "ipLocation": "河北"},
                                    {"nickname": "基石施工围挡", "ipLocation": "河北"}))
        self.assertFalse(same_person({"nickname": "基石施工围挡", "ipLocation": "河北"},
                                     {"nickname": "基石施工围挡", "ipLocation": "广东"}))
        self.assertFalse(same_person({"nickname": "小王"}, {"nickname": "小李"}))

    def test_skipped_commenter_card_is_closed_before_reading_on(self):
        data = fixture("user-card-stuck.json")
        ui = FakeCommentUi([data["cardLoading"], data["cardStillOpen"], data["drawer"]])
        ui.height = data["height"]
        self.assertTrue(user_card_open(data["cardStillOpen"], data["height"]))
        self.assertFalse(user_card_open(data["drawer"], data["height"]))
        comment = {"nickname": "L", "key": "k", "_nameBox": [152, 1269, 32, 38]}
        with self.assertRaises(CommenterProfileSkipped) as caught:
            inspect_commenter_profile(ui, comment)
        self.assertEqual(caught.exception.reason, "identity_unconfirmed")
        self.assertEqual(ui.backs, 2)
        self.assertIs(caught.exception.recovered_lines, data["drawer"])

    def test_comment_limits(self):
        self.assertEqual(comment_limits({}), {})
        self.assertEqual(comment_limits({"maxPages": "20", "maxReplyExpansions": "20", "maxProfiles": "0"}),
                         {"max_pages": 20, "max_reply_expansions": 20, "max_profiles": 0})
        self.assertEqual(comment_limits({"unlimited": True, "maxPages": "3"}),
                         {"max_pages": None, "max_reply_expansions": None, "max_profiles": None})
        with self.assertRaises(ValueError):
            comment_limits({"maxPages": "0"})
        with self.assertRaises(ValueError):
            comment_limits({"maxProfiles": "-1"})

    def test_unlimited_run_keeps_comments_read_before_expanding_and_follows_more_replies(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            path = Path(directory) / "videos.sqlite3"
            video_event, _ = LocalStore(path).save({
                "authorNickname": "作者", "description": "作品", "authorDouyinId": "sphAuthor001",
            })
            bar = line("我发表评论：", 1500, 110, 200, 25)
            first = [line("评论5", 400, 25, 90, 29),
                     line("甲广东8月2日", 500), line("甲的评论", 540),
                     line("乙广东8月2日", 620), line("乙的评论", 660),
                     line("2条回复", 700, 96, 110), bar]
            # Opening the thread scrolls 甲 out of view.
            thread = [line("评论5", 400, 25, 90, 29),
                      line("乙广东8月2日", 500), line("乙的评论", 540),
                      line("丙广东8月3日", 600, 160, 280), line("回复乙：丙的回复", 640, 160, 280),
                      line("展开更多回复", 700, 160, 150), bar]
            full = [*thread[:5],
                    line("丁广东8月3日", 720, 160, 280), line("回复丙：丁的回复", 760, 160, 280), bar]
            ui = FakeCommentUi([[line("5", 1555, 622, 42, 23)], first, thread, full, full])
            result = collect_visible_comments(
                ui, path, video_event, Timings(Path(directory) / "t.jsonl", "run"),
                max_pages=None, max_reply_expansions=None, max_profiles=0)
            names = {e["payload"]["nickname"]: e["payload"] for e in result["events"]}
            self.assertEqual(set(names), {"甲", "乙", "丙", "丁"})
            self.assertEqual(result["replyExpansions"], 2)
            self.assertEqual(result["endReason"], "end_of_list")
            self.assertEqual(names["丁"]["replyToNickname"], "丙")
            self.assertEqual(ui.labels.count("comments-replies"), 2)


if __name__ == "__main__":
    unittest.main()
