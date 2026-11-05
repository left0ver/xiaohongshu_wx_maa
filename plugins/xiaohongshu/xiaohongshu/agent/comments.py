"""Read visible image-note comments and verify one commenter's profile round trip."""

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

    @property
    def key(self):
        return hashlib.sha256(json.dumps(self.payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def visible_comments(page):
    if any(n.get("text", "").startswith("小红书号：") for n in page.nodes):
        raise PageMismatch("Profile still covers the comments")
    author = page.unique(resource="nickNameTV").get("text", "").strip()
    headers = [n for n in page.nodes if re.fullmatch(r"共\s*\d+\s*条评论", n.get("text", ""))]
    if not author or len(headers) != 1:
        raise PageMismatch("Not a loaded image-note comment section")
    top = bounds(headers[0])[3]
    bottom = bounds(page.unique(description="评论框"))[1]
    parents = {child: parent for parent in page.root.iter() for child in parent}
    comments = []
    for meta in page.nodes:
        match = re.fullmatch(r"((?:刚刚|今天|昨天|前天|\d)[^\n]*?)(?:\s+(\S+))?\s+回复", meta.get("text", "").strip())
        if not match:
            continue
        footer = parents.get(meta)
        panel = parents.get(footer)
        if panel is None or len(panel) < 3:
            continue
        names = [n for n in panel[0].iter("node") if n.get("class") == "android.widget.TextView"
                 and n.get("clickable") == "true" and n.get("text", "").strip()]
        bodies = [n for n in panel if n.get("class") == "android.widget.TextView" and n.get("text", "").strip()]
        box = bounds(panel)
        if len(names) != 1 or len(bodies) != 1 or box[1] < top or box[3] > bottom:
            continue
        body = bodies[0].get("text", "").strip()
        if body.endswith(("…", "...", "展开")):
            continue
        # shortcut: visible text comments only; add reply relationships and media in the next collection batch.
        comments.append(Comment(dict(nickname=names[0].get("text").strip(), content=body,
                                     commentTime=match[1], ipLocation=match[2] or ""), names[0], box))
    if not comments:
        raise PageMismatch("No complete visible text comments")
    return author, sorted(comments, key=lambda c: (c.box[1], c.box[0]))


def selected_comment(page, key):
    matches = [c for c in visible_comments(page)[1] if c.key == key]
    if len(matches) != 1:
        raise PageMismatch("Selected comment changed or is ambiguous")
    return matches[0]


def commenter_profile(page, expected_name):
    red_id = page.author_identity(expected_name)
    followers = [re.fullmatch(r"([\d.万亿]+)粉丝", n.get("content-desc", "")) for n in page.nodes]
    followers = [m[1] for m in followers if m]
    if len(followers) != 1:
        raise PageMismatch("Profile follower count is not unambiguous")
    return dict(nickname=expected_name, douyinId=red_id, followersText=followers[0], profileVisited=True)


def inspect_comments(device, data_dir):
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

        device.click(comment_button)
    author, comments = device.wait(visible_comments)
    keys = [c.key for c in comments]
    selected = next((c for c in comments if keys.count(c.key) == 1), None)
    if selected is None:
        raise PageMismatch("All visible comments are ambiguous")
    mark("read_comments")
    device.click(lambda current: selected_comment(current, selected.key).name_node)
    profile = device.wait(lambda current: commenter_profile(current, selected.payload["nickname"]))
    mark("open_commenter_profile")
    device.back()
    returned_author, returned = device.wait(visible_comments)
    if returned_author != author or [(c.key, c.box) for c in returned] != [(c.key, c.box) for c in comments]:
        raise PageMismatch("Did not return to the original comment section and position")
    mark("return_to_comments")
    report = dict(status="comment_profile_verified_locally", scope="current_image_note_comments",
                  sourceAuthor=author, comments=[dict(key=c.key, **c.payload) for c in comments],
                  selectedCommentKey=selected.key, commenterProfile=profile, timingsMs=timings,
                  capturedAtUnixMs=time.time_ns() // 1_000_000,
                  elapsedMs=round((time.monotonic() - started) * 1000), uploaded=False)
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / ("comments-" + uuid.uuid4().hex + ".json")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    temporary.replace(path)
    return report
