"""Run recognition against local captures without posting device actions."""

import subprocess
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import numpy as np
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import Toolkit
from lazy_ocr import RecognitionOnlyController, register

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "discover": {"OpenDiscover", "OpenChannels"},
    "video-before": {"FeedReady", "LikeCurrentVideo"},
    "returned": {"FeedReady", "OpenAuthor", "OpenPersonal"},
    "video-light": {"FeedReady", "LikeCurrentVideo"},
    "video-light-liked": {"FeedReady", "OpenAuthor", "OpenPersonal"},
    "author": {"ReturnToFeed"},
    "author-simple": {"ReturnToFeed"},
    "author-many-videos": {"ReturnToFeed"},
    "author-no-tabs": {"ReturnToFeed"},
    "personal": {"LeavePersonal", "PersonalReady"},
    "tina-video-before": {"FeedReady", "LikeCurrentVideo"},
    "tina-video-raised": {"FeedReady", "LikeCurrentVideo"},
    "tina-video-liked": {"FeedReady", "OpenAuthor", "OpenPersonal"},
    "tina-busy-before": {"FeedReady", "LikeCurrentVideo"},
    "tina-personal": {"LeavePersonal", "PersonalReady"},
    "tina-personal-split": {"LeavePersonal", "PersonalReady"},
}
# Hand-labelled control bounds in the original normalized captures.
CLICK_BOUNDS = {
    "LikeCurrentVideo": (345, 1465, 405, 1525),
    "OpenAuthor": (115, 1470, 290, 1515),
    "OpenPersonal": (635, 65, 695, 125),
}
TINA_CLICK_BOUNDS = {
    "LikeCurrentVideo": (360, 1420, 415, 1480),
    "OpenAuthor": (115, 1430, 230, 1485),
    "OpenPersonal": (640, 85, 695, 135),
}


