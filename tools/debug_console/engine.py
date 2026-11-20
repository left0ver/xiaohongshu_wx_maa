"""Core engine for the MAA Mobile Automation & Collector Web Debug Console.

Supports:
1. Discovering runs from local workspace (`data/`) and connected ADB devices
   (`/data/local/tmp/wechat-channels-data`, `/data/local/tmp/wechat-channels-dev`, etc.).
2. Syncing/pulling run directories and SQLite databases from ADB devices.
3. Analyzing a run's full execution lifecycle:
   - `timings.jsonl` events & phase durations
   - Snapshot sequence (`001-<label>.json`, `.npy` / `.png` frames)
   - High-level workflow stage waterfall & bottleneck detection
   - Outcome, errors, and final result payloads
4. Single-step parser replay with hot-reload of `plugins/wechat/wechat_channels/collector/*.py`:
   - Re-runs `classify_page_state`, `classify_mode_page`, `detect_abnormal_state`,
     `field_evidence`, `parse_video`, `parse_author`, `parse_basic_info`,
     `parse_comment_drawer`, `parse_search_video_cards`, `reading_rows`, `work_region`,
     `author_in_feed`, `feed_video_preview`, `comment_control_in_feed`.
   - Computes semantic visual overlay boxes and click targets (e.g. leftmost nickname `x + 16`).
5. Inspecting `videos.sqlite3` (`video_drafts`, `comment_drafts`, `user_drafts`, `metadata`),
   running `acceptance.sql` & foreign-key integrity checks (`VIDEO -> COMMENT -> USER`),
   and inspecting upload batches (`uploads/*-request.json`, `*-ack.json`).
6. Live ADB screen/hierarchy probing (`screencap` + `ui-dump.dex`), code push, and task execution.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
import uuid
import zlib
from contextlib import closing
from pathlib import Path
from typing import Any

from tools.debug_console.narrative import (
    build_timeline,
    ACTION_TO_LINK_TASK,
    deduplicate_visual_lines,
    enrich_run_with_chinese_narrative,
    get_task_type_cn,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WECHAT_COLLECTOR_DIR = REPO_ROOT / "plugins" / "wechat" / "wechat_channels" / "collector"
XHS_AGENT_DIR = REPO_ROOT / "plugins" / "xiaohongshu" / "xiaohongshu" / "agent"
CACHE_ROOT = REPO_ROOT / "data" / "debug-console-cache"

SNAPSHOT_FILE_RE = re.compile(r"^(\d{3})-(.+)\.json$")
FRAME_EXTENSIONS = (".jpg", ".jpeg", ".webp", ".png", ".npy")


def find_snapshot_frame(run_dir: Path, stem: str) -> Path | None:
    """Locate a captured frame image beside a snapshot JSON, preferring compressed formats."""
    base = Path(run_dir)
    for ext in FRAME_EXTENSIONS:
        candidate = (base / f"{stem}{ext}").resolve()
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None

# Known stage display names for WeChat Channels workflows
STAGE_LABELS = {
    "ensure_feed": "入口导航 (进入视频号推荐流)",
    "collect_author_nickname": "识别首屏作者昵称",
    "collect_description_time_location": "展开作品详情 (文案/时间/地点)",
    "copy_share_link": "提取分享短链",
    "collect_author_profile": "采集作者主页与基础信息",
    "persist_video_draft": "保存本地 VIDEO 草稿",
    "collect_video_total": "单视频采集汇总",
    "collect_visible_comments": "评论区翻页与二级回复采集",
    "collect_commenter_profile": "访问评论者主页/资料卡",
    "persist_comment_drafts": "保存本地 COMMENT/USER 草稿",
    "run_mode": "运行模式调度",
    "interact_work_and_comments": "作品与评论互动执行",
    "discoverySending": "流式上报发送 (WebSocket/Bridge)",
    "discoveryAcknowledged": "流式上报服务端 ACK",
}

PAGE_STATE_LABELS = {
    "channels_feed": "视频号播放流 (channels_feed)",
    "comment_drawer": "评论半屏抽屉 (comment_drawer)",
    "user_profile": "用户主页 (user_profile)",
    "user_card_overlay": "评论者资料卡半屏 (user_card_overlay)",
    "basic_info_overlay": "更多信息/基础信息页 (basic_info_overlay)",
    "share_overlay": "分享菜单弹窗 (share_overlay)",
    "menu_overlay": "更多菜单弹窗 (menu_overlay)",
    "composer_overlay": "输入框唤醒态 (composer_overlay)",
    "channels_message_center": "视频号消息中心 (channels_message_center)",
    "personal_page": "视频号个人页 (personal_page)",
    "search_results": "搜索结果页 (search_results)",
    "unknown": "未分类/过渡页 (unknown)",
}


def _ensure_collector_imports(hot_reload: bool = False) -> dict[str, Any]:
    """Import (or hot-reload) WeChat collector modules with lightweight stubs for optional network packages."""
    collector_str = str(WECHAT_COLLECTOR_DIR)
    if collector_str not in sys.path:
        sys.path.insert(0, collector_str)

    stub_specs = [
        ("dotenv", {"load_dotenv": lambda *a, **k: False}),
        ("certifi", {"where": lambda: ""}),
        ("websockets", {}),
        ("websockets.sync", {}),
        ("websockets.sync.client", {"connect": lambda *a, **k: None}),
    ]
    for mod_name, attrs in stub_specs:
        if mod_name not in sys.modules:
            try:
                __import__(mod_name)
            except ImportError:
                stub = types.ModuleType(mod_name)
                stub.__dict__.update(attrs)
                sys.modules[mod_name] = stub

    module_names = [
        "commands",
        "foreground",
        "layout",
        "readiness",
        "hierarchy",
        "collection",
        "comments",
        "interactions",
        "upload",
        "publisher",
        "links",
        "modes",
    ]
    loaded = {}
    for name in module_names:
        if name in sys.modules:
            if hot_reload:
                loaded[name] = importlib.reload(sys.modules[name])
            else:
                loaded[name] = sys.modules[name]
        else:
            loaded[name] = importlib.import_module(name)
    return loaded


# ---------------------------------------------------------------------------
# Pure-Python NPY & Raw Screencap -> PNG Encoder (zero external dependencies)
# ---------------------------------------------------------------------------

def encode_rgb_to_png(width: int, height: int, rgb_bytes: bytes, bgr: bool = False) -> bytes:
    """Encode raw 3-channel (RGB or BGR) or 4-channel (RGBA) uint8 bytes to PNG bytes."""
    stride = len(rgb_bytes) // (width * height) if (width > 0 and height > 0) else 0
    if stride not in (3, 4) or len(rgb_bytes) < width * height * stride:
        raise ValueError(f"Invalid image buffer size {len(rgb_bytes)} for {width}x{height}")

    raw_rows = bytearray()
    row_bytes = width * stride
    for y in range(height):
        raw_rows.append(0)  # PNG filter type 0 (None)
        start = y * row_bytes
        row = rgb_bytes[start : start + row_bytes]
        if bgr and stride == 3:
            # Swap B and R channels
            swapped = bytearray(row_bytes)
            swapped[0::3] = row[2::3]
            swapped[1::3] = row[1::3]
            swapped[2::3] = row[0::3]
            raw_rows.extend(swapped)
        elif bgr and stride == 4:
            swapped = bytearray(row_bytes)
            swapped[0::4] = row[2::4]
            swapped[1::4] = row[1::4]
            swapped[2::4] = row[0::4]
            swapped[3::4] = row[3::4]
            raw_rows.extend(swapped)
        else:
            raw_rows.extend(row)

    color_type = 2 if stride == 3 else 6  # 2 = Truecolor RGB, 6 = RGBA

    def make_chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(chunk_type)
        crc = zlib.crc32(data, crc) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    compressed = zlib.compress(bytes(raw_rows), level=6)
    return (
        b"\x89PNG\r\n\x1a\n"
        + make_chunk(b"IHDR", ihdr)
        + make_chunk(b"IDAT", compressed)
        + make_chunk(b"IEND", b"")
    )


def npy_file_to_png_bytes(npy_path: Path) -> bytes:
    """Read a NumPy .npy uint8 image file (H, W, 3) and return PNG bytes without requiring numpy."""
    data = npy_path.read_bytes()
    if data[:6] != b"\x93NUMPY":
        raise ValueError("Not a valid .npy file")
    major, minor = data[6], data[7]
    if major == 1:
        header_len = struct.unpack_from("<H", data, 8)[0]
        offset = 10 + header_len
        header_str = data[10:offset].decode("latin1").strip()
    else:
        header_len = struct.unpack_from("<I", data, 8)[0]
        offset = 12 + header_len
        header_str = data[12:offset].decode("latin1").strip()

    header = ast.literal_eval(header_str)
    shape = header.get("shape", ())
    if len(shape) != 3 or shape[2] not in (3, 4):
        raise ValueError(f"Unsupported .npy image shape: {shape}")
    height, width, _ = shape
    raw = data[offset:]
    # MaaFramework / PhoneController stores BGR arrays
    return encode_rgb_to_png(width, height, raw, bgr=True)


def android_screencap_to_png_bytes(raw: bytes) -> tuple[bytes, int, int]:
    """Convert raw Android `screencap` output (or direct PNG) to (png_bytes, width, height)."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        w, h = struct.unpack(">II", raw[16:24])
        return raw, w, h
    width, height, pixel_format = struct.unpack_from("<III", raw)
    if pixel_format not in (1, 2) or width <= 0 or height <= 0:
        raise ValueError("Unsupported Android screencap format")
    header = len(raw) - width * height * 4
    if header not in (12, 16):
        raise ValueError("Unexpected Android screencap header size")
    rgba = raw[header : header + width * height * 4]
    return encode_rgb_to_png(width, height, rgba, bgr=False), width, height


# ---------------------------------------------------------------------------
# ADB Device Discovery, Sync & Live Probe
# ---------------------------------------------------------------------------

def run_adb(serial: str | None, args: list[str], timeout: float = 15.0, binary: bool = False) -> bytes | str:
    cmd = ["adb"]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ADB command failed ({' '.join(cmd)}): {stderr or proc.returncode}")
    return proc.stdout if binary else proc.stdout.decode("utf-8", errors="replace")


