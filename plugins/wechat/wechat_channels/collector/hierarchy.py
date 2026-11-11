"""Fresh WeChat semantic controls first, then OCR corrected by the controls it overlaps."""

import re
import unicodedata
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

from collection import find, parse_author, parse_basic_info, parse_video, profile_bio_expand
from layout import LABELS, label_values, reading_rows, work_region

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


# Pages whose controls carry every field and whose layout is simple enough to
# parse from control bounds alone. Drawers and profiles keep OCR geometry:
# their TextView bounds include padding, and covered feed controls stay in the tree.
SEMANTIC_LABELS = ("navigation-bottom", "navigation-top", "profile-menu", "author-basic-info")
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
    if candidates[0]["box"] != previous["box"]:
        raise RuntimeError("Control moved before click; no input was sent")
    return candidates[0]


def complete_for(lines, label, height, author=None):
    """Do not accept a partial hierarchy just because it contains some text."""
    if label not in SEMANTIC_LABELS:
        return False
    try:
        if label.startswith("navigation-"):
            home = find(lines, r"^发现$", lambda line: line["box"][1] > height - 180)
            discover = find(lines, r"^发现$", lambda line: line["box"][1] < 250)
            return bool((home and find(lines, r"^通讯录$"))
                        or (discover and find(lines, r"^视频号$"))
                        or (find(lines, r"^推荐$") and find(lines, r"^[<＜《‹«]?\s*关注$") and find(lines, r"^朋友")))
        if label == "profile-menu":
            return sum(line["text"] == "更多信息" for line in lines) == 1
        if label == "author-basic-info":
            parse_basic_info(lines)
            return all(label_values(lines).values())
    except ValueError:
        pass
    return False


def choose_snapshot(label, height, author, hierarchy, templates, ocr):
    """Controls first; OCR for what they do not expose, corrected by the controls it overlaps."""
    try:
        lines = hierarchy()
    except HierarchyUnavailable:
        lines = []
    if complete_for(lines, label, height, author):
        return lines, "hierarchy"
    # Keep this lazy: a successful semantic lookup must not take a screenshot.
    if label.startswith("navigation-"):
        matched = templates()
        if matched:
            return matched, "template"
    recognized = ocr()
    if lines:
        recognized = correct_ocr(recognized, lines)
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
