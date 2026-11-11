"""Packaged Maa AgentServer entry; the same collector is exercised by phone.py."""

import fcntl
import json
import os
import sys
import uuid
from contextlib import ExitStack
from pathlib import Path

# Must select the agent binding before importing any other Maa modules.
from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

from accessibility import expose_controls
from android_ui import MaaUi, foreground
from collection import LocalStore, Timings, collect, ensure_feed
from comments import COMMENT_LIMIT_KEYS, collect_visible_comments, comment_limits
from interactions import (
    INTERACTION_ACTIONS,
    INTERACTION_RESULT_KEYS,
    DailyQuotaStore,
    classify_page_state,
    failed_interactions,
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
            with (root / "collection.lock").open("a") as lock, ExitStack() as stack:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Restored before the lock is released, even when the run fails.
                exposed = stack.enter_context(expose_controls(root, timings.emit))
                ui = MaaUi(context.tasker, run_dir, context, controls_exposed=exposed)
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


def interaction_summary(result):
    """One status per performed action, so the run log shows what happened without opening the JSON."""
    summary = {key: result[key].get("status") or result[key].get("touchStatus") for key in INTERACTION_RESULT_KEYS
               if isinstance(result.get(key), dict) and key != "messages"}
    if isinstance(result.get("messages"), dict):
        messages = result["messages"]
        if messages.get("status") == "failed":
            summary["messages"] = "failed"
        else:
            summary["messagesHandled"] = len(messages.get("messagesHandled") or [])
    return summary


def wechat_in_front():
    try:
        foreground()
    except RuntimeError:
        return False
    return True


def start_on_feed(ui):
    """Open WeChat and enter the Channels feed; a feed already on screen is kept.

    Starting from the helper app leaves the helper in front, so WeChat is opened
    first. Navigating re-selects 推荐 and may refresh the feed, which is why a
    work already showing in the feed is not navigated away from.
    """
    if not wechat_in_front() or classify_page_state(ui.snapshot("interact-start"), ui.height) != "channels_feed":
        ensure_feed(ui, launch=True)


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
            # Fail on an unusable configuration before touching the phone. A
            # ticked action with blank text is reported as such, not as "nothing enabled".
            validate_interaction_config(config)
            if not any(config.get(key) for key in INTERACTION_ACTIONS):
                raise ValueError("No interaction is enabled")
            with (root / "collection.lock").open("a") as lock, ExitStack() as stack:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Restored before the lock is released, even when the run fails.
                exposed = stack.enter_context(expose_controls(root, timings.emit))
                ui = MaaUi(context.tasker, run_dir, context, controls_exposed=exposed)
                if config.get("ensureFeed"):
                    start_on_feed(ui)
                quota_store = DailyQuotaStore(root / "daily_quotas.json", limits=config.get("limits"))
                result = interact_work_and_comments(ui, quota_store, config)
                (run_dir / "interactions.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                # Every action has run; the task still reports failure when any of them failed.
                failed = failed_interactions(result)
                timings.emit({"kind": "interactionsCompleted", "outcome": "failed" if failed else "succeeded",
                              "workKey": result.get("workKey"), "summary": interaction_summary(result),
                              **({"failedActions": failed} if failed else {})})
                return not failed
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
            with (root / "collection.lock").open("a") as lock, ExitStack() as stack:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Restored before the lock is released, even when the run fails.
                exposed = stack.enter_context(expose_controls(root, timings.emit))
                ui = MaaUi(context.tasker, run_dir, context, controls_exposed=exposed)
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
