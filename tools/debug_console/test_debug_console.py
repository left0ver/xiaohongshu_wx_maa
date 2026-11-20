import json
import struct
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from unittest import mock

import tools.debug_console.engine as engine
from tools.debug_console.engine import (
    REPO_ROOT,
    analyze_run,
    classify_scope_origin,
    discover_local_runs,
    encode_rgb_to_png,
    find_snapshot_frame,
    get_snapshot_detail,
    inspect_sqlite_and_uploads,
    list_device_runs,
    npy_file_to_png_bytes,
    replay_parsers_on_lines,
    sync_device_run,
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

    def test_jpeg_frame_serving_and_sync_preserves_cached_frames(self):
        # 1. Verify .jpg snapshot frames are discovered and served as image/jpeg.
        with tempfile.TemporaryDirectory(dir=REPO_ROOT / "data") as tmp:
            run_dir = Path(tmp) / "run-jpg"
            run_dir.mkdir()
            (run_dir / "001-feed.json").write_text(
                json.dumps({"label": "feed", "width": 720, "height": 1560, "lines": []}),
                encoding="utf-8",
            )
            fake_jpg = b"\xff\xd8\xff\xe0\x00\x10JFIFfakejpegbytes\xff\xd9"
            (run_dir / "001-feed.jpg").write_bytes(fake_jpg)

            self.assertEqual(find_snapshot_frame(run_dir, "001-feed"), (run_dir / "001-feed.jpg").resolve())
            snap = get_snapshot_detail(run_dir, "001-feed.json")
            self.assertTrue(snap["hasFrame"])
            self.assertEqual(snap["frameExt"], ".jpg")

            rel = str(run_dir.relative_to(REPO_ROOT))
            server = ThreadingHTTPServer(("127.0.0.1", 0), DebugConsoleHandler)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{port}/api/runs/frame?runRef=local:{rel}&filename=001-feed.json"
                with urlopen(url, timeout=5) as resp:
                    self.assertEqual(resp.headers.get("Content-Type"), "image/jpeg")
                    self.assertEqual(resp.read(), fake_jpg)
            finally:
                server.shutdown()
                server.server_close()

        # 2. Verify re-syncing after phone pruned old images preserves cached local images.
        with tempfile.TemporaryDirectory() as cache_tmp:
            cache_root = Path(cache_tmp)
            scope = "/data/local/tmp/wechat-channels-data"
            scope_slug = scope.strip("/").replace("/", "__")
            cached_run = cache_root / "SERIAL1" / scope_slug / "runs" / "run-keep-img"
            cached_run.mkdir(parents=True)
            (cached_run / "001-feed.json").write_text('{"label":"feed","version":1}', encoding="utf-8")
            (cached_run / "001-feed.jpg").write_bytes(b"\xff\xd8cached-image\xff\xd9")

            def fake_run_adb(serial, args, timeout=15.0, binary=False):
                if args[0] == "pull" and args[1].endswith("/runs/run-keep-img"):
                    dest = Path(args[2])
                    dest.mkdir(parents=True, exist_ok=True)
                    # Phone only has updated JSON; old .jpg was pruned on phone.
                    (dest / "001-feed.json").write_text('{"label":"feed","version":2}', encoding="utf-8")
                    return ""
                return ""

            with mock.patch.object(engine, "CACHE_ROOT", cache_root), mock.patch.object(
                engine, "run_adb", side_effect=fake_run_adb
            ):
                synced = sync_device_run("SERIAL1", scope, "run-keep-img", include_sqlite=False)
                self.assertEqual(synced, cached_run)
                # Updated JSON merged in, while previously cached .jpg is still preserved!
                self.assertEqual(json.loads((synced / "001-feed.json").read_text(encoding="utf-8"))["version"], 2)
                self.assertEqual((synced / "001-feed.jpg").read_bytes(), b"\xff\xd8cached-image\xff\xd9")

    def test_origin_classification_and_local_dedup_real_timestamp(self):
        self.assertEqual(classify_scope_origin("/data/local/tmp/wechat-channels-data/remote/abc123"), "remote")
        self.assertEqual(classify_scope_origin("data/debug-console-cache/DEV/data__local__tmp__wechat-channels-data__remote__abc/runs/r1"), "remote")
        self.assertEqual(classify_scope_origin("/data/local/tmp/wechat-channels-data"), "manual")
        self.assertEqual(classify_scope_origin("/data/local/tmp/wechat-channels-dev/data"), "cli")

        # Verify discover_local_runs deduplicates by runId and sorts by observedAtUnixMs (not folder mtime).
        with tempfile.TemporaryDirectory() as tmp_root:
            tmp_path = Path(tmp_root)
            data_dir = tmp_path / "data"
            old_run_a = data_dir / "verify" / "case1" / "native"
            old_run_b = data_dir / "debug-console-cache" / "DEV" / "data__local__tmp__wechat-channels-data__remote__abc" / "runs" / "dup-run-1"
            new_run = data_dir / "debug-console-cache" / "DEV" / "data__local__tmp__wechat-channels-dev__data" / "runs" / "newer-run-2"
            for d in (old_run_a, old_run_b, new_run):
                d.mkdir(parents=True)

            # Write newer-run-2 first (older folder mtime), with newer real observedAtUnixMs
            (new_run / "timings.jsonl").write_text(json.dumps({"runId": "newer-run-2"}) + "\n", encoding="utf-8")
            (new_run / "001-feed.json").write_text(
                json.dumps({"lines": [{"text": "推荐", "observedAtUnixMs": 1795150000000}]}), encoding="utf-8"
            )

            # Write dup-run-1 in two locations with older real observedAtUnixMs; old_run_b has a frame so it should win
            for d in (old_run_a, old_run_b):
                (d / "timings.jsonl").write_text(json.dumps({"runId": "dup-run-1"}) + "\n", encoding="utf-8")
                (d / "001-feed.json").write_text(
                    json.dumps({"lines": [{"text": "推荐", "observedAtUnixMs": 1794000000000}]}), encoding="utf-8"
                )
            (old_run_b / "001-feed.jpg").write_bytes(b"\xff\xd8fake\xff\xd9")

            with mock.patch.object(engine, "REPO_ROOT", tmp_path):
                runs = discover_local_runs(limit=10)
            self.assertEqual(len(runs), 2)
            self.assertEqual([r["runId"] for r in runs], ["newer-run-2", "dup-run-1"])
            self.assertEqual(runs[0]["origin"], "cli")
            self.assertEqual(runs[1]["origin"], "remote")
            self.assertEqual(runs[1]["frameCount"], 1)

        # Verify list_device_runs aggregates scopes and sorts globally newest-first.
        fake_adb_out = "\n".join([
            "RUN|/data/local/tmp/wechat-channels-data|run-manual|1794000000|3|1|,link-task.json|,001-link-video.json|{\"success\":true}|",
            "RUN|/data/local/tmp/wechat-channels-data/remote/abc123|run-remote|1795150000|5|2|,remote-result.json|,001-feed.json|{\"success\":true}|",
        ])
        with mock.patch.object(engine, "run_adb", return_value=fake_adb_out):
            dev_runs = list_device_runs("DEV1", "all", limit=20)
        self.assertEqual([r["runId"] for r in dev_runs], ["run-remote", "run-manual"])
        self.assertEqual(dev_runs[0]["origin"], "remote")
        self.assertEqual(dev_runs[1]["origin"], "manual")


if __name__ == "__main__":
    unittest.main()
