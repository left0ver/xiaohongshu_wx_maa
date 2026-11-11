"""WeChat Channels Batch 3 interactions, profile touch, message handling, and daily quota recovery.

Implements:
- Daily quota persistence & action idempotency ledger (`DailyQuotaStore`)
- Stop checks before every UI action & dirty overlay cleanup on resume (`ensure_clean_page_on_resume`)
- Work-level interactions: thumb like (`like_work`), heart recommend/collect (`collect_work_action`),
  and primary comment posting (`post_work_comment`)
- Comment-level interactions: comment like (`like_comment`), comment reply with 3-layer self-comment
  anti-loop protection (`reply_to_comment`), and preemptive target-comment reply (`preemptive_reply_comment`)
- User profile touch (skipping DM): follow (`follow_profile_user`), first-work open/like/collect
  (`interact_profile_first_work`), and return verification to comment drawer (`visit_and_touch_commenter_profile`)
- Personal page message center inspection (`视频号消息 -> 评论`): parse notifications, in-place like & reply,
  and verified return to Channels feed (`inspect_and_handle_messages`)
"""

from datetime import datetime
import json
from pathlib import Path
import re
import time

from collection import (
    MESSAGE_COMMENT_TAB,
    MESSAGE_LIKE_TAB,
    author_in_feed,
    comment_control_in_feed,
    feed_video_preview,
    find,
    identity,
    open_basic_info,
    parse_author,
)
from comments import (
    LIKE_BADGE_RE,
    clean_comment_nickname,
    nickname_matches,
    parse_comment_drawer,
    parse_wechat_user_card,
)
from layout import (
    DATE,
    REGION,
    WorkHeaderMissing,
    compact,
    line_height,
    ocr_text_matches,
    profile_name,
    reading_rows,
    work_region,
)

DEFAULT_DAILY_LIMITS = {
    "like": 50,
    "collect": 30,
    "comment": 20,
    "follow": 20,
    "dm": 10,
}

ACTION_RECORDS = (
    "work_like",
    "work_collect",
    "work_comment",
    "comment_like",
    "comment_reply",
    "self_comment",
    "follow",
    "dm",
    "message_handled",
)

MAX_KNOWN_WORKS = 500
# A freshly opened profile keeps reflowing (bio, counters) for a moment; tapping
# 关注 off an early snapshot can land on the bio instead of the button.
PROFILE_SETTLE_SECONDS = 3


class QuotaExceededError(RuntimeError):
    """Raised when a daily interaction quota is exhausted."""

    def __init__(self, quota_type, limit, used):
        super().__init__(f"Daily quota exhausted for {quota_type}: {used}/{limit}")
        self.quota_type = quota_type
        self.limit = limit
        self.used = used


def check_stopped(ui):
    """Raise InterruptedError immediately if the tasker/UI has been asked to stop."""
    if ui is None:
        return
    if hasattr(ui, "stopped") and callable(ui.stopped) and ui.stopped():
        raise InterruptedError("Interaction was stopped")
    tasker = getattr(ui, "tasker", None)
    if tasker is not None and getattr(tasker, "stopping", False):
        raise InterruptedError("Interaction was stopped")


class DailyQuotaStore:
    """Persistent daily interaction counter and per-target idempotency ledger."""

    def __init__(self, path, limits=None, today_fn=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._today_fn = today_fn or (lambda: datetime.now().strftime("%Y-%m-%d"))
        self._explicit_limits = dict(limits or {})
        self.limits = {**DEFAULT_DAILY_LIMITS, **self._explicit_limits}
        self.state = self._load()

    def _today(self):
        return str(self._today_fn())

    def _fresh_state(self, date_str, preserved_self_comments=(), preserved_works=()):
        actions = {kind: [] for kind in ACTION_RECORDS}
        if preserved_self_comments:
            actions["self_comment"] = list(dict.fromkeys(str(x) for x in preserved_self_comments if x))
        return {
            "date": date_str,
            "limits": dict(self.limits),
            "used": {key: 0 for key in DEFAULT_DAILY_LIMITS},
            "actions": actions,
            "works": _valid_works(preserved_works),
            "replies": [],
            "updatedAt": datetime.now().isoformat(timespec="seconds"),
        }

    def _load(self):
        today = self._today()
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                prev_self = raw.get("actions", {}).get("self_comment", []) if isinstance(raw, dict) else []
                prev_works = raw.get("works", []) if isinstance(raw, dict) else []
                if isinstance(raw, dict):
                    self.limits = {**DEFAULT_DAILY_LIMITS, **(raw.get("limits") or {}), **self._explicit_limits}
                if isinstance(raw, dict) and raw.get("date") == today:
                    used = {
                        key: int(raw.get("used", {}).get(key, 0))
                        for key in DEFAULT_DAILY_LIMITS
                    }
                    actions = {
                        kind: list(dict.fromkeys(str(x) for x in raw.get("actions", {}).get(kind, [])))
                        for kind in ACTION_RECORDS
                    }
                    state = {
                        "date": today,
                        "limits": dict(self.limits),
                        "used": used,
                        "actions": actions,
                        "works": _valid_works(prev_works),
                        "replies": [r for r in raw.get("replies", []) if isinstance(r, dict)],
                        "updatedAt": raw.get("updatedAt") or datetime.now().isoformat(timespec="seconds"),
                    }
                    self._save(state)
                    return state
                state = self._fresh_state(today, preserved_self_comments=prev_self, preserved_works=prev_works)
                self._save(state)
                return state
            except (OSError, ValueError, TypeError):
                pass
        state = self._fresh_state(today)
        self._save(state)
        return state

    def _save(self, state=None):
        if state is None:
            state = self.state
        state["updatedAt"] = datetime.now().isoformat(timespec="seconds")
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.path)

    def refresh_day(self):
        today = self._today()
        if self.state.get("date") != today:
            prev_self = self.state.get("actions", {}).get("self_comment", [])
            self.state = self._fresh_state(
                today, preserved_self_comments=prev_self, preserved_works=self.state.get("works", [])
            )
            self._save()

    def remaining(self, quota_type):
        self.refresh_day()
        limit = int(self.state["limits"].get(quota_type, DEFAULT_DAILY_LIMITS.get(quota_type, 0)))
        used = int(self.state["used"].get(quota_type, 0))
        return max(0, limit - used)

    def has_quota(self, quota_type, amount=1):
        return self.remaining(quota_type) >= amount

    def require_quota(self, quota_type, amount=1):
        self.refresh_day()
        limit = int(self.state["limits"].get(quota_type, DEFAULT_DAILY_LIMITS.get(quota_type, 0)))
        used = int(self.state["used"].get(quota_type, 0))
        if used + amount > limit:
            raise QuotaExceededError(quota_type, limit, used)

    def has_acted(self, action_kind, target_key):
        self.refresh_day()
        if not target_key:
            return False
        return str(target_key) in self.state["actions"].get(action_kind, ())

    def mark_action(self, action_kind, target_key):
        self.refresh_day()
        if not target_key:
            return
        bucket = self.state["actions"].setdefault(action_kind, [])
        key_str = str(target_key)
        if key_str not in bucket:
            bucket.append(key_str)
            self._save()

    def record(self, quota_type, action_kind, target_key=None, amount=1, extra_records=()):
        self.require_quota(quota_type, amount=amount)
        self.state["used"][quota_type] = int(self.state["used"].get(quota_type, 0)) + amount
        if action_kind and target_key:
            bucket = self.state["actions"].setdefault(action_kind, [])
            key_str = str(target_key)
            if key_str not in bucket:
                bucket.append(key_str)
        for extra_kind, extra_key in extra_records:
            if extra_kind and extra_key:
                bucket = self.state["actions"].setdefault(extra_kind, [])
                extra_str = str(extra_key)
                if extra_str not in bucket:
                    bucket.append(extra_str)
        self._save()

    def resolve_work_key(self, author, caption=""):
        """Reuse the key of a known work whose caption is an OCR variant of this one."""
        self.refresh_day()
        author = (author or "").strip()
        caption = (caption or "").strip()
        if not author:
            raise ValueError("author is required to identify a work")
        works = self.state.setdefault("works", [])
        for work in works:
            if (nickname_matches(work["author"], author)
                    and ocr_text_matches(work["caption"], caption, allow_truncation=True)):
                return work["key"]
        key = compute_work_key(author=author, title=caption)
        works.append({"author": author, "caption": caption, "key": key})
        del works[:-MAX_KNOWN_WORKS]
        self._save()
        return key

    def has_replied(self, work_key, nickname, content, text):
        """Match a reply target by OCR similarity: its key changes with OCR noise and thread parsing."""
        self.refresh_day()
        return any(
            r.get("workKey") == work_key
            and compact(r.get("text", "")) == compact(text)
            and nickname_matches(r.get("nickname", ""), nickname)
            and ocr_text_matches(r.get("content", ""), content)
            for r in self.state.get("replies", ())
        )

    def remember_reply(self, work_key, nickname, content, text):
        self.refresh_day()
        if self.has_replied(work_key, nickname, content, text):
            return
        self.state.setdefault("replies", []).append(
            {"workKey": work_key, "nickname": nickname, "content": content, "text": text}
        )
        self._save()

    def summary(self):
        self.refresh_day()
        return {
            "date": self.state["date"],
            "limits": dict(self.state["limits"]),
            "used": dict(self.state["used"]),
            "remaining": {k: self.remaining(k) for k in DEFAULT_DAILY_LIMITS},
            "actionCounts": {k: len(v) for k, v in self.state["actions"].items()},
        }