ORIGIN_META: dict[str, dict[str, str]] = {
    "remote": {
        "label": "📡 后台下发",
        "optionTitle": "📡 后台下发 · 服务端远程",
        "desc": "服务端 / 管理后台远程下发给手机 App 自动执行的任务（已自动汇总手机上全部远端授权目录）",
    },
    "manual": {
        "label": "👆 App手动",
        "optionTitle": "👆 App手动 · 手机界面点击",
        "desc": "直接在手机 App 界面中手动点击「开始」运行的任务（wechat-channels-data）",
    },
    "cli": {
        "label": "🛠️ 开发调试",
        "optionTitle": "🛠️ 开发调试 · 命令行/调试台",
        "desc": "通过电脑命令行或本页右上角「下发任务」经 USB 直接运行的调试任务（wechat-channels-dev/data）",
    },
}


def classify_scope_origin(path_str: str) -> str:
    """Map a phone scope path or local cache relative path to one of: remote, manual, cli, other."""
    normalized = path_str.replace("\\", "/")
    if "/remote/" in normalized or "__remote__" in normalized:
        return "remote"
    if "wechat-channels-dev" in normalized:
        return "cli"
    if "wechat-channels-data" in normalized:
        return "manual"
    return "other"


def _origin_info(origin: str) -> dict[str, str]:
    return ORIGIN_META.get(
        origin,
        {
            "label": "📁 本地留档",
            "optionTitle": "📁 本地留档",
            "desc": "电脑本地保存的历史调试或测试归档记录",
        },
    )


def _extract_real_run_timestamp(rdir: Path, snaps: list[str], fallback_mtime: int) -> int:
    """Read the real execution unix timestamp (seconds) from the latest snapshot JSON when available."""
    rx = re.compile(rb'"observedAtUnixMs"\s*:\s*(\d{12,14})')
    for snap_name in reversed(snaps):
        snap_path = rdir / snap_name
        try:
            with snap_path.open("rb") as fh:
                head = fh.read(8192)
            m = rx.search(head)
            if not m:
                raw = snap_path.read_bytes()
                m = rx.search(raw)
            if m:
                return int(int(m.group(1)) // 1000)
        except OSError:
            continue
    return fallback_mtime


def list_adb_devices() -> list[dict[str, Any]]:
    """List connected ADB devices with screen geometry, foreground app, and available data scopes."""
    try:
        out = run_adb(None, ["devices", "-l"], timeout=5.0)
    except Exception as exc:
        return [{"error": str(exc)}]

    devices = []
    for line in str(out).splitlines()[1:]:
        line = line.strip()
        if not line or "offline" in line:
            continue
        parts = line.split()
        if len(parts) < 2 or parts[1] != "device":
            continue
        serial = parts[0]
        meta = {}
        for token in parts[2:]:
            if ":" in token:
                k, v = token.split(":", 1)
                meta[k] = v

        info: dict[str, Any] = {
            "serial": serial,
            "model": meta.get("model", serial),
            "product": meta.get("product", ""),
            "device": meta.get("device", ""),
            "width": 720,
            "physicalWidth": None,
            "physicalHeight": None,
            "logicalHeight": 1600,
            "foregroundPackage": None,
            "foregroundActivity": None,
            "installedApps": [],
            "scopes": [],
            "originCounts": {"all": 0, "remote": 0, "manual": 0, "cli": 0},
        }
        try:
            probe_script = (
                "wm size 2>/dev/null; "
                "echo '---FG---'; "
                "dumpsys window displays 2>/dev/null | grep -E 'mCurrentFocus|mFocusedApp' | head -n 4; "
                "echo '---APPS---'; "
                "for pkg in top.natsuu.mta.wechat_channels top.natsuu.mta.wechat_channels.integration "
                "top.natsuu.mta.wechat_collects top.natsuu.mta.xiaohongshu; do "
                "  ver=$(dumpsys package $pkg 2>/dev/null | grep versionName | head -n 1 | tr -d ' '); "
                "  [ -n \"$ver\" ] || continue; "
                "  uid=$(dumpsys package $pkg 2>/dev/null | grep userId= | head -n 1 | sed -E 's/.*userId=([0-9]+).*/\\1/'); "
                "  dbg=$(dumpsys package $pkg 2>/dev/null | grep -o 'DEBUGGABLE' | head -n 1); "
                "  rscopes=$(run-as $pkg ls files/wechat-remote 2>/dev/null | tr '\\n' ','); "
                "  echo \"APP|$pkg|$ver|${dbg:-RELEASE}|$uid|$rscopes\"; "
                "done; "
                "echo '---SCOPES---'; "
                "for d in /data/local/tmp/wechat-channels-data "
                "/data/local/tmp/wechat-channels-data/remote/* "
                "/data/local/tmp/wechat-channels-dev/data; do "
                "  if [ -d \"$d/runs\" ] || [ -f \"$d/videos.sqlite3\" ] || [ -f \"$d/discovery.sqlite3\" ]; then "
                "    rc=$(ls -1 \"$d/runs\" 2>/dev/null | wc -l); "
                "    newest=$(ls -1t \"$d/runs\" 2>/dev/null | head -n 1); "
                "    mt=0; [ -n \"$newest\" ] && mt=$(stat -c %Y \"$d/runs/$newest\" 2>/dev/null || echo 0); "
                "    echo \"SCOPE|$d|$rc|$mt\"; "
                "  fi; "
                "done"
            )
            shell_out = str(run_adb(serial, ["shell", probe_script], timeout=8.0))
            wm_part, _, rest = shell_out.partition("---FG---")
            fg_part, _, rest2 = rest.partition("---APPS---")
            apps_part, _, scopes_part = rest2.partition("---SCOPES---")

            sizes = re.findall(r"(\d+)x(\d+)", wm_part)
            if sizes:
                pw, ph = map(int, sizes[-1])
                info["physicalWidth"] = pw
                info["physicalHeight"] = ph
                if pw > 0:
                    info["logicalHeight"] = round(ph * 720 / pw)

            fg_match = re.search(r"([a-zA-Z0-9_.]+)/([a-zA-Z0-9_.$]+)", fg_part)
            if fg_match:
                info["foregroundPackage"] = fg_match.group(1)
                info["foregroundActivity"] = fg_match.group(2)

            scope_owner_map: dict[str, str] = {}
            for aline in apps_part.splitlines():
                if not aline.startswith("APP|"):
                    continue
                _, pkg, ver_s, build_kind, uid_s, rscopes_s = (aline.split("|", 5) + ["", "", "", "", "", ""])[:6]
                ver = ver_s.replace("versionName=", "").strip()
                is_debug = build_kind.strip() == "DEBUGGABLE"
                if pkg == "top.natsuu.mta.wechat_channels":
                    app_label = f"微信视频号助手 [正式版 Release v{ver}]"
                elif pkg == "top.natsuu.mta.wechat_channels.integration":
                    app_label = f"微信视频号助手 [Debug 调试版 v{ver}]"
                elif pkg == "top.natsuu.mta.wechat_collects":
                    app_label = f"微信视频号采集 [Debug 旧版 v{ver}]"
                elif pkg == "top.natsuu.mta.xiaohongshu":
                    app_label = f"小红书助手 [{'Debug版' if is_debug else '正式版'} v{ver}]"
                else:
                    app_label = f"{pkg} ({build_kind} v{ver})"

                owned_scopes = [s.strip() for s in rscopes_s.split(",") if s.strip()]
                for sc in owned_scopes:
                    scope_owner_map[sc] = "Debug版" if is_debug else "正式版"

                info["installedApps"].append({
                    "package": pkg,
                    "version": ver,
                    "buildType": "debug" if is_debug else "release",
                    "uid": uid_s.strip(),
                    "label": app_label,
                    "remoteScopes": owned_scopes,
                })

            origin_counts = {"all": 0, "remote": 0, "manual": 0, "cli": 0}
            for sline in scopes_part.splitlines():
                if sline.startswith("SCOPE|"):
                    parts_s = (sline.split("|", 3) + ["0", "0", "0", "0"])[:4]
                    _, spath, rcount_s, mt_s = parts_s
                    spath = spath.strip()
                    rcount = int(rcount_s.strip() or "0")
                    latest_mt = int(mt_s.strip() or "0")
                    origin = classify_scope_origin(spath)
                    label = _origin_info(origin)["optionTitle"]
                    if "/remote/" in spath:
                        scope_hash = spath.rsplit("/", 1)[-1]
                        owner_tag = scope_owner_map.get(scope_hash, "正式版或历史授权")
                        label = f"后台下发 [{owner_tag}] (remote/{scope_hash[:10]}…)"
                    if origin in origin_counts:
                        origin_counts[origin] += rcount
                        origin_counts["all"] += rcount
                    info["scopes"].append({
                        "path": spath,
                        "origin": origin,
                        "label": label,
                        "runCount": rcount,
                        "latestMtime": latest_mt,
                    })
            info["scopes"].sort(key=lambda s: s.get("latestMtime", 0), reverse=True)
            info["originCounts"] = origin_counts
        except Exception as exc:
            info["probeError"] = str(exc)

        devices.append(info)
    return devices


def list_device_runs(serial: str, scope_path: str = "all", limit: int = 30) -> list[dict[str, Any]]:
    """Fast batch inspection of recent runs on an ADB device without pulling full directories.

    `scope_path` supports high-level task origins (`all`, `remote`, `manual`, `cli`) as well as
    an explicit device path (e.g. `/data/local/tmp/wechat-channels-data`).
    """
    cap = min(max(int(limit), 1), 40)
    scope_key = (scope_path or "all").strip()
    if scope_key == "all":
        scope_expr = (
            "/data/local/tmp/wechat-channels-data "
            "/data/local/tmp/wechat-channels-data/remote/* "
            "/data/local/tmp/wechat-channels-dev/data"
        )
    elif scope_key == "remote":
        scope_expr = "/data/local/tmp/wechat-channels-data/remote/*"
    elif scope_key == "manual":
        scope_expr = "/data/local/tmp/wechat-channels-data"
    elif scope_key == "cli":
        scope_expr = "/data/local/tmp/wechat-channels-dev/data"
    else:
        safe_path = scope_key.rstrip("/").replace('"', '\\"')
        scope_expr = f'"{safe_path}"'

    script = f"""
candidates=$(
  for d in {scope_expr}; do
    [ -d "$d/runs" ] || continue
    for rid in $(ls -1t "$d/runs" 2>/dev/null | head -n {cap}); do
      p="$d/runs/$rid"
      [ -d "$p" ] || continue
      mt=$(stat -c %Y "$p" 2>/dev/null || echo 0)
      printf '%s|%s|%s\\n' "$mt" "$d" "$rid"
    done
  done | sort -t '|' -k1,1rn | head -n {cap}
)
printf '%s\\n' "$candidates" | while IFS='|' read -r mtime sdir rid; do
  [ -n "$rid" ] || continue
  rpath="$sdir/runs/$rid"
  [ -d "$rpath" ] || continue
  set -- "$rpath"/[0-9][0-9][0-9]-*.json
  if [ -e "$1" ]; then snaps=$#; else snaps=0; fi
  set -- "$rpath"/*.npy "$rpath"/*.png "$rpath"/*.jpg "$rpath"/*.webp
  npys=0
  for f in "$@"; do [ -e "$f" ] && npys=$((npys + 1)); done
  files=""
  for jf in collection.json link-task.json interactions.json mode.json comments.json video.json remote-result.json result.json; do
    [ -f "$rpath/$jf" ] && files="$files,$jf"
  done
  rem_res=""
  if [ -f "$rpath/remote-result.json" ]; then
    rem_res=$(cat "$rpath/remote-result.json" 2>/dev/null)
  elif [ -f "$rpath/link-task.json" ]; then
    rem_res=$(cat "$rpath/link-task.json" 2>/dev/null)
  elif [ -f "$rpath/result.json" ]; then
    rem_res=$(cat "$rpath/result.json" 2>/dev/null)
  fi
  last_timing=""
  if [ -f "$rpath/timings.jsonl" ]; then
    last_timing=$(tail -n 3 "$rpath/timings.jsonl" 2>/dev/null)
  fi
  snap_names=""
  for sf in "$rpath"/[0-9][0-9][0-9]-*.json; do
    [ -e "$sf" ] && snap_names="$snap_names,${{sf##*/}}"
  done
  printf 'RUN|%s|%s|%s|%s|%s|%s|%s|%s|%s\\n' "$sdir" "$rid" "$mtime" "$snaps" "$npys" "$files" "$snap_names" "$(echo "$rem_res" | tr '\\n' ' ')" "$(echo "$last_timing" | tr '\\n' ' ')"
done
"""
    out = str(run_adb(serial, ["shell", script], timeout=12.0))
    results = []
    for line in out.splitlines():
        if not line.startswith("RUN|"):
            continue
        parts = line.split("|", 9)
        if len(parts) < 10:
            continue
        _, sdir, rid, mtime_s, snaps_s, npys_s, files_s, snap_names_s, rem_res_s, last_timing_s = parts
        sdir = sdir.strip()
        mtime = int(mtime_s.strip() or "0")
        snap_count = int(snaps_s.strip() or "0")
        frame_count = int(npys_s.strip() or "0")
        files = [f for f in files_s.split(",") if f]

        task_type = _infer_task_type_from_hints(files, rem_res_s, last_timing_s, snap_names_s)
        outcome = _infer_outcome_from_hints(rem_res_s, last_timing_s)
        summary_msg = _infer_summary_from_hints(rem_res_s, last_timing_s)

        scope_slug = sdir.strip("/").replace("/", "__")
        local_cache_dir = CACHE_ROOT / serial / scope_slug / "runs" / rid
        is_synced = (local_cache_dir / "timings.jsonl").is_file() or any(local_cache_dir.glob("*.json"))
        origin = classify_scope_origin(sdir)
        oinfo = _origin_info(origin)

        results.append({
            "id": f"adb:{serial}:{sdir}:{rid}",
            "runId": rid,
            "sourceType": "adb",
            "serial": serial,
            "scopePath": sdir,
            "origin": origin,
            "originLabel": oinfo["label"],
            "originDesc": oinfo["desc"],
            "localPath": str(local_cache_dir) if is_synced else None,
            "synced": is_synced,
            "mtime": mtime,
            "snapshotCount": snap_count,
            "frameCount": frame_count,
            "resultFiles": files,
            "taskType": task_type,
            "taskTypeCn": get_task_type_cn(task_type),
            "outcome": outcome,
            "summaryMessage": summary_msg,
        })
    results.sort(key=lambda item: item["mtime"], reverse=True)
    return results


def sync_device_run(serial: str, scope_path: str, run_id: str, include_sqlite: bool = True) -> Path:
    """Pull a run directory (and optionally videos.sqlite3, daily_quotas.json, link_actions.json) to local cache.

    Pulls into a temporary staging directory and merges into the cached run directory so
    previously synced local frame images (.jpg/.webp/.png/.npy) are preserved even after
    the phone prunes older frames.
    """
    scope_slug = scope_path.strip("/").replace("/", "__")
    scope_cache = CACHE_ROOT / serial / scope_slug
    runs_cache = scope_cache / "runs"
    runs_cache.mkdir(parents=True, exist_ok=True)
    target_run_dir = runs_cache / run_id
    staging_parent = runs_cache / f".sync-{run_id}-{uuid.uuid4().hex[:8]}"
    staging_run_dir = staging_parent / run_id
    staging_parent.mkdir(parents=True, exist_ok=True)

    remote_run_dir = f"{scope_path.rstrip('/')}/runs/{run_id}"
    try:
        run_adb(serial, ["pull", remote_run_dir, str(staging_run_dir)], timeout=60.0)
        pulled_root = staging_run_dir if staging_run_dir.is_dir() else staging_parent
        target_run_dir.mkdir(parents=True, exist_ok=True)
        for src in pulled_root.rglob("*"):
            rel = src.relative_to(pulled_root)
            dst = target_run_dir / rel
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            elif src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)

    if include_sqlite:
        for aux_file in ("videos.sqlite3", "daily_quotas.json", "link_actions.json", "seen.json"):
            remote_aux = f"{scope_path.rstrip('/')}/{aux_file}"
            try:
                run_adb(serial, ["pull", remote_aux, str(scope_cache / aux_file)], timeout=20.0)
            except Exception:
                pass

    # Also attempt to correlate and fetch Tauri Host run_history log from Debug App if available
    try:
        find_tauri_script = (
            f"for pkg in top.natsuu.mta.wechat_channels.integration top.natsuu.mta.wechat_collects; do "
            f"  hit=$(run-as $pkg grep -rl '\"{run_id}\"' runs/ 2>/dev/null | head -n 1); "
            f"  if [ -n \"$hit\" ]; then "
            f"    run-as $pkg cat \"$hit\" 2>/dev/null; "
            f"    break; "
            f"  fi; "
            f"done"
        )
        tauri_raw = str(run_adb(serial, ["shell", find_tauri_script], timeout=6.0)).strip()
        if tauri_raw:
            tauri_events = []
            for line in tauri_raw.splitlines():
                if line.strip().startswith("{"):
                    try:
                        tauri_events.append(json.loads(line))
                    except Exception:
                        pass
            if tauri_events:
                (target_run_dir / "tauri-host-events.json").write_text(
                    json.dumps(tauri_events, ensure_ascii=False, indent=2), encoding="utf-8"
                )
    except Exception:
        pass

    return target_run_dir


