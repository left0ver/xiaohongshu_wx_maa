import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "wechat_channels/collector"))

from hierarchy import channels_intro_acknowledgement, text_lines


class RecordedChannelsIntroTests(unittest.TestCase):
    def test_actual_intro_selects_the_acknowledgement(self):
        path = Path(__file__).resolve().parents[1] / "wechat_channels/collector/testdata/channels-intro-huawei.json"
        lines = json.loads(path.read_text())["lines"]
        control = channels_intro_acknowledgement(lines)
        self.assertEqual(control["text"], "我知道了")
        self.assertEqual(control["selector"]["class"], "android.widget.Button")
        self.assertEqual(control["selector"]["resource"], "com.tencent.mm:id/v6_")
        self.assertIn(control, lines)

    def test_actual_feed_has_no_intro_acknowledgement(self):
        path = Path(__file__).resolve().parents[1] / "wechat_channels/collector/testdata/recommend-selected-vivo.xml"
        lines = text_lines(path.read_text(), 1080, 2408)
        self.assertIsNone(channels_intro_acknowledgement(lines))
