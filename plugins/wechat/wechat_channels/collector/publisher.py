import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

from upload import DEFAULT_SERVER_URL, _prepare_events, upload_wechat_events, wechat_payload


def _encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_encode(value).encode("utf-8")).hexdigest()


def configure_upload(directory):
    load_dotenv(Path(directory) / ".env", override=False)
    license_key = os.environ["WECHAT_LICENSE_KEY"].strip().upper()
    machine = os.environ["WECHAT_DEVICE_ID"].strip()
    if not license_key or len(license_key) > 64 or not machine or len(machine) > 128:
        raise ValueError("WECHAT_LICENSE_KEY 和 WECHAT_DEVICE_ID 必须配置")
    return license_key, machine


class WechatPublisher:
    def __init__(self, database, *, records, emit=None, server_url=DEFAULT_SERVER_URL, software_code="qiaomen"):
        database = Path(database)
        database.parent.mkdir(parents=True, exist_ok=True)
        self.license_key, self.machine = configure_upload(database.parent)
        self.server_url = server_url
        self.software_code = software_code
        self.scope = _digest([server_url, software_code, self.license_key, self.machine])
        self.prefix = f"wechat-upload:{self.scope}:"
        self.records = Path(records)
        self.records.mkdir(parents=True, exist_ok=True)
        self.emit = emit
        self.acknowledged_count = 0
        self.sent_count = 0
        self.skipped_count = 0
        self.db = sqlite3.connect(database, timeout=10)
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def _event_id(self, draft):
        return _digest([self.scope, "event", draft["event_type"], draft["dedupe_key"]])

    def _pending_key(self, event):
        rank = {"VIDEO": 0, "COMMENT": 1, "USER": 2}[event["event_type"]]
        return f"{self.prefix}pending:{rank}:{event['event_id']}"

    def _ack_key(self, event_id):
        return self.prefix + "ack:" + event_id

    def _scoped_event(self, draft, id_map):
        event = json.loads(_encode(draft))
        event["event_id"] = id_map[draft["event_id"]]
        parent_id = draft.get("parent_event_id")
        if parent_id:
            if parent_id not in id_map:
                row = self.db.execute("SELECT value FROM metadata WHERE key=?",
                                      (self.prefix + "event:" + parent_id,)).fetchone()
                if row is None:
                    raise ValueError("父事件尚未生成服务端关联标识")
                id_map[parent_id] = row[0]
            event["parent_event_id"] = id_map[parent_id]
        else:
            event["parent_event_id"] = ""
        event["dedupe_key"] = _digest([self.scope, "dedupe", draft["event_type"], draft["dedupe_key"]])
        event["payload"] = wechat_payload(draft["event_type"], draft["payload"])
        if draft["event_type"] == "COMMENT":
            for name in ("parentCommentKey", "replyToCommentKey"):
                if event["payload"].get(name):
                    event["payload"][name] = _digest([self.scope, "dedupe", "COMMENT", event["payload"][name]])
        return event

    def _emit(self, event):
        if self.emit is not None:
            self.emit(event)

    def publish(self, drafts):
        _prepare_events(drafts)
        id_map = {draft["event_id"]: self._event_id(draft) for draft in drafts}
        events = [self._scoped_event(draft, id_map) for draft in drafts]
        _prepare_events(events)
        with self.db:
            for local_id, remote_id in id_map.items():
                key = self.prefix + "event:" + local_id
                previous = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
                if previous is not None and previous[0] != remote_id:
                    raise ValueError("同一事件的去重身份发生变化")
                self.db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES(?,?)", (key, remote_id))
            for event in events:
                acknowledged = self.db.execute("SELECT 1 FROM metadata WHERE key=?",
                                               (self._ack_key(event["event_id"]),)).fetchone()
                if acknowledged:
                    self.skipped_count += 1
                    continue
                self.db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES(?,?)",
                                (self._pending_key(event), _encode(event)))
            for event in events:
                if event["event_type"] == "VIDEO":
                    continue
                parent_kind = "VIDEO" if event["event_type"] == "COMMENT" else "COMMENT"
                parent_id = event["parent_event_id"]
                keys = (self._ack_key(parent_id),
                        self._pending_key({"event_type": parent_kind, "event_id": parent_id}))
                if self.db.execute("SELECT 1 FROM metadata WHERE key IN (?,?) LIMIT 1", keys).fetchone() is None:
                    raise ValueError("上报子事件前必须保存对应父事件的上报记录")
        return self.flush()

    def pending_count(self):
        prefix = self.prefix + "pending:"
        return self.db.execute("SELECT count(*) FROM metadata WHERE key>=? AND key<?",
                               (prefix, prefix + "~")).fetchone()[0]

    def flush(self):
        if os.environ["WECHAT_LICENSE_KEY"].strip().upper() != self.license_key:
            raise RuntimeError("任务运行期间授权码发生变化")
        if os.environ["WECHAT_DEVICE_ID"].strip() != self.machine:
            raise RuntimeError("任务运行期间设备身份发生变化")
        prefix = self.prefix + "pending:"
        acknowledged_ids = []
        while True:
            rows = self.db.execute("SELECT key,value FROM metadata WHERE key>=? AND key<? ORDER BY key LIMIT 200",
                                   (prefix, prefix + "~")).fetchall()
            if not rows:
                break
            events = [json.loads(row[1]) for row in rows]
            batch_id = uuid.uuid4().hex
            request = {"softwareCode": self.software_code, "machine": self.machine,
                       "discoveryEvents": _prepare_events(events)}
            (self.records / f"{batch_id}-request.json").write_text(
                json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
            self._emit({"kind": "discoverySending", "batchId": batch_id,
                        "eventTypes": [event["event_type"] for event in events],
                        "eventIds": [event["event_id"] for event in events], "sentAt": time.time_ns() // 1_000_000})
            self.sent_count += len(events)
            receipt = upload_wechat_events(events, machine=self.machine, software_code=self.software_code,
                                           server_url=self.server_url)
            confirmed = set(receipt["discoveryAckIds"])
            with self.db:
                for (key, _), event in zip(rows, events):
                    if event["event_id"] in confirmed:
                        self.db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES(?,?)",
                                        (self._ack_key(event["event_id"]), _encode({
                                            "eventType": event["event_type"], "acknowledgedAt": time.time_ns() // 1_000_000})))
                        self.db.execute("DELETE FROM metadata WHERE key=?", (key,))
            acknowledged_ids.extend(receipt["discoveryAckIds"])
            self.acknowledged_count += len(confirmed)
            (self.records / f"{batch_id}-ack.json").write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
            self._emit({"kind": "discoveryAcknowledged", "batchId": batch_id,
                        "eventTypes": [event["event_type"] for event in events if event["event_id"] in confirmed],
                        "discoveryAckIds": receipt["discoveryAckIds"], "acknowledgedAt": time.time_ns() // 1_000_000})
            if receipt["pendingEventIds"]:
                raise RuntimeError("部分采集数据未收到 ACK，已保留待发送记录")
        return acknowledged_ids

    def summary(self):
        return {"sentCount": self.sent_count, "acknowledgedCount": self.acknowledged_count,
                "alreadyAcknowledgedCount": self.skipped_count, "pendingCount": self.pending_count()}
