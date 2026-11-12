"""Tasks on one video given by its share link.

WeChat does not open a Channels share link handed to it by another app, and
Channels search treats a pasted link as a keyword. Sending the link to
文件传输助手 and tapping the message opens the video in WeChat's own video page
(FinderShareFeedRelUI, same controls as the feed), so every task here starts
that way. Verified on Hebe-BD00 / WeChat 8.0.80, 2026-11-11.
"""

import json
import re
import time
from pathlib import Path

from collection import author_in_feed, control, feed_video_preview, find, identity
from interactions import (
    PROFILE_SETTLE_SECONDS,
    check_stopped,
    classify_page_state,
    collect_work_action,
    compute_work_key,
    like_comment,
    like_work,
    locate_target_comment,
    post_work_comment,
    reply_to_comment,
    visit_and_touch_author_profile,
)

LINK_RE = re.compile(r"https://(?:weixin\.qq\.com/sph/[A-Za-z0-9_-]{4,64}|channels\.weixin\.qq\.com/[^\s\"'<>]+)")
FILE_HELPER = "文件传输助手"
# Every comment is searched, as far as the drawer scrolls.
ALL_COMMENT_PAGES = 300
TEXT_LIMIT = 500

# action -> (needs a commenter nickname, needs a text)
LINK_ACTIONS = {
    "likeVideo": (False, False),
    "followAuthor": (False, False),
    "messageAuthor": (False, True),
    "commentVideo": (False, True),
    "likeComment": (True, False),
    "replyComment": (True, True),
    "messageCommenter": (True, True),
}


def share_link(text):
    """The first Channels share link inside text the user pasted."""
    match = LINK_RE.search(str(text or ""))
    if not match:
        raise ValueError("视频链接无效：需要 https://weixin.qq.com/sph/… 形式的视频号分享链接")
    return match.group(0)


def validate_link_task(config):
    """Reject a run that cannot do what it asks before the phone is touched."""
    action = config.get("action")
    if action not in LINK_ACTIONS:
        raise ValueError(f"Unsupported link task: {action!r}")
    share_link(config.get("link"))
    needs_nickname, needs_text = LINK_ACTIONS[action]
    if needs_nickname and not str(config.get("nickname") or "").strip():
        raise ValueError("评论者昵称不能为空")
    if needs_text:
        text = str(config.get("text") or "").strip()
        if not text:
            raise ValueError("发送内容不能为空")
        if len(text) > TEXT_LIMIT:
            raise ValueError(f"发送内容不能超过 {TEXT_LIMIT} 个字")


