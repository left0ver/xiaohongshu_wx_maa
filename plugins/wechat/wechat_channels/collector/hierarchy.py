"""Fresh WeChat semantic controls first, then OCR corrected by the controls it overlaps."""

import re
import unicodedata
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

from collection import author_in_feed, find, parse_author, parse_basic_info, parse_video, profile_bio_expand
from layout import LABELS, label_values, reading_rows, work_region

PACKAGE = "com.tencent.mm"


class HierarchyUnavailable(ValueError):
    pass


# Layers stacked in one window. The comment drawer and the profile sheets stay in
# the tree above the feed, and the order of these siblings differs between
# devices, so the visible layer is chosen by kind: feed < drawer < other sheet.
FEED_LAYER, FEED_PAGER, DRAWER_LAYER = "nsv", "jlt", "ean"
# Containers WeChat draws without exposing their text. Their bounds limit OCR
# to the field; observed in WeChat 8.0.79/8.0.80 (2026-11-11).
TEXT_CONTAINERS = {
    "c6d": "comment-body", "kp7": "reply-expander", "kr": "ad-label", "dzo": "feed-author",
    "fzm": "profile-name", "fxa": "certification", "fy7": "bio", "fy8": "bio",
    "fyk": "follow-button", "mld": "message-button", "lz3": "reply-button",
}
# Descriptions that name an icon rather than repeat drawn text. OCR never sees
# them, so they are kept for locating controls but not offered as page text.
ICON_DESCRIPTIONS = {
    "暂停", "播放", "头像", "视频号logo", "搜索", "浮窗", "个人中心", "返回", "私信按钮",
    "表情", "图片", "清除搜索词", "更多功能", "取消静音", "纱罩", "camera", "消息提示管理",
}
# A single text line is drawn about this tall (720-wide pixels); TextView bounds
# often add padding, which would merge or split OCR-style reading rows.
TEXT_LINE_HEIGHT = 34


def _resource(node):
    return node.get("resource-id", "").rsplit("/", 1)[-1]


def _bounds(node):
    match = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", node.get("bounds", ""))
    return tuple(map(int, match.groups())) if match else None


def _has_label(node):
    return any(item.get("text", "").strip() or item.get("content-desc", "").strip() for item in node.iter("node"))


def covered_layers(root, width, height):
    """Full-screen sibling subtrees drawn under another one; none when no known layering is found.

    The feed is the bottom layer (the whole Channels page `nsv`, or the video
    pager `jlt` beside a sheet), the comment drawer is above it, and any other
    full-screen sheet (profiles) is above the drawer.
    """
    covered = []
    for node in root.iter("node"):
        layers = []
        for child in node:
            box = _bounds(child) if child.tag == "node" and _has_label(child) else None
            if box and (box[2] - box[0]) * (box[3] - box[1]) >= 0.8 * width * height:
                layers.append(child)
        rank = lambda layer: {FEED_LAYER: 0, FEED_PAGER: 0, DRAWER_LAYER: 1}.get(_resource(layer), 2)
        if len(layers) >= 2 and any(rank(layer) < 2 for layer in layers):
            top = max(rank(layer) for layer in layers)
            covered += [layer for layer in layers if rank(layer) < top]
    return covered


def _scaled(box, width):
    left, top, right, bottom = box
    scale = 720 / width
    return [round(left * scale), round(top * scale),
            max(1, round((right - left) * scale)), max(1, round((bottom - top) * scale))]


