"""Comment drawer parsing, paging, reply expansion, and commenter identity collection."""

import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from difflib import SequenceMatcher

from collection import (
    comment_control_in_feed,
    find,
    identity,
    open_basic_info,
    parse_author,
    profile_bio_expand,
)
from layout import DATE, REGION, compact, line_height, profile_name, reading_rows

LIKE_BADGE_RE = r"[\d.万wW]+[凸👍♡赞]*|[凸👍♡赞]+"
TAG_ONLY_RE = r"作者赞过|置顶|首评|热评|精选|收起|赞过"
FOOTER_END_RE = r"没有更多评论|已显示全部评论|到底了|THE\s*END"
EMPTY_COMMENTS_RE = r"暂无评论|还没有评论"


class CommenterProfileSkipped(ValueError):
    def __init__(self, reason, message=None, recovered_lines=None):
        super().__init__(message or reason)
        self.reason = reason
        self.recovered_lines = recovered_lines


def clean_comment_nickname(raw):
    """Strip work-author suffix and OCR'd Channels butterfly badge ('W')."""
    text = re.sub(r"作者$", "", (raw or "").strip()).strip()
    has_badge = False
    if re.search(r"[.…⋯·]{2,}\s*[Ww]$", text):
        text = re.sub(r"[.…⋯·]{2,}\s*[Ww]$", "...", text).strip()
        has_badge = True
    elif re.search(r"\s+[Ww]$", text):
        text = re.sub(r"\s+[Ww]$", "", text).strip()
        has_badge = True
    elif re.search(r"(?<=[\u4e00-\u9fff）】》\s])W$", text):
        text = re.sub(r"(?<=[\u4e00-\u9fff）】》\s])W$", "", text).strip()
        has_badge = True
    text = re.sub(r"[.…⋯·]{2,}$", "...", text).strip()
    return text, has_badge


def nickname_matches(expected, observed):
    """Verify whether a profile/card name corresponds to a comment nickname."""
    exp, _ = clean_comment_nickname(expected)
    obs, _ = clean_comment_nickname(observed)
    if not obs:
        return False
    if exp == "[表情昵称]":
        return True
    exp_compact = compact(exp)
    obs_compact = compact(obs)
    if exp_compact == obs_compact:
        return True
    prefix = compact(re.sub(r"[.…⋯·]+$", "", exp))
    if prefix and (obs_compact.startswith(prefix) or prefix.startswith(obs_compact)):
        return True
    base = prefix or exp_compact
    return bool(base and SequenceMatcher(None, base, obs_compact).ratio() >= 0.6)


def reply_target_name(content, is_reply=True):
    """Extract unambiguous reply target display name while preserving raw content."""
    if not is_reply or not content:
        return None
    first_line = content.splitlines()[0] if content.splitlines() else content
    if len(re.findall(r"[:：]", first_line)) != 1:
        return None
    match = re.match(r"^回复\s*([^:：\n]+)[:：]\s*\S", first_line)
    if not match:
        return None
    target, _ = clean_comment_nickname(match[1])
    return target or None


def comment_key(parent_id, comment):
    """Stable deduplication key scoped to the parent work or parent comment."""
    return "wxch-comment:" + identity(
        parent_id,
        comment["nickname"],
        comment["content"],
        comment["commentTime"],
        comment.get("ipLocation") or "",
    )


def _is_ad_row(row):
    text = compact(row["text"])
    if re.fullmatch(r"了解详情|查看详情|立即下载|去看看", text):
        return True
    if any(
        item["box"][0] > 450 and re.fullmatch(r"广告(?:\s*[×xX∨⌄])?", item["text"].strip())
        for item in row["items"]
    ):
        return True
    if text.endswith("广告") and max((item["box"][0] for item in row["items"]), default=0) > 450:
        return True
    return False


def _header_name_box(row, nickname):
    """Construct a safe click target over the first character of the commenter's nickname."""
    left_items = [item for item in row["items"] if item["box"][0] < 450]
    first = left_items[0] if left_items else row["items"][0]
    x, y, w, h = first["box"]
    return [x, y, min(w, 32), h]


def _expand_click_target(item):
    x, y, w, h = item["box"]
    text = item["text"].strip()
    if len(text) > 3 and w > 60:
        return {**item, "box": [x + w - 48, y, 48, h]}
    return item


