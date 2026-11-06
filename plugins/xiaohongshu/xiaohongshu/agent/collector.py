"""One bounded, read-only collection transaction using fresh semantic controls."""

import json
from pathlib import Path
import tempfile
import time
import uuid

from commands import run
from discovery import note_link
from ui import Page, PageMismatch, bounds, center, merge_note_paragraphs, verify_card, verify_return
from vision import capture_raw_frame, ocr_author_identity, wait_frame_stable


class Device:
    def __init__(self, stopped=lambda: False, screencap=None, recognizer=None,
                 stable_threshold=2.0, settle_timeout=1.5):
        self.stopped = stopped
        if screencap is None and callable(capture_raw_frame):
            self.screencap = None
        else:
            self.screencap = screencap
        self.recognizer = recognizer
        self.has_vision = callable(self.screencap)
        self.stable_threshold = float(stable_threshold)
        self.settle_timeout = float(settle_timeout)
        self._last_page = None
        self._last_frame = None
        self._pre_action_frame = None
        self._transition_pending = False
        self._swipe_pending = False

    def capture_frame(self):
        if not self.has_vision:
            return None
        try:
            return self.screencap()
        except Exception:
            return None

    def verify_roi_stable(self, roi=None, baseline=None, timeout=0.45):
        if not self.has_vision:
            return False
        ref = self._last_frame if baseline is None else baseline
        try:
            result = wait_frame_stable(
                self.screencap,
                roi=roi,
                baseline=ref,
                threshold=self.stable_threshold,
                timeout=timeout,
                interval=0.04,
                stopped=self.stopped,
            )
        except InterruptedError:
            raise
        except ValueError:
            if roi is None:
                return False
            return self.verify_roi_stable(roi=None, baseline=baseline, timeout=timeout)
        except Exception:
            return False
        if result["frame"] is not None:
            self._last_frame = result["frame"]
        expected_samples = 2 if ref is None else 1
        return bool(result["stable"] and result["samples"] <= expected_samples)

    def wait_visual_settle(self, roi=None, timeout=None):
        if not self.has_vision:
            return False
        try:
            result = wait_frame_stable(
                self.screencap,
                roi=roi,
                threshold=self.stable_threshold,
                timeout=self.settle_timeout if timeout is None else timeout,
                interval=0.04,
                stopped=self.stopped,
            )
        except InterruptedError:
            raise
        except Exception:
            return False
        if result["frame"] is not None:
            self._last_frame = result["frame"]
        return bool(result["stable"])

    def command(self, *args):
        is_uidump = (
            len(args) >= 5
            and args[0] == "env"
            and any("xhs.UiDump" in str(a) for a in args)
            and "--resume-xhs" not in args
            and "--set-text" not in args
        )
        if not is_uidump:
            if self.has_vision and args and args[0] in ("input", "am"):
                self._pre_action_frame = self._last_frame
                self._transition_pending = True
                self._swipe_pending = len(args) >= 2 and args[:2] == ("input", "swipe")
            self._last_page = None
            self._last_frame = None
        print(json.dumps({"deviceCommand": args[0], "phase": "start"}), flush=True)
        if self.stopped():
            raise InterruptedError("Task stopped")
        result = run(args, timeout=15, stopped=self.stopped)
        if self.stopped():
            raise InterruptedError("Task stopped")
        print(json.dumps({"deviceCommand": args[0], "phase": "done"}), flush=True)
        return result

    def page(self, reuse_if_stable=False):
        if reuse_if_stable and self._last_page is not None and not self._transition_pending:
            return self._last_page
        if self._swipe_pending and self.has_vision:
            self._swipe_pending = False
            self.wait_visual_settle(timeout=self.settle_timeout)
        self._transition_pending = False
        self._pre_action_frame = None
        # A unique dump file prevents an unsuccessful dump from reusing old screen contents.
        with tempfile.TemporaryDirectory(prefix="xhs-ui-") as directory:
            target = Path(directory) / "window.xml"
            helper = Path(__file__).with_name("ui-dump.dex")
            if not helper.is_file():
                raise RuntimeError("UI hierarchy helper is missing; reinstall plugin resources")
            self.command("env", "CLASSPATH=" + str(helper), "app_process", "/system/bin", "xhs.UiDump", str(target))
            if not target.is_file():
                raise PageMismatch("UI hierarchy was unavailable during transition")
            parsed = Page(target.read_text())
            self._last_page = parsed
            return parsed

    def wait(self, parse, timeout=20):
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            try:
                return parse(self.page())
            except PageMismatch as error:
                if "Verification detected" in str(error):
                    raise
                last_error = error
            time.sleep(0.25)
        raise PageMismatch(f"Page did not become ready: {last_error}")

    def click(self, selector):
        if self.has_vision:
            page = self.page(reuse_if_stable=True)
            node = selector(page)
            box = bounds(node)
            if self.verify_roi_stable(roi=box):
                x, y = center(node)
                print(json.dumps({"clickBounds": box}), flush=True)
                self.command("input", "tap", str(x), str(y))
                return
            previous = box
            for _ in range(3):
                node = selector(self.page())
                box = bounds(node)
                if box == previous:
                    x, y = center(node)
                    print(json.dumps({"clickBounds": box}), flush=True)
                    self.command("input", "tap", str(x), str(y))
                    return
                previous = box
            raise PageMismatch("Control bounds did not stabilize before click")
        previous = None
        for _ in range(4):
            node = selector(self.page())
            box = bounds(node)
            if box == previous:
                x, y = center(node)
                print(json.dumps({"clickBounds": box}), flush=True)
                self.command("input", "tap", str(x), str(y))
                return
            previous = box
        raise PageMismatch("Control bounds did not stabilize before click")

    def back(self):
        self.command("input", "keyevent", "4")

    def set_text(self, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Interaction text must be a non-empty string")
        helper = Path(__file__).with_name("ui-dump.dex")
        if not helper.is_file():
            raise RuntimeError("UI hierarchy helper is missing; reinstall plugin resources")
        output = self.command(
            "env",
            "CLASSPATH=" + str(helper),
            "app_process",
            "/system/bin",
            "xhs.UiDump",
            "--set-text",
            text,
        )
        if "text_set" not in (output or ""):
            raise PageMismatch("Could not set text in active EditText")


def open_discover(device):
    device.command("am", "start", "--display", "0", "-n", "com.xingin.xhs/.index.v2.IndexActivityV2")
    for _ in range(4):
        page = device.wait(lambda current: current)
        try:
            page.cards()
            return
        except PageMismatch:
            pass
        if any(n.get("content-desc") == "首页" for n in page.nodes):
            if any(n.get("content-desc") == "发现" for n in page.nodes):
                device.click(lambda current: current.unique(description="发现"))
            else:
                device.click(lambda current: current.unique(description="首页"))
        else:
            if (any(n.get("content-desc") == "复制链接" for n in page.nodes)
                    and any(n.get("text") == "分享至" for n in page.nodes)):
                device.back()
                continue
            if isinstance(page, Page):
                try:
                    page.video_drawer_close_button()
                    device.back()
                    continue
                except PageMismatch:
                    pass
            try:
                if any(n.get("resource-id", "").endswith("/matrixNickNameView") for n in page.nodes):
                    page.unique(description="分享")
                    page.unique(description="返回")
                else:
                    page.note()
            except PageMismatch:
                if not any(n.get("text", "").startswith("小红书号：") for n in page.nodes):
                    raise PageMismatch("Unknown screen; refusing an unverified back action")
            device.back()
        time.sleep(0.4)
    raise PageMismatch("Could not restore Discover within four transitions")


def scroll_feed(device, cards):
    if not cards:
        raise PageMismatch("Cannot scroll an empty feed")
    left = min(c.box[0] for c in cards)
    top = min(c.box[1] for c in cards)
    right = max(c.box[2] for c in cards)
    bottom = max(c.box[3] for c in cards)
    x = str((left + right) // 2)
    device.command("input", "swipe", x, str(top + (bottom - top) * 4 // 5),
                   x, str(top + (bottom - top) // 5), "450")
    time.sleep(0.4)


def _current_page(device):
    if getattr(device, "has_vision", False) is True:
        return device.page(reuse_if_stable=True)
    return device.page()


def select_feed_card(device, seen, note_type=None, max_screens=None):
    if note_type not in (None, "VIDEO", "IMAGE_TEXT"):
        raise ValueError("Unsupported note type")
    screens = (1 if note_type is None else 6) if max_screens is None else max_screens
    if type(screens) is not int or not 1 <= screens <= 50:
        raise ValueError("Screen search limit must be between 1 and 50")
    for screen in range(screens):
        if (screen == 0 and getattr(device, "has_vision", False) is True
                and getattr(device, "_last_page", None) is not None
                and not getattr(device, "_transition_pending", False)):
            try:
                cards = device._last_page.cards()
            except PageMismatch:
                cards = device.wait(lambda page: page.cards())
        else:
            cards = device.wait(lambda page: page.cards())
        card = next((item for item in cards if item.key not in seen
                     and (note_type is None or item.note_type == note_type)), None)
        if card is not None:
            if getattr(device, "has_vision", False) is True and device.verify_roi_stable(card.box):
                return cards, card
            current = device.page().cards()
            if screen > 0 and card not in current:
                for _ in range(3):
                    time.sleep(0.2)
                    settled = device.page().cards()
                    if settled == current:
                        cards = settled
                        card = next((item for item in cards if item.key not in seen
                                     and (note_type is None or item.note_type == note_type)), None)
                        break
                    current = settled
            if card is None or card not in current:
                raise PageMismatch("Feed changed before click; no action performed")
            return cards, card
        if not cards or screen + 1 == screens:
            raise PageMismatch("No unprocessed note of the requested type within the search limit")
        scroll_feed(device, cards)
    raise PageMismatch("No unprocessed note of the requested type within the search limit")


def swipe_note_content(device, page):
    _, top_bar, bottom_bar = page.image_note_viewport()
    nick_box = bounds(page.unique(resource="nickNameTV"))
    share_box = bounds(page.unique(resource="moreOperateIV"))
    x = str((nick_box[0] + share_box[2]) // 2)
    height = bottom_bar - top_bar
    device.command("input", "swipe", x, str(top_bar + height * 3 // 4),
                   x, str(top_bar + height // 3), "600")


def settled_image_note(device, expected_author):
    previous = None

    def ready(page):
        nonlocal previous
        state = page.image_note_state(allow_scrolled=True)
        if state["authorNickname"] != expected_author:
            raise PageMismatch("Work author changed during note scrolling")
        if getattr(device, "has_vision", False) is True and device.verify_roi_stable(state["box"]):
            return state
        signature = (state["authorNickname"], state["title"], tuple(state["paragraphs"]),
                     state["box"], state["incomplete"])
        if signature != previous:
            previous = signature
            raise PageMismatch("Image note layout is still moving")
        return state

    return device.wait(ready, timeout=20)


def read_note_detail(device, expected_card=None, max_scrolls=8):
    if type(max_scrolls) is not int or not 1 <= max_scrolls <= 50:
        raise ValueError("Note scroll limit must be between 1 and 50")
    detail = dict(device.wait(lambda page: page.note(allow_incomplete=True)))
    incomplete = detail.pop("_incomplete", None)
    if not incomplete:
        if expected_card is not None:
            verify_card(expected_card, detail)
        return detail
    if expected_card is not None:
        if (detail.get("noteType") != expected_card.note_type
                or detail.get("authorNickname") != expected_card.author):
            raise PageMismatch("Feed changed or opened work does not match the selected card")
    if detail.get("noteType") == "VIDEO":
        author = detail["authorNickname"]
        device.click(lambda page: page.note_expand_control())
        expanded = dict(device.wait(lambda page: page.expanded_video_note(author)))
        close_node = expanded.pop("_drawer_close", None)
        if close_node is not None:
            device.click(lambda page: page.video_drawer_close_button())
            try:
                device.wait(lambda page: page.returned_note(expanded), timeout=4)
            except PageMismatch:
                device.back()
                device.wait(lambda page: page.returned_note(expanded), timeout=4)
        if expected_card is not None:
            verify_card(expected_card, expanded)
        return expanded
    author = detail["authorNickname"]
    state = _current_page(device).image_note_state(allow_scrolled=True)
    if state["authorNickname"] != author:
        raise PageMismatch("Work author changed during note loading")
    if state["incomplete"] == "truncated":
        device.click(lambda page: page.note_expand_control())
        state = settled_image_note(device, author)
    title = state["title"]
    paragraphs = list(state["paragraphs"])
    for _ in range(max_scrolls):
        if not state["incomplete"]:
            break
        if state["incomplete"] == "truncated":
            device.click(lambda page: page.note_expand_control())
            state = settled_image_note(device, author)
        else:
            swipe_note_content(device, _current_page(device))
            state = settled_image_note(device, author)
        if not title and state["title"] and paragraphs and state["title"] == paragraphs[0]:
            title = state["title"]
            paragraphs = paragraphs[1:]
        paragraphs = merge_note_paragraphs(paragraphs, state["paragraphs"], title=title)
    if state["incomplete"]:
        raise PageMismatch("Title or description is truncated" if state["incomplete"] == "truncated"
                           else "Full title/body boundary is not visible")
    result = dict(noteType="IMAGE_TEXT", authorNickname=author,
                  title=title, description="\n".join(paragraphs))
    if expected_card is not None:
        verify_card(expected_card, result)
    return result


def collect_author_red_id(device, detail):
    note_type = detail.get("noteType")
    device.click(lambda page: page.unique(resource="matrixNickNameView" if note_type == "VIDEO" else "nickNameTV"))
    if getattr(device, "has_vision", False) is True:
        device.wait_visual_settle(timeout=0.8)

    def resolve_identity(page):
        try:
            return page.author_identity(detail["authorNickname"])
        except PageMismatch:
            if getattr(device, "recognizer", None) is None or getattr(device, "has_vision", False) is not True:
                raise
            frame = device.capture_frame()
            name_in_xml = any(
                (n.get("text") == detail["authorNickname"]
                 or n.get("content-desc") == f"头像,{detail['authorNickname']}")
                and not n.get("resource-id", "").endswith(("/nickNameTV", "/matrixNickNameView"))
                for n in getattr(page, "nodes", ())
            )
            red_id = ocr_author_identity(
                device.recognizer,
                frame,
                detail["authorNickname"],
                name_confirmed=name_in_xml,
            )
            if red_id:
                return red_id
            raise

    red_id = device.wait(resolve_identity)
    device.back()
    returned = device.wait(lambda page: page.returned_note(detail))
    verify_return(detail, returned)
    return red_id


def copy_note_share_link(device, detail, read_clipboard):
    note_type = detail.get("noteType")
    for attempt in range(3):
        device.click(lambda page: page.unique(description="分享") if note_type == "VIDEO"
                     else page.unique(resource="moreOperateIV"))
        try:
            device.wait(lambda page: page.unique(description="复制链接"), timeout=4)
            break
        except PageMismatch:
            if attempt == 2:
                raise
            verify_return(detail, device.wait(lambda page: page.returned_note(detail), timeout=4))
    copied_after = time.time_ns() // 1_000_000
    device.click(lambda page: page.unique(description="复制链接"))
    clip = read_clipboard()
    if not isinstance(clip, dict) or not isinstance(clip.get("timestamp"), int) or clip["timestamp"] < copied_after:
        raise PageMismatch("Clipboard did not contain a freshly copied link")
    url, note_id, _ = note_link(clip.get("text"))
    try:
        returned = device.wait(lambda page: page.returned_note(detail))
    except PageMismatch as error:
        if "share panel still covers" not in str(error):
            raise
        device.back()
        returned = device.wait(lambda page: page.returned_note(detail))
    verify_return(detail, returned)
    return url, note_id


def restore_feed(device, expected_cards=None):
    device.back()
    restored = device.wait(lambda page: page.cards())
    if expected_cards is not None and [(c.key, c.box) for c in expected_cards] != [(c.key, c.box) for c in restored]:
        if getattr(device, "has_vision", False) is True:
            device.wait_visual_settle(timeout=0.6)
            restored = device.wait(lambda page: page.cards())
        if [(c.key, c.box) for c in expected_cards] != [(c.key, c.box) for c in restored]:
            raise PageMismatch("Feed position changed; collection not committed")
    return restored


def collect_open_note(device, read_clipboard, expected_card=None, mark=lambda stage: None):
    detail = read_note_detail(device, expected_card=expected_card)
    mark("open_and_identify")
    red_id = collect_author_red_id(device, detail)
    mark("author_and_return")
    url, note_id = copy_note_share_link(device, detail, read_clipboard)
    detail.update(authorDouyinId=red_id, videoUrl=url, awemeId=note_id)
    mark("share_link_and_return")
    return detail


def collect_note(device, read_clipboard, data_dir, note_type=None, max_screens=None):
    if note_type not in (None, "VIDEO", "IMAGE_TEXT"):
        raise ValueError("Unsupported note type")
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    seen_file = data_dir / "seen.json"
    seen = set(json.loads(seen_file.read_text())) if seen_file.exists() else set()
    timings = {}
    started = previous = time.monotonic()

    def mark(stage):
        nonlocal previous
        now = time.monotonic()
        timings[stage] = round((now - previous) * 1000)
        print(json.dumps({"stage": stage, "elapsedMs": timings[stage]}), flush=True)
        previous = now

    cards, card = select_feed_card(device, seen, note_type=note_type, max_screens=max_screens)
    left, top, right, bottom = card.cover_box
    device.command("input", "tap", str((left + right) // 2), str((top + bottom) // 2))
    detail = collect_open_note(device, read_clipboard, expected_card=card, mark=mark)

    restore_feed(device, cards)
    mark("restore_feed")
    report = dict(status="collected_locally", payload=detail, timingsMs=timings,
                  elapsedMs=round((time.monotonic() - started) * 1000), uploaded=False)
    # Reports live outside the replaceable agent bundle; an incomplete write never becomes a result.
    report_path = data_dir / (uuid.uuid4().hex + ".json")
    temporary = report_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    temporary.replace(report_path)
    seen.add(card.key)
    temporary = seen_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(sorted(seen)))
    temporary.replace(seen_file)
    return report


def collect_notes(device, read_clipboard, data_dir, count=1, note_type="IMAGE_TEXT", max_screens=6):
    if type(count) is not int or not 1 <= count <= 50:
        raise ValueError("Note collection count must be between 1 and 50")
    started = time.monotonic()
    reports = []
    for _ in range(count):
        reports.append(collect_note(device, read_clipboard, data_dir,
                                    note_type=note_type, max_screens=max_screens))
    return dict(status="collected_batch_locally", count=len(reports), reports=reports,
                elapsedMs=round((time.monotonic() - started) * 1000), uploaded=False)
