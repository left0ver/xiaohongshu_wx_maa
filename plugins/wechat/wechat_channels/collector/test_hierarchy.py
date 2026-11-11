"""Semantic parsing, lazy fallbacks and stale-control rejection without a device."""

import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent))
from hierarchy import (HierarchyUnavailable, choose_snapshot, complete_for, correct_ocr, field_evidence,
                       refresh_target, text_lines)
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

    def test_drawer_is_never_read_from_control_bounds_alone(self):
        # TextView bounds include padding and covered feed controls stay in the
        # tree, so even a parseable drawer is read with OCR layout.
        known = [control(item["text"], *item["box"]) for item in DETAILS]
        ocr = Mock(return_value=DETAILS)
        lines, source = choose_snapshot("video-details", 1558, AUTHOR, lambda: known, Mock(), ocr)
        ocr.assert_called_once()
        self.assertEqual((lines, source), (DETAILS, "ocr"))

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
        self.assertEqual(lines[0]["box"], [400, 1400, 100, 50])

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


if __name__ == "__main__":
    unittest.main()
