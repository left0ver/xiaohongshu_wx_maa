"""Business tests: parsing, identity, persistence, failure safety and timing."""

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
from collection import LocalStore, Timings, collect, identity, open_basic_info, parse_author, parse_basic_info, parse_video


def line(text, x, y, width=160, height=28):
    return {"text": text, "box": [x, y, width, height], "score": 0.99}


AUTHOR = "示例作者"
FEED = [line("关注", 210, 92), line("朋友", 315, 92), line("推荐", 445, 92),
        line(AUTHOR, 122, 1455), line("294", 630, 1500, 55)]
DETAILS = [line("评论294", 27, 482), line(AUTHOR + "W作者福建8月1日", 99, 568, 362),
           line("完整的作品文案#音乐#我", 98, 604, 594), line("们俩", 99, 643, 58),
           line("相关音乐·我们俩", 102, 691, 251), line("帮她上热门", 122, 766),
           line("评论者四川8月1日", 99, 870), line("不能采集成作品文案", 99, 909)]
PROFILE = [line(AUTHOR, 192, 509), line("福建厦门女", 192, 552),
           line("作者简介", 35, 644), line("115条原创内容", 35, 682),
           line("关注", 162, 760, 56), line("私信", 496, 760, 56)]
INFO = [line("基础信息", 40, 479), line("IP归属地", 40, 608, 125), line("福建", 250, 607, 68),
        line("资料所在地", 40, 670), line("福建厦门", 252, 673),
        line("视频号ID", 41, 737, 126), line("sphExample123", 250, 735, 278)]


class FakeUi:
    height = 1558

    def __init__(self, snapshots):
        self.snapshots = iter(snapshots)
        self.clicks = []
        self.backs = 0
        self.labels = []

    def snapshot(self, label, author=None):
        self.labels.append(label)
        return next(self.snapshots)

    @contextlib.contextmanager
    def prepare_basic_info(self):
        yield None

    def wait_basic_info(self, timeout=5, reader=None):
        from layout import basic_info_ready
        lines = self.snapshot("author-basic-info-ready")
        if not basic_info_ready(lines):
            raise ValueError("Author basic information was not ready within 5 seconds")
        return lines

    def click(self, target):
        self.clicks.append(target["text"])

    def back(self):
        self.backs += 1

    def profile_more(self, name):
        self.clicks.append("profile-menu")

    def start_wechat(self):
        pass


