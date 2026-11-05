"""Semantic selectors over a fresh Android UI hierarchy, in physical pixels."""

from dataclasses import dataclass
import hashlib
import re
import xml.etree.ElementTree as ET

PACKAGE = "com.xingin.xhs"


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

    def cards(self):
        self.unique(description="首页")
        tab = self.unique(description="发现")
        if tab.get("selected") != "true":
            raise PageMismatch("Discovery tab is not selected")
        top = bounds(tab)[3]
        bottom = bounds(self.unique(description="首页"))[1]
        cards = []
        for node in self.nodes:
            match = re.fullmatch(r"(视频|笔记)\s+(.+) 来自(.+) ([\d.万亿]+)赞", node.get("content-desc", ""))
            if not match:
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

    def image_note(self):
        author = self.unique(resource="nickNameTV").get("text", "").strip()
        self.unique(resource="moreOperateIV")
        self.unique(description="评论框")
        pictures = [n for n in self.nodes if re.match(r"图片,第\d+张,共\d+张", n.get("content-desc", ""))]
        if len(pictures) != 1 or not author:
            raise PageMismatch("Not a loaded image note")
        lower = bounds(pictures[0])[3]
        upper = bounds(self.unique(description="评论框"))[1]
        groups = [n for n in self.nodes if n.get("class") == "android.widget.LinearLayout"
                  and len(n) == 2 and all(c.get("class") == "android.widget.TextView"
                  and c.get("text", "").strip() for c in n)
                  and bounds(n)[1] >= lower and bounds(n)[3] < upper]
        if not groups:
            raise PageMismatch("Full title/body boundary is not visible")
        group = min(groups, key=lambda n: bounds(n)[1])
        body = [n.get("text", "").strip() for n in group]
        if any(text.endswith(("…", "...", "展开")) for text in body):
            raise PageMismatch("Title or description is truncated")
        return dict(noteType="IMAGE_TEXT", authorNickname=author,
                    title=body[0],
                    description=body[-1])

    def note(self):
        if any(n.get("text", "").startswith("小红书号：") or n.get("content-desc") == "复制链接"
               for n in self.nodes):
            raise PageMismatch("A profile or share panel still covers the note")
        if not any(n.get("resource-id", "").endswith("/matrixNickNameView") for n in self.nodes):
            return self.image_note()
        name = self.unique(resource="matrixNickNameView").get("text", "").strip()
        self.unique(description="作者" + name)
        self.unique(description="分享")
        caption = self.unique(resource="noteContentText").get("content-desc", "").replace("\\n", "\n").strip()
        if not name or not caption or caption.endswith(("…", "...", "展开")):
            raise PageMismatch("Video caption is incomplete")
        return dict(noteType="VIDEO", authorNickname=name, title=caption.splitlines()[0], description=caption)

    def author_identity(self, expected_name):
        ids = [re.fullmatch(r"小红书号[：:]\s*(\S+)", n.get("text", "").strip()) for n in self.nodes]
        ids = [m[1] for m in ids if m]
        names = [n for n in self.nodes if n.get("text") == expected_name]
        if len(ids) != 1 or not names:
            raise PageMismatch("Author profile is incomplete or identity does not match")
        # A detail-page nickname left behind by an unfinished transition is not profile evidence.
        if any(n.get("resource-id", "").endswith("/nickNameTV") for n in names):
            raise PageMismatch("Author transition has not completed")
        return ids[0]


def verify_card(card, detail):
    title = detail.get("title") or detail.get("description", "")
    if (card.note_type != detail.get("noteType") or card.author != detail.get("authorNickname")
            or card.title != title):
        raise PageMismatch("Feed changed or opened work does not match the selected card")


def verify_return(before, after):
    keys = ("noteType", "authorNickname", "title", "description")
    if any(before.get(key) != after.get(key) for key in keys):
        raise PageMismatch("Returned to a different work")
