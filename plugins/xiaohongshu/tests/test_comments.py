import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "xiaohongshu" / "agent"))
from comments import (Comment, comment_count, comment_end, comment_section, commenter_profile,
                      empty_comments, folded_comments_control, inspect_comments, link_comments, reply_controls,
                      selected_comment, visible_comments)
from ui import Page, PageMismatch, bounds

FIXTURES = Path(__file__).with_name("fixtures")


def comment_page(xml=None):
    page = Page(xml or (FIXTURES / "comments.xml").read_text())
    _, listing = comment_section(page)
    for row in list(listing):
        if any(n.get("content-desc") == "已到底" for n in row.iter("node")):
            listing.remove(row)
    return page


def profile_page():
    return Page((FIXTURES / "commenter.xml").read_text())


def with_end(page):
    _, listing = comment_section(page)
    row = ET.SubElement(listing, "node", {"class": "android.widget.FrameLayout", "bounds": "[0,1900][1080,2200]"})
    marker = ET.SubElement(row, "node", {"class": "android.widget.FrameLayout", "content-desc": "已到底",
                                        "bounds": "[0,1900][1080,2200]"})
    ET.SubElement(marker, "node", {"text": "- 到底了 -"})
    return page


def driver(pages=None, returned=None):
    pages = pages or [comment_page()]
    state = {"index": 0, "profile": None}
    device = Mock()

    def page():
        return state["profile"] if state["profile"] is not None else pages[state["index"]]

    def command(*args):
        if args[:2] == ("input", "swipe"):
            state["index"] = min(state["index"] + 1, len(pages) - 1)
        return "resumed\n"

    def click(selector):
        node = selector(page())
        if node.get("content-desc", "").startswith("评论 "):
            pages[state["index"]] = comment_page()
            return
        name = node.get("text")
        state["profile"] = Page((FIXTURES / "commenter.xml").read_text().replace("评论者甲", name))

    def back():
        state["profile"] = None
        if returned is not None:
            pages[state["index"]] = returned

    def wait(parse, **kwargs):
        for attempt in range(3):
            try:
                return parse(page())
            except PageMismatch:
                if attempt == 2:
                    raise

    device.page.side_effect = page
    device.command.side_effect = command
    device.wait.side_effect = wait
    device.click.side_effect = click
    device.back.side_effect = back
    return device


