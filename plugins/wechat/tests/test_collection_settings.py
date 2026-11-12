import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "wechat_channels/collector"))

from modes import collection_task_config, mode_comment_limits


class CollectionSettingsTests(unittest.TestCase):
    def test_default_collection_visits_commenter_profiles(self):
        settings = collection_task_config({"entry": "recommend", "count": 1})
        self.assertIs(settings["commentOptions"]["visitProfiles"], True)
        self.assertIsNone(mode_comment_limits(settings["commentOptions"])["max_profiles"])

    def test_collection_accepts_bounded_profile_visits(self):
        options = {"maxPages": 1, "maxReplyExpansions": 0, "maxProfiles": 2}
        settings = collection_task_config({"entry": "search", "keyword": "家常菜", "commentOptions": options})
        self.assertEqual(mode_comment_limits(settings["commentOptions"]),
                         {"max_pages": 1, "max_reply_expansions": 0, "max_profiles": 2})
        self.assertIs(settings["commentOptions"]["visitProfiles"], True)
        self.assertNotIn("visitProfiles", options)

    def test_profile_visits_can_be_disabled(self):
        settings = collection_task_config({"commentOptions": {"visitProfiles": False, "maxProfiles": 2}})
        self.assertEqual(mode_comment_limits(settings["commentOptions"])["max_profiles"], 0)

    def test_invalid_limits_fail_during_configuration(self):
        for options in ([], {"visitProfiles": "false"}, {"maxProfiles": -1}, {"maxPages": 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                collection_task_config({"commentOptions": options})


if __name__ == "__main__":
    unittest.main()
