"""Field association and boundaries across layouts; no rendered UI or device."""

import unittest

from collection import parse_author, parse_basic_info, parse_video
from layout import basic_info_ready, work_region
from test_collection import AUTHOR, DETAILS, INFO, PROFILE, line


class LayoutTests(unittest.TestCase):
    def test_original_audio_is_a_work_boundary(self):
        for label in ("原声", "」原声", "♪ 原声", "原声 · 示例歌曲", "相关音乐·示例歌曲"):
            with self.subTest(label=label):
                data = [*DETAILS[:4], line(label, 102, 691), *DETAILS[6:]]
                self.assertEqual(parse_video(data, AUTHOR)["description"], "完整的作品文案#音乐#我们俩")

    def test_words_and_dates_inside_prose_do_not_end_description(self):
        data = [*DETAILS[:2], line("音乐让人开心，原声也好听", 99, 604),
                line("我在8月1日写下这段话", 99, 646),
                line("#相关音乐 #原声", 99, 688), line("#原声", 99, 728), line("相关音乐·歌曲", 99, 788)]
        self.assertEqual(parse_video(data, AUTHOR)["description"],
                         "音乐让人开心，原声也好听我在8月1日写下这段话#相关音乐 #原声#原声")

    def test_separate_nickname_tag_region_and_date_boxes(self):
        data = [DETAILS[0], line(AUTHOR, 99, 560, 150), line("作者", 260, 561, 55),
                line("福建", 320, 560, 55), line("8月1日", 390, 562, 90), *DETAILS[2:]]
        value = parse_video(data, AUTHOR)
        self.assertEqual(value["publishLocation"], "福建")
        self.assertEqual(value["publishTimeText"], "8月1日")
        self.assertEqual(value["authorNickname"], AUTHOR)

    def test_metadata_below_author_row_and_split_across_rows(self):
        for metadata in ([line("福建8月1日", 99, 620)],
                         [line("福建", 99, 620), line("8月1日", 99, 660)]):
            data = [DETAILS[0], line(AUTHOR + "作者", 99, 570), *metadata,
                    line("正文", 99, 710), line("相关音乐·歌曲", 99, 760)]
            result = parse_video(data, AUTHOR)
            self.assertEqual(result["publishLocation"], "福建")
            self.assertEqual(result["publishTimeText"], "8月1日")
            self.assertEqual(result["description"], "正文")

    def test_nickname_containing_author_word_does_not_replace_separate_tag(self):
        data = [DETAILS[0], line("示例作者", 99, 563), line("作者浙江6月27日", 99, 605),
                line("正文", 99, 652), line("原声", 99, 701)]
        result = parse_video(data, "首次昵称")
        self.assertEqual(result["authorNickname"], "首次昵称")
        self.assertEqual(result["publishLocation"], "浙江")
        self.assertEqual(result["publishTimeText"], "6月27日")
        self.assertEqual(result["description"], "正文")

    def test_unknown_header_badge_is_not_a_location(self):
        data = list(DETAILS)
        data[1] = line(AUTHOR + "作者徽章8月1日", 99, 568)
        self.assertIsNone(parse_video(data, AUTHOR)["publishLocation"])

    def test_time_can_precede_location_in_header_or_adjacent_rows(self):
        for header, extra in ((AUTHOR + "作者8月1日福建", []),
                              (AUTHOR + "作者8月1日", [line("福建", 99, 620)]),
                              (AUTHOR + "作者", [line("8月1日", 99, 620), line("福建", 99, 660)])):
            with self.subTest(header=header, extra=extra):
                result = parse_video([DETAILS[0], line(header, 99, 570), *extra,
                                      line("正文", 99, 710), line("原声", 99, 760)], AUTHOR)
                self.assertEqual(result["publishTimeText"], "8月1日")
                self.assertEqual(result["publishLocation"], "福建")
                self.assertEqual(result["description"], "正文")

    def test_comment_structure_can_bound_work_without_music(self):
        data = [*DETAILS[:4], line("评论者四川8月1日", 99, 790), line("82", 630, 790, 40),
                line("这是一条评论", 99, 832), line(AUTHOR + "作者福建8月1日", 99, 930),
                line("作者的回复不能混入文案", 99, 970)]
        self.assertEqual(parse_video(data, AUTHOR)["description"], "完整的作品文案#音乐#我们俩")

    def test_prose_ending_with_location_and_date_is_not_a_comment(self):
        data = [*DETAILS[:4], line("我在福建8月1日", 99, 800), line("记住这一天", 99, 842),
                line("相关音乐·歌曲", 99, 900)]
        self.assertIn("我在福建8月1日记住这一天", parse_video(data, AUTHOR)["description"])

    def test_a_date_only_row_is_not_a_comment_boundary(self):
        data = [*DETAILS[:4], line("2026年8月1日", 99, 800), line("我会记住这一天", 99, 842)]
        with self.assertRaisesRegex(ValueError, "boundary"):
            parse_video(data, AUTHOR)

    def test_author_reply_cannot_replace_missing_original_header(self):
        data = [DETAILS[0], line("评论者四川8月1日", 99, 870),
                line(AUTHOR + "作者福建8月1日", 99, 940), line("原声", 99, 1020)]
        with self.assertRaises(ValueError):
            parse_video(data, AUTHOR)

    def test_comment_expand_is_not_a_work_expand_control(self):
        data = [*DETAILS, line("展开", 99, 1000)]
        self.assertIsNone(work_region(data)["expand"])
        self.assertEqual(parse_video(data, AUTHOR)["description"], "完整的作品文案#音乐#我们俩")

    def test_work_expand_is_requested_before_accepting_description(self):
        data = [*DETAILS[:4], line("展开", 99, 684), line("原声", 99, 735)]
        self.assertEqual(work_region(data)["expand"]["text"], "展开")
        with self.assertRaisesRegex(ValueError, "expansion"):
            parse_video(data, AUTHOR)

    def test_font_and_coordinate_scaling_preserves_fields(self):
        expected = parse_video(DETAILS, AUTHOR)
        for scale in (0.7, 1.4, 2):
            data = [{**item, "box": [round(value * scale) for value in item["box"]]} for item in DETAILS]
            self.assertEqual(parse_video(data, AUTHOR), expected)

    def test_basic_info_values_can_be_inline_below_or_wrapped(self):
        data = [line("基础信息", 40, 480), line("IP归属地：福建", 40, 550),
                line("资料所在地", 40, 610), line("福建", 40, 648), line("厦门", 40, 686),
                line("视频号 ID", 40, 750), line("sphExample", 40, 788), line("123", 40, 826)]
        self.assertTrue(basic_info_ready(data))
        value = parse_basic_info(data)
        self.assertEqual(value["authorIpLocation"], "福建")
        self.assertEqual(value["authorProfileLocation"], "福建厦门")
        self.assertEqual(value["authorDouyinId"], "sphExample123")

    def test_empty_value_does_not_consume_next_label(self):
        data = [item for item in INFO if item["text"] != "福建"]
        result = parse_basic_info(data)
        self.assertIsNone(result["authorIpLocation"])
        self.assertEqual(result["authorProfileLocation"], "福建厦门")

    def test_duplicate_id_labels_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            parse_basic_info([*INFO, line("视频号ID another123", 40, 800)])

    def test_certification_section_does_not_extend_id_and_is_collected(self):
        data = [*INFO, line("认证信息", 40, 795), line("认证信息", 40, 860),
                line("音乐达人√", 250, 860), line("主体类型", 40, 910), line("个人", 250, 910)]
        result = parse_basic_info(data)
        self.assertEqual(result["authorDouyinId"], "sphExample123")
        self.assertEqual(result["authorCertification"], "音乐达人")

    def test_profile_bio_is_not_invented_as_location(self):
        data = [PROFILE[0], line("喜欢分享生活", 192, 552), *PROFILE[2:]]
        result = parse_author(data, AUTHOR)
        self.assertIsNone(result["authorProfileLocation"])
        self.assertIn("喜欢分享生活", result["authorBio"])

    def test_collapsed_biography_is_missing_instead_of_certification_and_location(self):
        data = [PROFILE[0], line("音乐达人", 192, 550), line("河南郑州男已实名", 192, 590),
                line("简介...展开", 35, 644), *PROFILE[-2:]]
        result = parse_author(data, AUTHOR)
        self.assertEqual(result["authorCertification"], "音乐达人")
        self.assertIsNone(result["authorBio"])
        self.assertIn("authorBio", result["missingFields"])
        expanded = [line("完整的作者简介", 35, 644) if item["text"] == "简介...展开" else item for item in data]
        self.assertEqual(parse_author(expanded, AUTHOR)["authorBio"], "完整的作者简介")

    def test_vertical_metadata_separator_is_not_part_of_location(self):
        data = [DETAILS[0], line(AUTHOR + "作者四川I8月1日", 99, 568), *DETAILS[2:]]
        self.assertEqual(parse_video(data, AUTHOR)["publishLocation"], "四川")


if __name__ == "__main__":
    unittest.main()
