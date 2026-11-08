"""Reading order and bounded work/label regions, independent of the OCR engine."""

import re
import unicodedata
from statistics import median
from difflib import SequenceMatcher

DATE = r"(?:\d{4}年)?\d{1,2}月\d{1,2}日|\d{4}[-/]\d{1,2}[-/]\d{1,2}|(?:今天|昨天|前天)(?:\s*\d{1,2}:\d{2})?|\d+\s*(?:分钟|小时|天)前|刚刚"
REGION = r"北京|天津|河北|山西|内蒙古|辽宁|吉林|黑龙江|上海|江苏|浙江|安徽|福建|江西|山东|河南|湖北|湖南|广东|广西|海南|重庆|四川|贵州|云南|西藏|陕西|甘肃|青海|宁夏|新疆|香港|澳门|台湾|中国|美国|日本|韩国|英国|法国|德国|加拿大|澳大利亚|新加坡|马来西亚|泰国|海外"
# Screenshots are scaled to 720 wide; the status bar text sits above this line.
STATUS_BAR_BOTTOM = 60
LABELS = {
    "channel": r"视频号\s*ID",
    "ip": r"IP\s*(?:归)?属地",
    "location": r"资料所在地",
    "gender": r"性别",
    "certification": r"(?:认证信息|企业全称)",
    "entity": r"主体类型",
    "account": r"(?:帐号|账号)实名",
}


class WorkHeaderMissing(ValueError):
    pass