def parse_metric_number(text):
    """Parse Chinese metric count strings ('4521', '1.2万', '赞', '喜欢') into integers."""
    raw = compact(str(text or ""))
    if not raw or re.fullmatch(r"[赞喜欢收藏评论转发推荐凸👍♡♥]+", raw):
        return 0
    raw = re.sub(r"[赞喜欢收藏评论转发推荐凸👍♡♥]+$", "", raw)
    match = re.search(r"(\d+(?:\.\d+)?)\s*([万wW])?", raw)
    if not match:
        return 0
    value = float(match[1])
    if match[2]:
        value *= 10000
    return int(round(value))


def is_red_active(frame, box, min_ratio=0.012, min_pixels=10):
    """Detect active red/orange-red state on BGR numpy frames or nested Python test frames."""
    if frame is None or not box or len(box) != 4:
        return False, 0.0
    x, y, w, h = [int(round(v)) for v in box]
    if w <= 0 or h <= 0:
        return False, 0.0

    # Case 1: numpy ndarray (H, W, 3) BGR
    if hasattr(frame, "shape") and hasattr(frame, "dtype"):
        fh, fw = int(frame.shape[0]), int(frame.shape[1])
        x0 = max(0, min(fw, x))
        x1 = max(0, min(fw, x + w))
        y0 = max(0, min(fh, y))
        y1 = max(0, min(fh, y + h))
        if x1 <= x0 or y1 <= y0:
            return False, 0.0
        patch = frame[y0:y1, x0:x1]
        total = int((y1 - y0) * (x1 - x0))
        b = patch[:, :, 0].astype("int16")
        g = patch[:, :, 1].astype("int16")
        r = patch[:, :, 2].astype("int16")
        red_mask = (r > 180) & (g < 115) & (b < 115) & ((r - g) > 65) & ((r - b) > 65)
        red_count = int(red_mask.sum())
        ratio = red_count / float(total) if total > 0 else 0.0
        return (red_count >= min_pixels and ratio >= min_ratio), round(ratio, 4)

    # Case 2: nested Python sequence frame[y][x] = (b, g, r) for host unit tests
    if isinstance(frame, (list, tuple)) and frame and isinstance(frame[0], (list, tuple)):
        fh = len(frame)
        fw = len(frame[0])
        x0 = max(0, min(fw, x))
        x1 = max(0, min(fw, x + w))
        y0 = max(0, min(fh, y))
        y1 = max(0, min(fh, y + h))
        if x1 <= x0 or y1 <= y0:
            return False, 0.0
        red_count = 0
        total = (y1 - y0) * (x1 - x0)
        for row in frame[y0:y1]:
            for pixel in row[x0:x1]:
                b, g, r = int(pixel[0]), int(pixel[1]), int(pixel[2])
                if r > 180 and g < 115 and b < 115 and (r - g) > 65 and (r - b) > 65:
                    red_count += 1
        ratio = red_count / float(total) if total > 0 else 0.0
        return (red_count >= min_pixels and ratio >= min_ratio), round(ratio, 4)

    return False, 0.0


def _valid_works(works):
    return [
        {"author": str(w["author"]), "caption": str(w.get("caption") or ""), "key": str(w["key"])}
        for w in works or ()
        if isinstance(w, dict) and w.get("author") and w.get("key")
    ][-MAX_KNOWN_WORKS:]


def compute_work_key(video_event=None, author="", title=""):
    """Return a deterministic work key for quota and idempotency tracking."""
    if isinstance(video_event, dict):
        if video_event.get("dedupe_key"):
            return str(video_event["dedupe_key"])
        payload = video_event.get("payload") or {}
        author = author or payload.get("authorNickname") or ""
        title = title or payload.get("description") or ""
    return "wxch-video:" + identity(author or "unknown", title or author or "untitled")


def comment_identity_key(work_key, nickname, content):
    """Return a deterministic key identifying a comment within a work."""
    clean_nick, _ = clean_comment_nickname(nickname or "")
    return "wxch-comment-act:" + identity(work_key or "*", clean_nick, content or "")


def is_self_comment(comment, self_nickname="", quota_store=None, work_key=""):
    """3-layer check preventing the agent from replying to its own comments."""
    if not comment:
        return False
    if comment.get("_isSelf") or comment.get("ipLocation") == "我":
        return True
    nick, _ = clean_comment_nickname(comment.get("nickname") or "")
    clean_self, _ = clean_comment_nickname(self_nickname or "")
    if clean_self and nick and (compact(clean_self) == compact(nick) or nickname_matches(clean_self, nick)):
        return True
    if quota_store is not None:
        c_key = comment.get("key")
        if c_key and quota_store.has_acted("self_comment", c_key):
            return True
        act_key = comment_identity_key(work_key, nick, comment.get("content") or "")
        if quota_store.has_acted("self_comment", act_key):
            return True
        global_key = comment_identity_key("*", "*", comment.get("content") or "")
        if quota_store.has_acted("self_comment", global_key):
            return True
    return False


