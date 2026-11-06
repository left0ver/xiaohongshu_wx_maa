"""Work and comment interactions with daily quotas, stop guards, and idempotency verification."""

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

from collector import open_discover, restore_feed, scroll_feed
from comments import (
    close_video_comment_drawer,
    comment_count,
    comment_end,
    comment_section,
    commenter_profile,
    is_video_comment_drawer,
    open_video_comment_drawer,
    settled_comments,
    video_comment_drawer,
    visible_comments,
)
from ui import PageMismatch, _clean_topic_text, bounds, strip_truncation_suffix, verify_card
from vision import is_red_heart_active, is_yellow_star_active

QUOTA_ACTIONS = ("like", "collect", "comment", "follow", "dm")
LEDGER_KINDS = (
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
DEFAULT_DAILY_LIMITS = {
    "like": 50,
    "collect": 30,
    "comment": 20,
    "follow": 20,
    "dm": 15,
}


class QuotaExceededError(RuntimeError):
    def __init__(self, action, limit, used):
        super().__init__(f"Daily quota reached for {action}: {used}/{limit}")
        self.action = action
        self.limit = limit
        self.used = used


def _today_str():
    return datetime.now().astimezone().strftime("%Y-%m-%d")


class DailyQuotaStore:
    """Persistent daily quota and idempotency ledger that survives restarts without resetting today's count."""

    def __init__(self, data_dir, limits=None, today_fn=_today_str):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "daily_quotas.json"
        self._today_fn = today_fn
        self._explicit_limits = dict(limits) if limits is not None else None
        self._state = self._load_or_init()

    def _validate_action(self, action):
        if action not in QUOTA_ACTIONS:
            raise ValueError(f"Unsupported quota action: {action}")

    def _validate_kind(self, kind):
        if kind not in LEDGER_KINDS:
            raise ValueError(f"Unsupported ledger kind: {kind}")

    def _normalize_limits(self, raw_limits):
        merged = dict(DEFAULT_DAILY_LIMITS)
        if isinstance(raw_limits, dict):
            for key, value in raw_limits.items():
                if key not in QUOTA_ACTIONS:
                    raise ValueError(f"Unsupported quota action in limits: {key}")
                if value is not None and (type(value) is not int or value < 0):
                    raise ValueError(f"Quota limit for {key} must be a non-negative integer or None")
                merged[key] = value
        return merged

    def _load_or_init(self):
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        today = self._today_fn()
        loaded = None
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text())
            except Exception:
                loaded = None
        stored_limits = loaded.get("limits") if isinstance(loaded, dict) else None
        limits = self._normalize_limits(
            self._explicit_limits if self._explicit_limits is not None else stored_limits
        )
        if isinstance(loaded, dict) and loaded.get("date") == today:
            raw_used = loaded.get("used") if isinstance(loaded.get("used"), dict) else {}
            used = {
                action: max(0, int(raw_used.get(action, 0)))
                for action in QUOTA_ACTIONS
            }
            raw_records = loaded.get("records") if isinstance(loaded.get("records"), dict) else {}
            records = {
                kind: list(dict.fromkeys(str(item) for item in raw_records.get(kind, []) if item))
                for kind in LEDGER_KINDS
            }
        else:
            used = dict.fromkeys(QUOTA_ACTIONS, 0)
            records = {kind: [] for kind in LEDGER_KINDS}
        state = {
            "date": today,
            "limits": limits,
            "used": used,
            "records": records,
        }
        self._save(state)
        return state

    def _save(self, state):
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        temporary.replace(self.path)

    def reload(self):
        self._state = self._load_or_init()
        return self._state

    def update_limits(self, limits):
        self._explicit_limits = dict(limits)
        state = self._load_or_init()
        state["limits"] = self._normalize_limits(limits)
        self._save(state)
        self._state = state

    def limit_for(self, action):
        self._validate_action(action)
        self.reload()
        return self._state["limits"].get(action)

    def used_for(self, action):
        self._validate_action(action)
        self.reload()
        return int(self._state["used"].get(action, 0))

    def remaining_for(self, action):
        limit = self.limit_for(action)
        if limit is None:
            return None
        return max(0, limit - self.used_for(action))

    def can_perform(self, action, amount=1):
        self._validate_action(action)
        if type(amount) is not int or amount <= 0:
            raise ValueError("Quota amount must be a positive integer")
        limit = self.limit_for(action)
        if limit is None:
            return True
        return self.used_for(action) + amount <= limit

    def consume(self, action, amount=1):
        self._validate_action(action)
        if type(amount) is not int or amount <= 0:
            raise ValueError("Quota amount must be a positive integer")
        state = self.reload()
        limit = state["limits"].get(action)
        used = int(state["used"].get(action, 0))
        if limit is not None and used + amount > limit:
            raise QuotaExceededError(action, limit, used)
        state["used"][action] = used + amount
        self._save(state)
        return state["used"][action]

    def has_record(self, kind, target_id):
        self._validate_kind(kind)
        if not target_id:
            return False
        state = self.reload()
        return str(target_id) in state["records"].get(kind, ())

    def record_action(self, kind, target_id, quota_action=None):
        self._validate_kind(kind)
        state = self.reload()
        if quota_action is not None:
            self._validate_action(quota_action)
            limit = state["limits"].get(quota_action)
            used = int(state["used"].get(quota_action, 0))
            if limit is not None and used + 1 > limit:
                raise QuotaExceededError(quota_action, limit, used)
            state["used"][quota_action] = used + 1
        if target_id:
            items = state["records"].setdefault(kind, [])
            target_str = str(target_id)
            if target_str not in items:
                items.append(target_str)
        self._save(state)
        return state

    def snapshot(self):
        state = self.reload()
        exhausted = [
            action
            for action in QUOTA_ACTIONS
            if state["limits"].get(action) is not None
            and state["used"].get(action, 0) >= state["limits"][action]
        ]
        return {
            "date": state["date"],
            "limits": dict(state["limits"]),
            "used": dict(state["used"]),
            "exhausted": exhausted,
            "records": {k: list(v) for k, v in state["records"].items()},
        }


def check_stopped(device):
    if callable(getattr(device, "stopped", None)) and device.stopped():
        raise InterruptedError("Task stopped")


def _current_page(device):
    check_stopped(device)
    if getattr(device, "has_vision", False) is True:
        return device.page(reuse_if_stable=True)
    return device.page()


def compute_work_key(work):
    if not isinstance(work, dict):
        return ""
    note_type = (work.get("noteType") or "").strip()
    author = (work.get("authorNickname") or "").strip()
    title = strip_truncation_suffix(_clean_topic_text(work.get("title") or ""))
    if not title:
        desc = strip_truncation_suffix(_clean_topic_text(work.get("description") or ""))
        title = desc.splitlines()[0].strip() if desc else ""
    raw = f"{note_type}|{author}|{title}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def comment_identity_key(work_key, comment):
    """Compute a time-invariant identity key for a comment so '刚刚' -> '1分钟前' never causes re-replies."""
    nickname = (comment.payload.get("nickname") or "").strip()
    content = (comment.payload.get("content") or "").strip()
    is_reply = "1" if comment.is_reply else "0"
    raw = f"{work_key or ''}|{nickname}|{content}|{is_reply}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_self_comment(comment, quota_store=None, work_key=""):
    if quota_store is None or comment is None:
        return False
    nickname = (comment.payload.get("nickname") or "").strip()
    content = (comment.payload.get("content") or "").strip()
    if nickname and quota_store.has_record("self_comment", f"self_nickname:{nickname}"):
        return True
    if quota_store.has_record("self_comment", comment_identity_key(work_key, comment)):
        return True
    if content and quota_store.has_record("self_comment", f"{work_key or ''}|self_text|{content}"):
        return True
    return False


def _record_self_comment(quota_store, work_key, text, matched_comment=None):
    if quota_store is None:
        return
    clean = (text or "").strip()
    if clean:
        quota_store.record_action("self_comment", f"{work_key or ''}|self_text|{clean}", quota_action=None)
    if matched_comment is not None:
        quota_store.record_action(
            "self_comment",
            comment_identity_key(work_key, matched_comment),
            quota_action=None,
        )
        nickname = (matched_comment.payload.get("nickname") or "").strip()
        if nickname:
            quota_store.record_action("self_comment", f"self_nickname:{nickname}", quota_action=None)


def identify_current_work(page, fallback_work=None):
    try:
        detail = dict(page.note(allow_incomplete=True))
        detail.pop("_incomplete", None)
        detail["workKey"] = compute_work_key(detail)
        return detail
    except PageMismatch:
        pass
    try:
        state = page.image_note_state(allow_scrolled=True)
        detail = {
            "noteType": "IMAGE_TEXT",
            "authorNickname": state["authorNickname"],
            "title": state["title"],
            "description": state["description"],
        }
        detail["workKey"] = compute_work_key(detail)
        return detail
    except PageMismatch:
        pass
    if is_video_comment_drawer(page):
        if isinstance(fallback_work, dict) and fallback_work.get("noteType") == "VIDEO":
            detail = {
                "noteType": "VIDEO",
                "authorNickname": fallback_work.get("authorNickname", ""),
                "title": fallback_work.get("title", ""),
                "description": fallback_work.get("description", ""),
            }
            detail["workKey"] = fallback_work.get("workKey") or compute_work_key(detail)
            return detail
        detail = {"noteType": "VIDEO", "authorNickname": "", "title": "", "description": ""}
        detail["workKey"] = compute_work_key(detail)
        return detail
    try:
        author, _ = comment_section(page)
        if author:
            title = ""
            description = ""
            if isinstance(fallback_work, dict) and fallback_work.get("authorNickname") == author:
                title = fallback_work.get("title", "")
                description = fallback_work.get("description", "")
            detail = {
                "noteType": "IMAGE_TEXT",
                "authorNickname": author,
                "title": title,
                "description": description,
            }
            detail["workKey"] = (
                fallback_work.get("workKey")
                if isinstance(fallback_work, dict)
                and fallback_work.get("authorNickname") == author
                and fallback_work.get("workKey")
                else compute_work_key(detail)
            )
            return detail
    except PageMismatch:
        pass
    raise PageMismatch("Current page is not a recognizable work or comment section")


