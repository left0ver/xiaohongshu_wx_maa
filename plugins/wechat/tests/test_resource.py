"""Validate composition and safety without controlling a device."""

import copy
import json
import re
import sys
import tomllib
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1] / "wechat_channels"
BASE = PROJECT / "resource/base"
NODES = {k: v for path in sorted((BASE / "pipeline").glob("*.json"))
         for k, v in json.loads(path.read_text(encoding="utf-8")).items() if not k.startswith("$")}
INTERFACE = json.loads((PROJECT / "interface.json").read_text(encoding="utf-8"))
OPTIONS = INTERFACE.get("option", {})
sys.path.insert(0, str(PROJECT / "collector"))
from interactions import validate_interaction_config  # noqa: E402
from links import LINK_ACTIONS, validate_link_task  # noqa: E402
from modes import collection_task_config  # noqa: E402


def merge(target, source):
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def substitute(value, inputs, values):
    """Mirror the host resolver: a whole-string placeholder takes the field's pipeline type."""
    if isinstance(value, dict):
        return {key: substitute(item, inputs, values) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item, inputs, values) for item in value]
    if isinstance(value, str):
        for field in inputs:
            raw = values.get(field["name"], field.get("default", ""))
            if value.strip() == "{%s}" % field["name"]:
                kind = field.get("pipeline_type", "string")
                return int(raw) if kind == "int" else {"true": True, "false": False}[raw] if kind == "bool" else raw
            value = value.replace("{%s}" % field["name"], raw)
    return value