def classify_page_state(lines, height=1605):
    """Classify the current WeChat Channels page or dirty overlay state."""
    if not lines:
        return "unknown"

    send_btn = find(
        lines,
        r"^(?:发送|回复)$",
        lambda line: line["box"][0] > 540 and 160 < line["box"][1] < height - 35,
    )
    if send_btn:
        return "composer_overlay"

    if find(lines, r"^基础信息$"):
        return "basic_info_overlay"

    if find(lines, r"^更多信息$") and find(lines, r"^(?:投诉|不感兴趣|举报)$"):
        return "menu_overlay"

    if find(lines, r"^转发给朋友$") or (find(lines, r"^朋友圈$") and find(lines, r"^收藏$")):
        return "share_overlay"

    if (
        find(lines, MESSAGE_LIKE_TAB, lambda l: l["box"][1] < 160)
        and find(lines, MESSAGE_COMMENT_TAB, lambda l: l["box"][1] < 160)
    ):
        return "channels_message_center"

    if find(lines, r"^(?:视频号消息|赞、收藏和最近看过|我的视频号)$"):
        return "personal_page"

    if find(lines, r"^(?:[√✓]?)\s*(?:已关注|互相关注|申请关注|关注)$") and find(lines, r"^(?:私信|客服)$"):
        return "user_profile"

    if find(lines, r"^设置$", lambda l: l["box"][0] > 520 and l["box"][1] > height * 0.55):
        return "user_card_overlay"

    if find(
        lines,
        r"^评论\s*[\d.万wW]*",
        lambda l: not (l["box"][0] > 560 and l["box"][1] > height - 140),
    ) or find(
        lines,
        r"发表评论|说点什么|写评论",
        lambda l: l["box"][1] >= height - 120,
    ):
        return "comment_drawer"

    try:
        author_in_feed(lines, height)
        return "channels_feed"
    except ValueError:
        pass

    if (
        find(lines, r"^推荐$", lambda l: l["box"][1] < 220)
        and find(lines, r"^关注$", lambda l: l["box"][1] < 220)
    ) or find(lines, r"^(?:转发|赞|收藏)$", lambda l: l["box"][1] > height - 120):
        return "channels_feed"

    grid_counts = [
        l
        for l in lines
        if re.fullmatch(r"凸?\s*\d+(?:\.\d+)?[万wW]?", l["text"].strip())
        and 300 < l["box"][1] < height - 120
    ]
    grid_cols = {0 if l["box"][0] < 240 else (1 if l["box"][0] < 480 else 2) for l in grid_counts}
    grid_rows = {l["box"][1] // 120 for l in grid_counts}
    if len(grid_counts) >= 3 and len(grid_cols) >= 2 and len(grid_rows) >= 2:
        return "user_profile"

    return "unknown"


def ensure_clean_page_on_resume(ui, max_steps=3):
    """Dismiss leftover composer, menu, basic-info, or user-card overlays before starting."""
    check_stopped(ui)
    lines = ui.snapshot("resume-check")
    initial_state = classify_page_state(lines, ui.height)
    dirty_states = {
        "composer_overlay",
        "basic_info_overlay",
        "menu_overlay",
        "share_overlay",
        "user_card_overlay",
    }
    dismissed = 0
    current_state = initial_state
    while current_state in dirty_states and dismissed < max_steps:
        check_stopped(ui)
        ui.back()
        dismissed += 1
        lines = ui.snapshot(f"resume-clean-{dismissed}")
        current_state = classify_page_state(lines, ui.height)
    return {
        "initialState": initial_state,
        "finalState": current_state,
        "dismissedCount": dismissed,
        "clean": current_state not in dirty_states,
        "lines": lines,
    }


def _feed_like_geometry(lines, height):
    count_line = find(
        lines,
        r"^(?:\d+(?:\.\d+)?[万wW]?|赞|喜欢)$",
        lambda line: 325 <= line["box"][0] <= 415 and line["box"][1] > height - 115,
    )
    if count_line:
        cx, cy, cw, ch = count_line["box"]
        icon_roi = [max(335, cx - 12), max(0, cy - 52), 56, 48]
        click_box = [max(342, cx - 6), max(0, cy - 48), 48, 44]
        count_text = count_line["text"].strip()
    else:
        icon_roi = [345, height - 135, 55, 50]
        click_box = [348, height - 132, 48, 48]
        count_text = ""
    return {
        "iconRoi": icon_roi,
        "clickBox": click_box,
        "countText": count_text,
        "count": parse_metric_number(count_text),
    }


def _feed_collect_geometry(lines, height):
    count_line = find(
        lines,
        r"^(?:\d+(?:\.\d+)?[万wW]?|赞|收藏|推荐)$",
        lambda line: 515 <= line["box"][0] <= 605 and line["box"][1] > height - 115,
    )
    if count_line:
        cx, cy, cw, ch = count_line["box"]
        icon_roi = [max(528, cx - 12), max(0, cy - 52), 56, 48]
        click_box = [max(535, cx - 6), max(0, cy - 48), 48, 44]
        count_text = count_line["text"].strip()
    else:
        icon_roi = [535, height - 135, 55, 50]
        click_box = [540, height - 132, 48, 48]
        count_text = ""
    return {
        "iconRoi": icon_roi,
        "clickBox": click_box,
        "countText": count_text,
        "count": parse_metric_number(count_text),
    }


def work_like_state(lines, frame, height):
    geom = _feed_like_geometry(lines, height)
    red_active, red_ratio = is_red_active(frame, geom["iconRoi"])
    return {
        **geom,
        "liked": red_active,
        "redRatio": red_ratio,
    }


def work_collect_state(lines, frame, height):
    geom = _feed_collect_geometry(lines, height)
    red_active, red_ratio = is_red_active(frame, geom["iconRoi"])
    return {
        **geom,
        "collected": red_active,
        "redRatio": red_ratio,
    }


def _ensure_on_feed_for_work_bar(ui, lines=None):
    check_stopped(ui)
    if lines is None:
        lines = ui.snapshot("feed")
    for _ in range(3):
        state = classify_page_state(lines, ui.height)
        if state not in ("comment_drawer", "composer_overlay", "user_profile", "user_card_overlay", "basic_info_overlay", "menu_overlay"):
            break
        ui.back()
        lines = ui.snapshot("feed")
    return lines


def like_work(ui, quota_store, work_key, lines=None):
    """Thumb-like a WeChat Channels work with anti-unlike and red-pixel/count verification."""
    check_stopped(ui)
    lines = _ensure_on_feed_for_work_bar(ui, lines)
    frame = ui.capture_frame() if hasattr(ui, "capture_frame") else None
    before = work_like_state(lines, frame, ui.height)

    if quota_store.has_acted("work_like", work_key) or before["liked"]:
        if not quota_store.has_acted("work_like", work_key):
            quota_store.mark_action("work_like", work_key)
        return {
            "action": "work_like",
            "status": "already_liked",
            "clicked": False,
            "verified": True,
            "workKey": work_key,
            "beforeCount": before["count"],
            "afterCount": before["count"],
            "redRatio": before["redRatio"],
        }

    quota_store.require_quota("like")
    check_stopped(ui)
    ui.click({"text": "like_work", "box": before["clickBox"]})
    after_lines = ui.snapshot("feed-after-like")
    after_frame = ui.capture_frame() if hasattr(ui, "capture_frame") else None
    after = work_like_state(after_lines, after_frame, ui.height)
    verified = bool(after["liked"] or after["count"] > before["count"])
    if not verified:
        raise RuntimeError("Work thumb-like state did not turn active after click")
    quota_store.record("like", "work_like", work_key)
    return {
        "action": "work_like",
        "status": "performed",
        "clicked": True,
        "verified": True,
        "workKey": work_key,
        "beforeCount": before["count"],
        "afterCount": after["count"],
        "redRatio": after["redRatio"],
    }


def collect_work_action(ui, quota_store, work_key, lines=None):
    """Heart recommend/collect a WeChat Channels work with anti-uncollect verification."""
    check_stopped(ui)
    lines = _ensure_on_feed_for_work_bar(ui, lines)
    frame = ui.capture_frame() if hasattr(ui, "capture_frame") else None
    before = work_collect_state(lines, frame, ui.height)

    if quota_store.has_acted("work_collect", work_key) or before["collected"]:
        if not quota_store.has_acted("work_collect", work_key):
            quota_store.mark_action("work_collect", work_key)
        return {
            "action": "work_collect",
            "status": "already_collected",
            "clicked": False,
            "verified": True,
            "workKey": work_key,
            "beforeCount": before["count"],
            "afterCount": before["count"],
            "redRatio": before["redRatio"],
        }

    quota_store.require_quota("collect")
    check_stopped(ui)
    ui.click({"text": "collect_work", "box": before["clickBox"]})
    after_lines = ui.snapshot("feed-after-collect")
    after_frame = ui.capture_frame() if hasattr(ui, "capture_frame") else None
    after = work_collect_state(after_lines, after_frame, ui.height)
    verified = bool(after["collected"] or after["count"] > before["count"])
    if not verified:
        raise RuntimeError("Work heart recommend/collect state did not turn active after click")
    quota_store.record("collect", "work_collect", work_key)
    return {
        "action": "work_collect",
        "status": "performed",
        "clicked": True,
        "verified": True,
        "workKey": work_key,
        "beforeCount": before["count"],
        "afterCount": after["count"],
        "redRatio": after["redRatio"],
    }


def _open_comment_drawer_if_needed(ui, lines=None):
    check_stopped(ui)
    if lines is None:
        lines = ui.snapshot("comments-check")
    if find(lines, r"^评论\s*[\d.万wW]*"):
        return lines, False
    feed_lines = _ensure_on_feed_for_work_bar(ui, lines)
    control = comment_control_in_feed(feed_lines, ui.height, ui)
    if control is None:
        raise ValueError("Comment control was not recognized in feed")
    check_stopped(ui)
    ui.click(control)
    drawer_lines = ui.snapshot("comments")
    if not find(drawer_lines, r"^评论\s*[\d.万wW]*"):
        raise RuntimeError("Comment drawer did not open")
    return drawer_lines, True


def _find_composer_send_button(lines, height):
    btn = find(
        lines,
        r"^(?:发送|回复)$",
        lambda line: line["box"][0] > 530 and 160 < line["box"][1] < height - 30,
    )
    if btn:
        return btn
    prompt = find(
        lines,
        r"发表评论|说点什么|写评论|回复\s*[^:：\n]+[:：]",
        lambda line: 160 < line["box"][1] < height - 30,
    )
    if prompt:
        py = prompt["box"][1]
        return {"text": "发送", "box": [592, max(180, py - 6), 106, 52]}
    return {"text": "发送", "box": [592, height - 95, 106, 52]}


def post_work_comment(
    ui,
    quota_store,
    work_key,
    comment_text,
    self_nickname="",
    lines=None,
    close_drawer=False,
):
    """Post a first-level comment on a WeChat Channels work and record self-comment identity."""
    check_stopped(ui)
    text = (comment_text or "").strip()
    if not text:
        raise ValueError("comment_text must not be empty")

    target_key = f"{work_key}:comment:{identity(text)}"
    if quota_store.has_acted("work_comment", target_key):
        return {
            "action": "work_comment",
            "status": "already_commented",
            "clicked": False,
            "submitted": False,
            "verified": True,
            "workKey": work_key,
            "commentText": text,
        }

    quota_store.require_quota("comment")
    drawer_lines, opened_here = _open_comment_drawer_if_needed(ui, lines)
    drawer = parse_comment_drawer(drawer_lines, ui.height, parent_event_id=work_key)
    detected_self = drawer.get("selfNickname") or self_nickname or ""
    input_bar = drawer.get("inputBar") or {
        "text": "发表评论",
        "box": [60, ui.height - 85, 300, 48],
    }

    check_stopped(ui)
    ui.click(input_bar)
    composer_lines = ui.snapshot("comments-composer")
    check_stopped(ui)
    ui.set_text(text)
    typed_lines = ui.snapshot("comments-typed")
    send_btn = _find_composer_send_button(typed_lines, ui.height)
    check_stopped(ui)
    ui.click(send_btn)

    after_lines = ui.snapshot("comments-after-post")
    after_drawer = parse_comment_drawer(after_lines, ui.height, parent_event_id=work_key)
    detected_self = after_drawer.get("selfNickname") or detected_self

    posted_comment = next(
        (
            c
            for c in after_drawer["comments"]
            if compact(text) in compact(c.get("content", ""))
            or (c.get("_isSelf") and compact(c.get("content", "")) == compact(text))
        ),
        None,
    )
    count_increased = (
        drawer.get("declaredTotal") is not None
        and after_drawer.get("declaredTotal") is not None
        and after_drawer["declaredTotal"] > drawer["declaredTotal"]
    )
    composer_closed = classify_page_state(after_lines, ui.height) == "comment_drawer"
    verified = bool(posted_comment is not None or count_increased or composer_closed)
    if not verified:
        raise RuntimeError("Primary comment submission could not be verified")

    extra = [
        ("self_comment", comment_identity_key(work_key, detected_self, text)),
        ("self_comment", comment_identity_key("*", "*", text)),
    ]
    if posted_comment and posted_comment.get("key"):
        extra.append(("self_comment", posted_comment["key"]))
    quota_store.record("comment", "work_comment", target_key, extra_records=extra)

    if close_drawer and opened_here:
        check_stopped(ui)
        ui.back()

    return {
        "action": "work_comment",
        "status": "performed",
        "clicked": True,
        "submitted": True,
        "verified": True,
        "workKey": work_key,
        "commentText": text,
        "selfNickname": detected_self or None,
        "postedCommentKey": posted_comment["key"] if posted_comment else None,
        "beforeTotal": drawer.get("declaredTotal"),
        "afterTotal": after_drawer.get("declaredTotal"),
    }


def _comment_matches_spec(comment, target_spec):
    if not target_spec:
        return True
    if target_spec.get("commentKey") and comment.get("key") == target_spec["commentKey"]:
        return True
    want_nick = target_spec.get("nickname")
    want_content = target_spec.get("content")
    nick_ok = True
    content_ok = True
    if want_nick:
        nick_ok = nickname_matches(want_nick, comment.get("nickname", ""))
    if want_content:
        content_ok = compact(want_content) in compact(comment.get("content", ""))
    return bool(nick_ok and content_ok and (want_nick or want_content))


def _reread_if_scroll_skipped(ui, previous_drawer, lines, work_key, label):
    """A scroll that shares no comment with the previous page may have skipped some."""
    previous = [c["nickname"] for c in previous_drawer.get("comments", [])]
    current = parse_comment_drawer(lines, ui.height, parent_event_id=work_key)
    # Compare nicknames, not keys: the same comment's body OCR varies between frames.
    if (not previous or not current["comments"] or current.get("empty")
            or any(nickname_matches(a, c["nickname"]) for a in previous for c in current["comments"])
            or not hasattr(ui, "scroll_comments_back")):
        return lines
    check_stopped(ui)
    ui.scroll_comments_back(lines)
    return ui.snapshot(label)


def locate_target_comment(
    ui,
    target_spec=None,
    self_nickname="",
    quota_store=None,
    work_key="",
    exclude_self=False,
    max_pages=10,
    lines=None,
):
    """Locate a target comment in the comment drawer across up to max_pages."""
    drawer_lines, _ = _open_comment_drawer_if_needed(ui, lines)
    detected_self = self_nickname or ""

    for page_index in range(max(1, max_pages)):
        check_stopped(ui)
        drawer = parse_comment_drawer(drawer_lines, ui.height, parent_event_id=work_key)
        if drawer.get("selfNickname"):
            detected_self = drawer["selfNickname"]

        candidates = [
            c for c in drawer["comments"] if _comment_matches_spec(c, target_spec)
        ]
        if candidates:
            non_self = [
                c
                for c in candidates
                if not is_self_comment(c, detected_self, quota_store=quota_store, work_key=work_key)
            ]
            if exclude_self and not non_self:
                return {
                    "found": False,
                    "rejectedSelf": True,
                    "comment": candidates[0],
                    "lines": drawer_lines,
                    "drawer": drawer,
                    "selfNickname": detected_self,
                    "pagesSearched": page_index + 1,
                }
            chosen = non_self[0] if exclude_self else candidates[0]
            return {
                "found": True,
                "rejectedSelf": False,
                "comment": chosen,
                "lines": drawer_lines,
                "drawer": drawer,
                "selfNickname": detected_self,
                "pagesSearched": page_index + 1,
            }

        if (
            page_index + 1 >= max_pages
            or drawer.get("footerReached")
            or drawer.get("empty")
            or not hasattr(ui, "scroll_comments")
        ):
            break
        check_stopped(ui)
        ui.scroll_comments(drawer_lines)
        drawer_lines = ui.snapshot(f"comments-locate-{page_index + 2}")
        drawer_lines = _reread_if_scroll_skipped(ui, drawer, drawer_lines, work_key, f"comments-locate-{page_index + 2}-back")

    return {
        "found": False,
        "rejectedSelf": False,
        "comment": None,
        "lines": drawer_lines,
        "drawer": parse_comment_drawer(drawer_lines, ui.height, parent_event_id=work_key),
        "selfNickname": detected_self,
        "pagesSearched": max_pages,
    }


def comment_like_state(comment, frame, quota_store=None, work_key=""):
    c_key = comment.get("key") or comment_identity_key(
        work_key, comment.get("nickname", ""), comment.get("content", "")
    )
    red_active, red_ratio = is_red_active(frame, comment.get("_likeBox"))
    already_recorded = quota_store.has_acted("comment_like", c_key) if quota_store else False
    liked = bool(comment.get("_liked") or red_active or already_recorded)
    return {
        "commentKey": c_key,
        "liked": liked,
        "badgeLiked": bool(comment.get("_liked")),
        "redActive": red_active,
        "redRatio": red_ratio,
        "count": parse_metric_number(comment.get("_likeCountText", "")),
        "likeBox": comment.get("_likeBox"),
    }


def like_comment(ui, quota_store, work_key, target_comment=None, lines=None):
    """Like a specific comment in the drawer with anti-unlike and red/badge verification."""
    check_stopped(ui)
    exclude_self = not bool(isinstance(target_comment, dict) and target_comment.get("allowSelf"))
    located = locate_target_comment(
        ui,
        target_spec=target_comment,
        quota_store=quota_store,
        work_key=work_key,
        exclude_self=exclude_self,
        lines=lines,
    )
    if not located["found"] or not located["comment"]:
        return {
            "action": "comment_like",
            "status": "target_not_found",
            "clicked": False,
            "verified": False,
            "workKey": work_key,
        }

    comment = located["comment"]
    frame = ui.capture_frame() if hasattr(ui, "capture_frame") else None
    before = comment_like_state(comment, frame, quota_store=quota_store, work_key=work_key)
    c_key = before["commentKey"]

    if before["liked"]:
        if not quota_store.has_acted("comment_like", c_key):
            quota_store.mark_action("comment_like", c_key)
        return {
            "action": "comment_like",
            "status": "already_liked",
            "clicked": False,
            "verified": True,
            "workKey": work_key,
            "commentKey": c_key,
            "nickname": comment["nickname"],
            "beforeCount": before["count"],
            "afterCount": before["count"],
        }

    quota_store.require_quota("like")
    check_stopped(ui)
    ui.click({"text": "like_comment", "box": comment["_likeBox"]})
    # A liked comment gains a 推荐 button that moves its count and thumb left,
    # so the state is read where the fresh page puts them, not at the old box.
    updated = None
    for attempt in range(2):
        after_lines = ui.snapshot("comments-after-like")
        after_frame = ui.capture_frame() if hasattr(ui, "capture_frame") else None
        after_drawer = parse_comment_drawer(after_lines, ui.height, parent_event_id=work_key)
        updated = next(
            (
                c
                for c in after_drawer["comments"]
                if c["key"] == comment["key"] or nickname_matches(comment["nickname"], c["nickname"])
            ),
            None,
        )
        if updated is not None:
            break
        time.sleep(0.5)
    updated = updated or comment
    after = comment_like_state(updated, after_frame, quota_store=None, work_key=work_key)
    verified = bool(after["badgeLiked"] or after["redActive"] or after["count"] > before["count"])
    if not verified:
        raise RuntimeError(f"Comment like state did not turn active for {comment['nickname']}")
    quota_store.record("like", "comment_like", c_key)
    return {
        "action": "comment_like",
        "status": "performed",
        "clicked": True,
        "verified": True,
        "workKey": work_key,
        "commentKey": c_key,
        "nickname": comment["nickname"],
        "beforeCount": before["count"],
        "afterCount": after["count"],
        "redRatio": after["redRatio"],
    }


def _visible_own_reply(drawer, comment, text):
    """Our earlier reply to this comment is on screen in the same thread."""
    thread = comment.get("parentCommentKey") or comment.get("key")
    want = compact(text)
    return bool(want) and any(
        c.get("_isSelf")
        and c.get("parentCommentKey")
        and c["parentCommentKey"] == thread
        and want in compact(c.get("content", ""))
        for c in drawer.get("comments", ())
    )


def reply_to_comment(
    ui,
    quota_store,
    work_key,
    reply_text,
    target_comment=None,
    self_nickname="",
    lines=None,
    max_comment_pages=10,
):
    """Reply to a target comment with 3-layer self-comment protection."""
    check_stopped(ui)
    text = (reply_text or "").strip()
    if not text:
        raise ValueError("reply_text must not be empty")

    located = locate_target_comment(
        ui,
        target_spec=target_comment,
        self_nickname=self_nickname,
        quota_store=quota_store,
        work_key=work_key,
        exclude_self=True,
        lines=lines,
        max_pages=max_comment_pages,
    )
    if located.get("rejectedSelf"):
        self_c = located.get("comment") or {}
        return {
            "action": "comment_reply",
            "status": "self_comment_skipped",
            "clicked": False,
            "submitted": False,
            "verified": True,
            "workKey": work_key,
            "nickname": self_c.get("nickname"),
            "reason": "matched_self_comment",
        }
    if not located["found"] or not located["comment"]:
        return {
            "action": "comment_reply",
            "status": "target_not_found",
            "clicked": False,
            "submitted": False,
            "verified": False,
            "workKey": work_key,
        }

    comment = located["comment"]
    detected_self = located.get("selfNickname") or self_nickname or ""
    c_key = comment.get("key") or comment_identity_key(work_key, comment["nickname"], comment["content"])
    reply_key = f"{c_key}:reply:{identity(text)}"

    already = None
    if (quota_store.has_acted("comment_reply", reply_key)
            or quota_store.has_replied(work_key, comment["nickname"], comment["content"], text)):
        already = "ledger"
    elif _visible_own_reply(located.get("drawer") or {}, comment, text):
        already = "visible_in_drawer"
    if already:
        quota_store.mark_action("comment_reply", reply_key)
        quota_store.remember_reply(work_key, comment["nickname"], comment["content"], text)
        return {
            "action": "comment_reply",
            "status": "already_replied",
            "clicked": False,
            "submitted": False,
            "verified": True,
            "workKey": work_key,
            "commentKey": c_key,
            "nickname": comment["nickname"],
            "reason": already,
        }

    quota_store.require_quota("comment")
    body_box = comment.get("_bodyBox") or [
        comment["_nameBox"][0],
        comment.get("_headerTop", 300) + 34,
        220,
        32,
    ]
    check_stopped(ui)
    # The middle of a comment can contain a tappable search term. Target its
    # leading text instead of the center of the full OCR row.
    reply_box = [body_box[0], body_box[1], min(32, body_box[2]), body_box[3]]
    ui.click({"text": comment["content"], "box": reply_box})
    composer_lines = ui.snapshot("comments-reply-composer")

    # Layer 3 self-comment defense: tapping our own comment in WeChat Channels
    # does NOT open a '回复 <nickname>:' composer.
    reply_prompt = find(
        composer_lines,
        r"回复\s*([^:：\n]+)[:：]",
        lambda line: line["box"][1] > 160,
    )
    reply_btn = find(
        composer_lines,
        r"^(?:回复|发送)$",
        lambda line: line["box"][0] > 530 and 160 < line["box"][1] < ui.height - 110,
    )
    if not reply_prompt and not reply_btn:
        if classify_page_state(composer_lines, ui.height) != "comment_drawer":
            raise RuntimeError("Reply composer did not open; unexpected page is not evidence of a self-comment")
        quota_store.mark_action("self_comment", c_key)
        return {
            "action": "comment_reply",
            "status": "self_comment_skipped",
            "clicked": True,
            "submitted": False,
            "verified": True,
            "workKey": work_key,
            "commentKey": c_key,
            "nickname": comment["nickname"],
            "reason": "composer_not_opened_self_comment",
        }

    check_stopped(ui)
    ui.set_text(text)
    typed_lines = ui.snapshot("comments-reply-typed")
    send_btn = _find_composer_send_button(typed_lines, ui.height)
    check_stopped(ui)
    ui.click(send_btn)

    after_lines = ui.snapshot("comments-after-reply")
    after_drawer = parse_comment_drawer(after_lines, ui.height, parent_event_id=work_key)
    detected_self = after_drawer.get("selfNickname") or detected_self

    posted_reply = next(
        (
            c
            for c in after_drawer["comments"]
            if compact(text) in compact(c.get("content", ""))
        ),
        None,
    )
    composer_closed = classify_page_state(after_lines, ui.height) == "comment_drawer"
    if not (posted_reply is not None or composer_closed):
        raise RuntimeError(f"Comment reply submission could not be verified for {comment['nickname']}")

    extra = [
        ("self_comment", comment_identity_key(work_key, detected_self, text)),
        ("self_comment", comment_identity_key("*", "*", text)),
    ]
    if posted_reply and posted_reply.get("key"):
        extra.append(("self_comment", posted_reply["key"]))
    quota_store.record("comment", "comment_reply", reply_key, extra_records=extra)
    quota_store.remember_reply(work_key, comment["nickname"], comment["content"], text)

    return {
        "action": "comment_reply",
        "status": "performed",
        "clicked": True,
        "submitted": True,
        "verified": True,
        "workKey": work_key,
        "commentKey": c_key,
        "nickname": comment["nickname"],
        "replyText": text,
        "postedReplyKey": posted_reply["key"] if posted_reply else None,
    }


def _work_body_text(region):
    return "".join(item["text"].strip() for row in region["body"] for item in row["items"]
                   if not re.search(r"已声明原创|发表评论", item["text"])
                   and not re.fullmatch(r"收起", item["text"].strip()))


def _caption_from_drawer(ui, lines):
    """The feed may show a hot comment instead of the caption; the drawer header always has it."""
    drawer_lines, opened = _open_comment_drawer_if_needed(ui, lines)
    try:
        caption = _work_body_text(work_region(drawer_lines))
    except ValueError:
        caption = ""
    if opened:
        check_stopped(ui)
        ui.back()
    return caption


def _work_matches_spec(ui, lines, target_work):
    if not target_work:
        return False, "", None
    want_author = (target_work.get("author") or "").strip()
    want_title = (target_work.get("title") or "").strip()
    want_key = (target_work.get("dedupeKey") or target_work.get("workKey") or "").strip()
    if not (want_author or want_title or want_key):
        return False, "", None

    state = classify_page_state(lines, ui.height)
    drawer_open = state == "comment_drawer"
    observed_author = ""
    if not drawer_open:
        try:
            observed_author = author_in_feed(lines, ui.height)["text"].strip()
        except ValueError:
            observed_author = ""
    if want_author and observed_author and not nickname_matches(want_author, observed_author):
        return False, "", lines

    # Every candidate starts at the original work header, even when WeChat
    # remembers a previously scrolled comment position.
    check_lines, opened_drawer = _open_comment_drawer_if_needed(ui, lines)
    reopened = False
    attempts = 8
    for attempt in range(attempts):
        check_stopped(ui)
        if not find(check_lines, r"^评论\s*[\d.万wW]*") and not reopened:
            # A pull at the top of the list can drag the whole sheet closed.
            reopened = True
            check_lines, now_opened = _open_comment_drawer_if_needed(ui, check_lines)
            opened_drawer = opened_drawer or now_opened
        try:
            region = work_region(check_lines)
            break
        except WorkHeaderMissing:
            if attempt == attempts - 1:
                # Without the header we cannot establish the work's identity.
                if opened_drawer:
                    ui.back()
                return False, "", lines
            ui.restore_work_header(check_lines)
            check_lines = ui.snapshot("comments-preemptive-work-header", author=observed_author or want_author)

    for _ in range(2):
        if not region["expand"]:
            break
        check_stopped(ui)
        ui.click(region["expand"])
        check_lines = ui.snapshot("comments-preemptive-work-expanded", author=observed_author or want_author)
        region = work_region(check_lines)

    observed_author = region["authorHeader"].rsplit("作者", 1)[0].strip()
    body_text = _work_body_text(region)
    computed = compute_work_key(author=observed_author, title=body_text)
    if (not observed_author
            or (want_author and not nickname_matches(want_author, observed_author))
            or (want_title and compact(want_title) not in compact(body_text))
            or (want_key and want_key != computed)):
        if opened_drawer:
            ui.back()
        return False, "", lines
    return True, computed, check_lines


def preemptive_reply_comment(
    ui,
    quota_store,
    target_work,
    target_comment,
    reply_text,
    self_nickname="",
    max_work_scrolls=2,
    max_comment_pages=10,
):
    """Locate a specified work and comment to reply preemptively, then restore task state."""
    check_stopped(ui)
    if not target_work or not any(target_work.get(k) for k in ("author", "title", "dedupeKey", "workKey")):
        return {
            "action": "preemptive_reply",
            "status": "invalid_target_work",
            "replied": False,
            "restoredToOriginalTask": True,
        }
    if not target_comment or not any(target_comment.get(k) for k in ("nickname", "content", "commentKey")):
        return {
            "action": "preemptive_reply",
            "status": "invalid_target_comment",
            "replied": False,
            "restoredToOriginalTask": True,
        }

    initial_lines = ui.snapshot("comments-preemptive-start")
    initial_state = classify_page_state(initial_lines, ui.height)
    if initial_state not in ("channels_feed", "comment_drawer"):
        initial_lines = _ensure_on_feed_for_work_bar(ui, initial_lines)
        initial_state = classify_page_state(initial_lines, ui.height)
    started_in_drawer = initial_state == "comment_drawer"

    scrolls_done = 0
    matched_work_key = ""
    active_lines = initial_lines
    found_work = False

    for step in range(max(1, max_work_scrolls + 1)):
        check_stopped(ui)
        ok, work_key, work_lines = _work_matches_spec(ui, active_lines, target_work)
        if ok:
            found_work = True
            matched_work_key = work_key
            active_lines = work_lines
            break
        if step >= max_work_scrolls or not hasattr(ui, "scroll_feed_next"):
            break
        if classify_page_state(active_lines, ui.height) == "comment_drawer":
            ui.back()
        ui.scroll_feed_next()
        scrolls_done += 1
        active_lines = ui.snapshot(f"preemptive-feed-{scrolls_done}")

    if not found_work:
        for _ in range(scrolls_done):
            if hasattr(ui, "scroll_feed_prev"):
                ui.scroll_feed_prev()
        return {
            "action": "preemptive_reply",
            "status": "target_work_not_found",
            "replied": False,
            "scrollsDone": scrolls_done,
            "restoredToOriginalTask": True,
        }

    reply_result = reply_to_comment(
        ui,
        quota_store=quota_store,
        work_key=matched_work_key,
        reply_text=reply_text,
        target_comment=target_comment,
        self_nickname=self_nickname,
        lines=active_lines,
        max_comment_pages=max_comment_pages,
    )

    # Restore original task position
    current_lines = ui.snapshot("preemptive-restore-check")
    current_state = classify_page_state(current_lines, ui.height)
    if scrolls_done > 0:
        if current_state == "comment_drawer":
            ui.back()
        for _ in range(scrolls_done):
            if hasattr(ui, "scroll_feed_prev"):
                ui.scroll_feed_prev()
        if started_in_drawer:
            _open_comment_drawer_if_needed(ui)
    elif not started_in_drawer and current_state == "comment_drawer":
        ui.back()

    return {
        "action": "preemptive_reply",
        "status": reply_result["status"],
        "replied": reply_result["status"] == "performed",
        "workKey": matched_work_key,
        "replyResult": reply_result,
        "scrollsDone": scrolls_done,
        "restoredToOriginalTask": True,
    }


def profile_follow_state(lines):
    """Inspect the follow button state on a WeChat Channels user profile drawer."""
    follow = find(lines, r"^(?:[√✓]?)\s*(已关注|互相关注|申请关注|关注)$")
    contact = find(lines, r"^(?:私信|客服)$")
    if not follow or not contact:
        raise ValueError("User profile follow/contact controls are not visible")
    label = re.sub(r"^[√✓\s]+", "", follow["text"].strip())
    followed = label in ("已关注", "互相关注", "申请关注")
    return {
        "followed": followed,
        "label": label,
        "button": follow,
        "contactButton": contact,
    }


def follow_profile_user(ui, quota_store, user_key, lines=None):
    """Follow a user on their WeChat Channels profile drawer (with anti-unfollow protection)."""
    check_stopped(ui)
    if lines is None:
        lines = ui.snapshot("profile-follow-check")
    before = profile_follow_state(lines)

    if quota_store.has_acted("follow", user_key) or before["followed"]:
        if not quota_store.has_acted("follow", user_key):
            quota_store.mark_action("follow", user_key)
        return {
            "action": "follow",
            "status": "already_followed",
            "clicked": False,
            "verified": True,
            "userKey": user_key,
            "beforeLabel": before["label"],
            "afterLabel": before["label"],
        }

    quota_store.require_quota("follow")
    check_stopped(ui)
    ui.click(before["button"])
    after_lines = ui.snapshot("profile-after-follow")
    after = profile_follow_state(after_lines)
    if not after["followed"]:
        raise RuntimeError(f"Follow state did not change to 已关注 (got {after['label']})")
    quota_store.record("follow", "follow", user_key)
    return {
        "action": "follow",
        "status": "performed",
        "clicked": True,
        "verified": True,
        "userKey": user_key,
        "beforeLabel": before["label"],
        "afterLabel": after["label"],
    }


def profile_first_work_card(lines, height):
    """Locate the first work card on a Channels profile drawer, or return None if empty."""
    if find(lines, r"暂无内容|还没有发表作品|暂未发表视频|私密账号|账号已注销"):
        return None
    state = profile_follow_state(lines)
    controls_bottom = max(
        state["button"]["box"][1] + state["button"]["box"][3],
        state["contactButton"]["box"][1] + state["contactButton"]["box"][3],
    )
    zero_works = find(lines, r"^0\s*条原创内容$")
    if zero_works:
        return None

    tab_line = find(
        lines,
        r"^(?:全部|视频|作品|动态)$",
        lambda l: controls_bottom <= l["box"][1] < height - 180,
    )
    grid_top = (tab_line["box"][1] + tab_line["box"][3] + 18) if tab_line else (controls_bottom + 42)
    if grid_top + 110 >= height:
        return None

    # Check if there is any evidence of works below controls_bottom
    below_items = [
        l
        for l in lines
        if l["box"][1] >= controls_bottom - 10
        and l is not state["button"]
        and l is not state["contactButton"]
    ]
    has_work_signal = bool(
        tab_line
        or below_items
        or find(lines, r"\d+\s*条原创内容")
    )
    if not has_work_signal:
        return None

    return {
        "text": "profile_first_work",
        "box": [110, min(grid_top + 60, height - 130), 110, 110],
    }


def interact_profile_first_work(
    ui,
    quota_store,
    user_key,
    do_like=True,
    do_collect=True,
    lines=None,
):
    """Open the first video work on a Channels profile, like/collect it, and return to profile."""
    check_stopped(ui)
    if lines is None:
        lines = ui.snapshot("profile-grid")
    card = profile_first_work_card(lines, ui.height)
    if card is None:
        return {
            "status": "no_work_found",
            "opened": False,
            "likeResult": None,
            "collectResult": None,
            "returnedToProfile": True,
        }

    check_stopped(ui)
    ui.click(card)
    work_lines = ui.snapshot("profile-first-work")
    if classify_page_state(work_lines, ui.height) == "user_profile":
        time.sleep(0.5)
        check_stopped(ui)
        ui.click(card)
        work_lines = ui.snapshot("profile-first-work-retry")
    if classify_page_state(work_lines, ui.height) == "user_profile":
        return {
            "status": "no_work_opened",
            "opened": False,
            "likeResult": None,
            "collectResult": None,
            "returnedToProfile": True,
        }

    first_work_key = f"{user_key}:first_work"
    like_res = None
    collect_res = None
    try:
        if do_like:
            like_res = like_work(ui, quota_store, first_work_key, lines=work_lines)
        if do_collect:
            collect_res = collect_work_action(ui, quota_store, first_work_key)
    finally:
        check_stopped(ui)
        ui.back()
        return_lines = ui.snapshot("profile-after-first-work")
        if classify_page_state(return_lines, ui.height) != "user_profile":
            ui.back()
            return_lines = ui.snapshot("profile-after-first-work-retry")

    returned = classify_page_state(return_lines, ui.height) == "user_profile"
    return {
        "status": "performed",
        "opened": True,
        "workKey": first_work_key,
        "likeResult": like_res,
        "collectResult": collect_res,
        "returnedToProfile": returned,
    }


def touch_user_profile(
    ui,
    quota_store,
    user_key,
    do_follow=True,
    do_like_first_work=True,
    do_collect_first_work=True,
    lines=None,
):
    """Execute follow + first-work like/collect on an open WeChat Channels profile."""
    check_stopped(ui)
    if lines is None:
        lines = ui.snapshot("profile-touch")
    actions_performed = []
    follow_res = None
    first_work_res = None

    if do_follow:
        follow_res = follow_profile_user(ui, quota_store, user_key, lines=lines)
        if follow_res["status"] in ("performed", "already_followed"):
            actions_performed.append("FOLLOW")

    if do_like_first_work or do_collect_first_work:
        first_work_res = interact_profile_first_work(
            ui,
            quota_store=quota_store,
            user_key=user_key,
            do_like=do_like_first_work,
            do_collect=do_collect_first_work,
        )
        if first_work_res.get("likeResult", {}) and first_work_res["likeResult"].get("status") in ("performed", "already_liked"):
            actions_performed.append("LIKE_FIRST_WORK")
        if first_work_res.get("collectResult", {}) and first_work_res["collectResult"].get("status") in ("performed", "already_collected"):
            actions_performed.append("COLLECT_FIRST_WORK")

    touch_status = "TOUCHED" if actions_performed else "VISITED"
    return {
        "userKey": user_key,
        "touchStatus": touch_status,
        "profileActionsPerformed": actions_performed,
        "followResult": follow_res,
        "firstWorkResult": first_work_res,
        "dmSkipped": True,
    }


def visit_and_touch_commenter_profile(
    ui,
    quota_store,
    comment,
    work_key="",
    do_follow=True,
    do_like_first_work=True,
    do_collect_first_work=True,
):
    """Open a commenter's profile from the comment drawer, touch it, and verify return to the comment position."""
    check_stopped(ui)
    nickname = comment["nickname"]
    name_box = comment.get("_nameBox")
    if not name_box:
        raise ValueError("Comment has no clickable _nameBox")

    ui.click({"text": nickname, "box": name_box})
    time.sleep(PROFILE_SETTLE_SECONDS)
    profile_lines = ui.snapshot("commenter-profile", author=nickname)

    if classify_page_state(profile_lines, ui.height) == "user_profile":
        profile = parse_author(profile_lines, nickname)
        user_key = f"wxch-user:{identity(profile['authorNickname'])}"
        touch_res = touch_user_profile(
            ui,
            quota_store=quota_store,
            user_key=user_key,
            do_follow=do_follow,
            do_like_first_work=do_like_first_work,
            do_collect_first_work=do_collect_first_work,
            lines=profile_lines,
        )
        douyin_id = None
        if hasattr(ui, "profile_more"):
            try:
                current_prof = ui.snapshot("commenter-profile-before-more", author=profile["authorNickname"])
                name_line = profile_name(current_prof, profile["authorNickname"])
                ui.profile_more(name_line)
                menu = ui.snapshot("commenter-profile-menu")
                more = find(menu, r"^更多信息$")
                if more:
                    basic = open_basic_info(ui, more)
                    douyin_id = basic.get("authorDouyinId")
                    ui.back()
            except Exception:
                pass

        check_stopped(ui)
        ui.back()
        return_lines = ui.snapshot("comments-after-profile-touch")
        if classify_page_state(return_lines, ui.height) != "comment_drawer":
            ui.back()
            return_lines = ui.snapshot("comments-after-profile-touch-retry")
        returned_drawer = parse_comment_drawer(return_lines, ui.height, parent_event_id=work_key)
        anchor_ok = any(
            c["key"] == comment.get("key") or nickname_matches(nickname, c["nickname"])
            for c in returned_drawer["comments"]
        )
        return {
            "commentKey": comment.get("key"),
            "nickname": profile["authorNickname"],
            "commentNickname": nickname,
            "douyinId": douyin_id,
            "accountType": "channels",
            "profileVisited": True,
            "touchStatus": touch_res["touchStatus"],
            "profileActionsPerformed": touch_res["profileActionsPerformed"],
            "returnedToCommentPosition": anchor_ok,
            "touchDetail": touch_res,
        }

    card = parse_wechat_user_card(profile_lines, nickname, ui.height)
    check_stopped(ui)
    ui.back()
    return_lines = ui.snapshot("comments-after-user-card")
    returned_drawer = parse_comment_drawer(return_lines, ui.height, parent_event_id=work_key)
    anchor_ok = any(
        c["key"] == comment.get("key") or nickname_matches(nickname, c["nickname"])
        for c in returned_drawer["comments"]
    )
    return {
        "commentKey": comment.get("key"),
        "nickname": card["nickname"] if card else nickname,
        "commentNickname": nickname,
        "douyinId": None,
        "accountType": "wechat_user",
        "profileVisited": bool(card),
        "touchStatus": "VISITED_NO_CHANNELS_ID",
        "profileActionsPerformed": [],
        "returnedToCommentPosition": anchor_ok,
    }


def visit_and_touch_author_profile(
    ui,
    quota_store,
    do_follow=True,
    do_like_first_work=True,
    do_collect_first_work=True,
    lines=None,
):
    """Open the current work author's Channels profile from the feed, touch it, and return to the feed."""
    check_stopped(ui)
    feed_lines = _ensure_on_feed_for_work_bar(ui, lines)
    target = author_in_feed(feed_lines, ui.height)
    author_name = target["text"].strip()
    ui.click(target)
    time.sleep(PROFILE_SETTLE_SECONDS)
    profile_lines = ui.snapshot("author-profile-touch", author=author_name)
    try:
        profile = parse_author(profile_lines, author_name)
    except ValueError:
        time.sleep(0.6)
        profile_lines = ui.snapshot("author-profile-touch-retry", author=author_name)
        profile = parse_author(profile_lines, author_name)
    user_key = f"wxch-user:{identity(profile['authorNickname'])}"
    touch_res = touch_user_profile(
        ui,
        quota_store=quota_store,
        user_key=user_key,
        do_follow=do_follow,
        do_like_first_work=do_like_first_work,
        do_collect_first_work=do_collect_first_work,
        lines=profile_lines,
    )
    check_stopped(ui)
    ui.back()
    after_lines = ui.snapshot("feed-after-author-touch")
    if classify_page_state(after_lines, ui.height) != "channels_feed":
        ui.back()
        after_lines = ui.snapshot("feed-after-author-touch-retry")
    returned = classify_page_state(after_lines, ui.height) == "channels_feed"
    return {
        "nickname": profile["authorNickname"],
        "accountType": "channels",
        "profileVisited": True,
        "touchStatus": touch_res["touchStatus"],
        "profileActionsPerformed": touch_res["profileActionsPerformed"],
        "returnedToFeed": returned,
        "touchDetail": touch_res,
    }


def open_personal_messages(ui, lines=None):
    """Navigate from Channels feed -> Personal Page (`个人页`) -> `视频号消息` -> `评论` tab."""
    check_stopped(ui)
    if lines is None:
        lines = ui.snapshot("messages-start")
    state = classify_page_state(lines, ui.height)

    if state == "comment_drawer":
        ui.back()
        lines = ui.snapshot("feed-before-personal")
        state = classify_page_state(lines, ui.height)

    if state == "channels_feed":
        personal_icon = {"text": "personal_icon", "box": [640, 65, 56, 56]}
        check_stopped(ui)
        ui.click(personal_icon)
        lines = ui.snapshot("personal-page")
        state = classify_page_state(lines, ui.height)

    if state == "personal_page":
        # The personal page may list the viewer's own 消息 above the creator's
        # 视频号消息; only the latter is the Channels message center.
        msg_entry = find(lines, r"^视频号消息$") or find(lines, r"^消息$")
        if not msg_entry:
            raise ValueError("视频号消息 entry was not found on personal page")
        check_stopped(ui)
        ui.click(msg_entry)
        lines = ui.snapshot("messages-center")
        state = classify_page_state(lines, ui.height)

    if state == "channels_message_center":
        comment_tab = find(lines, MESSAGE_COMMENT_TAB, lambda l: l["box"][1] < 160)
        if comment_tab:
            check_stopped(ui)
            ui.click(comment_tab)
            lines = ui.snapshot("messages-comments")
        return lines

    raise RuntimeError(f"Could not navigate to Channels message center from state={state}")


def parse_comment_messages(lines, height=1605, frame=None):
    """Parse comment notification items on the `视频号消息 -> 评论` tab."""
    rows = reading_rows(lines)
    reply_rows = [
        (idx, r)
        for idx, r in enumerate(rows)
        if 150 < r["top"] < height - 70
        and any(
            110 <= item["box"][0] <= 220 and item["text"].strip() == "回复"
            for item in r["items"]
        )
    ]

    messages = []
    prev_bottom = 140
    for idx, reply_row in reply_rows:
        reply_item = next(
            item
            for item in reply_row["items"]
            if 110 <= item["box"][0] <= 220 and item["text"].strip() == "回复"
        )
        rx, ry, rw, rh = reply_item["box"]
        like_item = next(
            (
                item
                for item in reply_row["items"]
                if 220 <= item["box"][0] <= 330
            ),
            None,
        )
        like_box = like_item["box"] if like_item else [rx + 96, max(0, ry - 4), 44, max(32, rh + 4)]

        block_rows = [
            r
            for r in rows[:idx]
            if r["top"] > prev_bottom and r["bottom"] <= reply_row["top"] + 8
        ]
        prev_bottom = reply_row["bottom"]
        if not block_rows:
            continue

        header_row = block_rows[0]
        header_pieces = [item for item in header_row["items"] if 110 <= item["box"][0] < 540]
        if not header_pieces:
            continue
        raw_header = " ".join(item["text"].strip() for item in header_pieces).strip()
        date_match = re.search(rf"({DATE})\s*$", raw_header)
        msg_time = compact(date_match[1]) if date_match else ""
        raw_prefix = raw_header[: date_match.start()].strip() if date_match else raw_header
        raw_prefix = re.sub(r"\s*(?:关注我的|朋友|作者)\s*$", "", raw_prefix).strip()
        nickname, has_badge = clean_comment_nickname(raw_prefix)
        if not nickname:
            continue

        body_lines = []
        quoted_line = None
        for br in block_rows[1:]:
            row_items = [item for item in br["items"] if 110 <= item["box"][0] < 540]
            if not row_items:
                continue
            txt = "".join(item["text"] for item in row_items).strip()
            if not txt:
                continue
            if br["left"] >= header_row["left"] + 10 and body_lines and re.match(r"^[^:：]+[:：]", txt):
                quoted_line = txt
            else:
                body_lines.append(txt)

        content = "\n".join(body_lines).strip()
        if not content:
            continue

        red_active, red_ratio = is_red_active(frame, like_box)
        msg_key = "wxch-msg:" + identity(nickname, content, msg_time)
        messages.append(
            {
                "key": msg_key,
                "nickname": nickname,
                "content": content,
                "quotedContent": quoted_line,
                "messageTime": msg_time,
                "hasChannelsBadge": has_badge,
                "replyBox": reply_item["box"],
                "likeBox": like_box,
                "liked": red_active,
                "redRatio": red_ratio,
            }
        )

    return messages


def handle_single_message(
    ui,
    quota_store,
    message,
    reply_text=None,
    do_like=True,
    frame=None,
):
    """Execute in-place like and/or reply on a parsed message center notification."""
    check_stopped(ui)
    msg_key = message["key"]
    # Like and reply are recorded separately below, so a notice that was only
    # liked earlier still gets the reply asked for now.
    liked_now = False
    replied_now = False
    like_status = "skipped"
    reply_status = "skipped"

    if do_like:
        if frame is None and hasattr(ui, "capture_frame"):
            frame = ui.capture_frame()
        already_red, _ = is_red_active(frame, message["likeBox"])
        if message.get("liked") or already_red or quota_store.has_acted("comment_like", msg_key):
            like_status = "already_liked"
        else:
            quota_store.require_quota("like")
            check_stopped(ui)
            ui.click({"text": "like_message", "box": message["likeBox"]})
            after_lines = ui.snapshot("messages-after-like")
            after_frame = ui.capture_frame() if hasattr(ui, "capture_frame") else None
            red_after, _ = is_red_active(after_frame, message["likeBox"])
            if not red_after and after_frame is not None:
                raise RuntimeError(f"Message thumb-like did not turn red for {message['nickname']}")
            quota_store.record("like", "comment_like", msg_key)
            liked_now = True
            like_status = "performed"

    if reply_text:
        text = reply_text.strip()
        if quota_store.has_acted("comment_reply", msg_key):
            reply_status = "already_replied"
        else:
            quota_store.require_quota("comment")
            check_stopped(ui)
            ui.click({"text": "回复", "box": message["replyBox"]})
            composer_lines = ui.snapshot("messages-reply-composer")
            check_stopped(ui)
            ui.set_text(text)
            typed_lines = ui.snapshot("messages-reply-typed")
            send_btn = _find_composer_send_button(typed_lines, ui.height)
            check_stopped(ui)
            ui.click(send_btn)
            after_reply_lines = ui.snapshot("messages-after-reply")
            if classify_page_state(after_reply_lines, ui.height) == "composer_overlay":
                raise RuntimeError("Message reply composer did not close after clicking send")
            quota_store.record("comment", "comment_reply", msg_key)
            replied_now = True
            reply_status = "performed"

    quota_store.mark_action("message_handled", msg_key)
    return {
        "action": "message_handle",
        "status": "performed" if (liked_now or replied_now) else "already_satisfied",
        "messageKey": msg_key,
        "nickname": message["nickname"],
        "content": message["content"],
        "likeStatus": like_status,
        "replyStatus": reply_status,
        "liked": liked_now,
        "replied": replied_now,
    }


def return_from_messages_to_task(ui, max_backs=4):
    """Return from `视频号消息` -> `个人页` -> Channels feed and verify feed state."""
    steps = 0
    lines = ui.snapshot("messages-return-check")
    state = classify_page_state(lines, ui.height)
    while state != "channels_feed" and steps < max_backs:
        check_stopped(ui)
        ui.back()
        steps += 1
        lines = ui.snapshot(f"messages-return-{steps}")
        state = classify_page_state(lines, ui.height)
    return {
        "returnedToTask": state == "channels_feed",
        "finalState": state,
        "steps": steps,
    }


def message_done(quota_store, msg_key, do_like=True, reply_text=None):
    """Whether every action asked for now was already done on this notice."""
    liked = not do_like or quota_store.has_acted("comment_like", msg_key)
    replied = not (reply_text or "").strip() or quota_store.has_acted("comment_reply", msg_key)
    return liked and replied


def inspect_and_handle_messages(
    ui,
    quota_store,
    max_messages=1,
    do_like=True,
    reply_text=None,
    target_nickname=None,
):
    """Open `视频号消息 -> 评论`, handle up to max_messages notifications, and return to feed."""
    check_stopped(ui)
    lines = open_personal_messages(ui)
    frame = ui.capture_frame() if hasattr(ui, "capture_frame") else None
    messages = parse_comment_messages(lines, height=ui.height, frame=frame)

    handled = []
    for msg in messages:
        if len(handled) >= max_messages:
            break
        if target_nickname and not nickname_matches(target_nickname, msg["nickname"]):
            continue
        if message_done(quota_store, msg["key"], do_like=do_like, reply_text=reply_text):
            continue
        res = handle_single_message(
            ui,
            quota_store=quota_store,
            message=msg,
            reply_text=reply_text,
            do_like=do_like,
            frame=frame,
        )
        handled.append(res)
        frame = None

    ret = return_from_messages_to_task(ui)
    return {
        "messagesFound": len(messages),
        "messagesHandled": handled,
        "returnedToTask": ret["returnedToTask"],
        "returnDetail": ret,
    }


# These actions are deduplicated per work, so they need to know which work is on screen.
WORK_SCOPED_ACTIONS = ("likeWork", "collectWork", "commentText", "likeComment", "replyText", "touchCommenterProfile")
# Every key that makes the entry point act; anything else only tunes those actions.
INTERACTION_ACTIONS = (*WORK_SCOPED_ACTIONS, "touchAuthorProfile", "preemptiveReply", "inspectMessages")


def _has_comment_target(spec):
    return isinstance(spec, dict) and any(
        str(spec.get(key) or "").strip() for key in ("commentKey", "nickname", "content")
    )


def commenter_target(config):
    """The comment whose author `touchCommenterProfile` visits; falls back to the shared target."""
    spec = config.get("touchCommenterProfile")
    own = spec.get("targetComment") if isinstance(spec, dict) else None
    return own if _has_comment_target(own) else config.get("targetComment")


def validate_interaction_config(config):
    """Reject configurations that would make the entry point act on an unintended target.

    An empty comment target matches the first visible comment, so comment actions
    without one would like, reply to or visit an arbitrary person. A text key that
    is present but blank was enabled without its content and must not be skipped
    silently.
    """
    for key in ("commentText", "replyText"):
        if key in config and not str(config.get(key) or "").strip():
            raise ValueError(f"{key} is enabled but empty")
    for key in ("likeComment", "replyText"):
        if config.get(key) and not _has_comment_target(config.get("targetComment")):
            raise ValueError(f"{key} needs targetComment with a nickname, content or commentKey")
    if config.get("touchCommenterProfile") and not _has_comment_target(commenter_target(config)):
        raise ValueError("touchCommenterProfile needs targetComment with a nickname, content or commentKey")


def _must_propagate(error):
    """Stops and verification pages end the whole run; mode runs pause on the latter and retry."""
    message = str(error)
    return isinstance(error, InterruptedError) or "Verification detected" in message or "captcha" in message.lower()


def recover_to_feed(ui, max_backs=4):
    """Back out of whatever page a failed action left behind, so the next action starts from the feed."""
    state = "unknown"
    for attempt in range(max_backs + 1):
        check_stopped(ui)
        state = classify_page_state(ui.snapshot(f"recover-{attempt}"), ui.height)
        if state == "channels_feed":
            break
        if attempt < max_backs:
            ui.back()
    return state


def _run_action(ui, results, key, action):
    """Run one configured action; its failure is recorded and does not stop the actions after it."""
    check_stopped(ui)
    try:
        results[key] = action()
    except Exception as error:
        check_stopped(ui)
        if _must_propagate(error):
            raise
        failure = {"status": "failed", "error": str(error)}
        try:
            failure["recoveredState"] = recover_to_feed(ui)
        except Exception as recovery_error:
            check_stopped(ui)
            if _must_propagate(recovery_error):
                raise
            failure["recoveryError"] = str(recovery_error)
        results[key] = failure


INTERACTION_RESULT_KEYS = ("workLike", "workCollect", "workComment", "commentLike", "commentReply",
                           "authorProfileTouch", "preemptiveReply", "profileTouch", "messages")


def failed_interactions(results):
    return [key for key in INTERACTION_RESULT_KEYS
            if isinstance(results.get(key), dict) and results[key].get("status") == "failed"]


def interact_work_and_comments(ui, quota_store, config, video_event=None):
    """High-level entry point executing configured Batch 3 actions with clean resume.

    Each action runs on its own: one that fails is recorded with status "failed",
    the page is backed out to the feed, and the remaining actions still run.
    """
    validate_interaction_config(config)
    check_stopped(ui)
    resume_info = ensure_clean_page_on_resume(ui)
    lines = resume_info["lines"]

    author = (config.get("author") or "").strip()
    caption = (config.get("title") or "").strip()
    work = None
    work_error = None
    work_key = config.get("workKey") or ""
    if not work_key and video_event is not None:
        work_key = compute_work_key(video_event=video_event, author=author, title=caption)
    if not work_key:
        if classify_page_state(lines, ui.height) == "channels_feed":
            try:
                preview = feed_video_preview(lines, ui.height)
            except ValueError:
                preview = {}
            author = author or preview.get("author", "")
            caption = caption or preview.get("captionPreview", "")
            if author and not caption and any(config.get(k) for k in WORK_SCOPED_ACTIONS):
                caption = _caption_from_drawer(ui, lines)
        if author:
            work_key = quota_store.resolve_work_key(author, caption)
            work = {"author": author, "caption": caption}
        elif any(config.get(k) for k in WORK_SCOPED_ACTIONS):
            # Acting without knowing the work could repeat a comment on it; the
            # author visit and the message inspection do not need it.
            work_error = "Cannot identify the current work: start from the Channels feed or pass workKey/author"
    results = {
        "workKey": work_key,
        "resume": {k: v for k, v in resume_info.items() if k != "lines"},
    }
    if work:
        results["work"] = work

    def work_action(key, action):
        if work_error:
            results[key] = {"status": "failed", "error": work_error}
        else:
            _run_action(ui, results, key, action)

    if config.get("likeWork"):
        work_action("workLike", lambda: like_work(ui, quota_store, work_key))

    if config.get("collectWork"):
        work_action("workCollect", lambda: collect_work_action(ui, quota_store, work_key))

    if config.get("commentText"):
        work_action("workComment", lambda: post_work_comment(
            ui,
            quota_store=quota_store,
            work_key=work_key,
            comment_text=config["commentText"],
            self_nickname=config.get("selfNickname", ""),
            close_drawer=bool(config.get("closeDrawerAfterComment", False)),
        ))

    if config.get("likeComment"):
        work_action("commentLike", lambda: like_comment(
            ui,
            quota_store=quota_store,
            work_key=work_key,
            target_comment=config.get("targetComment"),
        ))

    if config.get("replyText"):
        work_action("commentReply", lambda: reply_to_comment(
            ui,
            quota_store=quota_store,
            work_key=work_key,
            reply_text=config["replyText"],
            target_comment=config.get("targetComment"),
            self_nickname=config.get("selfNickname", ""),
        ))

    if config.get("touchAuthorProfile"):
        spec = config["touchAuthorProfile"] if isinstance(config["touchAuthorProfile"], dict) else {}
        _run_action(ui, results, "authorProfileTouch", lambda: visit_and_touch_author_profile(
            ui,
            quota_store=quota_store,
            do_follow=spec.get("follow", True),
            do_like_first_work=spec.get("likeFirstWork", True),
            do_collect_first_work=spec.get("collectFirstWork", True),
        ))

    if config.get("preemptiveReply"):
        spec = config["preemptiveReply"]
        _run_action(ui, results, "preemptiveReply", lambda: preemptive_reply_comment(
            ui,
            quota_store=quota_store,
            target_work=spec.get("targetWork"),
            target_comment=spec.get("targetComment"),
            reply_text=spec.get("replyText", ""),
            self_nickname=config.get("selfNickname", ""),
        ))

    if config.get("touchCommenterProfile"):
        spec = config["touchCommenterProfile"] if isinstance(config["touchCommenterProfile"], dict) else {}

        def touch_commenter():
            located = locate_target_comment(
                ui,
                target_spec=commenter_target(config),
                quota_store=quota_store,
                work_key=work_key,
                exclude_self=True,
            )
            if not (located["found"] and located["comment"]):
                return {"status": "target_comment_not_found"}
            return visit_and_touch_commenter_profile(
                ui,
                quota_store=quota_store,
                comment=located["comment"],
                work_key=work_key,
                do_follow=spec.get("follow", True),
                do_like_first_work=spec.get("likeFirstWork", True),
                do_collect_first_work=spec.get("collectFirstWork", True),
            )

        work_action("profileTouch", touch_commenter)

    if config.get("inspectMessages"):
        spec = config["inspectMessages"] if isinstance(config["inspectMessages"], dict) else {}
        _run_action(ui, results, "messages", lambda: inspect_and_handle_messages(
            ui,
            quota_store=quota_store,
            max_messages=int(spec.get("maxMessages", 1)),
            do_like=bool(spec.get("like", True)),
            reply_text=spec.get("replyText"),
            target_nickname=spec.get("targetNickname"),
        ))

    results["quotas"] = quota_store.summary()
    return results
