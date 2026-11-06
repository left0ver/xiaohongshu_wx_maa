"""Maa OCR adapter. Both the packaged agent and on-device acceptance runner use it."""

import json
import re
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from contextlib import contextmanager

import numpy as np
from maa.controller import CustomController
from maa.library import Library
from maa.pipeline import JAnd, JOCR, JTemplateMatch
from maa.resource import Resource
from maa.tasker import Tasker
from collection import find, navigation_roi
from commands import run
from hierarchy import HierarchyUnavailable, choose_snapshot, refresh_target, text_lines
from readiness import wait_basic_info
from layout import line_height
from probe_reader import prepared_reader


def command(*args, timeout=15):
    return run(list(args), timeout=timeout, binary=True)


def foreground(timeout=15):
    activity = command("dumpsys", "activity", "activities", timeout=timeout).decode("utf-8", "replace")
    resumed = re.findall(r"(?:mResumedActivity|topResumedActivity):[^\n]+", activity)
    if not resumed or not all(" com.tencent.mm/" in line for line in resumed):
        raise RuntimeError("WeChat is not the foreground app; no input was sent")


class PhoneController(CustomController):
    """Acceptance adapter executed as Android shell, never a desktop ADB driver."""

    def connect(self):
        return True

    def request_uuid(self):
        return "wechat-channels-phone"

    def get_features(self):
        return 0

    def start_app(self, intent):
        if intent != "com.tencent.mm":
            return False
        command("am", "start", "-n", "com.tencent.mm/.ui.LauncherUI")
        return True

    def screencap(self):
        raw = command("screencap")
        width, height, pixel_format = struct.unpack_from("<III", raw)
        if pixel_format not in (1, 2) or width <= 0 or height <= 0:
            raise RuntimeError("Unsupported Android screenshot format")
        header = len(raw) - width * height * 4
        if header not in (12, 16):
            raise RuntimeError("Unexpected Android screenshot stride")
        rgba = np.frombuffer(raw, dtype=np.uint8, offset=header).reshape(height, width, 4)
        return rgba[:, :, 2::-1].copy()

    def click(self, x, y):
        foreground()
        command("input", "tap", str(x), str(y))
        return True

    def click_key(self, keycode):
        foreground()
        if keycode != 4:
            return False
        command("input", "keyevent", "4")
        return True

    def swipe(self, x1, y1, x2, y2, duration):
        foreground()
        command("input", "swipe", *map(str, (x1, y1, x2, y2, duration)))
        return True

    # These actions are intentionally unavailable to this read-only collector.
    def stop_app(self, intent):
        return False

    def input_text(self, text):
        return False

    def touch_down(self, contact, x, y, pressure):
        return False

    def touch_move(self, contact, x, y, pressure):
        return False

    def touch_up(self, contact):
        return False

    def key_down(self, keycode):
        return False

    def key_up(self, keycode):
        return False


