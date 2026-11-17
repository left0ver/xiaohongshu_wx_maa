import unittest

from tools.debug_console.engine import REPO_ROOT, analyze_run


class TestTimelineRealRuns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs = {}
        cls.base = REPO_ROOT / "data/testenv-stability-20261113/five-20261115-2228/phone/card1/runs"

    def read_run(self, run_id):
        if run_id not in self.runs:
            self.runs[run_id] = analyze_run(self.base / run_id)
        return self.runs[run_id]

    def test_link_actions_and_message_navigation(self):
        for run_id in (
            "a1a4cb52-82dd-454d-991a-c0632fc9ea89",
            "fb5bebc6-f036-4dd3-a4c8-19e2b921105b",
            "a44ef12f-40aa-4190-adda-6bf3f74bcaee",
            "6e558fbb-c868-42c6-b635-cc7406b1cb55",
            "0bafb485-0f1b-45f5-b2ed-5efb09a26286",
            "6577d59e-7e9c-4924-8998-6dea4a218d3b",
        ):
            with self.subTest(run_id=run_id):
                run = self.read_run(run_id)
                self.assertTrue(run["snapshots"])
                self.assertFalse(any(s["titleCn"].startswith("执行步骤：") for s in run["snapshots"]))
                self.assertEqual(run["timelineSteps"][-1]["stepKind"], "result")
                frames = [s for s in run["timelineSteps"] if s["stepKind"] == "snapshot"]
                self.assertEqual([s["filename"] for s in frames], [s["filename"] for s in run["snapshots"]])
        dm = self.read_run("a44ef12f-40aa-4190-adda-6bf3f74bcaee")
        check = next(s for s in dm["snapshots"] if s["label"] == "profile-follow-check")
        self.assertNotIn("关注", check["titleCn"])
        comment = self.read_run("0bafb485-0f1b-45f5-b2ed-5efb09a26286")
        composer = next(s for s in comment["snapshots"] if s["label"] == "comments-composer")
        self.assertNotIn("回复", composer["titleCn"])
        drawer = next(s for s in comment["snapshots"] if s["label"] == "comments")
        self.assertIsNone(drawer["clickTarget"])
        messages = self.read_run("6577d59e-7e9c-4924-8998-6dea4a218d3b")
        result = str(messages["timelineSteps"][-1]["collectedItems"])
        self.assertIn("已经返回任务页面", result)
        self.assertIn("杨语晴", result)
        self.assertIn("已经点赞，跳过点击", result)

    def test_uploads_and_duplicate_result(self):
        cache = REPO_ROOT / "data/debug-console-cache"
        collection = cache / "8PGBB22315202169/data__local__tmp__wechat-channels-data__remote__52c273cae71482147dece3344da06eb42208a7601800b30de594f5161669d7e0/runs/9529bc32-2046-460f-919f-1dfc75251617"
        run = analyze_run(collection)
        steps = run["timelineSteps"]
        self.assertEqual(sum(s["label"] == "discoverySending" for s in steps), 3)
        self.assertEqual(sum(s["label"] == "discoveryAcknowledged" for s in steps), 3)
        timed = [s["observedAtUnixMs"] for s in steps if s["stepKind"] == "event" and s.get("observedAtUnixMs")]
        self.assertEqual(timed, sorted(timed))
        self.assertEqual([s["sequence"] for s in steps], list(range(1, len(steps) + 1)))
        duplicate = analyze_run(cache / "3413812929000TN/data__local__tmp__wechat-channels-data/runs/fc1bdf4a-1cc0-4f99-b0a0-94f220dfc1e7")
        self.assertIn("跳过回复", str(duplicate["timelineSteps"][-1]["collectedItems"]))
        self.assertEqual(len(duplicate["snapshots"]), 8)


if __name__ == "__main__":
    unittest.main()
