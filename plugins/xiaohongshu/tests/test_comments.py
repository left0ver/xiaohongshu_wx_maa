import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "xiaohongshu" / "agent"))
from comments import commenter_profile, inspect_comments, selected_comment, visible_comments
from ui import Page, PageMismatch

FIXTURES = Path(__file__).with_name("fixtures")


def comment_page():
    return Page((FIXTURES / "comments.xml").read_text())


def profile_page():
    return Page((FIXTURES / "commenter.xml").read_text())


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

    def driver(self, returned=None):
        page = comment_page()
        driver = Mock()
        driver.command.return_value = "resumed\n"
        driver.wait.side_effect = [page, visible_comments(page), commenter_profile(profile_page(), "评论者甲"),
                                   returned or visible_comments(page)]
        return driver

    def test_report_only_after_return_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            report = inspect_comments(self.driver(), directory)
            self.assertEqual(report["selectedCommentKey"], report["comments"][0]["key"])
            self.assertFalse(report["uploaded"])
            paths = list(Path(directory).glob("*.json"))
            self.assertEqual(len(paths), 1)
            self.assertEqual(json.loads(paths[0].read_text()), report)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PageMismatch):
                inspect_comments(self.driver(("其他作品作者", visible_comments(comment_page())[1])), directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_no_actions_when_resume_fails(self):
        driver = Mock()
        driver.command.return_value = ""
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PageMismatch):
                inspect_comments(driver, directory)
        driver.click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
