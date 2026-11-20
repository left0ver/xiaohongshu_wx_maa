#!/usr/bin/env python3
"""HTTP API & Static UI Server for the MAA Mobile Automation Web Debug Console.

Usage:
    python3 tools/debug_console/server.py --port 8765
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.debug_console.engine import (  # noqa: E402
    TASK_MANAGER,
    analyze_run,
    capture_live_device_probe,
    discover_local_runs,
    find_snapshot_frame,
    get_snapshot_detail,
    inspect_sqlite_and_uploads,
    list_adb_devices,
    list_device_runs,
    npy_file_to_png_bytes,
    push_collector_code_to_device,
    replay_parsers_on_lines,
    resolve_run_directory,
    sync_device_run,
)

STATIC_DIR = Path(__file__).with_name("static")


class DebugConsoleHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep console output concise
        sys.stderr.write(f"[DebugConsole] {self.address_string()} - {fmt % args}\n")

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        def q1(name: str, default: str | None = None) -> str | None:
            vals = qs.get(name)
            return vals[0] if vals else default

        try:
            if path == "/api/health":
                self._send_json({"ok": True, "service": "maa-debug-console"})
                return

            if path == "/api/devices":
                devices = list_adb_devices()
                self._send_json({"ok": True, "devices": devices})
                return

            if path == "/api/runs":
                source = q1("source", "adb")
                serial = q1("serial")
                scope = q1("scope", "all") or "all"
                limit = int(q1("limit", "40") or "40")
                runs: list[dict[str, Any]] = []
                if source in ("all", "adb") and serial:
                    try:
                        runs.extend(list_device_runs(serial, scope, limit=limit))
                    except Exception as exc:
                        if source == "adb":
                            raise exc
                if source in ("all", "local"):
                    local_runs = discover_local_runs(limit=limit)
                    if source == "all":
                        seen_ids = {r.get("runId") for r in runs if r.get("runId")}
                        for lr in local_runs:
                            if lr.get("runId") not in seen_ids:
                                runs.append(lr)
                        runs.sort(key=lambda r: r.get("mtime") or 0, reverse=True)
                        runs = runs[:limit]
                    else:
                        runs.extend(local_runs)
                self._send_json({"ok": True, "runs": runs})
                return

            if path == "/api/runs/detail":
                run_ref = q1("runRef")
                if not run_ref:
                    self._send_json({"ok": False, "error": "Missing runRef parameter"}, HTTPStatus.BAD_REQUEST)
                    return
                run_dir = resolve_run_directory(run_ref)
                detail = analyze_run(run_dir)
                detail["runRef"] = run_ref
                self._send_json({"ok": True, "run": detail})
                return

            if path == "/api/runs/snapshot":
                run_ref = q1("runRef")
                filename = q1("filename")
                if not run_ref or not filename:
                    self._send_json({"ok": False, "error": "Missing runRef or filename"}, HTTPStatus.BAD_REQUEST)
                    return
                run_dir = resolve_run_directory(run_ref)
                hot_reload = q1("hotReload", "0") in ("1", "true", "yes")
                detail = get_snapshot_detail(
                    run_dir,
                    filename,
                    author_hint=q1("author") or None,
                    keyword_hint=q1("keyword") or None,
                    hot_reload=hot_reload,
                )
                self._send_json({"ok": True, "snapshot": detail})
                return

            if path == "/api/runs/frame":
                run_ref = q1("runRef")
                filename = q1("filename")
                if not run_ref or not filename:
                    self._send_json({"ok": False, "error": "Missing runRef or filename"}, HTTPStatus.BAD_REQUEST)
                    return
                run_dir = resolve_run_directory(run_ref)
                frame_path = find_snapshot_frame(run_dir, Path(filename).stem)
                if frame_path is None:
                    self._send_json(
                        {"ok": False, "error": "No frame (.jpg/.webp/.png/.npy) available for this snapshot"},
                        HTTPStatus.NOT_FOUND,
                    )
                    return
                ext = frame_path.suffix.lower()
                if ext in (".jpg", ".jpeg"):
                    self._send_bytes(frame_path.read_bytes(), "image/jpeg")
                    return
                if ext == ".webp":
                    self._send_bytes(frame_path.read_bytes(), "image/webp")
                    return
                if ext == ".png":
                    self._send_bytes(frame_path.read_bytes(), "image/png")
                    return
                if ext == ".npy":
                    png_bytes = npy_file_to_png_bytes(frame_path)
                    self._send_bytes(png_bytes, "image/png")
                    return
                self._send_json({"ok": False, "error": f"Unsupported frame format: {ext}"}, HTTPStatus.NOT_FOUND)
                return

            if path == "/api/runs/sqlite":
                run_ref = q1("runRef")
                if not run_ref:
                    self._send_json({"ok": False, "error": "Missing runRef"}, HTTPStatus.BAD_REQUEST)
                    return
                run_dir = resolve_run_directory(run_ref)
                data = inspect_sqlite_and_uploads(run_dir)
                self._send_json({"ok": True, "sqlite": data})
                return

            if path == "/api/device/tasks":
                self._send_json({"ok": True, "tasks": TASK_MANAGER.list_tasks()})
                return

            # Static files
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (STATIC_DIR / rel).resolve()
            if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
                self._send_json({"ok": False, "error": f"Not found: {path}"}, HTTPStatus.NOT_FOUND)
                return
            ctype, _ = mimetypes.guess_type(str(target))
            self._send_bytes(target.read_bytes(), ctype or "application/octet-stream")

        except Exception as exc:
            self._send_json(
                {"ok": False, "error": str(exc), "traceback": traceback.format_exc()},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            body = self._read_json_body()

            if path == "/api/runs/sync":
                serial = body.get("serial")
                scope_path = body.get("scopePath") or "/data/local/tmp/wechat-channels-data"
                run_id = body.get("runId")
                include_sqlite = bool(body.get("includeSqlite", True))
                if not serial or not run_id:
                    self._send_json({"ok": False, "error": "serial and runId are required"}, HTTPStatus.BAD_REQUEST)
                    return
                local_dir = sync_device_run(serial, scope_path, run_id, include_sqlite=include_sqlite)
                rel = str(local_dir.relative_to(REPO_ROOT))
                run_ref = f"local:{rel}"
                detail = analyze_run(local_dir)
                detail["runRef"] = run_ref
                self._send_json({"ok": True, "runRef": run_ref, "run": detail})
                return

            if path == "/api/runs/replay":
                lines = body.get("lines")
                run_ref = body.get("runRef")
                filename = body.get("filename")
                hot_reload = bool(body.get("hotReload", True))
                author_hint = body.get("authorHint") or None
                keyword_hint = body.get("keywordHint") or None

                if lines is not None and isinstance(lines, list):
                    height = int(body.get("height") or 1605)
                    width = int(body.get("width") or 720)
                    label = str(body.get("label") or "custom-replay")
                    replay = replay_parsers_on_lines(
                        lines=lines,
                        height=height,
                        width=width,
                        label=label,
                        author_hint=author_hint,
                        keyword_hint=keyword_hint,
                        hot_reload=hot_reload,
                    )
                    self._send_json({"ok": True, "replay": replay})
                    return

                if run_ref and filename:
                    run_dir = resolve_run_directory(run_ref)
                    snap = get_snapshot_detail(
                        run_dir,
                        filename,
                        author_hint=author_hint,
                        keyword_hint=keyword_hint,
                        hot_reload=hot_reload,
                    )
                    self._send_json({"ok": True, "snapshot": snap, "replay": snap["replay"]})
                    return

                self._send_json({"ok": False, "error": "Provide either lines[] or (runRef, filename)"}, HTTPStatus.BAD_REQUEST)
                return

            if path == "/api/device/probe":
                serial = body.get("serial")
                label = str(body.get("label") or "live-probe")
                if not serial:
                    self._send_json({"ok": False, "error": "serial is required"}, HTTPStatus.BAD_REQUEST)
                    return
                res = capture_live_device_probe(serial, label=label)
                run_dir = resolve_run_directory(res["runRef"])
                run_detail = analyze_run(run_dir)
                run_detail["runRef"] = res["runRef"]
                snap_detail = get_snapshot_detail(run_dir, res["filename"], hot_reload=False)
                self._send_json({
                    "ok": True,
                    "probe": res,
                    "run": run_detail,
                    "snapshot": snap_detail,
                })
                return

            if path == "/api/device/push-code":
                serial = body.get("serial")
                target_root = body.get("targetRoot") or "/data/local/tmp/wechat-channels-dev"
                if not serial:
                    self._send_json({"ok": False, "error": "serial is required"}, HTTPStatus.BAD_REQUEST)
                    return
                res = push_collector_code_to_device(serial, target_root=target_root)
                self._send_json({"ok": True, "result": res})
                return

            if path == "/api/device/tasks/start":
                serial = body.get("serial")
                task_mode = body.get("taskMode") or "collect_video"
                if not serial:
                    self._send_json({"ok": False, "error": "serial is required"}, HTTPStatus.BAD_REQUEST)
                    return
                task_rec = TASK_MANAGER.start_task(
                    serial=serial,
                    task_mode=task_mode,
                    config=body.get("config") or {},
                    dump_frames=bool(body.get("dumpFrames", True)),
                    no_launch=bool(body.get("noLaunch", False)),
                    sync_code_first=bool(body.get("syncCodeFirst", False)),
                )
                self._send_json({"ok": True, "task": task_rec})
                return

            if path == "/api/device/tasks/stop":
                task_id = body.get("taskId")
                if not task_id:
                    self._send_json({"ok": False, "error": "taskId is required"}, HTTPStatus.BAD_REQUEST)
                    return
                task_rec = TASK_MANAGER.stop_task(task_id)
                self._send_json({"ok": True, "task": task_rec})
                return

            self._send_json({"ok": False, "error": f"Unknown endpoint: {path}"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_json(
                {"ok": False, "error": str(exc), "traceback": traceback.format_exc()},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), DebugConsoleHandler)
    print(f"============================================================")
    print(f"  MAA Workflow & Collector Debug Console")
    print(f"  Listening on: http://{host}:{port}")
    print(f"============================================================", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down debug console...")
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MAA Workflow & Collector Web Debug Console")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    args = parser.parse_args()
    run_server(host=args.host, port=args.port)
