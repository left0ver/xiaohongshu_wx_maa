"""Maa OCR adapter. Both the packaged agent and on-device acceptance runner use it."""

import json
import re
import struct
import subprocess
import time
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
from maa.controller import CustomController
from maa.library import Library
from maa.pipeline import JOCR, JTemplateMatch
from maa.resource import Resource
from maa.tasker import Tasker
from collection import navigation_roi


def command(*args, timeout=15):
    return subprocess.run(args, check=True, capture_output=True, timeout=timeout).stdout


def foreground():
    activity = command("dumpsys", "activity", "activities").decode("utf-8", "replace")
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
        self.height = 0
        if context:
            sizes = re.findall(r"(\d+)x(\d+)", command("wm", "size").decode())
            if not sizes:
                raise RuntimeError("Physical screen size is unavailable")
            width, height = map(int, sizes[-1])
            if width >= height:
                raise RuntimeError("Collection requires portrait orientation")
            self.height = round(height * 720 / width)

    def snapshot(self, label):
        if self.tasker.stopping:
            raise RuntimeError("Collection was stopped")
        foreground()
        if self.context:
            # Keep screenshots on the native side: transferring the same pixels
            # to Python and back over Agent RPC was dominating collection time.
            roi = navigation_roi(label, self.height)
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
            roi = navigation_roi(label, self.height)
            result = self.tasker.post_recognition("OCR", JOCR(threshold=0.55, roi=roi), image).wait().get()
            detail = result.nodes[0].recognition if result and result.nodes else None
        if detail is None:
            raise RuntimeError("OCR returned no result")
        # Native results retain every OCR box, including ones not chosen as best_result.
        lines = detail.raw_detail.get("all", [])
        lines = [line for line in lines if line.get("score", 0) >= 0.55 and line.get("text", "").strip()]
        self.sequence += 1
        evidence = {"label": label, "width": 720, "height": self.height, "roi": roi, "lines": lines}
        (self.evidence_dir / f"{self.sequence:03d}-{label}.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        return lines

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
            result = self.tasker.post_recognition("TemplateMatch", JTemplateMatch(
                template=["profile-more.png"], roi=roi, threshold=[0.88],
            ), self.image).wait().get()
            detail = result.nodes[0].recognition if result and result.nodes else None
        if detail is None or not detail.hit or detail.box is None:
            raise ValueError("Author profile menu icon was not recognized")
        box = detail.box
        self.click({"box": [box.x, box.y, box.w, box.h]})

    def refine_profile_name(self, lines, hint):
        # Large profile names can be split into overlapping detector boxes.
        # Similarity chooses a region only; the resulting full name still has
        # to agree with the original and returned work headers before saving.
        candidates = [line for line in lines if 170 <= line["box"][0] < 540
                      and self.height / 4 < line["box"][1] < self.height * 0.7]
        target = max(candidates, key=lambda line: SequenceMatcher(None, hint, line["text"]).ratio(), default=None)
        if target is None or SequenceMatcher(None, hint, target["text"]).ratio() < 0.6:
            raise ValueError("Profile name region could not be identified")
        center = target["box"][1] + target["box"][3] / 2
        row = [line for line in candidates if abs(line["box"][1] + line["box"][3] / 2 - center) < target["box"][3] / 2]
        left = max(0, min(line["box"][0] for line in row) - 20)
        top = max(0, min(line["box"][1] for line in row) - 5)
        right = min(540, max(line["box"][0] + line["box"][2] for line in row) + 8)
        bottom = max(line["box"][1] + line["box"][3] for line in row) + 5
        roi = [left, top, right - left, bottom - top]
        if self.context:
            result = self.context.run_task("BatchText", {"BatchText": {
                "recognition": "OCR", "roi": roi, "only_rec": True, "threshold": 0.55,
                "action": "DoNothing", "next": [], "pre_delay": 0, "post_delay": 0,
            }})
        else:
            result = self.tasker.post_recognition("OCR", JOCR(roi=roi, only_rec=True, threshold=0.55), self.image).wait().get()
        detail = result.nodes[-1].recognition if result and result.nodes else None
        refined = detail.raw_detail.get("all", []) if detail else []
        if len(refined) != 1 or refined[0].get("score", 0) < 0.55:
            raise ValueError("Full profile name recognition failed")
        (self.evidence_dir / "refined-profile-name.json").write_text(json.dumps(refined, ensure_ascii=False, indent=2), encoding="utf-8")
        return [line for line in lines if line not in row] + refined

    def click(self, line):
        if self.tasker.stopping:
            raise RuntimeError("Collection was stopped")
        foreground()
        x, y, width, height = line["box"]
        if not self.controller.post_click(x + width // 2, y + height // 2).wait().succeeded:
            raise RuntimeError("Click failed")
        time.sleep(0.6)

    def back(self):
        if self.tasker.stopping:
            raise RuntimeError("Collection was stopped")
        foreground()
        if not self.controller.post_click_key(4).wait().succeeded:
            raise RuntimeError("Back failed")
        time.sleep(0.6)

    def start_wechat(self):
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
