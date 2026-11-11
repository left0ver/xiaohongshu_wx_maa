"""Phone-side VIDEO collection, evidence, timing and durable local drafts."""

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path

from layout import REGION, WorkHeaderMissing, label_values, line_height, profile_name, video_values, work_region
from readiness import BASIC_INFO_TIMEOUT


def normalize(text):
    # Identity ignores zero-width spaces that controls interleave with text.
    return re.sub(r"[\s\u200b-\u200d\u2060\ufeff]+", "", unicodedata.normalize("NFKC", text)).strip()


def identity(*parts):
    encoded = json.dumps([normalize(part) for part in parts], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# Message-center tabs only need to contain the word: OCR often reads an unread
# badge into the same box ("点赞+65").
MESSAGE_LIKE_TAB = r"点赞"
MESSAGE_COMMENT_TAB = r"评论"


def find(lines, pattern, predicate=lambda line: True):
    return next((line for line in sorted(lines, key=lambda item: (item["box"][1], item["box"][0]))
                 if re.search(pattern, line["text"]) and predicate(line)), None)


def _join_same_row_fragments(start, fragments):
    """OCR may split one nickname (e.g. a trailing emoji) into adjacent boxes on one row."""
    merged = dict(start)
    while True:
        x, y, w, h = merged["box"]
        center = y + h / 2
        right = min((item for item in fragments
                     if -6 <= item["box"][0] - (x + w) <= 24
                     and abs(item["box"][1] + item["box"][3] / 2 - center) <= max(h, item["box"][3]) / 2),
                    key=lambda item: item["box"][0], default=None)
        if right is None:
            return merged
        rx, ry, rw, rh = right["box"]
        top, bottom_edge = min(y, ry), max(y + h, ry + rh)
        merged = {**merged, "text": merged["text"].strip() + right["text"].strip(),
                  "box": [x, top, rx + rw - x, bottom_edge - top]}
        fragments = [item for item in fragments if item is not right]


def author_in_feed(lines, height):
    hot_rows = [line["box"][1] for line in lines if re.search(r"热评|\d+条回复", line["text"])]
    fragments = [
        line for line in lines
        if line["box"][1] > height - 150 and line["box"][0] > 65
        and not re.search(r"关注|私信|客服|热评|设置为铃声|发表评论|说点什么|写评论|\d+条回复", line["text"])
        and not any(abs(line["box"][1] - y) <= 20 for y in hot_rows)
        and not re.fullmatch(r"[√✓✔]+", line["text"].strip())
        and not re.fullmatch(r"[\d.万wW]+", line["text"])
    ]
    starts = sorted((line for line in fragments if line["box"][0] < 250),
                    key=lambda item: (item["box"][1], item["box"][0]))
    candidates, absorbed = [], []
    for start in starts:
        if any(start is item for item in absorbed):
            continue
        rest = [item for item in fragments if item is not start and not any(item is a for a in absorbed)]
        merged = _join_same_row_fragments(start, rest)
        absorbed += [item for item in rest if item["box"][0] >= start["box"][0]
                     and item["box"][0] + item["box"][2] <= merged["box"][0] + merged["box"][2]
                     and abs(item["box"][1] - start["box"][1]) <= max(start["box"][3], item["box"][3])]
        candidates.append(merged)
    if len(candidates) == 2:
        first, second = candidates
        gap = second["box"][1] - (first["box"][1] + first["box"][3])
        if abs(first["box"][0] - second["box"][0]) <= 20 and -5 <= gap <= 20:
            return {
                **first,
                "text": first["text"].strip() + second["text"].strip(),
            }
    if len(candidates) != 1:
        raise ValueError("The feed author is missing or ambiguous")
    return candidates[0]


def feed_video_preview(lines, height=1605) -> dict:
    """Extract the visible feed author and caption preview before opening drawers/profiles."""
    author_line = author_in_feed(lines, height)
    author = author_line["text"].strip()
    ay = author_line["box"][1]

    caption_lines = sorted(
        (
            l
            for l in lines
            if height - 310 <= l["box"][1] < ay - 8
            and l["box"][0] < 80
            and not re.search(
                r"热评|可能含有AI|作者提示|设置为铃声|\d+条回复|倍速|^\d{2}:\d{2}",
                l.get("text", ""),
            )
        ),
        key=lambda l: (l["box"][1], l["box"][0]),
    )
    caption = "".join(l["text"].strip() for l in caption_lines).strip()
    author_key = "wxch-author:" + identity(author)
    preview_key = "wxch-preview:" + identity(author, caption or author)
    return {
        "author": author,
        "captionPreview": caption,
        "authorKey": author_key,
        "previewKey": preview_key,
    }


def comment_control_in_feed(lines, height, ui=None):
    comment = find(lines, r"^(?:\d+(?:\.\d+)?[万wW]?|评论)$",
                   lambda line: line["box"][0] > 590 and line["box"][1] > height - 130)
    if comment:
        return comment
    if ui is not None and hasattr(ui, "ocr_snapshot"):
        roi = [580, max(0, height - 130), 140, min(130, height)]
        extra = ui.ocr_snapshot("feed-comment-control", roi)
        comment = find(extra, r"^(?:\d+(?:\.\d+)?[万wW]?|评论)$",
                       lambda line: line["box"][0] > 580 and line["box"][1] > height - 130)
        if comment:
            return comment
        bar_anchor = find(lines, r"^(?:转发|赞|喜欢|收藏|推荐|\d+(?:\.\d+)?[万wW]?)$",
                          lambda line: 340 <= line["box"][0] <= 575 and line["box"][1] > height - 110)
        if bar_anchor:
            return {"text": "1", "box": [625, max(0, bar_anchor["box"][1] - 32), 44, 52]}
    return None


def parse_video(lines, author):
    return video_values(lines, author)


def parse_author(lines, author):
    contact = find(lines, r"^(私信|客服)$")
    follow = find(lines, r"^(?:[√✓]?)\s*(已关注|申请关注|关注)$")
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
                     and (re.fullmatch(f"(?:{REGION})(?:[\u4e00-\u9fff\\s]{{0,8}}[男女市区县])?(?:\\s*已实名)?", line["text"])
                          # The profile control separates province, city and gender with spaces.
                          or re.fullmatch(f"(?:{REGION})(?:\\s+[\u4e00-\u9fff]{{1,8}}){{0,3}}\\s*", line["text"]))
                     and line is not ip and line is not certification), None)
    # Profile statistics and feature rows follow the biography. Stop at their
    # structural boundary, rather than collecting everything above Follow.
    boundary = min((line["box"][1] for line in area
                    if re.fullmatch(r"\d+(?:\.\d+)?[万wW]?\s*(?:条原创内容|条朋友圈内容|作品|粉丝|获赞)|音乐电台(?:.*人在听)?|播放|商品橱窗", line["text"].strip())),
                   default=follow["box"][1])
    bio = [line["text"] for line in area if line["box"][1] < boundary
           if line is not ip and line is not certification and line is not location]
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
    follow = find(lines, r"^(?:[√✓]?)\s*(已关注|申请关注|关注)$")
    if not follow:
        return None
    try:
        name = profile_name(lines, author)
    except ValueError:
        return None
    return find(lines, r"(?:[.…⋯·]+\s*(?:展开|全文)|^(?:展开|全文))$",
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
        deadline = time.monotonic() + BASIC_INFO_TIMEOUT
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

    def save(self, payload, source_mode="RECOMMEND", video_url=None, search_keyword=None):
        """Save a VIDEO draft in the shape agreed for the later server upload.

        The dedupe key is the 64-hex digest of nickname + description (the
        share link changes on every copy, so it cannot identify the video).
        A repeat keeps the first event ID, share link and collection time.
        """
        if not payload.get("authorNickname") or not payload.get("description"):
            raise ValueError("Cannot save an incomplete VIDEO")
        key = identity(payload["authorNickname"], payload["description"])
        payload = video_payload(payload, video_url, search_keyword)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT event_json FROM video_drafts WHERE dedupe_key=?", (key,)).fetchone()
            if previous:
                event = json.loads(previous[0])
                first_url = event["payload"].get("videoUrl")
                event["payload"] = {**payload, "videoUrl": first_url or payload["videoUrl"]}
                db.execute("UPDATE video_drafts SET event_json=? WHERE dedupe_key=?",
                           (json.dumps(event, ensure_ascii=False), key))
                return event, True
            device = db.execute("SELECT value FROM metadata WHERE key='deviceId'").fetchone()[0]
            event = {
                "event_id": str(uuid.uuid4()), "event_type": "VIDEO", "dedupe_key": key,
                "device_id": device, "source_mode": source_mode or "RECOMMEND",
                "collected_at": now_ms(), "upload_status": "local_only", "payload": payload,
            }
            # Local drafts only; the upload queue is not built yet.
            db.execute("INSERT INTO video_drafts VALUES (?, ?, ?, 'local_only', ?)",
                       (key, event["event_id"], event["collected_at"], json.dumps(event, ensure_ascii=False)))
            return event, False


PLATFORM = "wechat"


def now_ms():
    return time.time_ns() // 1_000_000


def video_payload(payload, video_url=None, search_keyword=None):
    """Upload field names: the author's Channels ID has its own field, never authorDouyinId."""
    payload = dict(payload)
    channels_id = payload.pop("authorDouyinId", None)
    if payload.get("authorIdentityMethod") != "wechat-channel-id":
        channels_id = None
    keyword = (search_keyword or "").strip() or None
    return {**payload, "platform": PLATFORM, "authorDouyinId": None,
            "author_wechat_channels_id": channels_id, "awemeId": None,
            "videoUrl": video_url or None, "searchKeyword": keyword, "matchedKeyword": keyword}


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


WORD_SEPARATORS = r"[\s,，、;；|/]+"
SHARE_LINK = re.compile(r"https://weixin\.qq\.com/sph/[A-Za-z0-9_-]{4,64}")
PASTE_KEY = 279


def filter_words(value):
    """Filter words typed by the user: separated by spaces, commas, 、 or semicolons."""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(item) for item in value)
    words = []
    for word in re.split(WORD_SEPARATORS, str(value or "")):
        if word and word not in words:
            words.append(word)
    return words


