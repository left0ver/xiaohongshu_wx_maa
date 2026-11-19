"""Semantic parsing, lazy fallbacks and stale-control rejection without a device."""

import ast
import json
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent))
from hierarchy import (CONTROL_PAGES, OCR_ONLY_LABELS, HierarchyUnavailable, choose_snapshot, complete_for,
                       correct_ocr, field_evidence, fill_containers, page_check, page_lines, refresh_target,
                       text_lines)
from layout import reading_rows
from collection import parse_basic_info
from test_collection import AUTHOR, DETAILS, INFO, PROFILE, line


def document(*nodes):
    root = ET.Element("hierarchy")
    for attributes in nodes:
        ET.SubElement(root, "node", {"package": "com.tencent.mm", "enabled": "true",
                                   "class": "android.widget.TextView", **attributes})
    return ET.tostring(root, encoding="unicode")


def home():
    return text_lines(document(
        {"text": "发现", "bounds": "[800,2800][1000,2900]"},
        {"text": "通讯录", "bounds": "[400,2800][600,2900]"}), 1440, 3116)


def control(text, x, y, width, height, attribute="text"):
    """A control as text_lines returns it, in 720-wide coordinates."""
    return {"text": text, "box": [x, y, width, height], "score": 1.0, "source": "hierarchy",
            "selector": {"attribute": attribute, "value": text, "resource": "", "class": "android.widget.TextView"}}


def label(text, resource, bounds, **attributes):
    return {"text": text, "resource-id": "com.tencent.mm:id/" + resource, "bounds": bounds, **attributes}


def box(resource, bounds, *children, **attributes):
    return ({"resource-id": "com.tencent.mm:id/" + resource, "bounds": bounds, "class": "android.widget.FrameLayout",
             **attributes}, list(children))


def sheet(*children):
    return box("", "[0,960][1440,3116]", *children)


def layer(resource, *children):
    return box(resource, "[0,0][1440,3116]", *children)


def screen(*layers):
    return box("", "[0,0][1440,3116]", *layers)


def tree(root):
    """Nested hierarchy XML: a node is (attributes, children) or an attribute dict."""
    document = ET.Element("hierarchy")

    def add(parent, item):
        attributes, children = item if isinstance(item, tuple) else (item, [])
        node = ET.SubElement(parent, "node", {"package": "com.tencent.mm", "enabled": "true",
                                              "class": "android.widget.TextView", **attributes})
        for child in children:
            add(node, child)

    add(document, root)
    return ET.tostring(document, encoding="unicode")


