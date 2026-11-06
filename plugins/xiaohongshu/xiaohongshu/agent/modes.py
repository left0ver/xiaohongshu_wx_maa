"""Batch 4 modes: Recommend, Search (sort & filter), City/Nearby, Auto stage switching,
captcha pause/resume, and bounded abnormal-page recovery."""

import json
from pathlib import Path
import re
import time
import uuid

from collector import (
    collect_open_note,
    open_discover,
    read_note_detail,
    restore_feed,
    scroll_feed,
    select_feed_card,
)
from comments import inspect_comments
from interactions import (
    DailyQuotaStore,
    check_stopped as check_stop,
    classify_page_state,
    ensure_clean_page_on_resume,
    inspect_and_handle_messages,
    interact_work_and_comments,
)
from ui import PageMismatch, bounds

SORT_ALIASES = {
    None: "综合",
    "": "综合",
    "综合": "综合",
    "general": "综合",
    "最新": "最新",
    "latest": "最新",
    "最多评论": "最多评论",
    "most_comments": "最多评论",
    "最多点赞": "最多点赞",
    "most_likes": "最多点赞",
    "最多收藏": "最多收藏",
    "most_collects": "最多收藏",
}

NOTE_TYPE_FILTER_ALIASES = {
    None: "不限",
    "": "不限",
    "不限": "不限",
    "ALL": "不限",
    "all": "不限",
    "视频": "视频",
    "VIDEO": "视频",
    "video": "视频",
    "图文": "图文",
    "IMAGE_TEXT": "图文",
    "image_text": "图文",
}

PUBLISH_TIME_ALIASES = {
    None: "不限",
    "": "不限",
    "不限": "不限",
    "all": "不限",
    "一天内": "一天内",
    "day": "一天内",
    "1d": "一天内",
    "一周内": "一周内",
    "week": "一周内",
    "7d": "一周内",
    "半年内": "半年内",
    "half_year": "半年内",
    "180d": "半年内",
}

FILTER_SECTION_ORDER = (
    "排序依据",
    "笔记类型",
    "发布时间",
    "搜索范围",
    "位置距离",
    "收起",
)

_LOADING_FAILURE_RE = re.compile(
    r"网络好像断开了|加载失败|网络不给力|点击重试|刷新重试|笔记不存在|内容不见了|页面不见了|暂时无法查看"
)


def _atomic_write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    tmp.replace(path)


def _load_seen(data_dir: Path) -> tuple[Path, set[str]]:
    seen_file = Path(data_dir) / "seen.json"
    if not seen_file.exists():
        return seen_file, set()
    try:
        return seen_file, set(json.loads(seen_file.read_text()))
    except (OSError, ValueError, TypeError):
        return seen_file, set()


def _save_seen(seen_file: Path, seen: set[str]):
    _atomic_write_json(seen_file, sorted(seen))


# ---------------------------------------------------------------------------
# 1. Captcha / Slider Verification Pause & Manual Recovery
# ---------------------------------------------------------------------------


def is_captcha_error(error: Exception) -> bool:
    return isinstance(error, PageMismatch) and "Verification detected" in str(error)


