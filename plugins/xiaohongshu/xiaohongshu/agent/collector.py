"""One bounded, read-only collection transaction using fresh semantic controls."""

import json
from pathlib import Path
import tempfile
import time
import uuid

from commands import run
from discovery import note_link
from ui import Page, PageMismatch, bounds, center, verify_card, verify_return


class Device:
    def __init__(self, stopped=lambda: False):
        self.stopped = stopped

    def command(self, *args):
        print(json.dumps({"deviceCommand": args[0], "phase": "start"}), flush=True)
        if self.stopped():
            raise InterruptedError("Task stopped")
        result = run(args, timeout=15, stopped=self.stopped)
        if self.stopped():
            raise InterruptedError("Task stopped")
        print(json.dumps({"deviceCommand": args[0], "phase": "done"}), flush=True)
        return result

    def page(self):
        # A unique dump file prevents an unsuccessful dump from reusing old screen contents.
        with tempfile.TemporaryDirectory(prefix="xhs-ui-") as directory:
            target = Path(directory) / "window.xml"
            helper = Path(__file__).with_name("ui-dump.dex")
            if not helper.is_file():
                raise RuntimeError("UI hierarchy helper is missing; reinstall plugin resources")
            self.command("env", "CLASSPATH=" + str(helper), "app_process", "/system/bin", "xhs.UiDump", str(target))
            if not target.is_file():
                raise PageMismatch("UI hierarchy was unavailable during transition")
            return Page(target.read_text())

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


def collect_note(device, read_clipboard, data_dir, note_type=None):
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

    for screen in range(6):
        cards = device.wait(lambda page: page.cards())
        card = next((item for item in cards if item.key not in seen
                     and (note_type is None or item.note_type == note_type)), None)
        if card is not None:
            break
        if note_type is None or not cards or screen == 5:
            raise PageMismatch("No unprocessed note of the requested type within the search limit")
        left = min(c.box[0] for c in cards)
        top = min(c.box[1] for c in cards)
        right = max(c.box[2] for c in cards)
        bottom = max(c.box[3] for c in cards)
        x = str((left + right) // 2)
        device.command("input", "swipe", x, str(top + (bottom - top) * 4 // 5),
                       x, str(top + (bottom - top) // 5), "450")
        time.sleep(0.4)
    current = device.page().cards()
    if card not in current:
        raise PageMismatch("Feed changed before click; no action performed")
    left, top, right, bottom = card.cover_box
    device.command("input", "tap", str((left + right) // 2), str((top + bottom) // 2))
    detail = device.wait(lambda page: page.note())
    verify_card(card, detail)
    if card.note_type == "VIDEO":
        device.click(lambda page: page.unique(description="暂停"))
    mark("open_and_identify")

    device.click(lambda page: page.unique(resource="matrixNickNameView" if card.note_type == "VIDEO" else "nickNameTV"))
    red_id = device.wait(lambda page: page.author_identity(detail["authorNickname"]))
    device.back()
    returned = device.wait(lambda page: page.note())
    verify_return(detail, returned)
    mark("author_and_return")

    for attempt in range(3):
        device.click(lambda page: page.unique(description="分享") if card.note_type == "VIDEO"
                     else page.unique(resource="moreOperateIV"))
        try:
            device.wait(lambda page: page.unique(description="复制链接"), timeout=4)
            break
        except PageMismatch:
            if attempt == 2:
                raise
            verify_return(detail, device.wait(lambda page: page.note(), timeout=4))
    copied_after = time.time_ns() // 1_000_000
    device.click(lambda page: page.unique(description="复制链接"))
    clip = read_clipboard()
    if not isinstance(clip, dict) or not isinstance(clip.get("timestamp"), int) or clip["timestamp"] < copied_after:
        raise PageMismatch("Clipboard did not contain a freshly copied link")
    url, note_id, _ = note_link(clip.get("text"))
    returned = device.wait(lambda page: page.note())
    verify_return(detail, returned)
    detail.update(authorDouyinId=red_id, videoUrl=url, awemeId=note_id)
    mark("share_link_and_return")

    device.back()
    restored = device.wait(lambda page: page.cards())
    if [(c.key, c.box) for c in cards] != [(c.key, c.box) for c in restored]:
        raise PageMismatch("Feed position changed; collection not committed")
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
