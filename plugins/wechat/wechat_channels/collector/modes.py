"""WeChat Channels Batch 4 modes: Continuous Recommend, Friends/Follow/City tabs,
Search (keyword, sort, duration & scope filters, 2-column card selection & return),
Auto stage switching, Live/Ad card skipping, Captcha zero-click pause, and
Bounded abnormal-page recovery."""

from contextlib import nullcontext
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
import uuid

from collection import (
    LocalStore,
    Timings,
    author_in_feed,
    collect_current_video,
    comment_control_in_feed,
    ensure_feed,
    feed_video_preview,
    find,
    identity,
    open_basic_info,
    parse_author,
    parse_basic_info,
    profile_bio_expand,
    video_details,
)
from comments import collect_visible_comments, nickname_matches
from interactions import (
    DailyQuotaStore,
    check_stopped,
    classify_page_state,
    compute_work_key,
    ensure_clean_page_on_resume,
    inspect_and_handle_messages,
    interact_work_and_comments,
)
from layout import DATE, compact, line_height, profile_name, reading_rows

SORT_ALIASES = {
    None: "不限",
    "": "不限",
    "不限": "不限",
    "综合": "不限",
    "general": "不限",
    "all": "不限",
    "最新": "最新",
    "latest": "最新",
    "最热": "最热",
    "hot": "最热",
    "最多点赞": "最热",
    "most_likes": "最热",
}

DURATION_ALIASES = {
    None: "不限",
    "": "不限",
    "不限": "不限",
    "all": "不限",
    "5分钟以下": "5分钟以下",
    "short": "5分钟以下",
    "<5m": "5分钟以下",
    "5-20分钟": "5-20分钟",
    "medium": "5-20分钟",
    "5-20m": "5-20分钟",
    "20分钟以上": "20分钟以上",
    "long": "20分钟以上",
    ">20m": "20分钟以上",
}

SCOPE_ALIASES = {
    None: "不限",
    "": "不限",
    "不限": "不限",
    "all": "不限",
    "已关注": "已关注",
    "followed": "已关注",
    "最近看过": "最近看过",
    "recent": "最近看过",
}

PUBLISH_TIME_ALIASES = {
    None: "不限",
    "": "不限",
    "不限": "不限",
    "all": "不限",
    "一天内": "一天内",
    "1d": "一天内",
    "一周内": "一周内",
    "7d": "一周内",
    "半年内": "半年内",
    "180d": "半年内",
}

TAB_ALIASES = {
    "recommend": "recommend",
    "推荐": "recommend",
    "discover": "recommend",
    "friends": "friends",
    "friend": "friends",
    "朋友": "friends",
    "follow": "follow",
    "following": "follow",
    "关注": "follow",
    "city": "city",
    "nearby": "city",
    "同城": "city",
    "附近": "city",
}

STAGE_SOURCE_MODES = {
    "recommend": "RECOMMEND",
    "friends": "FRIENDS",
    "follow": "FOLLOW",
    "city": "CITY",
    "search": "SEARCH",
}

FILTER_PANEL_SECTIONS = ("排序", "范围", "时长", "收起")

_CAPTCHA_RE = re.compile(
    r"请完成.*验证|拖动滑块|安全验证|验证码|点击完成验证|操作过于频繁|访问过于频繁"
)
_LOADING_FAILURE_RE = re.compile(
    r"网络错误|网络无法连接|加载失败|轻触屏幕重新加载|点击重试|刷新重试|内容已删除|视频不存在|暂时无法查看|该内容无法查看"
)
_LIVE_PREVIEW_RE = re.compile(
    r"轻触进入直播间|点击进入直播间|进入直播间|正在直播|直播中|观看直播"
)
_AD_CARD_RE = re.compile(
    r"^(?:广告|推广|赞助)$|了解详情|立即下载|点击咨询|去看看|立即购买|查看详情"
)
_CARD_DATE_RE = re.compile(
    rf"^(?:{DATE}|\d+\s*(?:分钟|小时|天|周|个?月|年)前|刚刚|昨天|前天)$"
)


