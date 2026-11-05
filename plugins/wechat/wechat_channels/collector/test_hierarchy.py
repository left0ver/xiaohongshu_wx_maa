"""Semantic parsing, lazy fallbacks and stale-control rejection without a device."""

import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent))
from hierarchy import HierarchyUnavailable, choose_snapshot, complete_for, refresh_target, text_lines


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


class HierarchyTests(unittest.TestCase):
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