def main():
    Toolkit.init_option(ROOT / "logs" / "capture-verification")
    resource = Resource()
    register(resource)
    assert resource.post_bundle(ROOT / "wechat_channels/resource/base").wait().succeeded
    controller = RecognitionOnlyController()
    assert controller.post_connection().wait().succeeded
    tasker = Tasker()
    assert tasker.bind(resource, controller)
    names = set().union(*CASES.values())
    failures = []
    incomplete_page_checks = 0
    anchored_author_checks = 0
    missing_anchor_checks = 0
    timings = {name: [] for name in names}
    for capture, expected in CASES.items():
        for scale, dx, dy in ((1.0, 0, 0), (0.8, 72, 180)):
            # At 0.8 this low-contrast header is unreadable; recognition must stop safely.
            expected_hits = set() if capture == "tina-busy-before" and scale == 0.8 else expected
            raw = subprocess.check_output([
                "magick", str(ROOT / "captures" / f"mta-wechat-{capture}.png"),
                "-resize", "720x", "-resize", f"{scale * 100}%",
                "-background", "#303030", "-gravity", "northwest",
                "-splice", f"{dx}x{dy}", "-extent", "720x1700",
                "-depth", "8", "bgr:-",
            ])
            image = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 720, 3)
            for name in sorted(names):
                node = resource.get_node_object(name)
                started = time.perf_counter()
                result = tasker.post_recognition(
                    node.recognition.type, node.recognition.param, image,
                ).wait().get()
                timings[name].append(time.perf_counter() - started)
                assert result and result.nodes, f"Recognition did not execute: {capture}/{name}"
                recognition = result.nodes[-1].recognition
                hit = recognition.hit
                if hit != (name in expected_hits):
                    failures.append(f"{capture}/{scale}/{name}: hit={hit}")
                    print("FAIL", failures[-1], flush=True)
                else:
                    print("PASS", capture, scale, name, f"hit={hit}", flush=True)
                if hit and name == "FeedReady" and capture == "tina-busy-before":
                    icon = tasker.post_recognition(
                        node.recognition.type, replace(node.recognition.param, box_index=1), image,
                    ).wait().get().nodes[-1].recognition.box
                    assert icon and 640 <= icon.x + icon.w / 2 <= 695 and 85 <= icon.y + icon.h / 2 <= 135
                if hit and name in CLICK_BOUNDS and name in expected_hits:
                    box = recognition.box
                    assert box is not None
                    x, y = box.x + box.w / 2, box.y + box.h / 2
                    left, top, right, bottom = CLICK_BOUNDS[name]
                    if capture.startswith("tina-"):
                        left, top, right, bottom = TINA_CLICK_BOUNDS[name]
                        if capture in ("tina-video-raised", "tina-busy-before") and name == "LikeCurrentVideo":
                            top, bottom = 1200, 1270
                    if capture.startswith("video-light") and name == "OpenAuthor":
                        right = 238  # The follow button starts to the right of the author name.
                    assert left * scale + dx <= x <= right * scale + dx, (capture, name, box)
                    assert top * scale + dy <= y <= bottom * scale + dy, (capture, name, box)
            # Supply the hand-labelled previous click box to isolate the after-like guard.
            # Production resolves this ROI from the executed LikeCurrentVideo node.
            node = resource.get_node_object("OpenAuthorAfterLike")
            param = deepcopy(node.recognition.param)
            left, top, right, bottom = (TINA_CLICK_BOUNDS if capture.startswith("tina-") else CLICK_BOUNDS)["LikeCurrentVideo"]
            if capture in ("tina-video-raised", "tina-busy-before"):
                top, bottom = 1200, 1270
            param.all_of[1]["recognition"]["param"]["roi"] = [
                int(left * scale + dx), int(top * scale + dy),
                int((right - left) * scale), int((bottom - top) * scale),
            ]
            result = tasker.post_recognition(node.recognition.type, param, image).wait().get()
            assert result and result.nodes
            recognition = result.nodes[-1].recognition
            assert recognition.hit == ("OpenAuthor" in expected_hits), (capture, scale, "after-like guard")
            if recognition.hit:
                box = recognition.box
                assert box is not None
                left, top, right, bottom = (TINA_CLICK_BOUNDS if capture.startswith("tina-") else CLICK_BOUNDS)["OpenAuthor"]
                if capture.startswith("video-light"):
                    right = 238
                assert left * scale + dx <= box.x + box.w / 2 <= right * scale + dx
                assert top * scale + dy <= box.y + box.h / 2 <= bottom * scale + dy
                missing = tasker.post_recognition(node.recognition.type, node.recognition.param, image).wait().get()
                assert missing and not missing.nodes[-1].recognition.hit, "A missing previous click must not be guessed"
                missing_anchor_checks += 1
            anchored_author_checks += 1
            if capture == "tina-personal-split":
                # Remove each required heading/row in turn; partial evidence must not finish a task.
                node = resource.get_node_object("PersonalReady")
                for left, top, right, bottom in ((20, 160, 230, 220), (20, 250, 240, 315), (20, 345, 240, 410)):
                    incomplete = image.copy()
                    x1, x2 = (int(v * scale + dx) for v in (left, right))
                    y1, y2 = (int(v * scale + dy) for v in (top, bottom))
                    incomplete[y1:y2, x1:x2] = 240
                    result = tasker.post_recognition(
                        node.recognition.type, node.recognition.param, incomplete,
                    ).wait().get()
                    assert result and not result.nodes[-1].recognition.hit, (capture, scale, top)
                    incomplete_page_checks += 1
    for name, durations in sorted(timings.items()):
        print(f"TIMING {name}: total={sum(durations):.3f}s max={max(durations):.3f}s count={len(durations)}")
    assert not failures, "\n".join(failures)
    icon_node = resource.get_node_object("PersonalIcon")
    icon_param = replace(icon_node.recognition.param, roi=[0, 0, 720, 160], roi_offset=[0, 0, 0, 0])
    for color in ((0, 0, 0), (255, 255, 255), (128, 128, 128), (0, 255, 0)):
        blank = np.empty((160, 720, 3), dtype=np.uint8)
        blank[:] = color
        result = tasker.post_recognition(icon_node.recognition.type, icon_param, blank).wait().get()
        assert result and not result.nodes[-1].recognition.hit, color
    red_node = resource.get_node_object("RedThumb")
    for width, height in ((40, 40), (60, 15), (15, 60)):
        blank = np.zeros((200, 200, 3), dtype=np.uint8)
        blank[80:80 + height, 80:80 + width] = (80, 80, 255)
        result = tasker.post_recognition(red_node.recognition.type, red_node.recognition.param, blank).wait().get()
        assert result and not result.nodes[-1].recognition.hit, (width, height)
    print("Passed 3 red non-thumb shape rejection checks.")
    print("Passed 4 uniform-background rejection checks.")
    print(f"Passed {incomplete_page_checks} incomplete personal-page rejection checks.")
    print(f"Passed {anchored_author_checks} anchored author checks and {missing_anchor_checks} missing-anchor rejection checks.")
    print(f"Passed {len(CASES) * len(names) * 2} recognition checks; no device actions posted.")


if __name__ == "__main__":
    main()
