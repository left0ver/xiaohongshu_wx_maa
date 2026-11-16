import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "wechat_channels/collector"))

from hierarchy import complete_for, text_lines
from modes import active_top_tab, find_top_tabs


class RecordedSelectedTabTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "wechat_channels/collector/testdata/recommend-selected-vivo.xml"
        self.lines = text_lines(path.read_text(encoding="utf-8"), 1080, 2408)

    def test_selected_state_is_preserved_from_phone_controls(self):
        recommend = next(line for line in self.lines if line["text"] == "推荐")
        self.assertIs(recommend["selected"], True)

    def test_friends_tab_uses_the_actual_phone_control(self):
        friends = find_top_tabs(self.lines)["friends"]
        self.assertEqual(friends["text"], "朋友赞过")
        self.assertNotIn("inferred", friends)

    def test_active_tab_uses_the_phone_selection(self):
        selected, evidence = active_top_tab(self.lines)
        self.assertEqual(selected, "recommend")
        self.assertIs(evidence["recommend"]["selected"], True)

    def test_tab_checks_accept_the_recorded_controls(self):
        for label in ("tab-check-recommend", "tab-switched-friends", "verify-stage-recommend"):
            with self.subTest(label=label):
                self.assertTrue(complete_for(self.lines, label, 1605))
