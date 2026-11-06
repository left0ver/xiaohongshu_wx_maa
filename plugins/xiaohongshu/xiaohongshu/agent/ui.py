"""Semantic selectors over a fresh Android UI hierarchy, in physical pixels."""

from dataclasses import dataclass
import hashlib
import re
import xml.etree.ElementTree as ET

PACKAGE = "com.xingin.xhs"
_VIDEO_DRAWER_HEADER_RE = re.compile(r"^(?:共\s*\d+\s*条评论|评论|暂无评论)$")
_VIDEO_DRAWER_META_RE = re.compile(
    r"^(?:编辑于\s*)?(?:刚刚|今天(?:\s*\d{1,2}:\d{2})?|昨天(?:\s*\d{1,2}:\d{2})?|"
    r"前天(?:\s*\d{1,2}:\d{2})?|\d+\s*(?:秒|分钟|小时|天)前|\d{1,2}-\d{1,2}|\d{4}-\d{2}-\d{2})"
    r"(?:\s+\S+)*$"
)


class PageMismatch(ValueError):
    pass


def bounds(node):
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    if not match:
        raise PageMismatch("Missing node bounds")
    box = tuple(map(int, match.groups()))
    if box[0] >= box[2] or box[1] >= box[3]:
        raise PageMismatch("Empty node bounds")
    return box


def center(node):
    left, top, right, bottom = bounds(node)
    return (left + right) // 2, (top + bottom) // 2


@dataclass(frozen=True)
class Card:
    note_type: str
    title: str
    author: str
    box: tuple
    cover_box: tuple | None = None

    @property
    def key(self):
        return hashlib.sha256(repr((self.note_type, self.title, self.author)).encode()).hexdigest()


