"""Chinese step-by-step narrative, collected data extractor, and intuitive screen model builder.

Transforms raw snapshot JSONs (`001-<label>.json`), `timings.jsonl`, and result JSONs into:
1. Plain-Chinese task summaries (`taskTypeCn`, `targetSummaryCn`, `finalCollectedSummary`).
2. Chronological Chinese step cards (`stageGroup`, `titleCn`, `actionCn`, `sourceCn`,
   `collectedItems`, `highlightBoxes`, `clickTarget`, `uiTheme`).
3. De-duplicated, clean visual elements for the Phone Screen Reconstruction view.
"""

from __future__ import annotations

import re
from typing import Any

TASK_TYPE_CN = {
    "collect_channels": "视频号综合采集任务（视频 + 评论 + 评论者主页 + 上报）",
    "collect_video": "单视频与作者资料采集",
    "collect_video_comments": "单视频 + 评论区采集",
    "link_like_video": "指定链接任务：视频点赞与收藏",
    "link_follow_author": "指定链接任务：关注视频作者",
    "link_message_author": "指定链接任务：私信视频作者",
    "link_comment_video": "指定链接任务：发表视频评论",
    "link_like_comment": "指定链接任务：给指定评论点赞",
    "link_reply_comment": "指定链接任务：回复指定评论",
    "link_message_commenter": "指定链接任务：私信指定评论者",
    "inspect_messages": "视频号消息中心巡检与回复",
    "sync_discovery": "待上传数据同步确认",
    "interact_work": "作品与评论互动任务",
    "run_mode": "多模式连续浏览与采集",
    "link_task": "指定视频链接操作任务",
    "wechat_task": "微信视频号自动化任务",
}

ACTION_TO_LINK_TASK = {
    "likeVideo": "link_like_video",
    "followAuthor": "link_follow_author",
    "messageAuthor": "link_message_author",
    "commentVideo": "link_comment_video",
    "likeComment": "link_like_comment",
    "replyComment": "link_reply_comment",
    "messageCommenter": "link_message_commenter",
}

SOURCE_NAME_CN = {
    "hierarchy": "无障碍控件树直接读取（速度快、准确率高）",
    "ocr": "全屏 OCR 图像文字识别",
    "ocr-region": "局部区域 OCR 文字识别",
    "template": "图标模板图像匹配",
    "mixed": "控件树 + 局部 OCR 混合识别",
    "empty_hierarchy": "控件树为空",
}

SOURCE_SHORT_CN = {
    "hierarchy": "控件树读取",
    "ocr": "全屏 OCR",
    "ocr-region": "局部 OCR",
    "template": "图标匹配",
    "mixed": "混合识别",
    "empty_hierarchy": "空控件树",
}

STATUS_CN = {
    "done": "操作已完成", "performed": "操作已执行", "sent": "发送完成",
    "already_messaged": "已私信过该用户，跳过发送",
    "already_replied": "已回复过该评论，跳过回复",
    "already_commented": "已评论过该视频，跳过发表",
    "already_liked": "已经点赞，跳过点击", "already_collected": "已经收藏，跳过点击",
    "already_followed": "已经关注，跳过点击", "already_satisfied": "所需操作已经完成",
    "skipped": "跳过操作", "succeeded": "执行成功", "failed": "执行失败",
}

MESSAGE_STEPS = {
    "messages-start": ("检查消息巡检入口", "读取当前视频号页面，准备进入个人页。"),
    "personal-page": ("打开个人页，定位视频号消息入口", "读取个人页中的视频号消息入口。"),
    "messages-center": ("打开视频号消息中心", "读取消息中心页签，准备查看评论通知。"),
    "messages-comments": ("读取评论通知列表", "读取评论通知中的用户昵称、评论内容和回复入口。"),
    "messages-after-like": ("检查通知点赞后的页面", "读取点赞后的通知列表，供点赞状态验证使用。"),
    "messages-reply-composer": ("打开通知回复输入框", "点击通知的回复入口后，检查输入框。"),
    "messages-reply-typed": ("输入通知回复内容，准备发送", "检查已输入的回复内容和发送按钮。"),
    "messages-after-reply": ("检查通知回复发送后的页面", "读取发送后的页面，检查回复输入框是否已经关闭。"),
    "messages-return-check": ("检查消息巡检结束后的页面", "识别当前页面，确认是否需要返回视频播放页。"),
}


def get_task_type_cn(task_type: str) -> str:
    return TASK_TYPE_CN.get(task_type, f"视频号任务 ({task_type})")


def build_timeline(snapshots, timing_events, payload_files, outcome, error_message):
    steps = [dict(s, snapshotIndex=i, snapshotSequence=s["sequence"], stepKind="snapshot")
             for i, s in enumerate(snapshots)]
    untimed = []
    timed = []
    event_titles = {
        "discoverySending": "发送采集数据", "discoveryAcknowledged": "服务器确认采集数据",
        "discoveryDeferred": "数据暂缓上传", "videoCollected": "保存视频采集结果",
        "linkTaskCompleted": "链接任务执行结果", "runCompleted": "任务结束",
        "modeCompleted": "浏览任务结束", "interactionsCompleted": "互动任务结束",
        "collectionCompleted": "采集任务结束",
    }
    for event in timing_events:
        kind = event.get("kind")
        if kind not in event_titles:
            continue
        timestamp = event.get("sentAt") or event.get("acknowledgedAt")
        items = []
        for key, name in (("batchId", "上传批次"), ("eventTypes", "数据类型"),
                          ("eventIds", "发送记录"), ("discoveryAckIds", "确认记录"),
                          ("videoUrl", "视频链接"), ("missingFields", "缺少的字段"),
                          ("pendingCount", "待上传数量"), ("stage", "处理阶段"), ("error", "错误信息")):
            value = event.get(key)
            if value:
                items.append({"label": name, "value": "、".join(map(str, value)) if isinstance(value, list) else str(value)})
        status = event.get("status") or event.get("outcome")
        if status:
            items.append({"label": "执行结果", "value": STATUS_CN.get(status, status)})
        step = {"stepKind": "event", "label": kind, "stageGroup": "执行事件",
                "titleCn": event_titles[kind], "actionCn": "来自任务执行日志。",
                "sourceCn": "执行日志", "collectedItems": items, "observedAtUnixMs": timestamp,
                "timingLineIndex": event.get("_lineIndex")}
        if timestamp:
            timed.append(step)
        else:
            step["stageGroup"] = "执行记录（未记录时间）"
            untimed.append(step)
    # 保留画面编号顺序，使用设备时间插入执行事件。
    for event in sorted(timed, key=lambda s: s["observedAtUnixMs"]):
        position = next((i for i, step in enumerate(steps)
                         if step.get("hasObservedTimestamp")
                         and step["observedAtUnixMs"] > event["observedAtUnixMs"]), len(steps))
        steps.insert(position, event)
    steps.extend(untimed)
    result = payload_files.get("remote-result.json") or payload_files.get("result.json") or {}
    operation = result.get("operation") or payload_files.get("link-task.json") or {}
    items = []
    if operation.get("status"):
        status = operation["status"]
        items.append({"label": "操作结果", "value": STATUS_CN.get(status, status)})
    for key, name in (("like", "视频点赞"), ("collect", "视频收藏"), ("follow", "关注作者"),
                      ("commentLike", "评论点赞"), ("reply", "回复评论"),
                      ("comment", "发表评论"), ("message", "发送私信")):
        detail = operation.get(key)
        if not isinstance(detail, dict):
            continue
        values = []
        if detail.get("status"):
            values.append(STATUS_CN.get(detail["status"], detail["status"]))
        if "verified" in detail:
            values.append("验证通过" if detail["verified"] else "验证未通过")
        if "beforeCount" in detail and "afterCount" in detail:
            values.append(f"数量：{detail['beforeCount']} → {detail['afterCount']}")
        if detail.get("afterLabel"):
            values.append(f"按钮状态：{detail['afterLabel']}")
        text = detail.get("replyText") or detail.get("commentText")
        if text:
            values.append(text)
        if values:
            items.append({"label": name, "value": "；".join(values)})
    if result.get("message"):
        items.append({"label": "执行结论", "value": str(result["message"])})
    messages = operation.get("messages") or {}
    if "messagesFound" in messages:
        items.append({"label": "发现的通知数量", "value": str(messages["messagesFound"])})
    for message in messages.get("messagesHandled", []):
        values = [str(message.get("content") or "")]
        for key, name in (("likeStatus", "点赞"), ("replyStatus", "回复")):
            if message.get(key):
                values.append(f"{name}：{STATUS_CN.get(message[key], message[key])}")
        items.append({"label": f"通知用户 @{message.get('nickname', '')}", "value": "；".join(values)})
    if "returnedToTask" in messages:
        items.append({"label": "返回检查", "value": "已经返回任务页面" if messages["returnedToTask"] else "尚未返回任务页面"})
    if error_message:
        items.append({"label": "错误信息", "value": str(error_message)})
    if items or outcome in ("succeeded", "failed"):
        steps.append({"stepKind": "result", "label": "task-result", "stageGroup": "任务结果",
                      "titleCn": STATUS_CN.get(outcome, "任务结果"), "sourceCn": "任务结果文件",
                      "actionCn": "查看本次任务记录的操作结果。", "collectedItems": items})
    for index, step in enumerate(steps, 1):
        step["sequence"] = index
    return steps


