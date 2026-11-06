import json
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "xiaohongshu" / "agent"))
from comments import video_comment_drawer
from interactions import DailyQuotaStore, ensure_clean_page_on_resume
from modes import (
    apply_search_filters,
    bounded_recover_to_target,
    call_with_captcha_pause,
    find_search_filter_option,
    open_city_feed,
    run_auto_mode,
    run_city_mode,
    run_recommend_mode,
    run_search_mode,
    search_keyword,
    verify_stage_page,
    wait_for_captcha_resolution,
)
from ui import Card, Page, PageMismatch, bounds, verify_card


def _node(
    parent,
    box,
    text="",
    desc="",
    resource="",
    kind="android.widget.TextView",
    pkg="com.xingin.xhs",
    **attrs,
):
    return ET.SubElement(
        parent,
        "node",
        {
            "package": pkg,
            "bounds": f"[{box[0]},{box[1]}][{box[2]},{box[3]}]",
            "text": text,
            "content-desc": desc,
            "class": kind,
            "resource-id": f"{pkg}:id/{resource}" if resource else "",
            **attrs,
        },
    )


def _scale_box(box, scale=1.0, offset=0):
    return tuple(round(v * scale) + offset for v in box)


def make_discover_feed_page(cards_spec=None, scale=1.0, offset=0):
    root = ET.Element("hierarchy")
    _node(
        root,
        _scale_box((234, 102, 438, 198), scale, offset),
        desc="关注",
        kind="androidx.appcompat.app.ActionBar$Tab",
        selected="false",
        clickable="true",
    )
    _node(
        root,
        _scale_box((438, 102, 642, 198), scale, offset),
        desc="发现",
        kind="androidx.appcompat.app.ActionBar$Tab",
        selected="true",
        clickable="true",
    )
    _node(
        root,
        _scale_box((642, 102, 846, 198), scale, offset),
        desc="同城",
        kind="androidx.appcompat.app.ActionBar$Tab",
        selected="false",
        clickable="true",
    )
    _node(
        root,
        _scale_box((945, 96, 1053, 204), scale, offset),
        desc="搜索",
        kind="android.widget.Button",
        clickable="true",
    )
    _node(
        root,
        _scale_box((0, 2273, 216, 2408), scale, offset),
        desc="首页",
        kind="android.view.ViewGroup",
        selected="true",
        clickable="true",
    )
    if cards_spec is None:
        cards_spec = [
            ("笔记", "推荐图文一", "作者A", (15, 240, 533, 1180)),
            ("笔记", "推荐图文二", "作者B", (547, 240, 1065, 1180)),
        ]
    for kind_label, title, author, box in cards_spec:
        sbox = _scale_box(box, scale, offset)
        cnode = _node(
            root,
            sbox,
            desc=f"{kind_label}  {title} 来自{author} 88赞",
            kind="android.widget.FrameLayout",
        )
        cover_bottom = sbox[1] + (sbox[3] - sbox[1]) * 3 // 4
        _node(
            cnode,
            (sbox[0], sbox[1], sbox[2], cover_bottom),
            kind="android.widget.ImageView",
        )
        _node(
            cnode,
            (sbox[0] + 20, cover_bottom + 20, sbox[2] - 20, sbox[3] - 10),
            text=author,
        )
    return Page(ET.tostring(root, encoding="unicode"))


