"""Debug screenshot configuration, compressed async frame persistence, and retention pruning."""

import os
import queue
import shutil
import struct
import threading
import zlib
from pathlib import Path

FRAME_EXTENSIONS = (".jpg", ".jpeg", ".webp", ".png", ".npy")
DEFAULT_MAX_FRAME_RUNS = 200
DEFAULT_MAX_FRAME_BYTES = 300 * 1024 * 1024  # 300 MB
DEFAULT_MAX_QUEUE_SIZE = 4
TOGGLE_FILENAME = ".dump_frames"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def _parse_bool_flag(raw):
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return None


def _candidate_toggle_files(evidence_dir=None):
    seen = set()
    candidates = []

    def add_root(path):
        if path is None:
            return
        try:
            resolved = Path(path)
        except TypeError:
            return
        toggle = resolved / TOGGLE_FILENAME
        key = str(toggle)
        if key not in seen:
            seen.add(key)
            candidates.append(toggle)

    if evidence_dir is not None:
        current = Path(evidence_dir)
        # Walk up at most 4 levels (runs/<id> -> scope -> remote -> WECHAT_DATA_DIR).
        for parent in [current, *list(current.parents)[:4]]:
            add_root(parent)

    env_root = os.environ.get("WECHAT_DATA_DIR")
    if env_root:
        add_root(Path(env_root))
    return candidates


def resolve_dump_frames(evidence_dir=None):
    """Decide whether to record real debug screenshots for this run.

    Precedence:
    1. Explicit environment variable ``WECHAT_DUMP_FRAMES`` (1/0).
    2. On-device toggle file ``.dump_frames`` in data root / scope dir (1/0).
    3. Host build mode ``PI_DEBUG_BUILD`` (Debug APK defaults to ON, Release to OFF).
    4. Default: False.
    """
    explicit = _parse_bool_flag(os.environ.get("WECHAT_DUMP_FRAMES"))
    if explicit is not None:
        return explicit

    for toggle_path in _candidate_toggle_files(evidence_dir):
        try:
            if toggle_path.is_file():
                parsed = _parse_bool_flag(toggle_path.read_text(encoding="utf-8", errors="replace"))
                if parsed is not None:
                    return parsed
        except OSError:
            continue

    host_debug = _parse_bool_flag(os.environ.get("PI_DEBUG_BUILD"))
    if host_debug is not None:
        return host_debug

    return False


def encode_bgr_to_png(frame, max_width=540, compression_level=1):
    """Fast, dependency-free BGR(A) NumPy array -> compressed RGB PNG bytes.

    Uses NumPy nearest-neighbor downscaling and vectorized scanline prefixing
    so it runs in ~15-30ms on a background thread without Pillow or OpenCV.
    """
    import numpy as np

    arr = np.asarray(frame)
    if arr.ndim != 3 or arr.shape[2] not in (3, 4):
        raise ValueError(f"Unsupported frame shape: {arr.shape}")
    height, width = arr.shape[0], arr.shape[1]
    if height <= 0 or width <= 0:
        raise ValueError(f"Empty frame shape: {arr.shape}")

    if max_width and width > max_width:
        target_w = int(max_width)
        target_h = max(1, round(height * target_w / width))
        ys = np.linspace(0, height - 1, target_h).astype(np.intp)
        xs = np.linspace(0, width - 1, target_w).astype(np.intp)
        arr = arr[np.ix_(ys, xs)]
        height, width = target_h, target_w

    # BGR -> RGB uint8 contiguous array.
    rgb = np.ascontiguousarray(arr[:, :, 2::-1], dtype=np.uint8)
    # Prefix every scanline with PNG filter byte 0 using NumPy (no Python row loop).
    rows = np.empty((height, 1 + width * 3), dtype=np.uint8)
    rows[:, 0] = 0
    rows[:, 1:] = rgb.reshape(height, width * 3)

    def chunk(chunk_type, payload):
        crc = zlib.crc32(payload, zlib.crc32(chunk_type)) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    compressed = zlib.compress(rows.tobytes(), level=compression_level)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )


