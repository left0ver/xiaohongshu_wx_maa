import fcntl
import json
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from urllib.request import Request, urlopen

from accessibility import expose_controls
from android_ui import MaaUi
from collection import Timings, ensure_feed
from interactions import DailyQuotaStore, failed_interactions, interact_work_and_comments
from links import LinkLedger, run_link_task
from modes import run_collection_task
from publisher import WechatPublisher
from upload import _prepare_events


LINK_TASKS = {
    "link_like_video": "likeVideo",
    "link_follow_author": "followAuthor",
    "link_message_author": "messageAuthor",
    "link_comment_video": "commentVideo",
    "link_like_comment": "likeComment",
    "link_reply_comment": "replyComment",
    "link_message_commenter": "messageCommenter",
}


class RemoteBridge:
    def __init__(self, config):
        self.config = config
        self.last_step = None
        if not config["url"].startswith("http://127.0.0.1:"):
            raise ValueError("手机通信地址无效")

    def call(self, operation, body):
        request = Request(self.config["url"] + operation,
                          data=json.dumps({**body, "commandId": self.config["commandId"]},
                                          ensure_ascii=False, allow_nan=False).encode(),
                          headers={"Content-Type": "application/json; charset=utf-8", "Authorization": "Bearer " + self.config["token"]},
                          method="POST")
        with urlopen(request, timeout=35) as response:
            return json.load(response)

    def progress(self, label):
        steps = (("share", "获取作品链接"), ("profile", "采集用户资料"), ("basic", "读取账号资料"),
                 ("comment", "处理评论"), ("message", "处理消息"), ("search", "搜索视频"),
                 ("navigation", "进入视频号"), ("video", "读取作品内容"), ("feed", "浏览视频"))
        step = next((message for name, message in steps if name in label), "正在执行视频号任务")
        if step != self.last_step:
            self.call("/progress", {"message": step})
            self.last_step = step

    def upload(self, drafts, **_):
        events = _prepare_events(drafts)
        receipt = self.call("/upload", {"discoveryEvents": events})
        confirmed = receipt.get("discoveryAckIds")
        if receipt.get("ok") is not True or not isinstance(confirmed, list):
            raise ValueError("服务端上传确认无效")
        sent = {event["eventId"] for event in events}
        if not set(confirmed).issubset(sent):
            raise ValueError("服务端确认了本批之外的事件")
        pending = [event["eventId"] for event in events if event["eventId"] not in confirmed]
        return {"discoveryAckIds": confirmed, "pendingEventIds": pending, "uploaded": not pending}


class RemoteTimings(Timings):
    def __init__(self, path, run_id, bridge):
        super().__init__(path, run_id)
        self.bridge = bridge
        self.counts = {"VIDEO": 0, "COMMENT": 0, "USER": 0}

    def emit(self, row):
        super().emit(row)
        if row.get("kind") == "discoveryAcknowledged":
            for kind in row["eventTypes"]:
                self.counts[kind] += 1
            self.bridge.call("/progress", {"message": f"已上传视频 {self.counts['VIDEO']}，评论 {self.counts['COMMENT']}，用户资料 {self.counts['USER']}"})


def execute_remote(tasker, context, root, config):
    remote = config["remote"]
    scope = remote["scope"]
    if len(scope) != 64 or any(char not in "0123456789abcdef" for char in scope):
        raise ValueError("设备授权范围无效")
    controls_root = Path(root)
    root = controls_root / "remote" / scope
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_id = str(uuid.uuid4())
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True)
    bridge = RemoteBridge(remote)
    timings = RemoteTimings(run_dir / "timings.jsonl", run_id, bridge)
    task = config["task"]
    options = config["options"]
    identity = {"scope": scope, "machine": remote["machine"]}
    summary = {"runId": run_id, "task": task, "success": False}
    try:
        with (root / "collection.lock").open("a") as lock, ExitStack() as resources:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if task == "sync_discovery":
                with WechatPublisher(root / "videos.sqlite3", records=run_dir / "uploads", identity=identity,
                                     transport=bridge.upload) as publisher:
                    publisher.flush()
                    summary.update({"success": True, "upload": publisher.summary(), "message": "待上传数据已确认"})
            else:
                exposed = resources.enter_context(expose_controls(controls_root, timings.emit))
                ui = MaaUi(tasker, run_dir, context, controls_exposed=exposed, progress=bridge.progress)
                bridge.call("/progress", {"message": "正在执行视频号任务"})
                if task == "collect_channels":
                    ensure_feed(ui, launch=True)
                    result = run_collection_task(ui, root, options, timings=timings,
                                                 publisher_identity=identity, upload_transport=bridge.upload)
                    videos = result.get("videos", [])
                    processed = result.get("processedCount", len(videos))
                    reports = [video.get("commentsReport", {}) for video in videos]
                    complete = processed == options["count"]
                    summary.update({"processedCount": processed, "complete": complete,
                                    "requestedCount": options["count"], "endReason": result.get("endReason"),
                                    "commentCount": sum(len(report.get("events", [])) for report in reports),
                                    "profileCount": sum(len(report.get("userEvents", [])) for report in reports),
                                    "commentCollectionsComplete": all(report.get("complete") is True for report in reports),
                                    "incompleteCommentVideos": sum(report.get("complete") is not True for report in reports),
                                    "upload": result["upload"], "success": complete and result["upload"]["pendingCount"] == 0,
                                    "message": f"采集结束：视频 {processed}/{options['count']}，确认上传 {result['upload']['acknowledgedCount']}，待上传 {result['upload']['pendingCount']}"})
                elif task in LINK_TASKS:
                    result = run_link_task(ui, DailyQuotaStore(root / "daily_quotas.json"),
                                           LinkLedger(root / "link_actions.json"), {**options, "action": LINK_TASKS[task]})
                    statuses = {"performed", "already_liked", "already_collected", "already_followed", "already_commented",
                                "already_replied", "already_messaged", "sent", "done"}
                    for key in ("like", "collect", "follow", "comment", "commentLike", "reply"):
                        if key in result and result[key].get("status") not in statuses:
                            raise RuntimeError(f"{key} 未完成：{result[key].get('status')}")
                    summary.update({"success": True, "operation": result, "message": "视频号操作已完成"})
                elif task == "inspect_messages":
                    ensure_feed(ui, launch=True)
                    result = interact_work_and_comments(ui, DailyQuotaStore(root / "daily_quotas.json"),
                                                       {"inspectMessages": options})
                    failures = failed_interactions(result)
                    summary.update({"success": not failures, "operation": result, "message": "消息巡检已结束",
                                    "failedActions": failures})
                else:
                    raise ValueError("不支持的视频号任务")
                (run_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except Exception as error:
        summary.update({"success": False, "message": str(error)[:300], "errorType": type(error).__name__})
        raise
    finally:
        summary["finishedAt"] = time.time_ns() // 1_000_000
        (run_dir / "remote-result.json").write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
        bridge.call("/result", {"result": summary})
    return summary["success"]
