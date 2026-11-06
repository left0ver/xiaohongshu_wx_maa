"""Read-only OCR in a separate process, killed by the parent's readiness budget.

Keeping this outside the Agent RPC makes even a stuck synchronous OCR call
cancellable without leaving a thread that could operate on a later page.
"""

import json
import os
import sys
import time
from pathlib import Path

from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker
from maa.pipeline import JTemplateMatch

from android_ui import MaaUi, PhoneController, foreground
from layout import basic_info_ready
from readiness import framework_directory


def template_labels(ui, image):
    # Readiness labels do not need a full-resolution frame. Match both against
    # the same 360px-wide image; convert hit boxes back to the collector's 720px.
    image = image[::2, ::2].copy()
    roi = [0, 0, image.shape[1], image.shape[0]]
    lines = []
    for name, text in (("BatchBasicTitle", "基础信息"), ("BatchChannelsId", "视频号ID")):
        node = ui.nodes[name]
        threshold = min(node["threshold"], 0.75 if name == "BatchBasicTitle" else 0.70)
        detail = None
        # Try the baseline scale first; the other sizes remain a fallback for
        # font/display scaling, rather than paying for all scales on every hit.
        for template in (node["template"] + "/100.png", node["template"]):
            result = ui.tasker.post_recognition("TemplateMatch", JTemplateMatch(
                template=[template], threshold=[threshold], method=node["method"], roi=roi,
            ), image).wait().get()
            detail = result.nodes[-1].recognition if result and result.nodes else None
            if detail and detail.hit and detail.box is not None:
                break
        if detail is None or not detail.hit or detail.box is None:
            return []
        box = detail.box
        lines.append({"text": text, "box": [2 * box.x, 2 * box.y, 2 * box.w, 2 * box.h], "source": "local-template"})
    title, channel = (line["box"] for line in lines)
    if (title[0] >= 120
            or title[1] >= channel[1]
            or abs(title[0] - channel[0]) > max(title[3], channel[3])
            or channel[1] - title[1] > max(title[3], channel[3]) * 12):
        return []
    return lines


def main():
    output = Path(sys.argv[1])
    prepared = sys.argv[2] == "--prepare"
    deadline = time.monotonic() + (15 if prepared else float(sys.argv[2]))
    Library.open(framework_directory(os.environ))
    Tasker.set_log_dir(output.parent / "probe-log")
    controller = PhoneController()
    controller.set_screenshot_target_short_side(720)
    if not controller.post_connection().wait().succeeded:
        raise RuntimeError("Readiness screenshot controller could not connect")
    resource = Resource()
    if not resource.post_bundle(Path(__file__).resolve().parents[1] / "resource/base").wait().succeeded:
        raise RuntimeError("Readiness resources could not load")
    tasker = Tasker()
    if not tasker.bind(resource, controller):
        raise RuntimeError("Readiness recognizer could not bind")
    ui = MaaUi(tasker, output.parent)
    if prepared:
        (output.parent / "prepared").touch()
        request = output.parent / "request"
        while not request.is_file():
            if time.monotonic() >= deadline:
                raise TimeoutError("Readiness probe was not requested")
            time.sleep(0.025)
        deadline = time.monotonic() + float(request.read_text(encoding="utf-8"))
    # The foreground video is deliberately excluded. This reader never clicks.
    roi = [0, ui.height // 4, 240, ui.height // 2]
    template_tries = 0
    while time.monotonic() < deadline:
        foreground()
        job = controller.post_screencap().wait()
        if not job.succeeded:
            raise RuntimeError("Readiness screenshot failed")
        image = job.get()
        lines = template_labels(ui, image)
        source = "local-template"
        template_tries += 1
        if not lines and (template_tries >= 3 or deadline - time.monotonic() <= 2.4):
            lines = ui.ocr_snapshot("author-basic-info", roi)
            source = "local-ocr"
        if basic_info_ready(lines):
            foreground()
            staged = output.with_suffix(".tmp")
            staged.write_text(json.dumps({"lines": lines, "source": source}, ensure_ascii=False), encoding="utf-8")
            staged.replace(output)
            return
        time.sleep(min(0.12, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    main()
