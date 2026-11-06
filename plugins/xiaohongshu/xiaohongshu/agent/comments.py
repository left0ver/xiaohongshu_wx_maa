"""Read image-note comments with bounded scrolling and verified profile returns."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

from ui import PageMismatch, bounds


@dataclass
class Comment:
    payload: dict
    name_node: object
    box: tuple
    is_reply: bool = False
    requires_parent_match: bool = False
    body_node: object = None
    like_node: object = None
    like_icon_node: object = None
    like_count_text: str = ""
    liked: bool = False

    @property
    def key(self):
        return hashlib.sha256(json.dumps(self.payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def video_comment_drawer(page):
    if any(n.get("text", "").startswith("小红书号：") for n in page.nodes):
        raise PageMismatch("Profile still covers the comments")
    if any(n.get("resource-id", "").endswith("/nickNameTV") for n in page.nodes):
        raise PageMismatch("Image note is not a video comment drawer")
    headers = [n for n in page.nodes
               if re.fullmatch(r"共\s*\d+\s*条评论", n.get("text", "")) and bounds(n)[1] > 200]
    if len(headers) != 1:
        raise PageMismatch("Not a loaded video comment drawer")
    header = headers[0]
    hbox = bounds(header)
    composers = [n for n in page.nodes
                 if n.get("resource-id", "").endswith("/commentLayout")
                 or n.get("text") == "留下你的想法吧"
                 or n.get("content-desc") == "评论框"]
    if not composers:
        raise PageMismatch("Not a loaded video comment drawer")
    bottom = max(bounds(n)[1] for n in composers)
    lists = [n for n in page.nodes
             if n.get("class") == "androidx.recyclerview.widget.RecyclerView"
             and bounds(n)[1] >= hbox[3] and bounds(n)[3] <= bottom]
    if len(lists) != 1:
        raise PageMismatch("Not a loaded video comment drawer")
    listing_nodes = set(lists[0].iter("node"))
    closes = [n for n in page.nodes
              if n not in listing_nodes
              and n.get("class") == "android.widget.ImageView" and n.get("clickable") == "true"
              and bounds(n)[1] >= hbox[1] - 120 and bounds(n)[3] <= hbox[3] + 60 and bounds(n)[0] > hbox[2]]
    if len(closes) != 1:
        raise PageMismatch("Not a loaded video comment drawer")
    return header, closes[0], lists[0]


def is_video_comment_drawer(page):
    try:
        video_comment_drawer(page)
        return True
    except PageMismatch:
        return False


def comment_section(page):
    if any(n.get("text", "").startswith("小红书号：") for n in page.nodes):
        raise PageMismatch("Profile still covers the comments")
    if not any(n.get("resource-id", "").endswith("/nickNameTV") for n in page.nodes):
        try:
            _, _, listing = video_comment_drawer(page)
            return "", listing
        except PageMismatch:
            pass
    author = page.unique(resource="nickNameTV").get("text", "").strip()
    share = page.unique(resource="moreOperateIV")
    comment_bar = page.unique(description="评论框")
    bottom = bounds(comment_bar)[1]
    max_bottom = bounds(comment_bar)[3] + 40
    picture_nodes = {
        sub
        for n in page.nodes
        if re.match(r"图片,第\d+张,共\d+张", n.get("content-desc", ""))
        for sub in n.iter("node")
    }
    lists = [n for n in page.nodes if n.get("class") == "androidx.recyclerview.widget.RecyclerView"
             and n not in picture_nodes
             and bounds(n)[1] >= bounds(share)[3] and bounds(n)[1] < bottom and bounds(n)[3] <= max_bottom]
    if not author or len(lists) != 1:
        raise PageMismatch("Not a loaded image-note comment section")
    return author, lists[0]


def visible_comments(page):
    author, listing = comment_section(page)
    headers = [n for n in page.nodes if re.fullmatch(r"共\s*\d+\s*条评论", n.get("text", ""))]
    _, top, _, bottom = bounds(listing)
    if author:
        bottom = min(bottom, bounds(page.unique(description="评论框"))[1])
    if len(headers) == 1:
        top = max(top, bounds(headers[0])[3])
    parents = {child: parent for parent in page.root.iter() for child in parent}
    comments = []
    unknown_parent = False
    for meta in listing.iter("node"):
        match = re.fullmatch(r"((?:刚刚|今天|昨天|前天|\d)[^\n]*?)(?:\s+(\S+))?\s+回复(?:\s+翻译)?", meta.get("text", "").strip())
        if not match:
            continue
        footer = parents.get(meta)
        panel = parents.get(footer)
        if panel is None or len(panel) < 3:
            continue
        row = panel
        while parents.get(row) is not listing and row in parents:
            row = parents[row]
        is_reply = bounds(row)[0] > bounds(listing)[0]
        names = [n for n in panel[0].iter("node") if n.get("class") == "android.widget.TextView"
                 and n.get("clickable") == "true" and n.get("text", "").strip()]
        bodies = [n for n in panel if n.get("class") == "android.widget.TextView" and n.get("text", "").strip()]
        box = bounds(panel)
        if len(names) != 1 or len(bodies) != 1 or box[1] < top or box[3] > bottom:
            if not is_reply:
                unknown_parent = True
            continue
        body = bodies[0].get("text", "").strip()
        if body.endswith(("…", "...", "展开")):
            if not is_reply:
                unknown_parent = True
            continue
        like_boxes = [
            n for n in footer
            if n.get("class") == "android.widget.LinearLayout"
            and n.get("clickable") == "true"
            and bounds(n)[0] >= bounds(meta)[2] - 20
        ]
        like_node = like_boxes[0] if len(like_boxes) == 1 else None
        like_icons = (
            [n for n in like_node if n.get("class") == "android.widget.ImageView"]
            if like_node is not None else []
        )
        like_icon_node = like_icons[0] if len(like_icons) == 1 else None
        like_texts = (
            [n.get("text", "").strip() for n in like_node if n.get("class") == "android.widget.TextView"]
            if like_node is not None else []
        )
        like_count_text = like_texts[0] if like_texts else ""
        liked = bool(
            (like_node is not None and like_node.get("selected") == "true")
            or (like_icon_node is not None and like_icon_node.get("selected") == "true")
        )
        # shortcut: collect the text portion only; media attachments need a separate collector.
        comments.append(Comment(
            dict(nickname=names[0].get("text").strip(), content=body,
                 commentTime=match[1], ipLocation=match[2] or ""),
            names[0],
            box,
            is_reply,
            is_reply and unknown_parent,
            body_node=bodies[0],
            like_node=like_node,
            like_icon_node=like_icon_node,
            like_count_text=like_count_text,
            liked=liked,
        ))
        unknown_parent = False
    if not comments and not empty_comments(page):
        raise PageMismatch("No complete visible text comments")
    return author, sorted(comments, key=lambda c: (c.box[1], c.box[0]))


def link_comments(comments, previous):
    linked = {}
    parent_key = None
    threads = {}
    for prev in previous.values():
        thread_key = prev.get("parentCommentKey") or prev.get("key")
        if thread_key and prev.get("key"):
            threads.setdefault(thread_key, {})[prev["key"]] = prev
    for comment in comments:
        if comment.key in linked:
            raise PageMismatch("Indistinguishable comments; refusing an ambiguous association")
        if not comment.is_reply:
            parent_key = comment.key
        elif parent_key is None or comment.requires_parent_match:
            parent_key = previous.get(comment.key, {}).get("parentCommentKey")
            if not parent_key:
                raise PageMismatch("Reply parent is unknown; reopen the top of the comment section")
        parent = parent_key if comment.is_reply else None
        key = hashlib.sha256((parent + comment.key).encode()).hexdigest() if parent else comment.key
        body = comment.payload["content"]
        target = re.match(r"回复[ \u00a0]+([^：\n]+)：", body) if comment.is_reply else None
        # The UI exposes display text, not span boundaries or a target user ID.
        nickname = (target[1].strip() or None) if target and body.count("：") == 1 else None
        thread_key = parent or key
        thread_items = threads.setdefault(thread_key, {})
        reply_to_key = None
        reply_to_id = None
        if nickname and parent:
            candidates = [r for k, r in thread_items.items() if k != key and r.get("nickname") == nickname]
            if len(candidates) == 1:
                reply_to_key = candidates[0]["key"]
                reply_to_id = candidates[0].get("authorDouyinId")
            elif not candidates and previous.get(comment.key, {}).get("parentCommentKey") == parent:
                reply_to_key = previous[comment.key].get("replyToCommentKey")
                reply_to_id = previous[comment.key].get("replyToDouyinId")
        record = dict(
            key=key,
            parentCommentKey=parent,
            replyToNickname=nickname,
            replyToCommentKey=reply_to_key,
            replyToDouyinId=reply_to_id,
            **comment.payload,
        )
        linked[comment.key] = record
        thread_items[key] = record
    return linked


def resolve_reply_targets(records, profiles=()):
    items = list(records.values()) if isinstance(records, dict) else list(records)
    profile_by_key = {
        p["commentKey"]: p["douyinId"]
        for p in profiles
        if p.get("profileVisited") and p.get("commentKey") and p.get("douyinId")
    }
    for index, record in enumerate(items):
        parent = record.get("parentCommentKey")
        target_name = record.get("replyToNickname")
        if not parent or not target_name:
            record["replyToCommentKey"] = None
            record["replyToDouyinId"] = None
            continue
        preceding = [
            c for c in items[:index]
            if (c.get("parentCommentKey") or c.get("key")) == parent
            and c.get("nickname") == target_name
            and c.get("key") != record.get("key")
        ]
        if not preceding:
            preceding = [
                c for c in items
                if (c.get("parentCommentKey") or c.get("key")) == parent
                and c.get("nickname") == target_name
                and c.get("key") != record.get("key")
            ]
        unique_keys = list(dict.fromkeys(c["key"] for c in preceding))
        if len(unique_keys) == 1:
            record["replyToCommentKey"] = unique_keys[0]
            record["replyToDouyinId"] = profile_by_key.get(unique_keys[0])
        elif len(unique_keys) > 1:
            record["replyToCommentKey"] = None
            ids = {profile_by_key.get(k) for k in unique_keys}
            record["replyToDouyinId"] = next(iter(ids)) if len(ids) == 1 and None not in ids else None
        else:
            record["replyToCommentKey"] = None
            record["replyToDouyinId"] = None
    return records


def folded_comments_control(page):
    _, listing = comment_section(page)
    for row in listing:
        texts = {n.get("text") for n in row.iter("node")}
        if (row.get("clickable") == "true" and "查看被折叠的评论" in texts
                and "被折叠评论包含疑似广告、不友好或无意义内容" in texts
                and bounds(row)[1] >= bounds(listing)[1] and bounds(row)[3] <= bounds(listing)[3]):
            return row
    return None


def comment_end(page):
    _, listing = comment_section(page)
    if not len(listing) or folded_comments_control(page) is not None:
        return False
    for node in listing[-1].iter("node"):
        if (node.get("class") == "android.widget.FrameLayout" and node.get("content-desc") == "已到底"
                and any(child.get("text") == "- 到底了 -" for child in node)
                and bounds(node)[1] >= bounds(listing)[1] and bounds(node)[3] <= bounds(listing)[3]):
            return True
    return False


def comment_count(page):
    counts = [re.fullmatch(r"评论\s+(\d+)", n.get("content-desc", "")) for n in page.nodes]
    counts = [int(match[1]) for match in counts if match]
    if len(counts) == 1:
        return counts[0]
    if len(counts) > 1:
        return None
    if is_video_comment_drawer(page):
        header, _, _ = video_comment_drawer(page)
        match = re.fullmatch(r"共\s*(\d+)\s*条评论", header.get("text", ""))
        if match:
            return int(match[1])
    return None


def empty_comments(page):
    if comment_count(page) != 0:
        return False
    if is_video_comment_drawer(page):
        header, _, listing = video_comment_drawer(page)
        top = bounds(header)[3]
        bottom = bounds(listing)[3]
    else:
        top = bounds(page.unique(resource="moreOperateIV"))[3]
        bottom = bounds(page.unique(description="评论框"))[1]
    parents = {child: parent for parent in page.root.iter() for child in parent}
    for label in page.nodes:
        if label.get("text") != "还没有评论哦":
            continue
        picture_group = parents.get(label)
        panel = parents.get(picture_group)
        if (panel is None or picture_group.get("class") != "android.widget.LinearLayout"
                or not any(n.get("class") == "android.widget.ImageView" for n in picture_group)):
            continue
        buttons = [n for n in panel.iter("node") if n.get("text") == "抢首评"]
        if (len(buttons) == 1 and bounds(label)[1] >= top and bounds(label)[3] <= bottom
                and bounds(buttons[0])[1] >= bounds(label)[3] and bounds(buttons[0])[3] <= bottom):
            return True
    return False


def reply_controls(page, comments):
    _, listing = comment_section(page)
    _, top, _, bottom = bounds(listing)
    controls = []
    for node in listing.iter("node"):
        if not re.fullmatch(r"展开\s*\d+\s*条回复|展开更多回复", node.get("text", "")):
            continue
        box = bounds(node)
        preceding = [c for c in comments if c.box[3] <= box[1]]
        if box[1] >= top and box[3] <= bottom and preceding:
            controls.append((preceding[-1].key, node))
    return controls


def selected_comment(page, key):
    matches = [c for c in visible_comments(page)[1] if c.key == key]
    if len(matches) != 1:
        raise PageMismatch("Selected comment changed or is ambiguous")
    return matches[0]


def _current_page(device):
    if getattr(device, "has_vision", False) is True:
        return device.page(reuse_if_stable=True)
    return device.page()


def settled_comments(device, expected_author=None):
    previous = None

    def ready(page):
        nonlocal previous
        _, listing = comment_section(page)
        author, comments = visible_comments(page)
        if expected_author is not None and author and author != expected_author:
            raise PageMismatch("Work author changed during comment scrolling")
        resolved_author = author or expected_author or ""
        if (getattr(device, "has_vision", False) is True
                and not any(n.get("text") == "加载中" for n in page.nodes)
                and device.verify_roi_stable(bounds(listing))):
            return resolved_author, comments
        signature = (resolved_author, [(c.key, c.box) for c in comments], comment_end(page),
                     folded_comments_control(page) is not None, empty_comments(page))
        if signature != previous:
            previous = signature
            raise PageMismatch("Comment layout is still moving")
        return resolved_author, comments

    return device.wait(ready, timeout=90)


def _profile_works_count(page):
    counts = []
    for node in page.nodes:
        for attr in ("text", "content-desc"):
            value = node.get(attr, "").strip()
            if not value:
                continue
            match = (re.fullmatch(r"(?:已选定)?笔记[\s·:：]+(\d[\d.万亿]*)", value)
                     or re.fullmatch(r"共\s*(\d[\d.万亿]*)\s*篇笔记", value))
            if match:
                counts.append(match[1])
    unique = list(dict.fromkeys(counts))
    if len(unique) > 1:
        raise PageMismatch("Profile works count is not unambiguous")
    if len(unique) == 1:
        return unique[0]
    if any(n.get("text", "").strip() in {"还没有发布过笔记", "暂无笔记", "还没有发布笔记哦"} for n in page.nodes):
        return "0"
    return None


def commenter_profile(page, expected_name):
    red_id = page.author_identity(expected_name)
    followers = [re.fullmatch(r"([\d.万亿]+)粉丝", n.get("content-desc", "")) for n in page.nodes]
    followers = [m[1] for m in followers if m]
    if len(followers) > 1:
        raise PageMismatch("Profile follower count is not unambiguous")
    has_notes_tab = any(
        n.get("content-desc", "").startswith("已选定笔记") or n.get("text", "").strip().startswith("笔记")
        for n in page.nodes
    )
    if not followers and not has_notes_tab:
        raise PageMismatch("Profile follower count is not unambiguous")
    works = _profile_works_count(page)
    return dict(
        nickname=expected_name,
        douyinId=red_id,
        followersText=followers[0] if followers else None,
        worksCountText=works,
        profileVisited=True,
    )


def open_video_comment_drawer(device):
    page = _current_page(device)
    video_note = page.note(allow_incomplete=True)
    if video_note.get("noteType") != "VIDEO":
        raise PageMismatch("Not a loaded video note")

    def comment_button(current):
        nodes = [n for n in current.nodes if re.fullmatch(r"评论\s+[1-9]\d*", n.get("content-desc", ""))]
        if len(nodes) != 1:
            raise PageMismatch("No unambiguous nonempty video comment button")
        return nodes[0]

    device.wait(comment_button, timeout=20)
    device.click(comment_button)
    if getattr(device, "has_vision", False) is True:
        device.wait_visual_settle(timeout=0.8)
    device.wait(video_comment_drawer, timeout=90)
    return video_note


def close_video_comment_drawer(device, expected_note=None):
    device.click(lambda current: video_comment_drawer(current)[1])
    if getattr(device, "has_vision", False) is True:
        device.wait_visual_settle(timeout=0.8)

    def returned(current):
        if is_video_comment_drawer(current):
            raise PageMismatch("Video comment drawer is still open")
        if any(n.get("text", "").startswith("小红书号：")
               or n.get("content-desc") in ("复制链接", "个人页头图")
               or n.get("content-desc", "").startswith("头像,")
               for n in current.nodes):
            raise PageMismatch("A profile or share panel still covers the video")
        actual = current.note(allow_incomplete=True)
        if actual.get("noteType") != "VIDEO":
            raise PageMismatch("Did not return to a video note")
        if expected_note is not None:
            for key in ("noteType", "authorNickname", "title", "description"):
                if expected_note.get(key) != actual.get(key):
                    raise PageMismatch("Returned to a different video work")
        return actual

    try:
        return device.wait(returned, timeout=20)
    except PageMismatch:
        if is_video_comment_drawer(_current_page(device)):
            device.back()
            if getattr(device, "has_vision", False) is True:
                device.wait_visual_settle(timeout=0.8)
            return device.wait(returned, timeout=20)
        raise


def inspect_comments(device, data_dir, max_pages=10, max_profiles=5):
    if type(max_pages) is not int or not 1 <= max_pages <= 100:
        raise ValueError("Comment page limit must be between 1 and 100")
    if type(max_profiles) is not int or not 0 <= max_profiles <= 100:
        raise ValueError("Comment profile limit must be between 0 and 100")
    started = previous = time.monotonic()
    timings = {}

    def mark(stage):
        nonlocal previous
        now = time.monotonic()
        timings[stage] = round((now - previous) * 1000)
        print(json.dumps({"stage": stage, "elapsedMs": timings[stage]}), flush=True)
        previous = now

    helper = Path(__file__).with_name("ui-dump.dex")
    output = device.command("env", "CLASSPATH=" + str(helper), "app_process", "/system/bin", "xhs.UiDump", "--resume-xhs")
    if output.strip() != "resumed":
        raise PageMismatch("Could not resume the existing Xiaohongshu page")
    page = device.wait(lambda current: current)
    video_note = None
    is_video_drawer = is_video_comment_drawer(page)
    if not is_video_drawer and any(n.get("resource-id", "").endswith("/matrixNickNameView") for n in page.nodes):
        video_note = open_video_comment_drawer(device)
        is_video_drawer = True
        mark("open_comment_drawer")
    elif not is_video_drawer:
        try:
            visible_comments(page)
        except PageMismatch:
            page.unique(resource="nickNameTV")
            page.unique(resource="moreOperateIV")
            page.unique(description="评论框")

            def comment_button(current):
                nodes = [n for n in current.nodes if re.fullmatch(r"评论\s+[1-9]\d*", n.get("content-desc", ""))]
                if len(nodes) != 1:
                    raise PageMismatch("No unambiguous nonempty comment section")
                return nodes[0]

            headers = [n for n in page.nodes if re.fullmatch(r"共\s*\d+\s*条评论", n.get("text", ""))]
            if any(n.get("content-desc", "").startswith("图片,第") for n in page.nodes):
                if comment_count(page) == 0:
                    _, listing = comment_section(page)
                    left, top, right, bottom = bounds(listing)
                    x = str((left + right) // 2)
                    height = bottom - top
                    device.command("input", "swipe", x, str(top + height * 3 // 4),
                                   x, str(top + height // 3), "1000")
                else:
                    device.wait(comment_button, timeout=90)
                    device.click(comment_button)
            elif not headers:
                raise PageMismatch("Open an image note or its loaded comment section first")
    expected_author = video_note["authorNickname"] if video_note else None
    author, comments = settled_comments(device, expected_author)
    for _ in range(10):
        if not comments or not comments[0].is_reply:
            break
        current_author, listing = comment_section(_current_page(device))
        if current_author and current_author != author:
            raise PageMismatch("Work changed while restoring reply context")
        left, top, right, bottom = bounds(listing)
        x = str((left + right) // 2)
        height = bottom - top
        device.command("input", "swipe", x, str(top + height // 3),
                       x, str(top + height // 3 + height // 5), "1000")
        _, comments = settled_comments(device, author or None)
    if comments and comments[0].is_reply:
        raise PageMismatch("Could not restore the parent comment within ten scrolls")
    mark("read_comments")
    expected_count = comment_count(_current_page(device))
    collected = {}
    profiles = []
    skipped_profiles = []
    visited = set()
    previous_links = {}
    expanded = set()
    folded_expansions = 0
    end_reason = "page_limit"

    def position(items):
        return [(c.key, c.box) for c in items]

    def same_position(current):
        returned_author, returned = visible_comments(current)
        if is_video_drawer:
            if not is_video_comment_drawer(current) or comment_count(current) != expected_count:
                raise PageMismatch("Did not return to the original comment drawer and position")
        elif returned_author != author:
            raise PageMismatch("Did not return to the original comment section and position")
        if position(returned) != position(comments):
            raise PageMismatch("Did not return to the original comment section and position")
        return current

    for page_index in range(max_pages):
        links = link_comments(comments, previous_links)
        current = same_position(_current_page(device))
        for anchor, control in reply_controls(current, comments):
            expansion = (links[anchor]["key"], control.get("text"))
            if expansion in expanded:
                continue

            def expand_target(current):
                same_position(current)
                matches = [node for key, node in reply_controls(current, visible_comments(current)[1])
                           if key == anchor and node.get("text") == control.get("text")]
                if len(matches) != 1:
                    raise PageMismatch("Reply expansion target changed")
                return matches[0]

            before = {c.key for c in comments}
            device.click(expand_target)
            if getattr(device, "has_vision", False) is True:
                _, listing = comment_section(_current_page(device))
                device.wait_visual_settle(roi=bounds(listing), timeout=0.8)

            def expanded_comments(current):
                current_author, following = visible_comments(current)
                if current_author and current_author != author:
                    raise PageMismatch("Work changed while expanding replies")
                loading = any(n.get("text") == "加载中" for n in current.nodes)
                unchanged = any(key == anchor and node.get("text") == control.get("text")
                                for key, node in reply_controls(current, following))
                if loading or (unchanged and not any(c.key not in before for c in following)):
                    raise PageMismatch("Waiting for newly expanded replies")
                return following

            device.wait(expanded_comments, timeout=90)
            _, following = settled_comments(device, author or None)
            collected.update({record["key"]: record for record in links.values()})
            comments = following
            links = link_comments(comments, links)
            expanded.add(expansion)
            # A fresh page may move other controls; reconsider them on the next iteration.
            break

        for selected in comments:
            record = links[selected.key]
            collected.setdefault(record["key"], record)

        target_names = {
            r["replyToNickname"]
            for r in list(collected.values()) + list(links.values())
            if r.get("replyToNickname")
        }
        ordered_candidates = sorted(
            comments,
            key=lambda c: (0 if c.payload["nickname"] in target_names else 1, c.box[1], c.box[0]),
        )
        for selected in ordered_candidates:
            record = links[selected.key]
            if record["key"] in visited or len(profiles) >= max_profiles:
                continue

            def profile_target(current):
                same_position(current)
                return selected_comment(current, selected.key).name_node

            try:
                device.click(profile_target)
            except PageMismatch as error:
                current = _current_page(device)
                current_author, current_comments = visible_comments(current)
                if is_video_drawer:
                    if not is_video_comment_drawer(current) or comment_count(current) != expected_count:
                        raise
                elif current_author != author:
                    raise
                if any(c.key == selected.key for c in current_comments):
                    raise
                visited.add(record["key"])
                skipped_profiles.append(dict(
                    commentKey=record["key"],
                    nickname=selected.payload["nickname"],
                    profileVisited=False,
                    reason="comment_disappeared",
                    detail=str(error),
                ))
                comments = current_comments
                links = link_comments(comments, previous_links)
                continue

            if getattr(device, "has_vision", False) is True:
                device.wait_visual_settle(timeout=0.8)
            try:
                profile = device.wait(
                    lambda current: commenter_profile(current, selected.payload["nickname"]),
                    timeout=20,
                )
            except PageMismatch as error:
                visited.add(record["key"])
                reason = (
                    "identity_unconfirmed"
                    if "identity does not match" in str(error) or "transition has not completed" in str(error)
                    else "profile_inaccessible"
                )
                skipped_profiles.append(dict(
                    commentKey=record["key"],
                    nickname=selected.payload["nickname"],
                    profileVisited=False,
                    reason=reason,
                    detail=str(error),
                ))
                try:
                    same_position(_current_page(device))
                except PageMismatch:
                    device.back()
                    if getattr(device, "has_vision", False) is True:
                        device.wait_visual_settle(timeout=0.8)
                    device.wait(same_position, timeout=90)
                continue

            device.back()
            if getattr(device, "has_vision", False) is True:
                device.wait_visual_settle(timeout=0.8)
            device.wait(same_position, timeout=90)
            profiles.append(dict(commentKey=record["key"], **profile))
            visited.add(record["key"])
            mark("profile_return_" + str(len(profiles)))

        current = same_position(_current_page(device))
        if empty_comments(current):
            end_reason = "empty_comments"
            break
        if comment_end(current):
            end_reason = "end_of_list"
            break
        if page_index + 1 == max_pages:
            break
        if folded_comments_control(current) is not None:
            def folded_target(current):
                same_position(current)
                control = folded_comments_control(current)
                if control is None:
                    raise PageMismatch("Folded comment control changed")
                return control

            device.click(folded_target)
            if getattr(device, "has_vision", False) is True:
                _, listing = comment_section(_current_page(device))
                device.wait_visual_settle(roi=bounds(listing), timeout=0.8)

            def unfolded(current):
                current_author, _ = comment_section(current)
                if (current_author and current_author != author) or folded_comments_control(current) is not None:
                    raise PageMismatch("Waiting for folded comments")
                if not any(n.get("text") == "以下是被折叠的评论" for n in current.nodes):
                    raise PageMismatch("Folded comment section has not appeared")
                return current

            device.wait(unfolded, timeout=90)
            _, following = settled_comments(device, author or None)
            if not ({c.key for c in following} & {c.key for c in comments}):
                raise PageMismatch("Lost comment overlap after unfolding comments")
            comments, previous_links = following, links
            folded_expansions += 1
            continue
        _, listing = comment_section(current)
        left, top, right, bottom = bounds(listing)
        x = str((left + right) // 2)
        height = bottom - top
        device.command("input", "swipe", x, str(top + height * 3 // 4),
                       x, str(top + height * 3 // 4 - height // 5), "1000")
        _, following = settled_comments(device, author or None)
        if not ({c.key for c in following} & {c.key for c in comments}):
            raise PageMismatch("Lost comment overlap after scrolling; refusing to join unverified pages")
        if position(following) == position(comments):
            current = same_position(_current_page(device))
            if folded_comments_control(current) is None:
                end_reason = "end_of_list" if comment_end(current) else "no_progress"
                break
        comments = following
        previous_links = links
        mark("comment_page_" + str(page_index + 2))

    resolve_reply_targets(collected, profiles)
    complete = (end_reason in {"end_of_list", "empty_comments"} and expected_count is not None
                and expected_count == comment_count(current) == len(collected)
                and not reply_controls(current, comments))
    if is_video_drawer:
        returned_video = close_video_comment_drawer(device, video_note)
        if not author:
            author = returned_video.get("authorNickname", "")
        mark("close_comment_drawer")
    scope = "current_video_note_comments" if is_video_drawer else "current_image_note_comments"
    report = dict(status="comment_profiles_verified_locally", scope=scope,
                  sourceAuthor=author, comments=list(collected.values()), commenterProfiles=profiles,
                  skippedProfiles=skipped_profiles,
                  pagesRead=page_index + 1, endReason=end_reason, complete=complete, timingsMs=timings,
                  expectedCommentCount=expected_count, replyExpansions=len(expanded),
                  foldedCommentExpansions=folded_expansions,
                  capturedAtUnixMs=time.time_ns() // 1_000_000,
                  elapsedMs=round((time.monotonic() - started) * 1000), uploaded=False)
    if is_video_drawer:
        report["drawerClosed"] = True
        report["returnedToVideo"] = True
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / ("comments-" + uuid.uuid4().hex + ".json")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    temporary.replace(path)
    return report