class Page:
    def __init__(self, xml):
        if len(xml) > 4_000_000 or "<!DOCTYPE" in xml or "<!ENTITY" in xml:
            raise PageMismatch("Invalid hierarchy document")
        self.root = ET.fromstring(xml)
        self.nodes = [n for n in self.root.iter("node") if n.get("package") == PACKAGE]
        if not self.nodes:
            raise PageMismatch("Xiaohongshu is not visible")
        text = [n.get("text", "") for n in self.nodes]
        if any(re.search(r"请完成.*验证|拖动滑块|安全验证|验证码", t) for t in text):
            raise PageMismatch("Verification detected; manual intervention required")

    def unique(self, *, text=None, description=None, resource=None):
        nodes = [n for n in self.nodes if
                 (text is None or n.get("text") == text) and
                 (description is None or n.get("content-desc") == description) and
                 (resource is None or n.get("resource-id") == PACKAGE + ":id/" + resource)]
        if len(nodes) != 1:
            raise PageMismatch("Selector did not identify exactly one visible control")
        bounds(nodes[0])
        return nodes[0]

    def _parse_waterfall_cards(self, top, bottom):
        cards = []
        for node in self.nodes:
            match = re.fullmatch(
                r"(视频|笔记)\s+(?:(?:热点|你的关注|常看|好友赞过|置顶)\s+)?(.+) 来自(.+) ([\d.万亿]*赞)",
                node.get("content-desc", ""),
                re.DOTALL,
            )
            if not match:
                continue
            if any(n.get("text", "").strip() in ("广告", "赞助") for n in node.iter("node")):
                continue
            box = bounds(node)
            # A clipped last row exposes a title but is not a safe card target.
            children = [n for n in node.iter("node") if n.get("text") == match[3]]
            if box[1] < top or box[3] > bottom or len(children) != 1:
                continue
            if bounds(children[0])[3] > box[3]:
                continue
            covers = [bounds(n) for n in node.iter("node")
                      if n.get("class") == "android.widget.ImageView"
                      and bounds(n)[0] == box[0] and bounds(n)[2] == box[2]
                      and bounds(n)[3] <= bounds(children[0])[1]]
            if not covers:
                continue
            cards.append(Card("VIDEO" if match[1] == "视频" else "IMAGE_TEXT",
                              match[2].strip(), match[3].strip(), box, max(covers, key=lambda b: b[3] - b[1])))
        return sorted(cards, key=lambda card: (card.box[1], card.box[0]))

    def cards(self):
        self.unique(description="首页")
        tab = self.unique(description="发现")
        if tab.get("selected") != "true":
            raise PageMismatch("Discovery tab is not selected")
        top = bounds(tab)[3]
        bottom = bounds(self.unique(description="首页"))[1]
        return self._parse_waterfall_cards(top, bottom)

    def city_tab(self):
        tabs = [n for n in self.nodes if n.get("class", "").endswith("ActionBar$Tab")]
        for tab in tabs:
            desc = tab.get("content-desc", "").strip()
            texts = [c.get("text", "").strip() for c in tab.iter("node") if c.get("text", "").strip()]
            if desc in ("同城", "附近") or any(t in ("同城", "附近") for t in texts):
                bounds(tab)
                return tab
        discover_nodes = [n for n in self.nodes if n.get("content-desc") == "发现"]
        if len(discover_nodes) == 1:
            d_box = bounds(discover_nodes[0])
            right_tabs = [
                tab for tab in tabs
                if bounds(tab)[0] >= d_box[2] - 8 and abs(bounds(tab)[1] - d_box[1]) <= 24
            ]
            if right_tabs:
                return min(right_tabs, key=lambda t: bounds(t)[0])
        for node in self.nodes:
            if node.get("content-desc") in ("同城", "附近") or node.get("text") in ("同城", "附近"):
                bounds(node)
                return node
        raise PageMismatch("City tab is not visible")

    def city_cards(self):
        home = self.unique(description="首页")
        tab = self.city_tab()
        selected = tab.get("selected") == "true" or any(
            c.get("selected") == "true" for c in tab.iter("node")
        )
        if not selected:
            raise PageMismatch("City tab is not selected")
        top = bounds(tab)[3]
        bottom = bounds(home)[1]
        return self._parse_waterfall_cards(top, bottom)

    def search_cards(self, expected_keyword=None):
        if any(n.get("text", "").strip() == "排序依据" for n in self.nodes):
            raise PageMismatch("Search filter panel still covers results")
        back_nodes = [n for n in self.nodes if n.get("content-desc") == "返回"]
        search_btns = [
            n for n in self.nodes
            if n.get("text", "").strip() == "搜索" or n.get("content-desc") == "全部删除"
        ]
        all_tabs = [
            n for n in self.nodes
            if n.get("text", "").strip() == "全部" and bounds(n)[1] < 600
        ]
        if not back_nodes or not search_btns or not all_tabs:
            raise PageMismatch("Not on search results page")
        all_tab = min(all_tabs, key=lambda n: bounds(n)[1])
        all_box = bounds(all_tab)
        if expected_keyword is not None:
            kw = expected_keyword.strip()
            kw_nodes = [
                n for n in self.nodes
                if n.get("text", "").strip() == kw and bounds(n)[3] <= all_box[1] + 24
            ]
            if not kw_nodes:
                raise PageMismatch("Search keyword in top bar does not match expected keyword")
        header_bottom = all_box[3]
        for node in self.nodes:
            if node.get("text", "").strip() in ("综合", "最新") and bounds(node)[1] >= all_box[1]:
                if bounds(node)[3] <= all_box[3] + 220:
                    header_bottom = max(header_bottom, bounds(node)[3])
        rvs = [
            n for n in self.nodes
            if n.get("class") == "androidx.recyclerview.widget.RecyclerView"
            and bounds(n)[3] - bounds(n)[1] > 300
        ]
        if not rvs:
            raise PageMismatch("Search results list is not visible")
        rv = max(rvs, key=lambda n: bounds(n)[3] - bounds(n)[1])
        rv_box = bounds(rv)
        desc_cards = self._parse_waterfall_cards(header_bottom, rv_box[3])
        if desc_cards:
            return desc_cards
        cards = []
        rv_width = rv_box[2] - rv_box[0]
        for item in rv:
            try:
                box = bounds(item)
            except PageMismatch:
                continue
            if box[2] - box[0] > rv_width * 3 // 4:
                continue
            if box[1] < header_bottom - 12 or box[3] >= rv_box[3] - 2:
                continue
            imgs = [
                bounds(n) for n in item.iter("node")
                if n.get("class") == "android.widget.ImageView"
            ]
            covers = [
                b for b in imgs
                if b[2] - b[0] >= (box[2] - box[0]) * 4 // 5 and abs(b[1] - box[1]) <= 12
            ]
            if not covers:
                continue
            cover_box = max(covers, key=lambda b: b[3] - b[1])
            texts = [
                (bounds(n), n.get("text", "").strip())
                for n in item.iter("node")
                if n.get("class") == "android.widget.TextView" and n.get("text", "").strip()
            ]
            if any(t in ("广告", "赞助") for _, t in texts):
                continue
            has_duration = any(
                re.fullmatch(r"\d+:\d{2}(?::\d{2})?", t)
                and b[1] >= cover_box[1] and b[3] <= cover_box[3] + 8
                for b, t in texts
            )
            avatars = [
                b for b in imgs
                if b[1] >= cover_box[3] - 4 and b[0] < box[0] + (box[2] - box[0]) // 3
            ]
            if not avatars:
                continue
            avatar_box = min(avatars, key=lambda b: b[0])
            if avatar_box[3] >= rv_box[3] - 2:
                continue
            title_candidates = [
                t for b, t in texts
                if b[1] >= cover_box[3] - 8 and b[3] <= avatar_box[1] + 16
            ]
            author_candidates = [
                (b, t) for b, t in texts
                if b[1] >= avatar_box[1] - 24
                and b[0] >= avatar_box[2] - 6
                and b[2] <= box[0] + (box[2] - box[0]) * 4 // 5
                and not _VIDEO_DRAWER_META_RE.fullmatch(t)
                and not re.fullmatch(r"[\d.万亿]+|赞", t)
            ]
            if not author_candidates:
                continue
            author_box, author = min(author_candidates, key=lambda pair: (pair[0][1], pair[0][0]))
            if author_box[3] >= rv_box[3] - 2:
                continue
            title = title_candidates[0].strip() if title_candidates else author
            cards.append(Card(
                "VIDEO" if has_duration else "IMAGE_TEXT",
                title,
                author.strip(),
                box,
                cover_box,
            ))
        return sorted(cards, key=lambda card: (card.box[1], card.box[0]))

    def image_note_viewport(self):
        nick = self.unique(resource="nickNameTV")
        share = self.unique(resource="moreOperateIV")
        comment_bar = self.unique(description="评论框")
        author = nick.get("text", "").strip()
        if not author:
            raise PageMismatch("Not a loaded image note")
        top_bar = max(bounds(nick)[3], bounds(share)[3])
        bottom_bar = bounds(comment_bar)[1]
        if top_bar >= bottom_bar:
            raise PageMismatch("Invalid image note viewport bounds")
        return author, top_bar, bottom_bar

    def _post_body_upper(self, lower, bottom_bar):
        upper = bottom_bar
        for node in self.nodes:
            text = node.get("text", "").strip()
            if not text:
                continue
            box = bounds(node)
            if box[1] <= lower or box[1] >= upper:
                continue
            if (re.fullmatch(r"共\s*\d+\s*条评论", text)
                    or text in {"还没有评论哦", "抢首评", "暂无人评论", "让大家听到你的声音", "有话要说，快来评论", "地点"}
                    or re.fullmatch(r"((?:刚刚|今天|昨天|前天|\d)[^\n]*?)(?:\s+(\S+))?\s+回复(?:\s+翻译)?", text)):
                upper = min(upper, box[1])
        return upper

    def image_note_state(self, allow_scrolled=False):
        author, top_bar, bottom_bar = self.image_note_viewport()
        pictures = [n for n in self.nodes if re.match(r"图片,第\d+张,共\d+张", n.get("content-desc", ""))]
        if len(pictures) > 1 or (not pictures and not allow_scrolled):
            raise PageMismatch("Not a loaded image note")
        lower = max(top_bar, bounds(pictures[0])[3]) if pictures else top_bar
        upper = self._post_body_upper(lower, bottom_bar)
        groups = []
        for node in self.nodes:
            if node.get("class") != "android.widget.LinearLayout" or not len(node):
                continue
            box = bounds(node)
            if box[1] < lower or box[1] >= upper:
                continue
            if not all(c.get("class") == "android.widget.TextView" and c.get("text", "").strip() for c in node):
                continue
            texts = [c.get("text", "").strip() for c in node]
            if any(c.get("resource-id", "").endswith("/nickNameTV") or c.get("content-desc") == "评论框"
                   or (c.get("clickable") == "true" and not is_truncated_text(c.get("text", "")))
                   for c in node):
                continue
            if len(texts) == 1 and (
                re.fullmatch(r"\d+/\d+", texts[0])
                or texts[0] in {"关注", "已关注", "互相关注", "发私信", "分享至", "首评", "- 到底了 -",
                                "查看被折叠的评论", "以下是被折叠的评论"}
                or re.fullmatch(
                    r"(?:编辑于\s*)?(?:刚刚|今天(?:\s*\d{1,2}:\d{2})?|昨天(?:\s*\d{1,2}:\d{2})?|"
                    r"前天(?:\s*\d{1,2}:\d{2})?|\d+\s*(?:秒|分钟|小时|天)前|\d{2}-\d{2}|\d{4}-\d{2}-\d{2})"
                    r"(?:\s+\S+)?",
                    texts[0],
                )
            ):
                continue
            groups.append((node, texts))
        if not groups:
            raise PageMismatch("Full title/body boundary is not visible")
        group, body = min(groups, key=lambda item: bounds(item[0])[1])
        group_box = bounds(group)
        expand_nodes = [n for n in group.iter("node")
                        if n.get("text", "").strip() == "展开" or n.get("content-desc", "").strip() == "展开"
                        or is_truncated_text(n.get("text", ""))]
        truncated = any(is_truncated_text(text) for text in body) or bool(expand_nodes)
        clipped = group_box[3] >= bottom_bar
        incomplete = "truncated" if truncated else ("clipped" if clipped else None)
        title = body[0] if len(body) >= 2 else ""
        paragraphs = body[1:] if len(body) >= 2 else body
        return dict(
            noteType="IMAGE_TEXT",
            authorNickname=author,
            title=title,
            description="\n".join(paragraphs),
            paragraphs=paragraphs,
            group=group,
            box=group_box,
            incomplete=incomplete,
        )

    def image_note(self, allow_incomplete=False):
        state = self.image_note_state(allow_scrolled=False)
        if state["incomplete"] == "truncated" and not allow_incomplete:
            raise PageMismatch("Title or description is truncated")
        if state["incomplete"] == "clipped" and not allow_incomplete:
            raise PageMismatch("Full title/body boundary is not visible")
        result = dict(
            noteType="IMAGE_TEXT",
            authorNickname=state["authorNickname"],
            title=state["title"],
            description=state["description"],
        )
        if allow_incomplete and state["incomplete"]:
            result["_incomplete"] = state["incomplete"]
        return result

    def note(self, allow_incomplete=False):
        if any(n.get("text", "").startswith("小红书号：") or n.get("content-desc") == "复制链接"
               for n in self.nodes):
            raise PageMismatch("A profile or share panel still covers the note")
        if not any(n.get("resource-id", "").endswith("/matrixNickNameView") for n in self.nodes):
            return self.image_note(allow_incomplete=allow_incomplete)
        if any(_VIDEO_DRAWER_HEADER_RE.fullmatch(n.get("text", "").strip()) for n in self.nodes):
            raise PageMismatch("A video drawer still covers the note")
        name = self.unique(resource="matrixNickNameView").get("text", "").strip()
        self.unique(description="作者" + name)
        self.unique(description="分享")
        caption_node = self.unique(resource="noteContentText")
        raw_text = caption_node.get("text", "").replace("\\n", "\n").strip()
        desc_text = caption_node.get("content-desc", "").replace("\\n", "\n").strip()
        caption = desc_text or raw_text
        truncated = is_truncated_text(raw_text) or is_truncated_text(desc_text)
        if not name or not caption or (truncated and not allow_incomplete):
            raise PageMismatch("Video caption is incomplete")
        result = dict(noteType="VIDEO", authorNickname=name, title=caption.splitlines()[0], description=caption)
        if allow_incomplete and truncated:
            result["_incomplete"] = "truncated"
        return result

    def note_expand_control(self):
        if any(n.get("resource-id", "").endswith("/matrixNickNameView") for n in self.nodes):
            node = self.unique(resource="noteContentText")
            raw_text = node.get("text", "").replace("\\n", "\n").strip()
            desc_text = node.get("content-desc", "").replace("\\n", "\n").strip()
            if not (is_truncated_text(raw_text) or is_truncated_text(desc_text)):
                raise PageMismatch("Video caption is not truncated")
            return node
        state = self.image_note_state(allow_scrolled=True)
        for node in state["group"].iter("node"):
            if node.get("text", "").strip() == "展开" or node.get("content-desc", "").strip() == "展开":
                bounds(node)
                return node
        for node in state["group"]:
            if is_truncated_text(node.get("text", "")):
                bounds(node)
                return node
        raise PageMismatch("No truncated note text control found")

    def _video_drawer_header_and_close(self):
        headers = [n for n in self.nodes
                   if n.get("class") == "android.widget.TextView"
                   and _VIDEO_DRAWER_HEADER_RE.fullmatch(n.get("text", "").strip())]
        if len(headers) != 1:
            raise PageMismatch("Video drawer header is not visible")
        header = headers[0]
        hbox = bounds(header)
        close_nodes = [
            n for n in self.nodes
            if n.get("class") == "android.widget.ImageView"
            and n.get("clickable") == "true"
            and bounds(n)[1] >= hbox[1] - 120
            and bounds(n)[3] <= hbox[3] + 60
            and bounds(n)[0] > hbox[2]
        ]
        if len(close_nodes) != 1:
            raise PageMismatch("Video drawer close control is not visible")
        return header, close_nodes[0]

    def video_drawer_close_button(self):
        _, close_node = self._video_drawer_header_and_close()
        return close_node

    def expanded_video_note(self, expected_author):
        if any(_VIDEO_DRAWER_HEADER_RE.fullmatch(n.get("text", "").strip()) for n in self.nodes):
            header, close_node = self._video_drawer_header_and_close()
        else:
            actual = self.note(allow_incomplete=False)
            if actual.get("authorNickname") != expected_author:
                raise PageMismatch("Expanded video author does not match")
            return actual
        hbox = bounds(header)
        rvs = [n for n in self.nodes
               if n.get("class") == "androidx.recyclerview.widget.RecyclerView"
               and bounds(n)[1] >= hbox[3] - 4 and len(n) >= 1]
        if len(rvs) > 1:
            nested = {sub for rv in rvs for sub in list(rv)[0].iter("node") if sub is not rv}
            for rv in rvs:
                for child in rv:
                    for sub in child.iter("node"):
                        nested.add(sub)
            rvs = [rv for rv in rvs if rv not in nested and bounds(rv)[1] <= hbox[3] + max(120, hbox[3] - hbox[1])]
        if len(rvs) != 1:
            raise PageMismatch("Video drawer list is not ready")
        first_item = rvs[0][0]
        text_nodes = [n for n in first_item.iter("node")
                      if n.get("class") == "android.widget.TextView" and n.get("text", "").strip()]
        author_nodes = [n for n in text_nodes if n.get("text", "").strip() == expected_author]
        if not author_nodes:
            raise PageMismatch("First item in video drawer does not match the video author")
        author_bottom = min(bounds(n)[3] for n in author_nodes)
        below_nodes = [
            n for n in text_nodes
            if bounds(n)[1] >= author_bottom - 4
            and n.get("text", "").strip() not in {expected_author, "作者", "已声明原创", "回复", "置顶", "翻译", "展开", "不喜欢"}
            and not _VIDEO_DRAWER_META_RE.fullmatch(n.get("text", "").strip())
        ]
        if not below_nodes:
            raise PageMismatch("Expanded video caption in drawer is incomplete")
        caption_node = min(below_nodes, key=lambda n: (bounds(n)[1], bounds(n)[0]))
        caption = caption_node.get("text", "").replace("\\n", "\n").strip()
        if not caption or is_truncated_text(caption):
            raise PageMismatch("Expanded video caption in drawer is incomplete")
        return dict(
            noteType="VIDEO",
            authorNickname=expected_author,
            title=caption.splitlines()[0].strip(),
            description=caption,
            _drawer_close=close_node,
        )

    def returned_note(self, expected):
        if any(n.get("text", "").startswith("小红书号：")
               or n.get("content-desc") in ("复制链接", "个人页头图")
               or n.get("content-desc", "").startswith("头像,")
               for n in self.nodes):
            raise PageMismatch("A profile or share panel still covers the note")
        if (expected.get("noteType") == "VIDEO"
                and any(_VIDEO_DRAWER_HEADER_RE.fullmatch(n.get("text", "").strip()) for n in self.nodes)):
            raise PageMismatch("A video drawer still covers the note")
        try:
            actual = self.note()
            verify_return(expected, actual)
            return actual
        except PageMismatch:
            if expected.get("noteType") == "VIDEO":
                actual = self.note(allow_incomplete=True)
                if actual.get("authorNickname") != expected.get("authorNickname"):
                    raise PageMismatch("Returned to a different work")
                vis_title = strip_truncation_suffix(_clean_topic_text(actual.get("title", "")))
                exp_title = _clean_topic_text(expected.get("title", ""))
                exp_desc = _clean_topic_text(expected.get("description", ""))
                if not vis_title or (vis_title not in exp_title and vis_title not in exp_desc):
                    raise PageMismatch("Returned to a different work")
                return {k: expected[k] for k in ("noteType", "authorNickname", "title", "description") if k in expected}
            if expected.get("noteType") != "IMAGE_TEXT":
                raise
        state = self.image_note_state(allow_scrolled=True)
        if state["authorNickname"] != expected.get("authorNickname"):
            raise PageMismatch("Returned to a different work")
        visible = [t for t in ([state["title"]] + state["paragraphs"]) if t]
        full_title = expected.get("title", "")
        full_desc = expected.get("description", "")
        if not visible or not all(t == full_title or t in full_desc for t in visible):
            raise PageMismatch("Returned to a different work")
        return {k: expected[k] for k in ("noteType", "authorNickname", "title", "description") if k in expected}

    def _certified_author_identity(self, expected_name):
        avatar_desc = f"头像,{expected_name}"
        for group in self.root.iter():
            if not any(c.get("content-desc") == avatar_desc for c in group):
                continue
            texts = [n.get("text", "").strip() for n in group.iter()
                     if n.get("class") == "android.widget.TextView" and n.get("text", "").strip()]
            if expected_name not in texts:
                continue
            after = texts[texts.index(expected_name) + 1:]
            identities = [t for t in after if not re.match(r"^(?:IP(?:属地)?|小红书号)[：:]", t)]
            if len(identities) == 1:
                return identities[0]
        return None

    def author_identity(self, expected_name):
        ids = [re.fullmatch(r"小红书号[：:]\s*(\S+)", n.get("text", "").strip()) for n in self.nodes]
        ids = [m[1] for m in ids if m]
        names = [n for n in self.nodes if n.get("text") == expected_name]
        # A detail-page nickname left behind by an unfinished transition is not profile evidence.
        profile_names = [n for n in names
                         if not n.get("resource-id", "").endswith(("/nickNameTV", "/matrixNickNameView"))]
        if len(ids) == 1 and names:
            if not profile_names:
                raise PageMismatch("Author transition has not completed")
            return ids[0]
        if not ids and names:
            if not profile_names:
                raise PageMismatch("Author transition has not completed")
            certified = self._certified_author_identity(expected_name)
            if certified:
                return certified
        raise PageMismatch("Author profile is incomplete or identity does not match")


