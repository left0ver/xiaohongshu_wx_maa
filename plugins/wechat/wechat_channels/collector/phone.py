"""On-device acceptance entry point; ADB only launches it and retrieves evidence."""

import json
import sys
import uuid
import unittest
from pathlib import Path

from android_ui import phone_ui
from collection import LocalStore, Timings, collect


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
    try:
        ui = phone_ui(root, run_dir)
        event = collect(ui, LocalStore(root / "data/videos.sqlite3"), timings)
        (run_dir / "video.json").write_text(json.dumps(event, ensure_ascii=False, indent=2), encoding="utf-8")
        timings.emit({"kind": "runCompleted", "outcome": "succeeded", "eventId": event["event_id"]})
    except Exception as error:
        timings.emit({"kind": "runCompleted", "outcome": "failed", "error": str(error)})
        raise


if __name__ == "__main__":
    main()