def _infer_task_type_from_hints(files: list[str], rem_res_s: str, last_timing_s: str, snap_names_s: str = "") -> str:
    if rem_res_s.strip().startswith("{"):
        try:
            obj = json.loads(rem_res_s)
            if obj.get("task"):
                return str(obj["task"])
            act = obj.get("action") or (obj.get("operation", {}) if isinstance(obj.get("operation"), dict) else {}).get("action")
            if act in ACTION_TO_LINK_TASK:
                return ACTION_TO_LINK_TASK[act]
        except Exception:
            pass
    m_act = re.search(r'"action"\s*:\s*"([a-zA-Z]+)"', last_timing_s)
    if m_act and m_act.group(1) in ACTION_TO_LINK_TASK:
        return ACTION_TO_LINK_TASK[m_act.group(1)]

    if "collection.json" in files or "collectionCompleted" in last_timing_s:
        return "collect_channels"
    if "link-task.json" in files or "linkTaskCompleted" in last_timing_s or "link-" in snap_names_s:
        if "commenter-profile" in snap_names_s and "dm-" in snap_names_s:
            return "link_message_commenter"
        if "author-profile" in snap_names_s and "dm-" in snap_names_s:
            return "link_message_author"
        if "profile-after-follow" in snap_names_s or "profile-follow-check" in snap_names_s:
            return "link_follow_author"
        if "comments-reply" in snap_names_s or "comments-after-reply" in snap_names_s:
            return "link_reply_comment"
        if "comments-after-like" in snap_names_s or "comments-locate" in snap_names_s:
            return "link_like_comment"
        if "comments-composer" in snap_names_s or "comments-after-post" in snap_names_s:
            return "link_comment_video"
        if "link-video" in snap_names_s:
            return "link_like_video"
        return "link_task"
    if "interactions.json" in files or "interactionsCompleted" in last_timing_s:
        return "interact_work"
    if "mode.json" in files or "modeCompleted" in last_timing_s:
        return "run_mode"
    if "comments.json" in files or "commentsCollected" in last_timing_s:
        return "collect_video_comments"
    if "video.json" in files or "videoCollected" in last_timing_s:
        return "collect_video"
    return "wechat_task"


def _infer_summary_from_hints(rem_res_s: str, last_timing_s: str) -> str:
    status_cn_map = {
        "done": "已完成",
        "performed": "已执行并验证",
        "sent": "已发送",
        "already_messaged": "已私信过（防重跳过）",
        "already_liked": "已点赞过（防重跳过）",
        "already_collected": "已收藏过（防重跳过）",
        "already_followed": "已关注过（防重跳过）",
        "already_replied": "已回复过（防重跳过）",
        "already_commented": "已评论过（防重跳过）",
    }
    if rem_res_s.strip().startswith("{"):
        try:
            obj = json.loads(rem_res_s)
            if obj.get("error") and isinstance(obj["error"], str):
                return obj["error"]
            op = obj.get("operation") if isinstance(obj.get("operation"), dict) else obj
            if isinstance(op, dict) and (op.get("action") or op.get("author") or op.get("status")):
                parts = []
                if op.get("author"):
                    parts.append(f"作者 @{op['author']}")
                if op.get("matchedNickname") or op.get("nickname"):
                    parts.append(f"评论者 @{op.get('matchedNickname') or op.get('nickname')}")
                st = str(op.get("status") or "")
                if st:
                    parts.append(status_cn_map.get(st, st))
                if parts:
                    return " · ".join(parts)
            if obj.get("message") and isinstance(obj["message"], str):
                return obj["message"]
        except Exception:
            pass
    m_err = re.search(r'"error"\s*:\s*"([^"]+)"', last_timing_s)
    if m_err:
        return m_err.group(1)
    return ""


def _infer_outcome_from_hints(rem_res_s: str, last_timing_s: str) -> str:
    if rem_res_s.strip().startswith("{"):
        try:
            obj = json.loads(rem_res_s)
            if "success" in obj:
                return "succeeded" if obj["success"] else "failed"
        except Exception:
            pass
    if '"outcome": "failed"' in last_timing_s or '"outcome":"failed"' in last_timing_s:
        return "failed"
    if '"outcome": "succeeded"' in last_timing_s or '"outcome":"succeeded"' in last_timing_s:
        return "succeeded"
    if '"status": "restored"' in last_timing_s:
        return "succeeded"
    return "unknown"