def compact(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def line_height(lines):
    return median([item["box"][3] for item in lines if item["box"][3] > 0]) if lines else 28


def bottom(item):
    return item["box"][1] + item["box"][3]


def reading_rows(lines):
    """Group fragments by vertical overlap, retaining original boxes/selectors."""
    rows = []
    for item in sorted(lines, key=lambda line: (line["box"][1], line["box"][0])):
        if not item.get("text", "").strip():
            continue
        row = next((row for row in reversed(rows) if
                    min(bottom(item), row["bottom"]) - max(item["box"][1], row["top"])
                    >= min(item["box"][3], row["height"]) * 0.5), None)
        if row is None:
            rows.append({"items": [item], "top": item["box"][1], "bottom": bottom(item),
                         "height": item["box"][3]})
        else:
            row["items"].append(item)
            row["top"] = min(row["top"], item["box"][1])
            row["bottom"] = max(row["bottom"], bottom(item))
    for row in rows:
        row["items"].sort(key=lambda line: line["box"][0])
        row["text"] = "".join(item["text"].strip() for item in row["items"])
        row["left"] = min(item["box"][0] for item in row["items"])
    return rows


def music_row(row, previous, height):
    pieces = [item for item in row["items"] if not re.fullmatch(r"展开|全文|更多", item["text"].strip())]
    text = compact("".join(item["text"] for item in pieces))
    # Keywords inside prose/hashtags are not controls. A separated control row
    # must have an exact label or a label followed by its song separator.
    label = re.fullmatch(r"[^#@\w\u4e00-\u9fff]*(?:(?:相关)?音乐|原声)(?:[·•:：].+)?", text)
    return bool(label and previous and row["top"] - previous["bottom"] >= height * 0.3)


def comment_row(row, previous, following, height):
    text = compact(row["text"])
    date = re.search(DATE, text)
    if not date or not following or not previous:
        return False
    prefix = text[:date.start()]
    region = re.search(f"(?:{REGION})$", prefix)
    # A timestamp alone (often part of the work's prose) is insufficient.
    author_and_region = bool(region and prefix[:region.start()])
    actions = any(item["box"][0] > 570
                  and re.fullmatch(r"[\d.万wW]+[凸👍♡赞]*|[凸👍♡赞]+", item["text"].strip())
                  for item in row["items"])
    return author_and_region and actions and row["top"] - previous["bottom"] >= height * 0.5


def work_region(lines):
    rows = reading_rows(lines)
    height = line_height(lines)
    heading = next((row for row in rows if re.match(r"^评论\s*\d*", row["text"])), None)
    if not heading:
        raise ValueError("The video details drawer is not open")
    rows = [row for row in rows if row["top"] > heading["bottom"]]
    header = None
    header_text = None
    for index, row in enumerate(rows):
        if row["top"] - heading["bottom"] > height * 6:
            break
        pieces = [item for item in row["items"] if not re.search(r"已声明原创|都在搜", item["text"])]
        text = compact("".join(item["text"] for item in pieces))
        marker = text.rfind("作者")
        if marker >= 0:
            # A nickname can itself end with 作者. Prefer a separate following
            # author-tag row when it contains only the tag and metadata.
            following = rows[index + 1] if index + 1 < len(rows) else None
            if (marker + 2 == len(text) and following
                    and following["top"] - row["bottom"] <= height * 1.5
                    and re.fullmatch(f"作者(?:(?:{REGION})?(?:{DATE})?|(?:{DATE})(?:{REGION})?)",
                                     compact(following["text"]))):
                continue
            header, header_text = row, text
            break
    if header is None:
        raise WorkHeaderMissing("Original work header is missing or outside the visible drawer")

    marker = header_text.rfind("作者")
    metadata = header_text[marker + 2:]
    content = [row for row in rows if row["top"] >= header["bottom"]]
    # Metadata may occupy its own line. Consume only exact date/location rows
    # immediately adjacent to the header, never arbitrary following prose.
    for row in content[:2]:
        text = compact(row["text"])
        existing = metadata_values({"metadata": metadata})
        incoming = metadata_values({"metadata": text})
        metadata_only = re.fullmatch(f"(?:(?:{REGION})(?:{DATE})?|(?:{DATE})(?:{REGION})?)", text)
        adds_missing = any(value and not existing[key] for key, value in incoming.items())
        if (row["top"] - header["bottom"] <= height * 1.2
                and metadata_only and adds_missing):
            metadata += text
            header = row
        else:
            break
    content = [row for row in content if row["top"] >= header["bottom"]]
    boundary, boundary_kind = None, None
    previous = header
    for index, row in enumerate(content):
        following = content[index + 1] if index + 1 < len(content) else None
        if music_row(row, previous, height):
            boundary, boundary_kind = row, "music"
        elif (re.fullmatch(rf"[●•·📍◎]+\s*(?:{REGION})(?:[省市县区]|[\s·•:：][\u4e00-\u9fffA-Za-z0-9]+)*",
                           compact(row["text"]))
              and row["top"] - previous["bottom"] >= height * 0.3):
            boundary, boundary_kind = row, "location"
        elif comment_row(row, previous, following, height):
            boundary, boundary_kind = row, "comment"
        elif (re.fullmatch(r"暂无评论|还没有评论[,，]?快来抢沙发|抢沙发|帮[他她]上热门|上热门", compact(row["text"]))
              and row["top"] - previous["bottom"] >= height * 0.3):
            boundary, boundary_kind = row, "footer"
        if boundary:
            break
        previous = row
    body = [row for row in content if boundary is None or row["top"] < boundary["top"]]
    expand_rows = [*body, *([boundary] if boundary_kind == "music" else [])]
    expand = next((item for row in expand_rows for item in row["items"]
                   if re.fullmatch(r"展开|全文|更多", item["text"].strip())), None)
    return {"heading": heading, "header": header, "authorHeader": header_text,
            "metadata": metadata, "body": body, "boundary": boundary,
            "boundaryKind": boundary_kind, "expand": expand, "lineHeight": height}


def metadata_values(region):
    metadata = compact(region["metadata"])
    date = re.search(DATE, metadata)
    location = metadata[:date.start()] + metadata[date.end():] if date else metadata
    location = location.strip("·•:：,，|丨I")
    if not re.fullmatch(f"(?:{REGION})(?:省|市|自治区|特别行政区)?", location):
        location = None
    return {"publishTimeText": date.group(0) if date else None, "publishLocation": location}


def video_values(lines, author):
    region = work_region(lines)
    if not region["boundary"]:
        raise ValueError("The full description boundary is not visible")
    if region["expand"]:
        raise ValueError("Description needs expansion")
    text = "".join(item["text"].strip() for row in region["body"] for item in row["items"]
                   if not re.search(r"已声明原创|发表评论", item["text"])
                   and not re.fullmatch(r"收起", item["text"].strip()))
    if not text or text.rstrip().endswith(("…", "...")):
        raise ValueError("Description is empty or still truncated")
    return {"authorNickname": author, "description": text, **metadata_values(region)}


def profile_name(lines, hint):
    """Locate the current header without validating it against an earlier name."""
    controls = [item for item in lines if re.fullmatch(r"[√✓]?\s*(?:已关注|申请关注|关注|私信|客服)", item["text"])]
    if len(controls) < 2:
        raise ValueError("Author profile controls are missing")
    end = min(item["box"][1] for item in controls)
    # The header sits directly above the follow/message row. Its distance from the
    # top differs between the feed sheet and the full-page profile, so only the
    # status bar is excluded; the hint ranking below separates any video caption.
    candidates = [item for item in lines if 170 <= item["box"][0] < 540
                  and STATUS_BAR_BOTTOM < item["box"][1] < end]
    # The hint ranks locations only. There is no similarity threshold, equality
    # requirement or second OCR; the saved nickname remains the first reading.
    name = max(candidates, key=lambda item: (SequenceMatcher(None, compact(hint), compact(item["text"])).ratio(),
                                           -item["box"][1]), default=None)
    if name is None:
        raise ValueError("Author profile name region is missing")
    return name


def label_values(lines):
    """Pair same-line, inline and wrapped values until the next label."""
    rows = reading_rows(lines)
    height = line_height(lines)
    anchors = []
    for index, row in enumerate(rows):
        for name, pattern in LABELS.items():
            match = re.match(r"^" + pattern + r"\s*[:：]?\s*", row["text"], re.I)
            if match:
                anchors.append((index, name, row["text"][match.end():].strip()))
    values = {}
    for offset, (index, name, inline) in enumerate(anchors):
        # This section title is repeated as the label on the following value row.
        if name == "certification" and not inline and any(other[1] == name for other in anchors[offset + 1:]):
            continue
        if name in values:
            raise ValueError("Basic information labels are ambiguous")
        end = anchors[offset + 1][0] if offset + 1 < len(anchors) else len(rows)
        parts = [inline] if inline else []
        previous = rows[index]
        value_x = None
        for row in rows[index + 1:end]:
            if row["top"] - previous["bottom"] > height * 1.5:
                break
            if value_x is not None and abs(row["left"] - value_x) > height:
                break
            if re.search(r"基础信息|取消|发表评论|^\S{2,8}[:：]", row["text"]):
                break
            parts.append(row["text"].strip())
            value_x, previous = row["left"], row
        values[name] = "".join(parts) or None
    return values


def basic_info_ready(lines):
    return (any(re.fullmatch(r"基础信息", item["text"].strip()) for item in lines)
            and "channel" in label_values(lines))


def supplemental_roi(lines, label, height, author=None):
    """Return a field group to replace; None means no reliable regional anchor."""
    if label in ("video-details", "expanded-description") and author:
        try:
            region = work_region(lines)
        except ValueError:
            return None
        missing_metadata = not all(metadata_values(region).values())
        try:
            video_values(lines, author)
            missing_body = False
        except ValueError:
            missing_body = True
        top = region["heading"]["bottom"] if missing_metadata else region["header"]["bottom"]
        if not missing_body and region["body"]:
            end = region["body"][0]["top"]
        else:
            end = region["boundary"]["bottom"] if region["boundary"] else height
        return [0, int(top), 720, max(1, int(end - top))]
    if label == "author-basic-info" and basic_info_ready(lines):
        values = label_values(lines)
        missing = [name for name in values if not values[name]]
        if missing:
            rows = reading_rows(lines)
            absent = [row for row in rows if any(re.match("^" + LABELS[name], row["text"], re.I) for name in missing)]
            row, last = absent[0], absent[-1]
            end = next((other["top"] for other in rows if other["top"] > last["bottom"]
                        and any(re.match("^" + pattern, other["text"]) for pattern in LABELS.values())), height)
            top = max(0, int(row["top"] - line_height(lines) / 2))
            return [0, top, 720, max(1, int(end - top))]
    if label == "author-profile" and author:
        try:
            name = profile_name(lines, author)
        except ValueError:
            return None
        controls = [item for item in lines if re.fullmatch(r"[√✓]?\s*(?:已关注|申请关注|关注|私信|客服)", item["text"])]
        if name and len(controls) >= 2:
            top, end = bottom(name), min(item["box"][1] for item in controls)
            if end > top:
                return [0, int(top), 720, int(end - top)]
    return None


def replace_region(lines, recognized, roi):
    """Never mix old and new observations of the same field region."""
    x, y, width, height = roi
    retained = [item for item in lines if bottom(item) <= y or item["box"][1] >= y + height
                or item["box"][0] + item["box"][2] <= x or item["box"][0] >= x + width]
    return retained + recognized