def text_lines(xml, width, height, containers=False):
    """Visible WeChat controls as OCR-like lines; with containers, also empty text containers.

    Text is kept exactly as the control reports it. A container entry has an
    empty text, a "container" kind and the container bounds.
    """
    if len(xml) > 4_000_000 or "<!DOCTYPE" in xml or "<!ENTITY" in xml:
        raise HierarchyUnavailable("Invalid hierarchy document")
    if width <= 0 or height <= width:
        raise RuntimeError("Collection requires a portrait physical display")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise HierarchyUnavailable("Incomplete hierarchy document") from error
    lines, holes = [], []
    hidden = {id(node) for layer in covered_layers(root, width, height) for node in layer.iter("node")}
    for node in root.iter("node"):
        if id(node) in hidden:
            continue
        if (node.get("package") != PACKAGE or node.get("enabled") == "false"
                or node.get("visible-to-user") == "false" or node.get("password") == "true"):
            continue
        box = _bounds(node)
        if not box or not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height):
            continue
        if node.get("class") == "android.widget.ProgressBar":
            resource = node.get("resource-id", "")
            lines.append({"text": "", "box": _scaled(box, width), "source": "hierarchy", "icon": True,
                          "selector": {"attribute": "resource-id", "value": resource,
                                       "resource": resource, "class": node.get("class")}})
            continue
        descendants = [child for child in node.iter("node") if child is not node]
        kind = TEXT_CONTAINERS.get(_resource(node))
        # A bio control can hold only its first lines, so it is always read
        # with OCR as well; fill_containers keeps the more complete reading.
        if kind and (kind == "bio" or not any(child.get("text", "").strip() for child in descendants)):
            holes.append({"text": "", "container": kind, "box": _scaled(box, width), "source": "hierarchy"})
        text = node.get("text", "").strip()
        if text and descendants:
            child_text = "".join(child.get("text", "").strip() for child in descendants)
            if child_text and re.sub(r"\s+", "", text) == re.sub(r"\s+", "", child_text):
                continue
        # Container descriptions can summarize children and duplicate whole
        # pages; only a container without labelled descendants speaks for itself.
        attribute = "text"
        if not text and not any(child.get("text", "").strip()
                                or child.get("content-desc", "").strip() not in ("", *ICON_DESCRIPTIONS)
                                for child in descendants):
            attribute = "content-desc"
            text = node.get(attribute, "").strip()
        if not text:
            continue
        if re.search(r"请完成.*验证|拖动滑块|安全验证|验证码", text):
            raise RuntimeError("Verification detected; manual intervention required")
        scaled = _scaled(box, width)
        if attribute == "text" and "\n" not in text and scaled[3] > TEXT_LINE_HEIGHT + 10:
            # Keep the vertical centre: a padded label must share the reading
            # row of the labels beside it, as OCR boxes do.
            scaled = [scaled[0], scaled[1] + (scaled[3] - TEXT_LINE_HEIGHT) // 2, scaled[2], TEXT_LINE_HEIGHT]
        selector = {"attribute": attribute, "value": node.get(attribute),
                    "resource": node.get("resource-id", ""), "class": node.get("class", "")}
        line = {"text": text, "box": scaled, "score": 1.0, "selector": selector, "source": "hierarchy"}
        if _resource(node) == "bkl":
            # 聊天消息保留完整控件范围，用于关联头像和发送失败按钮。
            line["controlBox"] = _scaled(box, width)
        if attribute == "content-desc" and text in ICON_DESCRIPTIONS:
            line["icon"] = True
        lines.append(line)
    if not lines:
        raise HierarchyUnavailable("No visible WeChat text controls")
    # Identical nested labels are one visual control, not two author candidates.
    lines = list({(line["text"], tuple(line["box"])): line for line in lines}.values())
    holes = list({(hole["container"], tuple(hole["box"])): hole for hole in holes}.values())
    return lines + holes if containers else lines


def fill_containers(lines, recognized):
    """Put OCR text into the empty containers it falls in; nothing else is taken from OCR.

    OCR only adds what the controls do not show: a reading that lies on a text
    control is dropped. Descriptions inside a container name the control, not
    the drawn text, so OCR replaces them. A bio control can hold only its first
    lines, so there the more complete of control text and OCR is kept.
    Each OCR line fills at most one container.
    """
    holes = [line for line in lines if line.get("container")]
    controls = [line for line in lines if not line.get("container")]
    texts = [item for item in controls if item.get("selector", {}).get("attribute") == "text"]

    def inside(item, hole):
        x, y, w, h = item["box"]
        cx, cy = x + w / 2, y + h / 2
        hx, hy, hw, hh = hole["box"]
        return hx - 4 <= cx <= hx + hw + 4 and hy - 4 <= cy <= hy + hh + 4

    def text_column(hole):
        # A comment body container spans the avatar column too; its text starts
        # under the nickname row above it, so OCR left of that is the avatar.
        if hole["container"] != "comment-body":
            return -1
        x, y = hole["box"][0], hole["box"][1]
        header = [item["box"][0] for item in controls
                  if 0 <= y - (item["box"][1] + item["box"][3]) <= 60 and item["box"][0] > x]
        return min(header) - 8 if header else x

    read = lambda items: len(re.sub(r"\s+", "", "".join(item["text"] for item in items)))
    taken, used, dropped = [], set(), set()
    for hole in holes:
        left = text_column(hole)
        found = [item for item in recognized
                 if id(item) not in used and inside(item, hole) and item["box"][0] >= left]
        own = [item for item in controls if inside(item, hole)]
        own_text = [item for item in own if item in texts]
        if hole["container"] == "bio" and own_text:
            if not found or read(own_text) >= read(found):
                used.update(id(item) for item in found)
                continue
            dropped.update(id(item) for item in own_text)
        else:
            found = [item for item in found if not any(inside(item, text) for text in texts)]
        if not found:
            continue
        dropped.update(id(item) for item in own if item not in texts)
        used.update(id(item) for item in found)
        taken += [{**item, "filledContainer": hole["container"]} for item in found]
    return [item for item in controls if id(item) not in dropped] + taken


def container_roi(lines, height):
    """One OCR region covering every empty container, or None."""
    holes = [line["box"] for line in lines if line.get("container")]
    if not holes:
        return None
    left = max(0, min(box[0] for box in holes))
    top = max(0, min(box[1] for box in holes))
    right = min(720, max(box[0] + box[2] for box in holes))
    bottom = min(height, max(box[1] + box[3] for box in holes))
    return [left, top, max(1, right - left), max(1, bottom - top)] if right > left and bottom > top else None


CORRECTION_RATIO = 0.75


def _word(text):
    return all(char.isalnum() for char in text)


def _reading_order(items):
    rows = []
    for item in sorted(items, key=lambda line: line["box"][1] + line["box"][3] / 2):
        center = item["box"][1] + item["box"][3] / 2
        row = next((row for row in rows if abs(row[0] - center) <= min(row[1], item["box"][3]) / 2), None)
        if row is None:
            rows.append([center, item["box"][3], [item]])
        else:
            row[2].append(item)
    return [item for row in rows for item in sorted(row[2], key=lambda line: line["box"][0])]


def _covers(control, line):
    """The control spans most of the OCR line's height and some of its width."""
    cx, cy, cw, ch = control["box"]
    lx, ly, lw, lh = line["box"]
    return (min(cx + cw, lx + lw) - max(cx, lx) > 0
            and min(cy + ch, ly + lh) - max(cy, ly) >= lh * 0.6)


def _components(recognized, controls):
    parent = list(range(len(recognized) + len(controls)))

    def root(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for i, line in enumerate(recognized):
        for j, control in enumerate(controls):
            if _covers(control, line):
                parent[root(i)] = root(len(recognized) + j)
    groups = {}
    for index in range(len(parent)):
        groups.setdefault(root(index), []).append(index)
    for members in groups.values():
        lines = [recognized[i] for i in members if i < len(recognized)]
        nodes = [controls[i - len(recognized)] for i in members if i >= len(recognized)]
        if lines and nodes:
            yield lines, nodes


def correct_ocr(recognized, semantic):
    """Correct OCR characters with overlapping control text, keeping OCR layout.

    OCR stays the source of geometry and of everything without a control
    (comment bodies, custom-drawn names). A group of overlapping lines and
    controls is corrected only when their texts already agree closely, so
    controls covered by a drawer or left from a previous frame cannot inject
    text. Only letters, digits and CJK are taken from controls: their text can
    hold icon placeholders, and OCR may read separators the control lacks.
    """
    controls = [item for item in semantic if item.get("selector", {}).get("attribute") == "text"]
    corrected = {id(line): line for line in recognized}
    for lines, nodes in _components(recognized, controls):
        lines, nodes = _reading_order(lines), _reading_order(nodes)
        ocr_chars, owner = [], []
        for index, line in enumerate(lines):
            for char in re.sub(r"\s+", "", line["text"]):
                ocr_chars.append(char)
                owner.append(index)
        control_chars = list(re.sub(r"\s+", "", "".join(node["text"] for node in nodes)))
        key = lambda chars: [unicodedata.normalize("NFKC", char) for char in chars]
        matcher = SequenceMatcher(None, key(ocr_chars), key(control_chars), autojunk=False)
        if not ocr_chars or matcher.ratio() < CORRECTION_RATIO:
            continue
        output = [[] for _ in lines]
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for offset in range(i2 - i1):
                    output[owner[i1 + offset]].append(control_chars[j1 + offset])
            elif tag == "replace" and _word("".join(control_chars[j1:j2])):
                # Spread a replacement across the OCR lines it came from.
                span = owner[i1:i2]
                for offset, char in enumerate(control_chars[j1:j2]):
                    output[span[min(len(span) - 1, offset * len(span) // (j2 - j1))]].append(char)
            elif tag == "insert":
                # Missed characters inside one OCR line; never at a line edge,
                # where a neighbouring or covered control's text would attach.
                if 0 < i1 < len(owner) and owner[i1 - 1] == owner[i1] and _word("".join(control_chars[j1:j2])):
                    output[owner[i1]].extend(control_chars[j1:j2])
            else:
                for offset in range(i1, i2):
                    output[owner[offset]].append(ocr_chars[offset])
        for line, chars in zip(lines, output):
            text = "".join(chars)
            if text and key(text) != key(re.sub(r"\s+", "", line["text"])):
                corrected[id(line)] = {**line, "text": text, "ocrText": line["text"], "source": "hierarchy"}
    return [corrected[id(line)] for line in recognized]


def refresh_target(lines, previous):
    candidates = [line for line in lines if line.get("selector") == previous.get("selector")]
    if len(candidates) != 1:
        raise RuntimeError("Control is missing or ambiguous; no input was sent")
    # A few pixels come from re-rounding padded bounds; a real move is larger.
    if max(abs(a - b) for a, b in zip(candidates[0]["box"], previous["box"])) > 6:
        raise RuntimeError("Control moved before click; no input was sent")
    return candidates[0]


def page_lines(lines):
    """The drawn text parsers read: no icon names or empty-container markers."""
    return [line for line in lines if not line.get("icon") and not line.get("container")]


def _feed_ready(lines, height, author):
    author_in_feed(lines, height)
    return True


def _drawer_ready(lines, height, author):
    work_region(lines)
    return True


def _profile_ready(lines, height, author):
    parse_author(lines, author or "")
    return True


def _comments_ready(lines, height, author):
    from comments import parse_comment_drawer
    parse_comment_drawer(lines, height)
    return True


def _profile_or_card_ready(lines, height, author):
    # A commenter without a Channels account opens a WeChat user card instead.
    from comments import user_card_open
    return user_card_open(lines, height) or _profile_ready(lines, height, author)


def _known_page(lines, height, author):
    from interactions import classify_page_state
    return classify_page_state(lines, height) != "unknown"


def _page_state(*states):
    def ready(lines, height, author):
        from interactions import classify_page_state
        return classify_page_state(lines, height) in states
    return ready


def _search_input_ready(lines, height, author):
    from modes import is_search_input_page
    return is_search_input_page(lines)


def _search_typed_ready(lines, height, author):
    return bool(find(lines, r"^搜索$", lambda line: line["box"][0] > 490 and line["box"][1] < 165))


def _search_results_ready(lines, height, author):
    from modes import is_search_results_page
    return is_search_results_page(lines)


def _search_filter_ready(lines, height, author):
    return bool(find(lines, r"^排序$", lambda line: line["box"][1] < 360))


def _search_sheet_ready(lines, height, author):
    return bool(find(lines, r"^(?:不限|5分钟以下|5-20分钟|20分钟以上|已关注|最近看过)$",
                     lambda line: line["box"][1] > 1150))


def _navigation_ready(lines, height, author):
    home = find(lines, r"^发现$", lambda line: line["box"][1] > height - 180)
    discover = find(lines, r"^发现$", lambda line: line["box"][1] < 250)
    return bool((home and find(lines, r"^通讯录$"))
                or (discover and find(lines, r"^视频号$"))
                or (find(lines, r"^推荐$") and find(lines, r"^[<＜《‹«]?\s*关注$") and find(lines, r"^朋友")))


def _menu_ready(lines, height, author):
    return sum(line["text"] == "更多信息" for line in lines) == 1


def _basic_info_ready(lines, height, author):
    parse_basic_info(lines)
    return all(label_values(lines).values())


def _share_sheet_ready(lines, height, author):
    return bool(find(lines, r"^(?:转发给朋友|复制链接)$"))


def _edit_box(lines, top=None):
    """An input box; its class only survives in control selectors."""
    return next((line for line in lines if line.get("selector", {}).get("class", "").endswith("EditText")
                 and (top is None or line["box"][1] < top)), None)


def _search_box_ready(lines, height, author):
    return bool(_edit_box(lines, top=200))


def _feed_or_search_ready(lines, height, author):
    if _edit_box(lines, top=200):
        return True
    return _feed_ready(lines, height, author)


def _home_ready(lines, height, author):
    bottom = lambda line: line["box"][1] > height - 180
    return bool(find(lines, r"^微信$", bottom) and find(lines, r"^通讯录$", bottom))


def _chat_search_ready(lines, height, author):
    return bool(_edit_box(lines, top=200))


def _chat_ready(lines, height, author):
    return bool(_edit_box(lines) or find(lines, r"^发送$|可按住\s*转文字|按住\s*说话|仅可发\s*1\s*条消息"))


# Pages read from controls, with OCR limited to empty text containers. Each
# check is the parser the flow applies next; a page that fails it, or a label
# not listed here, is read with full-screen OCR as before.
CONTROL_PAGES = {
    "navigation-bottom": _navigation_ready, "navigation-top": _navigation_ready,
    **dict.fromkeys(("feed", "before-author", "feed-after-collect", "feed-after-like", "feed-after-author-touch",
                     "feed-after-author-touch-retry", "feed-before-personal", "profile-first-work",
                     "profile-first-work-retry"), _feed_ready),
    **dict.fromkeys(("video-details", "expanded-description", "comments-preemptive-work-header",
                     "comments-preemptive-work-expanded"), _drawer_ready),
    **dict.fromkeys(("comments", "comments-check", "comments-expanded", "comments-replies", "comments-return",
                     "comments-after-skip", "comments-recover", "comments-after-like", "comments-after-post",
                     "comments-after-reply", "comments-after-profile-touch", "comments-after-profile-touch-retry",
                     "comments-after-user-card", "comments-preemptive-start", "comments-composer", "comments-typed",
                     "comments-reply-composer", "comments-reply-typed", "drawer-recover"), _comments_ready),
    **dict.fromkeys(("author-profile", "author-profile-touch", "author-profile-touch-retry", "profile-touch",
                     "profile-follow-check", "profile-after-follow", "profile-grid", "profile-after-first-work",
                     "profile-after-first-work-retry", "commenter-profile-before-more"), _profile_ready),
    "commenter-profile": _profile_or_card_ready,
    "profile-menu": _menu_ready, "commenter-profile-menu": _menu_ready,
    "author-basic-info": _basic_info_ready, "author-basic-info-retry": _basic_info_ready,
    **dict.fromkeys(("interact-start", "resume-check", "messages-start", "messages-return-check",
                     "preemptive-restore-check"), _known_page),
    "personal-page": _page_state("personal_page"),
    **dict.fromkeys(("messages-center", "messages-comments", "messages-after-like", "messages-after-reply"),
                    _page_state("channels_message_center")),
    **dict.fromkeys(("messages-reply-composer", "messages-reply-typed"), _page_state("composer_overlay")),
    "search-input": _search_input_ready, "search-typed": _search_typed_ready,
    **dict.fromkeys(("search-results", "search-results-retry", "search-video-tab", "search-filter-start",
                     "search-filter-video-tab", "search-filter-collapsed", "search-cards", "search-after-"),
                    _search_results_ready),
    **dict.fromkeys(("search-filter-panel", "search-filter-panel-reopen", "search-filter-after-"), _search_filter_ready),
    **dict.fromkeys(("search-duration-sheet", "search-scope-sheet"), _search_sheet_ready),
    **dict.fromkeys(("share-sheet", "share-sheet-scrolled"), _share_sheet_ready),
    "share-closed": _known_page, "share-link-paste": _search_box_ready, "share-feed": _feed_ready,
    "share-paste-drawer": _comments_ready, "share-link-paste-composer": _chat_ready, "share-link-cleared": _chat_ready,
    **dict.fromkeys(("share-return-", "share-recover-"), _feed_or_search_ready),
    "link-after-dm-": _known_page,
    **dict.fromkeys(("link-home", "link-home-"), _home_ready),
    **dict.fromkeys(("link-search", "link-search-typed"), _chat_search_ready),
    **dict.fromkeys(("link-chat", "link-chat-typed", "link-chat-sent", "dm-chat", "dm-chat-typed", "dm-chat-sent",
                     "dm-chat-after-notice", "link-chat-after-notice"), _chat_ready),
    **dict.fromkeys(("link-video", "link-video-"), _feed_ready),
}


def page_check(label):
    """The readiness check for a label; entries ending in '-' cover labels with a dynamic suffix."""
    if label in CONTROL_PAGES:
        return CONTROL_PAGES[label]
    return next((check for prefix, check in CONTROL_PAGES.items()
                 if prefix.endswith("-") and label.startswith(prefix)), None)


def complete_for(lines, label, height, author=None):
    """Whether these lines carry what the flow reads next on this page."""
    ready = page_check(label)
    if not ready:
        return False
    try:
        return bool(ready(page_lines(lines), height, author))
    except ValueError:
        return False


def choose_snapshot(label, height, author, hierarchy, templates, ocr, regional_ocr=None):
    """Controls first, OCR only inside their empty text containers, full OCR as the fallback."""
    try:
        lines = hierarchy()
    except HierarchyUnavailable:
        lines = []
    if lines and page_check(label):
        roi = container_roi(lines, height) if regional_ocr else None
        filled = fill_containers(lines, regional_ocr(roi)) if roi else lines
        if complete_for(filled, label, height, author):
            source = "mixed" if any(line.get("filledContainer") for line in filled) else "hierarchy"
            return page_lines(filled), source
    # Keep this lazy: a successful semantic lookup must not take a screenshot.
    if label.startswith("navigation-"):
        matched = templates()
        if matched:
            return matched, "template"
    recognized = ocr()
    if lines:
        recognized = correct_ocr(recognized, page_lines(lines))
        if any(line.get("source") == "hierarchy" for line in recognized):
            return recognized, "mixed"
    return recognized, "ocr"


def field_evidence(lines, label, author=None):
    """Record this snapshot's observations, without claiming cross-page identity verification."""
    try:
        if label in ("video-details", "expanded-description") and author:
            values = parse_video(lines, author)
            region = work_region(lines)
        elif label.startswith("author-basic-info"):
            values, region = parse_basic_info(lines), None
        elif label == "author-profile" and author:
            values, region = parse_author(lines, author), None
        else:
            return {}
    except ValueError as error:
        return {"page": {"status": "unreadable", "reason": str(error)}}
    fields = {}
    for key, value in values.items():
        if key in ("missingFields", "authorIdentityMethod"):
            continue
        if key == "description" and region:
            evidence = [item for row in region["body"] for item in row["items"]]
        elif label.startswith("author-basic-info"):
            names = {"authorDouyinId": "channel", "authorIpLocation": "ip", "authorProfileLocation": "location",
                     "authorCertification": "certification"}
            rows = reading_rows(lines)
            candidates = [index for index, row in enumerate(rows)
                          if re.match("^" + LABELS[names[key]], row["text"], re.I)]
            # A certification section heading can precede the actual value label.
            index = candidates[-1] if candidates else None
            evidence = []
            if index is not None and value is not None:
                for row in rows[index:]:
                    if row is not rows[index] and any(re.match("^" + pattern, row["text"], re.I) for pattern in LABELS.values()):
                        break
                    evidence.extend(row["items"])
        else:
            parts = str(value).splitlines() if value is not None else []
            evidence = [line for line in lines if any(part.replace(" ", "") in line["text"].replace(" ", "") for part in parts)]
        status = "observed" if value is not None else "not_observed"
        if key == "authorBio" and profile_bio_expand(lines, author):
            status = "truncated"
        fields[key] = {"value": value, "status": status,
                       "sources": sorted({line.get("source", "unknown") for line in evidence}),
                       "boxes": [line["box"] for line in evidence],
                       "observationIds": sorted({line["observationId"] for line in evidence if "observationId" in line}),
                       "observedAtUnixMs": max((line.get("observedAtUnixMs", 0) for line in evidence), default=None)}
    return fields
