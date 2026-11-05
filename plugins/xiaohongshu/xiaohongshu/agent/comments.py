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

    @property
    def key(self):
        return hashlib.sha256(json.dumps(self.payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def comment_section(page):
    if any(n.get("text", "").startswith("小红书号：") for n in page.nodes):
        raise PageMismatch("Profile still covers the comments")
    author = page.unique(resource="nickNameTV").get("text", "").strip()
    share = page.unique(resource="moreOperateIV")
    bottom = bounds(page.unique(description="评论框"))[1]
    lists = [n for n in page.nodes if n.get("class") == "androidx.recyclerview.widget.RecyclerView"
             and bounds(n)[1] >= bounds(share)[3] and bounds(n)[3] <= bottom]
    if not author or len(lists) != 1:
        raise PageMismatch("Not a loaded image-note comment section")
    return author, lists[0]


def visible_comments(page):
    author, listing = comment_section(page)
    headers = [n for n in page.nodes if re.fullmatch(r"共\s*\d+\s*条评论", n.get("text", ""))]
    _, top, _, bottom = bounds(listing)
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
        # shortcut: collect the text portion only; media attachments need a separate collector.
        comments.append(Comment(dict(nickname=names[0].get("text").strip(), content=body,
                                     commentTime=match[1], ipLocation=match[2] or ""), names[0], box,
                                is_reply, is_reply and unknown_parent))
        unknown_parent = False
    if not comments and not empty_comments(page):
        raise PageMismatch("No complete visible text comments")
    return author, sorted(comments, key=lambda c: (c.box[1], c.box[0]))


def link_comments(comments, previous):
    linked = {}
    parent_key = None
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
        linked[comment.key] = dict(key=key, parentCommentKey=parent, replyToNickname=nickname, **comment.payload)
    return linked


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
    return counts[0] if len(counts) == 1 else None


def empty_comments(page):
    if comment_count(page) != 0:
        return False
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


def settled_comments(device, expected_author=None):
    previous = None

    def ready(page):
        nonlocal previous
        author, comments = visible_comments(page)
        if expected_author is not None and author != expected_author:
            raise PageMismatch("Work author changed during comment scrolling")
        signature = (author, [(c.key, c.box) for c in comments], comment_end(page),
                     folded_comments_control(page) is not None, empty_comments(page))
        if signature != previous:
            previous = signature
            raise PageMismatch("Comment layout is still moving")
        return author, comments

    return device.wait(ready, timeout=90)


def commenter_profile(page, expected_name):
    red_id = page.author_identity(expected_name)
    followers = [re.fullmatch(r"([\d.万亿]+)粉丝", n.get("content-desc", "")) for n in page.nodes]
    followers = [m[1] for m in followers if m]
    if len(followers) != 1:
        raise PageMismatch("Profile follower count is not unambiguous")
    return dict(nickname=expected_name, douyinId=red_id, followersText=followers[0], profileVisited=True)


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
    author, comments = settled_comments(device)
    for _ in range(10):
        if not comments or not comments[0].is_reply:
            break
        current_author, listing = comment_section(device.page())
        if current_author != author:
            raise PageMismatch("Work changed while restoring reply context")
        left, top, right, bottom = bounds(listing)
        x = str((left + right) // 2)
        height = bottom - top
        device.command("input", "swipe", x, str(top + height // 3),
                       x, str(top + height // 3 + height // 5), "1000")
        _, comments = settled_comments(device, author)
    if comments and comments[0].is_reply:
        raise PageMismatch("Could not restore the parent comment within ten scrolls")
    mark("read_comments")
    expected_count = comment_count(device.page())
    collected = {}
    profiles = []
    visited = set()
    previous_links = {}
    expanded = set()
    folded_expansions = 0
    end_reason = "page_limit"

    def position(items):
        return [(c.key, c.box) for c in items]

    def same_position(current):
        returned_author, returned = visible_comments(current)
        if returned_author != author or position(returned) != position(comments):
            raise PageMismatch("Did not return to the original comment section and position")
        return current

    for page_index in range(max_pages):
        links = link_comments(comments, previous_links)
        current = same_position(device.page())
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

            def expanded_comments(current):
                current_author, following = visible_comments(current)
                if current_author != author:
                    raise PageMismatch("Work changed while expanding replies")
                loading = any(n.get("text") == "加载中" for n in current.nodes)
                unchanged = any(key == anchor and node.get("text") == control.get("text")
                                for key, node in reply_controls(current, following))
                if loading or (unchanged and not any(c.key not in before for c in following)):
                    raise PageMismatch("Waiting for newly expanded replies")
                return following

            device.wait(expanded_comments, timeout=90)
            _, following = settled_comments(device, author)
            collected.update({record["key"]: record for record in links.values()})
            comments = following
            links = link_comments(comments, links)
            expanded.add(expansion)
            # A fresh page may move other controls; reconsider them on the next iteration.
            break

        for selected in comments:
            record = links[selected.key]
            collected.setdefault(record["key"], record)
            if record["key"] in visited or len(profiles) >= max_profiles:
                continue

            def profile_target(current):
                same_position(current)
                return selected_comment(current, selected.key).name_node

            device.click(profile_target)
            profile = device.wait(lambda current: commenter_profile(current, selected.payload["nickname"]), timeout=90)
            device.back()
            device.wait(same_position, timeout=90)
            profiles.append(dict(commentKey=record["key"], **profile))
            visited.add(record["key"])
            mark("profile_return_" + str(len(profiles)))

        current = same_position(device.page())
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

            def unfolded(current):
                current_author, _ = comment_section(current)
                if current_author != author or folded_comments_control(current) is not None:
                    raise PageMismatch("Waiting for folded comments")
                if not any(n.get("text") == "以下是被折叠的评论" for n in current.nodes):
                    raise PageMismatch("Folded comment section has not appeared")
                return current

            device.wait(unfolded, timeout=90)
            _, following = settled_comments(device, author)
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
        _, following = settled_comments(device, author)
        if not ({c.key for c in following} & {c.key for c in comments}):
            raise PageMismatch("Lost comment overlap after scrolling; refusing to join unverified pages")
        if position(following) == position(comments):
            current = same_position(device.page())
            if folded_comments_control(current) is None:
                end_reason = "end_of_list" if comment_end(current) else "no_progress"
                break
        comments = following
        previous_links = links
        mark("comment_page_" + str(page_index + 2))

    complete = (end_reason in {"end_of_list", "empty_comments"} and expected_count is not None
                and expected_count == comment_count(current) == len(collected)
                and not reply_controls(current, comments))
    report = dict(status="comment_profiles_verified_locally", scope="current_image_note_comments",
                  sourceAuthor=author, comments=list(collected.values()), commenterProfiles=profiles,
                  pagesRead=page_index + 1, endReason=end_reason, complete=complete, timingsMs=timings,
                  expectedCommentCount=expected_count, replyExpansions=len(expanded),
                  foldedCommentExpansions=folded_expansions,
                  capturedAtUnixMs=time.time_ns() // 1_000_000,
                  elapsedMs=round((time.monotonic() - started) * 1000), uploaded=False)
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / ("comments-" + uuid.uuid4().hex + ".json")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    temporary.replace(path)
    return report
