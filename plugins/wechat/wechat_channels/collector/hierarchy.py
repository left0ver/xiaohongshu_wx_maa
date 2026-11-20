"""Fresh WeChat semantic controls first, then OCR corrected by the controls it overlaps."""

import re
import unicodedata
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

from collection import author_in_feed, find, parse_author, parse_basic_info, parse_video, profile_bio_expand
from layout import DATE, LABELS, REGION, WorkHeaderMissing, compact, find_comment_drawer_heading, is_comment_drawer_heading, label_values, reading_rows, work_region

PACKAGE = "com.tencent.mm"


class HierarchyUnavailable(ValueError):
    pass


# Descriptions that name an icon rather than repeat drawn text. OCR never sees
# them, so they are kept for locating controls but not offered as page text.
ICON_DESCRIPTIONS = {
    "暂停", "播放", "头像", "视频号logo", "搜索", "浮窗", "个人中心", "返回", "私信按钮",
    "表情", "图片", "清除搜索词", "更多功能", "取消静音", "纱罩", "camera", "消息提示管理",
}
# A single text line is drawn about this tall (720-wide pixels); TextView bounds
# often add padding, which would merge or split OCR-style reading rows.
TEXT_LINE_HEIGHT = 34


def _bounds(node):
    match = re.fullmatch(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", node.get("bounds", ""))
    return tuple(map(int, match.groups())) if match else None


def _has_label(node):
    return any(item.get("text", "").strip() or item.get("content-desc", "").strip() for item in node.iter("node"))


def _is_drawer_subtree(layer, width, height):
    scaled_height = round(height * 720 / width)
    has_seek = False
    has_heading = False
    for item in layer.iter("node"):
        if item.get("visible-to-user") == "false":
            continue
        if item.get("class") == "android.widget.SeekBar":
            has_seek = True
            break
        text = item.get("text", "").strip()
        if text and (box := _bounds(item)):
            if is_comment_drawer_heading({"text": text, "box": _scaled(box, width)}, scaled_height):
                has_heading = True
    return has_heading and not has_seek


def _is_feed_subtree(layer, width):
    top_tabs = set()
    for item in layer.iter("node"):
        if item.get("visible-to-user") == "false":
            continue
        if item.get("class") == "android.widget.SeekBar":
            return True
        desc = item.get("content-desc", "").strip()
        if desc in ("暂停", "播放", "浮窗", "个人中心"):
            return True
        label = item.get("text", "").strip() or desc
        if label in ("推荐", "关注") and (box := _bounds(item)) and _scaled(box, width)[1] < 250:
            top_tabs.add(label)
    return len(top_tabs) >= 2


def covered_layers(root, width, height):
    """Full-screen sibling subtrees drawn under another one; none when no known layering is found.

    The feed is the bottom layer (detected semantically via SeekBar/player controls),
    the comment drawer is above it, and any other full-screen sheet (profiles, menus,
    basic info) is above the drawer.
    """
    def rank(layer):
        if _is_drawer_subtree(layer, width, height):
            return 1
        if _is_feed_subtree(layer, width):
            return 0
        return 2

    covered = []
    for node in root.iter("node"):
        layers = []
        for child in node:
            box = _bounds(child) if child.tag == "node" and _has_label(child) else None
            if box and (box[2] - box[0]) * (box[3] - box[1]) >= 0.8 * width * height:
                layers.append(child)
        if len(layers) >= 2 and any(rank(layer) < 2 for layer in layers):
            top = max(rank(layer) for layer in layers)
            covered += [layer for layer in layers if rank(layer) < top]
    return covered


def _scaled(box, width):
    left, top, right, bottom = box
    scale = 720 / width
    return [round(left * scale), round(top * scale),
            max(1, round((right - left) * scale)), max(1, round((bottom - top) * scale))]


def _drawer_scroll_forward(root, hidden, width, height):
    """Whether the visible comment drawer's RecyclerView can still scroll down, or None if unknown."""
    scaled_height = round(height * 720 / width)
    visible_nodes = []
    heading_top = None
    for node in root.iter("node"):
        if id(node) in hidden:
            continue
        if (node.get("package") != PACKAGE or node.get("enabled") == "false"
                or node.get("visible-to-user") == "false"):
            continue
        box = _bounds(node)
        if not box or not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height):
            continue
        visible_nodes.append((node, box))
        text = node.get("text", "").strip()
        if heading_top is None and text:
            if is_comment_drawer_heading({"text": text, "box": _scaled(box, width)}, scaled_height):
                heading_top = box[1]
    if heading_top is None:
        return None
    scroll_flags = []
    loading = False
    for node, box in visible_nodes:
        if box[1] < heading_top:
            continue
        if node.get("class") == "android.widget.ProgressBar":
            loading = True
        elif (node.get("class") == "androidx.recyclerview.widget.RecyclerView"
                and node.get("scroll-forward") in ("true", "false")):
            scroll_flags.append(node.get("scroll-forward") == "true")
    if scroll_flags:
        return any(scroll_flags) or loading
    return None


