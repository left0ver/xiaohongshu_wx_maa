"""Local discovery outbox. Transport and UI collection are separate boundaries."""

import hashlib
import json
import re
import sqlite3
import time
from urllib.parse import urlsplit


MODES = {"RECOMMEND", "SEARCH", "CITY", "AUTO_RECOMMEND", "AUTO_SEARCH", "AUTO_CITY"}


def clipboard_value(detail):
    if isinstance(detail, str):
        detail = json.loads(detail)
    if not isinstance(detail, dict) or not isinstance(detail.get("best"), dict):
        raise ValueError("Malformed Maa custom recognition result")
    value = detail["best"].get("detail")
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict) or not isinstance(value.get("text"), str) or type(value.get("timestamp")) is not int:
        raise ValueError("Malformed clipboard recognition detail")
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def note_link(value):
    """Keep signed URLs intact; never resolve links or guess a short-link ID."""
    if not isinstance(value, str):
        raise ValueError("Missing note link")
    for match in re.finditer(r'https?://[^\s<>"，。；]+', value):
        url = match.group().rstrip(".,;!?)）")
        parts = urlsplit(url)
        if parts.username or parts.password or parts.port not in (None, 80, 443):
            continue
        if parts.hostname in ("xhslink.com", "xhslink.cn") and parts.path.strip("/"):
            return url, "", "short:" + parts.hostname + parts.path.rstrip("/")
        if parts.hostname in ("www.xiaohongshu.com", "xiaohongshu.com"):
            note = re.fullmatch(r"/(?:explore|discovery/item)/([0-9a-fA-F]{24})/?", parts.path)
            if note:
                note_id = note[1].lower()
                return url, note_id, "note:" + note_id
    raise ValueError("Not a supported Xiaohongshu note link")


class Outbox:
    """Retain identity receipts after ACK; store no plaintext authorization code."""

    def __init__(self, path, authorization, device_id):
        if not authorization or not device_id:
            raise ValueError("Authorization and device identity are required")
        self.scope = digest(["xiaohongshu", authorization, device_id])
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("""CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY, scope TEXT NOT NULL, kind TEXT NOT NULL,
            mode TEXT NOT NULL, parent_id TEXT REFERENCES events(event_id),
            body TEXT, acknowledged INTEGER NOT NULL DEFAULT 0)""")
        self.db.commit()

    def close(self):
        self.db.close()

    def enqueue(self, kind, payload, mode="RECOMMEND", parent_id=""):
        if mode not in MODES or kind not in {"NOTE", "VIDEO", "COMMENT", "USER"}:
            raise ValueError("Unsupported event kind or source mode")
        payload = dict(payload)
        if kind in {"NOTE", "VIDEO"}:
            if parent_id:
                raise ValueError("A work cannot have a parent")
            expected = "IMAGE_TEXT" if kind == "NOTE" else "VIDEO"
            if payload.get("noteType") != expected:
                raise ValueError("Work kind and noteType disagree")
            url, note_id, identity = note_link(payload.get("videoUrl"))
            supplied_id = payload.get("awemeId", "")
            if supplied_id and (not re.fullmatch(r"[0-9a-f]{24}", supplied_id)
                                or note_id and supplied_id != note_id):
                raise ValueError("Invalid or conflicting note ID")
            note_id = note_id or supplied_id
            identity = "note:" + note_id if note_id else identity
            payload.update(videoUrl=url, awemeId=note_id)
            required = ("authorNickname", "authorDouyinId")
            if not (payload.get("title") or payload.get("description")):
                raise ValueError("Work title or description is required")
        else:
            parent = self.db.execute(
                "SELECT scope, kind, mode FROM events WHERE event_id=?", (parent_id,)
            ).fetchone()
            allowed = {"NOTE", "VIDEO"} if kind == "COMMENT" else {"COMMENT"}
            if not parent or parent[0] != self.scope or parent[1] not in allowed or parent[2] != mode:
                raise ValueError("Missing parent or cross-scope/type/mode association")
            if kind == "COMMENT":
                required = ("nickname", "content", "commentTime")
                # shortcut: UI comments may lack IDs; upgrade when a stable comment ID is exposed.
                identity = [parent_id, payload.get("commentId") or
                            [payload.get(key) for key in required]]
            else:
                required = ("douyinId", "nickname", "touchStatus")
                if payload.get("touchStatus") not in {"TOUCHED", "UNTOUCHED"}:
                    raise ValueError("Invalid touch status")
                identity = [parent_id, payload.get("douyinId")]
        if any(not isinstance(payload.get(key), str) or not payload[key].strip() for key in required):
            raise ValueError("Missing required discovery fields")
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 20000:
            raise ValueError("Payload exceeds the discovery envelope limit")
        key = digest([self.scope, kind, mode, identity])
        event_id = digest(["event", key])
        event = dict(eventId=event_id, parentEventId=parent_id, eventType=kind,
                     sourceMode=mode, dedupeKey=key, contentHash=digest(payload),
                     foundAt=time.time_ns() // 1_000_000, payload=payload)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?, ?, 0)",
                            (event_id, self.scope, kind, mode, parent_id or None,
                             json.dumps(event, ensure_ascii=False)))
        return event_id

    def batch(self):
        rows = self.db.execute(
            "SELECT body FROM events WHERE scope=? AND acknowledged=0 ORDER BY rowid LIMIT 200",
            (self.scope,))
        return {"discoveryEvents": [json.loads(row[0]) for row in rows]}

    def acknowledge(self, reply, sent_ids):
        """Call only with the authenticated transport response and its sent batch IDs."""
        ids = reply.get("discoveryAckIds") if isinstance(reply, dict) else None
        if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
            raise ValueError("Missing or malformed discoveryAckIds")
        allowed = set(sent_ids)
        removed = 0
        with self.db:
            for event_id in set(ids) & allowed:
                removed += self.db.execute(
                    "UPDATE events SET acknowledged=1, body=NULL "
                    "WHERE event_id=? AND scope=? AND acknowledged=0",
                    (event_id, self.scope)).rowcount
        return removed