def parse_comment_drawer(lines, height, parent_event_id=""):
    """Parse first-level comments, second-level replies, and expansion controls."""
    rows = reading_rows(lines)
    lh = line_height(lines)
    heading = next(
        (row for row in rows if any(re.fullmatch(r"评论\s*[\d.万wW]*", item["text"].strip()) for item in row["items"])),
        None,
    )
    if heading is None:
        raise ValueError("Comment drawer is not visible")

    heading_text = compact(heading["text"])
    count_match = re.search(r"评论(\d+)$", heading_text)
    declared_total = int(count_match[1]) if count_match else None
    empty = bool(find(lines, EMPTY_COMMENTS_RE)) or declared_total == 0
    footer_reached = bool(find(lines, FOOTER_END_RE))

    input_bar = find(
        lines,
        r"发表评论|说点什么|写评论",
        lambda line: line["box"][1] >= height - 260 and line["box"][0] < 480,
    )
    self_nickname = None
    if input_bar:
        bar_match = re.match(r"^(.+?)(?:发表评论|说点什么|写评论)[:：]?$", compact(input_bar["text"]))
        if bar_match:
            self_nickname = clean_comment_nickname(bar_match[1])[0] or None

    headers = []
    for index, row in enumerate(rows):
        if row["top"] <= heading["bottom"] or row["top"] >= height - 80:
            continue
        if _is_ad_row(row):
            continue
        like_item = next(
            (
                item
                for item in row["items"]
                if item["box"][0] > 480 and re.fullmatch(LIKE_BADGE_RE, item["text"].strip())
            ),
            None,
        )
        has_recommend_badge = any(
            item["box"][0] > 500 and re.fullmatch(r"推荐[♡♥]?", item["text"].strip())
            for item in row["items"]
        )
        has_like_action = like_item is not None or has_recommend_badge
        pieces = [
            item
            for item in row["items"]
            if item["box"][0] < 570
            and not re.search(r"已声明原创|都在搜", item["text"])
            and not (item["box"][0] > 400 and re.fullmatch(LIKE_BADGE_RE, item["text"].strip()))
            and not (item["box"][0] > 500 and re.fullmatch(r"推荐[♡♥]?", item["text"].strip()))
        ]
        if not pieces:
            continue
        left = min(item["box"][0] for item in pieces)
        if left > 220:
            continue
        text = compact("".join(item["text"] for item in pieces))
        match = re.fullmatch(rf"(.+?)({REGION}|我)({DATE})", text)
        raw_nick, region, date = None, None, None
        if match:
            raw_nick, region, date = match[1], match[2], match[3]
        elif has_like_action and 90 <= left <= 215:
            bare = re.fullmatch(rf"({REGION}|我)({DATE})", text)
            prev_row = rows[index - 1] if index > 0 else None
            is_work_meta = bool(
                prev_row
                and "作者" in prev_row["text"]
                and row["top"] - prev_row["bottom"] <= lh * 1.5
            )
            if bare and not is_work_meta:
                raw_nick, region, date = "[表情昵称]", bare[1], bare[2]
        if not raw_nick:
            continue
        nickname, has_badge = clean_comment_nickname(raw_nick)
        if not nickname:
            continue
        level = 1 if left <= 140 else 2
        like_count_text = (
            re.sub(r"[凸👍♡赞]+$", "", like_item["text"].strip())
            if like_item is not None
            else ""
        )
        row_h = max(24, row["bottom"] - row["top"])
        if has_recommend_badge:
            like_box = [590, row["top"], 36, row_h]
        elif like_item is not None:
            lx, ly, lw, lbh = like_item["box"]
            like_box = [lx + max(0, lw - 36), ly, min(max(lw, 32), 40), max(24, lbh)]
        else:
            like_box = [656, row["top"], 36, row_h]
        is_self = (region == "我") or bool(self_nickname and nickname == self_nickname)
        headers.append(
            {
                "index": index,
                "row": row,
                "nickname": nickname,
                "region": region,
                "date": date,
                "level": level,
                "hasChannelsBadge": has_badge,
                "nameBox": _header_name_box(row, nickname),
                "likeBox": like_box,
                "liked": has_recommend_badge,
                "likeCountText": like_count_text,
                "isSelf": is_self,
                "hasAuthorTag": "作者" in row["text"],
            }
        )

    # The first author/metadata row beneath the drawer title belongs to the work.
    if headers and headers[0]["level"] == 1 and headers[0]["hasAuthorTag"]:
        headers = headers[1:]

    comments = []
    comment_expands = []
    reply_expands = []
    current_parent = None

    for position, header in enumerate(headers):
        index = header["index"]
        row = header["row"]
        level = header["level"]
        end = headers[position + 1]["index"] if position + 1 < len(headers) else len(rows)
        body = []
        reply_count = 0
        reply_expand_box = None
        has_expand_control = False
        bounded = position + 1 < len(headers)
        body_box = None

        for r in rows[index + 1 : end]:
            text = r["text"].strip()
            comp = compact(text)
            if _is_ad_row(r):
                bounded = True
                break
            reply_match = re.fullmatch(r"(?:展开)?(\d+)条回复(?:[⌄∨vV>＞])?", comp)
            more_replies = re.fullmatch(r"(?:展开)?更多回复(?:[⌄∨vV>＞])?", comp)
            if reply_match or more_replies:
                if reply_match:
                    reply_count = int(reply_match[1])
                reply_expand_box = [
                    r["left"],
                    r["top"],
                    max(item["box"][0] + item["box"][2] for item in r["items"]) - r["left"],
                    r["bottom"] - r["top"],
                ]
                bounded = True
                break
            if re.search(rf"发表评论|说点什么|写评论|{FOOTER_END_RE}", text):
                bounded = True
                break
            if r["top"] >= height - 80:
                break
            if re.fullmatch(TAG_ONLY_RE, comp):
                bounded = True
                continue
            if level == 1 and r["left"] > row["left"] + 45:
                bounded = True
                break
            for item in r["items"]:
                if re.search(r"(?:[.…⋯·]+\s*(?:展开|全文)|^(?:展开|全文))$", item["text"].strip()):
                    comment_expands.append(_expand_click_target(item))
                    has_expand_control = True
            if body_box is None:
                body_box = [
                    r["left"],
                    r["top"],
                    min(280, max(60, max(item["box"][0] + item["box"][2] for item in r["items"]) - r["left"])),
                    max(24, r["bottom"] - r["top"]),
                ]
            body.append(text)

        content = "\n".join(body).strip()
        target_parent = current_parent if level == 2 else None
        if level == 1:
            current_parent = None

        is_truncated = (
            has_expand_control
            or content.endswith(("...", "…", "⋯"))
            or bool(re.search(r"[.…⋯·]+\s*(?:展开|全文)$", content))
        )
        if not bounded or not content or is_truncated:
            if level == 1:
                current_parent = {
                    "nickname": header["nickname"],
                    "content": content,
                    "commentTime": header["date"],
                    "ipLocation": header["region"],
                    "key": comment_key(parent_event_id or "", {
                        "nickname": header["nickname"],
                        "content": content or header["nickname"],
                        "commentTime": header["date"],
                        "ipLocation": header["region"],
                    }),
                    "complete": False,
                    "inlineReplies": 0,
                }
            continue

        entry = {
            "nickname": header["nickname"],
            "content": content,
            "commentTime": header["date"],
            "ipLocation": header["region"],
            "authorDouyinId": None,
            "replyCount": reply_count if level == 1 else 0,
            "repliesCollected": False,
            "commentLevel": level,
            "parentCommentKey": target_parent["key"] if target_parent else None,
            "replyToNickname": None,
            "replyToCommentKey": None,
            "replyToDouyinId": None,
            "_hasChannelsBadge": header["hasChannelsBadge"],
            "_nameBox": header["nameBox"],
            "_bodyBox": body_box,
            "_likeBox": header["likeBox"],
            "_liked": header["liked"],
            "_likeCountText": header["likeCountText"],
            "_isSelf": header["isSelf"],
            "_headerTop": row["top"],
        }
        entry["key"] = comment_key(
            (parent_event_id or "") + (f":{entry['parentCommentKey']}" if entry["parentCommentKey"] else ""),
            entry,
        )

        if level == 1:
            current_parent = entry
            comments.append(entry)
            if reply_expand_box:
                reply_expands.append(
                    {
                        "parentKey": entry["key"],
                        "parentNickname": entry["nickname"],
                        "replyCount": reply_count,
                        "box": reply_expand_box,
                        "text": f"{reply_count}条回复" if reply_count else "展开更多回复",
                    }
                )
        elif target_parent is not None:
            explicit_target = reply_target_name(content, is_reply=True)
            entry["replyToNickname"] = explicit_target or target_parent["nickname"]
            if not explicit_target and target_parent.get("complete", True):
                entry["replyToCommentKey"] = target_parent["key"]
            target_parent["inlineReplies"] = target_parent.get("inlineReplies", 0) + 1
            if reply_count and target_parent.get("replyCount", 0) < reply_count:
                target_parent["replyCount"] = reply_count
            if reply_expand_box:
                reply_expands.append(
                    {
                        "parentKey": target_parent["key"],
                        "parentNickname": target_parent["nickname"],
                        "replyCount": reply_count or target_parent.get("replyCount", 0),
                        "box": reply_expand_box,
                        "text": f"{reply_count}条回复" if reply_count else "展开更多回复",
                    }
                )
            elif target_parent.get("replyCount", 0) == 0:
                target_parent["replyCount"] = target_parent["inlineReplies"]
                target_parent["repliesCollected"] = True
            comments.append(entry)

    return {
        "heading": heading,
        "declaredTotal": declared_total,
        "empty": empty,
        "footerReached": footer_reached,
        "selfNickname": self_nickname,
        "inputBar": input_bar,
        "comments": comments,
        "commentExpands": comment_expands,
        "replyExpands": reply_expands,
    }