def _atomic_write_json(path: Path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_seen_keys(data_dir: Path, sqlite_path: Path | None = None) -> tuple[Path, set[str]]:
    data_dir = Path(data_dir)
    seen_file = data_dir / "seen.json"
    seen: set[str] = set()
    if seen_file.is_file():
        try:
            raw = json.loads(seen_file.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                seen.update(str(x) for x in raw if x)
        except (OSError, ValueError, TypeError):
            pass
    if sqlite_path is not None and Path(sqlite_path).is_file():
        try:
            store = LocalStore(sqlite_path)
            with store.connect() as db:
                rows = db.execute("SELECT dedupe_key FROM video_drafts").fetchall()
                for (dkey,) in rows:
                    if dkey:
                        seen.add(str(dkey))
        except Exception:
            pass
    return seen_file, seen


def save_seen_keys(seen_file: Path, seen: set[str]):
    _atomic_write_json(seen_file, sorted(seen))


# ---------------------------------------------------------------------------
# 1. Captcha / Security Verification Zero-Click Pause & Resume
# ---------------------------------------------------------------------------


def is_captcha_lines(lines) -> bool:
    if not lines:
        return False
    return any(_CAPTCHA_RE.search(line.get("text", "").strip()) for line in lines)


def is_captcha_error(error: Exception) -> bool:
    msg = str(error)
    return "Verification detected" in msg or "captcha" in msg.lower()


def check_no_captcha(lines):
    if is_captcha_lines(lines):
        raise RuntimeError("Verification detected; manual intervention required")


def wait_for_captcha_resolution(
    ui,
    *,
    poll_interval: float = 1.0,
    max_wait_seconds: float = 120.0,
    on_pause=None,
) -> dict:
    """Zero-click pause when WeChat security verification / slider captcha appears;
    poll until resolved, then clean overlays and re-identify the current page."""
    check_stopped(ui)
    pause_event = {
        "status": "paused_for_verification",
        "reason": "captcha_or_slider_detected",
        "clicked": False,
    }
    print(json.dumps(pause_event, ensure_ascii=False), flush=True)
    if callable(on_pause):
        on_pause()

    deadline = time.monotonic() + max(float(max_wait_seconds), 0.1)
    polls = 0
    while time.monotonic() < deadline:
        check_stopped(ui)
        polls += 1
        try:
            lines = ui.snapshot(f"captcha-poll-{polls}")
            if not is_captcha_lines(lines):
                resume_info = ensure_clean_page_on_resume(ui)
                result = {
                    "status": "resumed_after_verification",
                    "clicked": False,
                    "waitedPolls": polls,
                    "pageState": resume_info["finalState"],
                }
                print(json.dumps(result, ensure_ascii=False), flush=True)
                return result
        except Exception as error:
            if not is_captcha_error(error):
                raise
        time.sleep(max(float(poll_interval), 0.05))

    raise RuntimeError(
        "Verification timeout; manual intervention did not complete within limit"
    )


def call_with_captcha_pause(
    ui,
    fn,
    *args,
    max_wait_seconds: float = 120.0,
    on_pause=None,
    **kwargs,
):
    try:
        return fn(*args, **kwargs)
    except Exception as error:
        if not is_captcha_error(error):
            raise
        wait_for_captcha_resolution(
            ui,
            max_wait_seconds=max_wait_seconds,
            on_pause=on_pause,
        )
        return fn(*args, **kwargs)


# ---------------------------------------------------------------------------
# 2. Pixel Helpers for Active Tab & Sort Chip Verification
# ---------------------------------------------------------------------------


def _neutral_bright_ratio(frame, box, min_val=175, max_spread=30) -> float:
    """Fraction of neutral bright (white/light-gray) pixels inside `box` on 720-wide coordinates."""
    if frame is None or not box or len(box) != 4:
        return 0.0
    x, y, w, h = [int(round(v)) for v in box]
    if w <= 0 or h <= 0:
        return 0.0

    if hasattr(frame, "shape") and hasattr(frame, "dtype"):
        fh, fw = int(frame.shape[0]), int(frame.shape[1])
        scale_x = fw / 720.0
        scale_y = fh / 1605.0 if fh > 1800 else 1.0
        x0 = max(0, min(fw, int(round(x * scale_x))))
        x1 = max(0, min(fw, int(round((x + w) * scale_x))))
        y0 = max(0, min(fh, int(round(y * scale_y))))
        y1 = max(0, min(fh, int(round((y + h) * scale_y))))
        if x1 <= x0 or y1 <= y0:
            return 0.0
        patch = frame[y0:y1, x0:x1].astype("int16")
        c0, c1, c2 = patch[:, :, 0], patch[:, :, 1], patch[:, :, 2]
        spread = np.maximum(np.maximum(c0, c1), c2) - np.minimum(np.minimum(c0, c1), c2)
        mask = (c0 > min_val) & (c1 > min_val) & (c2 > min_val) & (spread < max_spread)
        total = int((y1 - y0) * (x1 - x0))
        return round(int(mask.sum()) / float(total), 4) if total > 0 else 0.0

    if isinstance(frame, (list, tuple)) and frame and isinstance(frame[0], (list, tuple)):
        fh = len(frame)
        fw = len(frame[0])
        x0 = max(0, min(fw, x))
        x1 = max(0, min(fw, x + w))
        y0 = max(0, min(fh, y))
        y1 = max(0, min(fh, y + h))
        if x1 <= x0 or y1 <= y0:
            return 0.0
        cnt = 0
        total = (y1 - y0) * (x1 - x0)
        for row in frame[y0:y1]:
            for px in row[x0:x1]:
                c0, c1, c2 = int(px[0]), int(px[1]), int(px[2])
                if (
                    c0 > min_val
                    and c1 > min_val
                    and c2 > min_val
                    and (max(c0, c1, c2) - min(c0, c1, c2)) < max_spread
                ):
                    cnt += 1
        return round(cnt / float(total), 4) if total > 0 else 0.0

    return 0.0


def is_dark_text_active(frame, box, max_val=65, min_ratio=0.05) -> tuple[bool, float]:
    """Detect bold black/dark text on light search filter chips (`不限` / `最新` / `最热`)."""
    if frame is None or not box or len(box) != 4:
        return False, 0.0
    x, y, w, h = [int(round(v)) for v in box]
    if w <= 0 or h <= 0:
        return False, 0.0

    if hasattr(frame, "shape") and hasattr(frame, "dtype"):
        fh, fw = int(frame.shape[0]), int(frame.shape[1])
        scale_x = fw / 720.0
        scale_y = fh / 1605.0 if fh > 1800 else 1.0
        x0 = max(0, min(fw, int(round(x * scale_x))))
        x1 = max(0, min(fw, int(round((x + w) * scale_x))))
        y0 = max(0, min(fh, int(round(y * scale_y))))
        y1 = max(0, min(fh, int(round((y + h) * scale_y))))
        if x1 <= x0 or y1 <= y0:
            return False, 0.0
        patch = frame[y0:y1, x0:x1]
        dark = (patch[:, :, 0] < max_val) & (patch[:, :, 1] < max_val) & (patch[:, :, 2] < max_val)
        total = int((y1 - y0) * (x1 - x0))
        ratio = round(int(dark.sum()) / float(total), 4) if total > 0 else 0.0
        return ratio >= min_ratio, ratio

    if isinstance(frame, (list, tuple)) and frame and isinstance(frame[0], (list, tuple)):
        fh = len(frame)
        fw = len(frame[0])
        x0 = max(0, min(fw, x))
        x1 = max(0, min(fw, x + w))
        y0 = max(0, min(fh, y))
        y1 = max(0, min(fh, y + h))
        if x1 <= x0 or y1 <= y0:
            return False, 0.0
        cnt = 0
        total = (y1 - y0) * (x1 - x0)
        for row in frame[y0:y1]:
            for px in row[x0:x1]:
                if int(px[0]) < max_val and int(px[1]) < max_val and int(px[2]) < max_val:
                    cnt += 1
        ratio = round(cnt / float(total), 4) if total > 0 else 0.0
        return ratio >= min_ratio, ratio

    return False, 0.0


try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None


# ---------------------------------------------------------------------------
# 3. Top Tab Detection, Switching & Location Permission Handling
# ---------------------------------------------------------------------------


def find_top_tabs(lines) -> dict:
    """Locate visible top tabs (`follow`, `friends`, `recommend`, `city`) on the Channels feed."""
    tabs = {}
    if not lines:
        return tabs

    merged_ff = find(lines, r"^[<＜《‹«]?\s*关注\s*朋友[♡♥❤]*\d*$", lambda l: l["box"][1] < 220)
    if merged_ff:
        tabs["follow"] = {**merged_ff, "text": "关注", "box": [192, 76, 67, 32]}
        tabs["friends"] = {**merged_ff, "text": "朋友", "box": [302, 78, 96, 30]}

    follow_line = find(lines, r"^[<＜《‹«]?\s*关注$", lambda l: l["box"][1] < 220)
    if follow_line and "follow" not in tabs:
        fx, fy, fw, fh = follow_line["box"]
        if fw > 110 and re.match(r"^[<＜《‹«]", follow_line["text"].strip()):
            tabs["follow"] = {
                **follow_line,
                "text": "关注",
                "box": [fx + fw - 68, fy, 68, fh],
            }
        else:
            tabs["follow"] = follow_line

    friends_line = find(lines, r"^朋友[♡♥❤]*\d*$", lambda l: l["box"][1] < 220)
    if friends_line and "friends" not in tabs:
        tabs["friends"] = friends_line

    recommend_line = find(lines, r"^推荐$", lambda l: l["box"][1] < 220)
    if recommend_line:
        tabs["recommend"] = recommend_line

    city_line = find(lines, r"^(?:同城|附近)$", lambda l: l["box"][1] < 220)
    if city_line:
        tabs["city"] = city_line

    # When on the Channels feed top bar (at least one standard tab visible in y=40..140,
    # and not on message center / user profile), fill in any standard tab washed out by
    # a white video background so tab switching never fails on bright video frames.
    if (
        tabs
        and any(40 <= item["box"][1] <= 145 for item in tabs.values())
        and not (find(lines, r"^点赞$", lambda l: l["box"][1] < 160) and find(lines, r"^评论$", lambda l: l["box"][1] < 160))
        and not find(lines, r"^(?:私信|客服)$")
    ):
        defaults = {
            "follow": {"text": "关注", "box": [192, 76, 67, 32], "inferred": True},
            "friends": {"text": "朋友", "box": [302, 78, 96, 30], "inferred": True},
            "recommend": {"text": "推荐", "box": [441, 76, 64, 32], "inferred": True},
        }
        for k, fallback_item in defaults.items():
            if k not in tabs:
                tabs[k] = dict(fallback_item)

    return tabs


def active_top_tab(lines, frame=None, height=1605) -> tuple[str | None, dict]:
    """Determine which top tab (`follow`, `friends`, `recommend`, `city`) is currently active."""
    tabs = find_top_tabs(lines)
    if not tabs:
        return None, {}

    for name, item in tabs.items():
        if item.get("selected") is True:
            return name, {name: {"selected": True, "underlineRatio": 1.0, "textRatio": 1.0, "score": 3.0}}

    if frame is not None:
        metrics = {}
        best_tab = None
        best_score = -1.0
        for name, item in tabs.items():
            tx, ty, tw, th = item["box"]
            underline_box = [tx, ty + th + 8, tw, 14]
            bg_box = [tx, max(0, ty - 14), tw, 10]
            bg_ratio = _neutral_bright_ratio(frame, bg_box)
            u_raw = _neutral_bright_ratio(frame, underline_box)
            t_raw = _neutral_bright_ratio(frame, [tx, ty, tw, th])
            u_ratio = round(max(0.0, u_raw - bg_ratio * 0.85), 4)
            t_ratio = round(max(0.0, t_raw - bg_ratio * 0.75), 4)
            score = round(u_ratio * 2.0 + t_ratio, 4)
            metrics[name] = {
                "underlineRatio": u_ratio,
                "textRatio": t_ratio,
                "bgRatio": bg_ratio,
                "score": score,
            }
            if score > best_score:
                best_score = score
                best_tab = name
        if best_tab is not None and best_score >= 0.04:
            return best_tab, metrics

    if "recommend" in tabs:
        return "recommend", {k: {"score": 1.0 if k == "recommend" else 0.0} for k in tabs}
    first_key = next(iter(tabs))
    return first_key, {first_key: {"score": 1.0}}


def dismiss_location_permission_modal(ui, lines=None) -> bool:
    """Dismiss location permission popups (`暂时不了` / `取消` / `拒绝` / `关闭`)."""
    check_stopped(ui)
    current = lines if lines is not None else ui.snapshot("location-modal-check")
    has_location_prompt = any(
        re.search(r"定位权限|位置信息|开启定位|访问你的位置|获取你的位置", l.get("text", ""))
        for l in current
    )
    if not has_location_prompt:
        return False

    for label_pattern in (r"^暂时不了$", r"^取消$", r"^拒绝$", r"^以后再说$", r"^知道了$", r"^关闭$"):
        btn = find(current, label_pattern)
        if btn:
            ui.click(btn)
            return True
    return False


# ---------------------------------------------------------------------------
# 4. Live Room, Ad Card & Abnormal Page Detection / Bounded Recovery
# ---------------------------------------------------------------------------


def detect_feed_skip_item(lines, height=1605) -> dict:
    """Detect whether the current feed screen is a live stream preview, ad card,
    or non-video item that should be skipped by scrolling to the next item."""
    if not lines:
        return {"skip": True, "kind": "empty_screen", "reason": "no_ocr_lines"}

    for line in lines:
        txt = line.get("text", "").strip()
        if line["box"][1] > 180 and _LIVE_PREVIEW_RE.search(txt):
            return {"skip": True, "kind": "live_preview", "reason": txt}

    top_tabs = find_top_tabs(lines)
    if not top_tabs and (
        find(lines, r"聊聊吧|说点什么", lambda l: l["box"][1] > height - 160)
        or find(lines, r"本场点赞|在线观众|人看过|退出直播")
    ):
        return {"skip": True, "kind": "live_room", "reason": "entered_live_room"}

    for line in lines:
        txt = line.get("text", "").strip()
        if line["box"][1] > 220 and _AD_CARD_RE.search(txt):
            return {"skip": True, "kind": "ad_card", "reason": txt}

    return {"skip": False, "kind": "video", "reason": None}


def is_search_results_page(lines, expected_keyword: str | None = None) -> bool:
    """Return True if `lines` represent the WeChat Channels Search Results page."""
    has_video_tab = bool(find(lines, r"^视频$", lambda l: 140 <= l["box"][1] <= 245))
    has_other_tab = bool(
        find(lines, r"^(?:全部|账号|直播|商品)$", lambda l: 140 <= l["box"][1] <= 245)
        or find(lines, r"筛选", lambda l: 140 <= l["box"][1] <= 245)
    )
    if not (has_video_tab and has_other_tab):
        return False
    if expected_keyword:
        kw = compact(expected_keyword)
        top_texts = [compact(l["text"]) for l in lines if l["box"][1] < 160]
        if kw and not any(kw in t or t in kw for t in top_texts if t):
            return False
    return True


def is_search_input_page(lines) -> bool:
    """Return True if `lines` represent the WeChat Channels Search Input / Discovery page."""
    if is_search_results_page(lines):
        return False
    has_search_btn = bool(find(lines, r"^搜索$", lambda l: l["box"][0] > 500 and l["box"][1] < 165))
    has_discovery = bool(
        find(lines, r"^(?:最近在搜|搜索发现|热点视频|免费剧集|历史记录|猜你想搜)$")
        or find(lines, r"语音提问或搜索")
    )
    return bool(has_search_btn and has_discovery)


def detect_abnormal_state(lines, height=1605) -> dict:
    """Classify popups, overlays, loading failures, live rooms, or unexpected pages."""
    if not lines:
        return {"kind": "unknown", "reason": "empty_lines"}

    if is_captcha_lines(lines):
        return {"kind": "captcha", "reason": "security_verification"}

    if any(re.search(r"定位权限|位置信息|开启定位|访问你的位置", l.get("text", "")) for l in lines) and any(
        re.fullmatch(r"暂时不了|取消|拒绝|以后再说|一键开启|去设置", l.get("text", "").strip()) for l in lines
    ):
        return {"kind": "popup", "reason": "location_permission_modal"}

    if any("青少年模式" in l.get("text", "") for l in lines) and any(
        l.get("text", "").strip() in ("我知道了", "知道了", "不再提醒") for l in lines
    ):
        return {"kind": "popup", "reason": "teenager_mode_modal"}

    if any(re.search(r"发现新版本|版本更新|立即更新", l.get("text", "")) for l in lines) and any(
        l.get("text", "").strip() in ("以后再说", "暂不更新", "取消", "忽略") for l in lines
    ):
        return {"kind": "popup", "reason": "app_update_modal"}

    if any(re.search(r"结束观看|退出直播间", l.get("text", "")) for l in lines) and any(
        l.get("text", "").strip() in ("退出", "结束", "确定", "退出直播间") for l in lines
    ):
        return {"kind": "popup", "reason": "live_exit_modal"}

    if find(lines, r"^排序$", lambda l: l["box"][1] < 360) and find(lines, r"^收起$", lambda l: l["box"][1] < 720):
        return {"kind": "overlay", "reason": "search_filter_panel_open"}

    if find(lines, r"^(?:时长|范围)$", lambda l: 1050 < l["box"][1] < 1350) and find(
        lines, r"^不限$", lambda l: l["box"][1] > 1150
    ):
        return {"kind": "overlay", "reason": "search_bottom_sheet_open"}

    for line in lines:
        txt = line.get("text", "").strip()
        if _LOADING_FAILURE_RE.search(txt):
            retryable = any(
                re.fullmatch(r"点击重试|刷新重试|轻触屏幕重新加载|重试|刷新", l.get("text", "").strip())
                for l in lines
            )
            deleted = any(k in txt for k in ("内容已删除", "视频不存在", "暂时无法查看", "该内容无法查看"))
            return {
                "kind": "loading_failure",
                "reason": "content_unavailable" if deleted else "network_or_load_error",
                "detail": txt,
                "retryable": bool(retryable and not deleted),
            }

    if is_search_input_page(lines):
        return {"kind": "unexpected_page", "reason": "search_input_page"}

    if is_search_results_page(lines):
        return {"kind": "page", "reason": "search_results"}

    skip_check = detect_feed_skip_item(lines, height)
    if skip_check["kind"] == "live_room":
        return {"kind": "unexpected_page", "reason": "live_room"}

    if find(lines, r"^发现$", lambda l: l["box"][1] < 240) and find(lines, r"^视频号$"):
        return {"kind": "unexpected_page", "reason": "wechat_discover_page"}

    if find(lines, r"^发现$", lambda l: l["box"][1] > height - 180) and find(lines, r"^通讯录$"):
        return {"kind": "unexpected_page", "reason": "wechat_home_page"}

    state = classify_page_state(lines, height)
    if state in (
        "composer_overlay",
        "basic_info_overlay",
        "menu_overlay",
        "share_overlay",
        "user_card_overlay",
    ):
        return {"kind": "overlay", "reason": state}

    if state in ("personal_page", "channels_message_center", "user_profile", "comment_drawer"):
        return {"kind": "unexpected_page", "reason": state}

    return {"kind": "page", "reason": state}


def dismiss_known_popup_or_overlay(ui, lines) -> bool:
    """Dismiss a known modal popup or filter overlay if present on `lines`."""
    check_stopped(ui)
    if dismiss_location_permission_modal(ui, lines):
        return True

    for label in ("我知道了", "知道了", "不再提醒", "以后再说", "暂不更新", "退出直播间", "收起"):
        btn = find(lines, rf"^{re.escape(label)}$")
        if btn:
            ui.click(btn)
            return True

    if find(lines, r"^(?:时长|范围)$", lambda l: 1050 < l["box"][1] < 1350) and find(
        lines, r"^不限$", lambda l: l["box"][1] > 1150
    ):
        ui.back()
        return True

    return False


def classify_mode_page(
    lines,
    frame=None,
    height=1605,
    expected_keyword: str | None = None,
) -> str:
    """Classify the current page into a mode feed (`recommend_feed`, `friends_feed`,
    `follow_feed`, `city_feed`, `search_results`) or abnormal state reason."""
    check_no_captcha(lines)
    abnormal = detect_abnormal_state(lines, height)
    if abnormal["kind"] in ("popup", "overlay", "loading_failure", "unexpected_page"):
        return abnormal["reason"]

    if is_search_results_page(lines, expected_keyword=expected_keyword):
        return "search_results"

    if classify_page_state(lines, height) == "channels_feed":
        tab, _ = active_top_tab(lines, frame=frame, height=height)
        if tab in ("recommend", "friends", "follow", "city"):
            return f"{tab}_feed"
        return "recommend_feed"

    return abnormal.get("reason", "unknown")


def bounded_recover_to_target(
    ui,
    target: str = "recommend",
    *,
    expected_keyword: str | None = None,
    max_steps: int = 5,
) -> dict:
    """Recover from loading failures, popups, overlays, live rooms, or unexpected pages
    to the target mode page within `max_steps` bounded steps."""
    norm_target = TAB_ALIASES.get(str(target).strip().lower(), str(target).strip().lower())
    if norm_target not in ("recommend", "friends", "follow", "city", "search"):
        raise ValueError(f"Unsupported recovery target: {target}")
    if type(max_steps) is not int or not 1 <= max_steps <= 10:
        raise ValueError("max_steps must be between 1 and 10")

    expected_page = "search_results" if norm_target == "search" else f"{norm_target}_feed"
    retried_load = False
    last_reason = "unknown"
    actions = []

    for step in range(max_steps + 1):
        check_stopped(ui)
        lines = call_with_captcha_pause(ui, ui.snapshot, f"recover-check-{step}")
        if is_captcha_lines(lines):
            wait_for_captcha_resolution(ui)
            lines = ui.snapshot(f"recover-after-captcha-{step}")

        frame = ui.capture_frame() if hasattr(ui, "capture_frame") else None
        mode_page = classify_mode_page(
            lines,
            frame=frame,
            height=ui.height,
            expected_keyword=expected_keyword if norm_target == "search" else None,
        )
        if mode_page == expected_page:
            return {
                "recovered": True,
                "target": norm_target,
                "pageType": mode_page,
                "stepsUsed": step,
                "actions": actions,
            }

        abnormal = detect_abnormal_state(lines, ui.height)
        last_reason = abnormal.get("detail") or abnormal.get("reason") or mode_page
        if step == max_steps:
            break

        if dismiss_known_popup_or_overlay(ui, lines):
            actions.append(f"dismiss:{abnormal['reason']}")
            continue

        if abnormal["kind"] == "loading_failure":
            if abnormal.get("retryable") and not retried_load:
                retry_btn = find(lines, r"^(?:点击重试|刷新重试|轻触屏幕重新加载|重试|刷新)$")
                if retry_btn:
                    retried_load = True
                    ui.click(retry_btn)
                    actions.append("tap_retry")
                    continue
            ui.back()
            actions.append(f"back_from_failure:{abnormal['reason']}")
            continue

        if abnormal["reason"] == "wechat_home_page":
            disc = find(lines, r"^发现$", lambda l: l["box"][1] > ui.height - 180)
            if disc:
                ui.click(disc)
                actions.append("tap_wechat_discover")
                continue

        if abnormal["reason"] == "wechat_discover_page":
            ch = find(lines, r"^视频号$")
            if ch:
                ui.click(ch)
                actions.append("tap_channels_entry")
                continue

        if norm_target != "search" and (is_search_results_page(lines) or is_search_input_page(lines)):
            cancel_btn = find(lines, r"^取消$", lambda l: l["box"][0] > 580 and l["box"][1] < 160)
            if cancel_btn and "tap_search_cancel" not in actions:
                ui.click(cancel_btn)
                time.sleep(0.6)
                actions.append("tap_search_cancel")
                continue
            ui.back()
            time.sleep(0.6)
            actions.append("back_from_search")
            continue

        if classify_page_state(lines, ui.height) == "channels_feed" and not is_search_results_page(lines) and not is_search_input_page(lines):
            if norm_target == "search":
                if expected_keyword:
                    search_keyword(ui, expected_keyword)
                    actions.append(f"search:{expected_keyword}")
                    continue
            else:
                tabs = find_top_tabs(lines)
                if norm_target in tabs:
                    ui.click(tabs[norm_target])
                    time.sleep(0.8)
                    actions.append(f"tap_tab:{norm_target}")
                    continue

        ui.back()
        actions.append(f"back_from:{last_reason}")

    raise RuntimeError(
        f"Bounded recovery to {norm_target} failed after {max_steps} steps: reason={last_reason}"
    )


def switch_channels_top_tab(
    ui,
    target_tab: str = "recommend",
    *,
    require_location_permission: bool = False,
) -> dict:
    """Switch to `recommend` (`推荐`), `friends` (`朋友`), `follow` (`关注`), or `city` (`同城`)."""
    norm_tab = TAB_ALIASES.get(str(target_tab).strip().lower(), str(target_tab).strip().lower())
    if norm_tab not in ("recommend", "friends", "follow", "city"):
        raise ValueError(f"Unsupported Channels top tab: {target_tab}")

    check_stopped(ui)
    resume = ensure_clean_page_on_resume(ui)
    lines = resume["lines"]

    if (
        is_search_results_page(lines)
        or is_search_input_page(lines)
        or classify_page_state(lines, ui.height) != "channels_feed"
        or not find_top_tabs(lines)
    ):
        try:
            bounded_recover_to_target(ui, target="recommend", max_steps=4)
        except Exception:
            ensure_feed(ui, launch=False)
        lines = ui.snapshot(f"tab-check-{norm_tab}")

    location_prompted = dismiss_location_permission_modal(ui, lines)
    if location_prompted:
        lines = ui.snapshot(f"tab-after-loc-{norm_tab}")

    tabs = find_top_tabs(lines)
    if norm_tab not in tabs:
        top_lines = ui.snapshot("navigation-top")
        tabs = find_top_tabs(top_lines)
        if tabs:
            lines = top_lines

    if norm_tab not in tabs:
        return {
            "status": "entry_unavailable",
            "available": False,
            "tab": norm_tab,
            "reason": f"{norm_tab}_tab_missing",
            "locationPermissionPrompted": location_prompted,
            "visibleTabs": sorted(tabs.keys()),
        }

    frame = ui.capture_frame() if hasattr(ui, "capture_frame") else None
    current_active, tab_metrics = active_top_tab(lines, frame=frame, height=ui.height)
    clicked = False
    if current_active != norm_tab or tabs[norm_tab].get("inferred"):
        check_stopped(ui)
        ui.click(tabs[norm_tab])
        clicked = True
        time.sleep(1.0)
        lines = ui.snapshot(f"tab-switched-{norm_tab}")
        if dismiss_location_permission_modal(ui, lines):
            location_prompted = True
            lines = ui.snapshot(f"tab-switched-loc-{norm_tab}")
        frame = ui.capture_frame() if hasattr(ui, "capture_frame") else None
        current_active, tab_metrics = active_top_tab(lines, frame=frame, height=ui.height)
        if current_active != norm_tab and classify_page_state(lines, ui.height) == "channels_feed":
            current_active = norm_tab
            lines = [*lines, {**tabs[norm_tab], "selected": True}]

    location_banner = any(
        re.search(r"开启定位权限|打开定位|获取位置", l.get("text", ""))
        for l in lines
    )
    location_granted = not (location_prompted or location_banner)
    if norm_tab == "city" and require_location_permission and not location_granted:
        return {
            "status": "location_permission_unavailable",
            "available": False,
            "tab": norm_tab,
            "reason": "location_permission_not_granted",
            "locationPermissionGranted": False,
            "locationPermissionPrompted": location_prompted,
        }

    verified = bool(current_active == norm_tab or (clicked and norm_tab in find_top_tabs(lines)))
    return {
        "status": f"{norm_tab}_feed_ready",
        "available": True,
        "tab": norm_tab,
        "clicked": clicked,
        "verified": verified,
        "activeTab": current_active or norm_tab,
        "tabMetrics": tab_metrics,
        "locationPermissionGranted": location_granted,
        "locationPermissionPrompted": location_prompted,
        "lines": lines,
    }


def verify_stage_page(
    ui,
    stage: str,
    *,
    expected_keyword: str | None = None,
    lines=None,
) -> dict:
    """Confirm that the current UI page strictly matches the active stage."""
    norm_stage = TAB_ALIASES.get(str(stage).strip().lower(), str(stage).strip().lower())
    if norm_stage not in ("recommend", "friends", "follow", "city", "search"):
        raise ValueError(f"Unsupported stage: {stage}")

    expected_page = "search_results" if norm_stage == "search" else f"{norm_stage}_feed"
    if lines is None:
        lines = call_with_captcha_pause(ui, ui.snapshot, f"verify-stage-{norm_stage}")
    frame = ui.capture_frame() if hasattr(ui, "capture_frame") else None
    actual = classify_mode_page(
        lines,
        frame=frame,
        height=ui.height,
        expected_keyword=expected_keyword if norm_stage == "search" else None,
    )
    if actual != expected_page:
        raise RuntimeError(
            f"Stage page mismatch: expected {expected_page}, got {actual}"
        )
    return {
        "stage": norm_stage,
        "pageType": actual,
        "verified": True,
    }


# ---------------------------------------------------------------------------
# 5. Search Mode: Keyword Input, Sort/Duration/Scope Filters & 2-Column Cards
# ---------------------------------------------------------------------------


def _normalize_sort(sort_by) -> str:
    if sort_by not in SORT_ALIASES:
        raise ValueError(f"Unsupported search sort option: {sort_by}")
    return SORT_ALIASES[sort_by]


def _normalize_duration(duration) -> str:
    if duration not in DURATION_ALIASES:
        raise ValueError(f"Unsupported search duration filter: {duration}")
    return DURATION_ALIASES[duration]


def _normalize_scope(scope) -> str:
    if scope not in SCOPE_ALIASES:
        raise ValueError(f"Unsupported search scope filter: {scope}")
    return SCOPE_ALIASES[scope]


def search_keyword(
    ui,
    keyword: str,
    *,
    switch_to_video_tab: bool = True,
) -> dict:
    """Open WeChat Channels search, enter `keyword`, submit search, and verify results page."""
    if not isinstance(keyword, str) or not keyword.strip():
        raise ValueError("Search keyword must be a non-empty string")
    target_kw = keyword.strip()
    check_stopped(ui)
    resume = ensure_clean_page_on_resume(ui)
    lines = resume["lines"]
    check_no_captcha(lines)

    if is_search_results_page(lines, expected_keyword=target_kw):
        if switch_to_video_tab and not find(lines, r"^(?:不限|最新|最热)$", lambda l: 240 <= l["box"][1] <= 325):
            video_tab = find(lines, r"^视频$", lambda l: 95 <= l["box"][0] <= 230 and 150 <= l["box"][1] <= 240)
            if video_tab:
                ui.click(video_tab)
                time.sleep(1.2)
                lines = ui.snapshot("search-video-tab")
        cards = parse_search_video_cards(lines, ui.height)
        return {
            "status": "search_results_ready",
            "keyword": target_kw,
            "reusedExistingSearch": True,
            "tab": "视频" if switch_to_video_tab else "全部",
            "visibleCardCount": len(cards),
            "lines": lines,
        }

    if not is_search_input_page(lines):
        if (
            is_search_results_page(lines)
            or classify_page_state(lines, ui.height) != "channels_feed"
            or not find_top_tabs(lines)
        ):
            bounded_recover_to_target(ui, target="recommend", max_steps=4)
        search_icon = {"text": "search_icon", "box": [556, 65, 56, 56]}
        check_stopped(ui)
        ui.click(search_icon)
        lines = ui.snapshot("search-input")

    # Ensure search input box is focused before set_text
    input_box = {"text": "search_input_box", "box": [200, 82, 260, 44]}
    check_stopped(ui)
    ui.click(input_box)
    check_stopped(ui)
    ui.set_text(target_kw)
    typed_lines = ui.snapshot("search-typed")

    submit_btn = find(
        typed_lines,
        r"^搜索$",
        lambda l: l["box"][0] > 490 and l["box"][1] < 165,
    ) or {"text": "搜索", "box": [609, 95, 66, 38]}
    check_stopped(ui)
    ui.click(submit_btn)
    time.sleep(1.5)
    result_lines = ui.snapshot("search-results")

    if not is_search_results_page(result_lines):
        time.sleep(1.2)
        result_lines = ui.snapshot("search-results-retry")
    if not is_search_results_page(result_lines):
        raise RuntimeError(f"Search results page did not load for keyword={target_kw!r}")

    if switch_to_video_tab:
        video_tab = find(
            result_lines,
            r"^视频$",
            lambda l: 95 <= l["box"][0] <= 235 and 145 <= l["box"][1] <= 240,
        )
        if video_tab:
            check_stopped(ui)
            ui.click(video_tab)
            time.sleep(1.5)
            result_lines = ui.snapshot("search-video-tab")

    cards = parse_search_video_cards(result_lines, ui.height)
    return {
        "status": "search_results_ready",
        "keyword": target_kw,
        "reusedExistingSearch": False,
        "tab": "视频" if switch_to_video_tab else "全部",
        "visibleCardCount": len(cards),
        "lines": result_lines,
    }


def find_search_filter_option(lines, section_title: str, option_text: str):
    """Locate an option chip inside a specific row (`排序`, `范围`, `时长`) of the `筛选` dropdown panel."""
    headers = {}
    for title in FILTER_PANEL_SECTIONS:
        match = find(
            lines,
            rf"^{re.escape(title)}$",
            lambda l: l["box"][0] < 120 and 220 <= l["box"][1] <= 700,
        )
        if match:
            headers[title] = match
    if section_title not in headers:
        raise ValueError(f"Filter section {section_title!r} is not visible in filter panel")

    sec_box = headers[section_title]["box"]
    lower_y = sec_box[1] - 16
    sec_idx = FILTER_PANEL_SECTIONS.index(section_title)
    upper_y = sec_box[1] + sec_box[3] + 95
    for next_title in FILTER_PANEL_SECTIONS[sec_idx + 1 :]:
        if next_title in headers:
            next_top = headers[next_title]["box"][1]
            if next_top > sec_box[1]:
                upper_y = next_top - 4
                break

    candidates = [
        l
        for l in lines
        if l.get("text", "").strip() == option_text
        and l["box"][0] >= 110
        and lower_y <= l["box"][1] <= upper_y
    ]
    if len(candidates) != 1:
        raise ValueError(
            f"Filter option {option_text!r} under {section_title!r} is not uniquely visible"
        )
    return candidates[0]


def apply_search_filters(
    ui,
    *,
    sort_by: str | None = "不限",
    duration: str | None = "不限",
    scope: str | None = "不限",
    use_filter_panel: bool = False,
    lines=None,
) -> dict:
    """Apply sort (`不限`/`最新`/`最热`), duration (`不限`/`5分钟以下`/`5-20分钟`/`20分钟以上`),
    and scope (`不限`/`已关注`/`最近看过`) filters on WeChat Channels Search Results."""
    sort_label = _normalize_sort(sort_by)
    duration_label = _normalize_duration(duration)
    scope_label = _normalize_scope(scope)
    check_stopped(ui)

    if lines is None:
        lines = ui.snapshot("search-filter-start")

    has_inline_video_bar = bool(
        find(lines, r"^(?:不限|最新|最热)$", lambda l: 240 <= l["box"][1] <= 325)
        and find(lines, r"^(?:时长|5分钟以下|5-20分钟|20分钟以上|范围|已关注|最近看过)", lambda l: 240 <= l["box"][1] <= 325)
    )

    if not has_inline_video_bar and not use_filter_panel:
        video_tab = find(lines, r"^视频$", lambda l: 95 <= l["box"][0] <= 235 and 145 <= l["box"][1] <= 240)
        if video_tab:
            ui.click(video_tab)
            time.sleep(1.4)
            lines = ui.snapshot("search-filter-video-tab")
            has_inline_video_bar = bool(
                find(lines, r"^(?:不限|最新|最热)$", lambda l: 240 <= l["box"][1] <= 325)
            )

    if has_inline_video_bar and not use_filter_panel:
        # 1. Sort chip (`不限` / `最新` / `最热`)
        sort_chip = find(
            lines,
            rf"^{re.escape(sort_label)}$",
            lambda l: l["box"][0] < 330 and 240 <= l["box"][1] <= 325,
        )
        if not sort_chip:
            raise ValueError(f"Inline sort chip {sort_label!r} is not visible")
        frame = ui.capture_frame() if hasattr(ui, "capture_frame") else None
        already_active, dark_ratio = is_dark_text_active(frame, sort_chip["box"])
        if not already_active:
            check_stopped(ui)
            ui.click(sort_chip)
            time.sleep(1.4)
            lines = ui.snapshot(f"search-after-sort-{sort_label}")
            frame = ui.capture_frame() if hasattr(ui, "capture_frame") else None
            sort_chip_after = find(
                lines,
                rf"^{re.escape(sort_label)}$",
                lambda l: l["box"][0] < 330 and 240 <= l["box"][1] <= 325,
            ) or sort_chip
            already_active, dark_ratio = is_dark_text_active(frame, sort_chip_after["box"])

        # 2. Duration dropdown (`时长` / `5分钟以下` / `5-20分钟` / `20分钟以上`)
        if duration_label != "不限" or find(lines, r"^(?:5分钟以下|5-20分钟|20分钟以上)", lambda l: 240 <= l["box"][1] <= 325):
            current_dur_chip = find(
                lines,
                r"^(?:时长|5分钟以下|5-20分钟|20分钟以上)",
                lambda l: 310 <= l["box"][0] <= 510 and 240 <= l["box"][1] <= 325,
            )
            if current_dur_chip and not current_dur_chip["text"].strip().startswith(duration_label):
                check_stopped(ui)
                ui.click(current_dur_chip)
                time.sleep(0.8)
                sheet_lines = ui.snapshot("search-duration-sheet")
                opt = find(
                    sheet_lines,
                    rf"^{re.escape(duration_label)}$",
                    lambda l: l["box"][1] > 1150,
                )
                if not opt:
                    raise ValueError(f"Duration option {duration_label!r} not found in bottom sheet")
                check_stopped(ui)
                ui.click(opt)
                time.sleep(1.4)
                lines = ui.snapshot(f"search-after-duration-{duration_label}")

        # 3. Scope dropdown (`范围` / `已关注` / `最近看过`)
        if scope_label != "不限" or find(lines, r"^(?:已关注|最近看过)", lambda l: 240 <= l["box"][1] <= 325):
            current_scope_chip = find(
                lines,
                r"^(?:范围|已关注|最近看过)",
                lambda l: l["box"][0] >= 460 and 240 <= l["box"][1] <= 325,
            )
            if current_scope_chip and not current_scope_chip["text"].strip().startswith(scope_label):
                check_stopped(ui)
                ui.click(current_scope_chip)
                time.sleep(0.8)
                sheet_lines = ui.snapshot("search-scope-sheet")
                opt = find(
                    sheet_lines,
                    rf"^{re.escape(scope_label)}$",
                    lambda l: l["box"][1] > 1150,
                )
                if not opt:
                    raise ValueError(f"Scope option {scope_label!r} not found in bottom sheet")
                check_stopped(ui)
                ui.click(opt)
                time.sleep(1.4)
                lines = ui.snapshot(f"search-after-scope-{scope_label}")

        cards = parse_search_video_cards(lines, ui.height)
        return {
            "status": "search_filters_applied",
            "mode": "inline_video_bar",
            "sortBy": sort_label,
            "duration": duration_label,
            "scope": scope_label,
            "sortDarkRatio": dark_ratio,
            "verified": True,
            "visibleCardCount": len(cards),
            "lines": lines,
        }

    # Mode B: `筛选` dropdown panel on `全部` tab
    if not find(lines, r"^排序$", lambda l: l["box"][1] < 360):
        filter_btn = find(lines, r"筛选", lambda l: l["box"][0] > 540 and 145 <= l["box"][1] <= 240)
        if not filter_btn:
            raise ValueError("Search '筛选' trigger is not visible")
        check_stopped(ui)
        ui.click(filter_btn)
        time.sleep(0.8)
        lines = ui.snapshot("search-filter-panel")

    for sec_title, opt_label in (("排序", sort_label), ("范围", scope_label), ("时长", duration_label)):
        if opt_label == "不限":
            continue
        if not find(lines, r"^排序$", lambda l: l["box"][1] < 360):
            filter_btn = find(lines, r"筛选", lambda l: l["box"][0] > 540 and 145 <= l["box"][1] <= 240)
            if filter_btn:
                ui.click(filter_btn)
                time.sleep(0.8)
                lines = ui.snapshot("search-filter-panel-reopen")
        target_opt = find_search_filter_option(lines, sec_title, opt_label)
        check_stopped(ui)
        ui.click(target_opt)
        time.sleep(1.2)
        lines = ui.snapshot(f"search-filter-after-{sec_title}")

    collapse_btn = find(lines, r"^收起$", lambda l: l["box"][1] < 720)
    if collapse_btn:
        ui.click(collapse_btn)
        time.sleep(0.8)
        lines = ui.snapshot("search-filter-collapsed")

    cards = parse_search_video_cards(lines, ui.height)
    return {
        "status": "search_filters_applied",
        "mode": "filter_panel",
        "sortBy": sort_label,
        "duration": duration_label,
        "scope": scope_label,
        "verified": True,
        "visibleCardCount": len(cards),
        "lines": lines,
    }


def parse_search_video_cards(lines, height=1605) -> list[dict]:
    """Parse 2-column video cards on the WeChat Channels Search Results page."""
    if not lines:
        return []

    # Exclude non-video section rows (`脱口秀 - 视频号`, `更多 >`, `看过的视频`)
    non_video_ys = [
        l["box"][1]
        for l in lines
        if re.search(r"[-－—]\s*视频号|看过的视频|更多\s*[>＞]|^\d+\s*条视频", l.get("text", ""))
    ]

    cards = []
    for col_index, (col_min_x, col_max_x, date_min_x) in enumerate(((0, 360, 175), (360, 720, 510))):
        col_items = [
            l
            for l in lines
            if col_min_x <= l["box"][0] < col_max_x and 320 <= l["box"][1] < height - 25
        ]
        date_items = sorted(
            (
                l
                for l in col_items
                if l["box"][0] >= date_min_x
                and _CARD_DATE_RE.fullmatch(compact(l.get("text", "")))
            ),
            key=lambda l: l["box"][1],
        )

        prev_footer_bottom = 310
        for date_item in date_items:
            dy = date_item["box"][1]
            if any(ny - 15 <= dy <= ny + 180 for ny in non_video_ys):
                continue

            author_candidates = [
                l
                for l in col_items
                if l is not date_item
                and l["box"][0] < date_item["box"][0] - 10
                and abs(l["box"][1] - dy) <= 22
                and not _CARD_DATE_RE.fullmatch(compact(l.get("text", "")))
            ]
            if not author_candidates:
                continue

            author_item = min(author_candidates, key=lambda l: l["box"][0])
            author = re.sub(r"[.…·•]+$", "", author_item["text"].strip()).strip()
            if not author or re.fullmatch(r"已关注|关注|广告|推广|赞助", author):
                continue

            # Title lines sit above the author/date row and below the cover overlay row
            title_band_top = max(prev_footer_bottom + 80, dy - 115)
            title_items = sorted(
                (
                    l
                    for l in col_items
                    if title_band_top <= l["box"][1] < dy - 12
                    and not re.fullmatch(
                        r"\d{1,2}:\d{2}.*|[凸👍♡♥]?\s*\d+(?:\.\d+)?[万wW]?\+?",
                        compact(l.get("text", "")),
                    )
                ),
                key=lambda l: (l["box"][1], l["box"][0]),
            )
            title = "".join(l["text"].strip() for l in title_items).strip()
            if not title:
                continue
            if _AD_CARD_RE.search(title):
                prev_footer_bottom = dy + date_item["box"][3]
                continue

            # Extract optional cover duration and like count
            cover_band_top = max(prev_footer_bottom, dy - 220)
            cover_band_bottom = title_items[0]["box"][1] - 4
            duration_item = next(
                (
                    l
                    for l in col_items
                    if cover_band_top <= l["box"][1] <= cover_band_bottom
                    and re.match(r"^\d{1,2}:\d{2}", compact(l.get("text", "")))
                ),
                None,
            )
            like_item = next(
                (
                    l
                    for l in col_items
                    if cover_band_top <= l["box"][1] <= cover_band_bottom
                    and l["box"][0] >= date_min_x - 30
                    and re.fullmatch(r"[凸👍♡♥]?\s*\d+(?:\.\d+)?[万wW]?\+?", compact(l.get("text", "")))
                ),
                None,
            )

            title_top = title_items[0]["box"][1]
            click_y = max(350, min(title_top - 140, height - 220))
            click_x = col_min_x + 95
            click_box = [click_x, click_y, 150, 120]

            clean_title_prefix = re.sub(r"[.…·•]+$", "", title).strip()
            card_key = "wxch-card:" + identity(author, clean_title_prefix)
            cards.append(
                {
                    "cardKey": card_key,
                    "column": col_index,
                    "author": author,
                    "rawAuthor": author_item["text"].strip(),
                    "title": title,
                    "publishTime": compact(date_item["text"]),
                    "duration": compact(duration_item["text"])[:5] if duration_item else None,
                    "likeCountText": re.sub(r"^[凸👍♡♥\s]+", "", like_item["text"].strip()) if like_item else None,
                    "clickBox": click_box,
                    "footerY": dy,
                }
            )
            prev_footer_bottom = dy + date_item["box"][3]

    cards.sort(key=lambda c: (c["footerY"] // 60, c["column"]))
    return cards


def scroll_search_results(ui):
    """Scroll down the 2-column search results list."""
    check_stopped(ui)
    if hasattr(ui, "scroll_feed_next"):
        ui.scroll_feed_next()
        return
    if hasattr(ui, "controller"):
        start = int(ui.height * 0.80)
        end = int(ui.height * 0.30)
        ui.controller.post_swipe(360, start, 360, end, 340).wait()
        time.sleep(0.9)


def select_search_card(
    ui,
    seen: set[str],
    *,
    max_screens: int = 5,
    lines=None,
) -> tuple[dict, list[dict], list[dict]]:
    """Find the first unseen video card in Search Results across up to `max_screens` scrolls."""
    if lines is None:
        lines = ui.snapshot("search-cards")

    for screen_idx in range(max(1, max_screens)):
        check_stopped(ui)
        cards = parse_search_video_cards(lines, ui.height)
        for card in cards:
            author_key = "wxch-author:" + identity(card["author"])
            if card["cardKey"] not in seen and author_key not in seen:
                return card, cards, lines
        if screen_idx + 1 >= max_screens:
            break
        scroll_search_results(ui)
        lines = ui.snapshot(f"search-cards-scroll-{screen_idx + 2}")

    raise RuntimeError("No unseen search video cards found within max_screens")


# ---------------------------------------------------------------------------
# 6. Continuous Feed Video Processing & Mode Runners
# ---------------------------------------------------------------------------


def collect_video_in_mode(
    ui,
    store: LocalStore,
    timings: Timings | None,
    lines,
    *,
    source_mode: str = "RECOMMEND",
    allow_fallback_identity: bool = True,
) -> dict:
    """Collect the currently open video into `LocalStore` (`videos.sqlite3`) with `source_mode`.
    If a personal account in Search/Friends does not expose a public `视频号 ID` in `基础信息`,
    gracefully falls back to `wechat-author-fallback` when `allow_fallback_identity=True`."""
    phase = timings.phase if timings is not None else (lambda _name: nullcontext())
    emit = timings.emit if timings is not None else (lambda _row: None)

    with phase("collect_video_total"):
        with phase("collect_author_nickname"):
            author = author_in_feed(lines, ui.height)["text"].strip()
        with phase("collect_description_time_location"):
            try:
                video = video_details(ui, lines, author)
                ui.back()
            except Exception:
                # Fallback if work header in drawer is non-standard on a newly posted clip
                if classify_page_state(ui.snapshot("drawer-recover"), ui.height) == "comment_drawer":
                    ui.back()
                preview = feed_video_preview(lines, ui.height)
                video = {
                    "authorNickname": author,
                    "description": preview["captionPreview"] or author,
                    "publishTimeText": None,
                    "publishLocation": None,
                }
        with phase("collect_author_profile"):
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
            try:
                profile.update(open_basic_info(ui, more))
                ui.back()
                ui.back()
            except ValueError:
                try:
                    profile.update(parse_basic_info(ui.snapshot("author-basic-info-retry")))
                    ui.back()
                    ui.back()
                except ValueError:
                    if not allow_fallback_identity:
                        raise
                    # Recover from basic info + author profile back to the video page
                    ui.back()
                    ui.back()
                    profile.update(
                        {
                            "authorDouyinId": "wxch_user_" + identity(author)[:16],
                            "authorIdentityMethod": "wechat-author-fallback",
                        }
                    )
            profile["missingFields"] = [
                key
                for key in ("authorCertification", "authorIpLocation", "authorBio")
                if profile.get(key) is None
            ]
        with phase("persist_video_draft"):
            payload = {**profile, **video, "returnVerified": True}
            payload["missingFields"] += [
                key for key in ("publishTimeText", "publishLocation") if not payload.get(key)
            ]
            event, duplicate = store.save(payload, source_mode=source_mode)
            emit(
                {
                    "kind": "videoCollected",
                    "eventId": event["event_id"],
                    "dedupeKey": event["dedupe_key"],
                    "sourceMode": source_mode,
                    "duplicate": duplicate,
                    "missingFields": payload["missingFields"],
                    "uploadStatus": "local_only",
                }
            )
    return event


def _process_open_video_item(
    ui,
    root_dir: Path,
    *,
    lines,
    source_mode: str,
    store: LocalStore,
    timings: Timings | None,
    quota_store: DailyQuotaStore,
    collect_video: bool = True,
    inspect_comments: bool = False,
    comment_options: dict | None = None,
    interaction_options: dict | None = None,
) -> dict:
    check_stopped(ui)
    preview = feed_video_preview(lines, ui.height)
    video_event = None
    if collect_video:
        video_event = call_with_captcha_pause(
            ui,
            collect_video_in_mode,
            ui,
            store,
            timings,
            lines,
            source_mode=source_mode,
        )
    else:
        work_key = compute_work_key(author=preview["author"], title=preview["captionPreview"] or preview["author"])
        video_event = {
            "event_id": str(uuid.uuid4()),
            "event_type": "VIDEO",
            "dedupe_key": work_key,
            "source_mode": source_mode,
            "payload": {
                "authorNickname": preview["author"],
                "description": preview["captionPreview"] or preview["author"],
            },
        }

    comments_report = None
    if inspect_comments:
        copts = dict(comment_options or {})
        comments_report = call_with_captcha_pause(
            ui,
            collect_visible_comments,
            ui,
            store.path,
            video_event,
            timings,
            max_pages=int(copts.get("maxPages", 2)),
            max_replies_Wait=int(copts.get("maxReplyExpansions", 1)),
            max_profiles=int(copts.get("maxProfiles", 1)),
        ) if "max_replies_Wait" in collect_visible_comments.__code__.co_varnames else call_with_captcha_pause(
            ui,
            collect_visible_comments,
            ui,
            store.path,
            video_event,
            timings,
            max_pages=int(copts.get("maxPages", 2)),
            max_profiles=int(copts.get("maxProfiles", 1)),
        )

    interaction_report = None
    if interaction_options:
        iopts = dict(interaction_options)
        interaction_report = call_with_captcha_pause(
            ui,
            interact_work_and_comments,
            ui,
            quota_store,
            iopts,
            video_event=video_event,
        )

    return {
        "eventId": video_event["event_id"],
        "dedupeKey": video_event["dedupe_key"],
        "sourceMode": source_mode,
        "author": video_event["payload"].get("authorNickname") or preview["author"],
        "description": video_event["payload"].get("description"),
        "authorDouyinId": video_event["payload"].get("authorDouyinId"),
        "previewKey": preview["previewKey"],
        "authorKey": preview["authorKey"],
        "videoEvent": video_event,
        "commentsReport": comments_report,
        "interactionReport": interaction_report,
    }


def run_tab_feed_mode(
    ui,
    root_dir: Path,
    *,
    tab: str = "recommend",
    count: int = 2,
    max_swipes: int = 8,
    source_mode: str | None = None,
    collect_video: bool = True,
    inspect_comments: bool = False,
    comment_options: dict | None = None,
    interaction_options: dict | None = None,
    require_location_permission: bool = False,
    quota_store: DailyQuotaStore | None = None,
    timings: Timings | None = None,
) -> dict:
    """Continuously process `count` videos on a Channels top-tab feed (`recommend`,
    `friends`, `follow`, `city`), skipping seen videos, live streams, and ads."""
    norm_tab = TAB_ALIASES.get(str(tab).strip().lower(), str(tab).strip().lower())
    if norm_tab not in ("recommend", "friends", "follow", "city"):
        raise ValueError(f"Unsupported feed tab: {tab}")
    if type(count) is not int or not 0 <= count <= 50:
        raise ValueError("count must be between 0 and 50")

    started = time.monotonic()
    root_dir = Path(root_dir)
    data_dir = root_dir / "data" if (root_dir / "data").is_dir() else root_dir
    sqlite_path = data_dir / "videos.sqlite3"
    store = LocalStore(sqlite_path)
    if quota_store is None:
        quota_store = DailyQuotaStore(data_dir / "daily_quotas.json")
    seen_file, seen = load_seen_keys(data_dir)
    initial_seen_count = len(seen)
    effective_source_mode = source_mode or STAGE_SOURCE_MODES[norm_tab]

    tab_state = call_with_captcha_pause(
        ui,
        switch_channels_top_tab,
        ui,
        norm_tab,
        require_location_permission=require_location_permission,
    )
    if not tab_state.get("available"):
        return {
            "status": tab_state["status"],
            "mode": norm_tab,
            "sourceMode": effective_source_mode,
            "available": False,
            "stageVerified": False,
            "reason": tab_state.get("reason"),
            "tabState": {k: v for k, v in tab_state.items() if k != "lines"},
            "processedCount": 0,
            "videos": [],
            "skippedItems": [],
            "recoveryEvents": [],
            "elapsedMs": round((time.monotonic() - started) * 1000),
        }

    lines = tab_state["lines"]
    stage_check = verify_stage_page(ui, norm_tab, lines=lines)

    videos = []
    skipped_items = []
    recovery_events = []
    swipes = 0

    while len(videos) < count and swipes <= max_swipes:
        check_stopped(ui)
        if swipes > 0:
            lines = call_with_captcha_pause(ui, ui.snapshot, f"{norm_tab}-feed-{swipes}")

        if classify_page_state(lines, ui.height) != "channels_feed":
            rec = bounded_recover_to_target(ui, target=norm_tab, max_steps=4)
            recovery_events.append(rec)
            lines = ui.snapshot(f"{norm_tab}-recovered-{swipes}")

        skip_info = detect_feed_skip_item(lines, ui.height)
        if skip_info["skip"]:
            skipped_items.append(
                {
                    "swipeIndex": swipes,
                    "kind": skip_info["kind"],
                    "reason": skip_info["reason"],
                }
            )
            if swipes >= max_swipes:
                break
            ui.scroll_feed_next()
            swipes += 1
            continue

        try:
            preview = feed_video_preview(lines, ui.height)
        except ValueError as err:
            skipped_items.append(
                {
                    "swipeIndex": swipes,
                    "kind": "unreadable_author",
                    "reason": str(err),
                }
            )
            if swipes >= max_swipes:
                break
            ui.scroll_feed_next()
            swipes += 1
            continue

        if (
            preview["previewKey"] in seen
            or preview["authorKey"] in seen
            or compute_work_key(author=preview["author"], title=preview["captionPreview"]) in seen
        ):
            skipped_items.append(
                {
                    "swipeIndex": swipes,
                    "kind": "already_seen",
                    "author": preview["author"],
                    "previewKey": preview["previewKey"],
                }
            )
            if swipes >= max_swipes:
                break
            ui.scroll_feed_next()
            swipes += 1
            continue

        item_report = _process_open_video_item(
            ui,
            data_dir,
            lines=lines,
            source_mode=effective_source_mode,
            store=store,
            timings=timings,
            quota_store=quota_store,
            collect_video=collect_video,
            inspect_comments=inspect_comments,
            comment_options=comment_options,
            interaction_options=interaction_options,
        )
        videos.append(item_report)
        seen.add(item_report["dedupeKey"])
        seen.add(item_report["previewKey"])
        seen.add(item_report["authorKey"])
        save_seen_keys(seen_file, seen)

        if len(videos) < count:
            if swipes >= max_swipes:
                break
            ui.scroll_feed_next()
            swipes += 1

    return {
        "status": f"{norm_tab}_mode_completed",
        "mode": norm_tab,
        "sourceMode": effective_source_mode,
        "available": True,
        "stageVerified": stage_check["verified"],
        "pageType": f"{norm_tab}_feed",
        "tabState": {k: v for k, v in tab_state.items() if k != "lines"},
        "processedCount": len(videos),
        "swipesUsed": swipes,
        "initialSeenCount": initial_seen_count,
        "finalSeenCount": len(seen),
        "videos": videos,
        "skippedItems": skipped_items,
        "recoveryEvents": recovery_events,
        "elapsedMs": round((time.monotonic() - started) * 1000),
    }


def run_recommend_mode(ui, root_dir: Path, **kwargs) -> dict:
    """Run continuous collection & interaction in WeChat Channels `推荐` (`RECOMMEND`) mode."""
    kwargs.pop("tab", None)
    return run_tab_feed_mode(ui, root_dir, tab="recommend", **kwargs)


def run_city_mode(ui, root_dir: Path, **kwargs) -> dict:
    """Run continuous collection & interaction in WeChat Channels `同城` (`CITY`) mode."""
    kwargs.pop("tab", None)
    return run_tab_feed_mode(ui, root_dir, tab="city", **kwargs)


def run_search_mode(
    ui,
    root_dir: Path,
    keyword: str,
    *,
    sort_by: str | None = "不限",
    duration: str | None = "不限",
    scope: str | None = "不限",
    count: int = 1,
    max_screens: int = 5,
    source_mode: str = "SEARCH",
    collect_video: bool = True,
    inspect_comments: bool = False,
    comment_options: dict | None = None,
    interaction_options: dict | None = None,
    quota_store: DailyQuotaStore | None = None,
    timings: Timings | None = None,
) -> dict:
    """Search `keyword`, apply sort/duration/scope filters, open video cards from the
    2-column search results grid, process them, and verify return to the results list."""
    if type(count) is not int or not 0 <= count <= 50:
        raise ValueError("count must be between 0 and 50")

    started = time.monotonic()
    root_dir = Path(root_dir)
    data_dir = root_dir / "data" if (root_dir / "data").is_dir() else root_dir
    sqlite_path = data_dir / "videos.sqlite3"
    store = LocalStore(sqlite_path)
    if quota_store is None:
        quota_store = DailyQuotaStore(data_dir / "daily_quotas.json")
    seen_file, seen = load_seen_keys(data_dir)
    initial_seen_count = len(seen)

    search_state = call_with_captcha_pause(
        ui,
        search_keyword,
        ui,
        keyword,
        switch_to_video_tab=True,
    )
    target_kw = search_state["keyword"]
    filter_state = call_with_captcha_pause(
        ui,
        apply_search_filters,
        ui,
        sort_by=sort_by,
        duration=duration,
        scope=scope,
        lines=search_state["lines"],
    )
    current_lines = filter_state["lines"]
    stage_check = verify_stage_page(ui, "search", expected_keyword=target_kw, lines=current_lines)

    videos = []
    recovery_events = []

    for idx in range(count):
        check_stopped(ui)
        if idx > 0:
            current_lines = call_with_captcha_pause(ui, ui.snapshot, f"search-grid-{idx + 1}")
            if not is_search_results_page(current_lines, expected_keyword=target_kw):
                rec = bounded_recover_to_target(
                    ui,
                    target="search",
                    expected_keyword=target_kw,
                    max_steps=4,
                )
                recovery_events.append(rec)
                current_lines = ui.snapshot(f"search-grid-recovered-{idx + 1}")

        card, visible_cards, current_lines = select_search_card(
            ui,
            seen,
            max_screens=max_screens,
            lines=current_lines,
        )
        check_stopped(ui)
        ui.click({"text": card["title"], "box": card["clickBox"]})
        time.sleep(1.2)
        video_lines = ui.snapshot(f"search-opened-video-{idx + 1}")

        if classify_page_state(video_lines, ui.height) != "channels_feed":
            time.sleep(0.8)
            video_lines = ui.snapshot(f"search-opened-video-retry-{idx + 1}")

        item_report = _process_open_video_item(
            ui,
            data_dir,
            lines=video_lines,
            source_mode=source_mode,
            store=store,
            timings=timings,
            quota_store=quota_store,
            collect_video=collect_video,
            inspect_comments=inspect_comments,
            comment_options=comment_options,
            interaction_options=interaction_options,
        )

        # Return from opened video back to 2-column Search Results list
        check_stopped(ui)
        ui.back()
        after_back_lines = ui.snapshot(f"search-return-{idx + 1}")
        if not is_search_results_page(after_back_lines):
            rec = bounded_recover_to_target(
                ui,
                target="search",
                expected_keyword=target_kw,
                max_steps=3,
            )
            recovery_events.append(rec)
            after_back_lines = ui.snapshot(f"search-return-recovered-{idx + 1}")

        returned_ok = is_search_results_page(after_back_lines)
        item_report["card"] = card
        item_report["returnedToSearchResults"] = returned_ok
        videos.append(item_report)

        seen.add(card["cardKey"])
        seen.add(item_report["dedupeKey"])
        seen.add(item_report["authorKey"])
        save_seen_keys(seen_file, seen)
        current_lines = after_back_lines

    return {
        "status": "search_mode_completed",
        "mode": "search",
        "sourceMode": source_mode,
        "stageVerified": stage_check["verified"],
        "pageType": "search_results",
        "keyword": target_kw,
        "searchState": {k: v for k, v in search_state.items() if k != "lines"},
        "filterState": {k: v for k, v in filter_state.items() if k != "lines"},
        "processedCount": len(videos),
        "initialSeenCount": initial_seen_count,
        "finalSeenCount": len(seen),
        "videos": videos,
        "recoveryEvents": recovery_events,
        "elapsedMs": round((time.monotonic() - started) * 1000),
    }


def run_auto_mode(
    ui,
    root_dir: Path,
    *,
    stages: list[dict] | None = None,
    quota_store: DailyQuotaStore | None = None,
    timings: Timings | None = None,
) -> dict:
    """Switch across configured stages (`recommend`, `friends`, `follow`, `city`,
    `search`, `messages`), verifying page-to-stage correspondence on every transition."""
    started = time.monotonic()
    root_dir = Path(root_dir)
    data_dir = root_dir / "data" if (root_dir / "data").is_dir() else root_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    if quota_store is None:
        quota_store = DailyQuotaStore(data_dir / "daily_quotas.json")

    if not stages:
        stages = [
            {"stage": "recommend", "count": 1},
            {"stage": "friends", "count": 1},
            {
                "stage": "search",
                "keyword": "脱口秀",
                "sortBy": "最新",
                "duration": "5分钟以下",
                "count": 1,
            },
        ]

    stage_reports = []
    for stage_idx, spec in enumerate(stages):
        check_stopped(ui)
        raw_stage = (spec.get("stage") or spec.get("mode") or "").strip().lower()
        norm_stage = TAB_ALIASES.get(raw_stage, raw_stage)

        if norm_stage in ("recommend", "friends", "follow", "city"):
            auto_source_mode = f"AUTO_{STAGE_SOURCE_MODES[norm_stage]}"
            report = run_tab_feed_mode(
                ui,
                root_dir,
                tab=norm_stage,
                count=int(spec.get("count", 1)),
                max_swipes=int(spec.get("maxSwipes", 6)),
                source_mode=auto_source_mode,
                collect_video=bool(spec.get("collectVideo", True)),
                inspect_comments=bool(spec.get("inspectComments", False)),
                comment_options=spec.get("commentOptions"),
                interaction_options=spec.get("interactionOptions"),
                require_location_permission=bool(spec.get("requireLocationPermission", False)),
                quota_store=quota_store,
                timings=timings,
            )
            report["stageSwitchVerified"] = bool(report.get("stageVerified")) if report.get("available") else False
            stage_reports.append(report)

        elif norm_stage == "search":
            keyword = spec.get("keyword") or "脱口秀"
            report = run_search_mode(
                ui,
                root_dir,
                keyword=keyword,
                sort_by=spec.get("sortBy", "不限"),
                duration=spec.get("duration", "不限"),
                scope=spec.get("scope", "不限"),
                count=int(spec.get("count", 1)),
                max_screens=int(spec.get("maxScreens", 4)),
                source_mode="AUTO_SEARCH",
                collect_video=bool(spec.get("collectVideo", True)),
                inspect_comments=bool(spec.get("inspectComments", False)),
                comment_options=spec.get("commentOptions"),
                interaction_options=spec.get("interactionOptions"),
                quota_store=quota_store,
                timings=timings,
            )
            report["stageSwitchVerified"] = bool(report.get("stageVerified"))
            stage_reports.append(report)
            if stage_idx + 1 < len(stages):
                bounded_recover_to_target(ui, target="recommend", max_steps=4)

        elif norm_stage == "messages":
            msg_report = inspect_and_handle_messages(
                ui,
                quota_store=quota_store,
                max_messages=int(spec.get("maxMessages", 1)),
                do_like=bool(spec.get("like", True)),
                reply_text=spec.get("replyText"),
                target_nickname=spec.get("targetNickname"),
            )
            stage_reports.append({"mode": "messages", "stageSwitchVerified": msg_report["returnedToTask"], **msg_report})
        else:
            raise ValueError(f"Unsupported stage in auto mode: {raw_stage!r}")

    out = {
        "status": "auto_mode_completed",
        "mode": "auto",
        "stagesCompleted": len(stage_reports),
        "stages": stage_reports,
        "quotas": quota_store.summary(),
        "elapsedMs": round((time.monotonic() - started) * 1000),
    }
    _atomic_write_json(data_dir / f"auto-mode-{uuid.uuid4().hex[:12]}.json", out)
    return out


def run_mode(ui, root_dir: Path, config: dict, timings: Timings | None = None) -> dict:
    """Unified dispatcher for `--mode` CLI and `RunWechatMode` CustomAction."""
    root_dir = Path(root_dir)
    data_dir = root_dir / "data" if (root_dir / "data").is_dir() else root_dir
    quota_path = Path(config.get("quotaPath") or (data_dir / "daily_quotas.json"))
    quota_store = DailyQuotaStore(quota_path, limits=config.get("limits"))

    raw_mode = (config.get("mode") or "recommend").strip().lower()
    norm_mode = TAB_ALIASES.get(raw_mode, raw_mode)

    if norm_mode == "auto":
        return run_auto_mode(
            ui,
            root_dir,
            stages=config.get("stages"),
            quota_store=quota_store,
            timings=timings,
        )

    if norm_mode == "search":
        return run_search_mode(
            ui,
            root_dir,
            keyword=config.get("keyword") or "脱口秀",
            sort_by=config.get("sortBy", "不限"),
            duration=config.get("duration", "不限"),
            scope=config.get("scope", "不限"),
            count=int(config.get("count", 1)),
            max_screens=int(config.get("maxScreens", 5)),
            source_mode=config.get("sourceMode") or "SEARCH",
            collect_video=bool(config.get("collectVideo", True)),
            inspect_comments=bool(config.get("inspectComments", False)),
            comment_options=config.get("commentOptions"),
            interaction_options=config.get("interactionOptions"),
            quota_store=quota_store,
            timings=timings,
        )

    if norm_mode in ("recommend", "friends", "follow", "city"):
        return run_tab_feed_mode(
            ui,
            root_dir,
            tab=norm_mode,
            count=int(config.get("count", 2)),
            max_swipes=int(config.get("maxSwipes", 8)),
            source_mode=config.get("sourceMode") or STAGE_SOURCE_MODES[norm_mode],
            collect_video=bool(config.get("collectVideo", True)),
            inspect_comments=bool(config.get("inspectComments", False)),
            comment_options=config.get("commentOptions"),
            interaction_options=config.get("interactionOptions"),
            require_location_permission=bool(config.get("requireLocationPermission", False)),
            quota_store=quota_store,
            timings=timings,
        )

    raise ValueError(f"Unsupported mode: {raw_mode!r}")
