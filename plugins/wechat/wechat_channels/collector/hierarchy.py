"""Fresh WeChat semantic controls, with conservative OCR fallback eligibility."""

import re
import xml.etree.ElementTree as ET

from collection import author_in_feed, find, parse_author, parse_basic_info, parse_video

PACKAGE = "com.tencent.mm"


class HierarchyUnavailable(ValueError):
    pass


def text_lines(xml, width, height):
    if len(xml) > 4_000_000 or "<!DOCTYPE" in xml or "<!ENTITY" in xml:
        raise HierarchyUnavailable("Invalid hierarchy document")
    if width <= 0 or height <= width:
        raise RuntimeError("Collection requires a portrait physical display")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise HierarchyUnavailable("Incomplete hierarchy document") from error
    lines = []
    for node in root.iter("node"):
        if node.get("package") != PACKAGE or node.get("enabled") == "false":
            continue
        text = node.get("text", "").strip()
        # Container descriptions can summarize children and duplicate whole pages.
        attribute = "text"
        if not text and not list(node):
            attribute = "content-desc"
            text = node.get(attribute, "").strip()
        if not text:
            continue
        if re.search(r"请完成.*验证|拖动滑块|安全验证|验证码", text):
            raise RuntimeError("Verification detected; manual intervention required")
        match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
        if not match:
            continue
        left, top, right, bottom = map(int, match.groups())
        if not (0 <= left < right <= width and 0 <= top < bottom <= height):
            continue
        scale = 720 / width
        box = [round(left * scale), round(top * scale),
               max(1, round((right - left) * scale)), max(1, round((bottom - top) * scale))]
        selector = {"attribute": attribute, "value": node.get(attribute),
                    "resource": node.get("resource-id", ""), "class": node.get("class", "")}
        lines.append({"text": text, "box": box, "score": 1.0, "selector": selector})
    if not lines:
        raise HierarchyUnavailable("No visible WeChat text controls")
    # Identical nested labels are one visual control, not two author candidates.
    return list({(line["text"], tuple(line["box"])): line for line in lines}.values())


def refresh_target(lines, previous):
    candidates = [line for line in lines if line.get("selector") == previous.get("selector")]
    if len(candidates) != 1:
        raise RuntimeError("Control is missing or ambiguous; no input was sent")
    if candidates[0]["box"] != previous["box"]:
        raise RuntimeError("Control moved before click; no input was sent")
    return candidates[0]


def complete_for(lines, label, height, author=None):
    """Do not accept a partial hierarchy just because it contains some text."""
    try:
        if label.startswith("navigation-"):
            home = find(lines, r"^发现$", lambda line: line["box"][1] > height - 180)
            discover = find(lines, r"^发现$", lambda line: line["box"][1] < 250)
            return bool((home and find(lines, r"^通讯录$"))
                        or (discover and find(lines, r"^视频号$"))
                        or (find(lines, r"^推荐$") and find(lines, r"^关注$") and find(lines, r"^朋友")))
        if label in ("feed", "before-author", "returned-feed"):
            author_in_feed(lines, height)
            return label == "before-author" or bool(find(lines, r"^\d+(?:\.\d+)?[万wW]?$",
                lambda line: line["box"][0] > 600 and line["box"][1] > height - 130))
        if label in ("video-details", "expanded-description", "confirm-returned-description") and author:
            # Let the flow expand only a work-header control, never a comment.
            expand = find(lines, r"^(展开|全文|更多)$")
            row = find(lines, "作者")
            if expand and row and row["box"][1] < expand["box"][1] < row["box"][1] + 350:
                return bool(find(lines, r"^评论\s*\d*"))
            parse_video(lines, author)
            return True
        if label == "author-profile" and author:
            parse_author(lines, author)
            return True
        if label == "profile-menu":
            return sum(line["text"] == "更多信息" for line in lines) == 1
        if label == "author-basic-info":
            parse_basic_info(lines)
            return True
    except ValueError:
        pass
    return False


def choose_snapshot(label, height, author, hierarchy, templates, ocr):
    try:
        lines = hierarchy()
    except HierarchyUnavailable:
        lines = []
    if complete_for(lines, label, height, author):
        return lines, "hierarchy"
    # Keep this lazy: a successful semantic lookup must not take a screenshot.
    if label.startswith("navigation-"):
        lines = templates()
        if lines:
            return lines, "template"
    return ocr(), "ocr"