def _structural_roles(root, hidden, parent_map, width, height):
    """Infer container kinds, control metadata, and 0-count action slots from invariant anchor topology."""
    container_kinds = {}
    node_meta = {}
    extra_lines = []
    chat_bubble_ids = set()
    scaled_height = round(height * 720 / width)

    def valid_visible(node):
        if node is None or node.tag != "node" or id(node) in hidden:
            return False
        if (node.get("package") != PACKAGE or node.get("enabled") == "false"
                or node.get("visible-to-user") == "false" or node.get("password") == "true"):
            return False
        box = _bounds(node)
        return bool(box and 0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)

    def visible_children(parent):
        return [child for child in parent if child.tag == "node" and valid_visible(child)]

    # 1. Feed playback page: anchor on visible SeekBar -> bottom main bar (author + 4 action slots).
    for node in root.iter("node"):
        if not valid_visible(node) or node.get("class") != "android.widget.SeekBar":
            continue
        sb_box = _bounds(node)
        branch = node
        bar = None
        for _ in range(3):
            parent = parent_map.get(branch)
            if parent is None:
                break
            candidates = [
                sib for sib in visible_children(parent)
                if sib is not branch
                and (b := _bounds(sib))
                and b[1] >= sb_box[1] - 24
                and (b[2] - b[0]) >= int(width * 0.8)
            ]
            if candidates:
                bar = min(candidates, key=lambda sib: _bounds(sib)[1])
                break
            branch = parent
        if bar is None:
            continue
        bar_children = visible_children(bar)
        # Left group: @author custom box (+ optional follow pill).
        left_group = next((c for c in bar_children if _bounds(c)[0] <= int(width * 0.15)), None)
        if left_group is not None:
            author_target = next(
                (c for c in visible_children(left_group) if c.get("clickable") == "true" and _bounds(c)[0] <= int(width * 0.25)),
                left_group if left_group.get("clickable") == "true" else None,
            )
            if author_target is not None:
                container_kinds[id(author_target)] = "feed-author"
        # Right group: 4 clickable action slots sorted left-to-right [like, share, collect, comment].
        right_group = next((c for c in bar_children if _bounds(c)[0] >= int(width * 0.4)), None)
        if right_group is not None:
            slots = sorted(
                (c for c in visible_children(right_group) if c.get("clickable") == "true"),
                key=lambda c: _bounds(c)[0],
            )
            if len(slots) == 4:
                for slot, (role, default_label) in zip(
                    slots,
                    (("like", "赞"), ("share", "转发"), ("collect", "收藏"), ("comment", "评论")),
                ):
                    s_box = _scaled(_bounds(slot), width)
                    img = next(
                        (d for d in slot.iter("node") if d is not slot and valid_visible(d)
                         and d.get("class", "").endswith("ImageView")),
                        None,
                    )
                    if img is not None:
                        i_box = _scaled(_bounds(img), width)
                    else:
                        iw, ih = min(48, s_box[2]), min(48, s_box[3])
                        i_box = [s_box[0] + (s_box[2] - iw) // 2, s_box[1], iw, ih]
                    tv = next(
                        (d for d in slot.iter("node") if d is not slot and valid_visible(d)
                         and d.get("class", "").endswith("TextView") and d.get("text", "").strip()),
                        None,
                    )
                    if tv is not None:
                        node_meta.setdefault(id(tv), {}).update(
                            {"feedRole": role, "buttonBox": s_box, "iconBox": i_box}
                        )
                    else:
                        extra_lines.append({
                            "text": default_label,
                            "box": s_box,
                            "score": 1.0,
                            "source": "hierarchy",
                            "feedRole": role,
                            "buttonBox": s_box,
                            "iconBox": i_box,
                        })

    # 2. Comment drawer & Profile: anchor on "作者" tag, ImageView(desc="头像"), "私信按钮", and reply-expander bars.
    comment_avatar_ancestors = set()
    profile_top_bottom = None
    profile_follow_top = None

    for node in root.iter("node"):
        if not valid_visible(node):
            continue
        desc = node.get("content-desc", "").strip()
        text = node.get("text", "").strip()
        cls = node.get("class", "")

        # Work header native author TextView left of / above "作者" tag, plus metadata siblings right of it.
        if cls.endswith("TextView") and text == "作者":
            node_meta.setdefault(id(node), {})["workRole"] = "author-tag"
            tag_parent = parent_map.get(node)
            if tag_parent is not None and valid_visible(tag_parent):
                seen_tag = False
                for sib in visible_children(tag_parent):
                    if sib is node:
                        seen_tag = True
                        continue
                    if not seen_tag or not sib.get("class", "").endswith("TextView"):
                        continue
                    stxt = sib.get("text", "").strip()
                    if not stxt or stxt == "已声明原创":
                        continue
                    srole = "date" if re.fullmatch(DATE, compact(stxt)) else "location"
                    node_meta.setdefault(id(sib), {})["workRole"] = srole
            branch = node
            for _ in range(3):
                parent = parent_map.get(branch)
                if parent is None or not valid_visible(parent):
                    break
                pb = _bounds(parent)
                if pb[3] - pb[1] > int(width * 0.25):
                    break
                for sib in visible_children(parent):
                    if sib is branch:
                        break
                    nick_tv = next(
                        (d for d in sib.iter("node") if valid_visible(d)
                         and d.get("class", "").endswith("TextView")
                         and d.get("clickable") == "true"
                         and d.get("text", "").strip()
                         and d.get("text", "").strip() not in ("作者", "已声明原创")),
                        None,
                    )
                    if nick_tv is not None:
                        node_meta.setdefault(id(nick_tv), {}).update(
                            {"workAuthorControl": True, "workRole": "author"}
                        )
                        break
                branch = parent

        # Profile message button -> sibling follow button.
        if desc in ("私信按钮", "客服按钮"):
            node_meta.setdefault(id(node), {})["profileRole"] = "action-button"
            msg_box = _bounds(node)
            profile_follow_top = min(profile_follow_top or msg_box[1], msg_box[1])
            btn_bar = parent_map.get(node)
            if btn_bar is not None:
                for sib in visible_children(btn_bar):
                    if sib is node:
                        continue
                    sb = _bounds(sib)
                    if sb[0] < msg_box[0] and abs(sb[1] - msg_box[1]) <= int(width * 0.05):
                        container_kinds[id(sib)] = "follow-button"

        # Avatar anchor: distinguish Profile top bar (has "设置" on right) vs Comment item (has header + right clickable like).
        if desc == "头像":
            av_scaled = _scaled(_bounds(node), width)
            parent = parent_map.get(node)
            if parent is None or av_scaled[0] >= 240:
                continue
            siblings = [sib for sib in visible_children(parent) if sib is not node]
            settings_sibs = [
                sib for sib in siblings
                if _scaled(_bounds(sib), width)[0] >= 500
                and any(d.get("content-desc", "").strip() == "设置" for d in sib.iter("node") if valid_visible(d))
            ]
            if settings_sibs:
                for ss in settings_sibs:
                    for d in ss.iter("node"):
                        if valid_visible(d) and d.get("content-desc", "").strip() == "设置":
                            node_meta.setdefault(id(d), {})["profileRole"] = "action-button"
                pb = _bounds(parent)
                profile_top_bottom = max(profile_top_bottom or pb[3], pb[3])
                mid = next(
                    (sib for sib in siblings if 150 <= _scaled(_bounds(sib), width)[0] < 540),
                    None,
                )
                if mid is not None:
                    for d in mid.iter("node"):
                        if valid_visible(d) and d.get("class", "").endswith("TextView") and d.get("text", "").strip():
                            node_meta.setdefault(id(d), {})["profileRole"] = "location"
                    mid_children = visible_children(mid)
                    name_target = mid_children[0] if mid_children else mid
                    container_kinds[id(name_target)] = "profile-name"
                    name_bottom = _bounds(name_target)[3]
                    for extra_child in mid_children[1:]:
                        eb = _bounds(extra_child)
                        if eb[1] >= name_bottom - 8 and not any(
                            d.get("text", "").strip() for d in extra_child.iter("node") if valid_visible(d)
                        ):
                            container_kinds[id(extra_child)] = "certification"
                            break
                continue

            # Ordinary WeChat user bottom card: header bar above avatar contains "设置",
            # and a right-side sibling ViewGroup holds nickname / location / gender / bio.
            av_box = _bounds(node)
            if av_box and av_box[1] > int(height * 0.5):
                header_above = next(
                    (
                        sib for sib in siblings
                        if (hb := _bounds(sib)) and hb[3] <= av_box[1] + int(height * 0.02)
                        and any(d.get("content-desc", "").strip() == "设置" for d in sib.iter("node") if valid_visible(d))
                    ),
                    None,
                )
                info_group = next(
                    (
                        sib for sib in siblings
                        if sib is not header_above
                        and (ib := _bounds(sib)) and ib[0] >= av_box[2] and ib[1] < av_box[3] and ib[3] > av_box[1]
                        and any(
                            c.get("class", "").endswith("TextView") and c.get("text", "").strip()
                            for c in visible_children(sib)
                        )
                    ),
                    None,
                )
                if header_above is not None and info_group is not None:
                    ib = _bounds(info_group)
                    iw = max(1, ib[2] - ib[0])
                    text_views = sorted(
                        (
                            c for c in visible_children(info_group)
                            if c.get("class", "").endswith("TextView") and c.get("text", "").strip() and _bounds(c)
                        ),
                        key=lambda c: (_bounds(c)[1], _bounds(c)[0]),
                    )
                    if text_views:
                        nick_node = text_views[0]
                        nb = _bounds(nick_node)
                        nick_bottom = nb[3]
                        nick_h = max(1, nb[3] - nb[1])
                        node_meta.setdefault(id(nick_node), {})["userCardRole"] = "nickname"
                        rest = text_views[1:]
                        gender_node = next(
                            (
                                c for c in rest
                                if c.get("text", "").strip() in ("男", "女")
                                and ((_bounds(c)[2] - _bounds(c)[0]) / iw) < 0.5
                            ),
                            None,
                        )
                        if gender_node is not None:
                            node_meta.setdefault(id(gender_node), {})["userCardRole"] = "gender"
                        others = [c for c in rest if c is not gender_node]
                        if gender_node is not None:
                            gb = _bounds(gender_node)
                            g_h = max(1, gb[3] - gb[1])
                            g_cy = (gb[1] + gb[3]) / 2.0
                            for c in others:
                                cb = _bounds(c)
                                c_h = max(1, cb[3] - cb[1])
                                c_cy = (cb[1] + cb[3]) / 2.0
                                if abs(c_cy - g_cy) < min(c_h, g_h) * 0.5 and cb[0] < gb[0]:
                                    node_meta.setdefault(id(c), {})["userCardRole"] = "location"
                                elif cb[1] >= gb[3] - int(g_h * 0.15):
                                    node_meta.setdefault(id(c), {})["userCardRole"] = "bio"
                        elif len(others) >= 2:
                            node_meta.setdefault(id(others[0]), {})["userCardRole"] = "location"
                            node_meta.setdefault(id(others[1]), {})["userCardRole"] = "bio"
                        elif len(others) == 1:
                            only = others[0]
                            ob = _bounds(only)
                            w_ratio = (ob[2] - ob[0]) / iw
                            gap_ratio = (ob[1] - nick_bottom) / nick_h
                            role = "bio" if (w_ratio >= 0.75 or gap_ratio >= 0.75) else "location"
                            node_meta.setdefault(id(only), {})["userCardRole"] = role
                        continue

            # Comment item or ad item: header group + right-side clickable container.
            # On 1236px Huawei screens a 0-like button can overhang the right edge by a few px (e.g. 1239).
            right_btn = next(
                (
                    sib for sib in parent
                    if sib is not node and sib.tag == "node" and id(sib) not in hidden
                    and sib.get("package") == PACKAGE and sib.get("enabled") != "false"
                    and sib.get("visible-to-user") != "false" and sib.get("clickable") == "true"
                    and (sb := _bounds(sib)) is not None and 0 <= sb[0] < sb[2] and 0 <= sb[1] < sb[3] <= height
                    and _scaled(sb, width)[0] >= 460
                ),
                None,
            )
            header_group = next(
                (sib for sib in siblings
                 if sib is not right_btn and 75 <= _scaled(_bounds(sib), width)[0] < 450
                 and any(d.get("class", "").endswith("TextView") and d.get("text", "").strip()
                         for d in visible_children(sib))),
                None,
            )
            if header_group is not None and right_btn is not None:
                tvs = [
                    d for d in visible_children(header_group)
                    if d.get("class", "").endswith("TextView") and d.get("text", "").strip()
                ]
                # Ad item in comment drawer: right-side button is an empty clickable FrameLayout ("广告")
                # and header has only the brand name (no date TextView).
                is_empty_frame = (
                    right_btn.get("class") == "android.widget.FrameLayout"
                    and len(right_btn) == 0
                    and not right_btn.get("text", "").strip()
                    and not right_btn.get("content-desc", "").strip()
                )
                if is_empty_frame and not any(re.fullmatch(DATE, compact(d.get("text", ""))) for d in tvs):
                    node_meta.setdefault(id(right_btn), {})["directText"] = "广告"
                    continue
                like_btn = right_btn

                grandparent = parent_map.get(parent)
                gp_cls = (grandparent.get("class") or "") if grandparent is not None else ""
                is_item_container = (
                    grandparent is not None
                    and parent_map.get(grandparent) is not None
                    and (gpb := _bounds(grandparent)) is not None
                    and (pb := _bounds(parent)) is not None
                    and (gpb[3] - gpb[1]) <= max((pb[3] - pb[1]) * 2, int(height * 0.45))
                )
                if is_item_container and gp_cls.endswith("FrameLayout"):
                    structural_level = 2
                elif is_item_container and gp_cls.endswith("LinearLayout"):
                    structural_level = 1
                else:
                    structural_level = None

                anc = parent
                for _ in range(4):
                    if anc is None:
                        break
                    comment_avatar_ancestors.add(id(anc))
                    anc = parent_map.get(anc)

                nick_tv = next((d for d in tvs if d.get("clickable") == "true"), None)
                if nick_tv is None and len(tvs) >= 2 and not re.fullmatch(DATE, compact(tvs[0].get("text", ""))):
                    # Non-clickable nickname fallback only when first TextView is not a region+date pair.
                    if len(tvs) >= 3 or not re.fullmatch(DATE, compact(tvs[-1].get("text", ""))):
                        nick_tv = tvs[0]
                date_tv = next(
                    (d for d in reversed(tvs) if d is not nick_tv and re.fullmatch(DATE, compact(d.get("text", "")))),
                    tvs[-1] if len(tvs) >= 2 and tvs[-1] is not nick_tv else None,
                )
                has_badge = any(
                    d.get("content-desc", "").strip() == "视频号logo"
                    for d in visible_children(header_group)
                )
                # Compute precise thumb box & count control inside like_btn.
                thumb_img = next(
                    (d for d in like_btn.iter("node") if d is not like_btn and valid_visible(d)
                     and d.get("class", "").endswith("ImageView")),
                    None,
                )
                like_cnt_tv = next(
                    (d for d in like_btn.iter("node") if d is not like_btn and valid_visible(d)
                     and d.get("class", "").endswith("TextView") and d.get("text", "").strip()),
                    None,
                )
                if like_cnt_tv is not None:
                    node_meta.setdefault(id(like_cnt_tv), {})["commentRole"] = "like-count"
                if thumb_img is not None:
                    tb = _scaled(_bounds(thumb_img), width)
                    tw, th = max(36, tb[2]), max(34, tb[3])
                    c_like_box = [tb[0] - (tw - tb[2]) // 2, tb[1] - (th - tb[3]) // 2, tw, th]
                else:
                    lb = _scaled(_bounds(like_btn), width)
                    lw, lh = min(40, lb[2]), min(40, lb[3])
                    c_like_box = [lb[0] + (lb[2] - lw) // 2, lb[1] + (lb[3] - lh) // 2, lw, lh]

                if nick_tv is not None:
                    meta = node_meta.setdefault(id(nick_tv), {})
                    meta["commentNicknameControl"] = True
                    meta["commentRole"] = "nickname"
                    meta["commentLikeBox"] = c_like_box
                    if structural_level is not None:
                        meta["commentLevel"] = structural_level
                    if has_badge:
                        meta["hasChannelsBadge"] = True
                if date_tv is not None:
                    dmeta = node_meta.setdefault(id(date_tv), {})
                    dmeta["commentHeaderDate"] = True
                    dmeta["commentRole"] = "date"
                    if nick_tv is None:
                        dmeta["commentLikeBox"] = c_like_box
                        if structural_level is not None:
                            dmeta["commentLevel"] = structural_level
                for mid_tv in tvs:
                    if mid_tv is nick_tv or mid_tv is date_tv:
                        continue
                    mtxt = compact(mid_tv.get("text", ""))
                    if mtxt == "作者":
                        mrole = "author-tag"
                    elif re.fullmatch(rf"(?:{REGION})(?:省|市|自治区|特别行政区)?|我", mtxt):
                        mrole = "region"
                    else:
                        mrole = "badge"
                    node_meta.setdefault(id(mid_tv), {})["commentRole"] = mrole

                hb = _bounds(header_group)
                for sib in siblings:
                    if sib in (header_group, like_btn):
                        continue
                    sb = _bounds(sib)
                    if sb[1] >= hb[3] - 20 and sib.get("class") == "android.widget.RelativeLayout":
                        container_kinds[id(sib)] = "comment-body"
                        for body_tv in sib.iter("node"):
                            if valid_visible(body_tv) and body_tv.get("class", "").endswith("TextView") and body_tv.get("text", "").strip():
                                node_meta.setdefault(id(body_tv), {})["commentRole"] = "body"

        # Chat self-avatar -> mark sibling TextView bubbles in same chat list row to preserve controlBox.
        if cls.endswith("ImageView") and desc.endswith("头像") and desc != "头像" and _scaled(_bounds(node), width)[0] >= 500:
            row_anc = parent_map.get(node)
            for _ in range(3):
                if row_anc is None:
                    break
                rb = _bounds(row_anc)
                if rb and (rb[2] - rb[0]) >= int(width * 0.85):
                    for d in row_anc.iter("node"):
                        if valid_visible(d) and d.get("class", "").endswith("TextView") and d.get("text", "").strip():
                            chat_bubble_ids.add(id(d))
                    break
                row_anc = parent_map.get(row_anc)

    # Profile bio wrapper detection when profile anchors are present.
    if profile_top_bottom is not None and profile_follow_top is not None and profile_follow_top > profile_top_bottom:
        bio_limit = profile_follow_top
        for node in root.iter("node"):
            if not valid_visible(node) or not node.get("class", "").endswith("TextView"):
                continue
            txt = node.get("text", "").strip()
            b = _bounds(node)
            if profile_top_bottom <= b[1] < profile_follow_top and re.fullmatch(
                r"\d+(?:\.\d+)?[万wW]?\s*(?:条原创内容|条朋友圈内容|作品|粉丝|获赞)|音乐电台(?:.*人在听)?|商品橱窗|没有更多动态",
                txt,
            ):
                node_meta.setdefault(id(node), {})["profileRole"] = "boundary"
                bio_limit = min(bio_limit, b[1])
        for node in root.iter("node"):
            if not valid_visible(node):
                continue
            b = _bounds(node)
            sb = _scaled(b, width)
            if sb[0] <= 70 and profile_top_bottom - 8 <= b[1] < bio_limit - 4 and b[3] <= bio_limit + 6:
                txt = node.get("text", "").strip()
                if re.search(r"IP\s*(?:归)?属地", txt):
                    continue
                if node.get("class", "").endswith("TextView") and txt:
                    node_meta.setdefault(id(node), {})["profileRole"] = "bio"
                    container_kinds.setdefault(id(node), "bio")
                elif sb[2] >= 180 and sb[3] >= 16 and len(node) == 0 and not txt and not node.get("content-desc", "").strip():
                    container_kinds.setdefault(id(node), "bio")

    # 3. Comment reply-expander bar & Message-center 2-button bar ("回复" + thumb-like).
    for node in root.iter("node"):
        if not valid_visible(node):
            continue
        box = _bounds(node)
        sb = _scaled(box, width)

        # Reply expander: full-width empty clickable FrameLayout beneath a comment item.
        is_structural_expander = (
            node.get("class") == "android.widget.FrameLayout"
            and node.get("clickable") == "true"
            and len(node) == 0
            and not node.get("text", "").strip()
            and not node.get("content-desc", "").strip()
            and box[0] == 0
            and box[2] == width
            and 22 <= sb[3] <= 105
            and (p := parent_map.get(node)) is not None
            and any(id(sib) in comment_avatar_ancestors for sib in visible_children(p) if sib is not node)
        )
        if is_structural_expander:
            lh = min(sb[3], TEXT_LINE_HEIGHT)
            node_meta.setdefault(id(node), {}).update({
                "directText": "展开更多回复",
                "directBox": [96, sb[1] + (sb[3] - lh) // 2, 160, lh],
            })
            continue

        # Message center horizontal 2-button bar: [child(0)="回复", child(1)=thumb-like].
        if 95 <= sb[0] <= 185 and 150 < sb[1] < scaled_height - 70:
            clicks = [
                c for c in visible_children(node)
                if c.get("clickable") == "true"
                and not c.get("text", "").strip()
                and not c.get("content-desc", "").strip()
                and not any(
                    d.get("text", "").strip() or d.get("content-desc", "").strip()
                    for d in c.iter("node") if valid_visible(d)
                )
            ]
            if len(clicks) == 2:
                b0, b1 = _bounds(clicks[0]), _bounds(clicks[1])
                s0, s1 = _scaled(b0, width), _scaled(b1, width)
                if (
                    abs(s0[1] - s1[1]) <= 12
                    and s1[0] >= s0[0] + s0[2] - 8
                    and 75 <= s0[2] <= 155
                    and 45 <= s1[2] <= 130
                    and 42 <= s0[3] <= 105
                ):
                    anc = parent_map.get(node)
                    has_msg_header = False
                    for _ in range(2):
                        if anc is None:
                            break
                        if any(
                            valid_visible(d)
                            and d.get("class", "").endswith("TextView")
                            and d.get("text", "").strip()
                            and _bounds(d)[3] <= b0[1] + 12
                            for d in anc.iter("node")
                        ):
                            has_msg_header = True
                            break
                        anc = parent_map.get(anc)
                    if has_msg_header:
                        lw, lh = min(48, s1[2]), min(48, s1[3])
                        like_box = [s1[0] + (s1[2] - lw) // 2, s1[1] + (s1[3] - lh) // 2, lw, lh]
                        node_meta.setdefault(id(clicks[0]), {}).update({
                            "directText": "回复",
                            "messageLikeBox": like_box,
                        })

    return container_kinds, node_meta, extra_lines, chat_bubble_ids


def text_lines(xml, width, height, containers=False):
    """Visible WeChat controls as OCR-like lines; with containers, also empty text containers.

    Text is kept exactly as the control reports it. A container entry has an
    empty text, a "container" kind and the container bounds.
    """
    if len(xml) > 4_000_000 or "<!DOCTYPE" in xml or "<!ENTITY" in xml:
        raise HierarchyUnavailable("Invalid hierarchy document")
    if width <= 0 or height <= width:
        raise RuntimeError("Collection requires a portrait physical display")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise HierarchyUnavailable("Incomplete hierarchy document") from error
    lines, holes = [], []
    hidden = {id(node) for layer in covered_layers(root, width, height) for node in layer.iter("node")}
    parent_map = {child: parent for parent in root.iter("node") for child in parent}
    drawer_scroll_forward = _drawer_scroll_forward(root, hidden, width, height)
    structural_kinds, node_meta, extra_lines, chat_bubble_ids = _structural_roles(
        root, hidden, parent_map, width, height
    )
    scaled_height = round(height * 720 / width)
    for node in root.iter("node"):
        if id(node) in hidden:
            continue
        if (node.get("package") != PACKAGE or node.get("enabled") == "false"
                or node.get("visible-to-user") == "false" or node.get("password") == "true"):
            continue
        box = _bounds(node)
        if not box or not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height):
            continue
        if node.get("class") == "android.widget.ProgressBar":
            resource = node.get("resource-id", "")
            lines.append({"text": "", "box": _scaled(box, width), "source": "hierarchy", "icon": True,
                          "selector": {"attribute": "resource-id", "value": resource,
                                       "resource": resource, "class": node.get("class")}})
            continue
        descendants = [child for child in node.iter("node") if child is not node]
        meta = node_meta.get(id(node), {})
        msg_btn_direct = node.get("content-desc", "").strip() == "私信按钮"
        kind = None if msg_btn_direct else structural_kinds.get(id(node))
        if kind:
            if kind == "feed-author":
                # Ignore avatar-column "直播中" pills so live-author VODs still expose the nickname hole.
                has_text_child = any(
                    child.get("text", "").strip()
                    and child.get("text", "").strip() != "直播中"
                    and ((cb := _bounds(child)) is None or _scaled(cb, width)[0] >= 88)
                    for child in descendants
                )
            elif kind == "bio":
                has_text_child = False
            else:
                has_text_child = any(child.get("text", "").strip() for child in descendants)
            if not has_text_child:
                hole_box = _scaled(box, width)
                if kind == "feed-author" and hole_box[0] < 96 and hole_box[0] + hole_box[2] > 110:
                    right = hole_box[0] + hole_box[2]
                    hole_box = [96, hole_box[1], right - 96, hole_box[3]]
                holes.append({"text": "", "container": kind, "box": hole_box, "source": "hierarchy"})
        text = node.get("text", "").strip()
        if text and descendants:
            child_text = "".join(child.get("text", "").strip() for child in descendants)
            if child_text and re.sub(r"\s+", "", text) == re.sub(r"\s+", "", child_text):
                continue
        # Container descriptions can summarize children and duplicate whole
        # pages; only a container without labelled descendants speaks for itself.
        attribute = "text"
        direct_only = False
        if msg_btn_direct and not text:
            attribute = "content-desc"
            text = "私信"
        elif not text and meta.get("directText"):
            text = meta["directText"]
            direct_only = True
        elif not text and not any(child.get("text", "").strip()
                                  or child.get("content-desc", "").strip() not in ("", *ICON_DESCRIPTIONS)
                                  for child in descendants):
            desc_text = node.get("content-desc", "").strip()
            if desc_text:
                attribute = "content-desc"
                text = desc_text
        if not text:
            continue
        if re.search(r"请完成.*验证|拖动滑块|安全验证|验证码", text):
            raise RuntimeError("Verification detected; manual intervention required")
        raw_scaled = _scaled(box, width)
        scaled = meta.get("directBox") or raw_scaled
        if not meta.get("directBox") and attribute == "text" and "\n" not in text and scaled[3] > TEXT_LINE_HEIGHT + 10:
            # Keep the vertical centre: a padded label must share the reading
            # row of the labels beside it, as OCR boxes do.
            scaled = [scaled[0], scaled[1] + (scaled[3] - TEXT_LINE_HEIGHT) // 2, scaled[2], TEXT_LINE_HEIGHT]
        line = {"text": text, "box": scaled, "score": 1.0, "source": "hierarchy"}
        if not direct_only:
            line["selector"] = {"attribute": attribute, "value": node.get(attribute),
                                "resource": node.get("resource-id", ""), "class": node.get("class", "")}
        elif node.get("resource-id"):
            line["selector"] = {"attribute": "resource-id", "value": node.get("resource-id"),
                                "resource": node.get("resource-id", ""), "class": node.get("class", "")}
        if node.get("selected") in ("true", "false"):
            line["selected"] = node.get("selected") == "true"
        if drawer_scroll_forward is not None and is_comment_drawer_heading(line, scaled_height):
            line["canScrollForward"] = drawer_scroll_forward
        if id(node) in chat_bubble_ids and scaled != raw_scaled:
            # 聊天消息保留完整控件范围，用于关联头像和发送失败按钮。
            line["controlBox"] = raw_scaled
        for meta_key in ("commentNicknameControl", "hasChannelsBadge", "commentHeaderDate",
                         "commentLikeBox", "commentRole", "commentLevel", "workAuthorControl", "workRole",
                         "profileRole", "userCardRole", "feedRole", "buttonBox", "iconBox", "messageLikeBox"):
            if meta_key in meta:
                line[meta_key] = meta[meta_key]
        if attribute == "content-desc" and (
            text in ICON_DESCRIPTIONS
            or (node.get("class", "").endswith("ImageView") and re.fullmatch(r"\[[^\[\]\s]{1,12}\]", text))
        ):
            line["icon"] = True
        lines.append(line)
    lines.extend(extra_lines)
    if not lines:
        raise HierarchyUnavailable("No visible WeChat text controls")
    # Identical nested labels are one visual control, not two author candidates.
    lines = list({(line["text"], tuple(line["box"])): line for line in lines}.values())
    holes = list({(hole["container"], tuple(hole["box"])): hole for hole in holes}.values())
    return lines + holes if containers else lines


def fill_containers(lines, recognized):
    """Put OCR text into the empty containers it falls in; nothing else is taken from OCR.

    OCR only adds what the controls do not show: a reading that lies on a text
    control is dropped. Descriptions inside a container name the control, not
    the drawn text, so OCR replaces them. A bio control can hold only its first
    lines, so there the more complete of control text and OCR is kept.
    Each OCR line fills at most one container.
    """
    holes = [line for line in lines if line.get("container")]
    controls = [line for line in lines if not line.get("container")]
    texts = [item for item in controls if item.get("selector", {}).get("attribute") == "text"]

    def inside(item, hole):
        x, y, w, h = item["box"]
        cx, cy = x + w / 2, y + h / 2
        hx, hy, hw, hh = hole["box"]
        return hx - 4 <= cx <= hx + hw + 4 and hy - 4 <= cy <= hy + hh + 4

    def text_column(hole):
        # A comment body container spans the avatar column too; its text starts
        # under the nickname row above it, so OCR left of that is the avatar.
        if hole["container"] != "comment-body":
            return -1
        x, y = hole["box"][0], hole["box"][1]
        header = [item["box"][0] for item in controls
                  if 0 <= y - (item["box"][1] + item["box"][3]) <= 60 and item["box"][0] > x]
        return min(header) - 8 if header else x

    read = lambda items: len(re.sub(r"\s+", "", "".join(item["text"] for item in items)))
    taken, used, dropped = [], set(), set()
    for hole in holes:
        left = text_column(hole)
        found = [item for item in recognized
                 if id(item) not in used and inside(item, hole) and item["box"][0] >= left]
        own = [item for item in controls if inside(item, hole)]
        own_text = [item for item in own if item in texts]
        if hole["container"] == "bio" and own_text:
            if not found or read(own_text) >= read(found):
                used.update(id(item) for item in found)
                continue
            dropped.update(id(item) for item in own_text)
        else:
            found = [item for item in found if not any(inside(item, text) for text in texts)]
        if not found:
            continue
        dropped.update(id(item) for item in own if item not in texts)
        used.update(id(item) for item in found)
        taken += [{**item, "filledContainer": hole["container"]} for item in found]
    return [item for item in controls if id(item) not in dropped] + taken


def container_roi(lines, height):
    """One OCR region covering every empty container, or None."""
    holes = [line["box"] for line in lines if line.get("container")]
    if not holes:
        return None
    left = max(0, min(box[0] for box in holes))
    top = max(0, min(box[1] for box in holes))
    right = min(720, max(box[0] + box[2] for box in holes))
    bottom = min(height, max(box[1] + box[3] for box in holes))
    return [left, top, max(1, right - left), max(1, bottom - top)] if right > left and bottom > top else None


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
        scroll_flag = next((node["canScrollForward"] for node in nodes if "canScrollForward" in node), None)
        for line, chars in zip(lines, output):
            text = "".join(chars)
            updated = corrected[id(line)]
            if text and key(text) != key(re.sub(r"\s+", "", line["text"])):
                updated = {**updated, "text": text, "ocrText": line["text"], "source": "hierarchy"}
            if scroll_flag is not None and is_comment_drawer_heading(updated):
                updated = {**updated, "canScrollForward": scroll_flag}
            corrected[id(line)] = updated
    return [corrected[id(line)] for line in recognized]


def refresh_target(lines, previous):
    role = previous.get("feedRole")
    if role:
        candidates = [line for line in lines if line.get("feedRole") == role]
    else:
        candidates = [line for line in lines if line.get("selector") == previous.get("selector")]
    if len(candidates) != 1:
        raise RuntimeError("Control is missing or ambiguous; no input was sent")
    matched = candidates[0]
    use_button_box = bool(previous.get("buttonBox") and previous["box"] == previous["buttonBox"])
    candidate_box = list(matched.get("buttonBox") or matched["box"]) if use_button_box else matched["box"]
    # A few pixels come from re-rounding padded bounds; a real move is larger.
    if max(abs(a - b) for a, b in zip(candidate_box, previous["box"])) > 6:
        raise RuntimeError("Control moved before click; no input was sent")
    return {**matched, "box": candidate_box} if use_button_box else matched


def channels_intro_acknowledgement(lines):
    notice = "为呵护未成年人健康成长，微信推出未成年人模式。该模式下部分功能将受限制使用，请监护人主动设置。"
    if not any(line.get("text") == notice for line in lines):
        return None
    buttons = [line for line in lines if line.get("text") == "我知道了"
               and line.get("selector", {}).get("class") == "android.widget.Button"]
    if len(buttons) != 1:
        raise RuntimeError("视频号介绍提示的确认按钮缺失或不唯一")
    return buttons[0]


def page_lines(lines):
    """The drawn text parsers read: no icon names or empty-container markers."""
    return [line for line in lines if not line.get("icon") and not line.get("container")]


def _feed_ready(lines, height, author):
    author_in_feed(lines, height)
    return True


def _drawer_ready(lines, height, author):
    try:
        work_region(lines)
        return True
    except WorkHeaderMissing:
        return bool(find_comment_drawer_heading(lines, height)) and len(lines) >= 4


def _profile_ready(lines, height, author):
    parse_author(lines, author or "")
    return True


def _profile_follow_ready(lines, height, author):
    from interactions import profile_follow_state
    profile_follow_state(lines)
    return True


def _comments_ready(lines, height, author):
    from comments import parse_comment_drawer
    parse_comment_drawer(lines, height)
    return True


def _profile_or_card_ready(lines, height, author):
    # A commenter without a Channels account opens a WeChat user card instead.
    from comments import user_card_open
    return user_card_open(lines, height) or _profile_ready(lines, height, author)


def _known_page(lines, height, author):
    from interactions import classify_page_state
    return (
        classify_page_state(lines, height) != "unknown"
        or _search_results_ready(lines, height, author)
        or _search_input_ready(lines, height, author)
    )


def _page_state(*states):
    def ready(lines, height, author):
        from interactions import classify_page_state
        return classify_page_state(lines, height) in states
    return ready


def _search_input_ready(lines, height, author):
    from modes import is_search_input_page
    return is_search_input_page(lines)


def _search_typed_ready(lines, height, author):
    return bool(find(lines, r"^搜索$", lambda line: line["box"][0] > 490 and line["box"][1] < 165))


def _search_results_ready(lines, height, author):
    from modes import is_search_results_page
    return is_search_results_page(lines)


def _search_filter_ready(lines, height, author):
    return bool(find(lines, r"^排序$", lambda line: line["box"][1] < 360))


def _search_sheet_ready(lines, height, author):
    return bool(find(lines, r"^(?:不限|5分钟以下|5-20分钟|20分钟以上|已关注|最近看过)$",
                     lambda line: line["box"][1] > 1150))


def _navigation_ready(lines, height, author):
    home = find(lines, r"^发现$", lambda line: line["box"][1] > height - 180)
    discover = find(lines, r"^发现$", lambda line: line["box"][1] < 250)
    return bool((home and find(lines, r"^通讯录$"))
                or (discover and find(lines, r"^视频号$"))
                or (find(lines, r"^推荐$") and find(lines, r"^[<＜《‹«]?\s*关注$") and find(lines, r"^朋友")))


def _menu_ready(lines, height, author):
    return sum(line["text"] == "更多信息" for line in lines) == 1


def _basic_info_ready(lines, height, author):
    parse_basic_info(lines)
    return all(label_values(lines).values())


def _share_sheet_ready(lines, height, author):
    return bool(find(lines, r"^(?:转发给朋友|复制链接)$"))


def _edit_box(lines, top=None):
    """An input box; its class only survives in control selectors."""
    return next((line for line in lines if line.get("selector", {}).get("class", "").endswith("EditText")
                 and (top is None or line["box"][1] < top)), None)


def _search_box_ready(lines, height, author):
    return bool(_edit_box(lines, top=200))


def _feed_or_search_ready(lines, height, author):
    if _edit_box(lines, top=200):
        return True
    return _feed_ready(lines, height, author)


def _home_ready(lines, height, author):
    bottom = lambda line: line["box"][1] > height - 180
    return bool(find(lines, r"^微信$", bottom) and find(lines, r"^通讯录$", bottom))


def _chat_search_ready(lines, height, author):
    return bool(_edit_box(lines, top=200))


def _chat_ready(lines, height, author):
    return bool(
        _edit_box(lines)
        or find(
            lines,
            r"^发送$|可按住\s*转文字|按住\s*说话|切换私信身份|切换到按住说话|切换到键盘|更多功能按钮，已折叠|仅可发\s*1\s*条消息"
            r"|设置.{0,12}才能发消息|才能(?:给\S+)?发消息|不接收.{0,6}消息"
            r"|无法发送私信|暂无法接收私信|频繁发送私信|私信.{0,8}频繁|请明天再试|私信.{0,8}达到.{0,8}上限",
        )
    )


def _link_video_ready(lines, height, author):
    if _chat_ready(lines, height, author):
        return True
    try:
        author_in_feed(lines, height)
        return True
    except ValueError:
        from interactions import classify_page_state
        if classify_page_state(lines, height) != "channels_feed" and find(
            lines, r"^(?:返回|关闭|更多|正在加载.*|复制链接|在默认浏览器中打开)$"
        ):
            return True
        return False


def _composer_or_drawer_ready(lines, height, author):
    from interactions import classify_page_state
    if classify_page_state(lines, height) == "composer_overlay" or _edit_box(lines):
        return True
    return _comments_ready(lines, height, author)


# Pages read from controls, with OCR limited to empty text containers that the
# label actually consumes. Each check is the parser the flow applies next; a
# page that fails it, or a label not listed here, falls back to full-screen OCR.
CONTROL_PAGES = {
    "navigation-bottom": _navigation_ready, "navigation-top": _navigation_ready,
    **dict.fromkeys(("feed-ready", "tab-check-", "tab-after-loc-", "tab-switched-", "tab-switched-loc-",
                     "verify-stage-recommend", "verify-stage-friends", "verify-stage-follow", "verify-stage-city",
                     "feed-after-collect", "feed-after-like", "feed-after-author-touch",
                     "feed-after-author-touch-retry", "feed-before-personal", "profile-first-work",
                     "profile-first-work-retry", "share-feed"),
                    _page_state("channels_feed")),
    **dict.fromkeys(("feed", "before-author", "search-opened-video-", "preemptive-feed-",
                     "recommend-feed-", "friends-feed-", "follow-feed-", "city-feed-",
                     "recommend-recovered-", "friends-recovered-", "follow-recovered-", "city-recovered-"),
                    _feed_ready),
    **dict.fromkeys(("video-details", "expanded-description", "comments-details",
                     "comments-preemptive-work-header", "comments-preemptive-work-expanded"), _drawer_ready),
    **dict.fromkeys(("comments", "comments-check", "comments-expanded", "comments-replies", "comments-return",
                     "comments-after-skip", "comments-recover", "comments-after-like", "comments-after-post",
                     "comments-after-reply", "comments-after-profile-touch", "comments-after-profile-touch-retry",
                     "comments-after-user-card", "comments-preemptive-start", "drawer-recover", "comments-locate-",
                     "comments-relocate-",
                     # Scrolled pages (comments-page-N, comments-page-N-back) must be read like the
                     # return check reads them, or the two readings of one comment cannot match.
                     "comments-page-"), _comments_ready),
    **dict.fromkeys(("comments-composer", "comments-typed", "comments-reply-composer", "comments-reply-typed"),
                    _composer_or_drawer_ready),
    **dict.fromkeys(("author-profile", "author-profile-touch", "author-profile-touch-retry",
                     "commenter-profile-before-more"), _profile_ready),
    **dict.fromkeys(("profile-touch", "profile-follow-check", "profile-after-follow", "profile-grid"),
                    _profile_follow_ready),
    **dict.fromkeys(("profile-after-first-work", "profile-after-first-work-retry"),
                    _page_state("user_profile")),
    "commenter-profile": _profile_or_card_ready,
    "profile-menu": _menu_ready, "commenter-profile-menu": _menu_ready,
    **dict.fromkeys(("author-basic-info", "author-basic-info-retry", "author-basic-info-ready"), _basic_info_ready),
    **dict.fromkeys(("interact-start", "resume-check", "resume-clean-", "messages-start", "messages-return-check",
                     "messages-return-", "preemptive-restore-check", "recover-check-", "recover-after-captcha-",
                     "recover-", "share-return-", "share-recover-"), _known_page),
    "personal-page": _page_state("personal_page"),
    **dict.fromkeys(("messages-center", "messages-comments", "messages-after-like", "messages-after-reply"),
                    _page_state("channels_message_center")),
    **dict.fromkeys(("messages-reply-composer", "messages-reply-typed"), _page_state("composer_overlay")),
    "search-input": _search_input_ready, "search-typed": _search_typed_ready,
    **dict.fromkeys(("search-results", "search-results-retry", "search-video-tab", "search-filter-start",
                     "search-filter-video-tab", "search-filter-collapsed", "search-cards", "search-cards-",
                     "search-grid-", "search-return-", "search-after-", "verify-stage-search"),
                    _search_results_ready),
    **dict.fromkeys(("search-filter-panel", "search-filter-panel-reopen", "search-filter-after-"), _search_filter_ready),
    **dict.fromkeys(("search-duration-sheet", "search-scope-sheet"), _search_sheet_ready),
    **dict.fromkeys(("share-sheet", "share-sheet-scrolled"), _share_sheet_ready),
    "share-closed": _known_page, "share-link-paste": _search_box_ready,
    "share-paste-drawer": _comments_ready, "share-link-paste-composer": _chat_ready, "share-link-cleared": _chat_ready,
    "link-after-dm-": _known_page,
    **dict.fromkeys(("link-home", "link-home-"), _home_ready),
    **dict.fromkeys(("link-search", "link-search-typed"), _chat_search_ready),
    **dict.fromkeys(("link-chat", "link-chat-typed", "link-chat-sent", "dm-chat", "dm-chat-typed", "dm-chat-sent",
                     "dm-chat-after-notice", "link-chat-after-notice"), _chat_ready),
    "link-video": _feed_ready,
    "link-video-": _link_video_ready,
}

# Labels that intentionally inspect visual overlays or system dialogs via full-screen OCR.
OCR_ONLY_LABELS = ("captcha-poll-", "location-modal-check", "probe")

_FEED_AUTHOR_LABELS = (
    "feed", "before-author", "search-opened-video-", "preemptive-feed-",
    "recommend-feed-", "friends-feed-", "follow-feed-", "city-feed-",
    "recommend-recovered-", "friends-recovered-", "follow-recovered-", "city-recovered-",
    "link-video", "link-video-",
)
_FULL_PROFILE_LABELS = (
    "author-profile", "commenter-profile",
)
_TOUCH_PROFILE_LABELS = (
    "author-profile-touch", "author-profile-touch-retry", "commenter-profile-before-more",
)
_FOLLOW_PROFILE_LABELS = (
    "profile-follow-check", "profile-after-follow", "profile-touch", "profile-grid",
)
_DRAWER_LABELS = (
    "comments", "comments-details", "comments-page-", "comments-replies", "comments-expanded",
    "comments-return", "comments-after-skip", "comments-recover",
    "video-details", "expanded-description", "comments-preemptive-work-header",
    "comments-preemptive-work-expanded", "comments-check", "comments-after-like",
    "comments-after-post", "comments-after-reply", "comments-after-profile-touch",
    "comments-after-profile-touch-retry", "comments-after-user-card",
    "comments-preemptive-start", "comments-locate-", "comments-relocate-", "drawer-recover", "share-paste-drawer",
)


def _matches_any(label, entries):
    return any(label == entry or (entry.endswith("-") and label.startswith(entry)) for entry in entries)


def _needed_containers(label, lines, height):
    """Return the custom-drawn container kinds required by `label`, skipping unneeded OCR."""
    if _matches_any(label, _FEED_AUTHOR_LABELS):
        return {"feed-author"}
    if _matches_any(label, _FULL_PROFILE_LABELS):
        if label == "commenter-profile":
            from comments import user_card_open
            if user_card_open(page_lines(lines), height):
                return set()
        return {"profile-name", "certification", "bio", "follow-button", "message-button"}
    if _matches_any(label, _TOUCH_PROFILE_LABELS):
        return {"profile-name", "follow-button", "message-button"}
    if _matches_any(label, _FOLLOW_PROFILE_LABELS):
        return {"follow-button", "message-button"}
    if _matches_any(label, _DRAWER_LABELS):
        return {"comment-body"}
    return set()


def page_check(label):
    """The readiness check for a label; entries ending in '-' cover labels with a dynamic suffix."""
    if label in CONTROL_PAGES:
        return CONTROL_PAGES[label]
    return next((check for prefix, check in CONTROL_PAGES.items()
                 if prefix.endswith("-") and label.startswith(prefix)), None)


def complete_for(lines, label, height, author=None):
    """Whether these lines carry what the flow reads next on this page."""
    ready = page_check(label)
    if not ready:
        return False
    try:
        return bool(ready(page_lines(lines), height, author))
    except ValueError:
        return False


def choose_snapshot(label, height, author, hierarchy, templates, ocr, regional_ocr=None):
    """Controls first, OCR only inside needed empty text containers, full OCR as fallback."""
    try:
        lines = hierarchy()
    except HierarchyUnavailable:
        lines = []
    if lines and page_check(label):
        needed = _needed_containers(label, lines, height)
        scoped = [line for line in lines if not line.get("container") or line["container"] in needed]
        roi = container_roi(scoped, height) if regional_ocr else None
        filled = fill_containers(scoped, regional_ocr(roi)) if roi else scoped
        if complete_for(filled, label, height, author):
            source = "mixed" if any(line.get("filledContainer") for line in filled) else "hierarchy"
            return page_lines(filled), source
        allow_extra = bool(needed or page_check(label) is _known_page)
        extra_roi = container_roi(lines, height) if regional_ocr and allow_extra and scoped != lines else None
        if extra_roi and extra_roi != roi:
            extra_filled = fill_containers(lines, regional_ocr(extra_roi))
            if complete_for(extra_filled, label, height, author):
                source = "mixed" if any(line.get("filledContainer") for line in extra_filled) else "hierarchy"
                return page_lines(extra_filled), source
    # Keep this lazy: a successful semantic lookup must not take a screenshot.
    if label.startswith("navigation-"):
        matched = templates()
        if matched:
            return matched, "template"
    recognized = ocr()
    if lines:
        recognized = correct_ocr(recognized, page_lines(lines))
        if any(line.get("source") == "hierarchy" for line in recognized):
            return recognized, "mixed"
    return recognized, "ocr"


def field_evidence(lines, label, author=None):
    """Record this snapshot's observations, without claiming cross-page identity verification."""
    try:
        if label in ("video-details", "expanded-description", "comments-details", "comments-expanded") and author:
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
