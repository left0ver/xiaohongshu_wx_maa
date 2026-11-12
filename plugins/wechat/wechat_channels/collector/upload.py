import hashlib
import json
import math
import os
import re
import ssl
import time
from urllib.parse import urlsplit

import certifi
from websockets.sync.client import connect


DEFAULT_SERVER_URL = "wss://api.joyfile.net/huoke/ws/telemetry"
EVENT_TYPES = {"VIDEO", "COMMENT", "USER"}
SOURCE_MODES = {"RECOMMEND", "SEARCH", "CITY", "AUTO_RECOMMEND", "AUTO_SEARCH", "AUTO_CITY"}
EVENT_ID = re.compile(r"[0-9a-fA-F-]{16,64}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _prepare_events(drafts):
    events = []
    for draft in drafts:
        event_id = draft["event_id"]
        kind = draft["event_type"]
        mode = draft["source_mode"]
        dedupe = draft["dedupe_key"]
        parent_id = draft.get("parent_event_id") or ""
        found_at = draft["collected_at"]
        payload = draft["payload"]
        if not isinstance(event_id, str) or not EVENT_ID.fullmatch(event_id):
            raise ValueError("event_id 格式无效")
        if kind not in EVENT_TYPES or mode not in SOURCE_MODES:
            raise ValueError("服务端未支持当前 event_type 或 source_mode")
        if not isinstance(dedupe, str) or not DIGEST.fullmatch(dedupe):
            raise ValueError("dedupe_key 必须是 64 位小写十六进制摘要")
        if type(found_at) is not int or found_at <= 0:
            raise ValueError("collected_at 必须是毫秒时间戳")
        if kind == "VIDEO":
            if parent_id:
                raise ValueError("VIDEO 的 parent_event_id 必须为空")
        elif not isinstance(parent_id, str) or not EVENT_ID.fullmatch(parent_id) or parent_id == event_id:
            raise ValueError("COMMENT 和 USER 必须提供有效的 parent_event_id")
        if not isinstance(payload, dict) or payload.get("platform") != "wechat":
            raise ValueError("payload.platform 必须是 wechat")
        encoded = _encode(payload)
        if len(encoded.encode("utf-8")) > 20_000:
            raise ValueError("单条 payload 超过 20000 字节")
        events.append({
            "eventId": event_id, "parentEventId": parent_id, "eventType": kind,
            "sourceMode": mode, "dedupeKey": dedupe,
            "contentHash": hashlib.sha256(f"{kind}|{mode}|{encoded}".encode("utf-8")).hexdigest(),
            "foundAt": found_at, "payload": json.loads(encoded),
        })
    by_id = {event["eventId"]: event for event in events}
    if len(by_id) != len(events):
        raise ValueError("同一批次出现重复 event_id")
    for event in events:
        parent = by_id.get(event["parentEventId"])
        if parent is not None:
            expected = "VIDEO" if event["eventType"] == "COMMENT" else "COMMENT"
            if parent["eventType"] != expected or parent["sourceMode"] != event["sourceMode"]:
                raise ValueError("父事件类型或 source_mode 不一致")
    order = {"VIDEO": 0, "COMMENT": 1, "USER": 2}
    return sorted(events, key=lambda event: order[event["eventType"]])


def _receive_ack(connection, sent_ids, timeout):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("等待 discoveryAckIds 超时")
        message = json.loads(connection.recv(timeout=remaining))
        if not isinstance(message, dict):
            raise ValueError("服务端回执必须是 JSON 对象")
        # 此连接只处理采集上报，远程任务交给任务执行入口处理。
        if message.get("type") == "command":
            continue
        if message.get("error") == "unauthorized":
            raise PermissionError("授权码或绑定设备验证失败")
        if message.get("ok") is not True:
            raise ValueError("服务端未确认上报请求")
        ids = message.get("discoveryAckIds")
        if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
            raise ValueError("服务端 discoveryAckIds 格式无效")
        if not set(ids).issubset(sent_ids):
            raise ValueError("服务端确认了本次请求之外的事件")
        return set(ids)


def upload_wechat_events(drafts, *, machine, software_code="qiaomen",
                         server_url=DEFAULT_SERVER_URL, timeout=15.0):
    """发送最多 200 条微信视频号草稿，返回已确认和待确认的事件 ID。"""
    if not isinstance(machine, str) or not machine.strip() or len(machine) > 128:
        raise ValueError("machine 必须是授权码绑定的设备 ID")
    if not isinstance(software_code, str) or not software_code.strip() or len(software_code) > 50:
        raise ValueError("software_code 无效")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout 必须是有限正数")
    if not isinstance(drafts, (list, tuple)) or len(drafts) > 200:
        raise ValueError("drafts 必须是最多包含 200 条事件的列表或元组")
    events = _prepare_events(drafts)
    if not events:
        return {"discoveryAckIds": [], "pendingEventIds": [], "uploaded": True}
    license_key = os.environ["WECHAT_LICENSE_KEY"].strip().upper()
    if not license_key or len(license_key) > 64:
        raise ValueError("环境变量 WECHAT_LICENSE_KEY 无效")
    endpoint = urlsplit(server_url)
    if endpoint.scheme not in {"ws", "wss"} or not endpoint.hostname or endpoint.username or endpoint.password:
        raise ValueError("server_url 必须是有效的 WebSocket 地址")
    tls = ssl.create_default_context(cafile=certifi.where()) if endpoint.scheme == "wss" else None
    envelope = {
        "softwareCode": software_code, "licenseKey": license_key, "machine": machine,
        "clientRole": "OPERATOR", "clientApp": "WECHAT_CHANNELS",
        "appName": "微信视频号助手", "protocolVersion": 1, "capabilities": [],
    }
    sent_ids = {event["eventId"] for event in events}
    with connect(server_url, ssl=tls, open_timeout=timeout, close_timeout=min(timeout, 5),
                 ping_interval=None, max_size=8 * 1024 * 1024) as connection:
        connection.send(_encode({**envelope, "discoveryEvents": []}))
        _receive_ack(connection, set(), timeout)
        connection.send(_encode({**envelope, "discoveryEvents": events}))
        acknowledged = _receive_ack(connection, sent_ids, timeout)
    return {
        "discoveryAckIds": [event["eventId"] for event in events if event["eventId"] in acknowledged],
        "pendingEventIds": [event["eventId"] for event in events if event["eventId"] not in acknowledged],
        "uploaded": acknowledged == sent_ids,
    }
