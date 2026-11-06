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
                      empty_comments, folded_comments_control, inspect_comments, is_video_comment_drawer,
                      link_comments, reply_controls, resolve_reply_targets, selected_comment,
                      video_comment_drawer, visible_comments)
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

    def test_commenter_profile_works_count_and_missing_fields_never_guessed(self):
        base = profile_page()
        profile = commenter_profile(base, "评论者甲")
        self.assertEqual(profile["douyinId"], "1234567890")
        self.assertEqual(profile["followersText"], "2569")
        self.assertIsNone(profile["worksCountText"])

        with_count = profile_page()
        with_count.unique(description="已选定笔记").set("content-desc", "已选定笔记 18")
        self.assertEqual(commenter_profile(with_count, "评论者甲")["worksCountText"], "18")

        empty_works = profile_page()
        ET.SubElement(empty_works.root, "node", {
            "package": "com.xingin.xhs",
            "class": "android.widget.TextView",
            "text": "还没有发布过笔记",
            "bounds": "[300,1200][780,1260]",
        })
        empty_works = Page(ET.tostring(empty_works.root, encoding="unicode"))
        self.assertEqual(commenter_profile(empty_works, "评论者甲")["worksCountText"], "0")

        missing_followers = Page((FIXTURES / "commenter.xml").read_text().replace('content-desc="2569粉丝"', 'content-desc=""'))
        parsed_missing = commenter_profile(missing_followers, "评论者甲")
        self.assertIsNone(parsed_missing["followersText"])
        self.assertIsNone(parsed_missing["worksCountText"])

    def test_secondary_reply_target_unique_identity_and_ambiguous_guard(self):
        first, second = visible_comments(comment_page())[1]
        reply_first = Comment(
            dict(nickname="目标用户", content="一层回复", commentTime="5小时前", ipLocation="广东"),
            first.name_node, first.box, True,
        )
        reply_second = Comment(
            dict(nickname="评论者甲", content="回复  目标用户：二层回复", commentTime="4小时前", ipLocation="山西"),
            first.name_node, first.box, True,
        )
        linked = link_comments([first, reply_first, reply_second, second], {})
        target_record = linked[reply_first.key]
        second_record = linked[reply_second.key]
        self.assertEqual(second_record["replyToNickname"], "目标用户")
        self.assertEqual(second_record["replyToCommentKey"], target_record["key"])
        self.assertIsNone(second_record["replyToDouyinId"])

        resolve_reply_targets(linked, [
            dict(commentKey=target_record["key"], nickname="目标用户", douyinId="target_red_99", profileVisited=True),
        ])
        self.assertEqual(second_record["replyToCommentKey"], target_record["key"])
        self.assertEqual(second_record["replyToDouyinId"], "target_red_99")

        # Duplicate nickname in the same thread with different or unverified IDs never guesses.
        dup_target = Comment(
            dict(nickname="目标用户", content="同名另一条回复", commentTime="4小时前", ipLocation="北京"),
            first.name_node, first.box, True,
        )
        ambiguous = link_comments([first, reply_first, dup_target, reply_second], {})
        resolve_reply_targets(ambiguous, [
            dict(commentKey=ambiguous[reply_first.key]["key"], nickname="目标用户", douyinId="id_1", profileVisited=True),
            dict(commentKey=ambiguous[dup_target.key]["key"], nickname="目标用户", douyinId="id_2", profileVisited=True),
        ])
        self.assertIsNone(ambiguous[reply_second.key]["replyToCommentKey"])
        self.assertIsNone(ambiguous[reply_second.key]["replyToDouyinId"])

        # Same user posting twice in the thread with identical verified douyinId resolves user identity safely.
        resolve_reply_targets(ambiguous, [
            dict(commentKey=ambiguous[reply_first.key]["key"], nickname="目标用户", douyinId="same_id", profileVisited=True),
            dict(commentKey=ambiguous[dup_target.key]["key"], nickname="目标用户", douyinId="same_id", profileVisited=True),
        ])
        self.assertIsNone(ambiguous[reply_second.key]["replyToCommentKey"])
        self.assertEqual(ambiguous[reply_second.key]["replyToDouyinId"], "same_id")

    def test_disappeared_comment_and_inaccessible_or_unconfirmed_profile_are_skipped_safely(self):
        base = comment_page()
        state = {"mode": "comments"}
        device = Mock()
        device.has_vision = False

        def current_page():
            if state["mode"] == "bad_profile":
                return Page((FIXTURES / "commenter.xml").read_text().replace("评论者甲", "错误用户"))
            if state["mode"] == "good_profile":
                return Page((FIXTURES / "commenter.xml").read_text().replace("评论者甲", "评论者乙"))
            return base

        def click(selector):
            node = selector(current_page())
            if node.get("text") == "评论者甲":
                state["mode"] = "bad_profile"
            elif node.get("text") == "评论者乙":
                state["mode"] = "good_profile"

        def back():
            state["mode"] = "comments"

        def wait(parse, **kwargs):
            for attempt in range(3):
                try:
                    return parse(current_page())
                except PageMismatch:
                    if attempt == 2:
                        raise

        device.page.side_effect = current_page
        device.command.return_value = "resumed\n"
        device.click.side_effect = click
        device.back.side_effect = back
        device.wait.side_effect = wait

        with tempfile.TemporaryDirectory() as directory:
            report = inspect_comments(device, directory, max_pages=1, max_profiles=2)
        self.assertEqual(len(report["commenterProfiles"]), 1)
        self.assertEqual(report["commenterProfiles"][0]["nickname"], "评论者乙")
        self.assertEqual(len(report["skippedProfiles"]), 1)
        self.assertEqual(report["skippedProfiles"][0]["nickname"], "评论者甲")
        self.assertFalse(report["skippedProfiles"][0]["profileVisited"])
        self.assertEqual(report["skippedProfiles"][0]["reason"], "identity_unconfirmed")

        # Disappeared comment before click is recorded in skippedProfiles and not associated.
        page_with_two = comment_page()
        page_without_first = comment_page()
        _, listing = comment_section(page_without_first)
        for child in list(listing):
            if any(n.get("text") == "评论者甲" for n in child.iter("node")):
                listing.remove(child)
        page_without_first = Page(ET.tostring(page_without_first.root, encoding="unicode"))

        disappear_state = {"page": page_with_two}
        device2 = Mock()
        device2.has_vision = False
        device2.page.side_effect = lambda: disappear_state["page"]
        device2.command.return_value = "resumed\n"

        def click_disappear(selector):
            disappear_state["page"] = page_without_first
            node = selector(disappear_state["page"])
            name = node.get("text")
            disappear_state["page"] = Page((FIXTURES / "commenter.xml").read_text().replace("评论者甲", name))

        def wait2(parse, **kwargs):
            for attempt in range(3):
                try:
                    return parse(disappear_state["page"])
                except PageMismatch:
                    if attempt == 2:
                        raise

        device2.click.side_effect = click_disappear
        device2.back.side_effect = lambda: disappear_state.update(page=page_without_first)
        device2.wait.side_effect = wait2

        with tempfile.TemporaryDirectory() as directory:
            report2 = inspect_comments(device2, directory, max_pages=1, max_profiles=2)
        self.assertEqual(len(report2["skippedProfiles"]), 1)
        self.assertEqual(report2["skippedProfiles"][0]["nickname"], "评论者甲")
        self.assertEqual(report2["skippedProfiles"][0]["reason"], "comment_disappeared")
        self.assertEqual(len(report2["commenterProfiles"]), 1)
        self.assertEqual(report2["commenterProfiles"][0]["nickname"], "评论者乙")

    def test_video_comment_drawer_open_read_profile_and_close_returns_to_video(self):
        def make_video_page(author="视频作者", title="测试视频标题"):
            root = ET.Element("hierarchy")
            ET.SubElement(root, "node", {
                "package": "com.xingin.xhs",
                "resource-id": "com.xingin.xhs:id/matrixNickNameView",
                "class": "android.widget.TextView",
                "text": author,
                "bounds": "[180,1920][360,1990]",
            })
            ET.SubElement(root, "node", {
                "package": "com.xingin.xhs",
                "content-desc": f"作者{author}",
                "bounds": "[30,1890][360,2030]",
            })
            ET.SubElement(root, "node", {
                "package": "com.xingin.xhs",
                "content-desc": "分享",
                "bounds": "[948,126][1032,210]",
            })
            ET.SubElement(root, "node", {
                "package": "com.xingin.xhs",
                "resource-id": "com.xingin.xhs:id/noteContentText",
                "text": title,
                "content-desc": title,
                "bounds": "[48,2046][1032,2112]",
            })
            ET.SubElement(root, "node", {
                "package": "com.xingin.xhs",
                "class": "android.widget.Button",
                "content-desc": "评论 2",
                "bounds": "[867,2276][1080,2408]",
                "clickable": "true",
            })
            return Page(ET.tostring(root, encoding="unicode"))

        def make_drawer_page():
            base = with_end(comment_page())
            for parent in base.root.iter():
                for child in list(parent):
                    if (child.get("resource-id", "").endswith(("/nickNameTV", "/moreOperateIV"))
                            or child.get("content-desc") == "评论框"
                            or child.get("content-desc", "").startswith("评论 ")):
                        parent.remove(child)
            header = ET.SubElement(base.root, "node", {
                "package": "com.xingin.xhs",
                "class": "android.widget.TextView",
                "text": "共 2 条评论",
                "bounds": "[15,420][381,550]",
                "clickable": "true",
            })
            ET.SubElement(base.root, "node", {
                "package": "com.xingin.xhs",
                "class": "android.widget.ImageView",
                "bounds": "[972,450][1032,510]",
                "clickable": "true",
            })
            ET.SubElement(base.root, "node", {
                "package": "com.xingin.xhs",
                "resource-id": "com.xingin.xhs:id/commentLayout",
                "class": "android.view.ViewGroup",
                "bounds": "[0,2264][1080,2408]",
            })
            for node in base.root.iter("node"):
                if node.get("class") == "androidx.recyclerview.widget.RecyclerView":
                    node.set("bounds", "[0,550][1080,2264]")
                elif node is not header and node.get("text") == "共 2 条评论":
                    node.set("text", "")
            return Page(ET.tostring(base.root, encoding="unicode"))

        video_page = make_video_page()
        drawer_page = make_drawer_page()
        self.assertTrue(is_video_comment_drawer(drawer_page))
        self.assertEqual(comment_count(drawer_page), 2)

        state = {"screen": video_page}
        device = Mock()
        device.has_vision = False
        device.page.side_effect = lambda: state["screen"]
        device.command.return_value = "resumed\n"

        def click(selector):
            node = selector(state["screen"])
            if node.get("content-desc") == "评论 2":
                state["screen"] = drawer_page
            elif node.get("class") == "android.widget.ImageView" and bounds(node) == (972, 450, 1032, 510):
                state["screen"] = video_page
            else:
                name = node.get("text")
                state["screen"] = Page((FIXTURES / "commenter.xml").read_text().replace("评论者甲", name))

        def wait_drawer(parse, **kwargs):
            for attempt in range(3):
                try:
                    return parse(state["screen"])
                except PageMismatch:
                    if attempt == 2:
                        raise

        device.click.side_effect = click
        device.back.side_effect = lambda: state.update(screen=drawer_page)
        device.wait.side_effect = wait_drawer

        with tempfile.TemporaryDirectory() as directory:
            report = inspect_comments(device, directory, max_pages=1, max_profiles=1)
        self.assertEqual(report["scope"], "current_video_note_comments")
        self.assertEqual(report["sourceAuthor"], "视频作者")
        self.assertTrue(report["drawerClosed"])
        self.assertTrue(report["returnedToVideo"])
        self.assertTrue(report["complete"])
        self.assertEqual(len(report["comments"]), 2)
        self.assertEqual(len(report["commenterProfiles"]), 1)

        # Returning to a different video after closing the drawer fails closed without writing a report.
        wrong_video = make_video_page(author="其他作者", title="其他视频")
        state["screen"] = video_page

        def click_wrong_return(selector):
            node = selector(state["screen"])
            if node.get("content-desc") == "评论 2":
                state["screen"] = drawer_page
            elif node.get("class") == "android.widget.ImageView":
                state["screen"] = wrong_video

        device.click.side_effect = click_wrong_return
        device.back.side_effect = lambda: None
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PageMismatch):
                inspect_comments(device, directory, max_pages=1, max_profiles=0)
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
