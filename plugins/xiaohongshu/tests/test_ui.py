import sys
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "xiaohongshu" / "agent"))
from ui import Card, Page, PageMismatch, center, merge_note_paragraphs, verify_card, verify_return


def node(parent, box, text="", desc="", resource="", kind="android.widget.TextView", **attrs):
    return ET.SubElement(parent, "node", {
        "package": "com.xingin.xhs", "bounds": f"[{box[0]},{box[1]}][{box[2]},{box[3]}]",
        "text": text, "content-desc": desc, "class": kind,
        "resource-id": "com.xingin.xhs:id/" + resource if resource else "", **attrs})


def feed(scale=1, offset=0):
    root = ET.Element("hierarchy")
    def add(parent, box, **kw):
        return node(parent, tuple(round(x * scale) + offset for x in box), **kw)
    add(root, (0, 0, 200, 50), desc="发现", selected="true")
    add(root, (0, 700, 200, 750), desc="首页")
    card = add(root, (10, 60, 190, 400), desc="笔记  标题 来自作者 123赞")
    add(card, (10, 60, 190, 340), kind="android.widget.ImageView")
    add(card, (20, 350, 80, 390), text="作者")
    add(root, (10, 410, 190, 700), desc="视频  被截断的视频 来自作者 1赞")
    return Page(ET.tostring(root, encoding="unicode"))


def detail():
    root = ET.Element("hierarchy")
    node(root, (20, 20, 100, 50), text="作者", resource="nickNameTV")
    node(root, (150, 20, 190, 50), resource="moreOperateIV")
    node(root, (0, 60, 200, 400), desc="图片,第1张,共3张,双指左划或右划即可查看更多内容")
    body = node(root, (0, 410, 200, 550), kind="android.widget.LinearLayout")
    node(body, (10, 420, 190, 460), text="标题")
    node(body, (10, 470, 190, 540), text="正文")
    node(root, (10, 700, 100, 750), desc="评论框")
    return root