def _has_active_composer(page):
    has_edit = any(n.get("class") == "android.widget.EditText" for n in page.nodes)
    has_send = any(
        n.get("class") == "android.widget.TextView" and n.get("text", "").strip() == "发送"
        for n in page.nodes
    )
    return has_edit and has_send


def _has_comment_action_sheet(page):
    if _has_active_composer(page):
        return False
    texts = {n.get("text", "").strip() for n in page.nodes if n.get("class") == "android.widget.TextView"}
    return ("分享到微信" in texts and "回复" in texts) or ({"回复", "复制", "收藏"} <= texts)


def _is_self_comment_action_sheet(page):
    if not _has_comment_action_sheet(page):
        return False
    texts = {n.get("text", "").strip() for n in page.nodes if n.get("class") == "android.widget.TextView"}
    return "删除" in texts or "分享到微信" in texts


def _has_share_sheet(page):
    return any(n.get("content-desc") == "复制链接" for n in page.nodes)


def _has_notification_prompt_modal(page):
    texts = {n.get("text", "").strip() for n in page.nodes}
    return "不再提醒" in texts and "开启通知" in texts


def classify_page_state(page, fallback_work=None):
    if _has_notification_prompt_modal(page):
        return {"pageType": "notification_prompt_modal", "page": page, "work": None}
    if _has_active_composer(page):
        return {"pageType": "composer_overlay", "page": page, "work": None}
    if _has_comment_action_sheet(page):
        return {"pageType": "comment_action_sheet", "page": page, "work": None}
    if _has_share_sheet(page):
        return {"pageType": "share_sheet", "page": page, "work": None}
    if is_video_comment_drawer(page):
        work = identify_current_work(page, fallback_work=fallback_work)
        return {"pageType": "video_comment_drawer", "page": page, "work": work}
    try:
        work = identify_current_work(page, fallback_work=fallback_work)
        page_type = "video_note" if work.get("noteType") == "VIDEO" else "image_note"
        return {"pageType": page_type, "page": page, "work": work}
    except PageMismatch:
        pass
    try:
        cards = page.cards()
        return {"pageType": "discover_feed", "page": page, "work": None, "cards": cards}
    except PageMismatch:
        pass
    if any(n.get("text", "").startswith("小红书号：") for n in page.nodes):
        return {"pageType": "author_profile", "page": page, "work": None}
    if any(n.get("text", "").strip() == "收到的评论和@" for n in page.nodes):
        return {"pageType": "comment_at_messages", "page": page, "work": None}
    if any(n.get("content-desc") == "评论和@按钮" for n in page.nodes):
        return {"pageType": "message_center", "page": page, "work": None}
    return {"pageType": "unknown", "page": page, "work": None}


def ensure_clean_page_on_resume(device, fallback_work=None, max_dismiss=3):
    """Dismiss any leftover input composer, comment menu, notification prompt, or share sheet on resume."""
    check_stopped(device)
    transient_types = (
        "notification_prompt_modal",
        "composer_overlay",
        "comment_action_sheet",
        "share_sheet",
    )
    for _ in range(max_dismiss):
        check_stopped(device)
        page = device.page()
        state = classify_page_state(page, fallback_work=fallback_work)
        if state["pageType"] not in transient_types:
            return state
        if state["pageType"] == "notification_prompt_modal":
            device.click(lambda current: current.unique(text="不再提醒"))
        else:
            device.back()
        if getattr(device, "has_vision", False) is True:
            device.wait_visual_settle(timeout=0.6)
        else:
            time.sleep(0.25)
    check_stopped(device)
    page = device.page()
    state = classify_page_state(page, fallback_work=fallback_work)
    if state["pageType"] in transient_types:
        raise PageMismatch(f"Could not dismiss transient overlay: {state['pageType']}")
    return state


def _work_matches(actual, expected):
    if not isinstance(expected, dict) or not expected:
        return True
    if not isinstance(actual, dict):
        return False
    exp_type = expected.get("noteType")
    if exp_type and actual.get("noteType") and exp_type != actual.get("noteType"):
        return False
    exp_author = (expected.get("authorNickname") or expected.get("author") or "").strip()
    act_author = (actual.get("authorNickname") or actual.get("author") or "").strip()
    if exp_author and act_author and exp_author != act_author:
        return False
    exp_title = strip_truncation_suffix(_clean_topic_text(expected.get("title") or ""))
    act_title = strip_truncation_suffix(_clean_topic_text(actual.get("title") or ""))
    act_desc = strip_truncation_suffix(_clean_topic_text(actual.get("description") or ""))
    if exp_title and (act_title or act_desc):
        if (
            exp_title != act_title
            and exp_title not in act_title
            and act_title not in exp_title
            and exp_title not in act_desc
        ):
            return False
    return True


def _find_bottom_collect_button(page):
    candidates = [
        n
        for n in page.nodes
        if n.get("class") == "android.widget.Button"
        and n.get("clickable") == "true"
        and re.match(r"^(已)?收藏\s*(.*)$", n.get("content-desc", "").strip())
        and bounds(n)[1] >= 1800
    ]
    if len(candidates) != 1:
        raise PageMismatch("No unambiguous bottom collect button on current work")
    return candidates[0]


def _find_bottom_like_button(page):
    direct = [
        n
        for n in page.nodes
        if n.get("class") == "android.widget.Button"
        and n.get("clickable") == "true"
        and re.match(r"^(已)?点赞\s*(.*)$", n.get("content-desc", "").strip())
        and bounds(n)[1] >= 1800
    ]
    if len(direct) == 1:
        return direct[0]
    if len(direct) > 1:
        raise PageMismatch("Multiple bottom like buttons found")
    collect_btn = _find_bottom_collect_button(page)
    c_left, c_top, _, c_bottom = bounds(collect_btn)
    video_candidates = [
        n
        for n in page.nodes
        if n.get("class") == "android.widget.LinearLayout"
        and n.get("clickable") == "true"
        and abs(bounds(n)[2] - c_left) <= 24
        and abs(bounds(n)[1] - c_top) <= 24
        and abs(bounds(n)[3] - c_bottom) <= 24
        and any(child.get("class") == "android.widget.ImageView" for child in n)
    ]
    if len(video_candidates) != 1:
        raise PageMismatch("No unambiguous bottom like control on current work")
    return video_candidates[0]


def work_like_state(page, frame=None):
    button = _find_bottom_like_button(page)
    desc = button.get("content-desc", "").strip()
    icons = [n for n in button.iter("node") if n.get("class") == "android.widget.ImageView"]
    icon_node = icons[0] if len(icons) == 1 else None
    texts = [
        n.get("text", "").strip()
        for n in button.iter("node")
        if n.get("class") == "android.widget.TextView" and n.get("text", "").strip()
    ]
    count_text = texts[0] if texts else ""
    if not count_text and desc:
        match = re.match(r"^(?:已)?点赞\s*(.*)$", desc)
        if match:
            count_text = match[1].strip()
    xml_liked = bool(
        desc.startswith("已点赞")
        or button.get("selected") == "true"
        or (icon_node is not None and icon_node.get("selected") == "true")
    )
    roi_node = icon_node if icon_node is not None else button
    visual_liked = bool(frame is not None and is_red_heart_active(frame, bounds(roi_node)))
    return {
        "button": button,
        "iconNode": icon_node,
        "description": desc,
        "countText": count_text,
        "xmlLiked": xml_liked,
        "visualLiked": visual_liked,
        "liked": xml_liked or visual_liked,
    }


def work_collect_state(page, frame=None):
    button = _find_bottom_collect_button(page)
    desc = button.get("content-desc", "").strip()
    icons = [n for n in button.iter("node") if n.get("class") == "android.widget.ImageView"]
    icon_node = icons[0] if len(icons) == 1 else None
    texts = [
        n.get("text", "").strip()
        for n in button.iter("node")
        if n.get("class") == "android.widget.TextView" and n.get("text", "").strip()
    ]
    count_text = texts[0] if texts else ""
    if not count_text and desc:
        match = re.match(r"^(?:已)?收藏\s*(.*)$", desc)
        if match:
            count_text = match[1].strip()
    xml_collected = bool(
        desc.startswith("已收藏")
        or button.get("selected") == "true"
        or (icon_node is not None and icon_node.get("selected") == "true")
    )
    roi_node = icon_node if icon_node is not None else button
    visual_collected = bool(frame is not None and is_yellow_star_active(frame, bounds(roi_node)))
    return {
        "button": button,
        "iconNode": icon_node,
        "description": desc,
        "countText": count_text,
        "xmlCollected": xml_collected,
        "visualCollected": visual_collected,
        "collected": xml_collected or visual_collected,
    }


def like_work(device, quota_store=None, enabled=True, work_key=None, expected_work=None):
    """Like an image-text or video work if enabled and not already liked, verifying state change."""
    if not enabled:
        return {"action": "like_work", "executed": False, "skipped": True, "reason": "disabled_by_config"}
    check_stopped(device)
    resumed = ensure_clean_page_on_resume(device, fallback_work=expected_work)
    page = resumed["page"]
    current_work = resumed["work"] or identify_current_work(page, fallback_work=expected_work)
    if expected_work is not None and not _work_matches(current_work, expected_work):
        raise PageMismatch("Current work does not match expected work before like")
    resolved_key = work_key or current_work.get("workKey") or compute_work_key(current_work)
    frame = device.capture_frame() if getattr(device, "has_vision", False) is True else None
    before = work_like_state(page, frame=frame)
    if before["liked"]:
        if quota_store is not None and resolved_key:
            quota_store.record_action("work_like", resolved_key, quota_action=None)
        return {
            "action": "like_work",
            "executed": False,
            "skipped": True,
            "reason": "already_liked",
            "workKey": resolved_key,
            "liked": True,
            "countText": before["countText"],
            "xmlLiked": before["xmlLiked"],
            "visualLiked": before["visualLiked"],
        }
    if quota_store is not None and not quota_store.can_perform("like"):
        return {
            "action": "like_work",
            "executed": False,
            "skipped": True,
            "reason": "quota_exceeded",
            "workKey": resolved_key,
            "liked": False,
            "limit": quota_store.limit_for("like"),
            "used": quota_store.used_for("like"),
        }
    check_stopped(device)
    roi = bounds(before["button"])
    device.click(lambda current: work_like_state(current)["button"])
    if getattr(device, "has_vision", False) is True:
        device.wait_visual_settle(roi=roi, timeout=0.6)

    def verified_liked(current):
        current_frame = device.capture_frame() if getattr(device, "has_vision", False) is True else None
        state = work_like_state(current, frame=current_frame)
        if not state["liked"]:
            raise PageMismatch("Work like state did not become active after click")
        return state

    after = device.wait(verified_liked, timeout=8)
    if quota_store is not None:
        quota_store.record_action("work_like", resolved_key, quota_action="like")
    return {
        "action": "like_work",
        "executed": True,
        "skipped": False,
        "workKey": resolved_key,
        "liked": True,
        "beforeCountText": before["countText"],
        "afterCountText": after["countText"],
        "xmlLiked": after["xmlLiked"],
        "visualLiked": after["visualLiked"],
    }