def _public_comment(comment):
    return {k: v for k, v in comment.items() if not k.startswith("_") and k != "inlineReplies"}


def parse_comments(lines, height, include_replies=True):
    drawer = parse_comment_drawer(lines, height)
    return [
        _public_comment(c)
        for c in drawer["comments"]
        if include_replies or c.get("commentLevel", 1) == 1
    ]


def resolve_reply_targets(comments, profiles=()):
    """Link replyToCommentKey/replyToDouyinId and backfill authorDouyinId from visited profiles."""
    profile_by_key = {
        p["commentKey"]: p
        for p in profiles
        if p.get("profileVisited") and p.get("commentKey")
    }
    profile_by_nick = {}
    for p in profiles:
        if p.get("profileVisited") and p.get("douyinId"):
            for name in (p.get("commentNickname"), p.get("nickname")):
                if name:
                    profile_by_nick.setdefault(name, p["douyinId"])

    for comment in comments:
        key = comment.get("key")
        prof = profile_by_key.get(key)
        if prof:
            if prof.get("douyinId"):
                comment["authorDouyinId"] = prof["douyinId"]
            comment["touchStatus"] = prof.get("touchStatus", "visited")
        elif comment.get("nickname") in profile_by_nick and not comment.get("authorDouyinId"):
            comment["authorDouyinId"] = profile_by_nick[comment["nickname"]]

    for idx, record in enumerate(comments):
        if record.get("commentLevel", 1) != 2:
            record["replyToNickname"] = None
            record["replyToCommentKey"] = None
            record["replyToDouyinId"] = None
            continue
        parent = record.get("parentCommentKey")
        target_name = record.get("replyToNickname")
        if not parent or not target_name:
            record["replyToCommentKey"] = None
            record["replyToDouyinId"] = None
            continue
        preceding = [
            c
            for c in comments[:idx]
            if (c.get("parentCommentKey") or c.get("key")) == parent
            and nickname_matches(target_name, c.get("nickname", ""))
            and c.get("key") != record.get("key")
        ]
        if not preceding:
            preceding = [
                c
                for c in comments
                if (c.get("parentCommentKey") or c.get("key")) == parent
                and nickname_matches(target_name, c.get("nickname", ""))
                and c.get("key") != record.get("key")
            ]
        unique_keys = list(dict.fromkeys(c["key"] for c in preceding if c.get("key")))
        if len(unique_keys) == 1:
            target_comment = next(c for c in preceding if c.get("key") == unique_keys[0])
            record["replyToCommentKey"] = unique_keys[0]
            record["replyToDouyinId"] = target_comment.get("authorDouyinId") or profile_by_nick.get(target_name)
        elif len(unique_keys) > 1:
            record["replyToCommentKey"] = None
            ids = {c.get("authorDouyinId") for c in preceding if c.get("key") in unique_keys}
            record["replyToDouyinId"] = next(iter(ids)) if len(ids) == 1 and None not in ids else None
        else:
            record["replyToCommentKey"] = None
            record["replyToDouyinId"] = None
    return comments


