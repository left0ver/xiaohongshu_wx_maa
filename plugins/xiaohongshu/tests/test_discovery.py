import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "xiaohongshu" / "agent"))
from discovery import Outbox, note_link, clipboard_value


NOTE_ID = "0123456789abcdef01234567"


def work(note_id=NOTE_ID):
    return dict(noteType="IMAGE_TEXT", title="标题", authorNickname="作者",
                authorDouyinId="red123", videoUrl=f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token=abc")


class DiscoveryTests(unittest.TestCase):
    def test_clipboard_uses_selected_custom_result(self):
        value = {"text": "https://xhslink.com/a/abc", "timestamp": 123}
        self.assertEqual(clipboard_value({"best": {"detail": value}, "all": []}), value)
        for invalid in ({}, {"best": {}}, {"best": {"detail": {"text": "x", "timestamp": True}}}):
            with self.assertRaises(ValueError):
                clipboard_value(invalid)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "events.sqlite"
        self.queue = Outbox(self.path, "authorization-a", "device-a")

    def tearDown(self):
        self.queue.close()
        self.temp.cleanup()

    def test_link_preserves_token_and_extracts_id(self):
        link = work()["videoUrl"]
        self.assertEqual(note_link(link), (link, NOTE_ID, "note:" + NOTE_ID))
        self.assertEqual(note_link("分享 https://xhslink.com/a/hello 。")[1], "")
        self.assertEqual(note_link("分享 http://xhslink.cn/a/hello")[1], "")
        self.assertNotEqual(note_link("https://xhslink.com/a/same")[2], note_link("https://xhslink.cn/a/same")[2])
        for scheme in ("http", "https"):
            for host in ("xiaohongshu.com", "www.xiaohongshu.com"):
                self.assertEqual(note_link(f"{scheme}://{host}/discovery/item/{NOTE_ID}")[1], NOTE_ID)
        for link in ("https://www.xiaohongshu.com.evil.test/explore/" + NOTE_ID,
                     "https://www.xiaohongshu.com/explore/123",
                     "https://user:pass@xhslink.com/a/hello",
                     "https://xhslink.cn.evil.test/a/hello"):
            with self.assertRaises(ValueError):
                note_link(link)

    def test_retry_reopen_and_ack_receipt(self):
        event_id = self.queue.enqueue("NOTE", work())
        self.assertEqual(self.queue.enqueue("NOTE", work()), event_id)
        self.queue.close()
        self.queue = Outbox(self.path, "authorization-a", "device-a")
        self.assertEqual(len(self.queue.batch()["discoveryEvents"]), 1)
        self.assertEqual(self.queue.acknowledge({"discoveryAckIds": [event_id]}, []), 0)
        with self.assertRaises(ValueError):
            self.queue.acknowledge({"ok": True}, [event_id])
        self.assertEqual(self.queue.acknowledge({"discoveryAckIds": [event_id]}, [event_id]), 1)
        self.assertEqual(self.queue.enqueue("NOTE", work()), event_id)
        self.assertEqual(self.queue.batch()["discoveryEvents"], [])

    def test_scope_isolation(self):
        event_id = self.queue.enqueue("NOTE", work())
        other = Outbox(self.path, "authorization-b", "device-a")
        try:
            self.assertEqual(other.batch()["discoveryEvents"], [])
            self.assertEqual(other.acknowledge({"discoveryAckIds": [event_id]}, [event_id]), 0)
            with self.assertRaises(ValueError):
                other.enqueue("COMMENT", dict(nickname="用户", content="正文", commentTime="昨天"), parent_id=event_id)
        finally:
            other.close()
        self.assertNotIn(b"authorization-a", self.path.read_bytes())

    def test_parent_chain_and_distinct_works(self):
        first = self.queue.enqueue("NOTE", work())
        second = self.queue.enqueue("NOTE", work("1123456789abcdef01234567"))
        payload = dict(nickname="用户", content="正文", commentTime="昨天")
        comment = self.queue.enqueue("COMMENT", payload, parent_id=first)
        self.assertEqual(comment, self.queue.enqueue("COMMENT", payload, parent_id=first))
        self.assertNotEqual(comment, self.queue.enqueue("COMMENT", payload, parent_id=second))
        user = dict(nickname="用户", douyinId="red456", touchStatus="TOUCHED")
        self.queue.enqueue("USER", user, parent_id=comment)
        with self.assertRaises(ValueError):
            self.queue.enqueue("USER", user, parent_id=first)
        with self.assertRaises(ValueError):
            self.queue.enqueue("COMMENT", payload, mode="SEARCH", parent_id=first)

    def test_validation_and_batch_limit(self):
        with self.assertRaises(ValueError):
            self.queue.enqueue("VIDEO", work())
        with self.assertRaises(ValueError):
            self.queue.enqueue("NOTE", {**work(), "awemeId": "f" * 24})
        for index in range(201):
            self.queue.enqueue("NOTE", work(f"{index:024x}"))
        self.assertEqual(len(self.queue.batch()["discoveryEvents"]), 200)


if __name__ == "__main__":
    unittest.main()
