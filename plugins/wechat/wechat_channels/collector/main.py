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
from collection import LocalStore, Timings, collect, ensure_feed, find
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
from links import LinkLedger, run_link_task, validate_link_task
from modes import collection_task_config, mode_failures, run_collection_task, run_mode, validate_mode_config
from publisher import configure_upload


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
    lines = ui.snapshot("interact-start") if wechat_in_front() else []
    # A video opened from a share link looks like the feed but has no top tabs.
    on_feed = (classify_page_state(lines, ui.height) == "channels_feed"
               and find(lines, r"^推荐$", lambda line: line["box"][1] < 250))
    if not on_feed:
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
        config = action_config(argv)
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
            # An unusable configuration fails before the phone is touched.
            validate_mode_config(config)
            with (root / "collection.lock").open("a") as lock, ExitStack() as stack:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Restored before the lock is released, even when the run fails.
                exposed = stack.enter_context(expose_controls(root, timings.emit))
                ui = MaaUi(context.tasker, run_dir, context, controls_exposed=exposed)
                # Started from the helper app, WeChat is not in front yet.
                start_on_feed(ui)
                result = run_mode(ui, root, config, timings=timings)
                (run_dir / "mode.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                # A failed interaction fails the task, as the interaction task does.
                failed = mode_failures(result)
                timings.emit(
                    {
                        "kind": "modeCompleted",
                        "outcome": "failed" if failed else "succeeded",
                        "mode": result.get("mode"),
                        "status": result.get("status"),
                        **({"failedActions": failed} if failed else {}),
                    }
                )
                return not failed
        except Exception as error:
            timings.emit({"kind": "modeCompleted", "outcome": "failed", "error": str(error)})
            return False


# The defaults each entry node carries. MaaFramework replaces a node's
# custom_action_param with the task's override as a whole (build 31 sent link
# tasks without their action), so they are filled in again from the node name.
NODE_DEFAULTS = {
    "CollectWechatChannels": {"entry": "recommend", "count": 10},
    "InspectWechatMessages": {"ensureFeed": True, "inspectMessages": {"like": True, "maxMessages": 1}},
    "WechatLinkLikeComment": {"action": "likeComment"},
    "WechatLinkReplyComment": {"action": "replyComment"},
    "WechatLinkMessageCommenter": {"action": "messageCommenter"},
    "WechatLinkLikeVideo": {"action": "likeVideo"},
    "WechatLinkFollowAuthor": {"action": "followAuthor"},
    "WechatLinkMessageAuthor": {"action": "messageAuthor"},
    "WechatLinkCommentVideo": {"action": "commentVideo"},
}


def with_node_defaults(node_name, config):
    merged = json.loads(json.dumps(NODE_DEFAULTS.get(node_name, {})))
    for key, value in config.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def action_config(argv):
    raw_param = getattr(argv, "custom_action_param", None)
    config = json.loads(raw_param) if isinstance(raw_param, str) and raw_param.strip() else {}
    return with_node_defaults(getattr(argv, "node_name", ""), config)


def _run_setup(argv):
    root = Path(os.environ["WECHAT_DATA_DIR"])
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_id = str(uuid.uuid4())
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True)
    return root, run_dir, Timings(run_dir / "timings.jsonl", run_id), action_config(argv)


@AgentServer.custom_action("CollectWechatChannels")
class CollectChannelsAction(CustomAction):
    """App task 1: videos from 推荐/搜索 with every matching comment and the share link."""

    def run(self, context, argv):
        root, run_dir, timings, config = _run_setup(argv)
        try:
            settings = collection_task_config(config)
            configure_upload(root)
            timings.emit({"kind": "collectionSettings", **settings})
            with (root / "collection.lock").open("a") as lock, ExitStack() as stack:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                exposed = stack.enter_context(expose_controls(root, timings.emit))
                ui = MaaUi(context.tasker, run_dir, context, controls_exposed=exposed)
                # Always through 发现 → 视频号 → 推荐, so the entry is the real feed.
                ensure_feed(ui, launch=True)
                result = run_collection_task(ui, root, config, timings=timings)
                (run_dir / "collection.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                                         encoding="utf-8")
                timings.emit({"kind": "collectionCompleted", "outcome": "succeeded",
                              "saved": len(result.get("videos") or []), "endReason": result.get("endReason"),
                              "filtered": len(result.get("filteredItems") or [])})
                return True
        except Exception as error:
            timings.emit({"kind": "collectionCompleted", "outcome": "failed", "error": str(error)})
            return False


@AgentServer.custom_action("WechatLinkTask")
class LinkTaskAction(CustomAction):
    """One action on the video behind a share link: like, follow, comment, reply or message."""

    def run(self, context, argv):
        root, run_dir, timings, config = _run_setup(argv)
        try:
            validate_link_task(config)
            with (root / "collection.lock").open("a") as lock, ExitStack() as stack:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                exposed = stack.enter_context(expose_controls(root, timings.emit))
                ui = MaaUi(context.tasker, run_dir, context, controls_exposed=exposed)
                quota_store = DailyQuotaStore(root / "daily_quotas.json")
                report = run_link_task(ui, quota_store, LinkLedger(root / "link_actions.json"), config)
                (run_dir / "link-task.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                                        encoding="utf-8")
                timings.emit({"kind": "linkTaskCompleted", "outcome": "succeeded", "action": report["action"],
                              "status": report.get("status"), "author": report.get("author")})
                return True
        except Exception as error:
            timings.emit({"kind": "linkTaskCompleted", "outcome": "failed", "action": config.get("action"),
                          "error": str(error)})
            return False


if __name__ == "__main__":
    if not AgentServer.start_up(sys.argv[-1]):
        raise RuntimeError("Could not start the collector agent")
    AgentServer.join()
    AgentServer.shut_down()