def make_city_feed_page(
    cards_spec=None,
    *,
    location_modal=False,
    location_banner=False,
    city_label="同城",
    include_city_tab=True,
    scale=1.0,
    offset=0,
):
    root = ET.Element("hierarchy")
    _node(
        root,
        _scale_box((438, 102, 642, 198), scale, offset),
        desc="发现",
        kind="androidx.appcompat.app.ActionBar$Tab",
        selected="false",
        clickable="true",
    )
    if include_city_tab:
        tab = _node(
            root,
            _scale_box((642, 102, 846, 198), scale, offset),
            desc=city_label,
            kind="androidx.appcompat.app.ActionBar$Tab",
            selected="true",
            clickable="true",
        )
        _node(
            tab,
            _scale_box((696, 113, 792, 187), scale, offset),
            text=city_label,
            selected="true",
        )
    _node(
        root,
        _scale_box((945, 96, 1053, 204), scale, offset),
        desc="搜索",
        kind="android.widget.Button",
        clickable="true",
    )
    _node(
        root,
        _scale_box((0, 2273, 216, 2408), scale, offset),
        desc="首页",
        kind="android.view.ViewGroup",
        selected="true",
        clickable="true",
    )
    if location_modal:
        _node(root, _scale_box((195, 1260, 885, 1334), scale, offset), text="定位权限未开启")
        _node(
            root,
            _scale_box((135, 1552, 539, 1709), scale, offset),
            text="暂时不了",
            clickable="true",
        )
        _node(
            root,
            _scale_box((541, 1552, 945, 1709), scale, offset),
            text="一键开启",
            clickable="true",
        )
        return Page(ET.tostring(root, encoding="unicode"))
    if location_banner:
        _node(
            root,
            _scale_box((135, 273, 906, 322), scale, offset),
            text="开启定位权限，发现更多本地好内容",
        )
        _node(
            root,
            _scale_box((906, 243, 1080, 352), scale, offset),
            text="开启",
            clickable="true",
        )
    if cards_spec is None:
        cards_spec = [
            ("笔记", "同城探店一", "同城作者A", (15, 380, 533, 1260)),
            ("视频", "同城视频二", "同城作者B", (547, 380, 1065, 1260)),
        ]
    for kind_label, title, author, box in cards_spec:
        sbox = _scale_box(box, scale, offset)
        cnode = _node(
            root,
            sbox,
            desc=f"{kind_label}  {title} 来自{author} 66赞",
            kind="android.widget.FrameLayout",
        )
        cover_bottom = sbox[1] + (sbox[3] - sbox[1]) * 3 // 4
        _node(
            cnode,
            (sbox[0], sbox[1], sbox[2], cover_bottom),
            kind="android.widget.ImageView",
        )
        _node(
            cnode,
            (sbox[0] + 20, cover_bottom + 20, sbox[2] - 20, sbox[3] - 10),
            text=author,
        )
    return Page(ET.tostring(root, encoding="unicode"))


def make_search_input_page(typed_text="搜索,"):
    root = ET.Element("hierarchy")
    _node(root, (48, 129, 114, 195), desc="返回", kind="android.widget.ImageView", clickable="true")
    _node(
        root,
        (180, 96, 748, 228),
        text=typed_text,
        hint="搜索笔记",
        kind="android.widget.EditText",
        clickable="true",
    )
    _node(
        root,
        (894, 96, 984, 228),
        text="搜索",
        kind="android.widget.Button",
        clickable="true",
    )
    _node(root, (48, 279, 930, 345), text="历史记录")
    return Page(ET.tostring(root, encoding="unicode"))