# ---------------------------------------------------------------------------
# Local Run Discovery
# ---------------------------------------------------------------------------

def discover_local_runs(limit: int = 80) -> list[dict[str, Any]]:
    """Scan local `data/` directory for run folders, deduplicate by runId, and sort by real execution time."""
    data_root = REPO_ROOT / "data"
    if not data_root.is_dir():
        return []

    candidate_dirs: set[Path] = set()
    for timings_file in data_root.rglob("timings.jsonl"):
        candidate_dirs.add(timings_file.parent)
    for snap_file in data_root.rglob("001-*.json"):
        candidate_dirs.add(snap_file.parent)

    by_run_id: dict[str, dict[str, Any]] = {}
    for rdir in candidate_dirs:
        try:
            stat = rdir.stat()
        except OSError:
            continue
        snaps = sorted(f.name for f in rdir.glob("[0-9][0-9][0-9]-*.json") if SNAPSHOT_FILE_RE.match(f.name))
        frames = (
            list(rdir.glob("*.npy"))
            + list(rdir.glob("*.png"))
            + list(rdir.glob("*.jpg"))
            + list(rdir.glob("*.webp"))
        )
        json_files = [
            f.name
            for f in rdir.glob("*.json")
            if not SNAPSHOT_FILE_RE.match(f.name)
        ]
        timings_path = rdir / "timings.jsonl"
        last_lines = ""
        run_id = rdir.name
        if timings_path.is_file():
            try:
                lines = timings_path.read_text(encoding="utf-8", errors="replace").splitlines()
                last_lines = "\n".join(lines[-4:])
                if lines:
                    first_obj = json.loads(lines[0])
                    run_id = first_obj.get("runId") or run_id
            except Exception:
                pass
        rem_res_s = ""
        for rfile in ("remote-result.json", "link-task.json", "result.json"):
            if (rdir / rfile).is_file():
                try:
                    rem_res_s = (rdir / rfile).read_text(encoding="utf-8", errors="replace")
                    break
                except Exception:
                    pass

        rel_path = str(rdir.relative_to(REPO_ROOT))
        snap_names_s = ",".join(snaps)
        task_type = _infer_task_type_from_hints(json_files, rem_res_s, last_lines, snap_names_s)
        outcome = _infer_outcome_from_hints(rem_res_s, last_lines)
        summary_msg = _infer_summary_from_hints(rem_res_s, last_lines)
        real_ts = _extract_real_run_timestamp(rdir, snaps, int(stat.st_mtime))
        origin = classify_scope_origin(rel_path)
        oinfo = _origin_info(origin)
        is_cache = "debug-console-cache" in rel_path

        entry = {
            "id": f"local:{rel_path}",
            "runId": run_id,
            "label": rel_path,
            "sourceType": "cache" if is_cache else "local",
            "origin": origin,
            "originLabel": oinfo["label"],
            "originDesc": oinfo["desc"],
            "localPath": str(rdir),
            "relPath": rel_path,
            "synced": True,
            "mtime": real_ts,
            "snapshotCount": len(snaps),
            "frameCount": len(frames),
            "resultFiles": sorted(json_files),
            "taskType": task_type,
            "taskTypeCn": get_task_type_cn(task_type),
            "outcome": outcome,
            "summaryMessage": summary_msg,
        }

        dedup_key = run_id if run_id and run_id != "native" else rel_path
        existing = by_run_id.get(dedup_key)
        if existing is None:
            by_run_id[dedup_key] = entry
        else:
            new_score = (entry["frameCount"], entry["snapshotCount"], 1 if is_cache else 0, entry["mtime"])
            old_score = (
                existing["frameCount"],
                existing["snapshotCount"],
                1 if existing["sourceType"] == "cache" else 0,
                existing["mtime"],
            )
            if new_score > old_score:
                by_run_id[dedup_key] = entry

    entries = list(by_run_id.values())
    entries.sort(key=lambda item: item["mtime"], reverse=True)
    return entries[:limit]


# ---------------------------------------------------------------------------
# Detailed Run Analysis (Workflow Stages, Timeline, Snapshots, SQLite & Uploads)
# ---------------------------------------------------------------------------

def resolve_run_directory(run_ref: str) -> Path:
    """Resolve a run reference (`local:<rel_path>` or `adb:<serial>:<scope>:<run_id>` or path) to a local Path."""
    if run_ref.startswith("local:"):
        rel = run_ref.split(":", 1)[1]
        target = (REPO_ROOT / rel).resolve()
        if not str(target).startswith(str(REPO_ROOT)):
            raise ValueError("Run path outside repository")
        return target
    if run_ref.startswith("adb:"):
        _, serial, scope_path, run_id = run_ref.split(":", 3)
        scope_slug = scope_path.strip("/").replace("/", "__")
        cached = CACHE_ROOT / serial / scope_slug / "runs" / run_id
        if not cached.is_dir():
            return sync_device_run(serial, scope_path, run_id, include_sqlite=True)
        return cached
    candidate = Path(run_ref).resolve()
    if candidate.is_dir():
        return candidate
    raise FileNotFoundError(f"Run directory not found: {run_ref}")