def collect_work_action(device, quota_store=None, enabled=True, work_key=None, expected_work=None):
    """Collect (bookmark) an image-text or video work if enabled and not already collected."""
    if not enabled:
        return {"action": "collect_work", "executed": False, "skipped": True, "reason": "disabled_by_config"}
    check_stopped(device)
    resumed = ensure_clean_page_on_resume(device, fallback_work=expected_work)
    page = resumed["page"]
    current_work = resumed["work"] or identify_current_work(page, fallback_work=expected_work)
    if expected_work is not None and not _work_matches(current_work, expected_work):
        raise PageMismatch("Current work does not match expected work before collect")
    resolved_key = work_key or current_work.get("workKey") or compute_work_key(current_work)
    frame = device.capture_frame() if getattr(device, "has_vision", False) is True else None
    before = work_collect_state(page, frame=frame)
    if before["collected"]:
        if quota_store is not None and resolved_key:
            quota_store.record_action("work_collect", resolved_key, quota_action=None)
        return {
            "action": "collect_work",
            "executed": False,
            "skipped": True,
            "reason": "already_collected",
            "workKey": resolved_key,
            "collected": True,
            "countText": before["countText"],
            "xmlCollected": before["xmlCollected"],
            "visualCollected": before["visualCollected"],
        }
    if quota_store is not None and not quota_store.can_perform("collect"):
        return {
            "action": "collect_work",
            "executed": False,
            "skipped": True,
            "reason": "quota_exceeded",
            "workKey": resolved_key,
            "collected": False,
            "limit": quota_store.limit_for("collect"),
            "used": quota_store.used_for("collect"),
        }
    check_stopped(device)
    roi = bounds(before["button"])
    device.click(lambda current: work_collect_state(current)["button"])
    if getattr(device, "has_vision", False) is True:
        device.wait_visual_settle(roi=roi, timeout=0.6)

    def verified_collected(current):
        current_frame = device.capture_frame() if getattr(device, "has_vision", False) is True else None
        state = work_collect_state(current, frame=current_frame)
        if not state["collected"]:
            raise PageMismatch("Work collect state did not become active after click")
        return state

    after = device.wait(verified_collected, timeout=8)
    if quota_store is not None:
        quota_store.record_action("work_collect", resolved_key, quota_action="collect")
    return {
        "action": "collect_work",
        "executed": True,
        "skipped": False,
        "workKey": resolved_key,
        "collected": True,
        "beforeCountText": before["countText"],
        "afterCountText": after["countText"],
        "xmlCollected": after["xmlCollected"],
        "visualCollected": after["visualCollected"],
    }


def _primary_comment_trigger(page):
    by_desc = [
        n
        for n in page.nodes
        if n.get("content-desc") == "评论框"
        or n.get("resource-id", "").endswith("/commentLayout")
    ]
    if len(by_desc) == 1:
        return by_desc[0]
    by_text = [
        n
        for n in page.nodes
        if n.get("class") != "android.widget.EditText"
        and n.get("text", "").strip() in ("说点什么...", "留下你的想法吧", "有话要说，快来评论", "抢首评")
    ]
    if len(by_text) == 1:
        return by_text[0]
    if by_text:
        return max(by_text, key=lambda n: bounds(n)[1])
    raise PageMismatch("No unambiguous primary comment input trigger found")


def _active_composer(page):
    edits = [n for n in page.nodes if n.get("class") == "android.widget.EditText"]
    sends = [
        n
        for n in page.nodes
        if n.get("class") == "android.widget.TextView" and n.get("text", "").strip() == "发送"
    ]
    if len(edits) != 1 or len(sends) != 1:
        raise PageMismatch("Comment composer EditText and send button are not active")
    edit = edits[0]
    send = sends[0]
    bounds(edit)
    bounds(send)
    return {
        "edit": edit,
        "send": send,
        "text": edit.get("text", "").strip(),
        "hint": edit.get("hint", "").strip(),
        "sendEnabled": send.get("enabled") == "true",
    }


def _verified_composer_input(page, expected_text, expected_reply_nickname=None):
    composer = _active_composer(page)
    hint = composer["hint"]
    if expected_reply_nickname is None:
        if hint.startswith("回复 @") or hint.startswith("回复 "):
            raise PageMismatch("Composer is targeting a comment reply instead of a primary comment")
    elif hint and expected_reply_nickname not in hint:
        raise PageMismatch(f"Reply composer hint {hint!r} does not match @{expected_reply_nickname}")
    if composer["text"] != expected_text.strip():
        raise PageMismatch("Composer EditText content does not match the expected text")
    if not composer["sendEnabled"]:
        raise PageMismatch("Send button is not enabled after setting text")
    return composer


def _visible_comment_matching_text(page, text, is_reply=False):
    try:
        _, comments = visible_comments(page)
    except PageMismatch:
        return None
    target = text.strip()
    for item in comments:
        if item.is_reply != bool(is_reply):
            continue
        body = item.payload.get("content", "").strip()
        if body == target or (is_reply and body.endswith("：" + target)):
            return item
    return None