def parse_wechat_user_card(lines, expected_nickname, height):
    """Parse the bottom profile card shown for commenters without a Channels account."""
    if find(lines, r"^(?:[√✓]?)\s*(已关注|申请关注|关注)$") and find(lines, r"^(私信|客服)$"):
        return None
    card_top = height * 0.55
    settings = find(lines, r"^设置$", lambda line: line["box"][0] > 520 and line["box"][1] > card_top)
    candidates = sorted(
        (
            line
            for line in lines
            if line["box"][1] > max(card_top, (settings["box"][1] + 20) if settings else height * 0.68)
            and 135 <= line["box"][0] <= 480
            and not re.fullmatch(r"设置|取消|确定|发表评论|说点什么", line["text"].strip())
        ),
        key=lambda item: (item["box"][1], item["box"][0]),
    )
    if not candidates:
        return None
    name_line = candidates[0]
    nickname = name_line["text"].strip()
    if not nickname_matches(expected_nickname, nickname):
        return None
    location = None
    for extra in candidates[1:3]:
        txt = extra["text"].strip()
        if re.search(rf"(?:{REGION})|[男女]$", txt):
            location = txt
            break
    return {
        "nickname": nickname,
        "profileLocation": location,
        "hasSettingsControl": bool(settings),
    }


