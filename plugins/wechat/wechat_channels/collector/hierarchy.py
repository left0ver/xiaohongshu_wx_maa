"""Fresh WeChat semantic controls, with conservative OCR fallback eligibility."""

import re
import xml.etree.ElementTree as ET

from collection import author_in_feed, find, parse_author, parse_basic_info, parse_video, profile_bio_expand
from layout import LABELS, label_values, reading_rows, replace_region, supplemental_roi, work_region

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
        if (node.get("package") != PACKAGE or node.get("enabled") == "false"
                or node.get("visible-to-user") == "false" or node.get("password") == "true"):
            continue
        text = node.get("text", "").strip()
        children = list(node)
        if text and children:
            child_text = "".join(child.get("text", "").strip() for child in node.iter("node") if child is not node)
            if child_text and re.sub(r"\s+", "", text) == re.sub(r"\s+", "", child_text):
                continue
        # Container descriptions can summarize children and duplicate whole pages.
        attribute = "text"
        if not text and not children:
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
        lines.append({"text": text, "box": box, "score": 1.0, "selector": selector, "source": "hierarchy"})
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
            if work_region(lines, author)["expand"]:
                return True
            video = parse_video(lines, author)
            return bool(video["publishTimeText"] and video["publishLocation"])
        if label == "author-profile" and author:
            profile = parse_author(lines, author)
            return bool(profile["authorBio"] and (profile["authorIpLocation"] or profile["authorProfileLocation"]))
        if label == "profile-menu":
            return sum(line["text"] == "更多信息" for line in lines) == 1
        if label == "author-basic-info":
            parse_basic_info(lines)
            return all(label_values(lines).values())
    except ValueError:
        pass
    return False


def anchor_signature(lines, author, label):
    heading = find(lines, r"^评论\s*\d*$|^基础信息$")
    if label == "author-profile" and author:
        heading = next((line for line in lines if line["text"].startswith(author)), None)
    if not heading:
        return []
    # Include the known work text as well as author/title. Exclude the moving
    # video above the drawer; any other change causes a conservative full read.
    return sorted((line["text"], tuple(line["box"])) for line in lines if line["box"][1] >= heading["box"][1])


def choose_snapshot(label, height, author, hierarchy, templates, ocr, regional_ocr=None, refresh=None):
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
    elif lines and regional_ocr and refresh:
        roi = supplemental_roi(lines, label, height, author)
        if roi:
            recognized = regional_ocr(roi)
            previous = anchor_signature(lines, author, label)
            try:
                current = refresh()
            except HierarchyUnavailable:
                current = []
            # A fresh anchor check prevents joining two different pages/works.
            if previous and previous == anchor_signature(current, author, label):
                return replace_region(current, recognized, roi), "mixed"
    return ocr(), "ocr"


def field_evidence(lines, label, author=None):
    """Observed fields are not certified identities until the collection checks pass."""
    try:
        if label in ("video-details", "expanded-description", "confirm-returned-description") and author:
            values = parse_video(lines, author)
            region = work_region(lines, author)
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