def resolve_task(task, values):
    """Build the pipeline override the host sends for a task, from option values keyed by option name."""
    output = copy.deepcopy(task.get("pipeline_override", {}))

    def apply(name):
        option = OPTIONS[name]
        kind = option.get("type", "select")
        if kind == "input":
            given = values.get(name, {})
            for field in option["inputs"]:
                # The host refuses to start a run whose input does not match its pattern.
                raw = given.get(field["name"], field.get("default", ""))
                if "verify" in field and not re.search(field["verify"], raw):
                    raise ValueError(f"{name}.{field['name']}: {field.get('pattern_msg', 'invalid input')}")
            merge(output, substitute(option["pipeline_override"], option["inputs"], given))
            return
        default = option.get("default_case", [] if kind == "checkbox" else option["cases"][0]["name"])
        chosen = values.get(name, default)
        chosen = chosen if isinstance(chosen, list) else [chosen]
        children = []
        for case in option["cases"]:
            if case["name"] in chosen:
                merge(output, case.get("pipeline_override", {}))
                children += [child for child in case.get("option", []) if child not in children]
        for child in children:
            apply(child)

    for name in task.get("option", []):
        apply(name)
    return output


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
        self.assertEqual([task["name"] for task in defaults], ["collect_channels"])
        self.assertEqual(NODES[defaults[0]["entry"]]["custom_action"], "CollectWechatChannels")
        self.assertTrue((PROJECT / INTERFACE["agent"]["child_args"][0]).is_file())
        self.assertEqual(INTERFACE["agent"]["child_args"][0], "agent/main.py")
        self.assertTrue((PROJECT / "collector/main.py").is_file())
        profile = tomllib.loads((PROJECT.parent / "profile.toml").read_text(encoding="utf-8"))
        self.assertEqual(profile["pi_assets"], PROJECT.name)
        self.assertIn("collector", profile["pi_include"])
        self.assertEqual(profile["agent"]["timeout_ms"], 21600000)
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

    def test_app_offers_the_agreed_tasks_and_each_has_its_own_entry(self):
        names = [task["name"] for task in INTERFACE["task"]]
        self.assertEqual(names, ["collect_channels", "link_like_comment", "link_reply_comment",
                                 "link_message_commenter", "inspect_messages", "link_like_video",
                                 "link_follow_author", "link_message_author", "link_comment_video"])
        entries = [task["entry"] for task in INTERFACE["task"]]
        self.assertEqual(len(set(entries)), len(entries))
        self.assertFalse(any(task.get("pipeline_override") for task in INTERFACE["task"]))
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

    def test_presets_only_reference_known_tasks(self):
        tasks = {task["name"] for task in INTERFACE["task"]}
        for preset in INTERFACE["preset"]:
            for task in preset["task"]:
                self.assertIn(task["name"], tasks)
                self.assertTrue(task.get("enabled", True))

    def task_param(self, name, values):
        task = next(task for task in INTERFACE["task"] if task["name"] == name)
        base = copy.deepcopy(NODES[task["entry"]].get("custom_action_param", {}))
        merge(base, resolve_task(task, values).get(task["entry"], {}).get("custom_action_param", {}))
        return base

    def test_collection_defaults_to_ten_recommended_videos_without_filters(self):
        param = self.task_param("collect_channels", {})
        self.assertEqual(param, {"entry": "recommend", "count": 10, "videoFilter": "", "commentFilter": ""})
        self.assertEqual(collection_task_config(param), {
            "entry": "recommend", "keyword": "", "count": 10, "videoFilterWords": [], "commentFilterWords": []})

    def test_collection_options_build_search_with_filters(self):
        param = self.task_param("collect_channels", {
            "collect_entry": "search", "collect_keyword": {"keyword": "创业"}, "collect_count": {"count": "25"},
            "collect_video_filter": {"words": "创业, 副业"}, "collect_comment_filter": {"words": "想学 怎么做"}})
        self.assertEqual(param, {"entry": "search", "keyword": "创业", "count": 25,
                                 "videoFilter": "创业, 副业", "commentFilter": "想学 怎么做"})
        settings = collection_task_config(param)
        self.assertEqual(settings["videoFilterWords"], ["创业", "副业"])
        self.assertEqual(settings["commentFilterWords"], ["想学", "怎么做"])

    def test_collection_refuses_an_unusable_entry_or_count(self):
        with self.assertRaisesRegex(ValueError, "collect_keyword_required"):
            self.task_param("collect_channels", {"collect_entry": "search"})
        with self.assertRaisesRegex(ValueError, "collect_count_invalid"):
            self.task_param("collect_channels", {"collect_count": {"count": "0"}})
        with self.assertRaisesRegex(ValueError, "同城"):
            collection_task_config(self.task_param("collect_channels", {"collect_entry": "city"}))

    def test_link_tasks_pass_their_action_link_and_inputs(self):
        link = "https://weixin.qq.com/sph/ASkxIf0OkN"
        expected = {
            "link_like_comment": ("likeComment", {"nickname": "俊国"}),
            "link_reply_comment": ("replyComment", {"nickname": "俊国", "text": "同感"}),
            "link_message_commenter": ("messageCommenter", {"nickname": "俊国", "text": "你好"}),
            "link_like_video": ("likeVideo", {}),
            "link_follow_author": ("followAuthor", {}),
            "link_message_author": ("messageAuthor", {"text": "你好"}),
            "link_comment_video": ("commentVideo", {"text": "拍得真好"}),
        }
        self.assertEqual({action for action, _ in expected.values()}, set(LINK_ACTIONS))
        for name, (action, inputs) in expected.items():
            param = self.task_param(name, {name + "_input": {"link": "看看 " + link, **inputs}})
            self.assertEqual(param, {"action": action, "link": "看看 " + link, **inputs})
            validate_link_task(param)
            with self.assertRaisesRegex(ValueError, "link_url_invalid"):
                self.task_param(name, {name + "_input": {"link": "https://example.com", **inputs}})
            for field in inputs:
                with self.assertRaisesRegex(ValueError, "required"):
                    self.task_param(name, {name + "_input": {"link": link, **inputs, field: " "}})

    def test_message_inspection_runs_alone_and_likes_by_default(self):
        param = self.task_param("inspect_messages", {})
        self.assertEqual(param, {"ensureFeed": True,
                                 "inspectMessages": {"like": True, "maxMessages": 1, "replyText": ""}})
        validate_interaction_config(param)
        param = self.task_param("inspect_messages", {"messages_like": "No",
                                                     "messages_settings": {"count": "3", "reply": "谢谢"}})
        self.assertEqual(param["inspectMessages"], {"like": False, "maxMessages": 3, "replyText": "谢谢"})

    def test_options_are_defined_localized_and_reachable(self):
        catalogs = [json.loads((PROJECT / path).read_text(encoding="utf-8")) for path in INTERFACE["languages"].values()]
        referenced = {name for task in INTERFACE["task"] for name in task.get("option", [])}
        for option in OPTIONS.values():
            for case in option.get("cases", []):
                referenced.update(case.get("option", []))
        self.assertEqual(referenced, set(OPTIONS))
        def keys(item, fields):
            return [item[field].removeprefix("$") for field in fields if item.get(field, "").startswith("$")]
        for option in OPTIONS.values():
            found = keys(option, ("label", "description"))
            for case in option.get("cases", []):
                found += keys(case, ("label", "description"))
            for field in option.get("inputs", []):
                found += keys(field, ("label", "description", "placeholder", "pattern_msg"))
                self.assertTrue(field["label"].startswith("$"))
            for key in found:
                for catalog in catalogs:
                    self.assertIn(key, catalog)
            if option.get("type") == "switch":
                self.assertEqual({case["name"] for case in option["cases"]}, {"Yes", "No"})

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
