"""Visible first-level comment drafts; replies and paging are not yet collected."""

import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone

from collection import find, identity
from layout import DATE, REGION, compact, reading_rows


def parse_comments(lines, height):
    rows = reading_rows(lines)
    heading = next((row for row in rows if any(re.fullmatch(r"评论\s*[\d.万wW]*", item["text"]) for item in row["items"])), None)
    if heading is None:
        raise ValueError("Comment drawer is not visible")
    headers = []
    for index, row in enumerate(rows):
        if row["top"] <= heading["bottom"] or row["top"] >= height - 80:
            continue
        pieces = [item for item in row["items"] if item["box"][0] < 570
                  and not re.search(r"已声明原创", item["text"])
                  and not (item["box"][0] > 400 and re.fullmatch(r"[\d.万wW]+", item["text"]))]
        text = compact("".join(item["text"] for item in pieces))
        match = re.fullmatch(rf"(.+?)({REGION})({DATE})", text)
        if not match or not pieces or min(item["box"][0] for item in pieces) > 150:
            continue
        nickname = re.sub(r"作者$", "", match[1]).strip()
        if not nickname:
            continue
        headers.append((index, row, nickname, match[2], match[3]))
    # The first author/metadata row beneath the drawer title belongs to the work.
    if headers and "作者" in headers[0][1]["text"]:
        headers = headers[1:]
    comments = []
    for position, (index, header, nickname, region, date) in enumerate(headers):
        end = headers[position + 1][0] if position + 1 < len(headers) else len(rows)
        body, reply_count, bounded = [], 0, position + 1 < len(headers)
        for row in rows[index + 1:end]:
            text = row["text"].strip()
            reply = re.fullmatch(r"(?:展开)?(\d+)条回复(?:[⌄∨vV])?", compact(text))
            if reply:
                reply_count = int(reply[1])
                bounded = True
                break
            if re.search(r"发表评论|说点什么|写评论", text):
                bounded = True
                break
            if row["top"] >= height - 80:
                break
            if re.fullmatch(r"作者赞过|置顶|收起", text):
                continue
            # Indented replies are not attached as part of the parent text.
            if row["left"] > header["left"] + 45:
                bounded = True
                break
            body.append(text)
        content = "\n".join(body).strip()
        if not bounded or not content or content.endswith(("...", "…", "展开")):
            continue
        comments.append({"nickname": nickname, "content": content,
                         "commentTime": date, "ipLocation": region,
                         "authorDouyinId": None, "replyCount": reply_count,
                         "repliesCollected": False})
    return comments


def save_comments(path, video_event, comments):
    """Local-only drafts linked to a real saved VIDEO event, never fabricated ACKs."""
    if video_event.get("event_type") != "VIDEO" or not video_event.get("event_id"):
        raise ValueError("A saved VIDEO parent is required")
    events = []
    with closing(sqlite3.connect(path)) as db, db:
        parent = db.execute("SELECT event_json FROM video_drafts WHERE event_id=?", (video_event["event_id"],)).fetchone()
        if not parent:
            raise ValueError("Parent VIDEO is not in the local database")
        saved = json.loads(parent[0])
        db.execute("""CREATE TABLE IF NOT EXISTS comment_drafts (
            dedupe_key TEXT PRIMARY KEY, event_id TEXT UNIQUE NOT NULL,
            parent_event_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status='local_only'),
            event_json TEXT NOT NULL)""")
        for comment in comments:
            if not all(comment.get(key) for key in ("nickname", "content", "commentTime")):
                raise ValueError("Comment is incomplete")
            key = "wxch-comment:" + identity(saved["event_id"], comment["nickname"], comment["content"], comment["commentTime"], comment.get("ipLocation") or "")
            previous = db.execute("SELECT event_json FROM comment_drafts WHERE dedupe_key=?", (key,)).fetchone()
            if previous:
                events.append(json.loads(previous[0]))
                continue
            event = {"event_id": str(uuid.uuid4()), "event_type": "COMMENT", "dedupe_key": key,
                     "parent_event_id": saved["event_id"], "device_id": saved["device_id"],
                     "source_mode": saved["source_mode"], "collected_at": datetime.now(timezone.utc).isoformat(),
                     "upload_status": "local_only", "payload": comment}
            db.execute("INSERT INTO comment_drafts VALUES (?, ?, ?, 'local_only', ?)",
                       (key, event["event_id"], saved["event_id"], json.dumps(event, ensure_ascii=False)))
            events.append(event)
    return events


def collect_visible_comments(ui, path, video_event, timings):
    with timings.phase("collect_visible_comments"):
        feed = ui.snapshot("feed")
        target = find(feed, r"^\d+(?:\.\d+)?[万wW]?$",
                      lambda line: line["box"][0] > 600 and line["box"][1] > ui.height - 130)
        if target is None:
            raise ValueError("Comment control is missing")
        ui.click(target)
        try:
            lines = ui.snapshot("comments")
            comments = parse_comments(lines, ui.height)
            if not comments and target["text"] != "0" and not find(lines, r"暂无评论|还没有评论"):
                raise ValueError("No complete visible comments were recognized")
            with timings.phase("persist_comment_drafts"):
                events = save_comments(path, video_event, comments)
            return {"coverage": "visible_first_level_only", "complete": False,
                    "repliesCollected": False, "events": events}
        finally:
            ui.back()
