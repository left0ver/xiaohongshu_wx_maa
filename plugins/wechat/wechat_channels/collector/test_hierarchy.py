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
            layer("ean", sheet(
                label("评论 294", "d4v", "[60,964][300,1020]"),
                label(AUTHOR, "kbf", "[198,1100][520,1232]"), label("作者", "a8r", "[530,1140][600,1190]"),
                label("福建", "hgh", "[610,1140][680,1190]"), label("8月1日", "cgp", "[690,1140][860,1190]"),
                label("完整的作品文案", "c1_", "[198,1208][1384,1270]"),
                label("评论者", "kbf", "[198,1460][400,1590]"), label("四川", "hgh", "[420,1500][500,1550]"),
                label("8月1日", "c85", "[520,1500][900,1550]"), label("12", "aa0", "[1250,1500][1300,1552]"),
                box("c6d", "[58,1600][1384,1660]"),
                label("帮她上热门", "x1", "[240,1400][700,1440]"))),
            layer("nsv", label("被盖住的推荐流文案", "o45", "[98,1600][1384,1700]")),
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
        drawer = layer("ean", sheet(label("评论 3", "d4v", "[60,964][300,1020]")))
        feed = layer("nsv", label("推荐流文案", "o45", "[98,2600][1384,2700]"))
        for layers in ((feed, drawer), (drawer, feed)):
            with self.subTest(order=[item[0]["resource-id"] for item in layers]):
                texts = [item["text"] for item in text_lines(tree(screen(*layers)), 1440, 3116)]
                self.assertEqual(texts, ["评论 3"])

    def test_profile_sheet_covers_drawer_and_feed(self):
        profile = box("", "[0,0][1440,3116]", sheet(label("浙江 宁波 男", "ov9", "[388,1050][680,1210]")))
        xml = tree(screen(layer("ean", sheet(label("评论 3", "d4v", "[60,964][300,1020]"))), profile,
                          layer("nsv", label("推荐流文案", "o45", "[98,2600][1384,2700]"))))
        self.assertEqual([item["text"] for item in text_lines(xml, 1440, 3116)], ["浙江 宁波 男"])

    def test_sheet_beside_the_video_pager_hides_the_pager(self):
        content = box("content", "[0,0][1440,3116]",
                      box("", "[0,0][1440,3116]", sheet(label("广东 韶关 男", "ov9", "[388,1050][680,1210]"))),
                      layer("jlt", label("热评", "ehh", "[60,2600][200,2680]")))
        texts = [item["text"] for item in text_lines(tree(layer("nsv", content)), 1440, 3116)]
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
        xml = tree(screen(
            label("狼王", "kbq", "[308,321][602,498]"),
            box("lz3", "[308,736][523,915]"),
            box("kr", "[1200,736][1380,810]"),
            box("mld", "[800,1189][1400,1307]", **{"content-desc": "私信按钮"}),
        ))
        items = text_lines(xml, 1620, 3612, containers=True)
        holes = [item for item in items if item.get("container")]
        self.assertEqual(holes, [])
        self.assertEqual(sorted(item["text"] for item in page_lines(items)), ["回复", "广告", "狼王", "私信"])
        # Fallback: mld without content-desc still exposes message-button container for OCR.
        fallback_items = text_lines(tree(screen(label("狼王", "kbq", "[308,321][602,498]"),
                                                box("mld", "[800,1189][1400,1307]"))), 1620, 3612, containers=True)
        self.assertEqual([h["container"] for h in fallback_items if h.get("container")], ["message-button"])

    def test_comment_body_with_c1_child_uses_pure_controls_without_ocr(self):
        from comments import parse_comment_drawer
        xml = tree(screen(
            layer("ean", sheet(
                label("评论 12", "d4v", "[151,656][341,711]"),
                label("楠霖27", "kbf", "[151,1277][271,1392]"),
                label("", "ehq", "[238,1275][357,1394]", **{"content-desc": "视频号logo", "class": "android.widget.ImageView"}),
                label("福建", "hgh", "[335,1309][411,1360]"),
                label("11天前", "c85", "[427,1309][863,1360]"),
                box("c6d", "[43,1371][1037,1478]",
                    label("从灶台来看父亲不爱干活[Facepalm]", "c1_", "[151,1371][1037,1478]")),
                label("6754", "aa0", "[885,1310][976,1361]"),
                label("AI吕哥说#\xa0发表评论:", "c6v", "[151,2273][838,2391]", **{"class": "android.widget.EditText"}),
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


if __name__ == "__main__":
    unittest.main()