class LinkLedger:
    """Replies, messages and comments sent by link tasks; kept forever, unlike daily quotas."""

    def __init__(self, path):
        self.path = Path(path)
        try:
            self.done = set(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            self.done = set()

    def has(self, key):
        return key in self.done

    def add(self, key):
        self.done.add(key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(sorted(self.done), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)


def _bottom(ui):
    return lambda line: line["box"][1] > ui.height - 180


def open_wechat_home(ui, attempts=10):
    """WeChat's 微信 tab; pages an earlier run left open are closed first."""
    if hasattr(ui, "start_wechat_home"):
        ui.start_wechat_home()
        time.sleep(1.0)
    else:
        ui.start_wechat()
    for attempt in range(attempts):
        check_stopped(ui)
        lines = ui.snapshot(f"link-home-{attempt + 1}")
        tab = find(lines, r"^微信$", _bottom(ui))
        if tab and find(lines, r"^通讯录$", _bottom(ui)):
            ui.click(tab)
            return ui.snapshot("link-home")
        if not lines:
            # WeChat is still drawing its first frame.
            time.sleep(1.0)
            continue
        ui.back()
    raise ValueError("Could not reach the WeChat chat list")


def _edit_box(lines):
    return next((line for line in lines if line.get("selector", {}).get("class", "").endswith("EditText")), None)


# Notices shown instead of the input box when the other side limits messages.
DM_RESTRICTED = r"设置.{0,12}才能发消息|才能(?:给\S+)?发消息|不接收.{0,6}消息|无法发送私信"

# A chat read by OCR has no control classes; its input hint and notices identify it.
CHAT_TEXT = r"可按住\s*转文字|按住\s*说话|仅可发\s*1\s*条消息|^发送$"


def _is_chat(lines):
    return bool(_edit_box(lines) or find(lines, CHAT_TEXT))


def open_file_helper(ui):
    """微信 tab → 搜索 → 文件传输助手 → its chat."""
    found = control(ui.controls_now(), r"^搜索$", lambda line: line["box"][1] < 200 and line["box"][0] > 480)
    ui.click(found or {"text": "搜索", "box": [550, 70, 80, 80]})
    time.sleep(0.8)
    try:
        ui.set_text(FILE_HELPER)
    except Exception:
        # The set-text helper can hang (Hebe-BD00, 2026-11-12); WeChat search
        # also finds the contact by its pinyin.
        if not hasattr(ui, "type_ascii"):
            raise
        ui.type_ascii("wenjianchuanshuzhushou")
    results = ui.snapshot("link-search-typed")
    box = _edit_box(results)
    below = box["box"][1] + box["box"][3] if box else 180
    # The contact comes first; web-search suggestions with the same words follow it.
    target = find(results, rf"^{FILE_HELPER}$",
                  lambda line: line["box"][1] > below and not line.get("selector", {}).get("class", "").endswith("EditText"))
    if not target:
        raise ValueError("文件传输助手 was not found in WeChat search")
    ui.click(target)
    chat = ui.snapshot("link-chat")
    if not _is_chat(chat):
        raise ValueError("The 文件传输助手 chat did not open")
    return chat


def _type(ui, text):
    if hasattr(ui, "type_ascii") and LINK_RE.fullmatch(text):
        ui.type_ascii(text)
    else:
        ui.set_text(text)


def send_chat_text(ui, lines, text, label):
    """Type into the open chat and send; returns the lines after sending."""
    box = _edit_box(lines)
    ui.click(box or {"text": "input", "box": [100, ui.height - 80, 450, 60]})
    _type(ui, text)
    typed = ui.snapshot(f"{label}-typed")
    notice = find(typed, r"^我知道了$")
    if notice and not find(typed, r"^发送$"):
        # The first Channels private message shows a one-time 消息安全保护提醒
        # over the input box; it is acknowledged and the text typed again.
        ui.click(notice)
        lines = ui.snapshot(f"{label}-after-notice")
        box = _edit_box(lines)
        ui.click(box or {"text": "input", "box": [100, ui.height - 80, 450, 60]})
        _type(ui, text)
        typed = ui.snapshot(f"{label}-typed")
    send = find(typed, r"^发送$")
    if not send:
        raise ValueError("The 发送 button did not appear after typing")
    check_stopped(ui)
    ui.click(send)
    # The keyboard opens after sending; it hides the chat's controls and
    # covers the messages, and back would leave the chat if it were closed.
    if hasattr(ui, "hide_keyboard"):
        ui.hide_keyboard()
    return ui.snapshot(f"{label}-sent")


_CONFUSABLE = str.maketrans({"I": "1", "l": "1", "|": "1", "O": "0", "o": "0"})


def _sent_bubble(lines, text):
    """The newest message showing text, never the input box.

    A link read by OCR can be split over two lines and mix up I/l/1 and O/0,
    so the link's code alone, compared loosely, also identifies it.
    """
    lines = [line for line in lines if not line.get("selector", {}).get("class", "").endswith("EditText")]
    bubbles = [line for line in lines if text in line["text"]]
    if not bubbles and LINK_RE.fullmatch(text):
        code = text.rstrip("/").rsplit("/", 1)[-1].translate(_CONFUSABLE)
        bubbles = [line for line in lines
                   if len(code) >= 6 and code in re.sub(r"\s+", "", line["text"]).translate(_CONFUSABLE)]
    return max(bubbles, key=lambda line: line["box"][1], default=None)


def open_video_by_link(ui, link):
    """Open a share link through 文件传输助手; returns the video page lines."""
    open_wechat_home(ui)
    chat = open_file_helper(ui)
    sent = send_chat_text(ui, chat, link, "link-chat")
    bubble = _sent_bubble(sent, link)
    if not bubble:
        raise ValueError("The link message did not appear in 文件传输助手")
    # Earlier runs leave the same link in the chat, so the bubble is tapped where
    # it was just read instead of being looked up again by its text.
    # Only the link's characters are tappable. The bubble wraps the link over two
    # lines and its box centre falls between them (vivo V2131A ignored such taps),
    # so the left part of the second line is tapped, then of the first.
    x, y, w, h = bubble["box"]
    for tap, offset in enumerate((16, -16)):
        ui.click({"text": link, "box": [x + w // 4 - 10, y + h // 2 + offset - 10, 20, 20]})
        time.sleep(2.0)
        # The link first loads in WeChat's browser, which then opens the video;
        # on a slow network that took over 10 s (Hebe-BD00, 2026-11-12).
        deadline = time.monotonic() + 15
        attempt = 0
        while True:
            attempt += 1
            lines = ui.snapshot(f"link-video-{tap * 10 + attempt}")
            if _is_chat(lines):
                # Still the chat: a keyboard that opened late swallowed the tap.
                break
            try:
                author_in_feed(lines, ui.height)
                return lines
            except ValueError:
                pass
            if time.monotonic() >= deadline:
                # Give up on this page load: close the browser and tap again.
                ui.back()
                break
            time.sleep(1.5)
        if hasattr(ui, "hide_keyboard"):
            ui.hide_keyboard()
    raise ValueError("The video did not open from its link")


def _profile_contact(ui, lines):
    found = control(ui.controls_now(), r"^私信按钮$")
    return found or find(lines, r"^私信$")


def send_private_message(ui, profile_lines, text):
    """From an open Channels profile: 私信 → send text → back to the profile."""
    button = _profile_contact(ui, profile_lines)
    if not button:
        raise ValueError("This profile has no 私信 button")
    ui.click(button)
    chat = ui.snapshot("dm-chat")
    restriction = find(chat, DM_RESTRICTED)
    if restriction and not _edit_box(chat):
        # e.g. 作者已设置关注7天后才能发消息: the chat has no input box at all.
        raise ValueError(f"对方限制了私信：{restriction['text']}")
    if not _is_chat(chat):
        raise ValueError("The private message chat did not open")
    sent = send_chat_text(ui, chat, text, "dm-chat")
    if not _sent_bubble(sent, text):
        # WeChat allows one message before the other side replies.
        limit = find(sent, r"仅可发\s*1\s*条消息|发送失败|无法发送")
        raise RuntimeError("The private message was not sent" + (f": {limit['text']}" if limit else ""))
    for _ in range(3):
        ui.back()
        lines = ui.snapshot("profile-follow-check")
        if classify_page_state(lines, ui.height) == "user_profile":
            return lines
    raise ValueError("Did not return to the profile after sending the message")


def _back_to_video(ui, label, backs=3):
    for attempt in range(backs):
        ui.back()
        lines = ui.snapshot(f"{label}-{attempt + 1}")
        if classify_page_state(lines, ui.height) == "channels_feed":
            return lines
    raise ValueError("Did not return to the video")


def _open_author_profile(ui, lines):
    target = author_in_feed(lines, ui.height)
    author = target["text"].strip()
    ui.click(target)
    time.sleep(PROFILE_SETTLE_SECONDS)
    profile = ui.snapshot("author-profile-touch", author=author)
    if classify_page_state(profile, ui.height) != "user_profile":
        raise ValueError("The author's profile did not open")
    return author, profile


def _locate(ui, quota_store, work_key, nickname, lines):
    located = locate_target_comment(ui, target_spec={"nickname": nickname}, quota_store=quota_store,
                                    work_key=work_key, exclude_self=True, max_pages=ALL_COMMENT_PAGES, lines=lines)
    if not located["found"]:
        reason = "只找到自己的评论" if located.get("rejectedSelf") else "翻完评论区也没找到"
        raise ValueError(f"评论者「{nickname}」的评论未找到：{reason}")
    return located


def run_link_task(ui, quota_store, ledger, config):
    """Open the linked video and do one action; returns a JSON-ready report."""
    validate_link_task(config)
    action = config["action"]
    link = share_link(config["link"])
    nickname = str(config.get("nickname") or "").strip()
    text = str(config.get("text") or "").strip()
    lines = open_video_by_link(ui, link)
    preview = feed_video_preview(lines, ui.height)
    work_key = compute_work_key(author=preview["author"], title=preview["captionPreview"] or preview["author"])
    report = {"action": action, "link": link, "author": preview["author"], "workKey": work_key}

    if action == "likeVideo":
        report["like"] = like_work(ui, quota_store, work_key, lines=lines)
        report["collect"] = collect_work_action(ui, quota_store, work_key)
        report["status"] = "done"
    elif action == "followAuthor":
        touched = visit_and_touch_author_profile(ui, quota_store, do_follow=True, do_like_first_work=False,
                                                 do_collect_first_work=False, lines=lines)
        report["follow"] = touched["touchDetail"]["followResult"]
        report["status"] = "done"
    elif action == "messageAuthor":
        key = "dm:" + identity(preview["author"])
        if ledger.has(key):
            report["status"] = "already_messaged"
        else:
            _, profile = _open_author_profile(ui, lines)
            send_private_message(ui, profile, text)
            ledger.add(key)
            report["status"] = "sent"
            _back_to_video(ui, "link-after-dm")
    elif action == "commentVideo":
        key = "comment:" + work_key
        if ledger.has(key):
            report["status"] = "already_commented"
        else:
            report["comment"] = post_work_comment(ui, quota_store, work_key, text, lines=lines)
            ledger.add(key)
            report["status"] = "sent"
    elif action == "likeComment":
        located = _locate(ui, quota_store, work_key, nickname, lines)
        report["commentLike"] = like_comment(ui, quota_store, work_key, target_comment={"nickname": nickname},
                                             lines=located["lines"])
        if report["commentLike"]["status"] == "target_not_found":
            raise ValueError(f"评论者「{nickname}」的评论未找到")
        report["status"] = "done"
    elif action == "replyComment":
        located = _locate(ui, quota_store, work_key, nickname, lines)
        comment = located["comment"]
        key = "reply:" + identity(work_key, comment["nickname"], comment.get("content") or "")
        if ledger.has(key):
            report["status"] = "already_replied"
        else:
            reply = reply_to_comment(ui, quota_store, work_key, text, target_comment={"nickname": nickname},
                                     lines=located["lines"], max_comment_pages=ALL_COMMENT_PAGES)
            report["reply"] = reply
            if reply["status"] not in ("performed", "already_replied"):
                raise ValueError(f"回复没有发出：{reply['status']}")
            ledger.add(key)
            report["status"] = "sent" if reply["status"] == "performed" else "already_replied"
    elif action == "messageCommenter":
        located = _locate(ui, quota_store, work_key, nickname, lines)
        comment = located["comment"]
        key = "dm:" + identity(comment["nickname"])
        if ledger.has(key):
            report["status"] = "already_messaged"
        else:
            ui.click({"text": comment["nickname"], "box": comment["_nameBox"]})
            time.sleep(PROFILE_SETTLE_SECONDS)
            profile = ui.snapshot("commenter-profile", author=comment["nickname"])
            if classify_page_state(profile, ui.height) != "user_profile":
                ui.back()
                raise ValueError(f"「{comment['nickname']}」没有开通视频号，资料卡上没有私信入口")
            send_private_message(ui, profile, text)
            ledger.add(key)
            report["status"] = "sent"
            report["commenter"] = comment["nickname"]
    report["matchedNickname"] = nickname or None
    return report
