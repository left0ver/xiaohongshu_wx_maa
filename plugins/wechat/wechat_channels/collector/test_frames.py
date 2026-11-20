import os
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    import numpy as np
except ImportError:  # pragma: no cover - host python without numpy
    np = None

from frames import (
    AsyncFrameWriter,
    DEFAULT_MAX_FRAME_BYTES,
    DEFAULT_MAX_FRAME_RUNS,
    encode_bgr_to_png,
    materialize_frame_file,
    prune_old_run_frames,
    resolve_dump_frames,
    save_bgr_frame_png,
)


class TestFrames(unittest.TestCase):
    def test_resolve_dump_frames_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "remote" / ("a" * 64) / "runs" / "r1"
            run_dir.mkdir(parents=True)

            # Default off when no env or toggle file is set.
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertFalse(resolve_dump_frames(run_dir))

            # Debug APK defaults ON, Release APK defaults OFF.
            with mock.patch.dict(os.environ, {"PI_DEBUG_BUILD": "1"}, clear=True):
                self.assertTrue(resolve_dump_frames(run_dir))
            with mock.patch.dict(os.environ, {"PI_DEBUG_BUILD": "0"}, clear=True):
                self.assertFalse(resolve_dump_frames(run_dir))

            # Toggle file overrides host build default.
            toggle = root / ".dump_frames"
            toggle.write_text("1\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"PI_DEBUG_BUILD": "0", "WECHAT_DATA_DIR": str(root)}, clear=True):
                self.assertTrue(resolve_dump_frames(run_dir))

            toggle.write_text("0\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"PI_DEBUG_BUILD": "1", "WECHAT_DATA_DIR": str(root)}, clear=True):
                self.assertFalse(resolve_dump_frames(run_dir))

            # Explicit env var overrides toggle file and build mode.
            with mock.patch.dict(
                os.environ,
                {"WECHAT_DUMP_FRAMES": "1", "PI_DEBUG_BUILD": "0", "WECHAT_DATA_DIR": str(root)},
                clear=True,
            ):
                self.assertTrue(resolve_dump_frames(run_dir))
            with mock.patch.dict(
                os.environ,
                {"WECHAT_DUMP_FRAMES": "0", "PI_DEBUG_BUILD": "1", "WECHAT_DATA_DIR": str(root)},
                clear=True,
            ):
                self.assertFalse(resolve_dump_frames(run_dir))

    @unittest.skipIf(np is None, "numpy not installed")
    def test_encode_bgr_to_png_scales_and_produces_valid_png(self):
        # 720x1440 BGR image -> scaled to 540x1080 PNG
        frame = np.zeros((1440, 720, 3), dtype=np.uint8)
        frame[:, :, 2] = 255  # Red in BGR
        png = encode_bgr_to_png(frame, max_width=540)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        w, h = struct.unpack(">II", png[16:24])
        self.assertEqual((w, h), (540, 1080))
        self.assertLess(len(png), 40 * 1024)

    @unittest.skipIf(np is None, "numpy not installed")
    def test_async_frame_writer_flushes_and_never_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            writer = AsyncFrameWriter(maxsize=4, max_width=120)
            frame = np.full((200, 100, 3), 128, dtype=np.uint8)
            for idx in range(3):
                self.assertTrue(writer.submit_array(frame, run_dir / f"{idx:03d}-step.png"))
            writer.close(timeout=2.0)
            self.assertEqual(writer.errors, 0)
            self.assertEqual(writer.written, 3)
            for idx in range(3):
                p = run_dir / f"{idx:03d}-step.png"
                self.assertTrue(p.is_file())
                self.assertTrue(p.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

    def test_materialize_frame_file_consume_and_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            pending = run_dir / ".pending-hierarchy.jpg"
            pending.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
            step1 = materialize_frame_file(pending, run_dir / "001-feed.jpg", consume_source=True)
            self.assertEqual(step1, run_dir / "001-feed.jpg")
            self.assertFalse(pending.exists())
            self.assertTrue(step1.is_file())

            # Reusing cached controls preserves step1 and creates step2.
            step2 = materialize_frame_file(step1, run_dir / "002-feed-reused.jpg", consume_source=False)
            self.assertEqual(step2, run_dir / "002-feed-reused.jpg")
            self.assertTrue(step1.is_file())
            self.assertTrue(step2.is_file())
            self.assertEqual(step1.read_bytes(), step2.read_bytes())

    def test_prune_old_run_frames_by_count_drops_oldest_half_and_keeps_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp) / "runs"
            runs_dir.mkdir()
            base_mtime = time.time() - 1000
            # Create 12 completed runs + 1 active run; limit max_runs=10.
            for idx in range(12):
                rdir = runs_dir / f"run-{idx:02d}"
                rdir.mkdir()
                (rdir / "001-feed.json").write_text('{"label":"feed"}', encoding="utf-8")
                (rdir / "001-feed.jpg").write_bytes(b"\xff\xd8" + b"x" * 1000)
                os.utime(rdir, (base_mtime + idx, base_mtime + idx))

            active_dir = runs_dir / "run-active"
            active_dir.mkdir()
            (active_dir / "001-feed.jpg").write_bytes(b"\xff\xd8" + b"a" * 1000)

            report = prune_old_run_frames(
                runs_dir,
                max_runs=10,
                max_bytes=10 * 1024 * 1024,
                current_run_dir=active_dir,
            )
            # 12 > 10 -> drops oldest 50% (6 runs: run-00 .. run-05).
            self.assertEqual(report["prunedRuns"], 6)
            self.assertEqual(report["deletedFiles"], 6)
            for idx in range(6):
                rdir = runs_dir / f"run-{idx:02d}"
                self.assertFalse((rdir / "001-feed.jpg").exists())
                self.assertTrue((rdir / "001-feed.json").is_file())
            for idx in range(6, 12):
                rdir = runs_dir / f"run-{idx:02d}"
                self.assertTrue((rdir / "001-feed.jpg").is_file())
            # Current active run is never touched.
            self.assertTrue((active_dir / "001-feed.jpg").is_file())

    def test_prune_old_run_frames_by_bytes_drops_oldest_half(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp) / "runs"
            runs_dir.mkdir()
            base_mtime = time.time() - 1000
            # 6 runs x 100KB = 600KB > 500KB limit -> drops oldest 50% (3 runs), leaving 300KB.
            for idx in range(6):
                rdir = runs_dir / f"run-{idx:02d}"
                rdir.mkdir()
                (rdir / "001-feed.json").write_text("{}", encoding="utf-8")
                (rdir / "001-feed.jpg").write_bytes(b"j" * (100 * 1024))
                os.utime(rdir, (base_mtime + idx, base_mtime + idx))

            report = prune_old_run_frames(
                runs_dir,
                max_runs=200,
                max_bytes=500 * 1024,
            )
            self.assertEqual(report["prunedRuns"], 3)
            self.assertEqual(report["freedBytes"], 300 * 1024)
            for idx in range(3):
                self.assertFalse((runs_dir / f"run-{idx:02d}" / "001-feed.jpg").exists())
                self.assertTrue((runs_dir / f"run-{idx:02d}" / "001-feed.json").is_file())
            for idx in range(3, 6):
                self.assertTrue((runs_dir / f"run-{idx:02d}" / "001-feed.jpg").is_file())

    def test_default_limits_match_spec(self):
        self.assertEqual(DEFAULT_MAX_FRAME_RUNS, 200)
        self.assertEqual(DEFAULT_MAX_FRAME_BYTES, 300 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
