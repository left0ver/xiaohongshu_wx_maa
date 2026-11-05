import sys
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "xiaohongshu" / "agent"))
from ui import Card, Page, PageMismatch, center, verify_card, verify_return


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
        with self.assertRaises(PageMismatch):
            page.author_identity("另一人")

    def test_video_uses_full_accessibility_caption(self):
        root = ET.Element("hierarchy")
        node(root, (20, 500, 100, 550), text="作者", resource="matrixNickNameView")
        node(root, (0, 490, 110, 560), desc="作者作者")
        node(root, (150, 20, 190, 50), desc="分享")
        caption = node(root, (0, 570, 200, 640), text="标题... 展开",
                       desc="标题\\n完整正文 #话题", resource="noteContentText")
        parsed = Page(ET.tostring(root, encoding="unicode")).note()
        self.assertEqual(parsed["noteType"], "VIDEO")
        self.assertEqual(parsed["title"], "标题")
        self.assertEqual(parsed["description"], "标题\n完整正文 #话题")
        caption.set("content-desc", "标题... 展开")
        with self.assertRaises(PageMismatch):
            Page(ET.tostring(root, encoding="unicode")).note()

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
