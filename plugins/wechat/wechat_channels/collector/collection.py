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


def normalize(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).strip()


def identity(*parts):
    encoded = json.dumps([normalize(part) for part in parts], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def visible_author_matches(observed, expected):
    # Feed names can fade beneath the follow button. This is only a navigation
    # hint; the full profile name must also match both work-detail headers.
    observed, expected = normalize(observed), normalize(expected)
    return bool(observed and expected) and (observed.startswith(expected) or expected.startswith(observed))


def work_author_matches(header, author):
    header, author = normalize(header), normalize(author)
    if not header.startswith(author):
        return False
    remainder = header[len(author):]
    badge, marker, _ = remainder.partition("作者")
    return bool(marker) and re.fullmatch(r"[WwVv√✓✔]*", badge) is not None


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


DATE = r"(?:\d{4}年)?\d{1,2}月\d{1,2}日|\d{4}[-/]\d{1,2}[-/]\d{1,2}|(?:今天|昨天|前天)(?:\s*\d{1,2}:\d{2})?|\d+\s*(?:分钟|小时|天)前|刚刚"


def parse_video(lines, author, include_author_evidence=False):
    header = find(lines, r"^评论\s*\d*")
    if not header:
        raise ValueError("The video details drawer is not open")
    row = find(lines, r"作者", lambda line: line["box"][1] > header["box"][1]
               and "作者" in (normalize(line["text"])[len(normalize(author)):]
                              if normalize(line["text"]).startswith(normalize(author))
                              else normalize(line["text"])))
    if not row:
        raise ValueError("Video author does not match the feed author")
    # Select only the work's header, never a later comment marked 作者.
    if row["box"][1] - header["box"][1] > 180:
        raise ValueError("Original work header is outside the visible drawer")
    author_header = row["text"]
    if normalize(row["text"]).startswith("作者"):
        # A separate author tag belongs to the nickname immediately above it
        # (or to its left when OCR split a single visual row into boxes).
        names = [item for item in lines if item is not row
                 and normalize(item["text"]).startswith(normalize(author))
                 and header["box"][1] < item["box"][1] <= row["box"][1] + 10
                 and row["box"][1] - item["box"][1] <= 70
                 and item["box"][0] <= row["box"][0] + 20]
        if len(names) != 1:
            raise ValueError("Video author does not match the feed author")
        author_header = names[0]["text"] + row["text"]
    if not normalize(author_header).startswith(normalize(author)):
        raise ValueError("Video author does not match the feed author")
    metadata = normalize(author_header)[len(normalize(author)):].split("作者", 1)[1].strip()
    date = re.search(DATE, metadata)
    published = date.group(0) if date else None
    location = metadata[:date.start()].strip() if date else None
    below = sorted((line for line in lines if line["box"][1] > row["box"][1] + row["box"][3] / 2),
                   key=lambda item: (item["box"][1], item["box"][0]))
    stop = next((line for line in below if
                 re.fullmatch(r"[^\w\u4e00-\u9fff]*(?:相关)?音乐\s*", line["text"].strip())
                 or re.search(r"相关音乐|音乐\s*[·•:：]|上热门|^展开|^收起|^全文|^更多|^暂无评论|抢沙发", line["text"])), None)
    # A following comment's timestamp is also a hard boundary for the work text.
    comment = next((line for line in below if re.search(DATE, line["text"])
                    and line["box"][1] > row["box"][1] + 100), None)
    boundaries = [line["box"][1] for line in (stop, comment) if line]
    if not boundaries:
        raise ValueError("The full description boundary is not visible")
    end = min(boundaries)
    description_lines = [line for line in below if line["box"][1] < end
                         and line["box"][0] >= row["box"][0] - 15
                         and not re.search(r"已声明原创|发表评论|回复|^\d+$", line["text"])]
    description = "".join(line["text"].strip() for line in description_lines)
    if not description or description.rstrip().endswith(("…", "...")):
        raise ValueError("Description is empty or still truncated")
    if stop and re.match(r"^(展开|全文|更多)", stop["text"]):
        raise ValueError("Description needs expansion")
    result = {"authorNickname": author, "description": description,
              "publishTimeText": published, "publishLocation": location or None}
    if include_author_evidence:
        result["_authorHeader"] = author_header
    return result


def parse_author(lines, author):
    contact = find(lines, r"^(私信|客服)$")
    follow = find(lines, r"^(?:[√✓]?)\s*(已关注|关注)$")
    if not contact or not follow or abs(contact["box"][1] - follow["box"][1]) > 40:
        raise ValueError("Author profile controls are missing")
    names = [line for line in lines if normalize(line["text"]).startswith(normalize(author))
             and line["box"][1] < follow["box"][1]]
    if len(names) != 1:
        raise ValueError("Author profile name does not match")
    name = names[0]
    area = sorted((line for line in lines
                   if name["box"][1] + name["box"][3] <= line["box"][1] < follow["box"][1]),
                  key=lambda line: (line["box"][1], line["box"][0]))
    ip = find(area, r"IP\s*属地\s*[:：]?")
    certification = find(area, r"认证|企业主体|职业信息|^音乐人$")
    location = next((line for line in area
                     if line["box"][0] >= name["box"][0] - 15
                     and line["box"][1] - name["box"][1] < 110
                     and line is not ip and line is not certification), None)
    bio = [line["text"] for line in area
           if line is not ip and line is not certification and line is not location
           and not re.search(r"\d+条原创内容|\d+作品|粉丝|获赞|音乐电台.*人在听|^播放$", line["text"])]
    fields = {
        "authorNickname": name["text"].strip(),
        "authorCertification": certification["text"] if certification else None,
        "authorIpLocation": re.sub(r".*IP\s*属地\s*[:：]?\s*", "", ip["text"]) if ip else None,
        "authorProfileLocation": location["text"] if location else None,
        "authorBio": "\n".join(bio) or None,
    }
    fields["missingFields"] = [key for key in ("authorCertification", "authorIpLocation", "authorBio") if fields[key] is None]
    return fields


def parse_basic_info(lines):
    if not find(lines, r"^基础信息$"):
        raise ValueError("Author basic information is not open")

    def value_for(pattern):
        label = find(lines, pattern)
        if not label:
            return None
        x, y, width, height = label["box"]
        values = [line for line in lines if line["box"][0] > x + width
                  and abs(line["box"][1] + line["box"][3] / 2 - y - height / 2) < max(12, height / 2)]
        return "".join(line["text"].strip() for line in sorted(values, key=lambda line: line["box"][0])) or None

    channel_id = value_for(r"^视频号\s*ID$")
    if not channel_id or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", channel_id):
        raise ValueError("A valid public Channels ID was not recognized")
    return {"authorDouyinId": channel_id, "authorIdentityMethod": "wechat-channel-id",
            "authorIpLocation": value_for(r"^IP(?:归)?属地$"),
            "authorProfileLocation": value_for(r"^资料所在地$")}


def open_basic_info(ui, more):
    # Include the click's settling delay in the readiness budget. An in-flight
    # synchronous OCR cannot be interrupted, but no new attempt starts at expiry.
    deadline = time.monotonic() + 5
    ui.click(more)
    last_error = None
    while time.monotonic() < deadline:
        lines = ui.snapshot("author-basic-info")
        try:
            return parse_basic_info(lines)
        except ValueError as error:
            last_error = error
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.5, remaining))
    raise ValueError("Author basic information was not ready within 5 seconds"
                     + (f": {last_error}" if last_error else "")) from last_error


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
    if label in ("feed", "before-author", "returned-feed"):
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
    for _ in range(2):
        expand = find(details, r"^(展开|全文|更多)$")
        if not expand:
            break
        # Only expand within the work header, above the first ordinary comment.
        row = find(details, "作者")
        if not row or expand["box"][1] > row["box"][1] + 350:
            break
        ui.click(expand)
        details = ui.snapshot("expanded-description", author=author)
    return parse_video(details, author, include_author_evidence=True)


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
            if not visible_author_matches(target["text"], author):
                raise ValueError("Feed changed before opening the author")
            ui.click(target)
            author_lines = ui.snapshot("author-profile", author=author)
            try:
                profile = parse_author(author_lines, author)
            except ValueError:
                author_lines = ui.refine_profile_name(author_lines, author)
                profile = parse_author(author_lines, author)
            author = profile["authorNickname"]
            if not work_author_matches(video.pop("_authorHeader"), author):
                raise ValueError("Full profile name does not match the original work header")
            video["authorNickname"] = author
            name = find(author_lines, "^" + re.escape(author) + "$")
            ui.profile_more(name)
            menu = ui.snapshot("profile-menu")
            more = find(menu, r"^更多信息$")
            if not more:
                raise ValueError("More information menu item is missing")
            profile.update(open_basic_info(ui, more))
            profile["missingFields"] = [key for key in ("authorCertification", "authorIpLocation", "authorBio") if profile[key] is None]
            ui.back()
            ui.back()
        with timings.phase("verify_original_video"):
            feed = ui.snapshot("returned-feed")
            if not visible_author_matches(author_in_feed(feed, ui.height)["text"], author):
                raise ValueError("Returned to a different author")
            returned = video_details(ui, feed, author)
            if not work_author_matches(returned.pop("_authorHeader"), author):
                raise ValueError("Returned work author does not match the full profile name")
            if identity(author, returned["description"]) != identity(author, video["description"]):
                # Re-read the open drawer once; punctuation remains significant.
                with timings.phase("verify_description_retry"):
                    confirmed = parse_video(ui.snapshot("confirm-returned-description", author=author), author,
                                            include_author_evidence=True)
                    if not work_author_matches(confirmed.pop("_authorHeader"), author):
                        raise ValueError("Returned work author changed during confirmation")
                    if identity(author, confirmed["description"]) != identity(author, video["description"]):
                        raise ValueError("Returned to a different work; event was not saved")
            ui.back()
        with timings.phase("persist_video_draft"):
            payload = {**video, **profile, "returnVerified": True}
            payload["missingFields"] += [key for key in ("publishTimeText", "publishLocation") if not payload[key]]
            event, duplicate = store.save(payload)
            timings.emit({"kind": "videoCollected", "eventId": event["event_id"],
                          "dedupeKey": event["dedupe_key"], "duplicate": duplicate,
                          "missingFields": payload["missingFields"], "uploadStatus": "local_only"})
    return event
