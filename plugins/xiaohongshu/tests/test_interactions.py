import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "xiaohongshu" / "agent"))
from comments import comment_section, visible_comments
from interactions import (
    DailyQuotaStore,
    QuotaExceededError,
    collect_work_action,
    comment_like_state,
    ensure_clean_page_on_resume,
    follow_profile_user,
    inspect_and_handle_messages,
    interact_profile_first_work,
    interact_work_and_comments,
    like_comment,
    like_work,
    locate_target_comment,
    parse_comment_at_messages,
    post_work_comment,
    preemptive_reply_comment,
    profile_first_work_card,
    profile_follow_state,
    reply_to_comment,
    visit_and_touch_commenter_profile,
    work_collect_state,
    work_like_state,
)
from ui import Page, PageMismatch

FIXTURES = Path(__file__).with_name("fixtures")


def make_comment_page():
    return Page((FIXTURES / "comments.xml").read_text())


def make_video_page(liked=False, collected=False):
    like_sel = "true" if liked else "false"
    collect_desc = "已收藏991" if collected else "收藏990"
    collect_sel = "true" if collected else "false"
    xml = f"""<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
    <hierarchy>
      <node class="android.widget.FrameLayout" package="com.xingin.xhs" bounds="[0,0][1080,2408]">
        <node class="android.widget.ImageView" package="com.xingin.xhs" content-desc="返回" clickable="true" bounds="[30,114][126,210]" />
        <node class="android.widget.ImageView" package="com.xingin.xhs" content-desc="分享" clickable="true" bounds="[942,114][1038,210]" />
        <node class="android.view.ViewGroup" package="com.xingin.xhs" content-desc="作者视频达人" bounds="[36,1840][600,1920]">
          <node class="android.widget.TextView" package="com.xingin.xhs" resource-id="com.xingin.xhs:id/matrixNickNameView" text="视频达人" bounds="[140,1850][360,1910]" />
        </node>
        <node class="android.widget.TextView" package="com.xingin.xhs" resource-id="com.xingin.xhs:id/noteContentText" text="这是一条测试视频标题" content-desc="这是一条测试视频标题" bounds="[36,1930][980,2040]" />
        <node class="android.widget.FrameLayout" package="com.xingin.xhs" clickable="true" bounds="[0,2276][447,2408]">
          <node class="android.widget.TextView" package="com.xingin.xhs" text="说点什么..." bounds="[36,2294][447,2390]" />
        </node>
        <node class="android.widget.LinearLayout" package="com.xingin.xhs" clickable="true" selected="{like_sel}" bounds="[447,2276][642,2408]">
          <node class="android.widget.ImageView" package="com.xingin.xhs" selected="{like_sel}" bounds="[486,2306][558,2378]" />
          <node class="android.widget.TextView" package="com.xingin.xhs" text="108" bounds="[564,2309][626,2375]" />
        </node>
        <node class="android.widget.Button" package="com.xingin.xhs" content-desc="{collect_desc}" clickable="true" selected="{collect_sel}" bounds="[642,2276][837,2408]">
          <node class="android.widget.ImageView" package="com.xingin.xhs" selected="{collect_sel}" bounds="[685,2306][757,2378]" />
          <node class="android.widget.TextView" package="com.xingin.xhs" text="{'991' if collected else '990'}" bounds="[763,2309][819,2375]" />
        </node>
        <node class="android.widget.Button" package="com.xingin.xhs" content-desc="评论 12" clickable="true" bounds="[837,2276][1032,2408]">
          <node class="android.widget.ImageView" package="com.xingin.xhs" bounds="[886,2306][958,2378]" />
          <node class="android.widget.TextView" package="com.xingin.xhs" text="12" bounds="[964,2309][1006,2375]" />
        </node>
      </node>
    </hierarchy>"""
    return Page(xml)


def make_composer_page(text="留下你的想法吧", hint="留下你的想法吧", send_enabled=False):
    enabled_str = "true" if send_enabled else "false"
    xml = f"""<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
    <hierarchy>
      <node class="android.widget.FrameLayout" package="com.xingin.xhs" bounds="[0,0][1080,2408]">
        <node class="android.widget.EditText" package="com.xingin.xhs" text="{text}" hint="{hint}" clickable="true" enabled="true" bounds="[66,1847][990,1961]" />
        <node class="android.widget.TextView" package="com.xingin.xhs" text="发送" clickable="true" enabled="{enabled_str}" bounds="[858,1997][1050,2093]" />
      </node>
    </hierarchy>"""
    return Page(xml)


