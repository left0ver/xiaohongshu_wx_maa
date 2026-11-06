import tempfile
import unittest
from pathlib import Path

from collection import LocalStore
from comments import parse_comments, save_comments


def line(text, y, x=99):
    return {"text": text, "box": [x, y, 300, 25]}


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