def save_bgr_frame_png(frame, target_path, max_width=540):
    """Atomically write a compressed PNG frame to ``target_path``."""
    target = Path(target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    data = encode_bgr_to_png(frame, max_width=max_width)
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return target


def materialize_frame_file(src_path, dst_path, *, consume_source=False):
    """Place a ready-made JPEG/PNG frame at ``dst_path`` without duplicating bytes when possible."""
    src = Path(src_path)
    dst = Path(dst_path)
    if not src.is_file() or src.stat().st_size <= 0:
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src == dst:
        return dst
    if consume_source:
        os.replace(src, dst)
        return dst
    try:
        if dst.exists():
            dst.unlink()
        os.link(src, dst)
    except OSError:
        tmp = dst.with_name(dst.name + ".tmp")
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    return dst


class AsyncFrameWriter:
    """Single-worker bounded background queue for Path B (OCR memory frame) encoding.

    Never blocks the collector main thread: when the device is heavily stalled and
    the queue reaches ``maxsize``, extra debug frames are silently dropped.
    """

    def __init__(self, maxsize=DEFAULT_MAX_QUEUE_SIZE, max_width=540):
        self._queue = queue.Queue(maxsize=maxsize)
        self._max_width = max_width
        self._closed = False
        self.dropped = 0
        self.written = 0
        self.errors = 0
        self._thread = threading.Thread(target=self._worker, name="wechat-frame-writer", daemon=True)
        self._thread.start()

    def submit_array(self, frame, target_path):
        if self._closed or frame is None:
            return False
        try:
            self._queue.put_nowait((frame, Path(target_path)))
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def _worker(self):
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                frame, target_path = item
                save_bgr_frame_png(frame, target_path, max_width=self._max_width)
                self.written += 1
            except Exception:
                self.errors += 1
            finally:
                self._queue.task_done()

    def close(self, timeout=0.5):
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put(None, timeout=0.05)
        except queue.Full:
            return
        self._thread.join(timeout=timeout)


def _list_run_frame_files(run_dir):
    files = []
    total_bytes = 0
    try:
        for entry in run_dir.iterdir():
            if not entry.is_file():
                continue
            name = entry.name.lower()
            is_frame = name.endswith(FRAME_EXTENSIONS) or any(
                name.endswith(ext + ".tmp") for ext in FRAME_EXTENSIONS
            )
            if not is_frame:
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            files.append((entry, size))
            total_bytes += size
    except OSError:
        return [], 0
    return files, total_bytes


def prune_old_run_frames(
    runs_dir,
    *,
    max_runs=DEFAULT_MAX_FRAME_RUNS,
    max_bytes=DEFAULT_MAX_FRAME_BYTES,
    current_run_dir=None,
):
    """Keep at most ``max_runs`` runs with frames and ``max_bytes`` total frame size.

    When either threshold is exceeded, deletes frame image files from the oldest
    50% of runs with frames (and continues deleting older runs if total bytes
    still exceed ``max_bytes``). JSON snapshots and ``timings.jsonl`` are always
    preserved.
    """
    runs_path = Path(runs_dir)
    if not runs_path.is_dir():
        return {"prunedRuns": 0, "deletedFiles": 0, "freedBytes": 0}

    current_resolved = Path(current_run_dir).resolve() if current_run_dir else None
    runs_with_frames = []
    total_bytes = 0

    try:
        children = list(runs_path.iterdir())
    except OSError:
        return {"prunedRuns": 0, "deletedFiles": 0, "freedBytes": 0}

    for child in children:
        if not child.is_dir():
            continue
        try:
            if current_resolved is not None and child.resolve() == current_resolved:
                continue
        except OSError:
            continue
        frame_files, run_bytes = _list_run_frame_files(child)
        if not frame_files:
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            mtime = 0.0
        runs_with_frames.append((mtime, child.name, child, frame_files, run_bytes))
        total_bytes += run_bytes

    if len(runs_with_frames) <= max_runs and total_bytes <= max_bytes:
        return {"prunedRuns": 0, "deletedFiles": 0, "freedBytes": 0}

    # Oldest first.
    runs_with_frames.sort(key=lambda item: (item[0], item[1]))
    drop_target = max(1, len(runs_with_frames) // 2)

    pruned_runs = 0
    deleted_files = 0
    freed_bytes = 0
    remaining_runs = len(runs_with_frames)
    remaining_bytes = total_bytes

    for idx, (_mtime, _name, _dir, frame_files, run_bytes) in enumerate(runs_with_frames):
        must_drop = idx < drop_target or remaining_runs > max_runs or remaining_bytes > max_bytes
        if not must_drop:
            break
        for fpath, fsize in frame_files:
            try:
                fpath.unlink(missing_ok=True)
                deleted_files += 1
                freed_bytes += fsize
            except OSError:
                pass
        pruned_runs += 1
        remaining_runs -= 1
        remaining_bytes = max(0, remaining_bytes - run_bytes)

    return {
        "prunedRuns": pruned_runs,
        "deletedFiles": deleted_files,
        "freedBytes": freed_bytes,
    }


def schedule_frame_prune(
    evidence_dir,
    *,
    max_runs=DEFAULT_MAX_FRAME_RUNS,
    max_bytes=DEFAULT_MAX_FRAME_BYTES,
):
    """Run ``prune_old_run_frames`` in a daemon background thread without delaying task start."""
    run_dir = Path(evidence_dir)
    runs_dir = run_dir.parent
    if runs_dir.name != "runs" or not runs_dir.is_dir():
        return None

    def _run():
        try:
            prune_old_run_frames(
                runs_dir,
                max_runs=max_runs,
                max_bytes=max_bytes,
                current_run_dir=run_dir,
            )
        except Exception:
            pass

    thread = threading.Thread(target=_run, name="wechat-frame-prune", daemon=True)
    thread.start()
    return thread