class CollectionTests(unittest.TestCase):
    def test_already_in_channels_can_use_top_template_after_bottom_miss(self):
        target = {"text": "推荐", "navigation": "recommend", "box": [440, 90, 50, 30]}
        ui = FakeUi([[], [target], FEED])
        from collection import ensure_feed
        self.assertEqual(ensure_feed(ui), FEED)
        self.assertEqual(ui.clicks, ["推荐"])

    def test_template_navigation_does_not_need_synthetic_ocr_labels(self):
        targets = [{"text": text, "navigation": role, "box": [100, 100, 50, 30]}
                   for text, role in [("发现", "discover"), ("视频号", "channels"), ("推荐", "recommend")]]
        ui = FakeUi([[target] for target in targets] + [FEED])
        from collection import ensure_feed
        self.assertEqual(ensure_feed(ui), FEED)
        self.assertEqual(ui.clicks, ["发现", "视频号", "推荐"])
        self.assertEqual(ui.labels, ["navigation-bottom", "navigation-top", "navigation-top", "feed"])

    def test_basic_info_ready_reuses_values_without_another_ocr(self):
        ui = FakeUi([INFO])
        self.assertEqual(open_basic_info(ui, line("更多信息", 280, 1400)), parse_basic_info(INFO))
        self.assertEqual(ui.labels, ["author-basic-info-ready"])
        self.assertEqual(ui.clicks, ["更多信息"])

    def test_click_settling_consumes_the_same_readiness_budget(self):
        ui = FakeUi([])
        ui.wait_basic_info = Mock(return_value=INFO)
        with patch("collection.time.monotonic", side_effect=[100, 100.8]):
            open_basic_info(ui, line("更多信息", 280, 1400))
        self.assertAlmostEqual(ui.wait_basic_info.call_args.kwargs["timeout"], 4.2)

    def test_basic_info_readiness_does_not_certify_missing_id(self):
        ui = FakeUi([INFO[:-1], INFO])
        self.assertEqual(open_basic_info(ui, line("更多信息", 280, 1400)), parse_basic_info(INFO))
        self.assertEqual(ui.labels, ["author-basic-info-ready", "author-basic-info"])
        self.assertEqual(ui.clicks, ["更多信息"])

    def test_basic_info_timeout_keeps_flow_unsaved_and_records_failure(self):
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
            ui = FakeUi([FEED, FEED, DETAILS, FEED, PROFILE, [line("更多信息", 280, 1400)], []])
            store = LocalStore(Path(root) / "videos.sqlite3")
            timings = Timings(Path(root) / "timings.jsonl", "info-timeout")
            with self.assertRaisesRegex(ValueError, "within 5 seconds"):
                collect(ui, store, timings)
            with store.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM video_drafts").fetchone()[0], 0)
            self.assertEqual([(row["taskName"], row["outcome"]) for row in timings.rows[-2:]],
                             [("collect_author_profile", "failed"), ("collect_video_total", "failed")])
            self.assertNotIn("returned-feed", ui.labels)

    def test_navigation_uses_bottom_then_top_quarter(self):
        from collection import ensure_feed, navigation_roi
        home = [line("发现", 426, 1522), line("通讯录", 220, 1522)]
        discover = [line("发现", 320, 90), line("视频号", 100, 300)]
        ui = FakeUi([home, discover, FEED[:3], FEED])
        ensure_feed(ui)
        self.assertEqual(ui.labels, ["navigation-bottom", "navigation-top", "navigation-top", "feed"])
        self.assertEqual(ui.clicks, ["发现", "视频号", "推荐"])
        self.assertEqual(navigation_roi("navigation-bottom", 1559), [0, 1169, 720, 390])
        self.assertEqual(navigation_roi("navigation-top", 1559), [0, 0, 720, 390])

    def test_bare_music_and_misread_icon_end_description(self):
        for music in ("音乐", "」音乐", "♪ 音乐", "相关音乐"):
            details = list(DETAILS)
            details[4] = line(music, 102, 691)
            self.assertEqual(parse_video(details, AUTHOR)["description"], "完整的作品文案#音乐#我们俩")

    def test_first_description_is_saved_without_return_read_or_punctuation_retry(self):
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
            dotted = list(DETAILS)
            dotted[3] = line("们俩.", 99, 643, 58)
            # No returned-page snapshots are provided: a second read would fail.
            ui = FakeUi([FEED, FEED, dotted, FEED, PROFILE, [line("更多信息", 280, 1400)], INFO])
            store = LocalStore(Path(root) / "drafts.sqlite3")
            timing = Timings(Path(root) / "timings.jsonl", "first-reading")
            event = collect(ui, store, timing)
            self.assertEqual(event["payload"]["description"], "完整的作品文案#音乐#我们俩.")
            self.assertIs(event["payload"]["returnVerified"], False)
            self.assertEqual(ui.labels.count("video-details"), 1)
            self.assertNotIn("returned-feed", ui.labels)
            self.assertFalse(any(row["taskName"].startswith("verify_") for row in timing.rows))
            self.assertEqual(ui.backs, 3)

    def test_feed_certification_checkmark_is_not_another_author(self):
        from collection import author_in_feed
        self.assertEqual(author_in_feed([*FEED, line("√", 220, 1459, 16, 13)], 1558)["text"], AUTHOR)

    def test_split_author_header_and_music_boundary(self):
        lines = [line("评论1102", 27, 482), line("小憩memories W", 99, 563),
                 line("作者浙江6月27日", 101, 605),
                 line("去看更多视频#钢琴", 99, 652), line("音乐·Luvsic", 104, 701),
                 line("帮他上热门", 120, 776), line("其他评论", 99, 880)]
        result = parse_video(lines, "小憩memories")
        self.assertEqual(result["publishLocation"], "浙江")
        self.assertEqual(result["publishTimeText"], "6月27日")
        self.assertEqual(result["description"], "去看更多视频#钢琴")
        self.assertEqual(parse_video(lines, "首次昵称")["authorNickname"], "首次昵称")

    def test_startup_waits_for_ready_page_and_continues_early(self):
        from collection import ensure_feed
        ui = FakeUi([[], [], FEED, FEED])
        with patch("collection.time.monotonic", side_effect=[100, 101, 101.5]), patch("collection.time.sleep") as sleep:
            self.assertEqual(ensure_feed(ui), FEED)
        sleep.assert_called_once_with(0.5)
        self.assertEqual(ui.clicks, ["推荐"])

    def test_startup_timeout_does_not_click(self):
        from collection import ensure_feed
        ui = FakeUi([[], []])
        with patch("collection.time.monotonic", side_effect=[100, 110]), patch("collection.time.sleep") as sleep:
            with self.assertRaisesRegex(ValueError, "10 seconds"):
                ensure_feed(ui)
        sleep.assert_not_called()
        self.assertEqual(ui.clicks, [])

    def payload(self):
        return {**parse_video(DETAILS, AUTHOR), **parse_author(PROFILE, AUTHOR), **parse_basic_info(INFO)}

    def test_work_text_excludes_comments_and_preserves_wrapped_hashtags(self):
        value = parse_video(DETAILS, AUTHOR)
        self.assertEqual(value["description"], "完整的作品文案#音乐#我们俩")
        self.assertEqual(value["publishTimeText"], "8月1日")
        self.assertEqual(value["publishLocation"], "福建")

    def test_reject_missing_work_header(self):
        for lines, author in ((DETAILS[1:], AUTHOR), (DETAILS[3:], AUTHOR)):
            with self.subTest(author=author, count=len(lines)), self.assertRaises(ValueError):
                parse_video(lines, author)

    def test_reject_truncated_or_unbounded_description(self):
        truncated = [*DETAILS[:2], line("长文案…", 98, 604), *DETAILS[4:]]
        for lines in (truncated, DETAILS[:4]):
            with self.subTest(lines=lines), self.assertRaises(ValueError):
                parse_video(lines, AUTHOR)

    def test_profile_location_is_not_ip_location(self):
        profile = parse_author(PROFILE, AUTHOR)
        self.assertIsNone(profile["authorIpLocation"])
        self.assertEqual(profile["authorBio"], "作者简介")
        self.assertIsNone(profile["authorCertification"])

    def test_profile_name_difference_does_not_require_another_read(self):
        self.assertEqual(parse_author(PROFILE, "其他作者")["authorBio"], "作者简介")

    def test_certification_and_music_controls_are_not_bio(self):
        profile = [*PROFILE[:1], line("音乐人", 192, 550), line("江苏女", 192, 590),
                   *PROFILE[2:], line("音乐电台40人在听", 35, 720), line("播放", 597, 720)]
        result = parse_author(profile, AUTHOR)
        self.assertEqual(result["authorCertification"], "音乐人")
        self.assertEqual(result["authorProfileLocation"], "江苏女")
        self.assertEqual(result["authorBio"], "作者简介")

    def test_basic_information_pairs_labels_with_same_row_values(self):
        value = parse_basic_info(INFO)
        self.assertEqual(value["authorIpLocation"], "福建")
        self.assertEqual(value["authorProfileLocation"], "福建厦门")
        self.assertEqual(value["authorDouyinId"], "sphExample123")

    def test_no_author_id_is_invented(self):
        with self.assertRaises(ValueError):
            parse_basic_info(INFO[:-1])

    def test_identity_normalizes_wrapping_but_separates_author_and_description(self):
        self.assertEqual(identity("作者", "文案\n#标签"), identity("作者", "文案 #标签"))
        self.assertNotEqual(identity("ab", "c"), identity("a", "bc"))
        self.assertNotEqual(identity("作者A", "文案"), identity("作者B", "文案"))

    def test_duplicate_keeps_event_id_timestamp_and_device_after_reopen(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "videos.sqlite3"
            first, duplicate = LocalStore(path).save(self.payload())
            self.assertFalse(duplicate)
            second, duplicate = LocalStore(path).save({**self.payload(), "authorBio": "更新后的简介"})
            self.assertTrue(duplicate)
            for key in ("event_id", "device_id", "collected_at", "dedupe_key"):
                self.assertEqual(first[key], second[key])
            self.assertEqual(second["payload"]["authorBio"], "更新后的简介")
            with contextlib.closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM video_drafts").fetchone()[0], 1)

    def test_incomplete_record_does_not_enter_storage(self):
        with tempfile.TemporaryDirectory() as root:
            store = LocalStore(Path(root) / "drafts.sqlite3")
            with self.assertRaises(ValueError):
                store.save({"authorNickname": AUTHOR})
            with store.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM video_drafts").fetchone()[0], 0)

    def test_acceptance_sql_accepts_explicitly_unverified_and_historical_records(self):
        with tempfile.TemporaryDirectory() as root:
            store = LocalStore(Path(root) / "drafts.sqlite3")
            invalid = set()
            for index, flag in enumerate((False, True, None, "false")):
                event, _ = store.save({**self.payload(), "description": f"work {index}", "returnVerified": flag})
                if type(flag) is not bool:
                    invalid.add(event["event_id"])
            statements, current = [], ""
            for line in Path(__file__).with_name("acceptance.sql").read_text(encoding="utf-8").splitlines(keepends=True):
                current += line
                if sqlite3.complete_statement(current):
                    statements.append(current)
                    current = ""
            with store.connect() as db:
                results = [db.execute(statement).fetchall() for statement in statements]
            self.assertEqual(len(results), 3)
            self.assertEqual(results[1], [])
            self.assertEqual({row[0] for row in results[2]}, invalid)

    def test_differing_names_on_later_pages_keep_first_nickname(self):
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
            first = [*FEED[:3], line("李可q", 122, 1455), FEED[-1]]
            later = [*FEED[:3], line("李可g", 122, 1455), FEED[-1]]
            details = [DETAILS[0], line("李可gW作者福建8月1日", 99, 568), *DETAILS[2:]]
            profile = [line("李可g", 192, 509), *PROFILE[1:]]
            ui = FakeUi([first, first, details, later, profile, [line("更多信息", 280, 1400)], INFO])
            store = LocalStore(Path(root) / "videos.sqlite3")
            timings = Timings(Path(root) / "timings.jsonl", "names")
            event = collect(ui, store, timings)
            self.assertEqual(event["payload"]["authorNickname"], "李可q")
            self.assertEqual(event["dedupe_key"], "wxch-video:" + identity("李可q", event["payload"]["description"]))
            self.assertIs(event["payload"]["returnVerified"], False)
            with store.connect() as db:
                self.assertEqual(json.loads(db.execute("SELECT event_json FROM video_drafts").fetchone()[0]), event)
            self.assertEqual(timings.rows[-1]["outcome"], "succeeded")

    def test_profile_expansion_is_bounded_and_never_saves_truncated_bio(self):
        collapsed = [*PROFILE[:2], line("简介...展开", 35, 644), *PROFILE[3:]]
        for expanded in (PROFILE, collapsed):
            with self.subTest(expanded=expanded is PROFILE), tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                ui = FakeUi([FEED, FEED, DETAILS, FEED, collapsed, expanded,
                             [line("更多信息", 280, 1400)], INFO])
                result = collect(ui, LocalStore(Path(root) / "db.sqlite3"), Timings(Path(root) / "timings.jsonl", "bio"))
                self.assertEqual(ui.clicks.count("简介...展开"), 1)
                self.assertEqual(result["payload"]["authorBio"], "作者简介" if expanded is PROFILE else None)

    def test_discover_page_opens_channels_instead_of_clicking_discover_again(self):
        from collection import ensure_feed
        discover = [line("发现", 320, 90), line("视频号", 100, 300),
                    line("发现", 426, 1522), line("通讯录", 220, 1522)]
        ui = FakeUi([discover, FEED, FEED])
        ensure_feed(ui, launch=False)
        self.assertEqual(ui.clicks, ["视频号", "推荐"])

    def test_first_nickname_is_not_replaced_by_a_longer_profile_name(self):
        for wrong_header in (False, True, "longer-name"):
            with self.subTest(wrong_header=wrong_header), tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                initial = [*FEED[:3], line("示例作", 122, 1455), FEED[-1]]
                after = [*FEED[:3], line("示例", 122, 1455), FEED[-1]]
                details = list(DETAILS)
                if wrong_header:
                    header_author = AUTHOR + "乙" if wrong_header == "longer-name" else "示例作品乙"
                    details[1] = line(header_author + "W作者福建8月1日", 99, 568, 362)
                ui = FakeUi([initial, initial, details, after, PROFILE,
                             [line("更多信息", 280, 1400)], INFO])
                store = LocalStore(Path(root) / "videos.sqlite3")
                timings = Timings(Path(root) / "timings.jsonl", "faded-name")
                event = collect(ui, store, timings)
                self.assertEqual(event["payload"]["authorNickname"], "示例作")
                self.assertNotIn("_authorHeader", event["payload"])
                with store.connect() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM video_drafts").fetchone()[0], 1)

    def test_timing_is_written_on_exception(self):
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
            path = Path(root) / "timing.jsonl"
            timings = Timings(path, "run-1")
            with self.assertRaises(RuntimeError), timings.phase("failed-step"):
                raise RuntimeError("expected")
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["outcome"], "failed")
            self.assertEqual(record["runId"], "run-1")
            self.assertIsInstance(record["durationMs"], int)


if __name__ == "__main__":
    unittest.main()
