"""Phone-side VIDEO collection, evidence, timing and durable local drafts."""

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from layout import REGION, WorkHeaderMissing, label_values, line_height, profile_name, video_values, work_region


def normalize(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).strip()


def identity(*parts):
    encoded = json.dumps([normalize(part) for part in parts], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def find(lines, pattern, predicate=lambda line: True):
    return next((line for line in sorted(lines, key=lambda item: (item["box"][1], item["box"][0]))
                 if re.search(pattern, line["text"]) and predicate(line)), None)


def author_in_feed(lines, height):
    candidates = [line for line in lines
                  if line["box"][1] > height - 150 and 65 < line["box"][0] < 250
                  and not re.search(r"关注|私信|客服|热评|设置为铃声", line["text"])
                  and not re.fullmatch(r"[√✓✔]+", line["text"].strip())
                  and not re.fullmatch(r"[\d.万wW]+", line["text"])]
    if len(candidates) != 1:
        raise ValueError("The feed author is missing or ambiguous")
    return candidates[0]


def parse_video(lines, author):
    return video_values(lines, author)


def parse_author(lines, author):
    contact = find(lines, r"^(私信|客服)$")
    follow = find(lines, r"^(?:[√✓]?)\s*(已关注|关注)$")
    if not contact or not follow or abs(contact["box"][1] - follow["box"][1]) > line_height(lines) * 2:
        raise ValueError("Author profile controls are missing")
    name = profile_name(lines, author)
    area = sorted((line for line in lines
                   if name["box"][1] + name["box"][3] <= line["box"][1] < follow["box"][1]),
                  key=lambda line: (line["box"][1], line["box"][0]))
    ip = find(area, r"IP\s*(?:归)?属地\s*[:：]?")
    certification = find(area, r"认证|企业主体|职业信息|^音乐(?:人|达人)$")
    location = next((line for line in area
                     if line["box"][0] >= name["box"][0] - 15
                     and line["box"][1] - name["box"][1] < line_height(lines) * 4
                     and re.fullmatch(f"(?:{REGION})(?:[\u4e00-\u9fff\\s]{{0,8}}[男女市区县])?(?:已实名)?", line["text"])
                     and line is not ip and line is not certification), None)
    bio = [line["text"] for line in area
           if line is not ip and line is not certification and line is not location
           and not re.search(r"\d+条原创内容|\d+作品|粉丝|获赞|音乐电台.*人在听|^播放$", line["text"])]
    fields = {
        "authorNickname": name["text"].strip(),
        "authorCertification": certification["text"] if certification else None,
        "authorIpLocation": re.sub(r".*IP\s*(?:归)?属地\s*[:：]?\s*", "", ip["text"]) if ip else None,
        "authorProfileLocation": location["text"] if location else None,
        "authorBio": None if profile_bio_expand(lines, author) else ("\n".join(bio) or None),
    }
    fields["missingFields"] = [key for key in ("authorCertification", "authorIpLocation", "authorBio") if fields[key] is None]
    return fields


def profile_bio_expand(lines, author):
    """Only expand a truncated biography between the name and profile controls."""
    follow = find(lines, r"^(?:[√✓]?)\s*(已关注|关注)$")
    if not follow:
        return None
    try:
        name = profile_name(lines, author)
    except ValueError:
        return None
    return find(lines, r"(?:[.…⋯]+\s*(?:展开|全文)|^(?:展开|全文))$",
                lambda line: name["box"][1] + name["box"][3] <= line["box"][1] < follow["box"][1])


def parse_basic_info(lines):
    if not find(lines, r"^基础信息$"):
        raise ValueError("Author basic information is not open")

    values = label_values(lines)
    channel_id = re.sub(r"\s+", "", values.get("channel") or "")
    if not channel_id or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", channel_id):
        raise ValueError("A valid public Channels ID was not recognized")
    result = {"authorDouyinId": channel_id, "authorIdentityMethod": "wechat-channel-id",
              "authorIpLocation": values.get("ip"), "authorProfileLocation": values.get("location")}
    if values.get("certification"):
        result["authorCertification"] = values["certification"].rstrip("√✓✔")
    return result


def open_basic_info(ui, more):
    # Library/model preparation happens before clicking and is logged separately.
    with ui.prepare_basic_info() as reader:
        deadline = time.monotonic() + 5
        ui.click(more)
        ready = ui.wait_basic_info(timeout=max(0, deadline - time.monotonic()), reader=reader)
    try:
        return parse_basic_info(ready)
    except ValueError:
        # Readiness (title + ID label) and reading the field values are separate
        # measurements. A visible label does not certify a complete record.
        return parse_basic_info(ui.snapshot("author-basic-info"))


class LocalStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('deviceId', ?)", (str(uuid.uuid4()),))
            db.execute("""CREATE TABLE IF NOT EXISTS video_drafts (
                dedupe_key TEXT PRIMARY KEY, event_id TEXT UNIQUE NOT NULL,
                collected_at TEXT NOT NULL, status TEXT NOT NULL CHECK(status='local_only'),
                event_json TEXT NOT NULL)""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, payload):
        if not payload.get("authorNickname") or not payload.get("description") or not payload.get("authorDouyinId"):
            raise ValueError("Cannot save an incomplete VIDEO")
        key = "wxch-video:" + identity(payload["authorNickname"], payload["description"])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT event_json FROM video_drafts WHERE dedupe_key=?", (key,)).fetchone()
            if previous:
                event = json.loads(previous[0])
                event["payload"] = {**payload, "videoUrl": event["payload"]["videoUrl"]}
                db.execute("UPDATE video_drafts SET event_json=? WHERE dedupe_key=?",
                           (json.dumps(event, ensure_ascii=False), key))
                return event, True
            device = db.execute("SELECT value FROM metadata WHERE key='deviceId'").fetchone()[0]
            event = {
                "event_id": str(uuid.uuid4()), "event_type": "VIDEO", "dedupe_key": key,
                "device_id": device, "source_mode": "RECOMMEND",
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "upload_status": "local_only",
                "payload": {**payload, "videoUrl": "wechat-channels://video/" + key.split(":", 1)[1]},
            }
            # No authorization code or transport is fabricated. These are local drafts,
            # to be mapped to the real server contract once supplied.
            db.execute("INSERT INTO video_drafts VALUES (?, ?, ?, 'local_only', ?)",
                       (key, event["event_id"], event["collected_at"], json.dumps(event, ensure_ascii=False)))
            return event, False


class Timings:
    def __init__(self, path, run_id):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.rows = []

    def emit(self, row):
        row = {"runId": self.run_id, **row}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(json.dumps(row, ensure_ascii=False), flush=True)

    @contextmanager
    def phase(self, name):
        started = time.monotonic_ns()
        status = "failed"
        try:
            yield
            status = "succeeded"
        finally:
            row = {"kind": "taskTiming", "taskName": name, "outcome": status,
                   "durationMs": (time.monotonic_ns() - started) // 1_000_000}
            self.rows.append(row)
            self.emit(row)


def navigation_roi(label, height):
    quarter = (height + 3) // 4
    if label == "navigation-bottom":
        return [0, height - quarter, 720, quarter]
    if label == "navigation-top":
        return [0, 0, 720, quarter]
    if label in ("feed", "before-author"):
        return [0, height - 250, 720, 250]
    return [0, 0, 0, 0]


def ensure_feed(ui, launch=True):
    if launch:
        ui.start_wechat()
    ready_deadline = getattr(ui, "startup_deadline", None) or time.monotonic() + 10
    waiting_for_start = launch
    navigation_steps = 0
    navigation_label = "navigation-bottom"
    while navigation_steps < 7:
        lines = ui.snapshot(navigation_label)
        if navigation_label == "navigation-bottom" and not find(lines, r"^发现$|^推荐$|^视频号$|^评论\s*\d*|^(私信|客服)$|^我的视频号$"):
            # Already inside Channels: inspect its top tabs without full-screen OCR.
            lines = ui.snapshot("navigation-top")
        if lines and lines[0].get("navigation"):
            target = lines[0]
            ui.click(target)
            if target["navigation"] == "recommend":
                return ui.snapshot("feed")
            navigation_label = "navigation-top"
            waiting_for_start = False
            navigation_steps += 1
            continue
        recommended = find(lines, r"^推荐$", lambda line: line["box"][1] < 250)
        if recommended and find(lines, r"^关注$", lambda line: line["box"][1] < 250) and find(lines, r"^朋友", lambda line: line["box"][1] < 250):
            ui.click(recommended)
            return ui.snapshot("feed")
        discover = find(lines, r"^发现$", lambda line: line["box"][1] > ui.height - 180)
        if find(lines, r"^视频号$") and find(lines, r"^发现$", lambda line: line["box"][1] < 250):
            ui.click(find(lines, r"^视频号$"))
        elif discover and find(lines, r"^通讯录$"):
            ui.click(discover)
            navigation_label = "navigation-top"
        elif find(lines, r"^评论\s*\d*") or find(lines, r"^(私信|客服)$") or find(lines, r"^我的视频号$"):
            ui.back()
        else:
            if waiting_for_start:
                remaining = ready_deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("WeChat page was not ready within 10 seconds")
                time.sleep(min(0.5, remaining))
                if time.monotonic() >= ready_deadline:
                    raise ValueError("WeChat page was not ready within 10 seconds")
                continue
            raise ValueError("Unsupported WeChat page; navigation stopped")
        waiting_for_start = False
        navigation_steps += 1
    raise ValueError("Could not reach recommendations")


def video_details(ui, lines, author):
    # The comment count is part of the visible comment control, located by OCR.
    comment = find(lines, r"^\d+(?:\.\d+)?[万wW]?$", lambda line: line["box"][0] > 600 and line["box"][1] > ui.height - 130)
    if not comment:
        raise ValueError("Comment control was not recognized")
    ui.click(comment)
    details = ui.snapshot("video-details", author=author)
    try:
        work_region(details)
    except WorkHeaderMissing:
        # A single drawer-only recovery; never swipe an unclassified feed.
        ui.restore_work_header(details)
        details = ui.snapshot("video-details", author=author)
    for _ in range(2):
        region = work_region(details)
        expand = region["expand"]
        if not expand:
            break
        ui.click(expand)
        details = ui.snapshot("expanded-description", author=author)
    return parse_video(details, author)


def collect(ui, store, timings, launch=True):
    with timings.phase("collect_video_total"):
        with timings.phase("enter_recommendations"):
            lines = ensure_feed(ui, launch)
        with timings.phase("collect_author_nickname"):
            author = author_in_feed(lines, ui.height)["text"].strip()
        with timings.phase("collect_description_time_location"):
            video = video_details(ui, lines, author)
            ui.back()
        with timings.phase("collect_author_profile"):
            feed = ui.snapshot("before-author")
            target = author_in_feed(feed, ui.height)
            ui.click(target)
            author_lines = ui.snapshot("author-profile", author=author)
            profile = parse_author(author_lines, author)
            expand = profile_bio_expand(author_lines, author)
            if expand:
                ui.click(expand)
                author_lines = ui.snapshot("author-profile", author=author)
                profile = parse_author(author_lines, author)
            name = profile_name(author_lines, author)
            ui.profile_more(name)
            menu = ui.snapshot("profile-menu")
            more = find(menu, r"^更多信息$")
            if not more:
                raise ValueError("More information menu item is missing")
            profile.update(open_basic_info(ui, more))
            profile["missingFields"] = [key for key in ("authorCertification", "authorIpLocation", "authorBio") if profile[key] is None]
            ui.back()
            ui.back()
        with timings.phase("persist_video_draft"):
            # Keep the first feed nickname and first complete work description;
            # later pages supply profile fields, not corrections to those values.
            payload = {**profile, **video, "returnVerified": False}
            payload["missingFields"] += [key for key in ("publishTimeText", "publishLocation") if not payload[key]]
            event, duplicate = store.save(payload)
            timings.emit({"kind": "videoCollected", "eventId": event["event_id"],
                          "dedupeKey": event["dedupe_key"], "duplicate": duplicate,
                          "missingFields": payload["missingFields"], "uploadStatus": "local_only"})
    return event