class HierarchyTests(unittest.TestCase):
    def test_visible_child_survives_an_invisible_parent(self):
        xml = '<hierarchy><node package="com.tencent.mm" visible-to-user="false" text="hidden" bounds="[0,0][1,1]">' \
              '<node package="com.tencent.mm" visible-to-user="true" text="更多信息" bounds="[0,100][200,200]"/>' \
              '</node></hierarchy>'
        self.assertEqual([item["text"] for item in text_lines(xml, 720, 1558)], ["更多信息"])

    def test_basic_info_from_controls_alone_skips_ocr(self):
        known = [control(item["text"], *item["box"]) for item in INFO]
        templates, ocr = Mock(), Mock()
        lines, source = choose_snapshot("author-basic-info", 1558, None, lambda: known, templates, ocr)
        self.assertEqual(source, "hierarchy")
        self.assertEqual(parse_basic_info(lines)["authorDouyinId"], "sphExample123")
        ocr.assert_not_called()

    def test_drawer_is_read_from_controls_with_ocr_only_inside_comment_bodies(self):
        # The work, the commenter row and the like count are controls; the
        # comment body is an empty container, so only it is recognized.
        xml = tree(screen(
            layer("", sheet(
                label("评论 294", "", "[60,964][300,1020]"),
                label(AUTHOR, "", "[198,1100][520,1232]"), label("作者", "", "[530,1140][600,1190]"),
                label("福建", "", "[610,1140][680,1190]"), label("8月1日", "", "[690,1140][860,1190]"),
                label("完整的作品文案", "", "[198,1208][1384,1270]"),
                box("", "[0,1450][1440,1680]",
                    label("", "", "[54,1460][190,1596]", **{"content-desc": "头像", "class": "android.widget.ImageView"}),
                    box("", "[198,1460][920,1590]",
                        label("评论者", "", "[198,1460][400,1590]", clickable="true"),
                        label("四川", "", "[420,1500][500,1550]"),
                        label("8月1日", "", "[520,1500][900,1550]"),
                        **{"class": "android.widget.LinearLayout"}),
                    box("", "[1180,1470][1400,1580]",
                        label("12", "", "[1250,1500][1300,1552]"),
                        **{"class": "android.widget.LinearLayout", "clickable": "true"}),
                    box("", "[58,1600][1384,1660]", **{"class": "android.widget.RelativeLayout"}),
                    **{"class": "android.view.ViewGroup"}),
                label("帮她上热门", "", "[240,1400][700,1440]"))),
            layer("",
                  ({"class": "android.widget.SeekBar", "bounds": "[16,2800][1424,2810]"}, []),
                  label("被盖住的推荐流文案", "", "[98,1600][1384,1700]")),
        ))
        lines = text_lines(xml, 1440, 3116, containers=True)
        body = line("不能采集成作品文案", 99, 803, 300)
        regional, full = Mock(return_value=[body]), Mock()
        result, source = choose_snapshot("video-details", 1558, AUTHOR, lambda: lines, Mock(), full, regional_ocr=regional)
        full.assert_not_called()
        self.assertEqual(source, "mixed")
        texts = [item["text"] for item in result]
        self.assertIn("不能采集成作品文案", texts)
        self.assertNotIn("被盖住的推荐流文案", texts)
        x, y, width, height = regional.call_args.args[0]
        self.assertEqual((x, y, height), (29, 800, 30))

    def test_visible_layer_does_not_depend_on_sibling_order(self):
        drawer = layer("", sheet(label("评论 3", "", "[60,964][300,1020]")))
        feed = layer("",
                     ({"class": "android.widget.SeekBar", "bounds": "[16,2800][1424,2810]"}, []),
                     label("推荐流文案", "", "[98,2600][1384,2700]"))
        for name, layers in (("feed-first", (feed, drawer)), ("drawer-first", (drawer, feed))):
            with self.subTest(order=name):
                texts = [item["text"] for item in text_lines(tree(screen(*layers)), 1440, 3116)]
                self.assertEqual(texts, ["评论 3"])

    def test_profile_sheet_covers_drawer_and_feed(self):
        profile = box("", "[0,0][1440,3116]", sheet(label("浙江 宁波 男", "", "[388,1050][680,1210]")))
        xml = tree(screen(
            layer("", sheet(label("评论 3", "", "[60,964][300,1020]"))),
            profile,
            layer("",
                  ({"class": "android.widget.SeekBar", "bounds": "[16,2800][1424,2810]"}, []),
                  label("推荐流文案", "", "[98,2600][1384,2700]")),
        ))
        self.assertEqual([item["text"] for item in text_lines(xml, 1440, 3116)], ["浙江 宁波 男"])

    def test_sheet_beside_the_video_pager_hides_the_pager(self):
        content = box("", "[0,0][1440,3116]",
                      box("", "[0,0][1440,3116]", sheet(label("广东 韶关 男", "", "[388,1050][680,1210]"))),
                      layer("",
                            ({"class": "android.widget.SeekBar", "bounds": "[16,2800][1424,2810]"}, []),
                            label("热评", "", "[60,2600][200,2680]")))
        texts = [item["text"] for item in text_lines(tree(layer("", content)), 1440, 3116)]
        self.assertEqual(texts, ["广东 韶关 男"])

    def test_padded_labels_share_one_reading_row(self):
        xml = tree(screen(label("评论者", "kbf", "[198,1460][400,1590]"), label("四川", "hgh", "[420,1500][500,1550]")))
        rows = reading_rows(text_lines(xml, 1440, 3116))
        self.assertEqual([row["text"] for row in rows], ["评论者四川"])

    def test_icon_names_are_not_page_text(self):
        xml = tree(screen(label("", "a_4", "[54,1460][190,1600]", **{"content-desc": "头像", "class": "android.widget.ImageView"}),
                          label("评论者", "kbf", "[198,1460][400,1590]")))
        lines = text_lines(xml, 1440, 3116)
        self.assertEqual([item["text"] for item in page_lines(lines)], ["评论者"])
        self.assertTrue(next(item for item in lines if item["text"] == "头像")["icon"])

    def test_container_description_without_labelled_children_is_text(self):
        xml = tree(screen(box("jdo", "[108,1960][840,2200]", box("", "[108,1960][400,2200]"),
                              **{"content-desc": "公众号: 小琦的House"})))
        self.assertEqual([item["text"] for item in text_lines(xml, 1440, 3116)], ["公众号: 小琦的House"])

    def test_ocr_fills_containers_but_never_repeats_or_replaces_control_text(self):
        controls = [control("广东 韶关 男", 194, 549, 146, 34), {"text": "", "container": "profile-name", "box": [194, 488, 339, 77]}]
        recognized = [line("林逸cut", 190, 501, 135, 44), line("广东韶关男", 190, 546, 144, 35), line("页面别处", 30, 1200)]
        texts = [item["text"] for item in fill_containers(controls, recognized)]
        self.assertEqual(sorted(texts), ["广东 韶关 男", "林逸cut"])

    def test_description_inside_container_is_replaced_by_drawn_text(self):
        button = control("私信按钮", 367, 805, 317, 79, attribute="content-desc")
        holes = [{"text": "", "container": "message-button", "box": [367, 805, 317, 79]}]
        texts = [item["text"] for item in fill_containers([button, *holes], [line("私信", 494, 825, 59, 36)])]
        self.assertEqual(texts, ["私信"])

    def test_bio_keeps_the_more_complete_reading(self):
        hole = {"text": "", "container": "bio", "box": [36, 640, 580, 150]}
        partial = control("第一行", 36, 643, 580, 30)
        ocr = [line("第一行", 36, 643), line("第二行更多内容", 36, 680), line("第三行", 36, 717)]
        self.assertEqual([item["text"] for item in fill_containers([partial, hole], ocr)],
                         ["第一行", "第二行更多内容", "第三行"])
        full = control("第一行\n第二行更多内容\n第三行", 36, 643, 580, 110)
        self.assertEqual([item["text"] for item in fill_containers([full, hole], ocr[:1])],
                         ["第一行\n第二行更多内容\n第三行"])

    def test_one_ocr_line_fills_one_container(self):
        holes = [{"text": "", "container": "bio", "box": [36, 643, 50, 100]},
                 {"text": "", "container": "bio", "box": [22, 643, 79, 100]}]
        self.assertEqual(len(fill_containers(holes, [line("同名", 28, 706, 62, 39)])), 1)

    def test_comment_body_ignores_the_avatar_column(self):
        name = control("Allan宅寳", 101, 872, 110, 34)
        hole = {"text": "", "container": "comment-body", "box": [29, 912, 663, 35]}
        ocr = [line("100", 42, 912, 25, 17), line("你血糖不高真的很幸运了", 99, 915, 298, 28)]
        self.assertEqual([item["text"] for item in fill_containers([name, hole], ocr)],
                         ["Allan宅寳", "你血糖不高真的很幸运了"])

    def test_labels_with_a_dynamic_suffix_share_their_page_check(self):
        from hierarchy import page_check
        self.assertIs(page_check("search-after-sort-最新"), page_check("search-after-"))
        self.assertIsNone(page_check("unknown-label"))

    def test_scrolled_comment_pages_are_read_like_the_return_check(self):
        # Real Hebe-BD00 drawer: full-screen OCR read three avatars as glyphs, which merged
        # 王腊梅's header into the emoji-only comment above it and failed the return check.
        from comments import parse_comment_drawer
        from hierarchy import page_check
        data = Path(__file__).with_name("testdata")
        xml = (data / "comment-drawer-sticker-emoji-hebe.xml").read_text(encoding="utf-8")
        frame = json.loads((data / "comment-drawer-sticker-emoji-hebe.json").read_text(encoding="utf-8"))
        controls = text_lines(xml, 1236, 2676, containers=True)
        for label in ("comments-page-2", "comments-page-12", "comments-page-12-back"):
            self.assertIs(page_check(label), page_check("comments-return"))

        readings = {}
        for label in ("comments-page-12", "comments-return"):
            full = Mock(return_value=frame["full"])
            lines, source = choose_snapshot(label, frame["height"], None, lambda: [dict(c) for c in controls],
                                            Mock(), full, regional_ocr=lambda roi: [dict(l) for l in frame["regional"]])
            full.assert_not_called()
            self.assertEqual(source, "mixed")
            self.assertFalse([l for l in lines if l["box"][0] < 92 and l["box"][1] > 480 and l.get("source") != "hierarchy"])
            readings[label] = parse_comment_drawer(lines, frame["height"], parent_event_id="P")["comments"]
        # Sticker and emoji-only comments carry no drawn text, so they are skipped on both readings.
        self.assertEqual([c["nickname"] for c in readings["comments-page-12"]], ["王腊梅"])
        self.assertEqual([c["key"] for c in readings["comments-page-12"]],
                         [c["key"] for c in readings["comments-return"]])

    def test_reply_ad_and_message_buttons_are_read_directly_from_controls(self):
        from comments import parse_comment_drawer

        # Zero resource-ids: message-center 2-button bar -> "回复", comment-drawer ad item -> "广告",
        # message button content-desc -> "私信", and composer emoji ImageView -> icon=True.
        xml = tree(box("", "[0,0][1236,2676]",
            box("", "[0,300][1236,680]",
                label("狼王", "", "[221,310][420,380]"),
                box("", "[221,498][549,633]",
                    box("", "[221,498][414,630]", clickable="true", **{"class": "android.widget.LinearLayout"}),
                    box("", "[414,498][549,633]", clickable="true", **{"class": "android.widget.LinearLayout"}),
                    **{"class": "android.widget.LinearLayout"}),
                **{"class": "android.widget.LinearLayout"}),
            box("", "[0,914][1236,1391]",
                label("", "", "[30,944][167,1081]", clickable="true", **{"content-desc": "头像", "class": "android.widget.ImageView"}),
                box("", "[173,963][1236,1021]",
                    label("洛克王国:世界", "", "[173,926][442,1058]", clickable="true"),
                    **{"class": "android.widget.LinearLayout"}),
                box("", "[1002,926][1187,1062]", clickable="true", **{"class": "android.widget.FrameLayout"}),
                box("", "[173,1046][1187,1342]",
                    label("了解详情", "", "[904,1209][1150,1305]"),
                    clickable="true", **{"class": "android.widget.LinearLayout"}),
                **{"class": "android.view.ViewGroup"}),
            box("", "[636,1500][1176,1620]", **{"content-desc": "私信按钮"}),
            label("", "", "[38,1700][160,1820]", **{"content-desc": "[强]", "class": "android.widget.ImageView"}),
        ))
        items = text_lines(xml, 1236, 2676, containers=True)
        holes = [item for item in items if item.get("container")]
        self.assertEqual(holes, [])
        self.assertEqual(
            sorted(item["text"] for item in page_lines(items)),
            ["了解详情", "回复", "广告", "洛克王国:世界", "狼王", "私信"],
        )
        self.assertTrue(next(item for item in items if item["text"] == "[强]")["icon"])

        # Comment drawer with an ID-free ad row between two normal comments skips the ad cleanly.
        drawer_xml = tree(box("", "[0,0][1236,2676]",
            label("评论 15", "", "[172,751][420,813]"),
            box("", "[0,900][1236,1150]",
                label("", "", "[30,920][167,1057]", clickable="true", **{"content-desc": "头像", "class": "android.widget.ImageView"}),
                box("", "[173,926][993,984]",
                    label("正常用户A", "", "[173,926][380,984]", clickable="true"),
                    label("江苏", "", "[400,926][486,984]"),
                    label("2天前", "", "[506,926][680,984]"),
                    **{"class": "android.widget.LinearLayout"}),
                box("", "[49,1000][1187,1090]",
                    label("说得太对了", "", "[173,1000][600,1090]"),
                    **{"class": "android.widget.RelativeLayout"}),
                box("", "[993,920][1236,1050]",
                    label("", "", "[1130,940][1186,996]", **{"class": "android.widget.ImageView"}),
                    clickable="true", **{"class": "android.widget.LinearLayout"}),
                **{"class": "android.view.ViewGroup"}),
            box("", "[0,1200][1236,1680]",
                label("", "", "[30,1230][167,1367]", clickable="true", **{"content-desc": "头像", "class": "android.widget.ImageView"}),
                box("", "[173,1249][1236,1307]",
                    label("红果免费短剧", "", "[173,1212][431,1344]", clickable="true"),
                    **{"class": "android.widget.LinearLayout"}),
                box("", "[1002,1212][1187,1348]", clickable="true", **{"class": "android.widget.FrameLayout"}),
                box("", "[173,1332][1187,1628]",
                    label("看漫剧还充会员？我已经免费看半年了", "", "[642,1369][1150,1431]"),
                    label("了解详情", "", "[904,1495][1150,1591]"),
                    clickable="true", **{"class": "android.widget.LinearLayout"}),
                **{"class": "android.view.ViewGroup"}),
            box("", "[0,1700][1236,1950]",
                label("", "", "[30,1720][167,1857]", clickable="true", **{"content-desc": "头像", "class": "android.widget.ImageView"}),
                box("", "[173,1726][993,1784]",
                    label("正常用户B", "", "[173,1726][380,1784]", clickable="true"),
                    label("浙江", "", "[400,1726][486,1784]"),
                    label("1天前", "", "[506,1726][680,1784]"),
                    **{"class": "android.widget.LinearLayout"}),
                box("", "[49,1800][1187,1890]",
                    label("支持一下", "", "[173,1800][600,1890]"),
                    **{"class": "android.widget.RelativeLayout"}),
                box("", "[993,1720][1236,1850]",
                    label("", "", "[1130,1740][1186,1796]", **{"class": "android.widget.ImageView"}),
                    clickable="true", **{"class": "android.widget.LinearLayout"}),
                **{"class": "android.view.ViewGroup"}),
            label("发表评论:", "", "[173,2521][961,2657]", **{"class": "android.widget.EditText"}),
        ))
        parsed = parse_comment_drawer(page_lines(text_lines(drawer_xml, 1236, 2676, containers=True)), 1559, parent_event_id="W")
        self.assertEqual([c["nickname"] for c in parsed["comments"]], ["正常用户A", "正常用户B"])
        self.assertEqual([c["content"] for c in parsed["comments"]], ["说得太对了", "支持一下"])

    def test_comment_body_with_c1_child_uses_pure_controls_without_ocr(self):
        from comments import parse_comment_drawer
        xml = tree(screen(
            layer("", sheet(
                label("评论 12", "", "[151,656][341,711]"),
                box("", "[0,1260][1080,1520]",
                    label("", "", "[27,1280][145,1398]", clickable="true", **{"content-desc": "头像", "class": "android.widget.ImageView"}),
                    box("", "[151,1277][870,1392]",
                        label("楠霖27", "", "[151,1277][271,1392]", clickable="true"),
                        label("", "", "[238,1275][357,1394]", **{"content-desc": "视频号logo", "class": "android.widget.ImageView"}),
                        label("福建", "", "[335,1309][411,1360]"),
                        label("11天前", "", "[427,1309][863,1360]"),
                        **{"class": "android.widget.LinearLayout"}),
                    box("", "[43,1371][1037,1478]",
                        label("从灶台来看父亲不爱干活[Facepalm]", "", "[151,1371][1037,1478]"),
                        **{"class": "android.widget.RelativeLayout"}),
                    box("", "[870,1280][1050,1390]",
                        label("6754", "", "[885,1310][976,1361]"),
                        label("", "", "[985,1310][1035,1361]", **{"class": "android.widget.ImageView"}),
                        clickable="true", **{"class": "android.widget.LinearLayout"}),
                    **{"class": "android.view.ViewGroup"}),
                label("AI吕哥说#\xa0发表评论:", "", "[151,2273][838,2391]", **{"class": "android.widget.EditText"}),
            )),
        ))
        controls = text_lines(xml, 1080, 2408, containers=True)
        regional, full = Mock(), Mock()
        result, source = choose_snapshot("comments", 1605, None, lambda: controls, Mock(), full, regional_ocr=regional)
        self.assertEqual(source, "hierarchy")
        regional.assert_not_called()
        full.assert_not_called()
        parsed = parse_comment_drawer(result, 1605, parent_event_id="W1")
        self.assertEqual(len(parsed["comments"]), 1)
        self.assertEqual(parsed["comments"][0]["nickname"], "楠霖27")
        self.assertEqual(parsed["comments"][0]["content"], "从灶台来看父亲不爱干活[Facepalm]")
        self.assertTrue(parsed["comments"][0]["_hasChannelsBadge"])

    def test_non_author_feed_and_return_labels_skip_container_ocr(self):
        xml = tree(screen(layer("nsv",
            label("关注", "tv1", "[260,120][360,200]"),
            label("推荐", "tv2", "[540,120][640,200]", selected="true"),
            box("dzo", "[0,2100][500,2260]"),
            label("3967", "msm", "[782,2200][866,2260]"),
        )))
        controls = text_lines(xml, 1080, 2408, containers=True)
        for snap_label in ("feed-after-like", "feed-after-collect", "tab-check-推荐", "share-return-1"):
            with self.subTest(label=snap_label):
                regional, full = Mock(), Mock()
                _, source = choose_snapshot(snap_label, 1605, None, lambda: controls, Mock(), full, regional_ocr=regional)
                self.assertEqual(source, "hierarchy")
                regional.assert_not_called()
                full.assert_not_called()

    def test_profile_follow_and_return_scope_containers_minimally(self):
        xml = tree(screen(sheet(
            box("fzm", "[291,731][800,848]"),
            label("浙江 杭州 男 ", "ov9", "[291,788][511,908]"),
            label("13条原创内容", "fzr", "[54,1107][286,1151]"),
            box("fyk", "[54,1189][529,1307]"),
            box("mld", "[551,1189][1026,1307]", **{"content-desc": "私信按钮"}),
        )))
        controls = text_lines(xml, 1080, 2408, containers=True)
        # profile-after-first-work succeeds with zero OCR via mld + fzr controls.
        reg_ret, full_ret = Mock(), Mock()
        _, src_ret = choose_snapshot("profile-after-first-work", 1605, None, lambda: controls, Mock(), full_ret, regional_ocr=reg_ret)
        self.assertEqual(src_ret, "hierarchy")
        reg_ret.assert_not_called()
        full_ret.assert_not_called()
        # profile-follow-check only crops the single fyk button, not fzm above it.
        reg_fol = Mock(return_value=[line("已关注", 160, 815, 80, 34)])
        full_fol = Mock()
        _, src_fol = choose_snapshot("profile-follow-check", 1605, None, lambda: controls, Mock(), full_fol, regional_ocr=reg_fol)
        self.assertEqual(src_fol, "mixed")
        full_fol.assert_not_called()
        roi_x, roi_y, roi_w, roi_h = reg_fol.call_args.args[0]
        self.assertGreater(roi_y, 750)

    def test_link_video_transitional_webview_skips_full_ocr(self):
        webview = [control("关闭", 40, 90, 60, 34), control("更多", 630, 90, 60, 34)]
        regional, full = Mock(), Mock()
        chosen, source = choose_snapshot("link-video-1", 1605, None, lambda: webview, Mock(), full, regional_ocr=regional)
        self.assertEqual(source, "hierarchy")
        self.assertEqual(chosen, webview)
        regional.assert_not_called()
        full.assert_not_called()

    def test_failed_page_check_falls_back_to_full_ocr(self):
        known = [control("评论294", 27, 482, 160, 28)]
        full = Mock(return_value=DETAILS)
        lines, source = choose_snapshot("video-details", 1558, AUTHOR, lambda: known, Mock(), full,
                                        regional_ocr=Mock())
        full.assert_called_once()
        self.assertEqual(lines, DETAILS)

    def test_pages_not_yet_checked_against_controls_keep_full_ocr(self):
        known = [control("消息", 40, 300, 60, 34)]
        full = Mock(return_value=[line("消息", 40, 300)])
        choose_snapshot("messages-center", 1558, None, lambda: known, Mock(), full, regional_ocr=Mock())
        full.assert_called_once()

    def test_misread_characters_are_corrected_without_moving_ocr_lines(self):
        recognized = [line("这是封离别信#吉他弹唱#Le", 98, 604, 594), line("ttinggo", 99, 643, 58)]
        caption = control("这是一封离别信#吉他弹唱#Lettinggo", 96, 590, 600, 90)
        lines, source = choose_snapshot("video-details", 1558, AUTHOR, lambda: [caption], Mock(), lambda: recognized)
        self.assertEqual(source, "mixed")
        self.assertEqual([item["text"] for item in lines], ["这是一封离别信#吉他弹唱#Le", "ttinggo"])
        self.assertEqual([item["box"] for item in lines], [item["box"] for item in recognized])
        self.assertEqual((lines[0]["source"], lines[0]["ocrText"]), ("hierarchy", "这是封离别信#吉他弹唱#Le"))

    def test_one_ocr_row_is_corrected_from_several_controls(self):
        recognized = [line("歪不歪Uv.作者福建6天前", 98, 535, 350, 31)]
        controls = [control("歪不歪Uu.", 100, 513, 113, 77), control("作者", 261, 536, 50, 34),
                    control("福建", 322, 536, 50, 34), control("6天前", 383, 536, 133, 34)]
        lines = correct_ocr(recognized, controls)
        self.assertEqual(lines[0]["text"], "歪不歪Uu.作者福建6天前")
        self.assertEqual(lines[0]["box"], recognized[0]["box"])

    def test_covered_controls_and_icon_placeholders_do_not_inject_text(self):
        composer = line("AI吕哥说发表评论：", 112, 1539, 261, 29)
        advert = line("广告", 591, 1367, 94, 44)
        controls = [control("AI吕哥说#\xa0发表评论:", 100, 1515, 458, 79),
                    control("20", 359, 1529, 26, 29),
                    control("胖人脖子黑咋回事？家里人一定要注意！", 29, 1363, 662, 77)]
        lines = correct_ocr([composer, advert], controls)
        self.assertEqual(lines, [composer, advert])

    def test_description_controls_are_not_used_for_correction(self):
        recognized = [line("私信", 494, 756, 58, 36)]
        button = control("私信按钮", 420, 734, 363, 79, attribute="content-desc")
        self.assertEqual(choose_snapshot("author-profile", 1558, AUTHOR, lambda: [button], Mock(),
                                         lambda: recognized), (recognized, "ocr"))

    def test_container_text_summary_does_not_duplicate_child_labels(self):
        xml = '<hierarchy><node package="com.tencent.mm" text="更多信息" bounds="[0,0][500,500]">' \
              '<node package="com.tencent.mm" text="更多信息" bounds="[0,100][200,200]"/>' \
              '</node></hierarchy>'
        self.assertEqual(len(text_lines(xml, 720, 1558)), 1)

    def test_certification_evidence_points_to_value_not_section_title(self):
        value = line("音乐达人", 250, 860)
        data = [*INFO, line("认证信息", 40, 795), line("认证信息", 40, 860), value]
        evidence = field_evidence(data, "author-basic-info")["authorCertification"]
        self.assertEqual(evidence["value"], "音乐达人")
        self.assertIn(value["box"], evidence["boxes"])
        self.assertNotIn(data[-3]["box"], evidence["boxes"])

    def test_physical_bounds_are_scaled_and_other_packages_are_ignored(self):
        lines = text_lines(document(
            {"text": "发现", "bounds": "[800,2800][1000,2900]"},
            {"text": "其他应用", "bounds": "[0,0][100,100]", "package": "other"},
            {"text": "停用", "bounds": "[0,0][100,100]", "enabled": "false"},
            {"text": "越界", "bounds": "[0,0][2000,100]"}), 1440, 3116)
        self.assertEqual(len(lines), 1)
        # Scaled to 720 wide; the padded single line keeps its centre at text height.
        self.assertEqual(lines[0]["box"], [400, 1408, 100, 34])

    def test_leaf_description_is_available_without_container_duplication(self):
        xml = '<hierarchy><node package="com.tencent.mm" content-desc="汇总" bounds="[0,0][500,500]">' \
              '<node package="com.tencent.mm" content-desc="更多信息" bounds="[0,100][200,200]"/>' \
              '</node></hierarchy>'
        lines = text_lines(xml, 720, 1558)
        self.assertEqual([line["text"] for line in lines], ["更多信息"])
        self.assertEqual(lines[0]["selector"]["attribute"], "content-desc")

    def test_invalid_xml_and_missing_wechat_are_unavailable(self):
        for xml in ('<!DOCTYPE hierarchy><hierarchy/>', '<broken', '<hierarchy/>'):
            with self.subTest(xml=xml), self.assertRaises(HierarchyUnavailable):
                text_lines(xml, 720, 1558)

    def test_verification_does_not_fall_back_or_click(self):
        hierarchy = Mock(side_effect=RuntimeError("Verification detected"))
        templates, ocr = Mock(), Mock()
        with self.assertRaisesRegex(RuntimeError, "Verification detected"):
            choose_snapshot("navigation-bottom", 1558, None, hierarchy, templates, ocr)
        templates.assert_not_called()
        ocr.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "Verification detected"):
            text_lines(document({"text": "请完成安全验证", "bounds": "[0,0][200,100]"}), 720, 1558)

    def test_semantic_success_skips_templates_and_ocr(self):
        templates, ocr = Mock(), Mock()
        lines, source = choose_snapshot("navigation-bottom", 1558, None, home, templates, ocr)
        self.assertEqual(source, "hierarchy")
        self.assertEqual(lines, home())
        templates.assert_not_called()
        ocr.assert_not_called()

    def test_partial_hierarchy_uses_template_before_ocr(self):
        target = {"navigation": "discover", "box": [400, 1400, 100, 50]}
        templates, ocr = Mock(return_value=[target]), Mock()
        lines, source = choose_snapshot("navigation-bottom", 1558, None, lambda: home()[:1], templates, ocr)
        self.assertEqual((lines, source), ([target], "template"))
        templates.assert_called_once()
        ocr.assert_not_called()

    def test_template_miss_is_the_only_navigation_ocr_path(self):
        order = []
        def read_hierarchy():
            order.append("hierarchy")
            raise HierarchyUnavailable("transition")
        def templates():
            order.append("template")
            return []
        def ocr():
            order.append("ocr")
            return home()
        self.assertEqual(choose_snapshot("navigation-bottom", 1558, None, read_hierarchy, templates, ocr)[1], "ocr")
        self.assertEqual(order, ["hierarchy", "template", "ocr"])

    def test_dynamic_content_requires_complete_fields_before_skipping_ocr(self):
        templates, ocr = Mock(), Mock(return_value=[])
        lines = [{"text": "基础信息", "box": [40, 480, 100, 30]}]
        self.assertFalse(complete_for(lines, "author-basic-info", 1558))
        self.assertEqual(choose_snapshot("author-basic-info", 1558, None, lambda: lines, templates, ocr)[1], "ocr")
        templates.assert_not_called()
        ocr.assert_called_once()

    def test_stale_missing_and_ambiguous_controls_are_rejected(self):
        previous = home()[0]
        self.assertEqual(refresh_target(home(), previous), previous)
        moved = {**previous, "box": [410, 1400, 100, 50]}
        for lines in ([], [previous, previous], [moved]):
            with self.subTest(lines=lines), self.assertRaises(RuntimeError):
                refresh_target(lines, previous)

    def test_search_scroll_grid_return_and_recover_labels_use_hierarchy_without_ocr(self):
        search_controls = [
            control("取消", 623, 106, 82, 34),
            control("今天吃什么", 72, 107, 367, 34),
            control("搜索", 511, 107, 97, 34),
            control("全部\n第 1 个标签，共 5 个", 0, 182, 108, 68, attribute="content-desc"),
            control("视频\n第 2 个标签，共 5 个", 108, 182, 108, 68, attribute="content-desc"),
            control("不限", 29, 272, 97, 66, attribute="content-desc"),
            control("最新", 126, 272, 97, 66, attribute="content-desc"),
            control("最热", 223, 272, 97, 66, attribute="content-desc"),
            control("1\n...生活的烟火气息 #今天吃什么\n郭俏一起吃点\n9分钟前", 367, 375, 338, 609, attribute="content-desc"),
        ]
        for snap_label in (
            "search-cards-scroll-2",
            "search-cards-scroll-10",
            "search-grid-2",
            "search-grid-recovered-2",
            "search-return-1",
            "search-return-recovered-1",
            "recover-check-0",
            "verify-stage-search",
        ):
            with self.subTest(label=snap_label):
                templates, ocr = Mock(), Mock()
                chosen, source = choose_snapshot(snap_label, 1559, None, lambda: search_controls, templates, ocr)
                self.assertEqual(source, "hierarchy")
                self.assertEqual(chosen, search_controls)
                ocr.assert_not_called()

    def test_all_collector_snapshot_labels_are_covered_by_control_pages_or_ocr_allowlist(self):
        def allowed_ocr(sample):
            return any(
                sample == entry or (entry.endswith("-") and sample.startswith(entry))
                for entry in OCR_ONLY_LABELS
            )

        collector_dir = Path(__file__).parent
        samples = set()
        for py_file in sorted(collector_dir.glob("*.py")):
            if py_file.name.startswith("test_"):
                continue
            parsed = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(parsed):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Attribute) and node.func.attr == "snapshot" and node.args:
                    arg = node.args[0]
                elif (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "call_with_captcha_pause"
                    and len(node.args) >= 3
                    and isinstance(node.args[1], ast.Attribute)
                    and node.args[1].attr == "snapshot"
                ):
                    arg = node.args[2]
                else:
                    continue
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    samples.add((py_file.name, arg.lineno, arg.value))
                elif (
                    isinstance(arg, ast.JoinedStr)
                    and arg.values
                    and isinstance(arg.values[0], ast.Constant)
                    and isinstance(arg.values[0].value, str)
                ):
                    prefix = arg.values[0].value
                    if prefix and not any(key != prefix and key.startswith(prefix) for key in CONTROL_PAGES):
                        samples.add((py_file.name, arg.lineno, prefix + "1"))

        missing = [
            f"{fname}:{lineno} ({sample!r})"
            for fname, lineno, sample in sorted(samples)
            if page_check(sample) is None and not allowed_ocr(sample)
        ]
        self.assertEqual(missing, [], f"Snapshot labels missing from CONTROL_PAGES: {missing}")

    def test_drawer_heading_carries_recyclerview_scroll_forward_state(self):
        feed_seek = ({"class": "android.widget.SeekBar", "resource-id": "com.tencent.mm:id/qm_",
                      "bounds": "[16,2800][1424,2810]", "scroll-forward": "true"}, [])
        feed = layer("nsv", label("推荐流文案", "o45", "[98,2600][1384,2700]"), feed_seek)
        for rv_fwd, with_spinner, expected in (("true", False, True), ("false", False, False), ("false", True, True)):
            children = [
                label("评论 9", "d4v", "[151,964][300,1020]"),
                ({"class": "androidx.recyclerview.widget.RecyclerView", "bounds": "[0,1100][1440,2800]",
                  "scrollable": "true", "scroll-forward": rv_fwd}, []),
            ]
            if with_spinner:
                children.append(({"class": "android.widget.ProgressBar", "bounds": "[680,2700][760,2780]"}, []))
            xml = tree(screen(feed, layer("ean", sheet(*children))))
            lines = text_lines(xml, 1440, 3116)
            heading = next(item for item in lines if item["text"] == "评论 9")
            self.assertEqual(heading.get("canScrollForward"), expected)

    def test_id_free_structural_topology_across_feed_profile_drawer_and_messages(self):
        from collection import comment_control_in_feed, drawer_author_nickname, parse_author, share_geometry
        from comments import parse_comment_drawer
        from interactions import _feed_collect_geometry, _feed_like_geometry, parse_comment_messages

        def node(cls, bounds, *children, **attrs):
            return ({"resource-id": "", "class": cls, "bounds": bounds, **attrs}, list(children))

        # 1. Feed with zero resource-ids, a "直播中" badge inside feed-author, and 0-comment slot.
        feed_xml = tree(node("android.widget.FrameLayout", "[0,0][1236,2676]",
            node("android.widget.LinearLayout", "[0,2097][1236,2676]",
                node("android.widget.TextView", "[49,2146][1187,2280]", text="家常红烧肉教程"),
                node("android.view.ViewGroup", "[0,2392][1236,2441]",
                    node("android.widget.FrameLayout", "[19,2392][1217,2441]",
                        node("android.widget.SeekBar", "[19,2417][1217,2429]"))),
                node("android.view.ViewGroup", "[0,2429][1236,2602]",
                    node("android.widget.FrameLayout", "[0,2429][589,2602]",
                        node("android.widget.LinearLayout", "[0,2429][463,2602]",
                            node("android.widget.TextView", "[40,2510][120,2560]", text="直播中"),
                            clickable="true")),
                    node("android.widget.LinearLayout", "[589,2439][1217,2592]",
                        node("android.view.ViewGroup", "[589,2439][746,2592]",
                            node("android.widget.ImageView", "[622,2439][713,2530]"),
                            node("android.widget.TextView", "[626,2542][710,2592]", text="88"),
                            clickable="true"),
                        node("android.widget.LinearLayout", "[746,2439][903,2592]",
                            node("android.widget.ImageView", "[779,2439][870,2530]"),
                            clickable="true"),
                        node("android.view.ViewGroup", "[903,2439][1060,2592]",
                            node("android.widget.ImageView", "[936,2439][1027,2530]"),
                            node("android.widget.TextView", "[940,2542][1024,2592]", text="19"),
                            clickable="true"),
                        node("android.widget.LinearLayout", "[1060,2439][1217,2592]",
                            node("android.widget.ImageView", "[1093,2439][1184,2530]"),
                            clickable="true")),
                    clickable="true"))))
        feed_controls = text_lines(feed_xml, 1236, 2676, containers=True)
        holes = [h for h in feed_controls if h.get("container") == "feed-author"]
        self.assertEqual(len(holes), 1)
        self.assertGreaterEqual(holes[0]["box"][0], 96)
        # resume-check / feed-ready succeed with zero OCR via structural bottom bar.
        reg_noop, full_noop = Mock(), Mock()
        ready_lines, ready_src = choose_snapshot("resume-check", 1559, None, lambda: feed_controls, Mock(), full_noop, regional_ocr=reg_noop)
        self.assertEqual(ready_src, "hierarchy")
        reg_noop.assert_not_called()
        full_noop.assert_not_called()
        self.assertIsNotNone(comment_control_in_feed(ready_lines, 1559))
        self.assertEqual(_feed_like_geometry(ready_lines, 1559)["count"], 88)
        self.assertEqual(_feed_collect_geometry(ready_lines, 1559)["count"], 19)
        self.assertTrue(440 <= share_geometry(ready_lines, 1559)["box"][0] <= 470)

        # 2. Profile with zero resource-ids: anchors on 头像 + 设置 + 私信按钮.
        profile_xml = tree(node("android.widget.FrameLayout", "[0,0][1236,2676]",
            node("android.widget.LinearLayout", "[0,750][1236,2676]",
                node("android.view.ViewGroup", "[0,800][1236,1020]",
                    node("android.widget.ImageView", "[60,820][260,1020]", **{"content-desc": "头像"}),
                    node("android.widget.LinearLayout", "[320,820][980,1020]",
                        node("android.view.View", "[320,830][780,910]"),
                        node("android.widget.TextView", "[320,930][600,990]", text="湖北 武汉 女")),
                    node("android.widget.RelativeLayout", "[1040,830][1180,970]", **{"content-desc": "设置"})),
                node("android.widget.TextView", "[60,1080][700,1140]", text="日常做菜分享"),
                node("android.widget.TextView", "[60,1170][400,1230]", text="32条原创内容"),
                node("android.widget.LinearLayout", "[60,1260][1176,1400]",
                    node("android.widget.FrameLayout", "[60,1260][600,1400]", clickable="true"),
                    node("android.widget.FrameLayout", "[636,1260][1176,1400]", clickable="true", **{"content-desc": "私信按钮"})))))
        prof_controls = text_lines(profile_xml, 1236, 2676, containers=True)
        self.assertEqual({h["container"] for h in prof_controls if h.get("container")},
                         {"profile-name", "follow-button", "bio"})
        # author-profile-touch skips bio container OCR.
        reg_touch = Mock(return_value=[line("阿梅厨房", 190, 490, 180, 34), line("关注", 160, 750, 60, 34)])
        prof_lines, prof_src = choose_snapshot("author-profile-touch", 1559, "阿梅厨房",
                                               lambda: prof_controls, Mock(), Mock(), regional_ocr=reg_touch)
        self.assertEqual(prof_src, "mixed")
        self.assertEqual(parse_author(prof_lines, "阿梅厨房")["authorNickname"], "阿梅厨房")

        # 3. Comment drawer over feed with zero resource-ids, "精选评论 108", native work author, and reply-expander.
        drawer_xml = tree(node("android.widget.FrameLayout", "[0,0][1236,2676]",
            node("android.widget.FrameLayout", "[0,0][1236,2676]",
                node("android.widget.SeekBar", "[19,2417][1217,2429]"),
                node("android.widget.TextView", "[49,2146][1187,2280]", text="底层视频流文案")),
            node("android.widget.FrameLayout", "[0,0][1236,2676]",
                node("android.widget.LinearLayout", "[0,696][1236,2676]",
                    node("android.widget.TextView", "[172,751][420,813]", text="精选评论 108"),
                    node("androidx.recyclerview.widget.RecyclerView", "[0,869][1236,2500]",
                        node("android.widget.LinearLayout", "[0,869][1236,1180]",
                            node("android.widget.RelativeLayout", "[173,882][1187,979]",
                                node("android.widget.LinearLayout", "[173,882][498,979]",
                                    node("android.widget.TextView", "[173,882][420,1014]", clickable="true", text="喜剧之王单口季")),
                                node("android.widget.LinearLayout", "[517,918][886,979]",
                                    node("android.widget.TextView", "[517,921][603,979]", text="作者"),
                                    node("android.widget.TextView", "[622,921][708,979]", text="上海"),
                                    node("android.widget.TextView", "[727,921][886,979]", text="11月6日"))),
                            node("android.widget.TextView", "[173,990][1100,1120]", text="作品正文内容"),
                            clickable="true"),
                        node("android.widget.FrameLayout", "[0,1245][1236,1620]",
                            node("android.widget.LinearLayout", "[0,1245][1236,1620]",
                                node("android.view.ViewGroup", "[0,1245][1236,1490]",
                                    node("android.widget.ImageView", "[30,1275][167,1412]", clickable="true", **{"content-desc": "头像"}),
                                    node("android.widget.LinearLayout", "[173,1294][993,1352]",
                                        node("android.widget.TextView", "[173,1257][320,1389]", clickable="true", text="小麦穗AAA"),
                                        node("android.widget.ImageView", "[322,1275][380,1370]", **{"content-desc": "视频号logo"}),
                                        node("android.widget.TextView", "[390,1294][476,1352]", text="广东"),
                                        node("android.widget.TextView", "[495,1294][993,1352]", text="3天前")),
                                    node("android.widget.RelativeLayout", "[49,1364][1187,1440]",
                                        node("android.widget.TextView", "[173,1364][1187,1440]", text="看着太有食欲了")),
                                    node("android.widget.LinearLayout", "[993,1251][1236,1399]",
                                        node("android.widget.ImageView", "[1130,1297][1186,1353]"),
                                        clickable="true")),
                                node("android.widget.FrameLayout", "[0,1490][1236,1610]", clickable="true")),
                            clickable="true"),
                        scrollable="true", **{"scroll-forward": "false"}),
                    node("android.widget.EditText", "[173,2521][961,2657]", text="佳一讲商业\xa0发表评论:")))))
        drawer_controls = text_lines(drawer_xml, 1236, 2676, containers=True)
        self.assertEqual([h for h in drawer_controls if h.get("container")], [])
        for drawer_label in ("comments", "comments-details", "comments-page-2", "comments-return"):
            reg_d, full_d = Mock(), Mock()
            d_lines, d_src = choose_snapshot(drawer_label, 1559, "喜剧之王单口季",
                                             lambda: drawer_controls, Mock(), full_d, regional_ocr=reg_d)
            self.assertEqual(d_src, "hierarchy")
            reg_d.assert_not_called()
            full_d.assert_not_called()
        self.assertNotIn("底层视频流文案", [l["text"] for l in d_lines])
        self.assertEqual(drawer_author_nickname(d_lines), "喜剧之王单口季")
        parsed_drawer = parse_comment_drawer(d_lines, 1559, parent_event_id="W")
        self.assertEqual(parsed_drawer["declaredTotal"], 108)
        self.assertIs(parsed_drawer["canScrollForward"], False)
        self.assertEqual(len(parsed_drawer["comments"]), 1)
        self.assertEqual(parsed_drawer["comments"][0]["nickname"], "小麦穗AAA")
        self.assertTrue(parsed_drawer["comments"][0]["_hasChannelsBadge"])
        self.assertEqual(len(parsed_drawer["replyExpands"]), 1)
        self.assertEqual(parsed_drawer["replyExpands"][0]["text"], "展开更多回复")

        # 4. Message center with zero resource-ids: 2-button horizontal bar [child(0)="回复", child(1)=thumb-like].
        msg_xml = tree(node("android.widget.FrameLayout", "[0,0][1236,2676]",
            node("androidx.appcompat.app.a", "[444,120][680,256]", **{"content-desc": "点赞"}),
            node("androidx.appcompat.app.a", "[680,120][916,256]", **{"content-desc": "评论"}),
            node("android.widget.LinearLayout", "[0,280][1236,660]",
                node("android.widget.TextView", "[221,298][403,356]", text="AI吕哥说"),
                node("android.widget.TextView", "[470,304][544,354]", text="昨天"),
                node("android.widget.TextView", "[221,370][752,432]", text="看到回复了，谢谢分享。"),
                node("android.widget.LinearLayout", "[221,498][549,633]",
                    node("android.widget.LinearLayout", "[221,498][414,630]", clickable="true"),
                    node("android.widget.LinearLayout", "[414,498][549,633]", clickable="true")))))
        msg_lines = page_lines(text_lines(msg_xml, 1236, 2676, containers=True))
        msgs = parse_comment_messages(msg_lines, height=1559)
        self.assertEqual(len(msgs), 1)
        like_x, like_y, like_w, like_h = msgs[0]["likeBox"]
        # Center of [414..549] in 720p is ~280 (never the old missed x=247 edge).
        self.assertTrue(270 <= like_x + like_w // 2 <= 290)

    def test_refresh_target_supports_structural_feed_role_button_box_and_count_updates(self):
        from collection import comment_control_in_feed

        before = [{
            "text": "194",
            "box": [641, 1529, 37, 29],
            "selector": {"attribute": "text", "value": "194", "resource": "com.tencent.mm:id/c6s", "class": "android.widget.TextView"},
            "feedRole": "comment",
            "buttonBox": [612, 1466, 96, 92],
            "iconBox": [632, 1466, 56, 56],
        }]
        after = [{
            "text": "195",
            "box": [641, 1529, 37, 29],
            "selector": {"attribute": "text", "value": "195", "resource": "com.tencent.mm:id/c6s", "class": "android.widget.TextView"},
            "feedRole": "comment",
            "buttonBox": [612, 1466, 96, 92],
            "iconBox": [632, 1466, 56, 56],
        }]
        ctrl = comment_control_in_feed(before, 1605)
        self.assertEqual(ctrl["box"], [612, 1466, 96, 92])
        refreshed = refresh_target(after, ctrl)
        self.assertEqual(refreshed["box"], [612, 1466, 96, 92])

    def test_control_first_field_extraction_avoids_ocr_concatenation_artifacts(self):
        from collection import parse_video
        from comments import parse_comment_drawer

        def node(cls, bounds, *children, **attrs):
            return ({"resource-id": "", "class": cls, "bounds": bounds, **attrs}, list(children))

        # Comment drawer with separate controls: "发仔" + "中国香港" + "1天前" + like count "4" + zero-width space in body,
        # plus "#都在搜: ..." on the same horizontal row as "评论 26".
        drawer_xml = tree(node("android.widget.FrameLayout", "[0,0][1080,2408]",
            node("android.widget.TextView", "[150,655][295,710]", text="评论 26"),
            node("android.widget.TextView", "[520,656][990,708]", text="#都在搜: 港式茶餐厅"),
            node("android.view.ViewGroup", "[40,850][1040,1090]",
                node("android.widget.LinearLayout", "[150,860][980,912]",
                    node("android.widget.LinearLayout", "[150,860][310,912]",
                        node("android.widget.TextView", "[150,860][302,911]", text="周周私房", clickable="true")),
                    node("android.widget.TextView", "[372,861][448,911]", text="作者"),
                    node("android.widget.TextView", "[463,861][540,911]", text="中国香港"),
                    node("android.widget.TextView", "[556,861][770,911]", text="2天前")),
                node("android.widget.TextView", "[150,940][990,1000]", text="鲫鱼豆腐汤\u200b做法")),
            node("android.view.ViewGroup", "[40,1200][1040,1420]",
                node("android.widget.ImageView", "[40,1210][120,1290]", **{"content-desc": "头像"}),
                node("android.widget.LinearLayout", "[129,1210][660,1265]",
                    node("android.widget.TextView", "[129,1210][249,1261]", text="发仔", clickable="true"),
                    node("android.widget.TextView", "[243,1210][395,1261]", text="中国香港"),
                    node("android.widget.TextView", "[411,1210][660,1261]", text="1天前")),
                node("android.widget.LinearLayout", "[920,1205][1040,1275]",
                    node("android.widget.TextView", "[952,1212][976,1261]", text="4"),
                    node("android.widget.ImageView", "[985,1212][1035,1261]"),
                    clickable="true"),
                node("android.widget.RelativeLayout", "[150,1272][990,1340]",
                    node("android.widget.TextView", "[150,1272][990,1330]", text="见到真面目\u200b[强]"))),
            node("android.view.ViewGroup", "[40,1450][1040,1660]",
                node("android.widget.ImageView", "[40,1460][120,1540]", **{"content-desc": "头像"}),
                node("android.widget.LinearLayout", "[125,1460][900,1515]",
                    node("android.widget.TextView", "[125,1460][243,1511]", text="貝...", clickable="true"),
                    node("android.widget.FrameLayout", "[279,1460][541,1511]",
                        node("android.widget.TextView", "[279,1460][541,1511]", text="已帮作者上热门")),
                    node("android.widget.TextView", "[560,1460][635,1511]", text="广东"),
                    node("android.widget.TextView", "[651,1460][900,1511]", text="8天前")),
                node("android.widget.LinearLayout", "[920,1455][1040,1525]",
                    node("android.widget.TextView", "[955,1462][978,1511]", text="3"),
                    node("android.widget.ImageView", "[985,1462][1035,1511]"),
                    clickable="true"),
                node("android.widget.RelativeLayout", "[150,1522][990,1590]",
                    node("android.widget.TextView", "[150,1522][990,1580]", text="Go go go 女食神"))),
            node("android.widget.EditText", "[150,2305][830,2356]", text="发表评论:")))

        lines = page_lines(text_lines(drawer_xml, 1080, 2408, containers=True))
        video = parse_video(lines, "周周私房")
        self.assertEqual(video["publishLocation"], "香港")
        self.assertEqual(video["publishTimeText"], "2天前")
        self.assertEqual(video["description"], "鲫鱼豆腐汤做法")

        parsed = parse_comment_drawer(lines, 1605, parent_event_id="vid-hk")
        self.assertEqual(parsed["declaredTotal"], 26)
        self.assertEqual(len(parsed["comments"]), 2)
        c = parsed["comments"][0]
        self.assertEqual(c["nickname"], "发仔")
        self.assertEqual(c["ipLocation"], "香港")
        self.assertEqual(c["commentTime"], "1天前")
        self.assertEqual(c["_likeCountText"], "4")
        self.assertEqual(c["content"], "见到真面目[强]")
        c2 = parsed["comments"][1]
        self.assertEqual(c2["nickname"], "貝...")
        self.assertEqual(c2["ipLocation"], "广东")
        self.assertEqual(c2["commentTime"], "8天前")
        self.assertEqual(c2["_likeCountText"], "3")

        from interactions import parse_comment_messages
        msg_lines = [
            {"text": "小朋友", "box": [129, 174, 90, 34], "source": "hierarchy"},
            {"text": "朋友", "box": [230, 176, 52, 29], "source": "hierarchy"},
            {"text": "前天", "box": [295, 177, 43, 29], "source": "hierarchy"},
            {"text": "看到回复了\u200b谢谢分享。", "box": [129, 216, 309, 36], "source": "hierarchy"},
            {"text": "佳一讲商业: 谢谢留言。", "box": [154, 259, 270, 29], "source": "hierarchy"},
            {"text": "回复", "box": [129, 311, 112, 34], "source": "hierarchy", "messageLikeBox": [256, 303, 48, 48]},
        ]
        msgs = parse_comment_messages(msg_lines, 1605)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["nickname"], "小朋友")
        self.assertEqual(msgs[0]["messageTime"], "前天")
        self.assertEqual(msgs[0]["content"], "看到回复了谢谢分享。")
        self.assertEqual(msgs[0]["quotedContent"], "佳一讲商业: 谢谢留言。")

    def test_ordinary_wechat_user_card_structural_roles_work_without_ids_or_fixed_px(self):
        from xml.sax.saxutils import escape
        from comments import parse_wechat_user_card

        def node(cls, bounds, *children, **attrs):
            base = {"package": "com.tencent.mm", "class": cls, "bounds": bounds,
                    "enabled": "true", "visible-to-user": "true", **attrs}
            attr_str = " ".join(f'{k}="{escape(str(v))}"' for k, v in base.items())
            inner = "".join(children)
            return f"<node {attr_str}>{inner}</node>"

        def tree(*children):
            return "<hierarchy>" + "".join(children) + "</hierarchy>"

        def card_xml(w, h, info_children):
            # Scale card layout dynamically from (w, h) to verify resolution/DPI independence.
            top = int(h * 0.73)
            hdr_bottom = int(h * 0.80)
            av_top = hdr_bottom
            av_size = int(w * 0.16)
            av_left = int(w * 0.06)
            info_left = av_left + av_size + int(w * 0.04)
            info_right = int(w * 0.94)
            return tree(node("android.widget.FrameLayout", f"[0,{top}][{w},{h}]",
                node("android.view.ViewGroup", f"[0,{top}][{w},{h}]",
                    node("android.widget.FrameLayout", f"[{av_left},{top}][{info_right},{hdr_bottom}]",
                        node("android.widget.FrameLayout", f"[{int(w*0.03)},{top+20}][{int(w*0.14)},{hdr_bottom-20}]",
                             clickable="true", **{"content-desc": "返回"}),
                        node("android.widget.FrameLayout", f"[{int(w*0.85)},{top+20}][{int(w*0.96)},{hdr_bottom-20}]",
                             clickable="true", **{"content-desc": "设置"})),
                    node("android.widget.ImageView", f"[{av_left},{av_top}][{av_left+av_size},{av_top+av_size}]",
                         clickable="true", **{"content-desc": "头像"}),
                    node("android.view.ViewGroup", f"[{info_left},{av_top}][{info_right},{av_top+int(av_size*1.1)}]",
                         *info_children))))

        # Case 1 (1080x2408): gender-only + full-width single-char bio -> profileLocation must be None.
        xml1 = card_xml(1080, 2408, [
            node("android.widget.TextView", "[281,1926][310,1988]", text="."),
            node("android.widget.TextView", "[281,1999][319,2050]", text="女"),
            node("android.widget.TextView", "[281,2061][972,2112]", text="。"),
        ])
        lines1 = page_lines(text_lines(xml1, 1080, 2408, containers=True))
        c1 = parse_wechat_user_card(lines1, ".", round(2408 * 720 / 1080))
        self.assertEqual((c1["nickname"], c1["profileLocation"], c1["gender"], c1["authorBio"]), (".", None, "女", "。"))

        # Case 2 (1440x3200 QHD): all 4 fields including overseas region without Chinese province prefix.
        xml2 = card_xml(1440, 3200, [
            node("android.widget.TextView", "[374,2560][590,2642]", text="献樾🔒流泪"),
            node("android.widget.TextView", "[374,2658][620,2724]", text="英格兰 谢菲尔德"),
            node("android.widget.TextView", "[636,2658][686,2724]", text="女"),
            node("android.widget.TextView", "[374,2740][1296,2806]", text="尘间不羡仙与鸳"),
        ])
        lines2 = page_lines(text_lines(xml2, 1440, 3200, containers=True))
        c2 = parse_wechat_user_card(lines2, "献樾🔒流泪", round(3200 * 720 / 1440))
        self.assertEqual((c2["nickname"], c2["profileLocation"], c2["gender"], c2["authorBio"]),
                         ("献樾🔒流泪", "英格兰 谢菲尔德", "女", "尘间不羡仙与鸳"))

        # Case 3 (1080x2408): bio-only (no location, no gender) at full width / row 3.
        xml3 = card_xml(1080, 2408, [
            node("android.widget.TextView", "[281,1926][420,1988]", text="张秀英"),
            node("android.widget.TextView", "[281,2061][972,2112]", text="好人一生平安"),
        ])
        lines3 = page_lines(text_lines(xml3, 1080, 2408, containers=True))
        c3 = parse_wechat_user_card(lines3, "张秀英", round(2408 * 720 / 1080))
        self.assertEqual((c3["nickname"], c3["profileLocation"], c3["gender"], c3["authorBio"]),
                         ("张秀英", None, None, "好人一生平安"))

        # Case 4 (1080x2408): location-only (wrap_content on row 2, no gender, no bio).
        xml4 = card_xml(1080, 2408, [
            node("android.widget.TextView", "[281,1926][380,1988]", text="遗憾"),
            node("android.widget.TextView", "[281,1999][442,2050]", text="安徽 蚌埠"),
        ])
        lines4 = page_lines(text_lines(xml4, 1080, 2408, containers=True))
        c4 = parse_wechat_user_card(lines4, "遗憾", round(2408 * 720 / 1080))
        self.assertEqual((c4["nickname"], c4["profileLocation"], c4["gender"], c4["authorBio"]),
                         ("遗憾", "安徽 蚌埠", None, None))

        # Case 5: full-width bio literally equal to "女" is classified as bio, not gender.
        xml5 = card_xml(1080, 2408, [
            node("android.widget.TextView", "[281,1926][380,1988]", text="测试"),
            node("android.widget.TextView", "[281,2061][972,2112]", text="女"),
        ])
        lines5 = page_lines(text_lines(xml5, 1080, 2408, containers=True))
        c5 = parse_wechat_user_card(lines5, "测试", round(2408 * 720 / 1080))
        self.assertEqual((c5["nickname"], c5["profileLocation"], c5["gender"], c5["authorBio"]),
                         ("测试", None, None, "女"))

    def test_comment_level_uses_grandparent_container_class_for_short_reply_nicknames(self):
        from xml.sax.saxutils import escape
        from comments import parse_comment_drawer

        def node(cls, bounds, *children, **attrs):
            base = {"package": "com.tencent.mm", "class": cls, "bounds": bounds,
                    "enabled": "true", "visible-to-user": "true", **attrs}
            attr_str = " ".join(f'{k}="{escape(str(v))}"' for k, v in base.items())
            return f"<node {attr_str}>{''.join(children)}</node>"

        # Reproduces Huawei run fe655468: reply by "." has clickable TextView expanded left to x=122 (<140),
        # but avatar.parent().parent() is FrameLayout (L2) vs LinearLayout (L1).
        drawer_xml = "<hierarchy>" + node("android.widget.FrameLayout", "[0,0][1236,2676]",
            node("android.widget.LinearLayout", "[0,696][1236,2676]",
                node("android.widget.TextView", "[172,751][420,813]", text="评论 405"),
                node("androidx.recyclerview.widget.RecyclerView", "[0,869][1236,2500]",
                    # L1 comment: grandparent is LinearLayout
                    node("android.widget.LinearLayout", "[0,900][1236,1200]",
                        node("android.view.ViewGroup", "[0,900][1236,1200]",
                            node("android.widget.ImageView", "[30,930][167,1067]", clickable="true", **{"content-desc": "头像"}),
                            node("android.widget.LinearLayout", "[173,935][993,995]",
                                node("android.widget.TextView", "[173,935][320,995]", clickable="true", text="念..."),
                                node("android.widget.TextView", "[340,935][426,995]", text="江西"),
                                node("android.widget.TextView", "[446,935][620,995]", text="4天前")),
                            node("android.widget.RelativeLayout", "[49,1010][1187,1120]",
                                node("android.widget.TextView", "[173,1010][1187,1120]", text="一级评论正文")),
                            node("android.widget.LinearLayout", "[993,920][1236,1050]",
                                node("android.widget.ImageView", "[1130,940][1186,996]"),
                                clickable="true"))),
                    # L2 reply by ".": grandparent is FrameLayout; nickname box starts at 210 (scaled x=122 <= 140!)
                    node("android.widget.FrameLayout", "[0,1210][1236,1500]",
                        node("android.view.ViewGroup", "[0,1210][1236,1500]",
                            node("android.widget.ImageView", "[142,1230][278,1366]", clickable="true", **{"content-desc": "头像"}),
                            node("android.widget.LinearLayout", "[210,1240][1060,1300]",
                                node("android.widget.TextView", "[210,1240][344,1300]", clickable="true", text="."),
                                node("android.widget.TextView", "[356,1240][442,1300]", text="河南"),
                                node("android.widget.TextView", "[462,1240][720,1300]", text="6月17日")),
                            node("android.widget.RelativeLayout", "[142,1310][1187,1400]",
                                node("android.widget.TextView", "[271,1310][1187,1400]", text="回复细品岁月:行呗")),
                            node("android.widget.LinearLayout", "[993,1230][1236,1360]",
                                node("android.widget.ImageView", "[1130,1250][1186,1306]"),
                                clickable="true"))),
                    scrollable="true", **{"scroll-forward": "false"}),
                node("android.widget.EditText", "[173,2521][961,2657]", text="佳一讲商业\xa0发表评论:"))) + "</hierarchy>"

        lines = page_lines(text_lines(drawer_xml, 1236, 2676, containers=True))
        parsed = parse_comment_drawer(lines, round(2676 * 720 / 1236), parent_event_id="video-1")
        self.assertEqual(len(parsed["comments"]), 2)
        parent_c, reply_c = parsed["comments"]
        self.assertEqual((parent_c["nickname"], parent_c["commentLevel"]), ("念...", 1))
        self.assertEqual((reply_c["nickname"], reply_c["commentLevel"]), (".", 2))
        self.assertEqual(reply_c["parentCommentKey"], parent_c["key"])
        self.assertEqual(reply_c["replyToNickname"], "细品岁月")


if __name__ == "__main__":
    unittest.main()