def make_search_results_page(
    keyword="宠物",
    *,
    filter_open=False,
    sort_by="综合",
    note_type="不限",
    publish_time="不限",
    cards_spec=None,
    scale=1.0,
    offset=0,
):
    root = ET.Element("hierarchy")
    _node(
        root,
        _scale_box((0, 84, 114, 240), scale, offset),
        desc="返回",
        kind="android.widget.ImageView",
        clickable="true",
    )
    _node(
        root,
        _scale_box((180, 96, 760, 228), scale, offset),
        text=keyword,
        clickable="true",
    )
    _node(
        root,
        _scale_box((769, 96, 841, 228), scale, offset),
        desc="全部删除",
        kind="android.widget.ImageView",
        clickable="true",
    )
    _node(
        root,
        _scale_box((864, 96, 1032, 228), scale, offset),
        text="搜索",
        clickable="true",
    )
    _node(
        root,
        _scale_box((48, 269, 144, 342), scale, offset),
        text="全部",
        selected="true",
        clickable="true",
    )
    _node(root, _scale_box((72, 407, 156, 468), scale, offset), text="综合", selected="true")
    _node(root, _scale_box((228, 407, 312, 468), scale, offset), text="最新", selected="false")

    rv = _node(
        root,
        _scale_box((0, 84, 1080, 2408), scale, offset),
        kind="androidx.recyclerview.widget.RecyclerView",
    )
    _node(rv, _scale_box((0, 84, 1080, 372), scale, offset), kind="android.widget.FrameLayout")
    _node(rv, _scale_box((0, 372, 1080, 504), scale, offset), kind="android.widget.FrameLayout")

    if cards_spec is None:
        if note_type == "视频":
            cards_spec = [
                ("VIDEO", "宠物视频教程 #萌宠", "视频博主A", (15, 519, 533, 1502)),
                ("VIDEO", "小狗日常训练 #训犬", "视频博主B", (547, 519, 1065, 1502)),
            ]
        elif note_type == "图文":
            cards_spec = [
                ("IMAGE_TEXT", "宠物原创小饼干 #狗狗配餐", "Rainbow宠物烘焙", (15, 519, 533, 1502)),
                ("IMAGE_TEXT", "猫咪日常护理指南 #养猫", "喵星人日记", (547, 519, 1065, 1502)),
            ]
        else:
            cards_spec = [
                ("IMAGE_TEXT", "宠物原创小饼干 #狗狗配餐", "Rainbow宠物烘焙", (15, 519, 533, 1502)),
                ("VIDEO", "宠物视频教程 #萌宠", "视频博主A", (547, 519, 1065, 1502)),
            ]

    for ctype, title, author, box in cards_spec:
        sbox = _scale_box(box, scale, offset)
        item = _node(rv, sbox, kind="android.widget.FrameLayout", clickable="true")
        cover_bottom = sbox[1] + (sbox[3] - sbox[1]) * 68 // 100
        _node(
            item,
            (sbox[0], sbox[1], sbox[2], cover_bottom),
            kind="android.widget.ImageView",
        )
        if ctype == "VIDEO":
            _node(
                item,
                (sbox[2] - round(80 * scale), cover_bottom - round(60 * scale), sbox[2] - round(20 * scale), cover_bottom - round(15 * scale)),
                text="0:18",
            )
        title_bottom = cover_bottom + round(140 * scale)
        _node(
            item,
            (sbox[0] + round(20 * scale), cover_bottom + round(16 * scale), sbox[2] - round(20 * scale), title_bottom),
            text=title,
        )
        avatar_top = title_bottom + round(16 * scale)
        avatar_bottom = min(sbox[3] - round(12 * scale), avatar_top + round(64 * scale))
        avatar_right = sbox[0] + round(90 * scale)
        _node(
            item,
            (sbox[0] + round(16 * scale), avatar_top, avatar_right, avatar_bottom),
            kind="android.widget.ImageView",
        )
        _node(
            item,
            (avatar_right + round(12 * scale), avatar_top - round(6 * scale), sbox[0] + round(320 * scale), avatar_bottom - round(18 * scale)),
            text=author,
        )
        _node(
            item,
            (avatar_right + round(12 * scale), avatar_bottom - round(18 * scale), avatar_right + round(110 * scale), avatar_bottom),
            text="6分钟前",
        )
        _node(
            item,
            (sbox[2] - round(90 * scale), avatar_top, sbox[2] - round(20 * scale), avatar_bottom),
            text="12",
        )

    # Also add a clipped bottom card touching rv_bottom (2408) to verify it gets excluded.
    clipped_box = _scale_box((15, 1517, 533, 2408), scale, offset)
    clipped_item = _node(rv, clipped_box, kind="android.widget.FrameLayout", clickable="true")
    _node(
        clipped_item,
        _scale_box((15, 1517, 533, 2207), scale, offset),
        kind="android.widget.ImageView",
    )
    _node(
        clipped_item,
        _scale_box((39, 2231, 509, 2363), scale, offset),
        text="底部截断卡片不应被选中",
    )

    if filter_open:
        _node(root, _scale_box((48, 408, 216, 457), scale, offset), text="排序依据")
        for idx, opt in enumerate(("综合", "最新", "最多点赞", "最多评论")):
            x0 = 60 + idx * 240
            _node(
                root,
                _scale_box((x0, 493, x0 + 180, 589), scale, offset),
                text=opt,
                selected="true" if opt == sort_by else "false",
                clickable="true",
            )
        _node(root, _scale_box((48, 775, 216, 824), scale, offset), text="笔记类型")
        for idx, opt in enumerate(("不限", "视频", "图文")):
            x0 = 60 + idx * 250
            _node(
                root,
                _scale_box((x0, 860, x0 + 160, 956), scale, offset),
                text=opt,
                selected="true" if opt == note_type else "false",
                clickable="true",
            )
        _node(root, _scale_box((48, 1010, 216, 1059), scale, offset), text="发布时间")
        for idx, opt in enumerate(("不限", "一天内", "一周内", "半年内")):
            x0 = 60 + idx * 240
            _node(
                root,
                _scale_box((x0, 1095, x0 + 180, 1191), scale, offset),
                text=opt,
                selected="true" if opt == publish_time else "false",
                clickable="true",
            )
        _node(root, _scale_box((48, 1245, 216, 1294), scale, offset), text="搜索范围")
        _node(root, _scale_box((111, 1330, 213, 1426), scale, offset), text="不限", selected="true")
        _node(root, _scale_box((48, 1480, 216, 1529), scale, offset), text="位置距离")
        _node(root, _scale_box((111, 1565, 213, 1661), scale, offset), text="不限", selected="true")
        _node(root, _scale_box((251, 1762, 335, 1811), scale, offset), text="重置", clickable="true")
        _node(root, _scale_box((792, 1762, 876, 1811), scale, offset), text="收起", clickable="true")

    return Page(ET.tostring(root, encoding="unicode"))