def post_work_comment(device, text, quota_store=None, enabled=True, work_key=None, expected_work=None):
    """Post a top-level comment on the target work after verifying work identity, input text, and result."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Comment text must be a non-empty string")
    clean_text = text.strip()
    if not enabled:
        return {"action": "post_work_comment", "executed": False, "skipped": True, "reason": "disabled_by_config"}
    check_stopped(device)
    resumed = ensure_clean_page_on_resume(device, fallback_work=expected_work)
    page = resumed["page"]
    current_work = resumed["work"] or identify_current_work(page, fallback_work=expected_work)
    if expected_work is not None and not _work_matches(current_work, expected_work):
        raise PageMismatch("Current work does not match expected work before commenting")
    resolved_key = work_key or current_work.get("workKey") or compute_work_key(current_work)
    record_id = f"{resolved_key}:{clean_text}"

    if quota_store is not None and quota_store.has_record("work_comment", record_id):
        return {
            "action": "post_work_comment",
            "executed": False,
            "skipped": True,
            "reason": "already_commented_in_ledger",
            "workKey": resolved_key,
            "text": clean_text,
        }
    existing = _visible_comment_matching_text(page, clean_text, is_reply=False)
    if existing is not None and existing.payload.get("commentTime") in ("刚刚", "1分钟前"):
        if quota_store is not None:
            quota_store.record_action("work_comment", record_id, quota_action=None)
        return {
            "action": "post_work_comment",
            "executed": False,
            "skipped": True,
            "reason": "already_visible_on_page",
            "workKey": resolved_key,
            "text": clean_text,
            "commentKey": existing.key,
        }
    if quota_store is not None and not quota_store.can_perform("comment"):
        return {
            "action": "post_work_comment",
            "executed": False,
            "skipped": True,
            "reason": "quota_exceeded",
            "workKey": resolved_key,
            "text": clean_text,
            "limit": quota_store.limit_for("comment"),
            "used": quota_store.used_for("comment"),
        }

    before_count = comment_count(page)
    check_stopped(device)
    device.click(_primary_comment_trigger)
    if getattr(device, "has_vision", False) is True:
        device.wait_visual_settle(timeout=0.6)

    def primary_composer_ready(current):
        composer = _active_composer(current)
        if composer["text"].startswith("回复 @") or composer["hint"].startswith("回复 @"):
            raise PageMismatch("Composer opened in reply mode instead of primary work comment mode")
        return composer

    device.wait(primary_composer_ready, timeout=8)
    check_stopped(device)
    device.set_text(clean_text)
    device.wait(lambda current: _verified_composer_input(current, clean_text), timeout=8)
    check_stopped(device)
    device.click(lambda current: _verified_composer_input(current, clean_text)["send"])
    if getattr(device, "has_vision", False) is True:
        device.wait_visual_settle(timeout=0.8)

    def send_completed(current):
        if _has_active_composer(current):
            raise PageMismatch("Comment composer is still open after clicking send")
        after_work = identify_current_work(current, fallback_work=current_work)
        if not _work_matches(after_work, current_work):
            raise PageMismatch("Work changed after sending primary comment")
        matched = _visible_comment_matching_text(current, clean_text, is_reply=False)
        after_count = comment_count(current)
        if matched is not None:
            return {
                "verifiedBy": "visible_comment",
                "commentKey": matched.key,
                "matchedComment": matched,
                "afterCount": after_count,
            }
        if before_count is not None and after_count is not None and after_count > before_count:
            return {
                "verifiedBy": "comment_count_increment",
                "commentKey": None,
                "matchedComment": None,
                "afterCount": after_count,
            }
        return {
            "verifiedBy": "composer_closed_on_same_work",
            "commentKey": None,
            "matchedComment": None,
            "afterCount": after_count,
        }

    verification = device.wait(send_completed, timeout=10)
    if quota_store is not None:
        quota_store.record_action("work_comment", record_id, quota_action="comment")
        _record_self_comment(quota_store, resolved_key, clean_text, verification.get("matchedComment"))
    return {
        "action": "post_work_comment",
        "executed": True,
        "skipped": False,
        "workKey": resolved_key,
        "text": clean_text,
        "beforeCount": before_count,
        "afterCount": verification["afterCount"],
        "verifiedBy": verification["verifiedBy"],
        "commentKey": verification["commentKey"],
    }


def _comment_matches_selector(comment, selector, quota_store=None, work_key="", exclude_self=False):
    if exclude_self and is_self_comment(comment, quota_store=quota_store, work_key=work_key):
        return False
    if isinstance(selector, str):
        return comment.key == selector or comment_identity_key(work_key, comment) == selector
    if hasattr(selector, "key") and hasattr(selector, "payload"):
        return comment.key == selector.key
    if not isinstance(selector, dict) or not selector:
        raise ValueError("Comment selector must be a key string, Comment, or non-empty dict")
    key = selector.get("key") or selector.get("commentKey")
    if key and comment.key != key and comment_identity_key(work_key, comment) != key:
        return False
    nickname = (selector.get("nickname") or "").strip()
    if nickname and comment.payload.get("nickname", "").strip() != nickname:
        return False
    exclude_nickname = (selector.get("excludeNickname") or "").strip()
    if exclude_nickname and comment.payload.get("nickname", "").strip() == exclude_nickname:
        return False
    content = (selector.get("content") or "").strip()
    if content and comment.payload.get("content", "").strip() != content:
        return False
    content_contains = (selector.get("contentContains") or "").strip()
    if content_contains and content_contains not in comment.payload.get("content", ""):
        return False
    if "isReply" in selector and bool(comment.is_reply) != bool(selector["isReply"]):
        return False
    return True


def _match_visible_comment(page, selector, quota_store=None, work_key="", exclude_self=False):
    _, comments = visible_comments(page)
    matches = [
        c
        for c in comments
        if _comment_matches_selector(
            c,
            selector,
            quota_store=quota_store,
            work_key=work_key,
            exclude_self=exclude_self,
        )
    ]
    if len(matches) > 1:
        raise PageMismatch("Target comment selector matched multiple visible comments; refusing ambiguous action")
    if not matches:
        raise PageMismatch("Target comment is not visible on the current screen")
    return matches[0]


def _ensure_comments_open(device, fallback_work=None):
    page = _current_page(device)
    try:
        visible_comments(page)
        return page
    except PageMismatch:
        pass
    if any(n.get("resource-id", "").endswith("/matrixNickNameView") for n in page.nodes):
        open_video_comment_drawer(device)
        settled_comments(device)
        return _current_page(device)
    buttons = [
        n
        for n in page.nodes
        if re.fullmatch(r"评论\s+[1-9]\d*", n.get("content-desc", "").strip())
    ]
    if len(buttons) == 1:
        device.click(lambda current: [
            n for n in current.nodes if re.fullmatch(r"评论\s+[1-9]\d*", n.get("content-desc", "").strip())
        ][0])
        if getattr(device, "has_vision", False) is True:
            device.wait_visual_settle(timeout=0.8)
        settled_comments(device)
        return _current_page(device)
    raise PageMismatch("Comment section is not open or has no visible comments")


def locate_target_comment(device, selector, max_scrolls=6, quota_store=None, work_key="", exclude_self=False):
    """Locate an unambiguous target comment in the open comment section with bounded scrolling."""
    if type(max_scrolls) is not int or not 0 <= max_scrolls <= 30:
        raise ValueError("Comment scroll limit must be between 0 and 30")
    check_stopped(device)
    _ensure_comments_open(device)
    for step in range(max_scrolls + 1):
        check_stopped(device)
        page = _current_page(device)
        _, comments = visible_comments(page)
        matches = [
            c
            for c in comments
            if _comment_matches_selector(
                c,
                selector,
                quota_store=quota_store,
                work_key=work_key,
                exclude_self=exclude_self,
            )
        ]
        if len(matches) > 1:
            raise PageMismatch("Target comment selector matched multiple visible comments; refusing ambiguous action")
        if len(matches) == 1:
            return matches[0]
        if step == max_scrolls or comment_end(page):
            break
        author, listing = comment_section(page)
        left, top, right, bottom = bounds(listing)
        x = str((left + right) // 2)
        height = bottom - top
        device.command(
            "input",
            "swipe",
            x,
            str(top + height * 3 // 4),
            x,
            str(top + height * 3 // 4 - height // 3),
            "800",
        )
        settled_comments(device, author or None)
    raise PageMismatch("Target comment not found within scroll limit")


def comment_like_state(page, selector, frame=None):
    comment = _match_visible_comment(page, selector)
    if comment.like_node is None:
        raise PageMismatch("Target comment does not expose an unambiguous like button")
    roi_node = comment.like_icon_node if comment.like_icon_node is not None else comment.like_node
    visual_liked = bool(frame is not None and is_red_heart_active(frame, bounds(roi_node)))
    xml_liked = bool(comment.liked)
    return {
        "comment": comment,
        "button": comment.like_node,
        "iconNode": comment.like_icon_node,
        "countText": comment.like_count_text,
        "xmlLiked": xml_liked,
        "visualLiked": visual_liked,
        "liked": xml_liked or visual_liked,
    }


def _parse_int_count(text):
    clean = (text or "").strip()
    return int(clean) if clean.isdigit() else None


def like_comment(device, selector, quota_store=None, enabled=True, work_key=None, max_scrolls=6):
    """Like a specific target comment after verifying target identity and existing like state."""
    if not enabled:
        return {"action": "like_comment", "executed": False, "skipped": True, "reason": "disabled_by_config"}
    check_stopped(device)
    ensure_clean_page_on_resume(device)
    target = locate_target_comment(device, selector, max_scrolls=max_scrolls)
    page = _current_page(device)
    resolved_work_key = work_key
    if not resolved_work_key:
        try:
            resolved_work_key = identify_current_work(page).get("workKey", "")
        except PageMismatch:
            resolved_work_key = ""
    record_id = comment_identity_key(resolved_work_key, target)
    frame = device.capture_frame() if getattr(device, "has_vision", False) is True else None
    before = comment_like_state(page, target.key, frame=frame)
    if before["liked"] or (quota_store is not None and quota_store.has_record("comment_like", record_id)):
        if quota_store is not None:
            quota_store.record_action("comment_like", record_id, quota_action=None)
        return {
            "action": "like_comment",
            "executed": False,
            "skipped": True,
            "reason": "already_liked",
            "commentKey": target.key,
            "nickname": target.payload["nickname"],
            "content": target.payload["content"],
            "liked": True,
            "countText": before["countText"],
            "xmlLiked": before["xmlLiked"],
            "visualLiked": before["visualLiked"],
        }
    if quota_store is not None and not quota_store.can_perform("like"):
        return {
            "action": "like_comment",
            "executed": False,
            "skipped": True,
            "reason": "quota_exceeded",
            "commentKey": target.key,
            "nickname": target.payload["nickname"],
            "content": target.payload["content"],
            "liked": False,
            "limit": quota_store.limit_for("like"),
            "used": quota_store.used_for("like"),
        }

    check_stopped(device)
    roi = bounds(before["button"])
    device.click(lambda current: comment_like_state(current, target.key)["button"])
    if getattr(device, "has_vision", False) is True:
        device.wait_visual_settle(roi=roi, timeout=0.6)
    before_num = _parse_int_count(before["countText"])

    def verified_comment_liked(current):
        current_frame = device.capture_frame() if getattr(device, "has_vision", False) is True else None
        state = comment_like_state(current, target.key, frame=current_frame)
        after_num = _parse_int_count(state["countText"])
        count_incremented = (
            (before_num is not None and after_num is not None and after_num == before_num + 1)
            or (not before["countText"] and after_num == 1)
        )
        if not state["liked"] and not count_incremented:
            raise PageMismatch("Target comment like state did not become active after click")
        state["liked"] = True
        return state

    after = device.wait(verified_comment_liked, timeout=8)
    if quota_store is not None:
        quota_store.record_action("comment_like", record_id, quota_action="like")
    return {
        "action": "like_comment",
        "executed": True,
        "skipped": False,
        "commentKey": target.key,
        "nickname": target.payload["nickname"],
        "content": target.payload["content"],
        "liked": True,
        "beforeCountText": before["countText"],
        "afterCountText": after["countText"],
        "xmlLiked": after["xmlLiked"],
        "visualLiked": after["visualLiked"],
    }


def _target_already_has_visible_reply(page, target, clean_text, quota_store=None, work_key=""):
    try:
        _, comments = visible_comments(page)
    except PageMismatch:
        return None
    target_idx = next((i for i, c in enumerate(comments) if c.key == target.key), None)
    if target_idx is None:
        return None
    for follower in comments[target_idx + 1:]:
        if not follower.is_reply:
            break
        body = follower.payload.get("content", "").strip()
        if (
            body == clean_text
            or body.endswith("：" + clean_text)
            or is_self_comment(follower, quota_store=quota_store, work_key=work_key)
        ):
            return follower
    return None


def reply_to_comment(
    device,
    selector,
    text,
    quota_store=None,
    enabled=True,
    work_key=None,
    max_scrolls=6,
    allow_self=False,
):
    """Reply to a specific target comment after verifying target identity, reply composer header, input, and send."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Reply text must be a non-empty string")
    clean_text = text.strip()
    if not enabled:
        return {"action": "reply_to_comment", "executed": False, "skipped": True, "reason": "disabled_by_config"}
    check_stopped(device)
    ensure_clean_page_on_resume(device)
    page = _current_page(device)
    resolved_work_key = work_key
    if not resolved_work_key:
        try:
            resolved_work_key = identify_current_work(page).get("workKey", "")
        except PageMismatch:
            resolved_work_key = ""

    target = locate_target_comment(
        device,
        selector,
        max_scrolls=max_scrolls,
        quota_store=quota_store,
        work_key=resolved_work_key,
        exclude_self=not allow_self,
    )
    if target.body_node is None:
        raise PageMismatch("Target comment does not expose a clickable body node")
    if not allow_self and is_self_comment(target, quota_store=quota_store, work_key=resolved_work_key):
        raise PageMismatch("Refusing to reply to own comment")
    target_nickname = target.payload["nickname"]
    target_content = target.payload["content"]
    page = _current_page(device)
    target_identity = comment_identity_key(resolved_work_key, target)

    if quota_store is not None and quota_store.has_record("comment_reply", target_identity):
        return {
            "action": "reply_to_comment",
            "executed": False,
            "skipped": True,
            "reason": "already_replied_in_ledger",
            "commentKey": target.key,
            "targetNickname": target_nickname,
            "targetContent": target_content,
            "text": clean_text,
        }
    existing_reply = _target_already_has_visible_reply(
        page,
        target,
        clean_text,
        quota_store=quota_store,
        work_key=resolved_work_key,
    )
    if existing_reply is not None:
        if quota_store is not None:
            quota_store.record_action("comment_reply", target_identity, quota_action=None)
        return {
            "action": "reply_to_comment",
            "executed": False,
            "skipped": True,
            "reason": "already_visible_on_page",
            "commentKey": target.key,
            "targetNickname": target_nickname,
            "targetContent": target_content,
            "text": clean_text,
            "replyKey": existing_reply.key,
        }
    if quota_store is not None and not quota_store.can_perform("comment"):
        return {
            "action": "reply_to_comment",
            "executed": False,
            "skipped": True,
            "reason": "quota_exceeded",
            "commentKey": target.key,
            "targetNickname": target_nickname,
            "targetContent": target_content,
            "text": clean_text,
            "limit": quota_store.limit_for("comment"),
            "used": quota_store.used_for("comment"),
        }

    before_count = comment_count(page)
    check_stopped(device)
    device.click(lambda current: _match_visible_comment(current, target.key).body_node)
    if getattr(device, "has_vision", False) is True:
        device.wait_visual_settle(timeout=0.6)

    def composer_or_menu(current):
        if _has_active_composer(current):
            return ("composer", _active_composer(current))
        if _has_comment_action_sheet(current):
            return ("menu", current)
        raise PageMismatch("Waiting for reply composer or comment action sheet")

    mode, data = device.wait(composer_or_menu, timeout=8)
    if mode == "menu":
        if not allow_self or _is_self_comment_action_sheet(data):
            _record_self_comment(quota_store, resolved_work_key, target_content, target)
            device.back()
            if getattr(device, "has_vision", False) is True:
                device.wait_visual_settle(timeout=0.6)
            raise PageMismatch(
                f"Target comment by @{target_nickname} opened self-comment menu; refusing to reply to own comment"
            )
        check_stopped(device)
        device.click(lambda current: current.unique(text="回复"))
        if getattr(device, "has_vision", False) is True:
            device.wait_visual_settle(timeout=0.6)
        data = device.wait(_active_composer, timeout=8)

    placeholder = data["hint"] or data["text"]
    if f"@{target_nickname}" not in placeholder and f"回复 {target_nickname}" not in placeholder:
        device.back()
        raise PageMismatch(
            f"Reply composer target mismatch: expected @{target_nickname}, got {placeholder!r}"
        )

    check_stopped(device)
    device.set_text(clean_text)
    device.wait(
        lambda current: _verified_composer_input(current, clean_text, expected_reply_nickname=target_nickname),
        timeout=8,
    )
    check_stopped(device)
    device.click(
        lambda current: _verified_composer_input(
            current, clean_text, expected_reply_nickname=target_nickname
        )["send"]
    )
    if getattr(device, "has_vision", False) is True:
        device.wait_visual_settle(timeout=0.8)

    def reply_send_completed(current):
        if _has_active_composer(current):
            raise PageMismatch("Reply composer is still open after clicking send")
        matched = _visible_comment_matching_text(current, clean_text, is_reply=True)
        after_count = comment_count(current)
        if matched is not None:
            return {
                "verifiedBy": "visible_reply",
                "replyKey": matched.key,
                "matchedReply": matched,
                "afterCount": after_count,
            }
        if before_count is not None and after_count is not None and after_count > before_count:
            return {
                "verifiedBy": "comment_count_increment",
                "replyKey": None,
                "matchedReply": None,
                "afterCount": after_count,
            }
        return {"verifiedBy": "composer_closed", "replyKey": None, "matchedReply": None, "afterCount": after_count}

    verification = device.wait(reply_send_completed, timeout=10)
    if quota_store is not None:
        quota_store.record_action("comment_reply", target_identity, quota_action="comment")
        _record_self_comment(quota_store, resolved_work_key, clean_text, verification.get("matchedReply"))
    return {
        "action": "reply_to_comment",
        "executed": True,
        "skipped": False,
        "commentKey": target.key,
        "targetNickname": target_nickname,
        "targetContent": target_content,
        "text": clean_text,
        "beforeCount": before_count,
        "afterCount": verification["afterCount"],
        "verifiedBy": verification["verifiedBy"],
        "replyKey": verification["replyKey"],
    }