def _recover_to_comment_drawer(ui, max_backs=2):
    for _ in range(max_backs):
        ui.back()
        lines = ui.snapshot("comments-recover")
        if find(lines, r"^评论\s*[\d.万wW]*") and not (
            find(lines, r"^(?:[√✓]?)\s*(已关注|申请关注|关注)$") and find(lines, r"^(私信|客服)$")
        ) and not find(lines, r"^基础信息$"):
            return lines
    raise RuntimeError("Could not recover to the comment drawer")


def inspect_commenter_profile(ui, comment):
    """Click a commenter in the drawer and collect either their Channels profile or WeChat card."""
    nickname = comment["nickname"]
    name_box = comment.get("_nameBox")
    if not name_box:
        raise CommenterProfileSkipped("comment_missing", "Comment has no clickable nickname box")

    ui.click({"text": nickname, "box": name_box})
    profile_lines = ui.snapshot("commenter-profile", author=nickname)

    # Case 1: Commenter has a public WeChat Channels account (Profile Drawer).
    if find(profile_lines, r"^(?:[√✓]?)\s*(已关注|申请关注|关注)$") and find(profile_lines, r"^(私信|客服)$"):
        try:
            try:
                profile = parse_author(profile_lines, nickname)
            except ValueError:
                import time
                time.sleep(0.5)
                profile_lines = ui.snapshot("commenter-profile", author=nickname)
                profile = parse_author(profile_lines, nickname)
            if not nickname_matches(nickname, profile["authorNickname"]):
                ui.back()
                raise CommenterProfileSkipped("identity_unconfirmed", "Channels profile name did not match comment")
            expand = profile_bio_expand(profile_lines, nickname)
            if expand:
                ui.click(_expand_click_target(expand))
                profile_lines = ui.snapshot("commenter-profile", author=nickname)
                profile = parse_author(profile_lines, nickname)
            if hasattr(ui, "read_profile_bio"):
                try:
                    profile["authorBio"] = ui.read_profile_bio(profile_lines, profile)
                except ValueError:
                    pass
            name = profile_name(profile_lines, profile["authorNickname"])
            ui.profile_more(name)
            menu = ui.snapshot("commenter-profile-menu")
            more = find(menu, r"^更多信息$")
            if not more:
                ui.back()
                ui.back()
                raise CommenterProfileSkipped("basic_info_unavailable", "More information menu item is missing")
            basic = open_basic_info(ui, more)
            ui.back()
            ui.back()
            return {
                "commentKey": comment["key"],
                "nickname": profile["authorNickname"],
                "commentNickname": nickname,
                "douyinId": basic["authorDouyinId"],
                "identityMethod": "wechat-channel-id",
                "accountType": "channels",
                "authorBio": profile.get("authorBio"),
                "authorCertification": basic.get("authorCertification") or profile.get("authorCertification"),
                "ipLocation": basic.get("authorIpLocation") or profile.get("authorIpLocation") or comment.get("ipLocation"),
                "profileLocation": basic.get("authorProfileLocation") or profile.get("authorProfileLocation"),
                "profileVisited": True,
                "touchStatus": "visited",
                "profileActionsPerformed": [],
            }
        except CommenterProfileSkipped:
            raise
        except Exception as error:
            recovered = _recover_to_comment_drawer(ui, max_backs=2)
            raise CommenterProfileSkipped("profile_unavailable", str(error), recovered_lines=recovered) from error

    # Case 2: Normal WeChat user without a Channels account (Bottom Card).
    card = parse_wechat_user_card(profile_lines, nickname, ui.height)
    if card:
        ui.back()
        return {
            "commentKey": comment["key"],
            "nickname": card["nickname"],
            "commentNickname": nickname,
            "douyinId": None,
            "identityMethod": "wechat-user-card",
            "accountType": "wechat_user",
            "authorBio": None,
            "authorCertification": None,
            "ipLocation": comment.get("ipLocation"),
            "profileLocation": card["profileLocation"],
            "profileVisited": True,
            "touchStatus": "visited_no_channels_id",
            "profileActionsPerformed": [],
        }

    recovered = _recover_to_comment_drawer(ui, max_backs=1)
    raise CommenterProfileSkipped(
        "identity_unconfirmed",
        f"Could not confirm commenter identity for {nickname}",
        recovered_lines=recovered,
    )


