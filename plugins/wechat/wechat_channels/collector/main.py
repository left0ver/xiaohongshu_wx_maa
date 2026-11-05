"""Packaged Maa AgentServer entry; the same collector is exercised by phone.py."""

import fcntl
import json
import os
import sys
import uuid
from pathlib import Path

# Must select the agent binding before importing any other Maa modules.
from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

from android_ui import MaaUi
from collection import LocalStore, Timings, collect


@AgentServer.custom_action("CollectWechatVideo")
class CollectVideo(CustomAction):
    def run(self, context, argv):
        root = Path(os.environ["WECHAT_DATA_DIR"])
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        run_id = str(uuid.uuid4())
        run_dir = root / "runs" / run_id
        run_dir.mkdir(parents=True)
        timings = Timings(run_dir / "timings.jsonl", run_id)
        try:
            with (root / "collection.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                ui = MaaUi(context.tasker, run_dir, context)
                event = collect(ui, LocalStore(root / "videos.sqlite3"), timings)
                (run_dir / "video.json").write_text(json.dumps(event, ensure_ascii=False, indent=2), encoding="utf-8")
                timings.emit({"kind": "runCompleted", "outcome": "succeeded", "eventId": event["event_id"]})
                return True
        except Exception as error:
            timings.emit({"kind": "runCompleted", "outcome": "failed", "error": str(error)})
            return False


if __name__ == "__main__":
    if not AgentServer.start_up(sys.argv[-1]):
        raise RuntimeError("Could not start the collector agent")
    AgentServer.join()
    AgentServer.shut_down()
