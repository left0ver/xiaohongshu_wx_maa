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
from collection import LocalStore, Timings, collect, ensure_feed
from comments import COMMENT_LIMIT_KEYS, collect_visible_comments, comment_limits
from interactions import (
    INTERACTION_ACTIONS,
    DailyQuotaStore,
    classify_page_state,
    interact_work_and_comments,
    validate_interaction_config,
)
from modes import run_mode


@AgentServer.custom_action("CollectWechatVideo")
class CollectVideo(CustomAction):
    include_comments = False

    def run(self, context, argv):
        root = Path(os.environ["WECHAT_DATA_DIR"])
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        run_id = str(uuid.uuid4())
        run_dir = root / "runs" / run_id
        run_dir.mkdir(parents=True)
        timings = Timings(run_dir / "timings.jsonl", run_id)
        try:
            limits = {}
            if self.include_comments:
                raw_param = getattr(argv, "custom_action_param", None)
                config = json.loads(raw_param) if isinstance(raw_param, str) and raw_param.strip() else {}
                # Reject unusable limits before touching the phone.
                limits = comment_limits(config)
                timings.emit({"kind": "commentLimits", **{key: limits.get(name, "default")
                                                          for key, name in COMMENT_LIMIT_KEYS.items()}})
            with (root / "collection.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                ui = MaaUi(context.tasker, run_dir, context)
                event = collect(ui, LocalStore(root / "videos.sqlite3"), timings)
                (run_dir / "video.json").write_text(json.dumps(event, ensure_ascii=False, indent=2), encoding="utf-8")
                if self.include_comments:
                    comments = collect_visible_comments(ui, root / "videos.sqlite3", event, timings, **limits)
                    (run_dir / "comments.json").write_text(json.dumps(comments, ensure_ascii=False, indent=2), encoding="utf-8")
                    timings.emit({"kind": "commentsCollected", "count": len(comments["events"]),
                                  "userCount": len(comments.get("userEvents", [])),
                                  "profilesVisited": len(comments.get("profiles", [])),
                                  "skippedProfiles": len(comments.get("skippedProfiles", [])),
                                  "coverage": comments["coverage"], "complete": comments["complete"], "pagesRead": comments["pagesRead"],
                                  "endReason": comments.get("endReason")})
                timings.emit({"kind": "runCompleted", "outcome": "succeeded", "eventId": event["event_id"]})
                return True
        except Exception as error:
            timings.emit({"kind": "runCompleted", "outcome": "failed", "error": str(error)})
            return False


@AgentServer.custom_action("CollectWechatComments")
class CollectComments(CollectVideo):
    include_comments = True


INTERACTION_RESULT_KEYS = ("workLike", "workCollect", "workComment", "commentLike", "commentReply",
                           "authorProfileTouch", "preemptiveReply", "profileTouch")


def interaction_summary(result):
    """One status per performed action, so the run log shows what happened without opening the JSON."""
    summary = {key: result[key].get("status") or result[key].get("touchStatus") for key in INTERACTION_RESULT_KEYS
               if isinstance(result.get(key), dict)}
    if isinstance(result.get("messages"), dict):
        summary["messagesHandled"] = len(result["messages"].get("messagesHandled") or [])
    return summary


@AgentServer.custom_action("InteractWechatWork")
class InteractWork(CustomAction):
    def run(self, context, argv):
        root = Path(os.environ["WECHAT_DATA_DIR"])
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        run_id = str(uuid.uuid4())
        run_dir = root / "runs" / run_id
        run_dir.mkdir(parents=True)
        timings = Timings(run_dir / "timings.jsonl", run_id)
        raw_param = getattr(argv, "custom_action_param", None)
        config = json.loads(raw_param) if isinstance(raw_param, str) and raw_param.strip() else {}
        try:
            # Fail on an unusable configuration before touching the phone.
            if not any(config.get(key) for key in INTERACTION_ACTIONS):
                raise ValueError("No interaction is enabled")
            validate_interaction_config(config)
            with (root / "collection.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                ui = MaaUi(context.tasker, run_dir, context)
                if config.get("ensureFeed"):
                    # Navigating re-selects 推荐 and may refresh the feed, so a work already on screen is kept.
                    if classify_page_state(ui.snapshot("interact-start"), ui.height) != "channels_feed":
                        ensure_feed(ui, launch=True)
                quota_store = DailyQuotaStore(root / "daily_quotas.json", limits=config.get("limits"))
                result = interact_work_and_comments(ui, quota_store, config)
                (run_dir / "interactions.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                timings.emit({"kind": "interactionsCompleted", "outcome": "succeeded", "workKey": result.get("workKey"),
                              "summary": interaction_summary(result)})
                return True
        except Exception as error:
            timings.emit({"kind": "interactionsCompleted", "outcome": "failed", "error": str(error)})
            return False


@AgentServer.custom_action("RunWechatMode")
class RunModeAction(CustomAction):
    def run(self, context, argv):
        root = Path(os.environ["WECHAT_DATA_DIR"])
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        run_id = str(uuid.uuid4())
        run_dir = root / "runs" / run_id
        run_dir.mkdir(parents=True)
        timings = Timings(run_dir / "timings.jsonl", run_id)
        raw_param = getattr(argv, "custom_action_param", None)
        config = json.loads(raw_param) if isinstance(raw_param, str) and raw_param.strip() else {}
        try:
            with (root / "collection.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                ui = MaaUi(context.tasker, run_dir, context)
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
                return True
        except Exception as error:
            timings.emit({"kind": "modeCompleted", "outcome": "failed", "error": str(error)})
            return False


if __name__ == "__main__":
    if not AgentServer.start_up(sys.argv[-1]):
        raise RuntimeError("Could not start the collector agent")
    AgentServer.join()
    AgentServer.shut_down()
