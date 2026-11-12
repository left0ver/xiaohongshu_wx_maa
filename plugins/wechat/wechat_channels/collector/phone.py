"""On-device acceptance entry point; ADB only launches it and retrieves evidence."""

import json
import sys
import uuid
import unittest
from contextlib import ExitStack
from pathlib import Path

from accessibility import expose_controls
from links import LinkLedger, run_link_task

from android_ui import phone_ui
from collection import LocalStore, Timings, collect, ensure_feed
from comments import collect_visible_comments, comment_limits
from interactions import DailyQuotaStore, interact_work_and_comments
from modes import run_collection_task, run_mode
from publisher import configure_upload


def _load_flag_config(argv, flag):
    idx = argv.index(flag)
    if idx + 1 >= len(argv) or argv[idx + 1].startswith("--"):
        return {}
    raw = argv[idx + 1]
    if not raw.lstrip().startswith("{"):
        try:
            candidate = Path(raw)
            if candidate.is_file():
                return json.loads(candidate.read_text(encoding="utf-8"))
        except OSError:
            pass
    return json.loads(raw)


def _load_interact_config(argv):
    return _load_flag_config(argv, "--interact")


def main():
    root = Path(sys.argv[1])
    if "--test" in sys.argv:
        suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent), pattern="test_*.py")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        sys.exit(0 if result.wasSuccessful() else 1)
    if "--probe" in sys.argv:
        ui = phone_ui(root, root / "data/probe")
        print(json.dumps(ui.snapshot("probe"), ensure_ascii=False), flush=True)
        return
    run_id = str(uuid.uuid4())
    run_dir = root / "data/runs" / run_id
    run_dir.mkdir(parents=True)
    timings = Timings(run_dir / "timings.jsonl", run_id)
    launch = "--no-launch" not in sys.argv
    if "--mode" in sys.argv:
        config = _load_flag_config(sys.argv, "--mode")
        try:
            ui = phone_ui(root, run_dir)
            if config.get("ensureFeed"):
                ensure_feed(ui, launch=launch)
            with timings.phase("run_mode"):
                result = run_mode(ui, root, config, timings=timings)
            (run_dir / "mode.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            timings.emit(
                {
                    "kind": "modeCompleted",
                    "outcome": "succeeded",
                    "mode": result.get("mode"),
                    "status": result.get("status"),
                }
            )
            print(
                json.dumps({"kind": "modeResult", "runId": run_id, "result": result}, ensure_ascii=False),
                flush=True,
            )
            return
        except Exception as error:
            timings.emit({"kind": "modeCompleted", "outcome": "failed", "error": str(error)})
            raise
    if "--collect" in sys.argv or "--link" in sys.argv:
        # The two App tasks of 2026-11-11, with controls exposed as the App does.
        collect = "--collect" in sys.argv
        config = _load_flag_config(sys.argv, "--collect" if collect else "--link")
        if collect:
            configure_upload(root / "data")
        try:
            with ExitStack() as stack:
                exposed = stack.enter_context(expose_controls(root / "data", timings.emit))
                ui = phone_ui(root, run_dir, controls_exposed=exposed)
                if collect:
                    ensure_feed(ui, launch=True)
                    result = run_collection_task(ui, root, config, timings=timings)
                    name, kind = "collection.json", "collectionCompleted"
                else:
                    result = run_link_task(ui, DailyQuotaStore(root / "data/daily_quotas.json"),
                                           LinkLedger(root / "data/link_actions.json"), config)
                    name, kind = "link-task.json", "linkTaskCompleted"
            (run_dir / name).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            timings.emit({"kind": kind, "outcome": "succeeded", "status": result.get("status"),
                          "endReason": result.get("endReason")})
            return
        except Exception as error:
            timings.emit({"kind": "collectionCompleted" if collect else "linkTaskCompleted",
                          "outcome": "failed", "error": str(error)})
            raise
    if "--interact" in sys.argv:
        config = _load_interact_config(sys.argv)
        try:
            with ExitStack() as stack:
                exposed = stack.enter_context(expose_controls(root / "data", timings.emit))
                ui = phone_ui(root, run_dir, controls_exposed=exposed)
                if config.get("ensureFeed"):
                    ensure_feed(ui, launch=launch)
                quota_path = Path(config.get("quotaPath") or (root / "data/daily_quotas.json"))
                quota_store = DailyQuotaStore(quota_path, limits=config.get("limits"))
                with timings.phase("interact_work_and_comments"):
                    result = interact_work_and_comments(ui, quota_store, config)
            (run_dir / "interactions.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            timings.emit({"kind": "interactionsCompleted", "outcome": "succeeded", "workKey": result.get("workKey")})
            print(json.dumps({"kind": "interactionResult", "runId": run_id, "result": result}, ensure_ascii=False), flush=True)
            return
        except Exception as error:
            timings.emit({"kind": "interactionsCompleted", "outcome": "failed", "error": str(error)})
            raise
    try:
        ui = phone_ui(root, run_dir)
        event = collect(ui, LocalStore(root / "data/videos.sqlite3"), timings, launch=launch)
        (run_dir / "video.json").write_text(json.dumps(event, ensure_ascii=False, indent=2), encoding="utf-8")
        if "--comments" in sys.argv:
            # Optional JSON after --comments uses the task-option keys, e.g. {"maxPages": "20"} or {"unlimited": true}.
            limits = comment_limits(_load_flag_config(sys.argv, "--comments"))
            timings.emit({"kind": "commentLimits", **limits})
            comments = collect_visible_comments(ui, root / "data/videos.sqlite3", event, timings, **limits)
            (run_dir / "comments.json").write_text(json.dumps(comments, ensure_ascii=False, indent=2), encoding="utf-8")
            timings.emit({"kind": "commentsCollected", "count": len(comments["events"]),
                          "userCount": len(comments.get("userEvents", [])),
                          "profilesVisited": len(comments.get("profiles", [])),
                          "skippedProfiles": len(comments.get("skippedProfiles", [])),
                          "coverage": comments["coverage"], "complete": comments["complete"],
                          "pagesRead": comments["pagesRead"], "endReason": comments.get("endReason")})
        timings.emit({"kind": "runCompleted", "outcome": "succeeded", "eventId": event["event_id"]})
    except Exception as error:
        timings.emit({"kind": "runCompleted", "outcome": "failed", "error": str(error)})
        raise


if __name__ == "__main__":
    main()
