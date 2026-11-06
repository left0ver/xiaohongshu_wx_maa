"""Semantic parsing, lazy fallbacks and stale-control rejection without a device."""

import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent))
from hierarchy import HierarchyUnavailable, choose_snapshot, complete_for, field_evidence, refresh_target, text_lines
from collection import parse_basic_info, parse_video
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


class HierarchyTests(unittest.TestCase):
    def test_visible_child_survives_an_invisible_parent(self):
        xml = '<hierarchy><node package="com.tencent.mm" visible-to-user="false" text="hidden" bounds="[0,0][1,1]">' \
              '<node package="com.tencent.mm" visible-to-user="true" text="更多信息" bounds="[0,100][200,200]"/>' \
              '</node></hierarchy>'
        self.assertEqual([item["text"] for item in text_lines(xml, 720, 1558)], ["更多信息"])

    def test_partial_work_reads_only_body_and_retains_semantic_metadata(self):
        known = [{**item, "source": "hierarchy"} for item in DETAILS[:2]]
        body = [{**item, "source": "ocr"} for item in DETAILS[2:]]
        full, regional = Mock(), Mock(return_value=body)
        lines, source = choose_snapshot("video-details", 1558, AUTHOR, lambda: known, Mock(), full,
                                        regional_ocr=regional, refresh=lambda: known)
        self.assertEqual(source, "mixed")
        self.assertGreaterEqual(regional.call_args.args[0][1], 596)
        self.assertEqual(parse_video(lines, AUTHOR), parse_video(DETAILS, AUTHOR))
        self.assertEqual(field_evidence(lines, "video-details", AUTHOR)["publishLocation"]["sources"], ["hierarchy"])
        self.assertEqual(field_evidence(lines, "video-details", AUTHOR)["description"]["sources"], ["ocr"])
        full.assert_not_called()

    def test_replaced_body_does_not_keep_duplicate_or_stale_fragments(self):
        known = [*DETAILS[:2], line("旧的部分文案", 99, 604)]
        lines, source = choose_snapshot("video-details", 1558, AUTHOR, lambda: known, Mock(), Mock(),
                                        regional_ocr=lambda _: DETAILS[2:], refresh=lambda: known)
        self.assertEqual(source, "mixed")
        self.assertNotIn("旧的部分文案", parse_video(lines, AUTHOR)["description"])

    def test_changed_anchor_discards_mixed_page_and_reads_fresh_frame(self):
        known = DETAILS[:2]
        changed = [DETAILS[0], line("另一个作者作者福建8月1日", 99, 568)]
        full = Mock(return_value=changed)
        lines, source = choose_snapshot("video-details", 1558, AUTHOR, lambda: known, Mock(), full,
                                        regional_ocr=lambda _: DETAILS[2:], refresh=lambda: changed)
        self.assertEqual((lines, source), (changed, "ocr"))
        full.assert_called_once()

    def test_missing_id_is_read_without_discarding_known_location(self):
        known = INFO[:-1]
        full = Mock()
        lines, source = choose_snapshot("author-basic-info", 1558, None, lambda: known, Mock(), full,
                                        regional_ocr=lambda _: INFO[-2:], refresh=lambda: known)
        self.assertEqual(source, "mixed")
        self.assertEqual(parse_basic_info(lines), parse_basic_info(INFO))
        full.assert_not_called()

    def test_missing_metadata_reads_header_without_repeating_description(self):
        known = [DETAILS[0], line(AUTHOR + "作者", 99, 568), *DETAILS[2:]]
        regional, full = Mock(return_value=[DETAILS[1]]), Mock()
        lines, source = choose_snapshot("video-details", 1558, AUTHOR, lambda: known, Mock(), full,
                                        regional_ocr=regional, refresh=lambda: known)
        self.assertEqual(source, "mixed")
        x, y, width, height = regional.call_args.args[0]
        self.assertLessEqual(y + height, DETAILS[2]["box"][1])
        self.assertEqual(parse_video(lines, AUTHOR), parse_video(DETAILS, AUTHOR))
        full.assert_not_called()

    def test_profile_missing_bio_preserves_nickname_and_controls(self):
        known = [PROFILE[0], *PROFILE[-2:]]
        full = Mock()
        lines, source = choose_snapshot("author-profile", 1558, AUTHOR, lambda: known, Mock(), full,
                                        regional_ocr=lambda _: PROFILE[1:-2], refresh=lambda: known)
        self.assertEqual(source, "mixed")
        self.assertEqual(field_evidence(lines, "author-profile", AUTHOR)["authorBio"]["value"], "作者简介")
        full.assert_not_called()

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
