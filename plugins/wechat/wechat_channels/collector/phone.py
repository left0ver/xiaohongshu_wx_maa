"""On-device acceptance entry point; ADB only launches it and retrieves evidence."""

import json
import sys
import uuid
import unittest
from pathlib import Path

from android_ui import phone_ui
from collection import LocalStore, Timings, collect
from comments import collect_visible_comments


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
    try:
        ui = phone_ui(root, run_dir)
        event = collect(ui, LocalStore(root / "data/videos.sqlite3"), timings, launch=launch)
        (run_dir / "video.json").write_text(json.dumps(event, ensure_ascii=False, indent=2), encoding="utf-8")
        if "--comments" in sys.argv:
            comments = collect_visible_comments(ui, root / "data/videos.sqlite3", event, timings)
            (run_dir / "comments.json").write_text(json.dumps(comments, ensure_ascii=False, indent=2), encoding="utf-8")
            timings.emit({"kind": "commentsCollected", "count": len(comments["events"]),
                          "userCount": len(comments.get("userEvents", [])),
                          "profilesVisited": len(comments.get("profiles", [])),
                          "skippedProfiles": len(comments.get("skippedProfiles", [])),
                          "coverage": comments["coverage"], "complete": comments["complete"],
                          "endReason": comments.get("endReason")})
        timings.emit({"kind": "runCompleted", "outcome": "succeeded", "eventId": event["event_id"]})
    except Exception as error:
        timings.emit({"kind": "runCompleted", "outcome": "failed", "error": str(error)})
        raise


if __name__ == "__main__":
    main()