def wait_for_captcha_resolution(
    device,
    *,
    poll_interval: float = 1.0,
    max_wait_seconds: float = 120.0,
    on_pause=None,
) -> dict:
    """Pause automated actions when a slider/captcha is detected, wait for manual
    completion, and re-identify the current page before continuing."""
    check_stop(device)
    print(
        json.dumps(
            {
                "status": "paused_for_verification",
                "reason": "captcha_or_slider_detected",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if callable(on_pause):
        on_pause()
    deadline = time.monotonic() + max(float(max_wait_seconds), 0.1)
    polls = 0
    while time.monotonic() < deadline:
        check_stop(device)
        polls += 1
        try:
            device.page()
            if getattr(device, "has_vision", False) is True:
                device.wait_visual_settle(timeout=0.8)
            raw_state = ensure_clean_page_on_resume(device)
            state = {
                k: v
                for k, v in raw_state.items()
                if k in ("pageType", "workAuthor", "workTitle")
            }
            result = dict(
                status="resumed_after_verification",
                waitedPolls=polls,
                pageState=state,
            )
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return result
        except PageMismatch as error:
            if not is_captcha_error(error):
                raw_state = ensure_clean_page_on_resume(device)
                state = {
                    k: v
                    for k, v in raw_state.items()
                    if k in ("pageType", "workAuthor", "workTitle")
                }
                return dict(
                    status="resumed_after_verification",
                    waitedPolls=polls,
                    pageState=state,
                )
        time.sleep(max(float(poll_interval), 0.05))
    raise PageMismatch(
        "Verification timeout; manual intervention did not complete within limit"
    )


def call_with_captcha_pause(
    device,
    fn,
    *args,
    max_wait_seconds: float = 120.0,
    on_pause=None,
    **kwargs,
):
    try:
        return fn(*args, **kwargs)
    except PageMismatch as error:
        if not is_captcha_error(error):
            raise
        wait_for_captcha_resolution(
            device,
            max_wait_seconds=max_wait_seconds,
            on_pause=on_pause,
        )
        return fn(*args, **kwargs)


# ---------------------------------------------------------------------------
# 2. Bounded Recovery for Loading Failures, Popups & Unexpected Pages
# ---------------------------------------------------------------------------


def detect_abnormal_state(page) -> dict:
    texts = [n.get("text", "").strip() for n in page.nodes if n.get("text", "").strip()]
    descs = [
        n.get("content-desc", "").strip()
        for n in page.nodes
        if n.get("content-desc", "").strip()
    ]
    if any(t == "定位权限未开启" for t in texts) and any(
        t in ("暂时不了", "一键开启") for t in texts
    ):
        return dict(kind="popup", reason="location_permission_modal")
    if any(t == "开启通知" for t in texts) and any(t == "不再提醒" for t in texts):
        return dict(kind="popup", reason="notification_permission_modal")
    if any("青少年模式" in t for t in texts) and any(
        t in ("我知道了", "知道了") for t in texts
    ):
        return dict(kind="popup", reason="teenager_mode_modal")
    if any(t in ("发现新版本", "立即更新", "版本更新") for t in texts) and any(
        t in ("以后再说", "暂不更新", "取消") for t in texts
    ):
        return dict(kind="popup", reason="app_update_modal")
    if any(t == "排序依据" for t in texts) and any(t == "收起" for t in texts):
        return dict(kind="overlay", reason="search_filter_panel_open")
    for text in texts:
        if _LOADING_FAILURE_RE.search(text):
            retryable = any(t in ("点击重试", "刷新重试", "重试", "刷新") for t in texts)
            deleted = any(
                k in text for k in ("笔记不存在", "内容不见了", "页面不见了", "暂时无法查看")
            )
            return dict(
                kind="loading_failure",
                reason="content_unavailable" if deleted else "network_or_load_error",
                detail=text,
                retryable=retryable and not deleted,
            )
    if any(t == "历史记录" or t == "猜你想搜" for t in texts) and any(
        t == "搜索" for t in texts
    ):
        return dict(kind="unexpected_page", reason="search_input_page")
    if any(d == "市集" and n.get("selected") == "true" for n, d in ((n, n.get("content-desc", "")) for n in page.nodes)):
        return dict(kind="unexpected_page", reason="store_tab")
    if any(t in ("立即购买", "加入购物车") for t in texts):
        return dict(kind="unexpected_page", reason="product_detail_page")
    if any("直播间" in t or "直播中" in t for t in texts + descs):
        return dict(kind="unexpected_page", reason="live_room")
    state = classify_page_state(page)
    return dict(kind="page", reason=state.get("pageType", "unknown"), state=state)


def classify_mode_page(page, expected_keyword: str | None = None) -> str:
    try:
        page.cards()
        return "recommend_feed"
    except PageMismatch:
        pass
    try:
        page.city_cards()
        return "city_feed"
    except PageMismatch:
        pass
    try:
        page.search_cards(expected_keyword=expected_keyword)
        return "search_feed"
    except PageMismatch:
        pass
    abnormal = detect_abnormal_state(page)
    if abnormal["kind"] in ("popup", "overlay", "loading_failure", "unexpected_page"):
        return abnormal["reason"]
    return abnormal.get("reason", "unknown")


def dismiss_location_permission_modal(device, page=None) -> bool:
    current = page if page is not None else device.page()
    texts = [n.get("text", "").strip() for n in current.nodes if n.get("text", "").strip()]
    if not (
        any("定位权限未开启" in t for t in texts)
        or (any(t == "暂时不了" for t in texts) and any(t == "一键开启" or "定位权限" in t for t in texts))
    ):
        return False
    dismiss_nodes = [
        n for n in current.nodes if n.get("text", "").strip() == "暂时不了"
    ] or [
        n for n in current.nodes if n.get("content-desc", "").strip() == "关闭"
    ]
    if not dismiss_nodes:
        return False
    box = bounds(dismiss_nodes[0])
    device.command(
        "input",
        "tap",
        str((box[0] + box[2]) // 2),
        str((box[1] + box[3]) // 2),
    )
    time.sleep(0.6)
    return True


def _dismiss_known_popup_or_overlay(device, page) -> bool:
    if dismiss_location_permission_modal(device, page):
        return True
    for label in ("暂时不了", "不再提醒", "我知道了", "知道了", "以后再说", "暂不更新", "收起"):
        nodes = [n for n in page.nodes if n.get("text", "").strip() == label]
        if nodes:
            box = bounds(nodes[0])
            device.command(
                "input",
                "tap",
                str((box[0] + box[2]) // 2),
                str((box[1] + box[3]) // 2),
            )
            time.sleep(0.5)
            return True
    if any(n.get("text", "").strip() == "定位权限未开启" for n in page.nodes):
        close_nodes = [n for n in page.nodes if n.get("content-desc", "").strip() == "关闭"]
        if close_nodes:
            box = bounds(close_nodes[0])
            device.command(
                "input",
                "tap",
                str((box[0] + box[2]) // 2),
                str((box[1] + box[3]) // 2),
            )
            time.sleep(0.5)
            return True
    return False


def bounded_recover_to_target(
    device,
    target: str = "recommend",
    *,
    expected_keyword: str | None = None,
    max_steps: int = 4,
) -> dict:
    """Recover from loading failures, popups, or unexpected pages to the target
    mode feed within `max_steps` bounded steps, or fail with a clear reason."""
    if target not in ("recommend", "discover", "city", "search"):
        raise ValueError(f"Unsupported recovery target: {target}")
    if type(max_steps) is not int or not 1 <= max_steps <= 10:
        raise ValueError("max_steps must be between 1 and 10")
    normalized_target = "recommend" if target == "discover" else target
    expected_mode_page = f"{normalized_target}_feed"
    retried_load = False
    last_reason = "unknown"
    actions = []

    for step in range(max_steps + 1):
        check_stop(device)
        try:
            page = call_with_captcha_pause(device, device.page)
        except PageMismatch as error:
            if "Xiaohongshu is not visible" in str(error) and step < max_steps:
                last_reason = "app_not_in_foreground"
                device.command(
                    "am",
                    "start",
                    "--display",
                    "0",
                    "-n",
                    "com.xingin.xhs/.index.v2.IndexActivityV2",
                )
                actions.append("relaunch_xhs")
                time.sleep(1.0)
                continue
            raise
        mode_page = classify_mode_page(page, expected_keyword=expected_keyword)
        if mode_page == expected_mode_page:
            return dict(
                recovered=True,
                target=normalized_target,
                pageType=mode_page,
                stepsUsed=step,
                actions=actions,
            )
        abnormal = detect_abnormal_state(page)
        last_reason = abnormal.get("detail") or abnormal.get("reason") or mode_page
        if step == max_steps:
            break

        if _dismiss_known_popup_or_overlay(device, page):
            actions.append(f"dismiss:{abnormal['reason']}")
            continue

        if abnormal["kind"] == "loading_failure":
            if abnormal.get("retryable") and not retried_load:
                retry_nodes = [
                    n
                    for n in page.nodes
                    if n.get("text", "").strip() in ("点击重试", "刷新重试", "重试", "刷新")
                ]
                if retry_nodes:
                    retried_load = True
                    box = bounds(retry_nodes[0])
                    device.command(
                        "input",
                        "tap",
                        str((box[0] + box[2]) // 2),
                        str((box[1] + box[3]) // 2),
                    )
                    actions.append("tap_retry")
                    time.sleep(0.8)
                    continue
            device.back()
            actions.append(f"back_from_failure:{abnormal['reason']}")
            time.sleep(0.5)
            continue

        # If we are on Search Input / Search Results and want Home (recommend/city), tap 返回 if visible.
        if normalized_target in ("recommend", "city") and any(
            n.get("text", "").strip() in ("历史记录", "猜你想搜", "全部") for n in page.nodes
        ):
            back_icons = [
                n
                for n in page.nodes
                if n.get("content-desc", "").strip() == "返回" and bounds(n)[1] < 300
            ]
            if back_icons:
                box = bounds(back_icons[0])
                device.command(
                    "input",
                    "tap",
                    str((box[0] + box[2]) // 2),
                    str((box[1] + box[3]) // 2),
                )
                actions.append("tap_top_back")
                time.sleep(0.6)
                continue

        # If the bottom Home tab is visible, switch directly to 首页 -> 发现 / 同城.
        home_nodes = [n for n in page.nodes if n.get("content-desc") == "首页"]
        if home_nodes and normalized_target in ("recommend", "city"):
            if home_nodes[0].get("selected") != "true":
                box = bounds(home_nodes[0])
                device.command(
                    "input",
                    "tap",
                    str((box[0] + box[2]) // 2),
                    str((box[1] + box[3]) // 2),
                )
                actions.append("tap_home_tab")
                time.sleep(0.5)
                continue
            if normalized_target == "recommend":
                disc_nodes = [n for n in page.nodes if n.get("content-desc") == "发现"]
                if disc_nodes:
                    box = bounds(disc_nodes[0])
                    device.command(
                        "input",
                        "tap",
                        str((box[0] + box[2]) // 2),
                        str((box[1] + box[3]) // 2),
                    )
                    actions.append("tap_discover_tab")
                    time.sleep(0.5)
                    continue
            elif normalized_target == "city":
                try:
                    city_node = page.city_tab()
                    box = bounds(city_node)
                    device.command(
                        "input",
                        "tap",
                        str((box[0] + box[2]) // 2),
                        str((box[1] + box[3]) // 2),
                    )
                    actions.append("tap_city_tab")
                    time.sleep(0.6)
                    continue
                except PageMismatch:
                    pass

        device.back()
        actions.append(f"back_from:{last_reason}")
        time.sleep(0.5)

    raise PageMismatch(
        f"Bounded recovery to {normalized_target} failed after {max_steps} steps: reason={last_reason}"
    )


# ---------------------------------------------------------------------------
# 3. Search Mode: Keyword Input, Sort & Filter Selection
# ---------------------------------------------------------------------------


def _normalize_sort(sort_by) -> str:
    if sort_by not in SORT_ALIASES:
        raise ValueError(f"Unsupported search sort option: {sort_by}")
    return SORT_ALIASES[sort_by]


def _normalize_filter_note_type(note_type) -> str:
    if note_type not in NOTE_TYPE_FILTER_ALIASES:
        raise ValueError(f"Unsupported search note_type filter: {note_type}")
    return NOTE_TYPE_FILTER_ALIASES[note_type]


def _normalize_publish_time(publish_time) -> str:
    if publish_time not in PUBLISH_TIME_ALIASES:
        raise ValueError(f"Unsupported search publish_time filter: {publish_time}")
    return PUBLISH_TIME_ALIASES[publish_time]


def search_keyword(device, keyword: str) -> dict:
    """Open search, enter `keyword`, submit search, and verify search results page."""
    if not isinstance(keyword, str) or not keyword.strip():
        raise ValueError("Search keyword must be a non-empty string")
    target_kw = keyword.strip()
    check_stop(device)
    ensure_clean_page_on_resume(device)
    page = call_with_captcha_pause(device, device.page)

    # Already on search results for this exact keyword?
    try:
        cards = page.search_cards(expected_keyword=target_kw)
        if cards:
            return dict(
                status="search_results_ready",
                keyword=target_kw,
                reusedExistingSearch=True,
                visibleCardCount=len(cards),
            )
    except PageMismatch:
        pass

    # Open the search input EditText if not already visible.
    edit_nodes = [n for n in page.nodes if n.get("class") == "android.widget.EditText"]
    if not edit_nodes:
        # Case 1: On search results for a different keyword -> tap clear or keyword box.
        clear_nodes = [n for n in page.nodes if n.get("content-desc") == "全部删除"]
        search_btn_nodes = [n for n in page.nodes if n.get("content-desc") == "搜索"]
        if clear_nodes:
            device.click(
                lambda current: next(
                    n for n in current.nodes if n.get("content-desc") == "全部删除"
                )
            )
        elif search_btn_nodes:
            device.click(lambda current: current.unique(description="搜索"))
        else:
            bounded_recover_to_target(device, target="recommend", max_steps=4)
            device.click(lambda current: current.unique(description="搜索"))
        if getattr(device, "has_vision", False) is True:
            device.wait_visual_settle(timeout=0.8)

    def find_search_edit(current):
        edits = [n for n in current.nodes if n.get("class") == "android.widget.EditText"]
        if not edits:
            raise PageMismatch("Search EditText is not visible")
        return edits[0]

    edit_node = device.wait(find_search_edit, timeout=8)
    box = bounds(edit_node)
    device.command(
        "input",
        "tap",
        str((box[0] + box[2]) // 2),
        str((box[1] + box[3]) // 2),
    )
    time.sleep(0.2)
    check_stop(device)
    device.set_text(target_kw)

    def verify_keyword_typed(current):
        edit = find_search_edit(current)
        actual = edit.get("text", "").strip()
        if target_kw not in actual:
            raise PageMismatch("Search keyword was not populated in EditText")
        search_triggers = [
            n
            for n in current.nodes
            if n.get("text", "").strip() == "搜索" and n.get("clickable") == "true"
        ]
        if not search_triggers:
            raise PageMismatch("Search submit button is not visible")
        return search_triggers[0]

    check_stop(device)
    device.click(verify_keyword_typed)
    if getattr(device, "has_vision", False) is True:
        device.wait_visual_settle(timeout=1.2)
    cards = device.wait(
        lambda current: current.search_cards(expected_keyword=target_kw),
        timeout=15,
    )
    return dict(
        status="search_results_ready",
        keyword=target_kw,
        reusedExistingSearch=False,
        visibleCardCount=len(cards),
    )


def find_search_filter_option(page, section_title: str, option_text: str):
    """Locate the option node inside a specific section of the search filter dropdown."""
    headers = {}
    for title in FILTER_SECTION_ORDER:
        matches = [
            n
            for n in page.nodes
            if n.get("class") == "android.widget.TextView"
            and n.get("text", "").strip() == title
        ]
        if matches:
            headers[title] = max(matches, key=lambda n: bounds(n)[1])
    if section_title not in headers:
        raise PageMismatch(f"Filter section {section_title!r} is not visible")
    sec_box = bounds(headers[section_title])
    lower_y = sec_box[3] - 4
    upper_y = None
    sec_index = FILTER_SECTION_ORDER.index(section_title)
    for next_title in FILTER_SECTION_ORDER[sec_index + 1 :]:
        if next_title in headers:
            next_top = bounds(headers[next_title])[1]
            if next_top > sec_box[3]:
                upper_y = next_top + 4
                break
    if upper_y is None:
        upper_y = sec_box[3] + 320

    candidates = [
        n
        for n in page.nodes
        if n.get("class") == "android.widget.TextView"
        and n.get("text", "").strip() == option_text
        and lower_y <= bounds(n)[1]
        and bounds(n)[3] <= upper_y
    ]
    if len(candidates) != 1:
        raise PageMismatch(
            f"Filter option {option_text!r} under {section_title!r} is not uniquely visible"
        )
    return candidates[0]


def apply_search_filters(
    device,
    *,
    sort_by: str | None = "综合",
    note_type: str | None = "不限",
    publish_time: str | None = "不限",
    expected_keyword: str | None = None,
) -> dict:
    """Open the search filter panel, select sort/note_type/publish_time options,
    verify each option's selected state, collapse the panel, and verify results."""
    sort_label = _normalize_sort(sort_by)
    type_label = _normalize_filter_note_type(note_type)
    time_label = _normalize_publish_time(publish_time)
    check_stop(device)

    page = device.page()
    if not any(n.get("text", "").strip() == "排序依据" for n in page.nodes):
        def find_all_filter_tab(current):
            tabs = [
                n
                for n in current.nodes
                if n.get("text", "").strip() == "全部" and bounds(n)[1] < 600
            ]
            if not tabs:
                raise PageMismatch("Search '全部' filter dropdown trigger is not visible")
            return min(tabs, key=lambda n: bounds(n)[1])

        device.click(find_all_filter_tab)
        if getattr(device, "has_vision", False) is True:
            device.wait_visual_settle(timeout=0.8)

    device.wait(
        lambda current: find_search_filter_option(current, "排序依据", sort_label),
        timeout=8,
    )

    selections = (
        ("排序依据", sort_label),
        ("笔记类型", type_label),
        ("发布时间", time_label),
    )
    for section_title, option_label in selections:
        check_stop(device)
        current_opt = find_search_filter_option(device.page(), section_title, option_label)
        if current_opt.get("selected") != "true":
            device.click(
                lambda current, s=section_title, o=option_label: find_search_filter_option(
                    current, s, o
                )
            )
            time.sleep(0.25)

            def verify_selected(current, s=section_title, o=option_label):
                node = find_search_filter_option(current, s, o)
                if node.get("selected") != "true":
                    raise PageMismatch(f"Option {o!r} in {s!r} is not selected yet")
                return node

            device.wait(verify_selected, timeout=6)

    # Collapse the filter panel via '收起' if still open.
    page = device.page()
    if any(n.get("text", "").strip() == "排序依据" for n in page.nodes):
        collapse_nodes = [
            n for n in page.nodes if n.get("text", "").strip() == "收起"
        ]
        if collapse_nodes:
            device.click(
                lambda current: next(
                    n for n in current.nodes if n.get("text", "").strip() == "收起"
                )
            )
        else:
            device.back()
        if getattr(device, "has_vision", False) is True:
            device.wait_visual_settle(timeout=1.0)

    cards = device.wait(
        lambda current: current.search_cards(expected_keyword=expected_keyword),
        timeout=15,
    )
    if type_label == "视频" and any(c.note_type != "VIDEO" for c in cards):
        raise PageMismatch("Video filter applied but non-video cards remain in search results")
    if type_label == "图文" and any(c.note_type != "IMAGE_TEXT" for c in cards):
        raise PageMismatch("Image-text filter applied but video cards remain in search results")

    return dict(
        status="search_filters_applied",
        sortBy=sort_label,
        noteType=type_label,
        publishTime=time_label,
        verifiedInPanel=True,
        panelClosed=True,
        visibleCardCount=len(cards),
        visibleCardTypes=sorted({c.note_type for c in cards}),
    )


# ---------------------------------------------------------------------------
# 4. City / Nearby Mode Entry & Location Permission Handling
# ---------------------------------------------------------------------------


def open_city_feed(device, *, require_location_permission: bool = False) -> dict:
    """Switch to the City/Nearby tab, detect missing entry or location-permission
    prompts, and verify the city feed."""
    check_stop(device)
    ensure_clean_page_on_resume(device)
    page = call_with_captcha_pause(device, device.page)

    if not any(n.get("content-desc") == "首页" for n in page.nodes):
        bounded_recover_to_target(device, target="recommend", max_steps=4)
        page = device.page()

    home_nodes = [n for n in page.nodes if n.get("content-desc") == "首页"]
    if home_nodes and home_nodes[0].get("selected") != "true":
        device.click(lambda current: current.unique(description="首页"))
        page = device.page()

    try:
        tab = page.city_tab()
    except PageMismatch:
        return dict(
            status="entry_unavailable",
            available=False,
            reason="city_tab_missing",
        )

    selected = tab.get("selected") == "true" or any(
        c.get("selected") == "true" for c in tab.iter("node")
    )
    if not selected:
        device.click(lambda current: current.city_tab())
        time.sleep(0.8)
        if getattr(device, "has_vision", False) is True:
            device.wait_visual_settle(timeout=0.8)

    page = device.page()
    location_prompted = dismiss_location_permission_modal(device, page)
    if location_prompted:
        page = device.page()

    location_banner = any(
        "开启定位权限" in n.get("text", "").strip() for n in page.nodes
    )
    location_granted = not (location_prompted or location_banner)

    if require_location_permission and not location_granted:
        return dict(
            status="location_permission_unavailable",
            available=False,
            reason="location_permission_not_granted",
            locationPermissionGranted=False,
            locationPermissionPrompted=location_prompted,
            locationBannerVisible=location_banner,
        )

    def _wait_city_cards(current):
        nonlocal location_prompted
        if dismiss_location_permission_modal(device, current):
            location_prompted = True
            raise PageMismatch("Dismissed location permission modal; waiting for city feed")
        return current.city_cards()

    try:
        cards = device.wait(_wait_city_cards, timeout=10)
    except PageMismatch:
        return dict(
            status="location_permission_unavailable"
            if not location_granted
            else "entry_unavailable",
            available=False,
            reason="city_feed_unavailable",
            locationPermissionGranted=location_granted,
            locationPermissionPrompted=location_prompted,
            locationBannerVisible=location_banner,
        )

    if not cards:
        return dict(
            status="location_permission_unavailable"
            if not location_granted
            else "city_feed_empty",
            available=False,
            reason="no_visible_city_cards",
            locationPermissionGranted=location_granted,
            locationPermissionPrompted=location_prompted,
            locationBannerVisible=location_banner,
        )

    return dict(
        status="city_feed_ready",
        available=True,
        locationPermissionGranted=location_granted,
        locationPermissionPrompted=location_prompted,
        locationBannerVisible=location_banner,
        visibleCardCount=len(cards),
    )


# ---------------------------------------------------------------------------
# 5. Unified Continuous Feed Runner (Recommend / Search / City) & Auto Mode
# ---------------------------------------------------------------------------


def _process_single_card_in_feed(
    device,
    data_dir: Path,
    *,
    seen: set[str],
    seen_file: Path,
    cards_fn,
    note_type: str | None = None,
    max_screens: int = 6,
    read_clipboard=None,
    inspect_note_comments: bool = False,
    comment_options: dict | None = None,
    interaction_options: dict | None = None,
    quota_store: DailyQuotaStore | None = None,
) -> dict:
    check_stop(device)
    cards, card = call_with_captcha_pause(
        device,
        select_feed_card,
        device,
        seen,
        note_type=note_type,
        max_screens=max_screens,
        cards_fn=cards_fn,
    )
    cover = card.cover_box or card.box
    device.command(
        "input",
        "tap",
        str((cover[0] + cover[2]) // 2),
        str((cover[1] + cover[3]) // 2),
    )
    if getattr(device, "has_vision", False) is True:
        device.wait_visual_settle(timeout=0.8)

    if callable(read_clipboard):
        detail = call_with_captcha_pause(
            device,
            collect_open_note,
            device,
            read_clipboard,
            expected_card=card,
        )
    else:
        detail = call_with_captcha_pause(
            device,
            read_note_detail,
            device,
            expected_card=card,
        )

    comments_report = None
    if inspect_note_comments:
        copts = dict(comment_options or {})
        comments_report = call_with_captcha_pause(
            device,
            inspect_comments,
            device,
            data_dir,
            max_pages=int(copts.get("maxPages", 2)),
            max_profiles=int(copts.get("maxProfiles", 2)),
        )

    interaction_report = None
    if interaction_options:
        iopts = dict(interaction_options)
        iopts.setdefault("expectedWork", detail)
        interaction_report = call_with_captcha_pause(
            device,
            interact_work_and_comments,
            device,
            data_dir,
            options=iopts,
        )

    call_with_captcha_pause(
        device,
        restore_feed,
        device,
        cards,
        cards_fn=cards_fn,
    )
    seen.add(card.key)
    _save_seen(seen_file, seen)

    return dict(
        cardKey=card.key,
        cardTitle=card.title,
        cardAuthor=card.author,
        noteType=detail.get("noteType"),
        detail=detail,
        commentsReport=comments_report,
        interactionReport=interaction_report,
    )


def run_recommend_mode(
    device,
    data_dir,
    *,
    count: int = 1,
    note_type: str | None = None,
    max_screens: int = 6,
    read_clipboard=None,
    inspect_note_comments: bool = False,
    comment_options: dict | None = None,
    interaction_options: dict | None = None,
    quota_store: DailyQuotaStore | None = None,
) -> dict:
    """Continuously select, process, and scroll works in Recommend (Discover) mode,
    skipping already-seen cards."""
    if type(count) is not int or not 1 <= count <= 50:
        raise ValueError("count must be between 1 and 50")
    started = time.monotonic()
    data_dir = Path(data_dir)
    seen_file, seen = _load_seen(data_dir)
    initial_seen_count = len(seen)

    ensure_clean_page_on_resume(device)
    try:
        current_mode = classify_mode_page(call_with_captcha_pause(device, device.page))
    except PageMismatch:
        current_mode = "unknown"
    if current_mode != "recommend_feed":
        try:
            bounded_recover_to_target(device, target="recommend", max_steps=4)
        except PageMismatch:
            call_with_captcha_pause(device, open_discover, device)

    verify_stage_page(device, "recommend")
    notes = []
    for index in range(count):
        if index > 0:
            current_cards = device.wait(lambda page: page.cards())
            if all(
                c.key in seen or (note_type is not None and c.note_type != note_type)
                for c in current_cards
            ):
                scroll_feed(device, current_cards)
        notes.append(
            _process_single_card_in_feed(
                device,
                data_dir,
                seen=seen,
                seen_file=seen_file,
                cards_fn=lambda page: page.cards(),
                note_type=note_type,
                max_screens=max_screens,
                read_clipboard=read_clipboard,
                inspect_note_comments=inspect_note_comments,
                comment_options=comment_options,
                interaction_options=interaction_options,
                quota_store=quota_store,
            )
        )

    return dict(
        status="recommend_mode_completed",
        mode="recommend",
        stageVerified=True,
        pageType="recommend_feed",
        processedCount=len(notes),
        initialSeenCount=initial_seen_count,
        finalSeenCount=len(seen),
        notes=notes,
        elapsedMs=round((time.monotonic() - started) * 1000),
    )


def run_search_mode(
    device,
    data_dir,
    keyword: str,
    *,
    sort_by: str | None = "综合",
    filter_note_type: str | None = "不限",
    publish_time: str | None = "不限",
    count: int = 1,
    max_screens: int = 6,
    read_clipboard=None,
    inspect_note_comments: bool = False,
    comment_options: dict | None = None,
    interaction_options: dict | None = None,
    quota_store: DailyQuotaStore | None = None,
) -> dict:
    """Search keyword, apply sort/note_type/publish_time filters, confirm filter effect,
    and process works from search results."""
    if type(count) is not int or not 0 <= count <= 50:
        raise ValueError("count must be between 0 and 50")
    started = time.monotonic()
    data_dir = Path(data_dir)
    seen_file, seen = _load_seen(data_dir)

    search_state = call_with_captcha_pause(device, search_keyword, device, keyword)
    target_kw = search_state["keyword"]
    filter_state = call_with_captcha_pause(
        device,
        apply_search_filters,
        device,
        sort_by=sort_by,
        note_type=filter_note_type,
        publish_time=publish_time,
        expected_keyword=target_kw,
    )
    verify_stage_page(device, "search", expected_keyword=target_kw)

    card_note_type = None
    if filter_state["noteType"] == "视频":
        card_note_type = "VIDEO"
    elif filter_state["noteType"] == "图文":
        card_note_type = "IMAGE_TEXT"

    cards_fn = lambda page: page.search_cards(expected_keyword=target_kw)
    notes = []
    for index in range(count):
        if index > 0:
            current_cards = device.wait(cards_fn)
            if all(
                c.key in seen
                or (card_note_type is not None and c.note_type != card_note_type)
                for c in current_cards
            ):
                scroll_feed(device, current_cards)
        notes.append(
            _process_single_card_in_feed(
                device,
                data_dir,
                seen=seen,
                seen_file=seen_file,
                cards_fn=cards_fn,
                note_type=card_note_type,
                max_screens=max_screens,
                read_clipboard=read_clipboard,
                inspect_note_comments=inspect_note_comments,
                comment_options=comment_options,
                interaction_options=interaction_options,
                quota_store=quota_store,
            )
        )

    return dict(
        status="search_mode_completed",
        mode="search",
        stageVerified=True,
        pageType="search_feed",
        keyword=target_kw,
        searchState=search_state,
        filterState=filter_state,
        processedCount=len(notes),
        notes=notes,
        elapsedMs=round((time.monotonic() - started) * 1000),
    )


def run_city_mode(
    device,
    data_dir,
    *,
    count: int = 1,
    note_type: str | None = None,
    max_screens: int = 6,
    require_location_permission: bool = False,
    read_clipboard=None,
    inspect_note_comments: bool = False,
    comment_options: dict | None = None,
    interaction_options: dict | None = None,
    quota_store: DailyQuotaStore | None = None,
) -> dict:
    """Enter City/Nearby tab, handle missing entry or location-permission state,
    and continuously process works."""
    if type(count) is not int or not 0 <= count <= 50:
        raise ValueError("count must be between 0 and 50")
    started = time.monotonic()
    data_dir = Path(data_dir)
    seen_file, seen = _load_seen(data_dir)

    city_state = call_with_captcha_pause(
        device,
        open_city_feed,
        device,
        require_location_permission=require_location_permission,
    )
    if not city_state.get("available"):
        return dict(
            status=city_state["status"],
            mode="city",
            stageVerified= False,
            available=False,
            reason=city_state.get("reason"),
            cityState=city_state,
            processedCount=0,
            notes=[],
            elapsedMs=round((time.monotonic() - started) * 1000),
        )

    verify_stage_page(device, "city")

    def cards_fn(page):
        if dismiss_location_permission_modal(device, page):
            return device.page().city_cards()
        return page.city_cards()

    notes = []
    for index in range(count):
        if index > 0:
            current_cards = device.wait(cards_fn)
            if all(
                c.key in seen or (note_type is not None and c.note_type != note_type)
                for c in current_cards
            ):
                scroll_feed(device, current_cards)
        notes.append(
            _process_single_card_in_feed(
                device,
                data_dir,
                seen=seen,
                seen_file=seen_file,
                cards_fn=cards_fn,
                note_type=note_type,
                max_screens=max_screens,
                read_clipboard=read_clipboard,
                inspect_note_comments=inspect_note_comments,
                comment_options=comment_options,
                interaction_options=interaction_options,
                quota_store=quota_store,
            )
        )

    return dict(
        status="city_mode_completed",
        mode="city",
        stageVerified=True,
        available=True,
        pageType="city_feed",
        cityState=city_state,
        processedCount=len(notes),
        notes=notes,
        elapsedMs=round((time.monotonic() - started) * 1000),
    )


def verify_stage_page(
    device,
    stage: str,
    *,
    expected_keyword: str | None = None,
) -> dict:
    """Confirm that the current UI page matches the active stage."""
    normalized = "recommend" if stage == "discover" else stage
    if normalized not in ("recommend", "search", "city"):
        raise ValueError(f"Unsupported stage: {stage}")
    expected_page_type = f"{normalized}_feed"

    def check(current):
        actual = classify_mode_page(current, expected_keyword=expected_keyword)
        if actual != expected_page_type:
            raise PageMismatch(
                f"Stage page mismatch: expected {expected_page_type}, got {actual}"
            )
        return dict(
            stage=normalized,
            pageType=actual,
            verified=True,
        )

    return device.wait(check, timeout=10)


def run_auto_mode(
    device,
    data_dir,
    *,
    stages: list[dict] | None = None,
    read_clipboard=None,
    quota_store: DailyQuotaStore | None = None,
) -> dict:
    """Switch across configured recommend, search, and city stages in order,
    verifying page-to-stage correspondence after every switch."""
    started = time.monotonic()
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if quota_store is None:
        quota_store = DailyQuotaStore(data_dir)

    if not stages:
        stages = [
            {"stage": "recommend", "count": 1},
            {
                "stage": "search",
                "keyword": "宠物",
                "sortBy": "最新",
                "noteType": "图文",
                "publishTime": "一周内",
                "count": 1,
            },
            {"stage": "city", "count": 1},
        ]

    stage_reports = []
    for spec in stages:
        check_stop(device)
        stage_name = (spec.get("stage") or spec.get("mode") or "").strip().lower()
        if stage_name in ("recommend", "discover"):
            bounded_recover_to_target(device, target="recommend", max_steps=4)
            stage_check = verify_stage_page(device, "recommend")
            report = run_recommend_mode(
                device,
                data_dir,
                count=int(spec.get("count", 1)),
                note_type=spec.get("noteType"),
                max_screens=int(spec.get("maxScreens", 6)),
                read_clipboard=read_clipboard,
                inspect_note_comments=bool(spec.get("inspectComments", False)),
                comment_options=spec.get("commentOptions"),
                interaction_options=spec.get("interactionOptions"),
                quota_store=quota_store,
            )
            report["stageSwitchVerified"] = stage_check["verified"]
            stage_reports.append(report)
        elif stage_name == "search":
            keyword = spec.get("keyword") or "宠物"
            report = run_search_mode(
                device,
                data_dir,
                keyword=keyword,
                sort_by=spec.get("sortBy", "综合"),
                filter_note_type=spec.get("noteType", "不限"),
                publish_time=spec.get("publishTime", "不限"),
                count=int(spec.get("count", 1)),
                max_screens=int(spec.get("maxScreens", 6)),
                read_clipboard=read_clipboard,
                inspect_note_comments=bool(spec.get("inspectComments", False)),
                comment_options=spec.get("commentOptions"),
                interaction_options=spec.get("interactionOptions"),
                quota_store=quota_store,
            )
            stage_check = verify_stage_page(
                device, "search", expected_keyword=keyword.strip()
            )
            report["stageSwitchVerified"] = stage_check["verified"]
            stage_reports.append(report)
        elif stage_name == "city":
            bounded_recover_to_target(device, target="recommend", max_steps=4)
            report = run_city_mode(
                device,
                data_dir,
                count=int(spec.get("count", 1)),
                note_type=spec.get("noteType"),
                max_screens=int(spec.get("maxScreens", 6)),
                require_location_permission=bool(
                    spec.get("requireLocationPermission", False)
                ),
                read_clipboard=read_clipboard,
                inspect_note_comments=bool(spec.get("inspectComments", False)),
                comment_options=spec.get("commentOptions"),
                interaction_options=spec.get("interactionOptions"),
                quota_store=quota_store,
            )
            if report.get("available"):
                stage_check = verify_stage_page(device, "city")
                report["stageSwitchVerified"] = stage_check["verified"]
            else:
                report["stageSwitchVerified"] = False
            stage_reports.append(report)
        elif stage_name == "messages":
            msg_report = inspect_and_handle_messages(
                device,
                quota_store,
                max_messages=int(spec.get("maxMessages", 3)),
                like_message=bool(spec.get("likeMessage", False)),
                reply_text=spec.get("replyText"),
            )
            stage_reports.append(dict(mode="messages", **msg_report))
        else:
            raise ValueError(f"Unsupported stage in auto mode: {stage_name!r}")

    out = dict(
        status="auto_mode_completed",
        stagesCompleted=len(stage_reports),
        stages=stage_reports,
        quotaSnapshot=quota_store.snapshot(),
        elapsedMs=round((time.monotonic() - started) * 1000),
    )
    report_path = data_dir / f"auto-mode-{uuid.uuid4().hex}.json"
    _atomic_write_json(report_path, out)
    return out