def make_note_detail_page(author, title, body="正文内容", note_type="IMAGE_TEXT"):
    root = ET.Element("hierarchy")
    if note_type == "VIDEO":
        _node(root, (36, 1750, 320, 1810), text=author, resource="matrixNickNameView")
        _node(root, (36, 1740, 330, 1820), desc=f"作者{author}")
        _node(root, (960, 100, 1040, 180), desc="分享")
        _node(root, (36, 1830, 900, 1960), text=f"{title}\n{body}", resource="noteContentText")
        return Page(ET.tostring(root, encoding="unicode"))
    _node(root, (180, 110, 420, 170), text=author, resource="nickNameTV")
    _node(root, (960, 110, 1040, 170), resource="moreOperateIV", kind="android.widget.ImageView")
    _node(root, (0, 200, 1080, 900), desc="图片,第1张,共1张")
    group = _node(root, (0, 920, 1080, 1260), kind="android.widget.LinearLayout")
    _node(group, (36, 930, 1040, 1010), text=title)
    _node(group, (36, 1020, 1040, 1220), text=body)
    _node(root, (36, 2240, 440, 2360), desc="评论框")
    return Page(ET.tostring(root, encoding="unicode"))


class FakeModeDevice:
    def __init__(self, initial_page=None):
        self.current_page = initial_page or make_discover_feed_page()
        self.has_vision = False
        self.commands = []
        self.clicks = []
        self.backs = 0
        self.typed_texts = []
        self._stopped = False
        self.on_tap = None
        self.on_back = None
        self.on_set_text = None
        self.page_hook = None

    def stopped(self):
        return self._stopped

    def page(self, reuse_if_stable=False):
        if callable(self.page_hook):
            res = self.page_hook()
            if res is not None:
                self.current_page = res
        return self.current_page

    def wait(self, parse, timeout=10):
        return parse(self.page())

    def click(self, selector):
        node = selector(self.page())
        box = bounds(node)
        x, y = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
        self.clicks.append((x, y, node.get("text", ""), node.get("content-desc", "")))
        if callable(self.on_tap):
            self.on_tap(x, y, node)

    def command(self, *args):
        self.commands.append(args)
        if args[:2] == ("input", "tap") and callable(self.on_tap):
            self.on_tap(int(args[2]), int(args[3]), None)
        return ""

    def back(self):
        self.backs += 1
        if callable(self.on_back):
            self.on_back()

    def set_text(self, text):
        self.typed_texts.append(text)
        if callable(self.on_set_text):
            self.on_set_text(text)


