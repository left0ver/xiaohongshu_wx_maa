"""Validate composition and safety without controlling a device."""

import copy
import json
import re
import tomllib
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1] / "wechat_channels"
BASE = PROJECT / "resource/base"
NODES = {k: v for path in sorted((BASE / "pipeline").glob("*.json"))
         for k, v in json.loads(path.read_text(encoding="utf-8")).items() if not k.startswith("$")}
INTERFACE = json.loads((PROJECT / "interface.json").read_text(encoding="utf-8"))


def merge(target, source):
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


class ResourceFlowTests(unittest.TestCase):
    def test_collection_navigation_fast_paths_are_guarded_templates_without_actions(self):
        for name, original in [("BatchDiscoverTab", "OpenDiscover"), ("BatchChannelsEntry", "OpenChannels"),
                               ("BatchRecommended", "FeedHeader")]:
            node = NODES[name]
            fast = NODES[original]["any_of"][0]
            self.assertEqual(node["all_of"], fast["all_of"])
            self.assertEqual(node.get("box_index", 0), fast.get("box_index", 0))
            self.assertTrue(all(child["recognition"] == "TemplateMatch" for child in node["all_of"]))
            self.assertEqual(node["action"], "DoNothing")
            self.assertEqual(node["next"], [])
            self.assertEqual(node["timeout"], 1)

    def test_collector_is_the_only_default_task_and_has_an_agent(self):
        defaults = [task for task in INTERFACE["task"] if task["default_check"]]
        self.assertEqual([task["name"] for task in defaults], ["collect_video"])
        self.assertEqual(NODES[defaults[0]["entry"]]["custom_action"], "CollectWechatVideo")
        self.assertTrue((PROJECT / INTERFACE["agent"]["child_args"][0]).is_file())
        self.assertEqual(INTERFACE["agent"]["child_args"][0], "agent/main.py")
        self.assertTrue((PROJECT / "collector/main.py").is_file())
        profile = tomllib.loads((PROJECT.parent / "profile.toml").read_text(encoding="utf-8"))
        self.assertEqual(profile["pi_assets"], PROJECT.name)
        self.assertIn("collector", profile["pi_include"])
        self.assertEqual(profile["agent"]["timeout_ms"], 600000)
        runtime = profile["agent"]["runtimes"][0]
        self.assertEqual(runtime["args"][1], INTERFACE["agent"]["child_args"][0])
        self.assertIn("{abi}", runtime["env"]["MAAFW_BINARY_PATH"])
        self.assertTrue((PROJECT / "requirements.txt").is_file())
        self.assertTrue((PROJECT / "collector/ui-dump.dex").read_bytes().startswith(b"dex\n"))
        templates = NODES["BatchProfileMore"]["template"]
        self.assertTrue(templates)
        for template in [templates] if isinstance(templates, str) else templates:
            self.assertTrue((BASE / "image" / template).is_file(), template)

    def test_each_composed_task_is_acyclic_and_likes_at_most_once(self):
        for task in INTERFACE["task"]:
            nodes = copy.deepcopy(NODES)
            merge(nodes, task.get("pipeline_override", {}))
            def visit(name, path):
                self.assertNotIn(name, path, (task["name"], path))
                path = [*path, name]
                node = nodes[name]
                self.assertFalse(node.get("on_error"))
                self.assertEqual(node.get("repeat", 1), 1)
                self.assertLessEqual(sum(x in ("LikeCurrentVideo", "LikeVisibleVideo") for x in path), 1)
                for successor in node.get("next", []):
                    visit(successor, path)
            visit(task["entry"], [])

    def test_atomic_task_overrides_do_not_leak_into_the_next_task(self):
        tasks = {t["name"]: t for t in INTERFACE["task"]}
        for name, entry in [("open_wechat", "StartWeChat"), ("open_discover", "OpenDiscover"), ("open_channels", "OpenChannels")]:
            atomic = copy.deepcopy(NODES)
            merge(atomic, tasks[name]["pipeline_override"])
            self.assertNotEqual(atomic[entry]["next"], NODES[entry]["next"])
        self.assertEqual(tasks["profile_video_like"]["pipeline_override"], {
            "OpenVisibleAuthor": {"next": ["OpenAuthorVideo"]},
            "OpenAuthorVideo": {"next": ["LikeVideo"]},
        })
        self.assertFalse(any(t["default_check"] for t in INTERFACE["task"][1:]))

    def test_standalone_like_is_idempotent_and_verifies_the_clicked_thumb(self):
        self.assertEqual(NODES["LikeVideo"]["next"], ["VideoAlreadyLiked", "LikeVisibleVideo"])
        self.assertEqual(NODES["VideoAlreadyLiked"]["all_of"], ["VideoScreen", "RedThumb"])
        self.assertEqual(NODES["LikeVisibleVideo"]["all_of"], ["VideoScreen", "WhiteThumb"])
        self.assertEqual(NODES["LikeVisibleVideo"]["max_hit"], 1)
        self.assertEqual(NODES["LikeVisibleVideo"]["next"], ["LikedVideoReady"])
        guard = NODES["LikedVideoReady"]["all_of"][1]
        self.assertEqual(guard["roi"], "LikeVisibleVideo")
        self.assertEqual(guard["template"], "thumb-red")
        self.assertEqual(guard["threshold"], .88)
        self.assertFalse(NODES["LikedVideoReady"].get("next"))

    def test_default_like_does_not_retry_or_skip_red_confirmation(self):
        self.assertNotIn("target", NODES["LikeCurrentVideo"])
        self.assertEqual(NODES["LikeCurrentVideo"].get("repeat", 1), 1)
        self.assertEqual(NODES["LikeCurrentVideo"]["all_of"][2], "WhiteThumb")
        self.assertEqual(NODES["LikeCurrentVideo"]["box_index"], 2)
        self.assertEqual(NODES["LikeCurrentVideo"]["next"], ["OpenAuthorAfterLike"])
        guard = NODES["OpenAuthorAfterLike"]["all_of"][1]
        self.assertEqual(guard["roi"], "LikeCurrentVideo")
        self.assertEqual(guard["template"], "thumb-red")
        self.assertEqual(NODES["LikeCurrentVideo"]["max_hit"], 1)
        self.assertEqual(NODES["OpenChannels"]["next"], ["OpenAuthor", "LikeCurrentVideo"])

    def test_label_fast_paths_retain_original_lazy_ocr_guards(self):
        for name in ["OpenDiscover", "OpenChannels", "FeedHeader", "ReturnToFeed", "PersonalPage"]:
            node = NODES[name]
            self.assertEqual(node["recognition"], "Or")
            fallback = node["any_of"][-1]
            self.assertEqual(fallback["custom_recognition"], "MtaOcrFallback")
            self.assertIn(fallback["custom_recognition_param"]["type"], ["And", "Or"])
            def check(tree):
                self.assertNotIn("color_filter", tree)
                self.assertNotIn("action", tree)
                for key in ("all_of", "any_of"):
                    for child in tree.get(key, []):
                        self.assertIsInstance(child, dict)
                        self.assertIn(child["recognition"], ["And", "Or", "OCR"])
                        check(child)
            check(fallback["custom_recognition_param"]["param"])

    def test_profile_video_click_requires_a_profile_and_a_card_match(self):
        node = NODES["OpenAuthorVideo"]
        page, card = node["all_of"]
        self.assertEqual(page, "AuthorReady")
        self.assertEqual(card["roi"], page)
        self.assertEqual(card["template"], "video-card")
        self.assertEqual(node["box_index"], 1)
        self.assertEqual(node["next"], ["VideoReady"])

    def test_clicks_use_recognized_boxes_and_all_references_exist(self):
        def walk(node):
            if node.get("action") == "Click":
                self.assertNotEqual(node.get("recognition"), "DirectHit")
                self.assertNotIn("target", node)
                self.assertNotIn("target_offset", node)
                self.assertEqual(node["max_hit"], 1)
            if node.get("recognition") == "TemplateMatch":
                templates = node["template"]
                self.assertTrue(templates)
                for template in [templates] if isinstance(templates, str) else templates:
                    path = BASE / "image" / template
                    self.assertTrue(path.exists(), path)
            for key in ("all_of", "any_of"):
                for child in node.get(key, []):
                    if isinstance(child, str):
                        self.assertIn(child, NODES)
                    else:
                        walk(child)
        for node in NODES.values():
            walk(node)

    def test_white_thumb_retains_wide_and_full_screen_fallback(self):
        narrow, broad, full = NODES["WhiteThumb"]["any_of"]
        self.assertLess(narrow["roi"][3], broad["roi"][3])
        self.assertNotIn("roi", full)
        self.assertEqual({k: v for k, v in broad.items() if k != "roi"}, full)
        self.assertEqual(full["threshold"], .88)

    def test_red_candidates_require_template_validation(self):
        color, shape = NODES["RedThumb"]["all_of"]
        self.assertEqual(color["recognition"], "ColorMatch")
        self.assertEqual(shape["roi"], color["sub_name"])
        self.assertEqual(shape["threshold"], .88)
        self.assertEqual(shape["method"], 10001)

    def test_author_filter_rejects_follow_buttons(self):
        pattern = NODES["FeedAuthorName"]["expected"]
        for label in ("关注", "+关注", "＋关注", "√已关注", "私信", "客服"):
            self.assertIsNone(re.fullmatch(pattern, label))
        self.assertIsNotNone(re.fullmatch(pattern, "腾讯娱乐"))

    def test_presets_only_reference_known_operations_in_declared_order(self):
        tasks = {task["name"] for task in INTERFACE["task"]}
        presets = {preset["name"]: preset for preset in INTERFACE["preset"]}
        for preset in presets.values():
            for task in preset["task"]:
                self.assertIn(task["name"], tasks)
                self.assertTrue(task.get("enabled", True))
        self.assertEqual([task["name"] for task in presets["compose_profile_like"]["task"]],
                         ["open_wechat", "open_discover", "open_channels", "open_author", "open_author_video", "like_video"])

    def test_localizations_cover_all_tasks_and_models_exist(self):
        catalogs = [json.loads((PROJECT / path).read_text(encoding="utf-8")) for path in INTERFACE["languages"].values()]
        self.assertEqual(set(catalogs[0]), set(catalogs[1]))
        for task in INTERFACE["task"]:
            self.assertIn(task["entry"], NODES)
            for field in ("label", "description"):
                for catalog in catalogs:
                    self.assertIn(task[field].removeprefix("$"), catalog)
        for name in ("det.onnx", "rec.onnx", "keys.txt"):
            self.assertGreater((BASE / "model/ocr" / name).stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