class SelectorTests(unittest.TestCase):
    def test_feed_adapts_scale_translation_and_rejects_partial_card(self):
        for scale, offset in [(1, 0), (0.8, 30), (1.5, 4)]:
            cards = feed(scale, offset).cards()
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0].author, "作者")
            self.assertEqual(cards[0].box, tuple(round(x * scale) + offset for x in (10, 60, 190, 400)))

    def test_image_fields_and_identity_guards(self):
        parsed = Page(ET.tostring(detail(), encoding="unicode")).image_note()
        verify_card(feed().cards()[0], parsed)
        self.assertEqual(parsed["description"], "正文")
        with self.assertRaises(PageMismatch):
            verify_card(Card("IMAGE_TEXT", "另一篇", "作者", (1, 1, 2, 2)), parsed)
        with self.assertRaises(PageMismatch):
            verify_return(parsed, {**parsed, "authorNickname": "另一人"})

    def test_truncation_and_ambiguous_control_fail_closed(self):
        root = detail()
        next(n for n in root.iter("node") if n.get("text") == "正文").set("text", "正文…")
        with self.assertRaises(PageMismatch):
            Page(ET.tostring(root, encoding="unicode")).image_note()
        node(root, (100, 20, 190, 50), text="作者", resource="nickNameTV")
        with self.assertRaises(PageMismatch):
            Page(ET.tostring(root, encoding="unicode")).unique(resource="nickNameTV")

    def test_profile_requires_id_and_matching_name(self):
        root = ET.Element("hierarchy")
        node(root, (20, 50, 100, 100), text="作者")
        node(root, (20, 110, 190, 150), text="小红书号：red123")
        page = Page(ET.tostring(root, encoding="unicode"))
        self.assertEqual(page.author_identity("作者"), "red123")
        node(root, (0, 142, 3, 193), text="作者", resource="nickNameTV")
        self.assertEqual(Page(ET.tostring(root, encoding="unicode")).author_identity("作者"), "red123")
        only_detail_root = ET.Element("hierarchy")
        node(only_detail_root, (0, 142, 3, 193), text="作者", resource="nickNameTV")
        node(only_detail_root, (20, 110, 190, 150), text="小红书号：red123")
        with self.assertRaises(PageMismatch):
            Page(ET.tostring(only_detail_root, encoding="unicode")).author_identity("作者")
        with self.assertRaises(PageMismatch):
            page.author_identity("另一人")

    def test_video_uses_full_accessibility_caption(self):
        root = ET.Element("hierarchy")
        node(root, (20, 500, 100, 550), text="作者", resource="matrixNickNameView")
        node(root, (0, 490, 110, 560), desc="作者作者")
        node(root, (150, 20, 190, 50), desc="分享")
        caption = node(root, (0, 570, 200, 640), text="标题",
                       desc="标题\\n完整正文 #话题", resource="noteContentText")
        parsed = Page(ET.tostring(root, encoding="unicode")).note()
        self.assertEqual(parsed["noteType"], "VIDEO")
        self.assertEqual(parsed["title"], "标题")
        self.assertEqual(parsed["description"], "标题\n完整正文 #话题")
        caption.set("text", "标题... 展开")
        with self.assertRaises(PageMismatch):
            Page(ET.tostring(root, encoding="unicode")).note()
        incomplete = Page(ET.tostring(root, encoding="unicode")).note(allow_incomplete=True)
        self.assertEqual(incomplete["_incomplete"], "truncated")
        drawer = ET.Element("hierarchy")
        node(drawer, (420, 825, 659, 882), text="共 51 条评论")
        node(drawer, (966, 823, 1026, 883), kind="android.widget.ImageView", clickable="true")
        rv = node(drawer, (0, 948, 1080, 2124), kind="androidx.recyclerview.widget.RecyclerView")
        first = node(rv, (0, 948, 1080, 1365), kind="android.view.ViewGroup")
        node(first, (183, 984, 373, 1045), text="作者")
        node(first, (385, 992, 459, 1037), text="作者")
        node(first, (183, 1051, 822, 1185), text="标题\n完整正文 #话题")
        node(first, (183, 1212, 387, 1259), text="3天前 河北")
        expanded = Page(ET.tostring(drawer, encoding="unicode")).expanded_video_note("作者")
        self.assertEqual(expanded["description"], "标题\n完整正文 #话题")
        self.assertIsNotNone(expanded.get("_drawer_close"))
        returned = Page(ET.tostring(root, encoding="unicode")).returned_note(expanded)
        self.assertEqual(returned["description"], "标题\n完整正文 #话题")

    def test_titleless_and_multiparagraph_image_notes_adapt_across_scales(self):
        for scale, offset in [(1, 0), (0.75, 20), (1.4, 8)]:
            root = ET.Element("hierarchy")
            def add(parent, box, **kw):
                return node(parent, tuple(round(x * scale) + offset for x in box), **kw)
            add(root, (20, 20, 100, 50), text="作者", resource="nickNameTV")
            add(root, (150, 20, 190, 50), resource="moreOperateIV")
            add(root, (0, 60, 200, 400), desc="图片,第1张,共2张")
            counter = add(root, (160, 70, 195, 100), kind="android.widget.LinearLayout")
            add(counter, (160, 70, 195, 100), text="1/2")
            body = add(root, (0, 410, 200, 520), kind="android.widget.LinearLayout")
            add(body, (10, 420, 190, 510), text="第一行无独立标题正文\n第二行补充内容")
            footer = add(root, (0, 530, 200, 570), kind="android.widget.LinearLayout")
            add(footer, (10, 535, 190, 565), text="编辑于 昨天 18:30 广东")
            add(root, (10, 700, 100, 750), desc="评论框")
            parsed = Page(ET.tostring(root, encoding="unicode")).image_note()
            self.assertEqual(parsed["title"], "")
            self.assertEqual(parsed["description"], "第一行无独立标题正文\n第二行补充内容")
            verify_card(Card("IMAGE_TEXT", "第一行无独立标题正文", "作者", (1, 1, 2, 2)), parsed)
            verify_card(Card("IMAGE_TEXT", "第一行无独立标题正文…", "作者", (1, 1, 2, 2)), parsed)

    def test_scrolled_long_note_return_and_paragraph_merging(self):
        merged = merge_note_paragraphs(["第一段…"], ["第一段完整内容", "第二段内容"], title="长文标题")
        self.assertEqual(merged, ["第一段完整内容", "第二段内容"])
        expected = dict(noteType="IMAGE_TEXT", authorNickname="作者",
                        title="长文标题", description="第一段完整内容\n第二段内容")
        root = ET.Element("hierarchy")
        node(root, (20, 20, 100, 50), text="作者", resource="nickNameTV")
        node(root, (150, 20, 190, 50), resource="moreOperateIV")
        body = node(root, (0, 120, 200, 420), kind="android.widget.LinearLayout")
        node(body, (10, 130, 190, 400), text="第二段内容")
        node(root, (10, 450, 190, 490), text="共 12 条评论")
        node(root, (10, 700, 100, 750), desc="评论框")
        page = Page(ET.tostring(root, encoding="unicode"))
        self.assertEqual(page.returned_note(expected), expected)
        with self.assertRaises(PageMismatch):
            page.returned_note({**expected, "description": "不相关的正文"})

    def test_certified_author_profile_identity_and_return_guard(self):
        root = ET.Element("hierarchy")
        node(root, (0, 142, 3, 193), text="Manulife 宏利", resource="nickNameTV")
        header = node(root, (0, 204, 1080, 558), kind="android.view.ViewGroup")
        node(header, (6, 234, 330, 558), desc="头像,Manulife 宏利", kind="android.widget.ImageView")
        node(header, (342, 299, 704, 385), text="Manulife 宏利")
        cert = node(header, (342, 397, 1035, 439), kind="android.widget.LinearLayout")
        node(cert, (390, 397, 958, 439), text="MANULIFE(INTERNATIONAL)LIMITED")
        node(header, (342, 451, 553, 493), text="IP：中国香港")
        page = Page(ET.tostring(root, encoding="unicode"))
        self.assertEqual(page.author_identity("Manulife 宏利"), "MANULIFE(INTERNATIONAL)LIMITED")
        with self.assertRaises(PageMismatch):
            page.returned_note(dict(noteType="IMAGE_TEXT", authorNickname="Manulife 宏利",
                                    title="", description="正文"))
        empty_id = Page(ET.tostring(root, encoding="unicode").replace("MANULIFE(INTERNATIONAL)LIMITED", "小红书号："))
        with self.assertRaises(PageMismatch):
            empty_id.author_identity("Manulife 宏利")

    def test_foreign_app_captcha_and_empty_bounds_are_rejected(self):
        with self.assertRaises(PageMismatch):
            Page('<hierarchy><node package="other"/></hierarchy>')
        root = ET.Element("hierarchy")
        node(root, (1, 1, 2, 2), text="拖动滑块完成验证")
        with self.assertRaises(PageMismatch):
            Page(ET.tostring(root, encoding="unicode"))
        with self.assertRaises(PageMismatch):
            center(ET.Element("node", bounds="[0,0][0,0]"))


if __name__ == "__main__":
    unittest.main()
