import json
import struct
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from tools.debug_console.engine import (
    REPO_ROOT,
    analyze_run,
    discover_local_runs,
    encode_rgb_to_png,
    get_snapshot_detail,
    inspect_sqlite_and_uploads,
    npy_file_to_png_bytes,
    replay_parsers_on_lines,
)
from tools.debug_console.server import DebugConsoleHandler


class TestDebugConsole(unittest.TestCase):
    def test_encode_rgb_and_npy_to_png(self):
        # Create a 4x3 BGR image in .npy v1.0 format
        width, height = 4, 3
        raw_pixels = bytes([255, 0, 0] * (width * height))  # Blue in BGR -> Red in RGB
        header_dict = f"{{'descr': '|u1', 'fortran_order': False, 'shape': ({height}, {width}, 3), }}"
        pad_len = (16 - ((10 + len(header_dict) + 1) % 16)) % 16
        header_bytes = (header_dict + (" " * pad_len) + "\n").encode("latin1")
        npy_bytes = b"\x93NUMPY\x01\x00" + struct.pack("<H", len(header_bytes)) + header_bytes + raw_pixels

        with tempfile.TemporaryDirectory() as tmp:
            npy_path = Path(tmp) / "001-frame.npy"
            npy_path.write_bytes(npy_bytes)
            png_bytes = npy_file_to_png_bytes(npy_path)
            self.assertTrue(png_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
            w, h = struct.unpack(">II", png_bytes[16:24])
            self.assertEqual((w, h), (width, height))

    def test_analyze_real_workspace_runs_and_sqlite(self):
        runs = discover_local_runs(limit=15)
        self.assertGreater(len(runs), 0)

        vivo_dir = REPO_ROOT / "data" / "streaming-upload" / "vivo-live"
        if vivo_dir.is_dir():
            analyzed = analyze_run(vivo_dir)
            self.assertEqual(analyzed["outcome"], "succeeded")
            self.assertGreater(len(analyzed["stages"]), 5)
            sq = analyzed["sqliteAndUploads"]
            self.assertTrue(sq["hasDatabase"])
            self.assertTrue(sq["acceptance"]["passed"])
            self.assertEqual(sq["counts"]["videos"], 1)
            self.assertEqual(sq["counts"]["comments"], 4)
            self.assertEqual(sq["counts"]["users"], 2)
            self.assertEqual(len(sq["hierarchyTree"]), 1)
            self.assertEqual(len(sq["hierarchyTree"][0]["comments"]), 4)

    def test_replay_parsers_on_comment_drawer_and_feed_lines(self):
        # Test comment drawer replay & safe click target x + 16
        drawer_lines = [
            {"text": "评论 12", "box": [310, 480, 100, 34], "source": "ocr"},
            {"text": "张三  广东 昨天", "box": [100, 560, 260, 32], "source": "ocr"},
            {"text": "5", "box": [620, 560, 30, 30], "source": "ocr"},
            {"text": "这道菜看起来太香了！", "box": [100, 602, 340, 36], "source": "ocr"},
            {"text": "展开2条回复", "box": [100, 650, 160, 30], "source": "ocr"},
            {"text": "发表评论", "box": [60, 1520, 200, 40], "source": "ocr"},
        ]
        replay = replay_parsers_on_lines(
            lines=drawer_lines,
            height=1600,
            width=720,
            label="comments",
            hot_reload=True,
        )
        self.assertEqual(replay["parsers"]["pageState"]["state"], "comment_drawer")
        cd = replay["parsers"]["parseCommentDrawer"]
        self.assertEqual(cd["status"], "ok")
        self.assertEqual(cd["commentCount"], 1)
        self.assertEqual(cd["comments"][0]["nickname"], "张三")
        # Safe click x should be 100 + 16 = 116
        self.assertEqual(cd["comments"][0]["safeProfileClick"]["x"], 116)
        self.assertTrue(any(t["kind"] == "comment_nickname_click" for t in replay["clickTargets"]))

    def test_http_server_endpoints(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), DebugConsoleHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{port}"
        try:
            with urlopen(f"{base}/api/health", timeout=5) as resp:
                health = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(health["ok"])

            with urlopen(f"{base}/", timeout=5) as resp:
                html = resp.read().decode("utf-8")
                self.assertIn("视频号任务执行全流程调试台", html)

            with urlopen(f"{base}/api/runs?source=local&limit=5", timeout=10) as resp:
                runs_resp = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(runs_resp["ok"])
                self.assertGreater(len(runs_resp["runs"]), 0)

            req = Request(
                f"{base}/api/runs/replay",
                data=json.dumps({
                    "lines": [
                        {"text": "推荐", "box": [320, 110, 60, 36], "source": "hierarchy"},
                        {"text": "关注", "box": [220, 110, 60, 36], "source": "hierarchy"},
                        {"text": "@美食达人", "box": [40, 1280, 180, 36], "source": "hierarchy"},
                    ],
                    "height": 1600,
                    "hotReload": True,
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=5) as resp:
                rep_data = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(rep_data["ok"])
                self.assertEqual(rep_data["replay"]["parsers"]["pageState"]["state"], "channels_feed")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