class MaaUi:
    def __init__(self, tasker, evidence_dir, context=None):
        self.tasker = tasker
        self.controller = tasker.controller
        self.context = context
        self.evidence_dir = Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.sequence = 0
        self.observation = 0
        self.hierarchy_unavailable = False
        self.navigation_stage = None
        sizes = re.findall(r"(\d+)x(\d+)", command("wm", "size").decode())
        if not sizes:
            raise RuntimeError("Physical screen size is unavailable")
        self.width, self.physical_height = map(int, sizes[-1])
        if self.width >= self.physical_height:
            raise RuntimeError("Collection requires portrait orientation")
        self.height = round(self.physical_height * 720 / self.width)
        path = Path(__file__).resolve().parents[1] / "resource/base/pipeline/collect-video.json"
        self.nodes = json.loads(path.read_text(encoding="utf-8"))

    def hierarchy(self, timeout=2, force=False):
        if self.tasker.stopping:
            raise InterruptedError("Collection was stopped")
        if self.hierarchy_unavailable and not force:
            raise HierarchyUnavailable("Empty hierarchy already observed on this page; probe skipped")
        deadline = time.monotonic() + timeout
        try:
            foreground(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise HierarchyUnavailable("Hierarchy foreground check timed out") from error
        helper = Path(__file__).with_name("ui-dump.dex")
        if not helper.is_file():
            raise RuntimeError("UI hierarchy helper is missing; reinstall plugin resources")
        with tempfile.TemporaryDirectory(prefix="wechat-ui-") as directory:
            target = Path(directory) / "window.xml"
            try:
                run(["env", "CLASSPATH=" + str(helper), "app_process", "/system/bin", "xhs.UiDump", str(target)],
                    timeout=max(0, deadline - time.monotonic()), stopped=lambda: self.tasker.stopping)
                xml = target.read_text(encoding="utf-8")
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as error:
                raise HierarchyUnavailable("Hierarchy is unavailable: " + type(error).__name__) from error
        if self.tasker.stopping:
            raise InterruptedError("Collection was stopped")
        try:
            foreground(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise HierarchyUnavailable("Hierarchy foreground confirmation timed out") from error
        try:
            return self.annotate(text_lines(xml, self.width, self.physical_height), "hierarchy")
        except HierarchyUnavailable:
            self.hierarchy_unavailable = True
            raise

    def annotate(self, lines, source):
        self.observation += 1
        observed = time.time_ns() // 1_000_000
        return [{**line, "source": source, "observationId": self.observation,
                 "observedAtUnixMs": observed} for line in lines]

    def template_navigation(self, label):
        names = ("BatchDiscoverTab",) if label == "navigation-bottom" else ("BatchChannelsEntry", "BatchRecommended")
        if label == "navigation-top" and self.navigation_stage in names:
            names = (self.navigation_stage,)
        for name in names:
            line = self.match_navigation(name)
            if line:
                return [line]
        return []

    def match_navigation(self, name):
        if self.tasker.stopping:
            raise InterruptedError("Collection was stopped")
        foreground()
        node = json.loads(json.dumps(self.nodes[name]))
        quarter = (self.height + 3) // 4
        roi = [0, self.height - quarter, 720, quarter] if name == "BatchDiscoverTab" else [0, 0, 720, quarter]
        for child in node["all_of"]:
            if isinstance(child.get("roi"), list):
                child["roi"] = roi
        if self.context:
            result = self.context.run_task(name, {name: node})
            detail = result.nodes[-1].recognition if result and result.nodes else None
        else:
            job = self.controller.post_screencap().wait()
            if not job.succeeded:
                raise RuntimeError("Screenshot failed")
            self.image = job.get()
            spec = {key: node[key] for key in ("all_of", "box_index") if key in node}
            result = self.tasker.post_recognition("And", JAnd(**spec), self.image).wait().get()
            detail = result.nodes[-1].recognition if result and result.nodes else None
        if detail is None or not detail.hit or detail.box is None:
            return None
        box = detail.box
        role, text = {"BatchDiscoverTab": ("discover", "发现"),
                      "BatchChannelsEntry": ("channels", "视频号"),
                      "BatchRecommended": ("recommend", "推荐")}[name]
        return {"text": text, "box": [box.x, box.y, box.w, box.h],
                "navigation": role, "templateNode": name}

    def snapshot(self, label, author=None):
        attempts = []

        def measured(source, read):
            started = time.monotonic_ns()
            attempt = {"source": source}
            try:
                return read()
            except HierarchyUnavailable as error:
                attempt["reason"] = str(error)
                raise
            finally:
                attempt["durationMs"] = (time.monotonic_ns() - started) // 1_000_000
                attempts.append(attempt)

        lines, source = choose_snapshot(label, self.height, author,
            lambda: measured("hierarchy", self.hierarchy),
            lambda: measured("template", lambda: self.template_navigation(label)),
            lambda: measured("ocr", lambda: self.ocr_snapshot(label)),
            regional_ocr=lambda roi: measured("ocr-region", lambda: self.ocr_snapshot(label, roi)),
            refresh=lambda: measured("hierarchy-confirm", lambda: self.hierarchy(force=True)))
        self.record_snapshot(label, lines, source, attempts, author)
        return lines

    def record_snapshot(self, label, lines, source, attempts, author=None):
        from hierarchy import field_evidence
        self.sequence += 1
        evidence = {"label": label, "width": 720, "height": self.height,
                    "roi": navigation_roi(label, self.height), "source": source, "attempts": attempts, "lines": lines,
                    "fields": field_evidence(lines, label, author)}
        (self.evidence_dir / f"{self.sequence:03d}-{label}.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"kind": "recognitionTiming", "label": label, "source": source,
                          "attempts": attempts}, ensure_ascii=False), flush=True)

    def ocr_snapshot(self, label, roi=None):
        if self.tasker.stopping:
            raise RuntimeError("Collection was stopped")
        foreground()
        if self.context:
            # Keep screenshots on the native side: transferring the same pixels
            # to Python and back over Agent RPC was dominating collection time.
            roi = navigation_roi(label, self.height) if roi is None else roi
            result = self.context.run_task("BatchText", {
                "BatchText": {"recognition": "OCR", "threshold": 0.55,
                              "roi": roi, "only_rec": False,
                              "action": "DoNothing", "next": [], "pre_delay": 0, "post_delay": 0},
            })
            detail = result.nodes[-1].recognition if result and result.nodes else None
        else:
            job = self.controller.post_screencap().wait()
            if not job.succeeded:
                raise RuntimeError("Screenshot failed")
            image = job.get()
            self.image = image
            self.height = image.shape[0]
            roi = navigation_roi(label, self.height) if roi is None else roi
            ocr_image = image
            if label.startswith("comments"):
                # Mask orange Channels butterfly badges next to nicknames so OCR
                # does not misread the decorative icon as a trailing 'W'.
                ocr_image = image.copy()
                b, g, r = ocr_image[:, :, 0].astype("int16"), ocr_image[:, :, 1].astype("int16"), ocr_image[:, :, 2].astype("int16")
                orange = (r > 195) & (g > 105) & (g < 210) & (b < 100) & ((r - b) > 110)
                if orange.any():
                    ocr_image[orange] = 255
            result = self.tasker.post_recognition("OCR", JOCR(threshold=0.55, roi=roi), ocr_image).wait().get()
            detail = result.nodes[0].recognition if result and result.nodes else None
        if detail is None:
            raise RuntimeError("OCR returned no result")
        # Native results retain every OCR box, including ones not chosen as best_result.
        lines = detail.raw_detail.get("all", [])
        lines = [line for line in lines if line.get("score", 0) >= 0.55 and line.get("text", "").strip()]
        return self.annotate(lines, "ocr")

    def read_profile_bio(self, lines, profile):
        """Read only the biography text, masking colored decorative symbols."""
        text = profile.get("authorBio")
        if not text:
            return text
        parts = text.splitlines()
        selected = [line for line in lines if line["text"] in parts]
        if not selected:
            raise ValueError("Biography region is missing")
        top = max(0, min(line["box"][1] for line in selected) - 3)
        bottom = max(line["box"][1] + line["box"][3] for line in selected) + 3
        foreground()
        job = self.controller.post_screencap().wait()
        if not job.succeeded:
            raise RuntimeError("Biography screenshot failed")
        pixels = job.get().copy()
        spread = pixels.max(axis=2).astype("int16") - pixels.min(axis=2).astype("int16")
        pixels[(spread > 24) | (pixels.min(axis=2) > 160)] = 255
        roi = [0, top, 720, bottom - top]
        if self.context:
            detail = self.context.run_recognition("BatchText", pixels, {
                "BatchText": {"recognition": "OCR", "threshold": 0.55, "roi": roi, "only_rec": False}})
        else:
            result = self.tasker.post_recognition("OCR", JOCR(threshold=0.55, roi=roi), pixels).wait().get()
            detail = result.nodes[0].recognition if result and result.nodes else None
        values = detail.raw_detail.get("all", []) if detail else []
        values = [line for line in values if line.get("score", 0) >= 0.55 and line.get("text", "").strip()]
        if not values:
            raise ValueError("Biography text was not recognized")
        self.record_snapshot("author-biography", self.annotate(values, "ocr-bio"), "ocr-bio", [])
        return "\n".join(line["text"] for line in sorted(values, key=lambda line: (line["box"][1], line["box"][0])))

    @contextmanager
    def prepare_basic_info(self):
        started = time.monotonic_ns()
        with prepared_reader(Path(__file__).with_name("probe.py"), stopped=lambda: self.tasker.stopping) as reader:
            print(json.dumps({"kind": "readinessPreparationTiming", "durationMs":
                              (time.monotonic_ns() - started) // 1_000_000}), flush=True)
            yield reader

    def wait_basic_info(self, timeout, reader):
        started = time.monotonic_ns()
        source, outcome = None, "failed"

        def bounded_ocr(remaining):
            result = reader(remaining)
            return self.annotate(result["lines"], result["source"])

        try:
            lines, source = wait_basic_info(lambda remaining: self.hierarchy(timeout=remaining), bounded_ocr, timeout)
            outcome = "succeeded"
            self.record_snapshot("author-basic-info-ready", lines, source, [
                {"source": "readiness", "durationMs": (time.monotonic_ns() - started) // 1_000_000}])
            return lines
        finally:
            print(json.dumps({"kind": "readinessTiming", "label": "author-basic-info", "source": source,
                              "outcome": outcome, "budgetMs": int(timeout * 1000),
                              "durationMs": (time.monotonic_ns() - started) // 1_000_000}), flush=True)

    def profile_more(self, name):
        roi = [540, max(0, name["box"][1] - 15), 180, name["box"][3] + 80]
        if self.context:
            result = self.context.run_task("BatchProfileMore", {
                "BatchProfileMore": {"recognition": "TemplateMatch", "template": "profile-more.png",
                                     "roi": roi, "threshold": 0.88, "action": "DoNothing",
                                     "next": [], "pre_delay": 0, "post_delay": 0},
            })
            detail = result.nodes[-1].recognition if result and result.nodes else None
        else:
            job = self.controller.post_screencap().wait()
            if not job.succeeded:
                raise RuntimeError("Screenshot failed")
            self.image = job.get()
            result = self.tasker.post_recognition("TemplateMatch", JTemplateMatch(
                template=["profile-more.png"], roi=roi, threshold=[0.88],
            ), self.image).wait().get()
            detail = result.nodes[0].recognition if result and result.nodes else None
        if detail is None or not detail.hit or detail.box is None:
            raise ValueError("Author profile menu icon was not recognized")
        box = detail.box
        self.click({"box": [box.x, box.y, box.w, box.h]})

    def click(self, line):
        if self.tasker.stopping:
            raise RuntimeError("Collection was stopped")
        if line.get("selector"):
            line = refresh_target(self.hierarchy(force=True), line)
        elif line.get("templateNode"):
            current = self.match_navigation(line["templateNode"])
            if not current or current["box"] != line["box"]:
                raise RuntimeError("Navigation control changed before click; no input was sent")
            line = current
        foreground()
        x, y, width, height = line["box"]
        if not self.controller.post_click(x + width // 2, y + height // 2).wait().succeeded:
            raise RuntimeError("Click failed")
        self.navigation_stage = {"发现": "BatchChannelsEntry", "视频号": "BatchRecommended"}.get(line.get("text"))
        time.sleep(0.6)

    def back(self):
        if self.tasker.stopping:
            raise RuntimeError("Collection was stopped")
        foreground()
        if not self.controller.post_click_key(4).wait().succeeded:
            raise RuntimeError("Back failed")
        self.navigation_stage = None
        time.sleep(0.95)

    def restore_work_header(self, lines):
        heading = find(lines, r"^评论\s*\d*")
        if not heading:
            raise ValueError("Cannot scroll an unclassified details drawer")
        height = line_height(lines)
        start = int(heading["box"][1] + height * 5)
        end = int(self.height - height * 4)
        if end <= start:
            raise ValueError("Details drawer has no safe scroll region")
        foreground()
        if not self.controller.post_swipe(360, start, 360, end, 300).wait().succeeded:
            raise RuntimeError("Details header recovery failed")
        self.navigation_stage = None
        time.sleep(0.6)

    def scroll_comments(self, lines):
        heading = find(lines, r"^评论\s*\d*")
        if not heading:
            raise ValueError("Cannot scroll an unclassified comment drawer")
        height = line_height(lines)
        end = max(int(heading["box"][1] + heading["box"][3] + height * 3), int(self.height * 0.42))
        start = min(int(self.height - 140), int(self.height * 0.84))
        if start <= end + int(height * 2):
            raise ValueError("Comment drawer has no safe scroll region")
        foreground()
        if not self.controller.post_swipe(360, start, 360, end, 350).wait().succeeded:
            raise RuntimeError("Comment drawer scroll failed")
        self.navigation_stage = None
        time.sleep(0.7)

    def start_wechat(self):
        self.hierarchy_unavailable = False
        self.navigation_stage = None
        if not self.controller.post_start_app("com.tencent.mm").wait().succeeded:
            raise RuntimeError("WeChat could not be opened")
        self.startup_deadline = time.monotonic() + 10
        while True:
            try:
                foreground()
                return
            except RuntimeError:
                remaining = self.startup_deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("WeChat did not enter the foreground within 10 seconds")
                time.sleep(min(0.25, remaining))


def phone_ui(root, evidence_dir):
    root = Path(root)
    Library.open(root / "runtime/lib")
    Tasker.set_log_dir(evidence_dir)
    controller = PhoneController()
    controller.set_screenshot_target_short_side(720)
    if not controller.post_connection().wait().succeeded:
        raise RuntimeError("Phone controller connection failed")
    resource = Resource()
    if not resource.post_bundle(root / "pi/resource/base").wait().succeeded:
        raise RuntimeError("Resource loading failed")
    tasker = Tasker()
    if not tasker.bind(resource, controller):
        raise RuntimeError("Tasker binding failed")
    return MaaUi(tasker, evidence_dir)