def is_truncated_text(text):
    stripped = (text or "").strip()
    return stripped.endswith(("…", "...", "展开")) or bool(re.search(r"(?:\.\.\.|…)\s*展开$", stripped))


def strip_truncation_suffix(text):
    stripped = (text or "").strip()
    stripped = re.sub(r"(?:\.\.\.|…)?\s*展开$", "", stripped).strip()
    return stripped.rstrip("….").strip()


def merge_note_paragraphs(existing, incoming, title=""):
    merged = list(existing)
    for item in incoming:
        text = item.strip()
        if not text or text == title:
            continue
        placed = False
        for index, prev in enumerate(merged):
            if text == prev:
                placed = True
                break
            prev_stem = strip_truncation_suffix(prev)
            text_stem = strip_truncation_suffix(text)
            if is_truncated_text(prev) and prev_stem and text.startswith(prev_stem):
                merged[index] = text
                placed = True
                break
            if is_truncated_text(text) and text_stem and prev.startswith(text_stem):
                placed = True
                break
            if text in prev and not is_truncated_text(prev):
                placed = True
                break
            if prev in text and not is_truncated_text(text):
                merged[index] = text
                placed = True
                break
        if not placed:
            merged.append(text)
    return merged


def _normalize_text(value):
    return re.sub(r"\s+", " ", (value or "")).strip()