def matched_words(text, words):
    """The filter words contained in text, ignoring case, width and spacing."""
    body = normalize(text or "").lower()
    return [word for word in words if normalize(word).lower() in body]


def control(lines, pattern, predicate=lambda line: True):
    """A control read from the hierarchy (icons included), never an OCR guess."""
    return find([line for line in lines if line.get("source") == "hierarchy"], pattern, predicate)


def share_geometry(lines, height):
    """The 转发 button: its count (or label) sits under the icon in the bottom bar."""
    count = find(lines, r"^(?:\d+(?:\.\d+)?[万wW]?\+?|转发|分享)$",
                 lambda line: 420 <= line["box"][0] + line["box"][2] / 2 <= 510 and line["box"][1] > height - 115)
    if count:
        x, y, w, h = count["box"]
        return {"text": "share", "box": [x + w // 2 - 24, max(0, y - 48), 48, 44]}
    return {"text": "share", "box": [442, height - 132, 48, 48]}


def _feed_search_button(ui):
    return control(ui.controls_now(), r"^搜索$", lambda line: line["box"][1] < 200 and line["box"][0] > 480)


def _edit_box(lines):
    return next((line for line in lines if line.get("selector", {}).get("class", "").endswith("EditText")), None)


def _read_pasted_link(lines):
    box = control(lines, SHARE_LINK.pattern)
    return SHARE_LINK.search(box["text"]).group(0) if box else None


def _paste_into_search(ui, search):
    """Channels search box: paste, read, clear. Returns the link or None."""
    ui.click(search)
    time.sleep(0.8)
    ui.key(PASTE_KEY)
    time.sleep(0.6)
    link = _read_pasted_link(ui.snapshot("share-link-paste"))
    clear = control(ui.controls_now(), r"^清除搜索词$")
    if clear:
        ui.click(clear)
    return link


def _paste_into_composer(ui, lines):
    """A video opened from search results has no search button; its comment box
    takes the paste instead. The box is emptied before leaving, nothing is sent."""
    target = comment_control_in_feed(lines, ui.height, ui)
    if not target:
        return None
    ui.click(target)
    drawer = ui.snapshot("share-paste-drawer")
    box = _edit_box(drawer) or find(drawer, r"发表评论|说点什么|写评论")
    if not box:
        return None
    ui.click(box)
    time.sleep(0.6)
    ui.key(PASTE_KEY)
    time.sleep(0.6)
    link = _read_pasted_link(ui.snapshot("share-link-paste-composer"))
    # Never leave the link behind as a comment draft: WeChat keeps unsent text.
    ui.clear_text()
    if _read_pasted_link(ui.snapshot("share-link-cleared")):
        ui.clear_text()
        if _read_pasted_link(ui.snapshot("share-link-cleared")):
            raise RuntimeError("The pasted link could not be removed from the comment box")
    return link


def back_to_video(ui, author, label):
    """Back out of search (keyboard first, then the page) to the video.

    Back from Channels search always lands on the video it was opened from;
    the nickname is not compared because its OCR reading varies between frames.
    """
    from interactions import classify_page_state

    for attempt in range(4):
        lines = ui.snapshot(f"{label}-{attempt + 1}")
        # A comment drawer still shows the feed author below it; only the bare video counts.
        if not _edit_box(lines) and classify_page_state(lines, ui.height) == "channels_feed":
            return lines
        ui.back()
    raise ValueError(f"Did not return to the video of {author} after reading its share link")


def copy_share_link(ui, lines, previous_link=None):
    """转发 → 复制链接, then paste into Channels search to read the clipboard.

    WeChat makes a new short link on every copy. Only a link read from the
    controls is kept: OCR would confuse letters inside the code.
    """
    author = author_in_feed(lines, ui.height)["text"].strip()
    ui.click(share_geometry(lines, ui.height))
    sheet = ui.snapshot("share-sheet")
    for swipe in range(3):
        copy = find(sheet, r"^复制链接$")
        if copy:
            break
        second_row = find(sheet, r"^(?:浮窗|倍速播放|不感兴趣|投诉|背景声音|自动上滑|关闭弹幕)$")
        if not second_row or swipe == 2:
            ui.back()
            raise ValueError("复制链接 is not in the share sheet")
        y = second_row["box"][1] - 40
        ui.swipe(640, y, 80, y, 500)
        sheet = ui.snapshot("share-sheet-scrolled")
    ui.click(copy)
    time.sleep(0.8)
    if find(ui.snapshot("share-closed"), r"^复制链接$|^转发给朋友$"):
        ui.back()
    search = _feed_search_button(ui)
    if search:
        link = _paste_into_search(ui, search)
    else:
        link = _paste_into_composer(ui, ui.snapshot("share-feed"))
    back_to_video(ui, author, "share-return")
    if not link:
        raise ValueError("The copied share link could not be read")
    if link == previous_link:
        raise ValueError("The clipboard still holds the previous video's link")
    return link


def ensure_feed(ui, launch=True):
    if launch:
        # Earlier runs can leave several pages (chats, profiles, shared videos)
        # above WeChat's tabs; start from the tabs instead of backing out of them.
        if hasattr(ui, "start_wechat_home"):
            ui.start_wechat_home()
        else:
            ui.start_wechat()
    ready_deadline = getattr(ui, "startup_deadline", None) or time.monotonic() + 10
    waiting_for_start = launch
    navigation_steps = 0
    navigation_label = "navigation-bottom"
    while navigation_steps < 7:
        lines = ui.snapshot(navigation_label)
        is_profile_grid = navigation_label == "navigation-bottom" and any(line["box"][1] > ui.height - 42 for line in lines)
        is_comment_drawer = bool(find(lines, r"^评论\s*\d*", lambda line: not (line["box"][0] > 580 and line["box"][1] > ui.height - 130)))
        if navigation_label == "navigation-bottom" and not is_profile_grid and not is_comment_drawer and not find(lines, r"^发现$|^推荐$|^视频号$|^(私信|客服)$|^我的视频号$|^正在观看$|^发表评论$|^基础信息$"):
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
        if recommended and find(lines, r"^[<＜《‹«]?\s*关注$", lambda line: line["box"][1] < 250) and find(lines, r"^朋友", lambda line: line["box"][1] < 250):
            ui.click(recommended)
            return ui.snapshot("feed")
        discover = find(lines, r"^发现$", lambda line: line["box"][1] > ui.height - 180)
        if find(lines, r"^视频号$") and find(lines, r"^发现$", lambda line: line["box"][1] < 250):
            ui.click(find(lines, r"^视频号$"))
        elif discover and find(lines, r"^通讯录$"):
            ui.click(discover)
            navigation_label = "navigation-top"
        elif is_profile_grid or is_comment_drawer or find(lines, r"^(私信|客服)$") or find(lines, r"^我的视频号$|^正在观看$|^发表评论$|^基础信息$"):
            ui.back()
        elif len(lines) >= 3:
            # A drawn page that is not on the way to 推荐 (a video opened from a
            # share link, a chat): leave it instead of waiting for it.
            ui.back()
            navigation_label = "navigation-bottom"
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
    comment = comment_control_in_feed(lines, ui.height, ui)
    if not comment:
        raise ValueError("Comment control was not recognized")
    ui.click(comment)
    details = ui.snapshot("video-details", author=author)
    for _ in range(3):
        try:
            work_region(details)
            break
        except WorkHeaderMissing:
            if not hasattr(ui, "restore_work_header"):
                raise
            ui.restore_work_header(details)
            details = ui.snapshot("video-details", author=author)
    for _ in range(2):
        # Comments load after the drawer opens; until then nothing marks where the description ends.
        region = work_region(details)
        if region["boundary"] or region["expand"]:
            break
        time.sleep(1.0)
        details = ui.snapshot("video-details", author=author)
    for _ in range(2):
        region = work_region(details)
        expand = region["expand"]
        if not expand:
            break
        ui.click(expand)
        details = ui.snapshot("expanded-description", author=author)
    return parse_video(details, author)


def collect_current_video(ui, store, timings, lines, source_mode="RECOMMEND"):
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
        event, duplicate = store.save(payload, source_mode=source_mode)
        timings.emit({"kind": "videoCollected", "eventId": event["event_id"],
                      "dedupeKey": event["dedupe_key"], "duplicate": duplicate,
                      "missingFields": payload["missingFields"], "uploadStatus": "local_only"})
    return event


def collect(ui, store, timings, launch=True, source_mode="RECOMMEND"):
    with timings.phase("collect_video_total"):
        with timings.phase("enter_recommendations"):
            lines = ensure_feed(ui, launch)
        return collect_current_video(ui, store, timings, lines, source_mode=source_mode)