def save_comments(path, video_event, comments):
    """Local-only drafts linked to a real saved VIDEO event, never fabricated ACKs."""
    if video_event.get("event_type") != "VIDEO" or not video_event.get("event_id"):
        raise ValueError("A saved VIDEO parent is required")
    events = []
    with closing(sqlite3.connect(path)) as db, db:
        parent = db.execute("SELECT event_json FROM video_drafts WHERE event_id=?", (video_event["event_id"],)).fetchone()
        if not parent:
            raise ValueError("Parent VIDEO is not in the local database")
        saved = json.loads(parent[0])
        db.execute("""CREATE TABLE IF NOT EXISTS comment_drafts (
            dedupe_key TEXT PRIMARY KEY, event_id TEXT UNIQUE NOT NULL,
            parent_event_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status='local_only'),
            event_json TEXT NOT NULL)""")
        for raw_comment in comments:
            comment = _public_comment(raw_comment)
            if not all(comment.get(key) for key in ("nickname", "content", "commentTime")):
                raise ValueError("Comment is incomplete")
            key = comment.pop("key", None) or comment_key(saved["event_id"], comment)
            if not key.startswith("wxch-comment:"):
                key = comment_key(saved["event_id"], comment)
            previous = db.execute("SELECT event_json FROM comment_drafts WHERE dedupe_key=?", (key,)).fetchone()
            if previous:
                event = json.loads(previous[0])
                merged = {
                    **event["payload"],
                    **{k: v for k, v in comment.items() if v is not None or k not in event["payload"]},
                }
                if merged != event["payload"]:
                    event["payload"] = merged
                    db.execute(
                        "UPDATE comment_drafts SET event_json=? WHERE dedupe_key=?",
                        (json.dumps(event, ensure_ascii=False), key),
                    )
                events.append(event)
                continue
            event = {
                "event_id": str(uuid.uuid4()),
                "event_type": "COMMENT",
                "dedupe_key": key,
                "parent_event_id": saved["event_id"],
                "device_id": saved["device_id"],
                "source_mode": saved["source_mode"],
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "upload_status": "local_only",
                "payload": comment,
            }
            db.execute(
                "INSERT INTO comment_drafts VALUES (?, ?, ?, 'local_only', ?)",
                (key, event["event_id"], saved["event_id"], json.dumps(event, ensure_ascii=False)),
            )
            events.append(event)
    return events