def deduplicate_visual_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove empty container boxes and deduplicate overlapping boxes with identical text."""
    cleaned: list[dict[str, Any]] = []
    for idx, line in enumerate(lines):
        if not isinstance(line, dict):
            continue
        box = line.get("box")
        if not isinstance(box, list) or len(box) < 4:
            continue
        text = str(line.get("text") or "").strip()
        if not text:
            # Skip empty layout containers so they don't clutter the visual diagram
            continue
        x, y, w, h = box[:4]
        if w <= 2 or h <= 2:
            continue

        # Check if an identical or near-identical text box at the same vertical band already exists
        duplicate = False
        norm_text = re.sub(r"\s+", "", text)
        for existing in cleaned:
            ex, ey, ew, eh = existing["box"][:4]
            enorm = re.sub(r"\s+", "", existing["text"])
            if enorm == norm_text and abs(ey - y) <= 18 and abs(ex - x) <= max(w, ew, 120):
                duplicate = True
                break
        if not duplicate:
            cleaned.append({
                "originalIndex": idx,
                "text": text,
                "ocrText": line.get("ocrText"),
                "box": [x, y, w, h],
                "source": line.get("source", "hierarchy"),
                "score": line.get("score", 1.0),
                "selector": line.get("selector"),
            })
    return cleaned


def _find_line(lines: list[dict[str, Any]], pattern: str, predicate=None) -> dict[str, Any] | None:
    regex = re.compile(pattern)
    for l in lines:
        txt = str(l.get("text") or "").strip()
        if regex.search(txt) and (predicate is None or predicate(l)):
            return l
    return None


def enrich_run_with_chinese_narrative(
    *,
    snapshots_raw: list[tuple[dict[str, Any], dict[str, Any]]],
    payload_files: dict[str, Any],
    timing_events: list[dict[str, Any]],
    sqlite_data: dict[str, Any],
    mods: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Enrich every snapshot summary with Chinese explanations, collected data, and visual highlights.

    `snapshots_raw` is a list of `(snapshot_summary_dict, raw_json_data_dict)`.
    Returns `(enriched_snapshots, run_chinese_overview)`.
    """
    collection_mod = mods["collection"]
    comments_mod = mods["comments"]
    modes_mod = mods["modes"]

    rem_res = payload_files.get("remote-result.json") or payload_files.get("result.json") or {}
    coll_res = payload_files.get("collection.json") or {}
    vid_res = payload_files.get("video.json") or {}
    comm_res = payload_files.get("comments.json") or {}
    inter_res = payload_files.get("interactions.json") or {}
    link_res = payload_files.get("link-task.json") or {}

    operation = rem_res.get("operation") if isinstance(rem_res, dict) else None
    if not isinstance(operation, dict):
        operation = link_res if isinstance(link_res, dict) else {}

    # Track context across sequential steps
    in_commenter_flow = False
    commenter_visit_index = 0
    current_author_hint: str | None = operation.get("author")
    if not current_author_hint and isinstance(vid_res.get("payload"), dict):
        current_author_hint = vid_res["payload"].get("authorNickname")
    if not current_author_hint and isinstance(coll_res.get("videos"), list) and coll_res["videos"]:
        v0 = coll_res["videos"][0]
        if isinstance(v0, dict):
            current_author_hint = v0.get("video", {}).get("payload", {}).get("authorNickname")

    # Also find any share link extracted in the run
    known_share_link: str | None = operation.get("link")
    if not known_share_link:
        for ev in timing_events:
            if ev.get("videoUrl"):
                known_share_link = ev["videoUrl"]
                break

    enriched_steps: list[dict[str, Any]] = []
    all_collected_videos: list[dict[str, Any]] = []
    all_collected_comments: list[dict[str, Any]] = []
    all_collected_users: list[dict[str, Any]] = []

    for idx, (summary, raw_data) in enumerate(snapshots_raw):
        label = str(summary.get("label") or "")
        lines = raw_data.get("lines") or []
        height = int(summary.get("height") or 1605)
        roi = summary.get("roi") or [0, 0, 0, 0]
        fields = summary.get("fields") or {}
        attempts = summary.get("attempts") or []
        source = str(summary.get("source") or "hierarchy")

        # Determine if we are inside commenter profile sub-flow
        if label.startswith("commenter-profile"):
            if not in_commenter_flow:
                commenter_visit_index += 1
            in_commenter_flow = True
        elif label in ("comments", "comments-return", "feed", "video-details", "before-author") or label.startswith("search-"):
            if not label.startswith("commenter-"):
                in_commenter_flow = False

        # Build human-readable recognition source explanation
        fallback_reasons = [a.get("reason") for a in attempts if isinstance(a, dict) and a.get("reason")]
        if fallback_reasons:
            source_cn = f"控件树不可用 → 自动切换为{SOURCE_SHORT_CN.get(source, source)}"
        else:
            source_cn = SOURCE_SHORT_CN.get(source, source)

        stage_group = "一、页面导航与准备"
        title_cn = f"执行步骤：{label}"
        action_cn = "读取当前手机屏幕控件与文字信息。"
        ui_theme = "light"  # "light" | "dark-video" | "comment-drawer"
        collected_items: list[dict[str, str]] = []
        highlight_boxes: list[dict[str, Any]] = []
        click_target: dict[str, Any] | None = None
        roi_cn: str | None = None

        if isinstance(roi, list) and len(roi) == 4 and roi[2] > 0 and roi[3] > 0:
            rx, ry, rw, rh = roi
            if ry > height * 0.65:
                roi_cn = f"底部导航栏扫描区 (Y: {ry}~{ry + rh})"
            elif ry < 200 and rh < height * 0.4:
                roi_cn = f"顶部标题/页签扫描区 (Y: {ry}~{ry + rh})"
            else:
                roi_cn = f"限定识别区域 [{rx}, {ry}, {rw}, {rh}]"

        # --- Case 1: Entry Navigation ---
        if label == "navigation-bottom":
            stage_group = "一、打开微信并进入视频号"
            title_cn = "定位微信底部导航栏，点击【发现】"
            action_cn = "在微信首页底部导航栏区域扫描「发现」页签，点击进入发现页面。"
            ui_theme = "light"
            disc = _find_line(lines, r"^发现$", lambda l: l["box"][1] > height * 0.7)
            if disc:
                bx, by, bw, bh = disc["box"]
                click_target = {"x": bx + bw // 2, "y": by + bh // 2, "label": "点击【发现】"}

        elif label == "navigation-top":
            stage_group = "一、打开微信并进入视频号"
            ch_entry = _find_line(lines, r"^视频号$", lambda l: l["box"][1] < height * 0.5)
            rec_tab = _find_line(lines, r"^推荐$", lambda l: l["box"][1] < 240)
            if ch_entry and not rec_tab:
                title_cn = "在微信发现页点击【视频号】入口"
                action_cn = "在发现页列表中找到「视频号」入口并点击打开视频号。"
                ui_theme = "light"
                bx, by, bw, bh = ch_entry["box"]
                click_target = {"x": bx + bw // 2, "y": by + bh // 2, "label": "点击【视频号】"}
            else:
                title_cn = "进入视频号主页，确认顶部页签与播放流"
                action_cn = "已进入视频号界面，检查顶部「推荐/朋友/关注」页签及当前视频画面。"
                ui_theme = "dark-video"
                try:
                    af = collection_mod.author_in_feed(lines, height)
                    current_author_hint = af["text"].lstrip("@").strip()
                    collected_items.append({"label": "当前画面视频作者", "value": f"@{current_author_hint}"})
                    highlight_boxes.append({"box": af["box"], "label": "首屏作者"})
                except Exception:
                    pass

        elif label.startswith("resume-"):
            stage_group = "一、打开微信并进入视频号"
            title_cn = "检查页面干净状态（清理残留弹窗或半屏抽屉）"
            action_cn = "在开始执行子任务前，确认当前视频号界面没有残留的输入框、菜单或半屏弹窗。"
            ui_theme = "dark-video"

        # --- Case 2: WeChat Link Opening Flow (`link-*`) ---
        elif label.startswith("link-home"):
            stage_group = "一、通过文件传输助手打开目标视频链接"
            fh = _find_line(lines, r"^文件传输助手$")
            search_icon = _find_line(lines, r"搜索", lambda l: l["box"][1] < 160)
            ui_theme = "light"
            if label == "link-home-1":
                title_cn = "打开微信主界面，检查当前聊天列表"
                action_cn = "唤醒微信并读取首页聊天列表，准备进入「文件传输助手」或打开顶部搜索。"
            elif fh:
                title_cn = "在微信首页定位【文件传输助手】或点击顶部搜索"
                action_cn = "确认微信首页已就绪，点击进入「文件传输助手」对话窗口。"
            else:
                title_cn = "扫描微信首页列表，点击顶部搜索查找【文件传输助手】"
                action_cn = "首屏未直接定位到入口，点击顶部搜索按钮搜索「文件传输助手」。"
            if fh:
                bx, by, bw, bh = fh["box"]
                click_target = {"x": bx + bw // 2, "y": by + bh // 2, "label": "点击【文件传输助手】"}
                collected_items.append({"label": "首页匹配对话", "value": "文件传输助手", "badgeIndex": 1})
                highlight_boxes.append({"box": fh["box"], "label": "文件传输助手", "badgeIndex": 1})
            elif search_icon:
                bx, by, bw, bh = search_icon["box"]
                click_target = {"x": bx + bw // 2, "y": by + bh // 2, "label": "点击顶部搜索"}

        elif label == "link-search-typed":
            stage_group = "一、通过文件传输助手打开目标视频链接"
            title_cn = "在微信搜索框输入【文件传输助手】并选择联系人"
            action_cn = "输入「文件传输助手」后，在搜索结果列表中点击匹配的「文件传输助手」进入聊天。"
            ui_theme = "light"
            fh = _find_line(lines, r"^文件传输助手$", lambda l: l["box"][1] > 160)
            if fh:
                bx, by, bw, bh = fh["box"]
                click_target = {"x": bx + bw // 2, "y": by + bh // 2, "label": "点击搜索结果【文件传输助手】"}
                highlight_boxes.append({"box": fh["box"], "label": "匹配联系人"})
            collected_items.append({"label": "搜索目标", "value": "文件传输助手"})

        elif label == "link-chat":
            stage_group = "一、通过文件传输助手打开目标视频链接"
            title_cn = "进入【文件传输助手】聊天页，点击底部输入框"
            action_cn = "已打开文件传输助手对话页，点击底部消息输入框准备粘贴目标视频链接。"
            ui_theme = "light"
            click_target = {"x": 300, "y": height - 55, "label": "点击底部聊天输入框"}

        elif label == "link-chat-typed":
            stage_group = "一、通过文件传输助手打开目标视频链接"
            title_cn = "在输入框粘贴目标视频链接，点击【发送】"
            action_cn = "已将目标视频号分享链接填入输入框，点击右侧「发送」按钮发出消息。"
            ui_theme = "light"
            url_line = _find_line(lines, r"https://(?:weixin\.qq\.com/sph/|channels\.weixin\.qq\.com/)",
                                  lambda l: bool(known_share_link and known_share_link in l["text"]))
            if url_line:
                collected_items.append({"label": "粘贴的目标链接", "value": url_line["text"].strip()})
                highlight_boxes.append({"box": url_line["box"], "label": "已输入链接"})
            send_btn = _find_line(lines, r"^发送$", lambda l: l["box"][0] > 500)
            if send_btn:
                bx, by, bw, bh = send_btn["box"]
                click_target = {"x": bx + bw // 2, "y": by + bh // 2, "label": "点击【发送】"}

        elif label == "link-chat-sent":
            stage_group = "一、通过文件传输助手打开目标视频链接"
            title_cn = "目标链接已发送至聊天窗，点击链接跳转到视频号"
            action_cn = "在聊天记录中定位刚刚发出的最新视频号链接气泡，点击该链接直接跳转打开目标视频。"
            ui_theme = "light"
            url_lines = [
                l for l in lines
                if re.search(r"https://(?:weixin\.qq\.com/sph/|channels\.weixin\.qq\.com/)", str(l.get("text") or ""))
            ]
            if url_lines:
                latest = max(url_lines, key=lambda l: l["box"][1])
                bx, by, bw, bh = latest["box"]
                click_target = {"x": bx + bw // 2, "y": by + bh // 2, "label": "点击链接跳转视频"}
                highlight_boxes.append({"box": latest["box"], "label": "刚发送的视频链接"})
                collected_items.append({"label": "点击跳转的视频链接", "value": latest["text"].strip()})

        elif label.startswith("link-video"):
            stage_group = "二、加载并识别目标视频页"
            title_cn = "通过链接打开目标视频播放页，识别作者与作品状态"
            action_cn = "等待视频号播放器加载完成，读取当前视频的作者昵称、作品文案与底部互动栏。"
            ui_theme = "dark-video"
            try:
                af = collection_mod.author_in_feed(lines, height)
                current_author_hint = af["text"].lstrip("@").strip()
                collected_items.append({"label": "识别到视频作者", "value": f"@{current_author_hint}"})
                highlight_boxes.append({"box": af["box"], "label": f"作者: @{current_author_hint}"})
            except Exception:
                if current_author_hint:
                    collected_items.append({"label": "目标视频作者", "value": f"@{current_author_hint}"})
            # Also find caption preview if visible
            cap_lines = [
                l for l in lines
                if height * 0.55 < l["box"][1] < height - 110
                and len(str(l.get("text") or "").strip()) >= 6
                and not re.fullmatch(r"\d+|转发|赞|收藏|评论.*", str(l.get("text") or "").strip())
            ]
            if cap_lines:
                preview_txt = " ".join(l["text"].strip() for l in cap_lines[:2])
                collected_items.append({"label": "画面可见视频文案", "value": preview_txt})
                highlight_boxes.append({"box": cap_lines[0]["box"], "label": "视频文案"})

        # --- Case 3: Search Flow (`search-*`) ---
        elif label == "search-input":
            stage_group = "二、关键词搜索与视频筛选"
            title_cn = "打开视频号搜索页，准备输入搜索词"
            action_cn = "点击进入视频号右上角搜索入口，定位顶部搜索输入框。"
            ui_theme = "light"

        elif label == "search-typed":
            stage_group = "二、关键词搜索与视频筛选"
            kw_line = _find_line(
                lines,
                r"\S+",
                lambda l: l["box"][1] < 160 and l["box"][0] < 500 and l.get("text", "").strip() not in ("搜索", "取消"),
            )
            kw = kw_line["text"].strip() if kw_line else ""
            title_cn = f"输入搜索关键词「{kw or '目标词'}」，点击【搜索】"
            action_cn = f"在顶部搜索栏输入关键词「{kw}」，并点击右侧「搜索」按钮提交查询。"
            ui_theme = "light"
            if kw:
                collected_items.append({"label": "搜索关键词", "value": kw})
                if kw_line:
                    highlight_boxes.append({"box": kw_line["box"], "label": f"搜索词: {kw}"})
            sbtn = _find_line(lines, r"^搜索$", lambda l: l["box"][0] > 500 and l["box"][1] < 165)
            if sbtn:
                bx, by, bw, bh = sbtn["box"]
                click_target = {"x": bx + bw // 2, "y": by + bh // 2, "label": "点击【搜索】"}

        elif label in ("search-results", "search-results-retry", "search-video-tab") or label.startswith("search-cards"):
            stage_group = "二、关键词搜索与视频筛选"
            ui_theme = "light"
            cards = []
            try:
                cards = modes_mod.parse_search_video_cards(lines, height)
            except Exception:
                pass
            if label == "search-results" and not cards:
                title_cn = "搜索结果页加载完成，点击切换至【视频】标签页"
                action_cn = "在搜索结果顶部标签栏（全部 / 视频 / 账号 / 直播）中点击「视频」页签，只看视频结果。"
                vtab = _find_line(lines, r"^视频", lambda l: 140 <= l["box"][1] <= 245)
                if vtab:
                    bx, by, bw, bh = vtab["box"]
                    click_target = {"x": bx + bw // 2, "y": by + bh // 2, "label": "点击【视频】标签"}
                    highlight_boxes.append({"box": vtab["box"], "label": "视频标签页"})
            else:
                scroll_tag = "（翻页滚动后）" if "scroll" in label else ""
                title_cn = f"解析搜索结果视频卡片{scroll_tag}（共识别到 {len(cards)} 个视频）"
                action_cn = "读取搜索结果双列瀑布流中的视频封面卡片，提取每张卡片的标题、作者、点赞数与发布时间，并选择未采集过的视频点击打开。"
                collected_items.append({"label": "本屏识别视频卡片数", "value": f"{len(cards)} 个"})
                for c_idx, card in enumerate(cards[:6]):
                    c_summary = f"@{card.get('author', '-')}：《{card.get('title', '')[:28]}》(赞:{card.get('likes', '-')} · {card.get('publishedText', '-')})"
                    collected_items.append({"label": f"视频卡片 #{c_idx + 1}", "value": c_summary})
                    if card.get("box"):
                        highlight_boxes.append({"box": card["box"], "label": f"卡片#{c_idx + 1}: @{card.get('author')}"})
                if cards and cards[0].get("clickPoint"):
                    cx, cy = cards[0]["clickPoint"]
                    click_target = {"x": cx, "y": cy, "label": f"点击打开卡片 #1 (@{cards[0].get('author')})"}

        elif label.startswith("search-opened-video"):
            stage_group = "二、关键词搜索与视频筛选"
            title_cn = "从搜索结果点开目标视频，进入视频播放页"
            action_cn = "已点击搜索结果卡片进入单视频全屏播放页，识别当前作品作者与文案预览。"
            ui_theme = "dark-video"
            try:
                af = collection_mod.author_in_feed(lines, height)
                current_author_hint = af["text"].lstrip("@").strip()
                collected_items.append({"label": "打开的视频作者", "value": f"@{current_author_hint}"})
                highlight_boxes.append({"box": af["box"], "label": f"作者: @{current_author_hint}"})
            except Exception:
                pass

        # --- Case 4: Video Details & Caption Extraction ---
        elif label in ("video-details", "expanded-description"):
            stage_group = "三、采集视频详情（作者 / 文案 / 时间 / 地点）"
            title_cn = "展开作品详情区，采集完整视频文案、发布时间与地点"
            action_cn = "在视频详情区或评论区顶部原帖区域读取作者昵称、完整正文文案、发布时间与地理位置。"
            has_drawer_header = bool(_find_line(lines, r"^评论\s*\d+$"))
            ui_theme = "comment-drawer" if has_drawer_header else "dark-video"
            field_name_cn = {
                "authorNickname": "视频作者昵称",
                "description": "视频正文文案",
                "publishTimeText": "发布时间",
                "publishLocation": "发布地点",
            }
            badge_idx = 0
            for fk, label_cn in field_name_cn.items():
                fmeta = fields.get(fk)
                if isinstance(fmeta, dict) and fmeta.get("value"):
                    val = str(fmeta["value"])
                    if fk == "authorNickname":
                        current_author_hint = val
                    badge_idx += 1
                    collected_items.append({"label": label_cn, "value": val, "badgeIndex": badge_idx})
                    for b in (fmeta.get("boxes") or []):
                        # Avoid matching comment timestamps at the bottom of the screen for video publishTimeText
                        if fk in ("publishTimeText", "publishLocation") and b[1] > 900:
                            continue
                        highlight_boxes.append({"box": b, "label": label_cn, "badgeIndex": badge_idx})
            if not collected_items and current_author_hint:
                try:
                    vp = collection_mod.parse_video(lines, current_author_hint)
                    for fk, label_cn in field_name_cn.items():
                        if vp.get(fk):
                            badge_idx += 1
                            collected_items.append({"label": label_cn, "value": str(vp[fk]), "badgeIndex": badge_idx})
                except Exception:
                    pass
            if collected_items:
                v_entry = {item["label"]: item["value"] for item in collected_items}
                all_collected_videos.append({
                    "authorNickname": v_entry.get("视频作者昵称") or current_author_hint,
                    "description": v_entry.get("视频正文文案"),
                    "publishTimeText": v_entry.get("发布时间"),
                    "publishLocation": v_entry.get("发布地点"),
                    "videoUrl": known_share_link,
                })

        # --- Case 5: Share Link Extraction (`share-*`) ---
        elif label.startswith("share-"):
            stage_group = "四、提取作品分享短链"
            ui_theme = "dark-video"
            if label == "share-sheet":
                title_cn = "点击右侧【转发/分享】按钮，打开分享菜单弹窗"
                action_cn = "已弹出底部分享面板（转发给朋友 / 分享到朋友圈 / 复制链接），查找「复制链接」按钮。"
                ui_theme = "light"
                cp_btn = _find_line(lines, r"^复制链接$")
                if cp_btn:
                    bx, by, bw, bh = cp_btn["box"]
                    click_target = {"x": bx + bw // 2, "y": by + bh // 2, "label": "点击【复制链接】"}
                else:
                    click_target = {"x": 580, "y": height - 360, "label": "向左滑动分享菜单查找【复制链接】"}
            elif label == "share-sheet-scrolled":
                title_cn = "横向滑动分享菜单，点击【复制链接】"
                action_cn = "滑动分享图标行后定位到「复制链接」按钮，点击将作品短链复制到系统剪贴板。"
                ui_theme = "light"
                cp_btn = _find_line(lines, r"^复制链接$")
                if cp_btn:
                    bx, by, bw, bh = cp_btn["box"]
                    click_target = {"x": bx + bw // 2, "y": by + bh // 2, "label": "点击【复制链接】"}
                    highlight_boxes.append({"box": cp_btn["box"], "label": "复制链接按钮"})
            elif label in ("share-closed", "share-feed"):
                title_cn = "已复制链接到剪贴板，准备借用输入框读取链接文本"
                action_cn = "分享弹窗已关闭，打开评论输入框以粘贴方式读取系统剪贴板中的短链。"
            elif label in ("share-paste-drawer", "share-link-paste-composer", "share-link-paste"):
                title_cn = "打开评论输入框粘贴剪贴板，提取视频分享短链"
                action_cn = "将剪贴板内容粘贴至输入框（不发送），读取其中的 https://weixin.qq.com/sph/... 短链。"
                ui_theme = "comment-drawer"
                url_line = _find_line(lines, r"https://(?:weixin\.qq\.com/sph/|channels\.weixin\.qq\.com/)\S*")
                extracted_url = url_line["text"].strip() if url_line else known_share_link
                if extracted_url:
                    known_share_link = extracted_url
                    collected_items.append({"label": "提取到的作品分享短链", "value": extracted_url})
                if url_line:
                    highlight_boxes.append({"box": url_line["box"], "label": "已采集：作品短链"})
            elif label == "share-link-cleared" or label.startswith("share-return"):
                title_cn = "清空临时粘贴内容并收起输入框，回到视频播放页"
                action_cn = "删除输入框内的临时链接文字，按返回键关闭键盘与评论抽屉，恢复到视频播放界面。"
                if known_share_link:
                    collected_items.append({"label": "已确认保存的作品短链", "value": known_share_link})

        # --- Case 6: Author Profile & Basic Info ---
        elif label == "before-author":
            stage_group = "五、采集作者主页与视频号 ID"
            title_cn = f"在视频播放页点击作者【@{current_author_hint or '作者'}】进入个人主页"
            action_cn = "在视频左下角定位作者头像/昵称控件并点击，跳转到作者个人主页。"
            ui_theme = "dark-video"
            try:
                af = collection_mod.author_in_feed(lines, height)
                bx, by, bw, bh = af["box"]
                click_target = {"x": bx + min(bw, 140) // 2, "y": by + bh // 2, "label": f"点击作者【{af['text']}】"}
                highlight_boxes.append({"box": af["box"], "label": "作者昵称入口"})
            except Exception:
                pass

        elif label.startswith("author-profile"):
            stage_group = "五、采集作者主页与视频号 ID"
            title_cn = f"进入作者【@{current_author_hint or ''}】主页，采集简介、认证与属地"
            action_cn = "读取作者主页顶部的昵称、认证信息、所在地区、IP 属地及个人简介。"
            ui_theme = "light"
            field_map = {
                "authorNickname": "作者昵称",
                "authorCertification": "认证信息",
                "authorProfileLocation": "所在地区",
                "authorIpLocation": "IP 属地",
                "authorBio": "作者个人简介",
            }
            for fk, label_cn in field_map.items():
                fmeta = fields.get(fk)
                if isinstance(fmeta, dict) and fmeta.get("value"):
                    collected_items.append({"label": label_cn, "value": str(fmeta["value"])})
                    for b in (fmeta.get("boxes") or []):
                        highlight_boxes.append({"box": b, "label": f"已采集：{label_cn}"})
            if not collected_items and current_author_hint:
                try:
                    ap = collection_mod.parse_author(lines, current_author_hint)
                    for fk, label_cn in field_map.items():
                        if ap.get(fk):
                            collected_items.append({"label": label_cn, "value": str(ap[fk])})
                except Exception:
                    pass

        elif label == "profile-menu":
            stage_group = "五、采集作者主页与视频号 ID"
            title_cn = "打开作者主页右上角菜单，点击【更多信息】"
            action_cn = "在作者主页弹出底部菜单（推荐给朋友 / 投诉 / 更多信息），点击「更多信息」查看账号基础信息。"
            ui_theme = "light"
            more_btn = _find_line(lines, r"^更多信息$")
            if more_btn:
                bx, by, bw, bh = more_btn["box"]
                click_target = {"x": bx + bw // 2, "y": by + bh // 2, "label": "点击【更多信息】"}
                highlight_boxes.append({"box": more_btn["box"], "label": "更多信息入口"})

        elif label.startswith("author-basic-info"):
            if in_commenter_flow:
                stage_group = "六、采集评论区与评论者资料"
                target_who = f"第 {commenter_visit_index} 位评论者"
            else:
                stage_group = "五、采集作者主页与视频号 ID"
                target_who = f"视频作者 (@{current_author_hint})" if current_author_hint else "视频作者"
            title_cn = f"打开{target_who}的【基础信息】页，提取公开视频号 ID"
            action_cn = f"在「基础信息」页面读取{target_who}的公开视频号 ID (v2_... / sph...)、性别、地区及 IP 归属地。"
            ui_theme = "light"
            field_map = {
                "authorDouyinId": "公开视频号 ID",
                "authorIpLocation": "IP 归属地",
                "authorProfileLocation": "注册地区",
                "authorCertification": "认证主体",
            }
            for fk, label_cn in field_map.items():
                fmeta = fields.get(fk)
                if isinstance(fmeta, dict) and fmeta.get("value"):
                    collected_items.append({"label": f"{target_who} · {label_cn}", "value": str(fmeta["value"])})
                    for b in (fmeta.get("boxes") or []):
                        highlight_boxes.append({"box": b, "label": f"已采集：{label_cn}"})
            if not collected_items:
                try:
                    bp = collection_mod.parse_basic_info(lines)
                    for fk, label_cn in field_map.items():
                        if bp.get(fk):
                            collected_items.append({"label": f"{target_who} · {label_cn}", "value": str(bp[fk])})
                except Exception:
                    pass

        # --- Case 7: Comments & Commenter Profiles ---
        elif label == "comments" or label.startswith("comments-page") or label in ("comments-expanded", "comments-replies") or label.startswith("comments-locate"):
            stage_group = "六、采集评论区与评论者资料" if not operation else "三、定位目标评论并执行互动"
            ui_theme = "comment-drawer"
            parsed_drawer = None
            try:
                parsed_drawer = comments_mod.parse_comment_drawer(lines, height, parent_event_id="step")
            except Exception:
                pass

            cm_list = (parsed_drawer or {}).get("comments") or []
            decl_total = (parsed_drawer or {}).get("declaredTotal")
            if decl_total is None:
                hdr_line = _find_line(lines, r"^评论\s*(\d+)$")
                if hdr_line:
                    m_cnt = re.search(r"(\d+)", hdr_line.get("text", ""))
                    if m_cnt:
                        decl_total = int(m_cnt.group(1))
            total_str = f"共 {decl_total} 条" if decl_total is not None else f"本屏 {len(cm_list)} 条"
            if label == "comments":
                title_cn = f"打开评论区半屏抽屉（{total_str}），读取首屏评论"
                action_cn = f"展开评论区抽屉，本屏共解析出 {len(cm_list)} 条可读文本评论。"
            elif label.startswith("comments-locate"):
                page_no = label.rsplit("-", 1)[-1]
                title_cn = f"在评论区翻页查找目标评论（第 {page_no} 屏）"
                action_cn = f"向上滑动评论列表加载更多评论，本屏解析出 {len(cm_list)} 条评论并核对目标评论者昵称。"
            elif label.startswith("comments-page"):
                title_cn = f"上滑评论区翻页采集（本屏提取 {len(cm_list)} 条评论）"
                action_cn = "向上滚动评论抽屉读取新一屏评论，自动按评论 Key 去重并检查是否触底。"
            elif label == "comments-expanded":
                title_cn = "点击【展开】折叠长评，采集完整评论正文"
                action_cn = "展开被折叠的超长评论，重新读取完整评论内容。"
            else:
                title_cn = "点击【展开更多回复】，采集楼中楼二级回复"
                action_cn = "展开一级评论下方的二级回复列表，关联被回复人昵称与父评论。"

            if decl_total is not None:
                collected_items.append({"label": "评论区显示总条数", "value": f"{decl_total} 条"})
            collected_items.append({"label": "本屏解析出的评论数", "value": f"{len(cm_list)} 条"})

            for c_i, cm in enumerate(cm_list[:8]):
                lvl = "二级回复" if cm.get("commentLevel", 1) > 1 else "一级评论"
                reply_tag = f" 回复 @{cm['replyToNickname']}" if cm.get("replyToNickname") else ""
                meta_tag = " · ".join(x for x in [cm.get("ipLocation"), cm.get("commentTime"), f"{cm.get('likeCount', 0)}赞"] if x)
                c_text = f"@{cm.get('nickname')}{reply_tag} ({meta_tag})：{cm.get('content')}"
                collected_items.append({"label": f"{lvl} #{c_i + 1}", "value": c_text, "badgeIndex": len(collected_items) + 1})
                b_idx = len(collected_items)
                all_collected_comments.append({
                    "nickname": cm.get("nickname"),
                    "content": cm.get("content"),
                    "commentTime": cm.get("commentTime"),
                    "ipLocation": cm.get("ipLocation"),
                    "likeCount": cm.get("likeCount", 0),
                    "level": cm.get("commentLevel", 1),
                    "replyTo": cm.get("replyToNickname"),
                })
                if cm.get("_bodyBox"):
                    highlight_boxes.append({"box": cm["_bodyBox"], "label": f"评论#{c_i + 1}: @{cm.get('nickname')}", "badgeIndex": b_idx})

            # If a target commenter is specified in operation, highlight or set click target on it
            want_nick = operation.get("matchedNickname") or operation.get("nickname")
            if want_nick and cm_list:
                for cm in cm_list:
                    if cm.get("nickname") == want_nick:
                        if operation.get("action") == "likeComment" and cm.get("_likeBox"):
                            lx, ly, lw, lh = cm["_likeBox"]
                            click_target = {"x": lx + lw // 2, "y": ly + lh // 2, "label": f"点击给 @{want_nick} 的评论点赞"}
                        elif operation.get("action") == "messageCommenter" and not operation.get("status", "").startswith("already_") and cm.get("_nameBox"):
                            nx, ny, nw, nh = cm["_nameBox"]
                            click_target = {"x": nx + min(nw, 32) // 2, "y": ny + nh // 2, "label": f"点击 @{want_nick} 昵称左端"}
                        break
            elif not operation and cm_list and cm_list[0].get("_nameBox"):
                nx, ny, nw, nh = cm_list[0]["_nameBox"]
                click_target = {"x": nx + min(nw, 32) // 2, "y": ny + nh // 2, "label": f"点击评论者 @{cm_list[0].get('nickname')} 昵称左端 (x+16)"}

        elif label.startswith("commenter-profile"):
            stage_group = "六、采集评论区与评论者资料" if not operation else "三、访问评论者主页并执行操作"
            ui_theme = "light"
            if label == "commenter-profile-menu":
                title_cn = f"打开第 {commenter_visit_index} 位评论者右上角菜单，点击【更多信息】"
                action_cn = "在评论者主页弹出菜单中点击「更多信息」，以读取其公开视频号 ID。"
                more_btn = _find_line(lines, r"^更多信息$")
                if more_btn:
                    bx, by, bw, bh = more_btn["box"]
                    click_target = {"x": bx + bw // 2, "y": by + bh // 2, "label": "点击【更多信息】"}
                    highlight_boxes.append({"box": more_btn["box"], "label": "更多信息"})
            else:
                title_cn = f"访问第 {commenter_visit_index} 位评论者资料页，采集地区、性别与简介"
                action_cn = "点击评论者昵称打开其资料卡/个人主页，提取公开显示的地区、性别、原创作品数及私信入口。"
                # Extract visible region/gender or stats
                for l in lines:
                    txt = str(l.get("text") or "").strip()
                    if re.search(r"[男女]|条原创内容|粉丝|获赞", txt) and l["box"][1] > 200:
                        collected_items.append({"label": f"评论者 #{commenter_visit_index} 资料项", "value": txt})
                        highlight_boxes.append({"box": l["box"], "label": "评论者资料"})
                dm_btn = _find_line(lines, r"^私信$")
                if dm_btn and operation.get("action") == "messageCommenter":
                    bx, by, bw, bh = dm_btn["box"]
                    click_target = {"x": bx + bw // 2, "y": by + bh // 2, "label": "点击【私信】按钮"}

        elif label == "comments-return":
            stage_group = "六、采集评论区与评论者资料"
            title_cn = "从评论者主页返回评论区抽屉，继续处理下一条评论"
            action_cn = "按返回键退出评论者资料页，确认已回到原视频的评论区列表位置。"
            ui_theme = "comment-drawer"

        elif label.startswith("search-return"):
            stage_group = "七、返回列表并完成上报"
            title_cn = "当前视频及评论采集完毕，返回搜索结果列表页"
            action_cn = "按返回键退出视频播放页，回到搜索结果瀑布流页面。"
            ui_theme = "light"

        # --- Case 8: Interaction & DM Confirmation Steps ---
        elif label == "comments-after-like":
            stage_group = "三、定位目标评论并执行互动"
            title_cn = "点击评论点赞图标后，核对点赞数与红心状态"
            action_cn = "重新读取评论区，验证目标评论的点赞数是否 +1 或大拇指图标已变红。"
            ui_theme = "comment-drawer"
            clike = operation.get("commentLike") or inter_res.get("commentLike") or {}
            if isinstance(clike, dict) and clike:
                collected_items.append({"label": "目标评论作者", "value": str(clike.get("nickname") or "-")})
                collected_items.append({
                    "label": "点赞前后数量对比",
                    "value": f"{clike.get('beforeCount', '?')} 赞 → {clike.get('afterCount', '?')} 赞（验证: {'通过' if clike.get('verified') else '未通过'}）",
                })

        elif label in ("comments-reply-composer", "comments-composer"):
            stage_group = "三、定位目标评论并执行互动"
            title_cn = "打开视频评论输入框" if label == "comments-composer" else "打开目标评论的回复输入框"
            action_cn = "检查底部输入框，准备输入评论文字。" if label == "comments-composer" else "检查目标评论的回复输入框，准备输入回复文字。"
            ui_theme = "comment-drawer"

        elif label in ("comments-reply-typed", "comments-typed"):
            stage_group = "三、定位目标评论并执行互动"
            title_cn = "输入视频评论内容，准备点击【发送】" if label == "comments-typed" else "输入回复内容，准备点击【发送/回复】"
            action_cn = "已将预设的评论/回复文本填入输入框，点击右侧发送按钮提交。"
            ui_theme = "comment-drawer"
            send_btn = _find_line(lines, r"^(?:发送|回复)$", lambda l: l["box"][0] > 520)
            if send_btn:
                bx, by, bw, bh = send_btn["box"]
                click_target = {"x": bx + bw // 2, "y": by + bh // 2, "label": f"点击【{send_btn['text']}】"}

        elif label in ("comments-after-reply", "comments-after-post"):
            stage_group = "三、定位目标评论并执行互动"
            title_cn = "评论/回复发送完成，校验评论区展示结果"
            action_cn = "确认输入框已收起，且新发表的评论/回复已出现在评论列表中。"
            ui_theme = "comment-drawer"
            rep = operation.get("reply") or operation.get("comment") or inter_res.get("commentReply") or {}
            if isinstance(rep, dict) and rep:
                if rep.get("nickname"):
                    collected_items.append({"label": "回复目标用户", "value": f"@{rep.get('nickname')}"})
                if rep.get("replyText") or rep.get("commentText"):
                    collected_items.append({"label": "已发送内容", "value": str(rep.get("replyText") or rep.get("commentText"))})

        elif label == "profile-follow-check":
            stage_group = "四、检查个人主页状态"
            title_cn = "检查返回后的个人主页"
            action_cn = "读取当前页面，确认是否已经返回个人主页。"
            ui_theme = "light"

        elif label == "profile-after-follow":
            stage_group = "三、执行主页关注并校验状态"
            title_cn = "读取点击关注后的按钮状态"
            action_cn = "读取个人主页上的关注按钮，供操作结果验证使用。"
            ui_theme = "light"
            fbtn = _find_line(lines, r"^(?:[√✓]?\s*)?(?:已关注|互相关注|申请关注|关注)$")
            if fbtn:
                collected_items.append({"label": "当前关注按钮状态", "value": fbtn["text"].strip()})
                highlight_boxes.append({"box": fbtn["box"], "label": f"状态: {fbtn['text']}"})

        elif label in MESSAGE_STEPS or label.startswith("messages-return-"):
            stage_group = "消息中心巡检与回复"
            if label in MESSAGE_STEPS:
                title_cn, action_cn = MESSAGE_STEPS[label]
            else:
                title_cn = "从消息中心返回视频播放页"
                action_cn = "按返回键后读取页面，检查是否已经回到视频播放页。"
            if label == "messages-reply-typed":
                send_btn = _find_line(lines, r"^(发送|回复)$", lambda l: l["box"][0] > 500)
                if send_btn:
                    bx, by, bw, bh = send_btn["box"]
                    click_target = {"x": bx + bw // 2, "y": by + bh // 2, "label": "点击发送回复"}

        elif label in ("feed-after-like", "feed-after-collect"):
            stage_group = "视频点赞与收藏"
            title_cn = "读取视频点赞后的状态" if label == "feed-after-like" else "读取视频收藏后的状态"
            action_cn = "读取互动按钮和数量，操作验证结果见任务结果步骤。"
            ui_theme = "dark-video"

        elif label.startswith(("feed-after-author-touch", "link-after-dm-")):
            stage_group = "返回视频播放页"
            title_cn = "从个人主页返回视频播放页"
            action_cn = "按返回键后读取页面，检查视频播放页是否恢复。"
            ui_theme = "dark-video"

        elif label == "navigation-control-resolved":
            title_cn = "确认视频号导航入口"
            action_cn = "读取已定位的导航控件，确认进入视频号的入口。"

        elif label.startswith("dm-"):
            stage_group = "四、私信会话发送与送达校验"
            ui_theme = "light"
            if label == "dm-chat":
                title_cn = "进入私信聊天窗口，定位底部消息输入框"
                action_cn = "已打开与目标用户的私信对话页面，点击底部输入框准备输入私信文本。"
                click_target = {"x": 300, "y": height - 55, "label": "点击私信输入框"}
            elif label == "dm-chat-typed":
                title_cn = "在私信输入框填入消息内容，点击【发送】"
                action_cn = "已输入私信内容，点击右侧「发送」按钮。"
                send_btn = _find_line(lines, r"^发送$", lambda l: l["box"][0] > 500)
                if send_btn:
                    bx, by, bw, bh = send_btn["box"]
                    click_target = {"x": bx + bw // 2, "y": by + bh // 2, "label": "点击【发送】"}
            else:
                title_cn = "检查私信发送结果与系统提示"
                action_cn = "核对私信气泡是否成功送达，并检测是否出现微信频率限制或拒收提示。"
                limit_line = _find_line(lines, r"频繁|打扰|无法发送|对方拒绝|明天再试|关注后")
                if limit_line:
                    collected_items.append({"label": "⚠️ 微信系统限制提示", "value": limit_line["text"].strip()})
                    highlight_boxes.append({"box": limit_line["box"], "label": "系统限制提示"})

        elif label == "feed":
            prev_label = str(snapshots_raw[idx - 1][0].get("label") or "") if idx > 0 else ""
            if prev_label.startswith("author-basic-info") or prev_label.startswith("author-profile"):
                stage_group = "五、采集作者主页与视频号 ID"
                title_cn = "从作者主页返回视频播放页，校验回到原视频并准备打开评论区"
            elif prev_label.startswith("navigation"):
                stage_group = "一、打开微信并进入视频号"
                title_cn = "成功进入视频号推荐播放流，读取首屏画面"
            else:
                stage_group = "三、采集视频详情（作者 / 文案 / 时间 / 地点）"
                title_cn = "回到视频播放页，确认当前视频画面与底部互动栏状态"
            action_cn = "读取当前视频播放画面上的作者、文案及底部「转发 / 赞 / 收藏 / 评论」按钮位置。"
            ui_theme = "dark-video"
            try:
                af = collection_mod.author_in_feed(lines, height)
                current_author_hint = af["text"].lstrip("@").strip()
                collected_items.append({"label": "当前视频作者", "value": f"@{current_author_hint}"})
                highlight_boxes.append({"box": af["box"], "label": f"作者: @{current_author_hint}"})
            except Exception:
                pass

        # Enrich summary with the Chinese narrative fields
        summary["stageGroup"] = stage_group
        summary["titleCn"] = title_cn
        summary["actionCn"] = action_cn
        summary["sourceCn"] = source_cn
        summary["sourceFullCn"] = SOURCE_NAME_CN.get(source, source)
        summary["uiTheme"] = ui_theme
        summary["roiCn"] = roi_cn
        summary["collectedItems"] = collected_items
        summary["highlightBoxes"] = highlight_boxes
        summary["clickTarget"] = click_target
        enriched_steps.append(summary)

    # Build top-level Chinese Run Summary (`runSummaryCn`)
    task_code = str(rem_res.get("task") or "")
    if not task_code:
        action_name = (
            (operation.get("action") if isinstance(operation, dict) else None)
            or (link_res.get("action") if isinstance(link_res, dict) else None)
        )
        if not action_name:
            for ev in timing_events:
                if ev.get("action"):
                    action_name = ev["action"]
                    break
        if action_name and action_name in ACTION_TO_LINK_TASK:
            task_code = ACTION_TO_LINK_TASK[action_name]
        elif "collection.json" in payload_files:
            task_code = "collect_channels"
        elif "link-task.json" in payload_files or any(s.get("label", "").startswith("link-") for s in enriched_steps):
            snap_labels = " ".join(s.get("label", "") for s in enriched_steps)
            if "commenter-profile" in snap_labels and "dm-" in snap_labels:
                task_code = "link_message_commenter"
            elif "author-profile" in snap_labels and "dm-" in snap_labels:
                task_code = "link_message_author"
            elif "profile-after-follow" in snap_labels or "profile-follow-check" in snap_labels:
                task_code = "link_follow_author"
            elif "comments-reply" in snap_labels or "comments-after-reply" in snap_labels:
                task_code = "link_reply_comment"
            elif "comments-after-like" in snap_labels or "comments-locate" in snap_labels:
                task_code = "link_like_comment"
            elif "comments-composer" in snap_labels or "comments-after-post" in snap_labels:
                task_code = "link_comment_video"
            else:
                task_code = "link_task"
        elif "interactions.json" in payload_files:
            task_code = "interact_work"
        elif "comments.json" in payload_files:
            task_code = "collect_video_comments"
        elif "video.json" in payload_files:
            task_code = "collect_video"
        else:
            task_code = "wechat_task"

    # Collect ONLY videos/comments/users that belong to THIS run
    is_collection_run = task_code in ("collect_channels", "collect_video", "collect_video_comments", "run_mode")
    run_video_event_ids = {
        ev.get("eventId") for ev in timing_events if ev.get("kind") == "videoCollected" and ev.get("eventId")
    }
    if isinstance(vid_res, dict) and vid_res.get("event_id"):
        run_video_event_ids.add(vid_res["event_id"])

    # Merge step-extracted author profile & channelId into step-extracted video if present
    step_author_bio = None
    step_author_channel_id = None
    step_author_ip = None
    for s in enriched_steps:
        for citem in (s.get("collectedItems") or []):
            lbl = citem.get("label", "")
            if lbl == "作者个人简介":
                step_author_bio = citem.get("value")
            elif "视频作者" in lbl and "公开视频号 ID" in lbl:
                step_author_channel_id = citem.get("value")
            elif "视频作者" in lbl and "IP 归属地" in lbl:
                step_author_ip = citem.get("value")

    for v in all_collected_videos:
        if not v.get("videoUrl") and known_share_link:
            v["videoUrl"] = known_share_link
        if not v.get("authorBio") and step_author_bio:
            v["authorBio"] = step_author_bio
        if not v.get("channelId") and step_author_channel_id:
            v["channelId"] = step_author_channel_id
        if not v.get("authorIpLocation") and step_author_ip:
            v["authorIpLocation"] = step_author_ip

    if is_collection_run and not all_collected_videos and isinstance(coll_res.get("videos"), list):
        for vitem in coll_res["videos"]:
            vp = (vitem.get("video") or {}).get("payload") or {}
            if vp:
                all_collected_videos.append({
                    "authorNickname": vp.get("authorNickname"),
                    "channelId": vp.get("author_wechat_channels_id") or vp.get("authorWechatChannelsId"),
                    "description": vp.get("description"),
                    "videoUrl": vp.get("videoUrl"),
                    "publishTimeText": vp.get("publishTimeText"),
                    "publishLocation": vp.get("publishLocation"),
                    "authorBio": vp.get("authorBio"),
                    "authorIpLocation": vp.get("authorIpLocation"),
                })

    if is_collection_run and not all_collected_videos:
        sq_tree = (sqlite_data or {}).get("hierarchyTree") or []
        for vnode in sq_tree:
            vp = vnode.get("event", {}).get("payload") or {}
            matches_id = vnode.get("eventId") in run_video_event_ids
            matches_author = bool(current_author_hint and vp.get("authorNickname") == current_author_hint)
            if not (matches_id or matches_author):
                continue
            all_collected_videos.append({
                "authorNickname": vp.get("authorNickname"),
                "channelId": vp.get("author_wechat_channels_id") or vp.get("authorWechatChannelsId"),
                "description": vp.get("description"),
                "videoUrl": vp.get("videoUrl"),
                "publishTimeText": vp.get("publishTimeText"),
                "publishLocation": vp.get("publishLocation"),
                "authorBio": vp.get("authorBio"),
                "authorIpLocation": vp.get("authorIpLocation"),
                "authorProfileLocation": vp.get("authorProfileLocation"),
                "uploadState": vnode.get("uploadState"),
                "commentCount": vnode.get("commentCount", 0),
                "userCount": vnode.get("userCount", 0),
            })

    # Summarize operation / target parameters in Chinese
    operation_summary_items: list[dict[str, str]] = []
    if isinstance(rem_res, dict) and rem_res.get("message"):
        operation_summary_items.append({"label": "任务执行结论", "value": str(rem_res["message"])})
    if operation:
        if operation.get("link"):
            operation_summary_items.append({"label": "目标视频短链", "value": str(operation["link"])})
        if operation.get("author"):
            operation_summary_items.append({"label": "目标视频作者", "value": f"@{operation['author']}"})
        if operation.get("matchedNickname") or operation.get("nickname"):
            operation_summary_items.append({"label": "目标评论用户", "value": f"@{operation.get('matchedNickname') or operation.get('nickname')}"})
        if operation.get("status"):
            st_val = str(operation["status"])
            operation_summary_items.append({"label": "动作完成状态", "value": STATUS_CN.get(st_val, st_val)})
        for sub_k, sub_label in (
            ("like", "视频点赞结果"),
            ("collect", "视频收藏结果"),
            ("follow", "关注作者结果"),
            ("commentLike", "评论点赞结果"),
            ("reply", "回复评论结果"),
            ("comment", "发表评论结果"),
        ):
            sub_obj = operation.get(sub_k)
            if isinstance(sub_obj, dict):
                if "beforeCount" in sub_obj and "afterCount" in sub_obj:
                    operation_summary_items.append({
                        "label": sub_label,
                        "value": f"数量从 {sub_obj['beforeCount']} 变为 {sub_obj['afterCount']}（验证{'通过' if sub_obj.get('verified') else '未通过'}）",
                    })
                elif "beforeLabel" in sub_obj and "afterLabel" in sub_obj:
                    operation_summary_items.append({
                        "label": sub_label,
                        "value": f"按钮从「{sub_obj['beforeLabel']}」变为「{sub_obj['afterLabel']}」",
                    })
                elif sub_obj.get("replyText") or sub_obj.get("commentText"):
                    operation_summary_items.append({
                        "label": sub_label,
                        "value": f"已发送：「{sub_obj.get('replyText') or sub_obj.get('commentText')}」",
                    })

    run_overview_cn = {
        "taskCode": task_code,
        "taskTypeCn": get_task_type_cn(task_code),
        "operationItems": operation_summary_items,
        "collectedVideos": all_collected_videos[:10],
        "collectedComments": all_collected_comments[:30],
        "collectedUsers": all_collected_users[:20],
    }

    return enriched_steps, run_overview_cn
