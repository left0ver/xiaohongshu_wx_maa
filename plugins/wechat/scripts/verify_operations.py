"""Check standalone operation recognition against saved screens, without device input."""

import subprocess
from pathlib import Path

import numpy as np
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import Toolkit

from lazy_ocr import RecognitionOnlyController, register

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "video-before": {"VideoReady", "LikeVisibleVideo", "OpenVisibleAuthor"},
    "returned": {"VideoReady", "VideoAlreadyLiked", "OpenVisibleAuthor"},
    "profile-video": {"VideoReady", "LikeVisibleVideo", "OpenVisibleAuthor"},
    "author-grid": {"AuthorReady", "OpenAuthorVideo"},
    "discover": set(),
    "personal": set(),
}


def main():
    Toolkit.init_option(ROOT / "logs/operation-verification")
    resource = Resource()
    register(resource)
    assert resource.post_bundle(ROOT / "wechat_channels/resource/base").wait().succeeded
    controller = RecognitionOnlyController()
    assert controller.post_connection().wait().succeeded
    tasker = Tasker()
    assert tasker.bind(resource, controller)
    names = set().union(*CASES.values(), {"LikedVideoReady"})
    failures = []
    for capture, expected in CASES.items():
        for scale, dx, dy in ((1, 0, 0), (.8, 72, 180)):
            raw = subprocess.check_output([
                "magick", str(ROOT / "captures" / f"mta-wechat-{capture}.png"),
                "-resize", "720x", "-resize", f"{scale * 100}%", "-background", "#303030",
                "-gravity", "northwest", "-splice", f"{dx}x{dy}", "-extent", "720x1700", "-depth", "8", "bgr:-",
            ])
            image = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 720, 3)
            for name in sorted(names):
                node = resource.get_node_object(name)
                result = tasker.post_recognition(node.recognition.type, node.recognition.param, image).wait().get()
                assert result and result.nodes
                reco = result.nodes[-1].recognition
                if reco.hit != (name in expected):
                    failures.append((capture, scale, name, reco.hit))
                if reco.hit and name == "OpenAuthorVideo":
                    box = reco.box
                    assert 170 * scale + dx <= box.x + box.w / 2 <= 215 * scale + dx
                    assert 1420 * scale + dy <= box.y + box.h / 2 <= 1480 * scale + dy
                if reco.hit and name == "OpenVisibleAuthor":
                    box = reco.box
                    assert 110 * scale + dx <= box.x + box.w / 2 <= 250 * scale + dx
                    assert 1470 * scale + dy <= box.y + box.h / 2 <= 1530 * scale + dy
                if reco.hit and name == "LikeVisibleVideo":
                    box = reco.box
                    assert 345 * scale + dx <= box.x + box.w / 2 <= 405 * scale + dx
                    assert 1465 * scale + dy <= box.y + box.h / 2 <= 1525 * scale + dy
                print("PASS" if reco.hit == (name in expected) else "FAIL", capture, scale, name, reco.hit, flush=True)
    assert not failures, failures
    print(f"Passed {len(CASES) * 2 * len(names)} operation recognition checks; no device actions.")


if __name__ == "__main__":
    main()