def save_users(path, comment_events, profiles):
    """Persist local-only USER drafts linked to their parent COMMENT event_id."""
    if not profiles:
        return []
    by_dedupe = {event["dedupe_key"]: event for event in comment_events}
    by_nick = {}
    for event in comment_events:
        nick = event.get("payload", {}).get("nickname")
        if nick:
            by_nick.setdefault(nick, event)
    events = []
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS user_drafts (
            dedupe_key TEXT PRIMARY KEY, event_id TEXT UNIQUE NOT NULL,
            parent_event_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status='local_only'),
            event_json TEXT NOT NULL)""")
        for profile in profiles:
            if not profile.get("profileVisited") or not profile.get("nickname"):
                continue
            parent_event = (
                by_dedupe.get(profile.get("commentKey"))
                or by_nick.get(profile.get("commentNickname"))
                or by_nick.get(profile.get("nickname"))
                or (comment_events[0] if comment_events else None)
            )
            if not parent_event:
                continue
            parent_id = parent_event["event_id"]
            if profile.get("douyinId"):
                key = "wxch-user:" + identity("channels", profile["douyinId"])
            else:
                key = "wxch-user:" + identity(
                    "wechat-user",
                    parent_id,
                    profile["nickname"],
                    profile.get("profileLocation") or "",
                )
            payload = {
                "nickname": profile["nickname"],
                "commentNickname": profile.get("commentNickname") or profile["nickname"],
                "douyinId": profile.get("douyinId"),
                "identityMethod": profile.get("identityMethod"),
                "accountType": profile.get("accountType"),
                "authorBio": profile.get("authorBio"),
                "authorCertification": profile.get("authorCertification"),
                "ipLocation": profile.get("ipLocation"),
                "profileLocation": profile.get("profileLocation"),
                "touchStatus": profile.get("touchStatus", "visited"),
                "profileActionsPerformed": profile.get("profileActionsPerformed", []),
                "returnVerified": bool(profile.get("returnVerified", True)),
            }
            previous = db.execute("SELECT event_json FROM user_drafts WHERE dedupe_key=?", (key,)).fetchone()
            if previous:
                event = json.loads(previous[0])
                event["payload"] = {**event["payload"], **payload}
                db.execute(
                    "UPDATE user_drafts SET event_json=? WHERE dedupe_key=?",
                    (json.dumps(event, ensure_ascii=False), key),
                )
                events.append(event)
                continue
            event = {
                "event_id": str(uuid.uuid4()),
                "event_type": "USER",
                "dedupe_key": key,
                "parent_event_id": parent_id,
                "device_id": parent_event["device_id"],
                "source_mode": parent_event["source_mode"],
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "upload_status": "local_only",
                "payload": payload,
            }
            db.execute(
                "INSERT INTO user_drafts VALUES (?, ?, ?, 'local_only', ?)",
                (key, event["event_id"], parent_id, json.dumps(event, ensure_ascii=False)),
            )
            events.append(event)
    return events


def _drawer_signature(drawer):
    return [(c["key"], c.get("_headerTop", 0)) for c in drawer["comments"]]


def collect_visible_comments(
    ui,
    path,
    video_event,
    timings,
    max_pages=3,
    max_reply_expansions=1,
    max_profiles=2,
):
    with timings.phase("collect_visible_comments"):
        feed = ui.snapshot("feed")
        for _ in range(2):
            if not find(feed, r"^正在观看$|^私信$|^客服$|^基础信息$") and not any(
                line["box"][1] > ui.height - 42 for line in feed
            ):
                break
            ui.back()
            feed = ui.snapshot("feed")
        drawer_already_open = bool(find(feed, r"^评论\s*[\d.万wW]*"))
        target = None
        if not drawer_already_open:
            target = comment_control_in_feed(feed, ui.height, ui)
            if target is None:
                raise ValueError("Comment control is missing")
            ui.click(target)
        try:
            lines = feed if drawer_already_open else ui.snapshot("comments")
            parent_id = video_event["event_id"]
            collected = {}
            profiles = []
            skipped_profiles = []
            visited_comment_keys = set()
            expanded_reply_parents = set()
            reply_expansions_done = 0
            pages_read = 0
            end_reason = "page_limit"
            complete = False

            for page_index in range(max(1, max_pages)):
                pages_read += 1
                drawer = parse_comment_drawer(lines, ui.height, parent_event_id=parent_id)

                # 1. Expand truncated comments on the current viewport (bounded to 2 clicks).
                for _ in range(2):
                    if not drawer["commentExpands"]:
                        break
                    ui.click(drawer["commentExpands"][0])
                    lines = ui.snapshot("comments-expanded")
                    drawer = parse_comment_drawer(lines, ui.height, parent_event_id=parent_id)

                # 2. Expand secondary replies when available and within budget.
                while reply_expansions_done < max_reply_expansions:
                    pending = [
                        exp
                        for exp in drawer["replyExpands"]
                        if exp["parentKey"] not in expanded_reply_parents
                    ]
                    if not pending:
                        break
                    chosen = pending[0]
                    expanded_reply_parents.add(chosen["parentKey"])
                    ui.click(chosen)
                    reply_expansions_done += 1
                    lines = ui.snapshot("comments-replies")
                    drawer = parse_comment_drawer(lines, ui.height, parent_event_id=parent_id)
                    for item in drawer["comments"]:
                        if item["key"] == chosen["parentKey"]:
                            item["repliesCollected"] = True

                for item in drawer["comments"]:
                    if item["key"] in expanded_reply_parents:
                        item["repliesCollected"] = True
                    prev = collected.get(item["key"])
                    if prev:
                        item["repliesCollected"] = item["repliesCollected"] or prev.get("repliesCollected", False)
                        item["replyCount"] = max(item.get("replyCount", 0), prev.get("replyCount", 0))
                    collected[item["key"]] = item

                # 3. Visit commenter profiles on the current viewport when supported by ui.
                can_inspect_profiles = hasattr(ui, "profile_more") and max_profiles > 0
                if can_inspect_profiles and len(profiles) < max_profiles and drawer["comments"]:
                    reply_targets = {
                        c["replyToNickname"]
                        for c in list(collected.values()) + drawer["comments"]
                        if c.get("replyToNickname")
                    }
                    while len(profiles) < max_profiles:
                        has_visited_channels = any(p.get("accountType") == "channels" for p in profiles)

                        def candidate_rank(c):
                            is_channels = bool(c.get("_hasChannelsBadge"))
                            wants_channels = 0 if (is_channels and not has_visited_channels) else 1
                            is_target = 0 if c["nickname"] in reply_targets else 1
                            return (is_target, wants_channels, c.get("_headerTop", 0))

                        remaining = [
                            c
                            for c in sorted(drawer["comments"], key=candidate_rank)
                            if c["key"] not in visited_comment_keys
                            and c["nickname"] != "[表情昵称]"
                        ]
                        if not remaining:
                            break
                        candidate = remaining[0]
                        visited_comment_keys.add(candidate["key"])
                        current_drawer = parse_comment_drawer(lines, ui.height, parent_event_id=parent_id)
                        live_match = next(
                            (
                                c
                                for c in current_drawer["comments"]
                                if c["key"] == candidate["key"]
                                or nickname_matches(candidate["nickname"], c["nickname"])
                            ),
                            None,
                        )
                        if live_match is None:
                            skipped_profiles.append(
                                {
                                    "commentKey": candidate["key"],
                                    "nickname": candidate["nickname"],
                                    "reason": "comment_missing",
                                }
                            )
                            continue
                        try:
                            with timings.phase("collect_commenter_profile"):
                                prof = inspect_commenter_profile(ui, live_match)
                                lines = ui.snapshot("comments-return")
                                if not find(lines, r"^评论\s*[\d.万wW]*") and (
                                    find(lines, r"^(?:[√✓]?)\s*(已关注|关注)$")
                                    or find(lines, r"^(私信|客服)$")
                                    or find(lines, r"^正在观看$")
                                ):
                                    ui.back()
                                    lines = ui.snapshot("comments-return")
                                returned_drawer = parse_comment_drawer(lines, ui.height, parent_event_id=parent_id)
                                anchor_ok = any(
                                    c["key"] == live_match["key"]
                                    or nickname_matches(live_match["nickname"], c["nickname"])
                                    for c in returned_drawer["comments"]
                                )
                                prof["returnVerified"] = anchor_ok
                                profiles.append(prof)
                        except CommenterProfileSkipped as skipped:
                            skipped_profiles.append(
                                {
                                    "commentKey": candidate["key"],
                                    "nickname": candidate["nickname"],
                                    "reason": skipped.reason,
                                }
                            )
                            lines = skipped.recovered_lines or ui.snapshot("comments-after-skip")

                # 4. Determine whether collection is complete or should scroll to next page.
                first_level_count = sum(1 for c in collected.values() if c.get("commentLevel", 1) == 1)
                if drawer["empty"]:
                    end_reason = "empty_comments"
                    complete = True
                    break
                if drawer["footerReached"]:
                    end_reason = "end_of_list"
                    complete = True
                    break
                if drawer["declaredTotal"] is not None and len(collected) >= drawer["declaredTotal"] and drawer["declaredTotal"] > 0:
                    end_reason = "all_declared_collected"
                    complete = True
                    break
                if page_index + 1 >= max_pages or not hasattr(ui, "scroll_comments"):
                    end_reason = "page_limit" if page_index + 1 >= max_pages else "single_page_only"
                    complete = False
                    break

                prev_sig = _drawer_signature(drawer)
                ui.scroll_comments(lines)
                lines = ui.snapshot(f"comments-page-{page_index + 2}")
                next_drawer = parse_comment_drawer(lines, ui.height, parent_event_id=parent_id)
                for item in next_drawer["comments"]:
                    if item["key"] in expanded_reply_parents:
                        item["repliesCollected"] = True
                    collected.setdefault(item["key"], item)
                if _drawer_signature(next_drawer) == prev_sig:
                    end_reason = "end_of_list"
                    complete = True
                    break

            ordered_comments = list(collected.values())
            if not ordered_comments and (target is None or target["text"] != "0") and not drawer["empty"]:
                raise ValueError("No complete visible comments were recognized")

            resolve_reply_targets(ordered_comments, profiles)
            with timings.phase("persist_comment_drafts"):
                events = save_comments(path, video_event, ordered_comments)
                user_events = save_users(path, events, profiles)

            has_replies = any(c.get("commentLevel") == 2 or c.get("repliesCollected") for c in ordered_comments)
            coverage = "paged_with_replies" if (pages_read > 1 or has_replies) else "visible_first_level_only"
            return {
                "coverage": coverage,
                "complete": complete,
                "endReason": end_reason,
                "pagesRead": pages_read,
                "replyExpansions": reply_expansions_done,
                "repliesCollected": has_replies,
                "profiles": profiles,
                "skippedProfiles": skipped_profiles,
                "events": events,
                "userEvents": user_events,
            }
        finally:
            ui.back()