class CommentTests(unittest.TestCase):
    def test_real_comment_structure_and_profile(self):
        author, comments = visible_comments(comment_page())
        self.assertEqual(author, "样本作者")
        self.assertEqual(len(comments), 2)
        self.assertEqual(comments[0].payload, dict(nickname="评论者甲", content="怎么联系", commentTime="6小时前", ipLocation="山西"))
        profile = commenter_profile(profile_page(), comments[0].payload["nickname"])
        self.assertEqual(profile["douyinId"], "1234567890")
        self.assertEqual(profile["followersText"], "2569")
        with self.assertRaises(PageMismatch):
            commenter_profile(profile_page(), "其他人")

    def test_translation_action_does_not_hide_comment_or_pollute_metadata(self):
        page = comment_page()
        page.unique(text="6小时前 山西 回复").set("text", "6小时前 山西 回复 翻译")
        comment = visible_comments(page)[1][0]
        self.assertEqual(comment.payload["commentTime"], "6小时前")
        self.assertEqual(comment.payload["ipLocation"], "山西")

    def test_comment_selection_tracks_content_not_repeated_nickname(self):
        page = comment_page()
        selected = visible_comments(page)[1][0]
        for node in page.nodes:
            if node.get("text") == "评论者乙":
                node.set("text", "评论者甲")
        self.assertEqual(selected_comment(page, selected.key).payload["content"], "怎么联系")
        changed = Page((FIXTURES / "comments.xml").read_text().replace("怎么联系 ", "其他评论 "))
        with self.assertRaises(PageMismatch):
            selected_comment(changed, selected.key)

    def test_comment_header_can_scroll_out_of_view(self):
        page = comment_page()
        expected = [c.key for c in visible_comments(page)[1]]
        page.unique(text="共 2 条评论").set("text", "")
        self.assertEqual([c.key for c in visible_comments(page)[1]], expected)

    def test_visible_header_below_note_still_opens_comments(self):
        page = comment_page()
        for node in page.nodes:
            if node.get("text", "").endswith("回复"):
                node.set("text", "")
        picture = ET.Element("node", {"content-desc": "图片,第1张,共1张", "bounds": "[0,252][1080,500]"})
        page.nodes.append(picture)
        device = driver([page])
        with tempfile.TemporaryDirectory() as directory:
            report = inspect_comments(device, directory, max_pages=1, max_profiles=0)
        self.assertEqual(len(report["comments"]), 2)
        self.assertEqual(device.click.call_count, 1)

    def test_report_only_after_return_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            report = inspect_comments(driver(), directory, max_pages=1)
            self.assertEqual(len(report["commenterProfiles"]), 2)
            self.assertEqual([p["commentKey"] for p in report["commenterProfiles"]],
                             [c["key"] for c in report["comments"]])
            self.assertEqual(report["endReason"], "page_limit")
            self.assertFalse(report["complete"])
            self.assertFalse(report["uploaded"])
            paths = list(Path(directory).glob("*.json"))
            self.assertEqual(len(paths), 1)
            self.assertEqual(json.loads(paths[0].read_text()), report)
        with tempfile.TemporaryDirectory() as directory:
            wrong = Page((FIXTURES / "comments.xml").read_text().replace("样本作者", "其他作品作者"))
            with self.assertRaises(PageMismatch):
                inspect_comments(driver(returned=wrong), directory, max_pages=1)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_scroll_deduplicates_overlap_and_stops_without_claiming_exhaustion(self):
        following = comment_page((FIXTURES / "comments.xml").read_text().replace("共 2 条评论", "")
                         .replace("怎么联系 ", "新增评论 "))
        device = driver([comment_page(), following])
        with tempfile.TemporaryDirectory() as directory:
            report = inspect_comments(device, directory, max_profiles=1)
        self.assertEqual(len(report["comments"]), 3)
        self.assertEqual(len(report["commenterProfiles"]), 1)
        self.assertEqual(report["pagesRead"], 2)
        self.assertEqual(report["endReason"], "no_progress")
        self.assertFalse(report["complete"])
        self.assertEqual(device.click.call_count, 1)

    def test_profile_failure_or_stop_never_commits_success(self):
        for error in (PageMismatch("Profile unavailable"), InterruptedError("Task stopped")):
            device = driver()
            device.back.side_effect = error
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(type(error)):
                    inspect_comments(device, directory)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_scrolling_without_overlap_never_joins_different_work_comments(self):
        following = comment_page((FIXTURES / "comments.xml").read_text().replace("评论者甲", "其他甲")
                         .replace("评论者乙", "其他乙"))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(PageMismatch, "Lost comment overlap"):
                inspect_comments(driver([comment_page(), following]), directory, max_profiles=0)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_invalid_limits_never_touch_device(self):
        for options in ({"max_pages": 0}, {"max_pages": True}, {"max_profiles": -1}):
            device = Mock()
            with self.assertRaises(ValueError):
                inspect_comments(device, "unused", **options)
            self.assertEqual(device.mock_calls, [])

    def test_replies_keep_their_parent_across_overlapping_pages(self):
        first, second = visible_comments(comment_page())[1]
        reply = Comment(dict(nickname="回复者", content="回复正文", commentTime="昨天", ipLocation=""),
                        first.name_node, first.box, True)
        linked = link_comments([first, reply], {})
        self.assertEqual(linked[reply.key]["parentCommentKey"], first.key)
        following = link_comments([reply, second], linked)
        self.assertEqual(following[reply.key], linked[reply.key])
        other_thread = link_comments([second, reply], {})
        self.assertNotEqual(other_thread[reply.key]["key"], linked[reply.key]["key"])
        with self.assertRaises(PageMismatch):
            link_comments([reply], {})
        with self.assertRaises(PageMismatch):
            link_comments([first, first], {})
        reply.requires_parent_match = True
        with self.assertRaises(PageMismatch):
            link_comments([first, reply], {})
        recovered = link_comments([first, reply], other_thread)
        self.assertEqual(recovered[reply.key]["parentCommentKey"], second.key)

    def test_device_reply_structure_and_expansion_anchor(self):
        page = Page((FIXTURES / "comment-replies.xml").read_text())
        _, comments = visible_comments(page)
        self.assertEqual([c.is_reply for c in comments], [False, True])
        linked = link_comments(comments, {})
        self.assertEqual(linked[comments[1].key]["parentCommentKey"], comments[0].key)
        controls = reply_controls(page, comments)
        self.assertEqual(len(controls), 1)
        self.assertEqual(controls[0][0], comments[1].key)

    def test_no_actions_when_resume_fails(self):
        driver = Mock()
        driver.command.return_value = ""
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PageMismatch):
                inspect_comments(driver, directory)
        driver.click.assert_not_called()

    def test_reply_target_name_preserves_raw_content_and_never_invents_user_ids(self):
        root = visible_comments(comment_page())[1][0]
        for body, expected in (("回复\u00a0\u00a0样本用户：好的", "样本用户"),
                               ("回复  有 空格 的昵称：好的", "有 空格 的昵称"),
                               ("回复  名称：后缀：内容", None), ("回复  ：好的", None),
                               ("@样本用户 好的", None), ("普通回复", None)):
            reply = Comment({**root.payload, "content": body}, root.name_node, root.box, True)
            record = link_comments([root, reply], {})[reply.key]
            self.assertEqual(record["replyToNickname"], expected)
            self.assertEqual(record["content"], body)
            self.assertNotIn("replyToUserId", record)
        root.payload["content"] = "回复  样本用户：这是一级评论正文"
        self.assertIsNone(link_comments([root], {})[root.key]["replyToNickname"])

    def test_end_requires_real_footer_not_comment_text_or_clipped_marker(self):
        page = comment_page()
        self.assertFalse(comment_end(page))
        page.unique(text="怎么联系 ").set("text", "- 到底了 -")
        self.assertFalse(comment_end(page))
        with_end(page)
        self.assertTrue(comment_end(page))
        _, listing = comment_section(page)
        listing[-1][0].set("bounds", "[0,1500][1080,2500]")
        self.assertFalse(comment_end(page))

    def test_end_only_means_complete_when_all_displayed_comments_are_accounted_for(self):
        for total, expected in ((2, True), (3, False)):
            page = with_end(comment_page())
            for n in page.nodes:
                if n.get("content-desc", "").startswith("评论 "):
                    n.set("content-desc", f"评论 {total}")
            with tempfile.TemporaryDirectory() as directory:
                report = inspect_comments(driver([page]), directory, max_pages=1, max_profiles=0)
            self.assertEqual(report["endReason"], "end_of_list")
            self.assertEqual(report["expectedCommentCount"], total)
            self.assertEqual(report["complete"], expected)

    def test_folded_comments_take_precedence_over_bottom_marker(self):
        page = with_end(comment_page())
        _, listing = comment_section(page)
        row = ET.Element("node", {"class": "android.view.ViewGroup", "clickable": "true",
                                  "bounds": "[0,1350][1080,1450]"})
        ET.SubElement(row, "node", {"text": "查看被折叠的评论"})
        ET.SubElement(row, "node", {"text": "被折叠评论包含疑似广告、不友好或无意义内容"})
        listing.insert(len(listing) - 1, row)
        self.assertIs(folded_comments_control(page), row)
        self.assertFalse(comment_end(page))
        row.set("clickable", "false")
        self.assertIsNone(folded_comments_control(page))

    def test_ambiguous_counts_do_not_claim_complete(self):
        page = with_end(comment_page())
        duplicate = ET.Element("node", {"content-desc": "评论 7"})
        page.nodes.append(duplicate)
        self.assertIsNone(comment_count(page))

    def test_folded_comment_loading_is_verified_before_marking_complete(self):
        for loads in (True, False):
            page = with_end(comment_page())
            _, listing = comment_section(page)
            for node in page.nodes:
                if node.get("content-desc", "").startswith("评论 "):
                    node.set("content-desc", "评论 3")
            row = ET.Element("node", {"class": "android.view.ViewGroup", "clickable": "true",
                                      "bounds": "[0,1350][1080,1450]"})
            label = ET.SubElement(row, "node", {"text": "查看被折叠的评论"})
            ET.SubElement(row, "node", {"text": "被折叠评论包含疑似广告、不友好或无意义内容"})
            listing.insert(len(listing) - 1, row)
            page.nodes.extend(row.iter("node"))
            first = visible_comments(page)[1][0]
            extra = copy.deepcopy(next(child for child in listing if first.name_node in list(child.iter("node"))))
            for node in extra.iter("node"):
                if node.get("text") == "怎么联系 ":
                    node.set("text", "新增折叠评论")
                if node.get("bounds"):
                    left, top, right, bottom = bounds(node)
                    node.set("bounds", f"[{left},{top + 850}][{right},{bottom + 850}]")

            def click(selector):
                self.assertIs(selector(page), row)
                if loads:
                    label.set("text", "以下是被折叠的评论")
                    row.set("clickable", "false")
                    listing.insert(len(listing) - 1, extra)
                    page.nodes.extend(extra.iter("node"))

            device = driver([page])
            device.click.side_effect = click
            with tempfile.TemporaryDirectory() as directory:
                if not loads:
                    with self.assertRaises(PageMismatch):
                        inspect_comments(device, directory, max_profiles=0)
                    self.assertEqual(list(Path(directory).iterdir()), [])
                else:
                    report = inspect_comments(device, directory, max_profiles=0)
                    self.assertEqual(report["foldedCommentExpansions"], 1)
                    self.assertEqual(len(report["comments"]), 3)
                    self.assertTrue(report["complete"])

    def test_zero_count_needs_explicit_empty_state_and_never_opens_composer(self):
        page = comment_page()
        _, listing = comment_section(page)
        listing[:] = []
        for n in page.nodes:
            if n.get("content-desc", "").startswith("评论 "):
                n.set("content-desc", "评论 0")
        self.assertFalse(empty_comments(page))
        with self.assertRaises(PageMismatch):
            visible_comments(page)
        row = ET.SubElement(listing, "node", {"class": "android.widget.LinearLayout"})
        picture = ET.SubElement(row, "node", {"class": "android.widget.LinearLayout"})
        ET.SubElement(picture, "node", {"class": "android.widget.ImageView"})
        ET.SubElement(picture, "node", {"text": "还没有评论哦", "bounds": "[0,1900][1080,1950]"})
        ET.SubElement(row, "node", {"text": "抢首评", "bounds": "[450,2000][630,2060]"})
        page.nodes.extend(row.iter("node"))
        self.assertTrue(empty_comments(page))
        device = driver([page])
        with tempfile.TemporaryDirectory() as directory:
            report = inspect_comments(device, directory)
        self.assertEqual(report["endReason"], "empty_comments")
        self.assertEqual(report["comments"], [])
        self.assertTrue(report["complete"])
        device.click.assert_not_called()

    def test_empty_state_with_nonzero_count_is_not_complete(self):
        page = comment_page()
        _, listing = comment_section(page)
        row = ET.SubElement(listing, "node")
        ET.SubElement(row, "node", {"text": "还没有评论哦"})
        ET.SubElement(row, "node", {"text": "抢首评"})
        self.assertFalse(empty_comments(page))


if __name__ == "__main__":
    unittest.main()