def _clean_topic_text(value):
    return _normalize_text(re.sub(r"\[话题\]#?", "", value or ""))


def verify_card(card, detail):
    title = detail.get("title") or detail.get("description", "")
    if card.note_type != detail.get("noteType") or card.author != detail.get("authorNickname") or not title:
        raise PageMismatch("Feed changed or opened work does not match the selected card")
    first_line = title.splitlines()[0].strip()
    card_norm = _clean_topic_text(card.title)
    title_norm = _clean_topic_text(title)
    first_norm = _clean_topic_text(first_line)
    desc_norm = _clean_topic_text(detail.get("description", ""))
    desc_lines = [line.strip() for line in (detail.get("description") or "").splitlines() if line.strip()]
    desc_first_norm = _clean_topic_text(desc_lines[0]) if desc_lines else ""
    title_and_first = _clean_topic_text(f"{title_norm} {desc_first_norm}".strip())
    combined_norm = _clean_topic_text(f"{detail.get('title', '')} {detail.get('description', '')}".strip())
    prefix = strip_truncation_suffix(card_norm)
    if (card.title in {title, first_line, detail.get("description", "")}
            or card_norm in {title_norm, first_norm, desc_norm, combined_norm}
            or (len(prefix) >= 6 and (title_norm.startswith(prefix)
                                      or first_norm.startswith(prefix)
                                      or desc_norm.startswith(prefix)
                                      or combined_norm.startswith(prefix)))
            or (len(title_norm) >= 4 and (card_norm.startswith(title_norm) or title_norm in card_norm))
            or (len(first_norm) >= 4 and (card_norm.startswith(first_norm) or first_norm in card_norm))
            or (len(title_and_first) >= 3 and card_norm.startswith(title_and_first))
            or (title_norm and card_norm.startswith(title_norm + " "))):
        return
    if desc_first_norm and len(desc_first_norm) >= 4 and desc_first_norm in card_norm:
        head = strip_truncation_suffix(card_norm.split(desc_first_norm, 1)[0]).strip()
        if not head or title_norm.startswith(head) or first_norm.startswith(head):
            return
    for sep in ("…", "..."):
        if sep in card_norm:
            head, tail = card_norm.split(sep, 1)
            head = head.strip()
            tail = strip_truncation_suffix(tail).strip()
            if head and (title_norm.startswith(head) or first_norm.startswith(head)):
                if not tail or tail in desc_norm or desc_norm.startswith(tail):
                    return
    raise PageMismatch("Feed changed or opened work does not match the selected card")


def verify_return(before, after):
    keys = ("noteType", "authorNickname", "title", "description")
    if any(before.get(key) != after.get(key) for key in keys):
        raise PageMismatch("Returned to a different work")