def analyze_run(run_dir: Path) -> dict[str, Any]:
    """Build a comprehensive debug model for a single run directory."""
    mods = _ensure_collector_imports(hot_reload=False)
    classify_page_state = mods["interactions"].classify_page_state
    classify_mode_page = mods["modes"].classify_mode_page
    detect_abnormal_state = mods["modes"].detect_abnormal_state

    run_dir = Path(run_dir).resolve()
    if not run_dir.is_dir():
        raise FileNotFoundError(f"Run directory does not exist: {run_dir}")

    # 1. Read timings.jsonl
    timing_events: list[dict[str, Any]] = []
    timings_path = run_dir / "timings.jsonl"
    run_id = run_dir.name
    if timings_path.is_file():
        for idx, raw_line in enumerate(timings_path.read_text(encoding="utf-8", errors="replace").splitlines()):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
                obj["_lineIndex"] = idx
                if obj.get("runId"):
                    run_id = obj["runId"]
                timing_events.append(obj)
            except Exception:
                timing_events.append({"kind": "rawLine", "text": raw_line, "_lineIndex": idx})

    # 2. Read all snapshot files (001-<label>.json)
    snapshot_summaries: list[dict[str, Any]] = []
    snapshots_raw_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    snap_files = sorted(
        (f for f in run_dir.glob("[0-9][0-9][0-9]-*.json") if SNAPSHOT_FILE_RE.match(f.name)),
        key=lambda p: p.name,
    )
    prev_ts_ms: int | None = None
    for snap_file in snap_files:
        m = SNAPSHOT_FILE_RE.match(snap_file.name)
        seq = int(m.group(1)) if m else 0
        label = m.group(2) if m else snap_file.stem
        try:
            data = json.loads(snap_file.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:
            snapshot_summaries.append({
                "sequence": seq,
                "filename": snap_file.name,
                "label": label,
                "error": str(exc),
            })
            continue

        lines = data.get("lines") or []
        height = int(data.get("height") or 1605)
        width = int(data.get("width") or 720)
        attempts = data.get("attempts") or []
        total_attempt_ms = sum(int(a.get("durationMs") or 0) for a in attempts if isinstance(a, dict))
        observed_ts = max(
            (int(l.get("observedAtUnixMs") or 0) for l in lines if isinstance(l, dict)),
            default=0,
        ) or int(snap_file.stat().st_mtime * 1000)
        delta_ms = (observed_ts - prev_ts_ms) if (prev_ts_ms and observed_ts >= prev_ts_ms) else None
        if observed_ts:
            prev_ts_ms = observed_ts

        try:
            page_state = classify_page_state(lines, height)
        except Exception as exc:
            page_state = f"error:{exc}"
        try:
            abnormal = detect_abnormal_state(lines, height)
        except Exception as exc:
            abnormal = {"kind": "error", "reason": str(exc)}
        try:
            mode_page = classify_mode_page(lines, height=height)
        except Exception as exc:
            mode_page = f"error:{exc}"

        frame_file = find_snapshot_frame(run_dir, snap_file.stem)
        has_frame = frame_file is not None

        # Count lines by source
        source_counts: dict[str, int] = {}
        for l in lines:
            src = l.get("source", "unknown")
            source_counts[src] = source_counts.get(src, 0) + 1

        fields = data.get("fields") or {}
        extracted_field_Summary = {
            k: v.get("value")
            for k, v in fields.items()
            if isinstance(v, dict) and "value" in v
        }

        snap_summary = {
            "sequence": seq,
            "filename": snap_file.name,
            "label": data.get("label") or label,
            "width": width,
            "height": height,
            "roi": data.get("roi"),
            "source": data.get("source", "unknown"),
            "sourceCounts": source_counts,
            "lineCount": len(lines),
            "attempts": attempts,
            "recognitionDurationMs": total_attempt_ms,
            "observedAtUnixMs": observed_ts,
            "hasObservedTimestamp": any(l.get("observedAtUnixMs") for l in lines if isinstance(l, dict)),
            "deltaMs": delta_ms,
            "pageState": page_state,
            "pageStateLabel": PAGE_STATE_LABELS.get(page_state, page_state),
            "modePage": mode_page,
            "abnormal": abnormal,
            "fields": fields,
            "extractedValues": extracted_field_Summary,
            "hasFrame": has_frame,
            "frameType": frame_file.suffix.lstrip(".").lower() if frame_file is not None else None,
            "previewTexts": [l.get("text", "").strip() for l in lines[:8] if l.get("text", "").strip()],
        }
        snapshot_summaries.append(snap_summary)
        snapshots_raw_pairs.append((snap_summary, data))

    # 3. Read output JSON payloads (video.json, comments.json, collection.json, etc.)
    payload_files: dict[str, Any] = {}
    for jf in sorted(run_dir.glob("*.json")):
        if SNAPSHOT_FILE_RE.match(jf.name):
            continue
        try:
            payload_files[jf.name] = json.loads(jf.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:
            payload_files[jf.name] = {"_parseError": str(exc)}

    # 4. Build Workflow Stage Waterfall & Summary Metrics
    stages: list[dict[str, Any]] = []
    total_task_duration_ms = 0
    error_message = None
    outcome = "unknown"

    for ev in timing_events:
        kind = ev.get("kind")
        if kind == "taskTiming":
            tname = ev.get("taskName", "unknown")
            dur = int(ev.get("durationMs") or 0)
            st = ev.get("outcome", "unknown")
            stages.append({
                "name": tname,
                "label": STAGE_LABELS.get(tname, tname),
                "durationMs": dur,
                "outcome": st,
                "kind": "phase",
            })
            if tname not in ("collect_video_total",):
                total_task_duration_ms += dur
        elif kind in ("runCompleted", "modeCompleted", "interactionsCompleted", "collectionCompleted", "linkTaskCompleted"):
            outcome = ev.get("outcome", outcome)
            if ev.get("error"):
                error_message = ev.get("error")
            stages.append({
                "name": kind,
                "label": f"任务结束 ({kind})",
                "durationMs": 0,
                "outcome": outcome,
                "error": ev.get("error"),
                "kind": "terminal",
            })
        elif kind == "discoverySending":
            stages.append({
                "name": "discoverySending",
                "label": f"上报批次发送 ({', '.join(ev.get('eventTypes', []))})",
                "durationMs": 0,
                "outcome": "succeeded",
                "batchId": ev.get("batchId"),
                "count": len(ev.get("eventIds", [])),
                "kind": "upload",
            })
        elif kind == "discoveryAcknowledged":
            stages.append({
                "name": "discoveryAcknowledged",
                "label": f"上报收到 ACK ({len(ev.get('discoveryAckIds', []))} 条)",
                "durationMs": 0,
                "outcome": "succeeded",
                "batchId": ev.get("batchId"),
                "kind": "upload",
            })
        elif kind == "discoveryDeferred":
            stages.append({
                "name": "discoveryDeferred",
                "label": f"上报暂缓/离线保留 ({ev.get('stage', '')})",
                "durationMs": 0,
                "outcome": "warning",
                "error": ev.get("error"),
                "pendingCount": ev.get("pendingCount"),
                "kind": "upload",
            })

    # Check remote-result.json or result.json if outcome still unknown
    for rname in ("remote-result.json", "result.json", "collection.json", "interactions.json", "mode.json", "link-task.json"):
        robj = payload_files.get(rname)
        if isinstance(robj, dict):
            if outcome == "unknown":
                if "success" in robj:
                    outcome = "succeeded" if robj["success"] else "failed"
                elif robj.get("status") in ("completed", "succeeded", "performed", "done"):
                    outcome = "succeeded"
                elif robj.get("status") == "failed":
                    outcome = "failed"
                elif rname == "collection.json" and isinstance(robj.get("videos"), list):
                    outcome = "succeeded" if not robj.get("abortedReason") else "failed"
            if not error_message and robj.get("error"):
                error_message = str(robj.get("error"))
            if not error_message and robj.get("success") is False and robj.get("message"):
                error_message = str(robj.get("message"))

    if outcome == "unknown" and not error_message:
        phase_outcomes = [e.get("outcome") for e in timing_events if e.get("kind") == "taskTiming"]
        if phase_outcomes and all(o == "succeeded" for o in phase_outcomes):
            outcome = "succeeded"
        elif any(o == "failed" for o in phase_outcomes):
            outcome = "failed"
        elif snapshot_summaries:
            outcome = "succeeded"

    # Compute recognition source breakdown across all snapshots
    reco_stats = {
        "totalSnapshots": len(snapshot_summaries),
        "totalRecognitionMs": sum(s.get("recognitionDurationMs", 0) for s in snapshot_summaries),
        "bySource": {},
        "hierarchyFallbacks": 0,
        "slowestSnapshots": [],
    }
    for s in snapshot_summaries:
        src = s.get("source", "unknown")
        reco_stats["bySource"][src] = reco_stats["bySource"].get(src, 0) + 1
        attempts = s.get("attempts") or []
        if any(a.get("source") == "hierarchy" and "reason" in a for a in attempts):
            reco_stats["hierarchyFallbacks"] += 1

    reco_stats["slowestSnapshots"] = [
        {
            "sequence": s["sequence"],
            "label": s["label"],
            "durationMs": s.get("recognitionDurationMs", 0),
            "source": s.get("source"),
        }
        for s in sorted(snapshot_summaries, key=lambda x: x.get("recognitionDurationMs", 0), reverse=True)[:5]
    ]

    # 5. Locate associated SQLite & auxiliary files (in run_dir, parent, or parent.parent)
    sqlite_data = inspect_sqlite_and_uploads(run_dir)

    # 6. Enrich every step with Chinese explanation, collected data, and visual highlights
    snapshot_summaries, run_overview_cn = enrich_run_with_chinese_narrative(
        snapshots_raw=snapshots_raw_pairs,
        payload_files=payload_files,
        timing_events=timing_events,
        sqlite_data=sqlite_data,
        mods=mods,
    )

    return {
        "runId": run_id,
        "runDir": str(run_dir),
        "relPath": str(run_dir.relative_to(REPO_ROOT)) if str(run_dir).startswith(str(REPO_ROOT)) else str(run_dir),
        "outcome": outcome,
        "errorMessage": error_message,
        "totalTaskDurationMs": total_task_duration_ms,
        "runOverviewCn": run_overview_cn,
        "stages": stages,
        "timingEvents": timing_events,
        "snapshots": snapshot_summaries,
        "timelineSteps": build_timeline(snapshot_summaries, timing_events, payload_files, outcome, error_message),
        "recognitionStats": reco_stats,
        "payloadFiles": payload_files,
        "sqliteAndUploads": sqlite_data,
    }


# ---------------------------------------------------------------------------
# Single-Step Snapshot Detail & Parser Replay (with Hot-Reload)
# ---------------------------------------------------------------------------

def get_snapshot_detail(
    run_dir: Path,
    filename: str,
    *,
    author_hint: str | None = None,
    keyword_hint: str | None = None,
    custom_lines: list[dict[str, Any]] | None = None,
    hot_reload: bool = False,
) -> dict[str, Any]:
    """Load a snapshot JSON and execute all relevant collector parsers on it."""
    snap_path = (Path(run_dir) / filename).resolve()
    if not snap_path.is_file():
        raise FileNotFoundError(f"Snapshot not found: {filename}")

    raw_data = json.loads(snap_path.read_text(encoding="utf-8", errors="replace"))
    lines = custom_lines if custom_lines is not None else (raw_data.get("lines") or [])
    height = int(raw_data.get("height") or 1605)
    width = int(raw_data.get("width") or 720)
    label = str(raw_data.get("label") or filename)

    # Try to infer author hint from run's video.json / remote-result.json or earlier snapshots if not explicitly passed
    if not author_hint:
        author_hint = _infer_author_from_run(Path(run_dir), lines, height)

    replay = replay_parsers_on_lines(
        lines=lines,
        height=height,
        width=width,
        label=label,
        author_hint=author_hint,
        keyword_hint=keyword_hint,
        hot_reload=hot_reload,
    )

    frame_file = find_snapshot_frame(snap_path.parent, snap_path.stem)

    return {
        "filename": filename,
        "label": label,
        "width": width,
        "height": height,
        "roi": raw_data.get("roi"),
        "source": raw_data.get("source"),
        "attempts": raw_data.get("attempts") or [],
        "recordedFields": raw_data.get("fields") or {},
        "lines": lines,
        "visualLines": deduplicate_visual_lines(lines),
        "hasFrame": frame_file is not None,
        "frameExt": frame_file.suffix.lower() if frame_file is not None else None,
        "authorHintUsed": author_hint,
        "keywordHintUsed": keyword_hint,
        "replay": replay,
    }


def _infer_author_from_run(run_dir: Path, lines: list[dict[str, Any]], height: int) -> str | None:
    for fname in ("video.json", "remote-result.json", "interactions.json", "collection.json"):
        fpath = run_dir / fname
        if fpath.is_file():
            try:
                obj = json.loads(fpath.read_text(encoding="utf-8", errors="replace"))
                if isinstance(obj, dict):
                    payload = obj.get("payload") or obj.get("operation") or obj.get("work") or obj
                    if isinstance(payload, dict):
                        author = payload.get("authorNickname") or payload.get("author")
                        if author:
                            return str(author)
                    videos = obj.get("videos")
                    if isinstance(videos, list) and videos:
                        v0 = videos[0]
                        if isinstance(v0, dict):
                            p0 = v0.get("video", {}).get("payload", {})
                            if p0.get("authorNickname"):
                                return str(p0["authorNickname"])
            except Exception:
                pass
    try:
        mods = _ensure_collector_imports(hot_reload=False)
        return mods["collection"].author_in_feed(lines, height)["text"].lstrip("@").strip()
    except Exception:
        return None


def replay_parsers_on_lines(
    *,
    lines: list[dict[str, Any]],
    height: int = 1605,
    width: int = 720,
    label: str = "probe",
    author_hint: str | None = None,
    keyword_hint: str | None = None,
    hot_reload: bool = False,
) -> dict[str, Any]:
    """Run all collector layout/page/entity parsers on `lines` and produce semantic overlay boxes."""
    started_ns = time.monotonic_ns()
    mods = _ensure_collector_imports(hot_reload=hot_reload)

    layout_mod = mods["layout"]
    hierarchy_mod = mods["hierarchy"]
    collection_mod = mods["collection"]
    comments_mod = mods["comments"]
    interactions_mod = mods["interactions"]
    modes_mod = mods["modes"]

    results: dict[str, Any] = {}
    semantic_boxes: list[dict[str, Any]] = []
    click_targets: list[dict[str, Any]] = []

    # 1. Page State & Abnormal State Classification
    try:
        pstate = interactions_mod.classify_page_state(lines, height)
        results["pageState"] = {
            "status": "ok",
            "state": pstate,
            "label": PAGE_STATE_LABELS.get(pstate, pstate),
        }
    except Exception as exc:
        results["pageState"] = {"status": "error", "error": str(exc)}

    try:
        abn = modes_mod.detect_abnormal_state(lines, height)
        results["abnormalState"] = {"status": "ok", **abn}
    except Exception as exc:
        results["abnormalState"] = {"status": "error", "error": str(exc)}

    try:
        mpage = modes_mod.classify_mode_page(lines, height=height, expected_keyword=keyword_hint)
        top_tab, top_conf = modes_mod.active_top_tab(lines, height=height)
        results["modePage"] = {
            "status": "ok",
            "modePage": mpage,
            "activeTopTab": top_tab,
            "topTabMethod": top_conf,
            "isSearchResults": modes_mod.is_search_results_page(lines, expected_keyword=keyword_hint),
            "isSearchInput": modes_mod.is_search_input_page(lines),
        }
    except Exception as exc:
        results["modePage"] = {"status": "error", "error": str(exc)}

    # 2. Reading Rows & Work Region
    try:
        rows = layout_mod.reading_rows(lines)
        results["readingRows"] = {
            "count": len(rows),
            "rows": [
                {"top": r["top"], "bottom": r["bottom"], "text": r["text"], "itemCount": len(r["items"])}
                for r in rows[:40]
            ],
        }
    except Exception as exc:
        results["readingRows"] = {"status": "error", "error": str(exc)}

    try:
        wreg = layout_mod.work_region(lines)
        if wreg:
            results["workRegion"] = {
                "status": "ok",
                "authorText": wreg["author"]["text"] if wreg.get("author") else None,
                "bodyRowCount": len(wreg.get("body", [])),
                "bodyText": "\n".join(r["text"] for r in wreg.get("body", [])),
            }
            for r in wreg.get("body", []):
                for item in r.get("items", []):
                    semantic_boxes.append({
                        "kind": "work_body",
                        "label": "文案区 (work_region.body)",
                        "box": item["box"],
                        "text": item.get("text", ""),
                        "color": "#38bdf8",
                    })
    except Exception as exc:
        results["workRegion"] = {"status": "not_matched", "reason": str(exc)}

    # 3. Feed Author & Comment Control Detection
    detected_author = author_hint
    try:
        author_line = collection_mod.author_in_feed(lines, height)
        detected_author = detected_author or author_line["text"].lstrip("@").strip()
        results["feedAuthor"] = {"status": "ok", "line": author_line}
        bx, by, bw, bh = author_line["box"]
        semantic_boxes.append({
            "kind": "feed_author",
            "label": f"首屏作者: {author_line['text']}",
            "box": author_line["box"],
            "color": "#22c55e",
        })
        click_targets.append({
            "kind": "feed_author_click",
            "label": "点击作者昵称",
            "x": bx + min(bw, 140) // 2,
            "y": by + bh // 2,
            "color": "#22c55e",
        })
    except Exception as exc:
        results["feedAuthor"] = {"status": "not_matched", "reason": str(exc)}

    try:
        c_ctrl = collection_mod.comment_control_in_feed(lines, height)
        if c_ctrl:
            results["commentControl"] = {"status": "ok", "control": c_ctrl}
            bx, by, bw, bh = c_ctrl["box"]
            semantic_boxes.append({
                "kind": "comment_control",
                "label": f"评论入口控件: {c_ctrl.get('text', '')}",
                "box": c_ctrl["box"],
                "color": "#a855f7",
            })
            click_targets.append({
                "kind": "comment_control_click",
                "label": "点击打开评论抽屉",
                "x": bx + bw // 2,
                "y": by + bh // 2,
                "color": "#a855f7",
            })
        else:
            results["commentControl"] = {"status": "not_matched"}
    except Exception as exc:
        results["commentControl"] = {"status": "error", "reason": str(exc)}

    # 4. Video Details & Field Evidence Parser
    if detected_author:
        try:
            v_parsed = collection_mod.parse_video(lines, detected_author)
            fev = hierarchy_mod.field_evidence(lines, "video-details", detected_author)
            results["parseVideo"] = {"status": "ok", "authorUsed": detected_author, "values": v_parsed, "fieldEvidence": fev}
            for fname, fmeta in fev.items():
                if isinstance(fmeta, dict) and fmeta.get("boxes"):
                    for fbox in fmeta["boxes"]:
                        semantic_boxes.append({
                            "kind": f"field_{fname}",
                            "label": f"字段[{fname}]: {fmeta.get('value')}",
                            "box": fbox,
                            "color": "#10b981",
                        })
        except Exception as exc:
            results["parseVideo"] = {"status": "not_matched", "authorUsed": detected_author, "reason": str(exc)}

        # 5. Author Profile Parser
        try:
            a_parsed = collection_mod.parse_author(lines, detected_author)
            bio_exp = collection_mod.profile_bio_expand(lines, detected_author)
            fev_prof = hierarchy_mod.field_evidence(lines, "author-profile", detected_author)
            results["parseAuthorProfile"] = {
                "status": "ok",
                "authorUsed": detected_author,
                "values": a_parsed,
                "bioExpandControl": bio_exp,
                "fieldEvidence": fev_prof,
            }
            for fname, fmeta in fev_prof.items():
                if isinstance(fmeta, dict) and fmeta.get("boxes"):
                    for fbox in fmeta["boxes"]:
                        semantic_boxes.append({
                            "kind": f"profile_{fname}",
                            "label": f"主页字段[{fname}]: {fmeta.get('value')}",
                            "box": fbox,
                            "color": "#06b6d4",
                        })
        except Exception as exc:
            results["parseAuthorProfile"] = {"status": "not_matched", "authorUsed": detected_author, "reason": str(exc)}

    # 6. Basic Info Parser (`基础信息`)
    try:
        b_parsed = collection_mod.parse_basic_info(lines)
        fev_basic = hierarchy_mod.field_evidence(lines, "author-basic-info")
        results["parseBasicInfo"] = {"status": "ok", "values": b_parsed, "fieldEvidence": fev_basic}
        for fname, fmeta in fev_basic.items():
            if isinstance(fmeta, dict) and fmeta.get("boxes"):
                for fbox in fmeta["boxes"]:
                    semantic_boxes.append({
                        "kind": f"basic_{fname}",
                        "label": f"基础信息[{fname}]: {fmeta.get('value')}",
                        "box": fbox,
                        "color": "#f59e0b",
                    })
    except Exception as exc:
        results["parseBasicInfo"] = {"status": "not_matched", "reason": str(exc)}

    # 7. Comment Drawer Parser (`parse_comment_drawer`)
    try:
        c_drawer = comments_mod.parse_comment_drawer(lines, height, parent_event_id="debug-parent")
        parsed_comments = c_drawer.get("comments", [])
        clean_comments = []
        for idx, cm in enumerate(parsed_comments):
            name_box = cm.get("_nameBox")
            body_box = cm.get("_bodyBox")
            like_box = cm.get("_likeBox")
            click_pt = None
            if name_box:
                nx, ny, nw, nh = name_box
                # High-velocity rule: leftmost character region min(w, 32) -> x + 16
                click_pt = {"x": nx + min(nw, 32) // 2, "y": ny + nh // 2}
                semantic_boxes.append({
                    "kind": "comment_name",
                    "label": f"评论昵称#{idx + 1}: {cm.get('nickname')}",
                    "box": name_box,
                    "color": "#f43f5e",
                })
                click_targets.append({
                    "kind": "comment_nickname_click",
                    "label": f"点击昵称左端#{idx + 1} ({cm.get('nickname')})",
                    "x": click_pt["x"],
                    "y": click_pt["y"],
                    "color": "#f43f5e",
                })
            if body_box:
                semantic_boxes.append({
                    "kind": "comment_body",
                    "label": f"评论内容#{idx + 1}: {cm.get('content', '')[:24]}",
                    "box": body_box,
                    "color": "#38bdf8",
                })
            if like_box:
                semantic_boxes.append({
                    "kind": "comment_like",
                    "label": f"评论点赞#{idx + 1} ({cm.get('likeCount', 0)})",
                    "box": like_box,
                    "color": "#eab308",
                })
            clean_comments.append({
                "index": idx + 1,
                "nickname": cm.get("nickname"),
                "content": cm.get("content"),
                "commentTime": cm.get("commentTime"),
                "ipLocation": cm.get("ipLocation"),
                "likeCount": cm.get("likeCount"),
                "commentLevel": cm.get("commentLevel"),
                "replyToNickname": cm.get("replyToNickname"),
                "hasChannelsBadge": cm.get("_hasChannelsBadge"),
                "isSelf": cm.get("_isSelf"),
                "nameBox": name_box,
                "bodyBox": body_box,
                "likeBox": like_box,
                "safeProfileClick": click_pt,
            })

        for exp in c_drawer.get("replyExpands", []):
            if exp.get("box"):
                ex, ey, ew, eh = exp["box"]
                semantic_boxes.append({
                    "kind": "reply_expand",
                    "label": f"展开回复: {exp.get('text')}",
                    "box": exp["box"],
                    "color": "#a855f7",
                })
                click_targets.append({
                    "kind": "reply_expand_click",
                    "label": f"点击{exp.get('text')}",
                    "x": ex + ew // 2,
                    "y": ey + eh // 2,
                    "color": "#a855f7",
                })

        for exp in c_drawer.get("commentExpands", []):
            if exp.get("box"):
                ex, ey, ew, eh = exp["box"]
                semantic_boxes.append({
                    "kind": "comment_expand",
                    "label": f"展开长评: {exp.get('nickname', '')}",
                    "box": exp["box"],
                    "color": "#14b8a6",
                })
                click_targets.append({
                    "kind": "comment_expand_click",
                    "label": f"点击展开长评 ({exp.get('nickname', '')})",
                    "x": ex + ew // 2,
                    "y": ey + eh // 2,
                    "color": "#14b8a6",
                })

        results["parseCommentDrawer"] = {
            "status": "ok",
            "declaredTotal": c_drawer.get("declaredTotal"),
            "empty": c_drawer.get("empty"),
            "footerReached": c_drawer.get("footerReached"),
            "selfNickname": c_drawer.get("selfNickname"),
            "commentCount": len(clean_comments),
            "headerCount": len(c_drawer.get("headers", [])),
            "headers": c_drawer.get("headers", []),
            "comments": clean_comments,
            "replyExpands": c_drawer.get("replyExpands", []),
            "commentExpands": c_drawer.get("commentExpands", []),
        }
    except Exception as exc:
        results["parseCommentDrawer"] = {"status": "not_matched", "reason": str(exc)}

    # 8. Search Video Cards Parser (`parse_search_video_cards`)
    try:
        s_cards = modes_mod.parse_search_video_cards(lines, height)
        if s_cards:
            results["parseSearchCards"] = {"status": "ok", "count": len(s_cards), "cards": s_cards}
            for idx, card in enumerate(s_cards):
                if card.get("box"):
                    semantic_boxes.append({
                        "kind": "search_card",
                        "label": f"搜索卡片#{idx + 1}: @{card.get('author')} - {card.get('title', '')[:16]}",
                        "box": card["box"],
                        "color": "#ec4899",
                    })
                if card.get("clickPoint"):
                    cx, cy = card["clickPoint"]
                    click_targets.append({
                        "kind": "search_card_click",
                        "label": f"点击搜索卡片#{idx + 1}",
                        "x": cx,
                        "y": cy,
                        "color": "#ec4899",
                    })
        else:
            results["parseSearchCards"] = {"status": "empty", "count": 0, "cards": []}
    except Exception as exc:
        results["parseSearchCards"] = {"status": "error", "reason": str(exc)}

    elapsed_ms = (time.monotonic_ns() - started_ns) // 1_000_000
    return {
        "durationMs": elapsed_ms,
        "hotReloaded": hot_reload,
        "parsers": results,
        "semanticBoxes": semantic_boxes,
        "clickTargets": click_targets,
    }


# ---------------------------------------------------------------------------
# SQLite (`videos.sqlite3`) & Upload Batches Inspector
# ---------------------------------------------------------------------------

def _find_sqlite_file(run_dir: Path) -> Path | None:
    candidates = [
        run_dir / "videos.sqlite3",
        run_dir.parent / "videos.sqlite3",
        run_dir.parent.parent / "videos.sqlite3",
    ]
    for c in candidates:
        if c.is_file():
            return c
    # Also check any *.sqlite3 in run_dir or parent
    for folder in (run_dir, run_dir.parent):
        found = sorted(folder.glob("*.sqlite3"))
        if found:
            return found[0]
    return None


def inspect_sqlite_and_uploads(run_dir: Path) -> dict[str, Any]:
    """Inspect `videos.sqlite3`, verify `acceptance.sql` & foreign keys, and read upload batches."""
    run_dir = Path(run_dir)
    db_path = _find_sqlite_file(run_dir)
    uploads_dir = run_dir / "uploads" if (run_dir / "uploads").is_dir() else (run_dir.parent / "uploads")

    upload_batches: list[dict[str, Any]] = []
    if uploads_dir.is_dir():
        req_files = sorted(uploads_dir.glob("*-request.json"), key=lambda p: p.stat().st_mtime)
        for req_f in req_files:
            batch_id = req_f.name[: -len("-request.json")]
            ack_f = uploads_dir / f"{batch_id}-ack.json"
            try:
                req_obj = json.loads(req_f.read_text(encoding="utf-8", errors="replace"))
            except Exception as exc:
                req_obj = {"error": str(exc)}
            ack_obj = None
            if ack_f.is_file():
                try:
                    ack_obj = json.loads(ack_f.read_text(encoding="utf-8", errors="replace"))
                except Exception as exc:
                    ack_obj = {"error": str(exc)}
            events = req_obj.get("discoveryEvents", []) if isinstance(req_obj, dict) else []
            ack_ids = ack_obj.get("discoveryAckIds", []) if isinstance(ack_obj, dict) else []
            upload_batches.append({
                "batchId": batch_id,
                "sentCount": len(events),
                "acknowledgedCount": len(ack_ids),
                "uploaded": ack_obj.get("uploaded") if isinstance(ack_obj, dict) else False,
                "eventTypes": [e.get("eventType") for e in events if isinstance(e, dict)],
                "events": events,
                "ack": ack_obj,
            })

    # Also read auxiliary JSON ledgers if present
    aux_ledgers: dict[str, Any] = {}
    for folder in (run_dir, run_dir.parent, run_dir.parent.parent):
        for lname in ("daily_quotas.json", "link_actions.json", "seen.json"):
            lpath = folder / lname
            if lname not in aux_ledgers and lpath.is_file():
                try:
                    aux_ledgers[lname] = json.loads(lpath.read_text(encoding="utf-8", errors="replace"))
                except Exception as exc:
                    aux_ledgers[lname] = {"_error": str(exc)}

    if db_path is None or not db_path.is_file():
        return {
            "hasDatabase": False,
            "dbPath": None,
            "uploadBatches": upload_batches,
            "auxLedgers": aux_ledgers,
        }

    videos: list[dict[str, Any]] = []
    comments: list[dict[str, Any]] = []
    users: list[dict[str, Any]] = []
    metadata_rows: dict[str, str] = {}
    acceptance_checks: dict[str, Any] = {
        "duplicateVideoKeys": [],
        "invalidVideoDraftIds": [],
        "orphanCommentIds": [],
        "orphanUserIds": [],
        "passed": True,
    }

    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)) as db:
            tables = {
                row[0]
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "metadata" in tables:
                for k, v in db.execute("SELECT key, value FROM metadata").fetchall():
                    metadata_rows[k] = v

            if "video_drafts" in tables:
                for dedupe_key, event_id, collected_at, status, event_json in db.execute(
                    "SELECT dedupe_key, event_id, collected_at, status, event_json FROM video_drafts ORDER BY collected_at DESC"
                ).fetchall():
                    try:
                        ev = json.loads(event_json)
                    except Exception:
                        ev = {}
                    videos.append({
                        "dedupeKey": dedupe_key,
                        "eventId": event_id,
                        "collectedAt": collected_at,
                        "status": status,
                        "event": ev,
                    })

                # Run acceptance.sql checks
                dup_rows = db.execute(
                    "SELECT dedupe_key, count(*) FROM video_drafts GROUP BY dedupe_key HAVING count(*) > 1"
                ).fetchall()
                acceptance_checks["duplicateVideoKeys"] = [r[0] for r in dup_rows]

                invalid_rows = db.execute(
                    """
                    SELECT event_id FROM video_drafts
                    WHERE status <> 'local_only'
                       OR coalesce(json_extract(event_json, '$.event_type'), '') <> 'VIDEO'
                       OR coalesce(json_type(event_json, '$.payload.returnVerified'), '') NOT IN ('true', 'false')
                       OR coalesce(json_extract(event_json, '$.payload.platform'), '') <> 'wechat'
                       OR length(dedupe_key) <> 64
                       OR json_type(event_json, '$.collected_at') <> 'integer'
                       OR coalesce(json_extract(event_json, '$.payload.authorNickname'), '') = ''
                       OR coalesce(json_extract(event_json, '$.payload.description'), '') = ''
                    """
                ).fetchall()
                acceptance_checks["invalidVideoDraftIds"] = [r[0] for r in invalid_rows]

            if "comment_drafts" in tables:
                for dedupe_key, event_id, parent_event_id, status, event_json in db.execute(
                    "SELECT dedupe_key, event_id, parent_event_id, status, event_json FROM comment_drafts"
                ).fetchall():
                    try:
                        ev = json.loads(event_json)
                    except Exception:
                        ev = {}
                    comments.append({
                        "dedupeKey": dedupe_key,
                        "eventId": event_id,
                        "parentEventId": parent_event_id,
                        "status": status,
                        "event": ev,
                    })

            if "user_drafts" in tables:
                for dedupe_key, event_id, parent_event_id, status, event_json in db.execute(
                    "SELECT dedupe_key, event_id, parent_event_id, status, event_json FROM user_drafts"
                ).fetchall():
                    try:
                        ev = json.loads(event_json)
                    except Exception:
                        ev = {}
                    users.append({
                        "dedupeKey": dedupe_key,
                        "eventId": event_id,
                        "parentEventId": parent_event_id,
                        "status": status,
                        "event": ev,
                    })
    except Exception as exc:
        return {
            "hasDatabase": True,
            "dbPath": str(db_path),
            "error": str(exc),
            "uploadBatches": upload_batches,
            "auxLedgers": aux_ledgers,
        }

    video_ids = {v["eventId"] for v in videos}
    comment_ids = {c["eventId"] for c in comments}

    for c in comments:
        if c["parentEventId"] not in video_ids:
            acceptance_checks["orphanCommentIds"].append(c["eventId"])
    for u in users:
        if u["parentEventId"] not in comment_ids:
            acceptance_checks["orphanUserIds"].append(u["eventId"])

    acceptance_checks["passed"] = not any(
        acceptance_checks[k]
        for k in ("duplicateVideoKeys", "invalidVideoDraftIds", "orphanCommentIds", "orphanUserIds")
    )

    # Summarize metadata upload queue (`wechat-upload:<scope>:pending:*` vs `ack:*`)
    pending_uploads = []
    acknowledged_uploads = []
    event_id_mappings: dict[str, str] = {}
    for k, v in metadata_rows.items():
        if ":pending:" in k:
            try:
                pending_uploads.append(json.loads(v))
            except Exception:
                pending_uploads.append({"rawKey": k, "rawValue": v})
        elif ":ack:" in k:
            remote_event_id = k.rsplit(":ack:", 1)[-1]
            try:
                acknowledged_uploads.append({"remoteEventId": remote_event_id, **json.loads(v)})
            except Exception:
                acknowledged_uploads.append({"remoteEventId": remote_event_id})
        elif ":event:" in k:
            local_id = k.rsplit(":event:", 1)[-1]
            event_id_mappings[local_id] = v

    ack_remote_set = {a["remoteEventId"] for a in acknowledged_uploads}
    pending_remote_set = {p.get("event_id") for p in pending_uploads if isinstance(p, dict)}

    # Build hierarchical tree: VIDEO -> COMMENT -> USER
    users_by_comment: dict[str, list[dict[str, Any]]] = {}
    for u in users:
        remote_uid = event_id_mappings.get(u["eventId"])
        u["remoteEventId"] = remote_uid
        u["uploadState"] = (
            "acknowledged"
            if remote_uid in ack_remote_set
            else ("pending" if remote_uid in pending_remote_set else "local_only")
        )
        users_by_comment.setdefault(u["parentEventId"], []).append(u)

    comments_by_video: dict[str, list[dict[str, Any]]] = {}
    for c in comments:
        remote_cid = event_id_mappings.get(c["eventId"])
        c["remoteEventId"] = remote_cid
        c["uploadState"] = (
            "acknowledged"
            if remote_cid in ack_remote_set
            else ("pending" if remote_cid in pending_remote_set else "local_only")
        )
        c["users"] = users_by_comment.get(c["eventId"], [])
        comments_by_video.setdefault(c["parentEventId"], []).append(c)

    hierarchy_tree = []
    for v in videos:
        remote_vid = event_id_mappings.get(v["eventId"])
        v_comments = comments_by_video.get(v["eventId"], [])
        v_users_count = sum(len(cm.get("users", [])) for cm in v_comments)
        hierarchy_tree.append({
            **v,
            "remoteEventId": remote_vid,
            "uploadState": (
                "acknowledged"
                if remote_vid in ack_remote_set
                else ("pending" if remote_vid in pending_remote_set else "local_only")
            ),
            "commentCount": len(v_comments),
            "userCount": v_users_count,
            "comments": v_comments,
        })

    return {
        "hasDatabase": True,
        "dbPath": str(db_path),
        "deviceId": metadata_rows.get("deviceId"),
        "counts": {
            "videos": len(videos),
            "comments": len(comments),
            "users": len(users),
            "pendingUploads": len(pending_uploads),
            "acknowledgedUploads": len(acknowledged_uploads),
            "uploadBatches": len(upload_batches),
        },
        "acceptance": acceptance_checks,
        "hierarchyTree": hierarchy_tree,
        "pendingUploads": pending_uploads,
        "acknowledgedUploads": acknowledged_uploads,
        "uploadBatches": upload_batches,
        "auxLedgers": aux_ledgers,
    }


# ---------------------------------------------------------------------------
# Live Device Screen Probe, Code Push & Background Task Manager
# ---------------------------------------------------------------------------

def capture_live_device_probe(serial: str, label: str = "live-probe") -> dict[str, Any]:
    """Capture live screenshot + UiDump XML from `serial`, convert to snapshot lines, and run parser replay."""
    mods = _ensure_collector_imports(hot_reload=False)
    text_lines = mods["hierarchy"].text_lines

    # 1. Read physical screen size
    wm_out = str(run_adb(serial, ["shell", "wm", "size"], timeout=5.0))
    sizes = re.findall(r"(\d+)x(\d+)", wm_out)
    if not sizes:
        raise RuntimeError(f"Cannot determine screen size on {serial}: {wm_out}")
    phys_w, phys_h = map(int, sizes[-1])
    logical_h = round(phys_h * 720 / phys_w)

    # 2. Capture live screenshot via screencap
    raw_cap = run_adb(serial, ["shell", "screencap"], timeout=10.0, binary=True)
    png_bytes, _, _ = android_screencap_to_png_bytes(bytes(raw_cap))

    # 3. Ensure ui-dump.dex is on device and run UiDump
    local_dex = WECHAT_COLLECTOR_DIR / "ui-dump.dex"
    remote_dex = "/data/local/tmp/wechat-debug-ui-dump.dex"
    remote_xml = "/data/local/tmp/wechat-debug-window.xml"
    if local_dex.is_file():
        run_adb(serial, ["push", str(local_dex), remote_dex], timeout=10.0)

    xml_text = ""
    hierarchy_error = None
    started_ns = time.monotonic_ns()
    try:
        dump_cmd = (
            f"rm -f {remote_xml}; "
            f"CLASSPATH={remote_dex} app_process /system/bin xhs.UiDump {remote_xml} >/dev/null 2>&1; "
            f"cat {remote_xml} 2>/dev/null || true"
        )
        xml_text = str(run_adb(serial, ["shell", dump_cmd], timeout=8.0))
    except Exception as exc:
        hierarchy_error = str(exc)
    dump_ms = (time.monotonic_ns() - started_ns) // 1_000_000

    lines: list[dict[str, Any]] = []
    source = "hierarchy"
    attempts = []
    if xml_text.strip().startswith("<?xml") or "<hierarchy" in xml_text:
        try:
            raw_lines = text_lines(xml_text, phys_w, phys_h, containers=True)
            now_ms = int(time.time() * 1000)
            lines = [
                {**line, "source": "hierarchy", "observationId": 1, "observedAtUnixMs": now_ms}
                for line in raw_lines
            ]
            attempts.append({"source": "hierarchy", "durationMs": dump_ms})
        except Exception as exc:
            hierarchy_error = str(exc)
            attempts.append({"source": "hierarchy", "durationMs": dump_ms, "reason": str(exc)})
            source = "empty_hierarchy"
    else:
        attempts.append({"source": "hierarchy", "durationMs": dump_ms, "reason": hierarchy_error or "empty_xml"})
        source = "empty_hierarchy"

    # Save live probe into a dedicated local run folder so it can be inspected & replayed like any run
    probe_dir = CACHE_ROOT / serial / "live-probes"
    probe_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(probe_dir.glob("[0-9][0-9][0-9]-*.json"))
    seq = len(existing) + 1
    safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "-", label).strip("-") or "live-probe"
    snap_name = f"{seq:03d}-{safe_label}.json"
    png_name = f"{seq:03d}-{safe_label}.png"

    evidence = {
        "label": safe_label,
        "width": 720,
        "height": logical_h,
        "physicalWidth": phys_w,
        "physicalHeight": phys_h,
        "roi": [0, 0, 0, 0],
        "source": source,
        "attempts": attempts,
        "lines": lines,
        "fields": {},
    }
    (probe_dir / snap_name).write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    (probe_dir / png_name).write_bytes(png_bytes)
    if xml_text.strip():
        (probe_dir / f"{seq:03d}-{safe_label}.xml").write_text(xml_text, encoding="utf-8")

    # Append to timings.jsonl in probe_dir
    with (probe_dir / "timings.jsonl").open("a", encoding="utf-8") as tf:
        tf.write(
            json.dumps(
                {
                    "runId": f"live-probes-{serial}",
                    "kind": "recognitionTiming",
                    "label": safe_label,
                    "source": source,
                    "attempts": attempts,
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    rel_dir = str(probe_dir.relative_to(REPO_ROOT))
    return {
        "runRef": f"local:{rel_dir}",
        "runDir": str(probe_dir),
        "filename": snap_name,
        "sequence": seq,
        "serial": serial,
        "lineCount": len(lines),
        "source": source,
        "hierarchyError": hierarchy_error,
    }


def push_collector_code_to_device(serial: str, target_root: str = "/data/local/tmp/wechat-channels-dev") -> dict[str, Any]:
    """Push `plugins/wechat/wechat_channels/collector/*.py` to CLI runner AND active Release/Debug App runtimes."""
    dest = f"{target_root.rstrip('/')}/pi/collector"
    run_adb(serial, ["shell", f"mkdir -p {dest}"], timeout=5.0)
    py_files = sorted(str(p) for p in WECHAT_COLLECTOR_DIR.glob("*.py"))
    sh_file = WECHAT_COLLECTOR_DIR / "run-phone.sh"
    dex_file = WECHAT_COLLECTOR_DIR / "ui-dump.dex"
    to_push = py_files + ([str(sh_file)] if sh_file.is_file() else []) + ([str(dex_file)] if dex_file.is_file() else [])
    run_adb(serial, ["push", *to_push, f"{dest}/"], timeout=30.0)

    # Also copy into active App agent runtimes (/data/local/tmp/maa-tauri-android/<uid>/agent/pi/collector)
    # so both Release App (top.natsuu.mta.wechat_channels) and Debug App (top.natsuu.mta.wechat_channels.integration)
    # receive the updated Python collector scripts immediately.
    sync_apps_script = (
        f"chmod +x {dest}/run-phone.sh 2>/dev/null || true; "
        f"for d in /data/local/tmp/maa-tauri-android/*/agent/pi/collector; do "
        f"  if [ -d \"$d\" ]; then "
        f"    cp -f {dest}/*.py \"$d/\" 2>/dev/null || true; "
        f"    cp -f {dest}/ui-dump.dex \"$d/\" 2>/dev/null || true; "
        f"    echo \"SYNCED|$d\"; "
        f"  fi; "
        f"done"
    )
    out = str(run_adb(serial, ["shell", sync_apps_script], timeout=10.0))
    app_runtimes = [line.split("|", 1)[1] for line in out.splitlines() if line.startswith("SYNCED|")]
    return {
        "serial": serial,
        "destination": dest,
        "appRuntimesSynced": app_runtimes,
        "pushedCount": len(to_push),
    }


class DeviceTaskManager:
    """Manages background ADB task executions initiated from the Web Debug Console."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}

    def start_task(
        self,
        *,
        serial: str,
        task_mode: str,
        config: dict[str, Any] | None = None,
        dump_frames: bool = True,
        no_launch: bool = False,
        sync_code_first: bool = False,
    ) -> dict[str, Any]:
        task_id = str(uuid.uuid4())[:8]
        if sync_code_first:
            push_collector_code_to_device(serial)

        runner = "/data/local/tmp/wechat-channels-dev/pi/collector/run-phone.sh"
        env_prefix = "WECHAT_DUMP_FRAMES=1 " if dump_frames else ""
        args_str = ""
        cfg_json = json.dumps(config or {}, ensure_ascii=False)
        quoted_cfg = "'" + cfg_json.replace("'", "'\"'\"'") + "'"

        if task_mode == "test":
            args_str = "--test"
        elif task_mode == "probe":
            args_str = "--probe"
        elif task_mode == "collect_video":
            args_str = "--no-launch" if no_launch else ""
        elif task_mode == "collect_comments":
            args_str = f"--comments {quoted_cfg}" + (" --no-launch" if no_launch else "")
        elif task_mode == "collect_channels":
            args_str = f"--collect {quoted_cfg}"
        elif task_mode in ("interact", "inspect_messages"):
            args_str = f"--interact {quoted_cfg}" + (" --no-launch" if no_launch else "")
        elif task_mode == "mode":
            args_str = f"--mode {quoted_cfg}" + (" --no-launch" if no_launch else "")
        elif task_mode == "link" or task_mode.startswith("link_"):
            args_str = f"--link {quoted_cfg}"
        else:
            raise ValueError(f"Unsupported task_mode: {task_mode}")

        remote_cmd = f"{env_prefix}{runner} {args_str}".strip()
        cmd = ["adb", "-s", serial, "shell", remote_cmd]

        record: dict[str, Any] = {
            "taskId": task_id,
            "serial": serial,
            "taskMode": task_mode,
            "config": config or {},
            "dumpFrames": dump_frames,
            "command": remote_cmd,
            "startedAt": int(time.time() * 1000),
            "finishedAt": None,
            "status": "running",
            "returnCode": None,
            "detectedRunId": None,
            "logs": [],
            "_proc": None,
        }

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        record["_proc"] = proc

        with self._lock:
            self._tasks[task_id] = record

        def _reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                line_s = line.rstrip("\r\n")
                with self._lock:
                    record["logs"].append(line_s)
                    if len(record["logs"]) > 500:
                        record["logs"] = record["logs"][-500:]
                    if not record["detectedRunId"] and '"runId"' in line_s:
                        try:
                            obj = json.loads(line_s)
                            if obj.get("runId"):
                                record["detectedRunId"] = obj["runId"]
                        except Exception:
                            pass
            rc = proc.wait()
            with self._lock:
                record["returnCode"] = rc
                record["finishedAt"] = int(time.time() * 1000)
                if record["status"] == "running":
                    record["status"] = "succeeded" if rc == 0 else "failed"

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        return self._public_record(record)

    def stop_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._tasks.get(task_id)
            if not record:
                raise KeyError(f"Task not found: {task_id}")
            proc = record.get("_proc")
            serial = record["serial"]
            record["status"] = "stopped"

        if proc and proc.poll() is None:
            proc.terminate()
        try:
            run_adb(serial, ["shell", "pkill -f 'collector/phone.py' 2>/dev/null || true"], timeout=5.0)
        except Exception:
            pass
        return self._public_record(record)

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._public_record(r)
                for r in sorted(self._tasks.values(), key=lambda x: x["startedAt"], reverse=True)
            ]

    @staticmethod
    def _public_record(record: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in record.items() if not k.startswith("_")}


TASK_MANAGER = DeviceTaskManager()
