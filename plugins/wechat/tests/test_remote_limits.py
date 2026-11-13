import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "wechat_channels/collector"))

from links import require_private_message_available


class RemoteLimitTests(unittest.TestCase):
    def test_actual_phone_notice_stops_private_message_delivery(self):
        path = Path(__file__).resolve().parents[3] / "data/laike-service-live/huawei-dm-evidence.json"
        if not path.exists():
            self.skipTest("需要已保存的真机私信限制记录")
        lines = json.loads(path.read_text(encoding="utf-8"))["lines"]
        with self.assertRaisesRegex(RuntimeError, "今天频繁发送私信"):
            require_private_message_available(lines)