def make_action_sheet_page():
    xml = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
    <hierarchy>
      <node class="android.widget.FrameLayout" package="com.xingin.xhs" bounds="[0,0][1080,2408]">
        <node class="android.widget.TextView" package="com.xingin.xhs" text="分享到微信" clickable="true" bounds="[48,1490][1032,1630]" />
        <node class="android.widget.TextView" package="com.xingin.xhs" text="回复" clickable="true" bounds="[48,1780][1032,1920]" />
        <node class="android.widget.TextView" package="com.xingin.xhs" text="复制" clickable="true" bounds="[48,1930][1032,2070]" />
      </node>
    </hierarchy>"""
    return Page(xml)


def make_feed_page(cards_spec=None):
    cards_spec = cards_spec or [
        ("笔记", "春日野餐攻略", "样本作者", "[18,260][531,1080]", "[18,260][531,900]"),
        ("视频", "这是一条测试视频标题", "视频达人", "[549,260][1062,1080]", "[549,260][1062,900]"),
    ]
    nodes_xml = []
    for kind, title, author, box, cover_box in cards_xml_entries(cards_spec):
        nodes_xml.append(
            f'<node class="android.widget.FrameLayout" package="com.xingin.xhs" '
            f'content-desc="{kind} {title} 来自{author} 99赞" bounds="{box}" clickable="true">'
            f'<node class="android.widget.ImageView" package="com.xingin.xhs" bounds="{cover_box}" />'
            f'<node class="android.widget.TextView" package="com.xingin.xhs" text="{author}" bounds="[18,920][531,1060]" />'
            f"</node>"
        )
    joined = "".join(nodes_xml)
    xml = f"""<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
    <hierarchy>
      <node class="android.widget.FrameLayout" package="com.xingin.xhs" bounds="[0,0][1080,2408]">
        <node class="android.widget.TextView" package="com.xingin.xhs" content-desc="发现" selected="true" bounds="[420,90][660,210]" />
        {joined}
        <node class="android.widget.TextView" package="com.xingin.xhs" content-desc="首页" bounds="[0,2240][270,2408]" />
      </node>
    </hierarchy>"""
    return Page(xml)


def cards_xml_entries(cards_spec):
    entries = []
    for item in cards_spec:
        kind, title, author, box, cover_box = item
        entries.append((kind, title, author, box, cover_box))
    return entries


def make_bgr_frame(width=1080, height=2408, fill=(240, 240, 240), patch_box=None, patch_bgr=None):
    row = [list(fill) for _ in range(width)]
    frame = [copy.deepcopy(row) for _ in range(height)]
    if patch_box is not None and patch_bgr is not None:
        left, top, right, bottom = patch_box
        for y in range(max(0, top), min(height, bottom)):
            for x in range(max(0, left), min(width, right)):
                frame[y][x] = list(patch_bgr)
    return frame


def add_comment_row(page, nickname, content, is_reply=False, comment_time="刚刚"):
    _, listing = comment_section(page)
    left_bound = 140 if is_reply else 0
    row = ET.Element(
        "node",
        {
            "class": "android.widget.LinearLayout",
            "package": "com.xingin.xhs",
            "bounds": f"[{left_bound},560][1080,598]",
            "clickable": "true",
        },
    )
    panel = ET.SubElement(
        row,
        "node",
        {
            "class": "android.widget.LinearLayout",
            "package": "com.xingin.xhs",
            "bounds": f"[{left_bound + 40},562][1080,596]",
        },
    )
    name_wrap = ET.SubElement(
        panel,
        "node",
        {
            "class": "android.widget.LinearLayout",
            "package": "com.xingin.xhs",
            "bounds": f"[{left_bound + 40},562][500,572]",
        },
    )
    ET.SubElement(
        name_wrap,
        "node",
        {
            "class": "android.widget.TextView",
            "package": "com.xingin.xhs",
            "text": nickname,
            "clickable": "true",
            "bounds": f"[{left_bound + 40},562][500,572]",
        },
    )
    ET.SubElement(
        panel,
        "node",
        {
            "class": "android.widget.TextView",
            "package": "com.xingin.xhs",
            "text": content,
            "clickable": "false",
            "bounds": f"[{left_bound + 40},573][960,584]",
        },
    )
    footer = ET.SubElement(
        panel,
        "node",
        {
            "class": "android.widget.RelativeLayout",
            "package": "com.xingin.xhs",
            "bounds": f"[{left_bound + 40},585][1080,596]",
        },
    )
    ET.SubElement(
        footer,
        "node",
        {
            "class": "android.widget.TextView",
            "package": "com.xingin.xhs",
            "text": f"{comment_time} 广东 回复",
            "bounds": f"[{left_bound + 40},585][760,596]",
        },
    )
    like_box = ET.SubElement(
        footer,
        "node",
        {
            "class": "android.widget.LinearLayout",
            "package": "com.xingin.xhs",
            "clickable": "true",
            "selected": "false",
            "bounds": "[843,585][921,596]",
        },
    )
    ET.SubElement(
        like_box,
        "node",
        {
            "class": "android.widget.ImageView",
            "package": "com.xingin.xhs",
            "selected": "false",
            "bounds": "[843,585][900,596]",
        },
    )
    ET.SubElement(
        like_box,
        "node",
        {
            "class": "android.widget.TextView",
            "package": "com.xingin.xhs",
            "text": "",
            "bounds": "[901,585][921,596]",
        },
    )
    listing.insert(1, row)
    return Page(ET.tostring(page.root, encoding="unicode"))


class InteractionTests(unittest.TestCase):
    def test_daily_quota_store_persists_across_restarts_and_isolates_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            limits = {"like": 2, "collect": 1, "comment": 1, "follow": 1, "dm": 1}
            store = DailyQuotaStore(tmp, limits=limits, today_fn=lambda: "2026-11-06")
            self.assertTrue(store.can_perform("like"))
            self.assertEqual(store.consume("like"), 1)
            store.record_action("work_like", "work-1", quota_action="like")
            self.assertFalse(store.can_perform("like"))
            self.assertTrue(store.can_perform("collect"))
            with self.assertRaises(QuotaExceededError):
                store.consume("like")

            # Simulate app/agent restart on the same day: used count and records must not reset.
            restarted = DailyQuotaStore(tmp, limits=limits, today_fn=lambda: "2026-11-06")
            self.assertEqual(restarted.used_for("like"), 2)
            self.assertEqual(restarted.remaining_for("like"), 0)
            self.assertFalse(restarted.can_perform("like"))
            self.assertTrue(restarted.has_record("work_like", "work-1"))
            self.assertTrue(restarted.can_perform("collect"))
            self.assertEqual(restarted.consume("collect"), 1)
            self.assertIn("like", restarted.snapshot()["exhausted"])
            self.assertIn("collect", restarted.snapshot()["exhausted"])
            self.assertNotIn("comment", restarted.snapshot()["exhausted"])

            # Next day rolls over counters while preserving limits.
            next_day = DailyQuotaStore(tmp, today_fn=lambda: "2026-11-07")
            self.assertEqual(next_day.used_for("like"), 0)
            self.assertEqual(next_day.limit_for("like"), 2)
            self.assertFalse(next_day.has_record("work_like", "work-1"))

    def test_resume_dismisses_composer_and_action_sheet_before_identifying_work(self):
        pages = [
            make_composer_page(text="半截草稿", hint="留下你的想法吧", send_enabled=True),
            make_action_sheet_page(),
            make_comment_page(),
        ]
        state = {"idx": 0, "backs": 0}
        device = Mock(has_vision=False, stopped=lambda: False)
        device.page.side_effect = lambda: pages[state["idx"]]

        def on_back():
            state["backs"] += 1
            state["idx"] = min(state["idx"] + 1, len(pages) - 1)

        device.back.side_effect = on_back
        resumed = ensure_clean_page_on_resume(device)
        self.assertEqual(state["backs"], 2)
        self.assertEqual(resumed["pageType"], "image_note")
        self.assertEqual(resumed["work"]["authorNickname"], "样本作者")

    def test_image_and_video_like_and_collect_detect_existing_state_and_avoid_unliking(self):
        with tempfile.TemporaryDirectory() as tmp:
            quota = DailyQuotaStore(tmp, limits={"like": 2, "collect": 2})
            current_page = {"page": make_comment_page()}
            clicks = []
            device = Mock(has_vision=False, stopped=lambda: False)
            device.page.side_effect = lambda: current_page["page"]
            device.wait.side_effect = lambda fn, **_: fn(current_page["page"])

            def click(selector):
                node = selector(current_page["page"])
                clicks.append(node.get("content-desc"))
                if node.get("content-desc", "").startswith("点赞"):
                    btn = current_page["page"].unique(description="点赞 18")
                    btn.set("content-desc", "已点赞19")
                    btn.set("selected", "true")
                    btn[0].set("selected", "true")
                    btn[1].set("text", "19")
                elif node.get("content-desc", "").startswith("收藏"):
                    btn = current_page["page"].unique(description="收藏 4")
                    btn.set("content-desc", "已收藏5")
                    btn.set("selected", "true")
                    btn[0].set("selected", "true")
                    btn[1].set("text", "5")
                current_page["page"] = Page(ET.tostring(current_page["page"].root, encoding="unicode"))

            device.click.side_effect = click

            res_like = like_work(device, quota_store=quota, enabled=True)
            self.assertTrue(res_like["executed"])
            self.assertEqual(res_like["afterCountText"], "19")
            self.assertEqual(quota.used_for("like"), 1)

            # Second call on already-liked work must skip and NOT click again.
            res_like_again = like_work(device, quota_store=quota, enabled=True)
            self.assertFalse(res_like_again["executed"])
            self.assertEqual(res_like_again["reason"], "already_liked")
            self.assertEqual(len(clicks), 1)
            self.assertEqual(quota.used_for("like"), 1)

            res_collect = collect_work_action(device, quota_store=quota, enabled=True)
            self.assertTrue(res_collect["executed"])
            self.assertEqual(res_collect["afterCountText"], "5")
            self.assertEqual(quota.used_for("collect"), 1)

            res_collect_again = collect_work_action(device, quota_store=quota, enabled=True)
            self.assertFalse(res_collect_again["executed"])
            self.assertEqual(res_collect_again["reason"], "already_collected")
            self.assertEqual(len(clicks), 2)

            # Video work like & collect + OpenCV visual state recognition.
            v_page = make_video_page(liked=False, collected=False)
            red_frame = make_bgr_frame(patch_box=(486, 2306, 558, 2378), patch_bgr=(30, 20, 235))
            yellow_frame = make_bgr_frame(patch_box=(685, 2306, 757, 2378), patch_bgr=(20, 210, 245))
            self.assertTrue(work_like_state(v_page, frame=red_frame)["visualLiked"])
            self.assertTrue(work_collect_state(v_page, frame=yellow_frame)["visualCollected"])

    def test_post_work_comment_verifies_input_and_stops_before_send_when_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            quota = DailyQuotaStore(tmp, limits={"comment": 1})
            base_page = make_comment_page()
            state = {"page": base_page, "stopped": False, "sent": False}
            device = Mock(has_vision=False, stopped=lambda: state["stopped"])
            device.page.side_effect = lambda: state["page"]
            device.wait.side_effect = lambda fn, **_: fn(state["page"])

            def click(selector):
                node = selector(state["page"])
                if node.get("content-desc") == "评论框":
                    state["page"] = make_composer_page(text="留下你的想法吧", hint="留下你的想法吧", send_enabled=False)
                elif node.get("text") == "发送":
                    state["sent"] = True
                    updated = add_comment_row(base_page, "测试账号", "拍得真好", is_reply=False)
                    updated.unique(description="评论 2").set("content-desc", "评论 3")
                    state["page"] = updated

            def set_text(text):
                state["page"] = make_composer_page(text=text, hint="留下你的想法吧", send_enabled=True)

            device.click.side_effect = click
            device.set_text.side_effect = set_text

            result = post_work_comment(device, "拍得真好", quota_store=quota)
            self.assertTrue(result["executed"])
            self.assertTrue(state["sent"])
            self.assertEqual(result["verifiedBy"], "visible_comment")
            self.assertEqual(quota.used_for("comment"), 1)

            # Calling again on the same work/text is idempotent and does not re-send.
            dup = post_work_comment(device, "拍得真好", quota_store=quota)
            self.assertFalse(dup["executed"])
            self.assertEqual(dup["reason"], "already_commented_in_ledger")

            # Stop protection right after set_text prevents clicking send.
            quota_stop = DailyQuotaStore(tmp, limits={"comment": 5})
            state["page"] = make_comment_page()
            state["sent"] = False

            def set_text_then_stop(text):
                state["page"] = make_composer_page(text=text, hint="留下你的想法吧", send_enabled=True)
                state["stopped"] = True

            device.set_text.side_effect = set_text_then_stop
            with self.assertRaises(InterruptedError):
                post_work_comment(device, "另一条评论", quota_store=quota_stop)
            self.assertFalse(state["sent"])

    def test_comment_like_and_reply_verify_target_and_reject_wrong_composer_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            quota = DailyQuotaStore(tmp, limits={"like": 2, "comment": 2})
            page = make_comment_page()
            _, comments = visible_comments(page)
            target_comment = comments[0]
            self.assertEqual(target_comment.payload["nickname"], "评论者甲")

            state = {"page": page}
            device = Mock(has_vision=False, stopped=lambda: False)
            device.page.side_effect = lambda: state["page"]
            device.wait.side_effect = lambda fn, **_: fn(state["page"])

            def click_for_like(selector):
                node = selector(state["page"])
                node.set("selected", "true")
                node[0].set("selected", "true")
                node[1].set("text", "1")
                state["page"] = Page(ET.tostring(state["page"].root, encoding="unicode"))

            device.click.side_effect = click_for_like
            like_res = like_comment(device, {"nickname": "评论者甲", "content": "怎么联系"}, quota_store=quota)
            self.assertTrue(like_res["executed"])
            self.assertEqual(like_res["afterCountText"], "1")
            self.assertEqual(quota.used_for("like"), 1)

            # Direct reply composer for another user's comment (@评论者甲)
            def click_for_reply(selector):
                node = selector(state["page"])
                if node.get("text", "").strip() == "怎么联系":
                    state["page"] = make_composer_page(
                        text="回复 @评论者甲：",
                        hint="回复 @评论者甲：",
                        send_enabled=False,
                    )
                elif node.get("text") == "发送":
                    state["page"] = add_comment_row(page, "测试账号", "赞", is_reply=True)

            device.click.side_effect = click_for_reply
            device.set_text.side_effect = lambda text: state.__setitem__(
                "page",
                make_composer_page(text=text, hint="回复 @评论者甲：", send_enabled=True),
            )
            reply_res = reply_to_comment(
                device,
                {"nickname": "评论者甲", "content": "怎么联系"},
                "赞",
                quota_store=quota,
            )
            self.assertTrue(reply_res["executed"])
            self.assertEqual(reply_res["targetNickname"], "评论者甲")
            self.assertEqual(reply_res["verifiedBy"], "visible_reply")
            self.assertEqual(quota.used_for("comment"), 1)

            # Even if reply text changes ("好的") or time advances ("6小时前" -> "7小时前"),
            # the same target comment must NOT be replied to a second time.
            aged_page = make_comment_page()
            aged_page.unique(text="6小时前 山西 回复").set("text", "7小时前 山西 回复")
            state["page"] = aged_page
            dup_reply = reply_to_comment(
                device,
                {"nickname": "评论者甲", "content": "怎么联系"},
                "好的",
                quota_store=quota,
            )
            self.assertFalse(dup_reply["executed"])
            self.assertEqual(dup_reply["reason"], "already_replied_in_ledger")

            # Self-comment protection 1: cannot target a comment authored by our own recorded nickname ("测试账号")
            state["page"] = add_comment_row(make_comment_page(), "测试账号", "我自己的评论", is_reply=False)
            with self.assertRaises(PageMismatch):
                reply_to_comment(
                    device,
                    {"content": "我自己的评论"},
                    "自言自语",
                    quota_store=quota,
                )

            # Self-comment protection 2: if clicking a comment opens the self-comment menu ("分享到微信" / "删除"),
            # reply_to_comment must back out and refuse to reply to own comment.
            state["page"] = make_comment_page()
            backed_out = {"count": 0}
            device.back.side_effect = lambda: backed_out.__setitem__("count", backed_out["count"] + 1)
            device.click.side_effect = lambda selector: state.__setitem__("page", make_action_sheet_page())
            with self.assertRaises(PageMismatch):
                reply_to_comment(
                    device,
                    {"nickname": "评论者乙"},
                    "误回自己",
                    quota_store=quota,
                )
            self.assertEqual(backed_out["count"], 1)

            # Wrong reply target in composer must be rejected and backed out without sending.
            state["page"] = make_comment_page()
            quota_fresh = DailyQuotaStore(tmp, limits={"like": 2, "comment": 5})
            quota_fresh._state["records"]["comment_reply"] = []
            quota_fresh._save(quota_fresh._state)
            device.click.side_effect = lambda selector: state.__setitem__(
                "page",
                make_composer_page(text="回复 @评论者乙：", hint="回复 @评论者乙：", send_enabled=False),
            )
            with self.assertRaises(PageMismatch):
                reply_to_comment(
                    device,
                    {"nickname": "评论者甲", "content": "怎么联系"},
                    "新回复",
                    quota_store=quota_fresh,
                )
            self.assertEqual(backed_out["count"], 2)

    def test_preemptive_reply_restores_feed_and_never_replies_when_target_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            quota = DailyQuotaStore(tmp, limits={"comment": 2})
            feed_page = make_feed_page()
            note_page = make_comment_page()
            # Ensure note_page has a body text matching feed card "春日野餐攻略" so verify_card passes.
            _, listing = comment_section(note_page)
            body_group = ET.Element(
                "node",
                {
                    "class": "android.widget.LinearLayout",
                    "package": "com.xingin.xhs",
                    "bounds": "[45,255][1035,280]",
                },
            )
            ET.SubElement(
                body_group,
                "node",
                {
                    "class": "android.widget.TextView",
                    "package": "com.xingin.xhs",
                    "text": "春日野餐攻略",
                    "bounds": "[45,255][600,268]",
                },
            )
            ET.SubElement(
                body_group,
                "node",
                {
                    "class": "android.widget.TextView",
                    "package": "com.xingin.xhs",
                    "text": "正文详情内容",
                    "bounds": "[45,268][600,280]",
                },
            )
            listing.insert(0, body_group)
            note_page = Page(ET.tostring(note_page.root, encoding="unicode"))

            state = {"page": feed_page, "sent_replies": []}
            device = Mock(has_vision=False, stopped=lambda: False)
            device.page.side_effect = lambda: state["page"]
            device.wait.side_effect = lambda fn, **_: fn(state["page"])

            def command(*args):
                if args[:2] == ("input", "tap"):
                    state["page"] = note_page
                return "resumed\n"

            def click(selector):
                node = selector(state["page"])
                if node.get("text", "").strip() == "怎么联系":
                    state["page"] = make_composer_page(
                        text="回复 @评论者甲：",
                        hint="回复 @评论者甲：",
                        send_enabled=False,
                    )
                elif node.get("text") == "发送":
                    state["sent_replies"].append("收到")
                    state["page"] = add_comment_row(note_page, "测试账号", "收到", is_reply=True)

            device.command.side_effect = command
            device.click.side_effect = click
            device.set_text.side_effect = lambda text: state.__setitem__(
                "page",
                make_composer_page(text=text, hint="回复 @评论者甲：", send_enabled=True),
            )
            device.back.side_effect = lambda: state.__setitem__("page", feed_page)

            # 1) Negative test: unlocatable comment on target work must NOT reply to any other comment and must restore feed.
            missing_comment_res = preemptive_reply_comment(
                device,
                target_work={"authorNickname": "样本作者", "title": "春日野餐攻略"},
                target_comment={"nickname": "不存在的评论者", "content": "不存在的内容"},
                reply_text="绝不能误发",
                quota_store=quota,
                max_feed_screens=1,
                max_comment_scrolls=0,
            )
            self.assertFalse(missing_comment_res["executed"])
            self.assertEqual(missing_comment_res["reason"], "target_comment_not_found")
            self.assertTrue(missing_comment_res["restored"])
            self.assertEqual(state["sent_replies"], [])
            self.assertEqual(len(state["page"].cards()), 2)

            # 2) Positive test: locates target work + target comment, replies, and restores original feed.
            ok_res = preemptive_reply_comment(
                device,
                target_work={"authorNickname": "样本作者", "title": "春日野餐攻略"},
                target_comment={"nickname": "评论者甲", "content": "怎么联系"},
                reply_text="收到",
                quota_store=quota,
                max_feed_screens=1,
                max_comment_scrolls=0,
            )
            self.assertTrue(ok_res["executed"])
            self.assertTrue(ok_res["replied"])
            self.assertTrue(ok_res["restored"])
            self.assertEqual(state["sent_replies"], ["收到"])
            self.assertEqual(len(state["page"].cards()), 2)

            # 3) End-to-end report runner test
            state["page"] = note_page
            report = interact_work_and_comments(
                device,
                tmp,
                options={"likeWork": False, "collectWork": False},
            )
            self.assertEqual(report["status"], "interactions_completed_locally")
            self.assertTrue(report["results"]["likeWork"]["skipped"])

    def test_profile_touch_follow_and_first_work_and_return_to_comment_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            quota = DailyQuotaStore(tmp, limits={"follow": 2, "like": 2, "collect": 2})
            comments_pg = make_comment_page()
            profile_pg = Page((FIXTURES / "commenter.xml").read_text())
            first_work_pg = make_video_page(liked=False, collected=False)

            state = {"page": comments_pg}
            device = Mock(has_vision=False, stopped=lambda: False)
            device.page.side_effect = lambda: state["page"]
            device.wait.side_effect = lambda fn, **_: fn(state["page"])

            def command(*args):
                if args[:2] == ("input", "tap"):
                    state["page"] = first_work_pg
                return "resumed\n"

            def click(selector):
                node = selector(state["page"])
                if node.get("text") == "评论者甲":
                    state["page"] = profile_pg
                elif node.get("bounds") == "[36,684][468,780]":
                    # Follow button clicked on profile -> update label to 已关注
                    for n in profile_pg.nodes:
                        if n.get("bounds") == "[210,702][294,762]":
                            n.set("text", "已关注")
                    state["page"] = Page(ET.tostring(profile_pg.root, encoding="unicode"))
                elif node.get("bounds") == "[447,2276][642,2408]":
                    state["page"] = make_video_page(liked=True, collected=False)
                elif node.get("bounds") == "[642,2276][837,2408]":
                    state["page"] = make_video_page(liked=True, collected=True)

            def back():
                if any(n.get("resource-id", "").endswith("/matrixNickNameView") for n in state["page"].nodes):
                    state["page"] = Page(ET.tostring(profile_pg.root, encoding="unicode"))
                else:
                    state["page"] = comments_pg

            device.command.side_effect = command
            device.click.side_effect = click
            device.back.side_effect = back

            res = visit_and_touch_commenter_profile(
                device,
                {"nickname": "评论者甲", "content": "怎么联系"},
                follow_enabled=True,
                like_first_work=True,
                collect_first_work=True,
                quota_store=quota,
            )
            self.assertEqual(res["touchStatus"], "TOUCHED")
            self.assertEqual(
                res["profileActionsPerformed"],
                ["FOLLOW", "LIKE_FIRST_WORK", "COLLECT_FIRST_WORK"],
            )
            self.assertTrue(res["returnedToCommentPosition"])
            self.assertEqual(quota.used_for("follow"), 1)
            self.assertEqual(quota.used_for("like"), 1)
            self.assertEqual(quota.used_for("collect"), 1)

            # Empty profile handling
            empty_prof_xml = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
            <hierarchy>
              <node class="android.widget.FrameLayout" package="com.xingin.xhs" bounds="[0,0][1080,2408]">
                <node class="android.widget.ImageView" package="com.xingin.xhs" content-desc="头像,空主页用户" bounds="[6,234][330,558]" />
                <node class="android.widget.TextView" package="com.xingin.xhs" text="空主页用户" bounds="[342,299][776,385]" />
                <node class="android.widget.TextView" package="com.xingin.xhs" text="小红书号：99999" bounds="[342,397][728,439]" />
                <node class="android.widget.Button" package="com.xingin.xhs" content-desc="10粉丝" bounds="[307,582][514,654]" />
                <node class="android.widget.FrameLayout" package="com.xingin.xhs" clickable="true" bounds="[36,684][468,780]" />
                <node class="android.widget.TextView" package="com.xingin.xhs" text="已关注" bounds="[210,702][294,762]" />
                <node class="android.widget.TextView" package="com.xingin.xhs" text="发私信" clickable="true" bounds="[492,684][924,780]" />
                <node class="android.view.ViewGroup" package="com.xingin.xhs" content-desc="已选定笔记" bounds="[60,902][156,957]" />
                <node class="android.widget.TextView" package="com.xingin.xhs" text="还没有发布过笔记" bounds="[300,1300][780,1380]" />
              </node>
            </hierarchy>"""
            state["page"] = Page(empty_prof_xml)
            empty_res = interact_profile_first_work(
                device,
                expected_name="空主页用户",
                like_enabled=True,
                collect_enabled=True,
                quota_store=quota,
            )
            self.assertFalse(empty_res["executed"])
            self.assertTrue(empty_res["skipped"])
            self.assertEqual(empty_res["reason"], "empty_profile_works")

    def test_message_center_inspection_reply_and_bounded_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            quota = DailyQuotaStore(tmp, limits={"like": 3, "comment": 3})
            feed_pg = make_feed_page()
            msg_center_xml = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
            <hierarchy>
              <node class="android.widget.FrameLayout" package="com.xingin.xhs" bounds="[0,0][1080,2408]">
                <node class="android.widget.RelativeLayout" package="com.xingin.xhs" content-desc="评论和@按钮" clickable="true" bounds="[704,217][1032,524]" />
                <node class="android.view.ViewGroup" package="com.xingin.xhs" content-desc="首页" clickable="true" bounds="[0,2273][216,2408]" />
                <node class="android.view.ViewGroup" package="com.xingin.xhs" content-desc="消息" clickable="true" bounds="[648,2273][864,2408]" />
              </node>
            </hierarchy>"""
            prompt_xml = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
            <hierarchy>
              <node class="android.widget.FrameLayout" package="com.xingin.xhs" bounds="[0,0][1080,2408]">
                <node class="android.widget.TextView" package="com.xingin.xhs" text="开启互动消息实时通知" bounds="[60,1914][1020,1976]" />
                <node class="android.widget.Button" package="com.xingin.xhs" text="不再提醒" clickable="true" bounds="[48,2240][522,2360]" />
                <node class="android.widget.Button" package="com.xingin.xhs" text="开启通知" clickable="true" bounds="[558,2240][1032,2360]" />
              </node>
            </hierarchy>"""
            comments_at_xml = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
            <hierarchy>
              <node class="android.widget.FrameLayout" package="com.xingin.xhs" bounds="[0,0][1080,2408]">
                <node class="android.widget.ImageView" package="com.xingin.xhs" clickable="true" bounds="[0,84][117,216]" />
                <node class="android.widget.TextView" package="com.xingin.xhs" text="收到的评论和@" bounds="[377,122][702,177]" />
                <node class="android.view.ViewGroup" package="com.xingin.xhs" clickable="true" bounds="[0,240][1080,520]">
                  <node class="android.widget.TextView" package="com.xingin.xhs" text="互动粉丝A" bounds="[180,260][480,310]" />
                  <node class="android.widget.TextView" package="com.xingin.xhs" text="评论了你的笔记 刚刚" bounds="[180,315][520,355]" />
                  <node class="android.widget.TextView" package="com.xingin.xhs" text="求同款链接！" bounds="[180,365][800,420]" />
                  <node class="android.widget.TextView" package="com.xingin.xhs" text="回复" clickable="true" bounds="[180,435][320,495]" />
                  <node class="android.widget.TextView" package="com.xingin.xhs" text="赞" clickable="true" selected="false" bounds="[350,435][490,495]" />
                </node>
                <node class="android.view.ViewGroup" package="com.xingin.xhs" clickable="true" bounds="[0,540][1080,780]">
                  <node class="android.widget.TextView" package="com.xingin.xhs" text="路人B" bounds="[180,560][480,610]" />
                  <node class="android.widget.TextView" package="com.xingin.xhs" text="回复了你的评论 1小时前" bounds="[180,615][520,655]" />
                  <node class="android.widget.TextView" package="com.xingin.xhs" text="该评论已删除" bounds="[180,665][800,720]" />
                </node>
              </node>
            </hierarchy>"""
            msg_center_pg = Page(msg_center_xml)
            prompt_pg = Page(prompt_xml)
            comments_at_pg = Page(comments_at_xml)

            state = {"page": msg_center_pg}
            device = Mock(has_vision=False, stopped=lambda: False)
            device.page.side_effect = lambda: state["page"]
            device.wait.side_effect = lambda fn, **_: fn(state["page"])

            def click(selector):
                node = selector(state["page"])
                if node.get("content-desc") == "评论和@按钮":
                    state["page"] = prompt_pg
                elif node.get("text") == "不再提醒":
                    state["page"] = comments_at_pg
                elif node.get("text") == "赞":
                    node.set("text", "已赞")
                    node.set("selected", "true")
                    state["page"] = Page(ET.tostring(state["page"].root, encoding="unicode"))
                elif node.get("text") == "回复":
                    state["page"] = make_composer_page(
                        text="回复 @互动粉丝A：",
                        hint="回复 @互动粉丝A：",
                        send_enabled=False,
                    )
                elif node.get("text") == "发送":
                    state["page"] = comments_at_pg
                elif node.get("bounds") == "[0,84][117,216]":
                    state["page"] = msg_center_pg
                elif node.get("content-desc") == "首页":
                    state["page"] = feed_pg

            device.click.side_effect = click
            device.set_text.side_effect = lambda text: state.__setitem__(
                "page",
                make_composer_page(text=text, hint="回复 @互动粉丝A：", send_enabled=True),
            )
            device.back.side_effect = lambda: state.__setitem__("page", msg_center_pg)

            res = inspect_and_handle_messages(
                device,
                reply_text="感谢支持",
                like_enabled=True,
                max_messages=5,
                quota_store=quota,
            )
            self.assertEqual(res["status"], "messages_inspected")
            self.assertEqual(res["totalVisible"], 2)
            self.assertTrue(res["processed"][0]["executed"])
            self.assertTrue(res["processed"][0]["liked"])
            self.assertTrue(res["processed"][0]["replied"])
            self.assertFalse(res["processed"][1]["executed"])
            self.assertEqual(res["processed"][1]["reason"], "deleted_or_unavailable")
            self.assertTrue(res["returnedToTask"])

            # Second run must skip already-handled messages
            state["page"] = comments_at_pg
            res2 = inspect_and_handle_messages(
                device,
                reply_text="感谢支持",
                like_enabled=True,
                max_messages=5,
                quota_store=quota,
            )
            self.assertEqual(res2["processed"][0]["reason"], "already_handled")
            self.assertFalse(res2["processed"][0]["executed"])


if __name__ == "__main__":
    unittest.main()