class ModeTests(unittest.TestCase):
    def test_recommend_mode_continuous_selection_scrolls_and_skips_seen(self):
        page1 = make_discover_feed_page(
            [
                ("笔记", "已看过的笔记", "作者Old", (15, 240, 533, 1180)),
                ("笔记", "第一篇新笔记", "作者One", (547, 240, 1065, 1180)),
            ]
        )
        page2 = make_discover_feed_page(
            [
                ("笔记", "第一篇新笔记", "作者One", (547, 240, 1065, 1180)),
                ("笔记", "滑动后第二篇", "作者Two", (15, 240, 533, 1180)),
            ]
        )
        old_key = page1.cards()[0].key
        dev = FakeModeDevice(page1)
        active_feed = {"page": page1}

        def handle_tap(x, y, node):
            if active_feed["page"] is page1 and x > 540:
                dev.current_page = make_note_detail_page("作者One", "第一篇新笔记")
            elif active_feed["page"] is page2 and x < 540:
                dev.current_page = make_note_detail_page("作者Two", "滑动后第二篇")

        def handle_back():
            dev.current_page = active_feed["page"]

        orig_command = dev.command

        def command(*args):
            if args[:2] == ("input", "swipe"):
                active_feed["page"] = page2
                dev.current_page = page2
            return orig_command(*args)

        dev.on_tap = handle_tap
        dev.on_back = handle_back
        dev.command = command

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "seen.json").write_text(json.dumps([old_key]))
            report = run_recommend_mode(dev, tmp, count=2, max_screens=4)
            self.assertEqual(report["status"], "recommend_mode_completed")
            self.assertEqual(report["processedCount"], 2)
            self.assertEqual(
                [n["cardAuthor"] for n in report["notes"]],
                ["作者One", "作者Two"],
            )
            saved_seen = set(json.loads((Path(tmp) / "seen.json").read_text()))
            self.assertEqual(len(saved_seen), 3)

    def test_search_keyword_and_sort_selection_verifies_panel_and_results(self):
        dev = FakeModeDevice(make_discover_feed_page())
        state = {"sort": "综合", "type": "不限", "time": "不限", "open": False, "kw": "宠物"}

        def rebuild():
            return make_search_results_page(
                state["kw"],
                filter_open=state["open"],
                sort_by=state["sort"],
                note_type=state["type"],
                publish_time=state["time"],
            )

        def on_tap(x, y, node):
            desc = node.get("content-desc", "") if node is not None else ""
            text = node.get("text", "") if node is not None else ""
            if desc == "搜索":
                dev.current_page = make_search_input_page()
            elif text == "搜索":
                dev.current_page = rebuild()
            elif text == "全部":
                state["open"] = True
                dev.current_page = rebuild()
            elif text in ("综合", "最新", "最多点赞", "最多评论") and state["open"]:
                state["sort"] = text
                dev.current_page = rebuild()
            elif text in ("不限", "视频", "图文") and state["open"] and 820 <= y <= 980:
                state["type"] = text
                dev.current_page = rebuild()
            elif text in ("不限", "一天内", "一周内", "半年内") and state["open"] and 1060 <= y <= 1220:
                state["time"] = text
                dev.current_page = rebuild()
            elif text == "收起":
                state["open"] = False
                dev.current_page = rebuild()

        def on_set_text(text):
            state["kw"] = text
            dev.current_page = make_search_input_page(typed_text=text)

        dev.on_tap = on_tap
        dev.on_set_text = on_set_text

        s_res = search_keyword(dev, "宠物")
        self.assertEqual(s_res["status"], "search_results_ready")
        self.assertEqual(s_res["keyword"], "宠物")

        for sort_opt in ("最新", "最多评论", "最多点赞", "综合"):
            f_res = apply_search_filters(
                dev,
                sort_by=sort_opt,
                note_type="图文",
                publish_time="一周内",
                expected_keyword="宠物",
            )
            self.assertEqual(f_res["sortBy"], sort_opt)
            self.assertEqual(f_res["noteType"], "图文")
            self.assertEqual(f_res["publishTime"], "一周内")
            self.assertTrue(f_res["verifiedInPanel"])
            self.assertTrue(f_res["panelClosed"])
            self.assertEqual(f_res["visibleCardTypes"], ["IMAGE_TEXT"])

    def test_search_filter_note_type_and_publish_time_validates_result_types(self):
        # When "视频" is selected, all visible cards must be VIDEO; if an IMAGE_TEXT card leaks in, raise PageMismatch.
        bad_video_page = make_search_results_page(
            "宠物",
            filter_open=False,
            sort_by="最新",
            note_type="视频",
            publish_time="一天内",
            cards_spec=[
                ("IMAGE_TEXT", "混入的图文卡片", "作者X", (15, 519, 533, 1502)),
            ],
        )
        dev = FakeModeDevice(
            make_search_results_page(
                "宠物",
                filter_open=True,
                sort_by="最新",
                note_type="视频",
                publish_time="一天内",
            )
        )
        dev.on_tap = lambda x, y, node: setattr(dev, "current_page", bad_video_page)
        with self.assertRaisesRegex(PageMismatch, "Video filter applied"):
            apply_search_filters(
                dev,
                sort_by="最新",
                note_type="视频",
                publish_time="一天内",
                expected_keyword="宠物",
            )

    def test_city_mode_handles_missing_entry_location_permission_modal_and_continuous_processing(self):
        # Case 1: City tab missing entirely
        root = ET.Element("hierarchy")
        _node(root, (438, 102, 642, 198), desc="发现", selected="true")
        _node(root, (0, 2273, 216, 2408), desc="首页", selected="true")
        dev_missing = FakeModeDevice(Page(ET.tostring(root, encoding="unicode")))
        with tempfile.TemporaryDirectory() as tmp:
            res_missing = run_city_mode(dev_missing, tmp, count=1)
            self.assertEqual(res_missing["status"], "entry_unavailable")
            self.assertFalse(res_missing["available"])

        # Case 2: Location permission modal pops up, require_location_permission=True -> cleanly reports unavailable
        dev_perm = FakeModeDevice(make_discover_feed_page())

        def tap_perm(x, y, node):
            desc = node.get("content-desc", "") if node is not None else ""
            if desc == "同城":
                dev_perm.current_page = make_city_feed_page(location_modal=True)
            elif 135 <= x <= 539 and 1552 <= y <= 1709:
                dev_perm.current_page = make_city_feed_page(location_banner=True)

        dev_perm.on_tap = tap_perm
        with tempfile.TemporaryDirectory() as tmp:
            res_perm = run_city_mode(
                dev_perm,
                tmp,
                count=1,
                require_location_permission=True,
            )
            self.assertEqual(res_perm["status"], "location_permission_unavailable")
            self.assertFalse(res_perm["available"])
            self.assertTrue(res_perm["cityState"]["locationPermissionPrompted"])

        # Case 3: City feed ready -> selects and processes work and returns to city feed
        city_feed = make_city_feed_page(location_banner=True)
        dev_ok = FakeModeDevice(city_feed)

        def tap_ok(x, y, node):
            if y > 350 and x < 540:
                dev_ok.current_page = make_note_detail_page("同城作者A", "同城探店一")

        dev_ok.on_tap = tap_ok
        dev_ok.on_back = lambda: setattr(dev_ok, "current_page", city_feed)
        with tempfile.TemporaryDirectory() as tmp:
            res_ok = run_city_mode(dev_ok, tmp, count=1, require_location_permission=False)
            self.assertEqual(res_ok["status"], "city_mode_completed")
            self.assertTrue(res_ok["available"])
            self.assertEqual(res_ok["processedCount"], 1)
            self.assertEqual(res_ok["notes"][0]["cardAuthor"], "同城作者A")

    def test_auto_mode_switches_recommend_search_city_and_verifies_stage_pages(self):
        discover_page = make_discover_feed_page()
        search_page = make_search_results_page(
            "宠物",
            filter_open=False,
            sort_by="最新",
            note_type="图文",
            publish_time="一周内",
        )
        city_page = make_city_feed_page()
        dev = FakeModeDevice(discover_page)
        return_page = {"page": discover_page}

        def on_tap(x, y, node):
            desc = node.get("content-desc", "") if node is not None else ""
            text = node.get("text", "") if node is not None else ""
            if desc == "搜索":
                dev.current_page = make_search_input_page()
            elif text == "搜索":
                return_page["page"] = search_page
                dev.current_page = search_page
            elif text == "全部":
                dev.current_page = make_search_results_page(
                    "宠物",
                    filter_open=True,
                    sort_by="最新",
                    note_type="图文",
                    publish_time="一周内",
                )
            elif text == "收起":
                dev.current_page = search_page
            elif desc == "返回" or (x < 120 and y < 240):
                return_page["page"] = discover_page
                dev.current_page = discover_page
            elif desc == "发现" or (438 <= x <= 642 and 102 <= y <= 198):
                return_page["page"] = discover_page
                dev.current_page = discover_page
            elif desc == "同城" or (642 <= x <= 846 and 102 <= y <= 198):
                return_page["page"] = city_page
                dev.current_page = city_page
            elif return_page["page"] is discover_page and y > 220:
                dev.current_page = make_note_detail_page("作者A", "推荐图文一")
            elif return_page["page"] is search_page and y > 500:
                dev.current_page = make_note_detail_page(
                    "Rainbow宠物烘焙",
                    "宠物原创小饼干",
                    body="#狗狗配餐",
                )
            elif return_page["page"] is city_page and y > 350:
                dev.current_page = make_note_detail_page("同城作者A", "同城探店一")

        dev.on_tap = on_tap
        dev.on_back = lambda: setattr(dev, "current_page", return_page["page"])
        dev.on_set_text = lambda text: setattr(
            dev, "current_page", make_search_input_page(typed_text=text)
        )

        with tempfile.TemporaryDirectory() as tmp:
            report = run_auto_mode(
                dev,
                tmp,
                stages=[
                    {"stage": "recommend", "count": 1},
                    {
                        "stage": "search",
                        "keyword": "宠物",
                        "sortBy": "最新",
                        "noteType": "图文",
                        "publishTime": "一周内",
                        "count": 1,
                    },
                    {"stage": "city", "count": 1},
                ],
            )
            self.assertEqual(report["status"], "auto_mode_completed")
            self.assertEqual(report["stagesCompleted"], 3)
            self.assertEqual(
                [s["mode"] for s in report["stages"]],
                ["recommend", "search", "city"],
            )
            self.assertTrue(all(s["stageSwitchVerified"] for s in report["stages"]))

    def test_captcha_pause_waits_without_clicking_and_resumes_after_manual_resolution(self):
        discover_page = make_discover_feed_page()
        dev = FakeModeDevice(discover_page)
        polls = {"count": 0}

        def page_hook():
            polls["count"] += 1
            if polls["count"] <= 2:
                root = ET.Element("hierarchy")
                _node(root, (100, 800, 980, 1200), text="请拖动滑块完成验证")
                return Page(ET.tostring(root, encoding="unicode"))
            return discover_page

        dev.page_hook = page_hook
        paused = {"called": False}
        result = call_with_captcha_pause(
            dev,
            lambda: dev.page().cards(),
            max_wait_seconds=5.0,
            on_pause=lambda: paused.__setitem__("called", True),
        )
        self.assertTrue(paused["called"])
        self.assertEqual(len(result), 2)
        self.assertEqual(dev.clicks, [])
        self.assertEqual(dev.backs, 0)

        # If verification never clears within timeout, raise PageMismatch
        dev_stuck = FakeModeDevice(discover_page)
        dev_stuck.page_hook = lambda: Page(
            '<hierarchy><node package="com.xingin.xhs" bounds="[0,0][100,100]" text="安全验证"/></hierarchy>'
        )
        with self.assertRaisesRegex(PageMismatch, "Verification timeout"):
            wait_for_captcha_resolution(
                dev_stuck,
                poll_interval=0.05,
                max_wait_seconds=0.15,
            )

    def test_bounded_recovery_handles_loading_failure_popups_unexpected_pages_and_stops_on_limit(self):
        # 1) Popup -> Loading failure with retry -> Store tab -> Discover feed within 3 steps
        popup_root = ET.Element("hierarchy")
        _node(popup_root, (100, 800, 900, 900), text="青少年模式")
        _node(popup_root, (300, 1200, 780, 1320), text="我知道了", clickable="true")
        popup_page = Page(ET.tostring(popup_root, encoding="unicode"))

        load_err_root = ET.Element("hierarchy")
        _node(load_err_root, (200, 900, 880, 980), text="网络好像断开了")
        _node(load_err_root, (380, 1050, 700, 1150), text="点击重试", clickable="true")
        load_err_page = Page(ET.tostring(load_err_root, encoding="unicode"))

        store_root = ET.Element("hierarchy")
        _node(store_root, (0, 2273, 216, 2408), desc="首页", selected="false", clickable="true")
        _node(store_root, (216, 2273, 432, 2408), desc="市集", selected="true", clickable="true")
        store_page = Page(ET.tostring(store_root, encoding="unicode"))

        discover_page = make_discover_feed_page()
        seq = [popup_page, load_err_page, store_page, discover_page]
        dev = FakeModeDevice(seq[0])
        step_idx = {"i": 0}

        def advance(*_args):
            step_idx["i"] = min(step_idx["i"] + 1, len(seq) - 1)
            dev.current_page = seq[step_idx["i"]]

        dev.on_tap = advance
        dev.on_back = advance
        rec = bounded_recover_to_target(dev, target="recommend", max_steps=4)
        self.assertTrue(rec["recovered"])
        self.assertEqual(rec["pageType"], "recommend_feed")
        self.assertEqual(rec["stepsUsed"], 3)

        # 2) Unrecoverable unknown page stops at max_steps and reports reason
        stuck_root = ET.Element("hierarchy")
        _node(stuck_root, (100, 500, 900, 600), text="笔记不存在或已删除")
        stuck_page = Page(ET.tostring(stuck_root, encoding="unicode"))
        dev_stuck = FakeModeDevice(stuck_page)
        with self.assertRaisesRegex(PageMismatch, "Bounded recovery to recommend failed after 3 steps: reason=笔记不存在或已删除"):
            bounded_recover_to_target(dev_stuck, target="recommend", max_steps=3)
        self.assertEqual(dev_stuck.backs, 3)

    def test_multi_resolution_search_and_city_cards_and_multi_work_commenter_pipeline(self):
        for scale, offset in [(0.67, 0), (1.0, 0), (1.33, 12)]:
            s_page = make_search_results_page(
                "宠物",
                filter_open=False,
                sort_by="最新",
                note_type="不限",
                publish_time="一周内",
                scale=scale,
                offset=offset,
            )
            s_cards = s_page.search_cards(expected_keyword="宠物")
            self.assertEqual(len(s_cards), 2)
            self.assertEqual([c.note_type for c in s_cards], ["IMAGE_TEXT", "VIDEO"])

            f_page = make_search_results_page(
                "宠物",
                filter_open=True,
                sort_by="最多评论",
                note_type="图文",
                publish_time="半年内",
                scale=scale,
                offset=offset,
            )
            self.assertEqual(
                find_search_filter_option(f_page, "排序依据", "最多评论").get("selected"),
                "true",
            )
            self.assertEqual(
                find_search_filter_option(f_page, "笔记类型", "图文").get("selected"),
                "true",
            )
            self.assertEqual(
                find_search_filter_option(f_page, "发布时间", "半年内").get("selected"),
                "true",
            )

            c_page = make_city_feed_page(
                city_label="杭州",
                scale=scale,
                offset=offset,
            )
            c_cards = c_page.city_cards()
            self.assertEqual(len(c_cards), 2)
            self.assertEqual([c.author for c in c_cards], ["同城作者A", "同城作者B"])

        # Verify 2-line truncated search card preview matching (title prefix + description first line)
        search_card = Card(
            "IMAGE_TEXT",
            "𝐒𝐞𝐜𝐫𝐞𝐭 两年里拍的一些横构图花花 我会永远喜欢花花咩哈哈哈",
            "Lyspunkt💫",
            (15, 519, 533, 1330),
            (15, 519, 533, 1037),
        )
        verify_card(
            search_card,
            {
                "noteType": "IMAGE_TEXT",
                "authorNickname": "Lyspunkt💫",
                "title": "𝐒𝐞𝐜𝐫𝐞𝐭 𝐆𝐚𝐫𝐝𝐞𝐧",
                "description": "两年里拍的一些横构图花花\n我会永远喜欢花花咩哈哈哈",
            },
        )

        # Verify video comment drawer ignores bottom quick-emoji RecyclerView strip
        drawer_root = ET.Element("hierarchy")
        _node(drawer_root, (15, 723, 337, 855), text="共 4 条评论")
        _node(
            drawer_root,
            (972, 759, 1032, 819),
            kind="android.widget.ImageView",
            clickable="true",
        )
        main_rv = _node(
            drawer_root,
            (0, 855, 1080, 2264),
            kind="androidx.recyclerview.widget.RecyclerView",
        )
        first_item = _node(main_rv, (0, 855, 1080, 1700), kind="android.view.ViewGroup")
        _node(first_item, (183, 891, 519, 952), text="彩虹家德文卷毛猫")
        _node(
            first_item,
            (183, 958, 1035, 1158),
            text="极品长毛甜美德文弟弟\n#德文开脸 #杭州德文",
        )
        emoji_rv = _node(
            drawer_root,
            (0, 2084, 1080, 2240),
            kind="androidx.recyclerview.widget.RecyclerView",
        )
        _node(emoji_rv, (36, 2084, 168, 2216), text="[笑哭R] ")
        _node(drawer_root, (90, 2294, 786, 2354), text="有话要说，快来评论")
        drawer_page = Page(ET.tostring(drawer_root, encoding="unicode"))
        expanded = drawer_page.expanded_video_note("彩虹家德文卷毛猫")
        self.assertEqual(expanded["title"], "极品长毛甜美德文弟弟")
        _, _, listing = video_comment_drawer(drawer_page)
        self.assertEqual(listing.get("bounds"), "[0,855][1080,2264]")

        # Verify ensure_clean_page_on_resume clicks '暂时不了' on location permission modal
        modal_page = make_city_feed_page(location_modal=True)
        city_ready_page = make_city_feed_page(location_modal=False, location_banner=True)
        resume_dev = FakeModeDevice(modal_page)
        resume_dev.on_tap = lambda *_args: setattr(resume_dev, "current_page", city_ready_page)
        resumed_state = ensure_clean_page_on_resume(resume_dev)
        self.assertEqual(resumed_state["pageType"], "city_feed")
        self.assertEqual(len(resume_dev.clicks), 1)


if __name__ == "__main__":
    unittest.main()