def _card_matches_target_work(card, target_work):
    if not isinstance(target_work, dict) or not target_work:
        raise ValueError("target_work must be a non-empty dict")
    card_key = target_work.get("cardKey") or target_work.get("key")
    if card_key and card.key != card_key:
        return False
    note_type = target_work.get("noteType")
    if note_type and card.note_type != note_type:
        return False
    author = (target_work.get("authorNickname") or target_work.get("author") or "").strip()
    if author and card.author != author:
        return False
    title = strip_truncation_suffix(_clean_topic_text(target_work.get("title") or ""))
    card_title = strip_truncation_suffix(_clean_topic_text(card.title))
    if title and title != card_title and title not in card_title and card_title not in title:
        return False
    return bool(card_key or author or title)


def _scroll_feed_up(device, cards):
    if not cards:
        return
    left = min(c.box[0] for c in cards)
    top = min(c.box[1] for c in cards)
    right = max(c.box[2] for c in cards)
    bottom = max(c.box[3] for c in cards)
    x = str((left + right) // 2)
    device.command(
        "input",
        "swipe",
        x,
        str(top + (bottom - top) // 5),
        x,
        str(top + (bottom - top) * 4 // 5),
        "450",
    )
    time.sleep(0.4)


def _restore_feed_position(device, origin_cards, scrolls_done):
    current_cards = device.wait(lambda page: page.cards())
    for _ in range(scrolls_done):
        if [(c.key, c.box) for c in current_cards] == [(c.key, c.box) for c in origin_cards]:
            break
        _scroll_feed_up(device, current_cards)
        current_cards = device.wait(lambda page: page.cards())
    return current_cards


def preemptive_reply_comment(
    device,
    target_work,
    target_comment,
    reply_text,
    quota_store=None,
    max_feed_screens=4,
    max_comment_scrolls=6,
):
    """Locate a specified work and target comment to reply preemptively, then restore the original task state."""
    if not isinstance(target_work, dict) or not target_work:
        raise ValueError("target_work must be a non-empty dict")
    if not target_comment:
        raise ValueError("target_comment must be specified")
    if not isinstance(reply_text, str) or not reply_text.strip():
        raise ValueError("reply_text must be a non-empty string")
    if type(max_feed_screens) is not int or not 1 <= max_feed_screens <= 20:
        raise ValueError("max_feed_screens must be between 1 and 20")

    check_stopped(device)
    initial = ensure_clean_page_on_resume(device)
    origin_type = initial["pageType"]

    if origin_type == "discover_feed":
        origin_cards = initial["cards"]
        scrolls_done = 0
        matched_card = None
        current_cards = origin_cards
        for screen in range(max_feed_screens):
            check_stopped(device)
            if screen > 0:
                current_cards = device.wait(lambda page: page.cards())
            matches = [c for c in current_cards if _card_matches_target_work(c, target_work)]
            if len(matches) > 1:
                _restore_feed_position(device, origin_cards, scrolls_done)
                return {
                    "status": "target_work_ambiguous",
                    "executed": False,
                    "replied": False,
                    "restored": True,
                    "reason": "target_work_ambiguous",
                }
            if len(matches) == 1:
                matched_card = matches[0]
                break
            if screen + 1 < max_feed_screens:
                scroll_feed(device, current_cards)
                scrolls_done += 1

        if matched_card is None:
            _restore_feed_position(device, origin_cards, scrolls_done)
            return {
                "status": "target_work_not_found",
                "executed": False,
                "replied": False,
                "restored": True,
                "reason": "target_work_not_found",
            }

        left, top, right, bottom = matched_card.cover_box
        device.command("input", "tap", str((left + right) // 2), str((top + bottom) // 2))
        if getattr(device, "has_vision", False) is True:
            device.wait_visual_settle(timeout=0.8)

        opened_work = None
        drawer_opened = False
        try:
            opened_work = device.wait(identify_current_work, timeout=12)
            verify_card(matched_card, opened_work)
            if not _work_matches(opened_work, target_work):
                raise PageMismatch("Opened work does not match target_work specification")
            work_key = compute_work_key(opened_work)
            if opened_work.get("noteType") == "VIDEO":
                open_video_comment_drawer(device)
                drawer_opened = True
                settled_comments(device)
            else:
                _ensure_comments_open(device, fallback_work=opened_work)
            target = locate_target_comment(
                device,
                target_comment,
                max_scrolls=max_comment_scrolls,
                quota_store=quota_store,
                work_key=work_key,
                exclude_self=True,
            )
            reply_result = reply_to_comment(
                device,
                target.key,
                reply_text,
                quota_store=quota_store,
                enabled=True,
                work_key=work_key,
                max_scrolls=0,
            )
        except PageMismatch as error:
            if drawer_opened or is_video_comment_drawer(_current_page(device)):
                close_video_comment_drawer(device, opened_work)
            restore_feed(device, current_cards)
            _restore_feed_position(device, origin_cards, scrolls_done)
            return {
                "status": "target_comment_not_found",
                "executed": False,
                "replied": False,
                "restored": True,
                "reason": "target_comment_not_found",
                "detail": str(error),
            }

        if drawer_opened or is_video_comment_drawer(_current_page(device)):
            close_video_comment_drawer(device, opened_work)
        restore_feed(device, current_cards)
        _restore_feed_position(device, origin_cards, scrolls_done)
        return {
            "status": "preemptive_reply_completed" if reply_result.get("executed") else reply_result.get("reason", "skipped"),
            "executed": bool(reply_result.get("executed")),
            "replied": bool(reply_result.get("executed")),
            "restored": True,
            "reply": reply_result,
        }

    if origin_type in ("image_note", "video_note", "video_comment_drawer"):
        current_work = initial["work"]
        if not _work_matches(current_work, target_work):
            return {
                "status": "target_work_mismatch",
                "executed": False,
                "replied": False,
                "restored": True,
                "reason": "target_work_mismatch",
            }
        drawer_opened = False
        work_key = current_work.get("workKey") or compute_work_key(current_work)
        try:
            if origin_type == "video_note":
                open_video_comment_drawer(device)
                drawer_opened = True
                settled_comments(device)
            elif origin_type == "image_note":
                _ensure_comments_open(device, fallback_work=current_work)
            target = locate_target_comment(
                device,
                target_comment,
                max_scrolls=max_comment_scrolls,
                quota_store=quota_store,
                work_key=work_key,
                exclude_self=True,
            )
            reply_result = reply_to_comment(
                device,
                target.key,
                reply_text,
                quota_store=quota_store,
                enabled=True,
                work_key=work_key,
                max_scrolls=0,
            )
        except PageMismatch as error:
            if drawer_opened and is_video_comment_drawer(_current_page(device)):
                close_video_comment_drawer(device, current_work)
            return {
                "status": "target_comment_not_found",
                "executed": False,
                "replied": False,
                "restored": True,
                "reason": "target_comment_not_found",
                "detail": str(error),
            }
        if drawer_opened and is_video_comment_drawer(_current_page(device)):
            close_video_comment_drawer(device, current_work)
        return {
            "status": "preemptive_reply_completed" if reply_result.get("executed") else reply_result.get("reason", "skipped"),
            "executed": bool(reply_result.get("executed")),
            "replied": bool(reply_result.get("executed")),
            "restored": True,
            "reply": reply_result,
        }

    raise PageMismatch(f"Cannot run preemptive reply from unsupported page state: {origin_type}")


_PROFILE_EMPTY_TEXTS = {
    "还没有发布过笔记",
    "暂无笔记",
    "还没有发布笔记哦",
    "还没有内容哦",
}
_PROFILE_INACCESSIBLE_TEXTS = {
    "该用户已设置私密",
    "笔记不可见",
    "因相关投诉该账户违反《社区规范》",
    "账号已注销",
    "该用户不存在",
}
_FOLLOW_STATUS_TEXTS = {"关注", "+ 关注", "+关注", "回关", "已关注", "互相关注"}
_FOLLOWED_ACTIVE_TEXTS = {"已关注", "互相关注"}


def profile_follow_state(page, expected_name=None):
    """Inspect the follow button state in the profile header action bar alongside '发私信'."""
    if expected_name:
        commenter_profile(page, expected_name)
    dm_nodes = [
        n
        for n in page.nodes
        if n.get("class") == "android.widget.TextView" and n.get("text", "").strip() == "发私信"
    ]
    if len(dm_nodes) != 1:
        raise PageMismatch("No unambiguous '发私信' control found on profile action bar")
    dm_box = bounds(dm_nodes[0])
    labels = [
        n
        for n in page.nodes
        if n.get("class") == "android.widget.TextView"
        and n.get("text", "").strip() in _FOLLOW_STATUS_TEXTS
        and bounds(n)[2] <= dm_box[0] + 24
        and abs(bounds(n)[1] - dm_box[1]) <= 60
        and abs(bounds(n)[3] - dm_box[3]) <= 60
    ]
    if len(labels) != 1:
        raise PageMismatch("No unambiguous follow status label found in profile action bar")
    label = labels[0]
    status_text = label.get("text", "").strip()
    lbox = bounds(label)
    clickables = [
        n
        for n in page.nodes
        if n.get("clickable") == "true"
        and bounds(n)[2] <= dm_box[0] + 24
        and abs(bounds(n)[1] - dm_box[1]) <= 60
        and abs(bounds(n)[3] - dm_box[3]) <= 60
        and bounds(n)[0] <= lbox[0] + 10
        and bounds(n)[2] >= lbox[2] - 10
    ]
    button = clickables[0] if len(clickables) == 1 else label
    return {
        "button": button,
        "labelNode": label,
        "statusText": status_text,
        "followed": status_text in _FOLLOWED_ACTIVE_TEXTS,
    }


def follow_profile_user(device, expected_name=None, user_id=None, quota_store=None, enabled=True):
    """Follow the user on the current profile page if enabled and not already followed, verifying state change."""
    if not enabled:
        return {"action": "follow_user", "executed": False, "skipped": True, "reason": "disabled_by_config"}
    check_stopped(device)
    resumed = ensure_clean_page_on_resume(device)
    page = resumed["page"]
    profile_info = commenter_profile(page, expected_name) if expected_name else {}
    target_id = (user_id or profile_info.get("douyinId") or expected_name or "").strip()
    before = profile_follow_state(page, expected_name=expected_name)
    if before["followed"]:
        if quota_store is not None and target_id:
            quota_store.record_action("follow", target_id, quota_action=None)
        return {
            "action": "follow_user",
            "executed": False,
            "skipped": True,
            "reason": "already_followed",
            "userId": target_id,
            "followed": True,
            "statusText": before["statusText"],
        }
    if quota_store is not None and not quota_store.can_perform("follow"):
        return {
            "action": "follow_user",
            "executed": False,
            "skipped": True,
            "reason": "quota_exceeded",
            "userId": target_id,
            "followed": False,
            "limit": quota_store.limit_for("follow"),
            "used": quota_store.used_for("follow"),
        }

    check_stopped(device)
    device.click(lambda current: profile_follow_state(current, expected_name=expected_name)["button"])
    if getattr(device, "has_vision", False) is True:
        device.wait_visual_settle(timeout=0.8)
    ensure_clean_page_on_resume(device)

    def verified_followed(current):
        if _has_notification_prompt_modal(current):
            raise PageMismatch("Notification prompt modal still covers profile")
        state = profile_follow_state(current, expected_name=expected_name)
        if not state["followed"]:
            raise PageMismatch("Profile follow state did not become active after click")
        return state

    try:
        after = device.wait(verified_followed, timeout=8)
    except PageMismatch:
        ensure_clean_page_on_resume(device)
        after = device.wait(verified_followed, timeout=8)

    if quota_store is not None:
        quota_store.record_action("follow", target_id, quota_action="follow")
    return {
        "action": "follow_user",
        "executed": True,
        "skipped": False,
        "userId": target_id,
        "followed": True,
        "beforeStatus": before["statusText"],
        "afterStatus": after["statusText"],
    }


def profile_first_work_card(page, expected_name=None):
    """Locate the first work card on a user profile, or identify empty / inaccessible profile state."""
    profile_info = commenter_profile(page, expected_name) if expected_name else {}
    texts = {n.get("text", "").strip() for n in page.nodes if n.get("text", "").strip()}
    inaccessible = texts & _PROFILE_INACCESSIBLE_TEXTS
    if inaccessible:
        return {
            "status": "inaccessible",
            "reason": "profile_inaccessible",
            "detail": next(iter(inaccessible)),
            "cardNode": None,
        }
    notes_tabs = [
        n
        for n in page.nodes
        if n.get("content-desc", "").startswith("已选定笔记")
        or n.get("text", "").strip().startswith("笔记")
    ]
    min_top = max((bounds(n)[3] for n in notes_tabs), default=850) - 20
    cards = []
    for node in page.nodes:
        if node.get("clickable") != "true":
            continue
        desc = node.get("content-desc", "").strip()
        match = re.fullmatch(
            r"(视频|笔记)[,\s]+(.+?)[,\s]+来自(.+?)[,\s]+([\d.万亿]+)赞[，,\s]*",
            desc,
            re.DOTALL,
        )
        if not match:
            continue
        box = bounds(node)
        if box[1] < min_top or box[3] - box[1] < 160:
            continue
        cards.append((box[1], box[0], node, match, box))
    if cards:
        cards.sort(key=lambda item: (item[0], item[1]))
        _, _, card_node, match, box = cards[0]
        left, top, right, bottom = box
        tap_bottom = min(bottom - 120, top + 360) if bottom - top > 240 else bottom
        tap_box = (left, top, right, max(top + 40, tap_bottom))
        return {
            "status": "found",
            "reason": "ok",
            "cardNode": card_node,
            "noteType": "VIDEO" if match[1] == "视频" else "IMAGE_TEXT",
            "title": match[2].strip(),
            "author": match[3].strip(),
            "box": box,
            "tapBox": tap_box,
        }
    if (texts & _PROFILE_EMPTY_TEXTS) or profile_info.get("worksCountText") == "0":
        return {"status": "empty", "reason": "empty_profile_works", "cardNode": None}
    return {"status": "empty", "reason": "no_visible_profile_works", "cardNode": None}


def interact_profile_first_work(
    device,
    expected_name=None,
    like_enabled=True,
    collect_enabled=False,
    quota_store=None,
):
    """Open the first work on a user's profile, perform configured like/collect, and return to the profile."""
    if not like_enabled and not collect_enabled:
        return {"action": "profile_first_work", "executed": False, "skipped": True, "reason": "disabled_by_config"}
    check_stopped(device)
    resumed = ensure_clean_page_on_resume(device)
    page = resumed["page"]
    first = profile_first_work_card(page, expected_name=expected_name)
    if first["status"] != "found":
        return {
            "action": "profile_first_work",
            "executed": False,
            "skipped": True,
            "reason": first["reason"],
            "status": first["status"],
            "returnedToProfile": True,
        }

    left, top, right, bottom = first["tapBox"]
    check_stopped(device)
    device.command("input", "tap", str((left + right) // 2), str((top + bottom) // 2))
    if getattr(device, "has_vision", False) is True:
        device.wait_visual_settle(timeout=0.8)

    opened_work = device.wait(identify_current_work, timeout=12)
    work_key = opened_work.get("workKey") or compute_work_key(opened_work)
    like_res = (
        like_work(device, quota_store=quota_store, enabled=True, work_key=work_key)
        if like_enabled
        else {"action": "like_work", "executed": False, "skipped": True, "reason": "disabled_by_config"}
    )
    collect_res = (
        collect_work_action(device, quota_store=quota_store, enabled=True, work_key=work_key)
        if collect_enabled
        else {"action": "collect_work", "executed": False, "skipped": True, "reason": "disabled_by_config"}
    )

    check_stopped(device)
    device.back()
    if getattr(device, "has_vision", False) is True:
        device.wait_visual_settle(timeout=0.8)
    ensure_clean_page_on_resume(device)
    if expected_name:
        device.wait(lambda current: commenter_profile(current, expected_name), timeout=12)
    else:
        device.wait(lambda current: profile_follow_state(current), timeout=12)

    return {
        "action": "profile_first_work",
        "executed": bool(like_res.get("executed") or collect_res.get("executed")),
        "skipped": False,
        "workTitle": first["title"],
        "noteType": first["noteType"],
        "like": like_res,
        "collect": collect_res,
        "returnedToProfile": True,
    }


def touch_user_profile(
    device,
    expected_name,
    follow_enabled=True,
    like_first_work=True,
    collect_first_work=False,
    quota_store=None,
):
    """Execute configured profile touch actions on an open user profile and record profileActionsPerformed."""
    check_stopped(device)
    resumed = ensure_clean_page_on_resume(device)
    profile_info = commenter_profile(resumed["page"], expected_name)
    actions_performed = []
    follow_res = follow_profile_user(
        device,
        expected_name=expected_name,
        user_id=profile_info.get("douyinId"),
        quota_store=quota_store,
        enabled=follow_enabled,
    )
    if follow_res.get("executed"):
        actions_performed.append("FOLLOW")

    first_work_res = interact_profile_first_work(
        device,
        expected_name=expected_name,
        like_enabled=like_first_work,
        collect_enabled=collect_first_work,
        quota_store=quota_store,
    )
    if first_work_res.get("like", {}).get("executed"):
        actions_performed.append("LIKE_FIRST_WORK")
    if first_work_res.get("collect", {}).get("executed"):
        actions_performed.append("COLLECT_FIRST_WORK")

    touch_status = "TOUCHED" if actions_performed else "VISITED"
    return {
        **profile_info,
        "touchStatus": touch_status,
        "profileActionsPerformed": actions_performed,
        "followResult": follow_res,
        "firstWorkResult": first_work_res,
    }


def visit_and_touch_commenter_profile(
    device,
    selector,
    follow_enabled=True,
    like_first_work=True,
    collect_first_work=False,
    quota_store=None,
    max_scrolls=6,
):
    """Open a commenter's profile from the comment section, execute profile touch, and verify return to the comment position."""
    check_stopped(device)
    ensure_clean_page_on_resume(device)
    target = locate_target_comment(device, selector, max_scrolls=max_scrolls)
    expected_name = target.payload["nickname"]
    before_page = _current_page(device)
    is_video_drawer = is_video_comment_drawer(before_page)
    expected_count = comment_count(before_page)
    author, before_comments = visible_comments(before_page)
    before_positions = [(c.key, c.box) for c in before_comments]

    def same_comment_position(current):
        returned_author, returned_comments = visible_comments(current)
        if is_video_drawer:
            if not is_video_comment_drawer(current):
                raise PageMismatch("Did not return to video comment drawer")
        elif returned_author != author:
            raise PageMismatch("Did not return to original work comment section")
        if [(c.key, c.box) for c in returned_comments] != before_positions:
            raise PageMismatch("Comment section position changed after profile return")
        return current

    device.click(lambda current: _match_visible_comment(current, target.key).name_node)
    if getattr(device, "has_vision", False) is True:
        device.wait_visual_settle(timeout=0.8)

    try:
        device.wait(lambda current: commenter_profile(current, expected_name), timeout=15)
        result = touch_user_profile(
            device,
            expected_name=expected_name,
            follow_enabled=follow_enabled,
            like_first_work=like_first_work,
            collect_first_work=collect_first_work,
            quota_store=quota_store,
        )
    except PageMismatch as error:
        ensure_clean_page_on_resume(device)
        try:
            same_comment_position(_current_page(device))
        except PageMismatch:
            device.back()
            if getattr(device, "has_vision", False) is True:
                device.wait_visual_settle(timeout=0.8)
            device.wait(same_comment_position, timeout=20)
        return {
            "commentKey": target.key,
            "nickname": expected_name,
            "profileVisited": False,
            "touchStatus": "UNTOUCHED",
            "profileActionsPerformed": [],
            "reason": "profile_inaccessible",
            "detail": str(error),
            "returnedToCommentPosition": True,
        }

    device.back()
    if getattr(device, "has_vision", False) is True:
        device.wait_visual_settle(timeout=0.8)
    ensure_clean_page_on_resume(device)
    device.wait(same_comment_position, timeout=20)
    result["commentKey"] = target.key
    result["returnedToCommentPosition"] = True
    result["expectedCommentCount"] = expected_count
    return result


_DELETED_MESSAGE_MARKERS = (
    "该评论已删除",
    "评论已删除",
    "评论不见了",
    "该笔记已删除",
    "笔记已删除",
    "内容已删除",
    "内容不可见",
    "笔记不可见",
    "暂时无法查看",
)
_MESSAGE_ACTION_RE = re.compile(
    r"^(评论了你的笔记|回复了你的评论|在评论中@了你|在笔记中@了你|提到了你)(?:\s+.*)?$"
)


def parse_comment_at_messages(page):
    """Parse unread / visible comment and @ items in the '收到的评论和@' page."""
    headers = [
        n
        for n in page.nodes
        if n.get("class") == "android.widget.TextView" and n.get("text", "").strip() == "收到的评论和@"
    ]
    if len(headers) != 1:
        raise PageMismatch("Not on the '收到的评论和@' messages page")
    if any(n.get("text", "").strip().startswith("暂无消息") for n in page.nodes):
        return []
    header_bottom = bounds(headers[0])[3]
    rows = []
    for node in page.nodes:
        if node.get("class") not in ("android.view.ViewGroup", "android.widget.RelativeLayout", "android.widget.LinearLayout"):
            continue
        if node.get("clickable") != "true":
            continue
        box = bounds(node)
        if box[1] < header_bottom or box[0] > 40 or box[2] < 1000 or box[3] - box[1] < 100:
            continue
        text_nodes = [
            n
            for n in node.iter("node")
            if n.get("class") in ("android.widget.TextView", "android.widget.Button")
            and n.get("text", "").strip()
        ]
        if len(text_nodes) < 2:
            continue
        texts = [n.get("text", "").strip() for n in text_nodes]
        reply_btn = next(
            (
                n
                for n in node.iter("node")
                if n.get("text", "").strip() == "回复"
                or n.get("content-desc", "").strip() == "回复"
            ),
            None,
        )
        like_btn = next(
            (
                n
                for n in node.iter("node")
                if n.get("text", "").strip() in ("赞", "已赞", "点赞", "已点赞")
                or n.get("content-desc", "").strip() in ("赞", "已赞", "点赞", "已点赞")
            ),
            None,
        )
        non_control_texts = [
            t for t in texts if t not in ("回复", "赞", "已赞", "点赞", "已点赞", "回关", "关注")
        ]
        if not non_control_texts:
            continue
        nickname = non_control_texts[0]
        action_type = ""
        content = ""
        for item_text in non_control_texts[1:]:
            if not action_type and _MESSAGE_ACTION_RE.match(item_text):
                action_type = item_text.split()[0]
            elif not content:
                content = item_text
        if not content and len(non_control_texts) >= 2:
            content = non_control_texts[-1]
        deleted = any(marker in t for t in non_control_texts for marker in _DELETED_MESSAGE_MARKERS)
        like_text = (
            (like_btn.get("text", "").strip() or like_btn.get("content-desc", "").strip())
            if like_btn is not None
            else ""
        )
        liked = bool(
            like_btn is not None
            and (like_btn.get("selected") == "true" or like_text in ("已赞", "已点赞"))
        )
        raw_key = f"{nickname}|{action_type}|{content}"
        msg_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        rows.append(
            {
                "messageKey": msg_key,
                "nickname": nickname,
                "actionType": action_type or "comment_or_at",
                "content": content,
                "deleted": deleted,
                "rowNode": node,
                "replyButton": reply_btn,
                "likeButton": like_btn,
                "liked": liked,
                "box": box,
            }
        )
    deduped = []
    seen_keys = set()
    for item in sorted(rows, key=lambda r: (r["box"][1], r["box"][0])):
        if item["messageKey"] not in seen_keys:
            seen_keys.add(item["messageKey"])
            deduped.append(item)
    return deduped


def open_comment_at_messages(device):
    """Navigate to Message Center -> '收到的评论和@' and dismiss any notification prompt modal."""
    check_stopped(device)
    state = ensure_clean_page_on_resume(device)
    if state["pageType"] == "comment_at_messages":
        return state["page"]
    for _ in range(3):
        check_stopped(device)
        page = _current_page(device)
        if any(n.get("text", "").strip() == "收到的评论和@" for n in page.nodes):
            return page
        if any(n.get("content-desc") == "评论和@按钮" for n in page.nodes):
            break
        msg_tabs = [
            n
            for n in page.nodes
            if n.get("content-desc", "").startswith("消息")
            or (n.get("class") == "android.widget.TextView" and n.get("text", "").strip() == "消息" and bounds(n)[1] > 2100)
        ]
        if msg_tabs:
            device.click(lambda current: [
                n
                for n in current.nodes
                if n.get("content-desc", "").startswith("消息")
                or (n.get("class") == "android.widget.TextView" and n.get("text", "").strip() == "消息" and bounds(n)[1] > 2100)
            ][0])
            if getattr(device, "has_vision", False) is True:
                device.wait_visual_settle(timeout=0.8)
            ensure_clean_page_on_resume(device)
            break
        device.back()
        if getattr(device, "has_vision", False) is True:
            device.wait_visual_settle(timeout=0.6)
        ensure_clean_page_on_resume(device)

    device.wait(lambda current: current.unique(description="评论和@按钮"), timeout=10)
    device.click(lambda current: current.unique(description="评论和@按钮"))
    if getattr(device, "has_vision", False) is True:
        device.wait_visual_settle(timeout=0.8)
    ensure_clean_page_on_resume(device)
    return device.wait(
        lambda current: current
        if any(n.get("text", "").strip() == "收到的评论和@" for n in current.nodes)
        else (_ for _ in ()).throw(PageMismatch("Waiting for '收到的评论和@' page")),
        timeout=10,
    )


def handle_single_message(device, message_item, reply_text=None, like_enabled=True, quota_store=None):
    """Handle one comment/@ message in place (like and/or reply), skipping deleted or already-handled items."""
    check_stopped(device)
    msg_key = message_item["messageKey"]
    nickname = message_item["nickname"]
    content = message_item["content"]
    if quota_store is not None and quota_store.has_record("message_handled", msg_key):
        return {
            "messageKey": msg_key,
            "nickname": nickname,
            "content": content,
            "executed": False,
            "skipped": True,
            "reason": "already_handled",
        }
    if message_item.get("deleted"):
        if quota_store is not None:
            quota_store.record_action("message_handled", msg_key, quota_action=None)
        return {
            "messageKey": msg_key,
            "nickname": nickname,
            "content": content,
            "executed": False,
            "skipped": True,
            "reason": "deleted_or_unavailable",
        }

    liked_now = False
    replied_now = False
    if (
        like_enabled
        and message_item.get("likeButton") is not None
        and not message_item.get("liked")
        and (quota_store is None or quota_store.can_perform("like"))
    ):
        check_stopped(device)
        device.click(
            lambda current: next(
                m["likeButton"]
                for m in parse_comment_at_messages(current)
                if m["messageKey"] == msg_key and m.get("likeButton") is not None
            )
        )
        if getattr(device, "has_vision", False) is True:
            device.wait_visual_settle(timeout=0.6)
        liked_now = True
        if quota_store is not None:
            quota_store.consume("like")

    if (
        isinstance(reply_text, str)
        and reply_text.strip()
        and message_item.get("replyButton") is not None
        and (quota_store is None or quota_store.can_perform("comment"))
    ):
        clean_reply = reply_text.strip()
        check_stopped(device)
        device.click(
            lambda current: next(
                m["replyButton"]
                for m in parse_comment_at_messages(current)
                if m["messageKey"] == msg_key and m.get("replyButton") is not None
            )
        )
        if getattr(device, "has_vision", False) is True:
            device.wait_visual_settle(timeout=0.6)
        composer = device.wait(_active_composer, timeout=8)
        placeholder = composer["hint"] or composer["text"]
        if placeholder and nickname and nickname not in placeholder and "回复" in placeholder:
            device.back()
            raise PageMismatch(f"Message reply composer target mismatch: expected @{nickname}, got {placeholder!r}")
        check_stopped(device)
        device.set_text(clean_reply)
        device.wait(
            lambda current: _verified_composer_input(current, clean_reply, expected_reply_nickname=nickname),
            timeout=8,
        )
        check_stopped(device)
        device.click(
            lambda current: _verified_composer_input(current, clean_reply, expected_reply_nickname=nickname)["send"]
        )
        if getattr(device, "has_vision", False) is True:
            device.wait_visual_settle(timeout=0.8)
        device.wait(
            lambda current: current
            if not _has_active_composer(current)
            else (_ for _ in ()).throw(PageMismatch("Message reply composer still open")),
            timeout=8,
        )
        replied_now = True
        if quota_store is not None:
            quota_store.consume("comment")

    if quota_store is not None and (liked_now or replied_now):
        quota_store.record_action("message_handled", msg_key, quota_action=None)
    return {
        "messageKey": msg_key,
        "nickname": nickname,
        "content": content,
        "executed": bool(liked_now or replied_now),
        "skipped": not (liked_now or replied_now),
        "liked": liked_now,
        "replied": replied_now,
    }


def return_from_messages_to_task(device, max_steps=4):
    """Return from '收到的评论和@' / Message Center to the Discover task page with a bounded step limit."""
    if type(max_steps) is not int or not 1 <= max_steps <= 10:
        raise ValueError("max_steps must be between 1 and 10")
    check_stopped(device)
    for _ in range(max_steps):
        check_stopped(device)
        state = ensure_clean_page_on_resume(device)
        page = state["page"]
        if state["pageType"] == "discover_feed":
            return {"restored": True, "pageType": "discover_feed", "cardsCount": len(state["cards"])}
        if any(n.get("text", "").strip() == "收到的评论和@" for n in page.nodes):
            back_icons = [
                n
                for n in page.nodes
                if n.get("class") == "android.widget.ImageView"
                and n.get("clickable") == "true"
                and bounds(n)[0] <= 120
                and bounds(n)[1] <= 220
                and bounds(n)[3] <= 240
            ]
            if len(back_icons) == 1:
                device.click(lambda current: [
                    n
                    for n in current.nodes
                    if n.get("class") == "android.widget.ImageView"
                    and n.get("clickable") == "true"
                    and bounds(n)[0] <= 120
                    and bounds(n)[1] <= 220
                    and bounds(n)[3] <= 240
                ][0])
            else:
                device.back()
            if getattr(device, "has_vision", False) is True:
                device.wait_visual_settle(timeout=0.6)
            continue
        home_tabs = [n for n in page.nodes if n.get("content-desc") == "首页"]
        if len(home_tabs) == 1:
            device.click(lambda current: current.unique(description="首页"))
            if getattr(device, "has_vision", False) is True:
                device.wait_visual_settle(timeout=0.6)
            page_after = _current_page(device)
            if any(
                n.get("content-desc") == "发现" and n.get("selected") != "true"
                for n in page_after.nodes
            ):
                device.click(lambda current: current.unique(description="发现"))
                if getattr(device, "has_vision", False) is True:
                    device.wait_visual_settle(timeout=0.6)
            cards = device.wait(lambda current: current.cards(), timeout=10)
            return {"restored": True, "pageType": "discover_feed", "cardsCount": len(cards)}
        device.back()
        if getattr(device, "has_vision", False) is True:
            device.wait_visual_settle(timeout=0.6)

    open_discover(device)
    cards = device.wait(lambda current: current.cards(), timeout=10)
    return {"restored": True, "pageType": "discover_feed", "cardsCount": len(cards)}


def inspect_and_handle_messages(
    device,
    reply_text=None,
    like_enabled=True,
    max_messages=5,
    quota_store=None,
):
    """Inspect Message Center unread/recent comments & @, handle replyable items once, and return to task page."""
    if type(max_messages) is not int or not 1 <= max_messages <= 50:
        raise ValueError("max_messages must be between 1 and 50")
    check_stopped(device)
    page = open_comment_at_messages(device)
    messages = parse_comment_at_messages(page)
    handled = []
    for item in messages[:max_messages]:
        check_stopped(device)
        res = handle_single_message(
            device,
            item,
            reply_text=reply_text,
            like_enabled=like_enabled,
            quota_store=quota_store,
        )
        handled.append(res)
    returned = return_from_messages_to_task(device)
    return {
        "status": "messages_inspected",
        "empty": len(messages) == 0,
        "totalVisible": len(messages),
        "processed": handled,
        "returnedToTask": returned["restored"],
        "returnedPageType": returned["pageType"],
    }


def interact_work_and_comments(device, data_dir, options=None):
    """Run configured work/comment interactions with daily quota enforcement and save a report."""
    options = dict(options or {})
    started = time.monotonic()
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    quota_store = DailyQuotaStore(directory, limits=options.get("quotas"))

    helper = Path(__file__).with_name("ui-dump.dex")
    if helper.is_file():
        output = device.command(
            "env",
            "CLASSPATH=" + str(helper),
            "app_process",
            "/system/bin",
            "xhs.UiDump",
            "--resume-xhs",
        )
        if output.strip() != "resumed":
            raise PageMismatch("Could not resume the existing Xiaohongshu page")

    resumed = ensure_clean_page_on_resume(device, fallback_work=options.get("expectedWork"))
    results = {}

    if options.get("handleMessages"):
        msg_cfg = options["handleMessages"] if isinstance(options["handleMessages"], dict) else {}
        results["handleMessages"] = inspect_and_handle_messages(
            device,
            reply_text=msg_cfg.get("replyText"),
            like_enabled=msg_cfg.get("likeEnabled", True),
            max_messages=msg_cfg.get("maxMessages", 5),
            quota_store=quota_store,
        )
    elif options.get("preemptiveReply"):
        preemptive_cfg = options["preemptiveReply"]
        results["preemptiveReply"] = preemptive_reply_comment(
            device,
            target_work=preemptive_cfg["targetWork"],
            target_comment=preemptive_cfg["targetComment"],
            reply_text=preemptive_cfg["replyText"],
            quota_store=quota_store,
            max_feed_screens=preemptive_cfg.get("maxFeedScreens", 4),
            max_comment_scrolls=preemptive_cfg.get("maxCommentScrolls", 6),
        )
    elif options.get("touchProfile"):
        prof_cfg = options["touchProfile"]
        if prof_cfg.get("commentSelector"):
            results["touchProfile"] = visit_and_touch_commenter_profile(
                device,
                selector=prof_cfg["commentSelector"],
                follow_enabled=prof_cfg.get("follow", True),
                like_first_work=prof_cfg.get("likeFirstWork", True),
                collect_first_work=prof_cfg.get("collectFirstWork", False),
                quota_store=quota_store,
                max_scrolls=prof_cfg.get("maxScrolls", 6),
            )
        else:
            results["touchProfile"] = touch_user_profile(
                device,
                expected_name=prof_cfg["expectedName"],
                follow_enabled=prof_cfg.get("follow", True),
                like_first_work=prof_cfg.get("likeFirstWork", True),
                collect_first_work=prof_cfg.get("collectFirstWork", False),
                quota_store=quota_store,
            )
    else:
        work = resumed["work"] or identify_current_work(resumed["page"], fallback_work=options.get("expectedWork"))
        work_key = work.get("workKey") or compute_work_key(work)
        if "likeWork" in options:
            results["likeWork"] = like_work(
                device,
                quota_store=quota_store,
                enabled=bool(options.get("likeWork")),
                work_key=work_key,
                expected_work=options.get("expectedWork"),
            )
        if "collectWork" in options:
            results["collectWork"] = collect_work_action(
                device,
                quota_store=quota_store,
                enabled=bool(options.get("collectWork")),
                work_key=work_key,
                expected_work=options.get("expectedWork"),
            )
        if options.get("commentText"):
            results["postWorkComment"] = post_work_comment(
                device,
                text=options["commentText"],
                quota_store=quota_store,
                enabled=options.get("enableComment", True),
                work_key=work_key,
                expected_work=options.get("expectedWork"),
            )
        if options.get("likeComment"):
            results["likeComment"] = like_comment(
                device,
                selector=options["likeComment"],
                quota_store=quota_store,
                enabled=options.get("enableCommentLike", True),
                work_key=work_key,
                max_scrolls=options.get("maxCommentScrolls", 6),
            )
        if options.get("replyComment"):
            reply_cfg = options["replyComment"]
            results["replyComment"] = reply_to_comment(
                device,
                selector=reply_cfg["target"],
                text=reply_cfg["text"],
                quota_store=quota_store,
                enabled=reply_cfg.get("enabled", True),
                work_key=work_key,
                max_scrolls=reply_cfg.get("maxScrolls", 6),
            )

    report = {
        "status": "interactions_completed_locally",
        "results": results,
        "quotas": quota_store.snapshot(),
        "capturedAtUnixMs": time.time_ns() // 1_000_000,
        "elapsedMs": round((time.monotonic() - started) * 1000),
        "uploaded": False,
    }
    path = directory / ("interaction-" + uuid.uuid4().hex + ".json")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    temporary.replace(path)
    return report
