"""Maa OCR adapter. Both the packaged agent and on-device acceptance runner use it."""

import atexit
import json
import os
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
from collection import MESSAGE_COMMENT_TAB, MESSAGE_LIKE_TAB, find, navigation_roi
from commands import run
from foreground import foreground, invalidate_foreground_cache, is_wechat_foreground
from frames import AsyncFrameWriter, materialize_frame_file, resolve_dump_frames, schedule_frame_prune
from hierarchy import HierarchyUnavailable, channels_intro_acknowledgement, choose_snapshot, page_lines, refresh_target, text_lines
from readiness import wait_basic_info
from layout import find_comment_drawer_heading, line_height
from probe_reader import prepared_reader

COMMENT_SWIPE_MS = 900
MESSAGE_TAB_NODES = ("BatchMessagesLikeTab", "BatchMessagesCommentTab")


def command(*args, timeout=15):
    return run(list(args), timeout=timeout, binary=True)


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
        # Back, and paste for reading a copied share link.
        if keycode not in (4, 279):
            return False
        command("input", "keyevent", str(keycode))
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
    def __init__(self, tasker, evidence_dir, context=None, controls_exposed=False, progress=None):
        self.tasker = tasker
        self.progress = progress
        # True while a screen reader makes WeChat publish its controls; an empty
        # tree is then a transition, not a page that never exposes controls.
        self.controls_exposed = controls_exposed
        self.controller = tasker.controller
        self.context = context
        if context is None and hasattr(Tasker, "set_reco_image_cache_limit"):
            Tasker.set_reco_image_cache_limit(16)
        self.evidence_dir = Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.sequence = 0
        self.observation = 0
        self.hierarchy_unavailable = False
        self.controls = []
        self.controls_at = 0.0
        self.last_snapshot_lines = None
        self.navigation_stage = None
        # Debug screenshots: enabled by default on Debug APK, off on Release APK
        # (overridable via WECHAT_DUMP_FRAMES or <data_root>/.dump_frames).
        self.dump_frames = resolve_dump_frames(self.evidence_dir)
        self.snapshot_frame = None
        self.snapshot_frame_file = None
        self.controls_frame_file = None
        self._pending_hierarchy_frame = self.evidence_dir / ".pending-hierarchy.jpg"
        self._frame_writer = AsyncFrameWriter() if self.dump_frames else None
        if self.dump_frames:
            schedule_frame_prune(self.evidence_dir)
            atexit.register(self.close)
        sizes = re.findall(r"(\d+)x(\d+)", command("wm", "size").decode())
        if not sizes:
            raise RuntimeError("Physical screen size is unavailable")
        self.width, self.physical_height = map(int, sizes[-1])
        if self.width >= self.physical_height:
            raise RuntimeError("Collection requires portrait orientation")
        self.height = round(self.physical_height * 720 / self.width)
        path = Path(__file__).resolve().parents[1] / "resource/base/pipeline/collect-video.json"
        self.nodes = json.loads(path.read_text(encoding="utf-8"))

    def close(self):
        writer = getattr(self, "_frame_writer", None)
        if writer is not None:
            writer.close(timeout=0.5)
        pending = getattr(self, "_pending_hierarchy_frame", None)
        if pending is not None:
            try:
                pending.unlink(missing_ok=True)
            except OSError:
                pass

    def _invalidate_controls(self):
        self.controls = []
        self.controls_at = 0.0
        self.controls_frame_file = None

    def _has_fresh_controls(self, max_age=2.5):
        controls = getattr(self, "controls", None)
        controls_at = getattr(self, "controls_at", 0.0)
        return bool(controls and controls_at > 0 and 0 <= time.monotonic() - controls_at <= max_age)

    # vivo V2131A needs 1.5-3.1 s for a dump and ~4-5.8 s on large drawers/profiles.
    def hierarchy(self, timeout=8, force=False):
        if self.tasker.stopping:
            raise InterruptedError("Collection was stopped")
        if self.hierarchy_unavailable and not force:
            error = HierarchyUnavailable("Empty hierarchy already observed on this page; probe skipped")
            error.skipped = True
            raise error
        deadline = time.monotonic() + timeout
        try:
            foreground(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise HierarchyUnavailable("Hierarchy foreground check timed out") from error
        helper = Path(__file__).with_name("ui-dump.dex")
        if not helper.is_file():
            raise RuntimeError("UI hierarchy helper is missing; reinstall plugin resources")
        shot_path = None
        if getattr(self, "dump_frames", False):
            shot_path = self._pending_hierarchy_frame
            try:
                shot_path.unlink(missing_ok=True)
            except OSError:
                pass
        with tempfile.TemporaryDirectory(prefix="wechat-ui-") as directory:
            target = Path(directory) / "window.xml"
            cmd = ["env", "CLASSPATH=" + str(helper), "app_process", "/system/bin", "xhs.UiDump", str(target)]
            if shot_path is not None:
                cmd.extend(["--screenshot", str(shot_path)])
            try:
                run(cmd, timeout=max(0, deadline - time.monotonic()), stopped=lambda: self.tasker.stopping)
                xml = target.read_text(encoding="utf-8")
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as error:
                raise HierarchyUnavailable("Hierarchy is unavailable: " + type(error).__name__) from error
        if self.tasker.stopping:
            raise InterruptedError("Collection was stopped")
        try:
            foreground(timeout=max(min(1.0, timeout), deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise HierarchyUnavailable("Hierarchy foreground confirmation timed out") from error
        captured_shot = None
        if shot_path is not None:
            try:
                if shot_path.is_file() and shot_path.stat().st_size > 0:
                    captured_shot = shot_path
            except OSError:
                captured_shot = None
        try:
            lines = self.annotate(text_lines(xml, self.width, self.physical_height, containers=True), "hierarchy")
            # Icons and empty containers are not page text, but locate controls.
            self.controls = lines
            self.controls_at = time.monotonic()
            self.controls_frame_file = captured_shot
            if captured_shot is not None:
                self.snapshot_frame_file = captured_shot
            return lines
        except HierarchyUnavailable:
            self._invalidate_controls()
            self.hierarchy_unavailable = not self.controls_exposed
            if captured_shot is not None:
                self.snapshot_frame_file = captured_shot
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
        if name in MESSAGE_TAB_NODES:
            roi = [120, 60, 400, 100]
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
            self.snapshot_frame = self.image
            spec = {key: node[key] for key in ("all_of", "box_index") if key in node}
            result = self.tasker.post_recognition("And", JAnd(**spec), self.image).wait().get()
            detail = result.nodes[-1].recognition if result and result.nodes else None
        if detail is None or not detail.hit or detail.box is None:
            return None
        box = detail.box
        role, text = {"BatchDiscoverTab": ("discover", "发现"),
                      "BatchChannelsEntry": ("channels", "视频号"),
                      "BatchRecommended": ("recommend", "推荐"),
                      "BatchMessagesLikeTab": ("message_like", "点赞"),
                      "BatchMessagesCommentTab": ("message_comment", "评论")}[name]
        return {"text": text, "box": [box.x, box.y, box.w, box.h],
                "navigation": role, "templateNode": name}

    def message_tabs(self, label, lines, measured):
        """Fill in 点赞/评论 message-center tabs that are drawn as icons instead of text.

        The creator 视频号消息 center labels 评论 but may draw 点赞 as a heart; the
        viewer 消息 page (accounts without a Channels account) draws both tabs as
        icons (heart, speech bubble). Tabs are only added when both are present,
        so a lone heart (e.g. the feed's 朋友♡) never fakes a message center.
        """
        top = lambda line: line["box"][1] < 160
        has_like = find(lines, MESSAGE_LIKE_TAB, top)
        has_comment = find(lines, MESSAGE_COMMENT_TAB, top)
        if has_like and has_comment:
            return []
        if not (label.startswith("messages") or has_like or has_comment):
            return []
        if not (has_like or has_comment) and label not in (
            "messages-center", "messages-comments", "messages-after-like", "messages-after-reply"
        ):
            from interactions import classify_page_state
            if classify_page_state(page_lines(lines), getattr(self, "height", 1605)) != "unknown":
                return []
        like = None if has_like else measured("template", lambda: self.match_navigation("BatchMessagesLikeTab"))
        if not (has_like or like):
            return []
        comment = None if has_comment else measured(
            "template", lambda: self.match_navigation("BatchMessagesCommentTab"))
        if not (has_comment or comment):
            return []
        return [tab for tab in (like, comment) if tab]

    def snapshot(self, label, author=None):
        if self.progress is not None:
            self.progress(label)
        attempts = []
        self.snapshot_frame = None
        self.snapshot_frame_file = None

        def measured(source, read):
            started = time.monotonic_ns()
            attempt = {"source": source}
            try:
                lines = read()
                return self._acknowledge_channels_intro(lines) if source == "hierarchy" else lines
            except HierarchyUnavailable as error:
                attempt["reason"] = str(error)
                raise
            finally:
                attempt["durationMs"] = (time.monotonic_ns() - started) // 1_000_000
                attempts.append(attempt)

        def read_hierarchy():
            if self._has_fresh_controls(max_age=0.6):
                raw = self.controls
                if getattr(self, "controls_frame_file", None) is not None:
                    self.snapshot_frame_file = self.controls_frame_file
                attempts.append({"source": "hierarchy", "durationMs": 0, "reused": True})
            else:
                raw = measured("hierarchy", self.hierarchy)
            tabs = self.message_tabs(label, raw, measured)
            return [*raw, *self.annotate(tabs, "template")] if tabs else raw

        lines, source = choose_snapshot(label, self.height, author,
            read_hierarchy,
            lambda: measured("template", lambda: self.template_navigation(label)),
            lambda: measured("ocr", lambda: self.ocr_snapshot(label)),
            regional_ocr=lambda roi: measured("ocr-region", lambda: self.ocr_snapshot(label, roi)))
        if source == "hierarchy" and any(line.get("templateNode") for line in lines):
            source = "mixed"
        corrected = sum(1 for line in lines if "ocrText" in line)
        if corrected:
            attempts.append({"source": "hierarchy-correction", "correctedLines": corrected})
        if source != "hierarchy":
            tabs = self.message_tabs(label, lines, measured)
            if tabs:
                lines = [*lines, *self.annotate(tabs, "template")]
                source = "mixed"
        self.record_snapshot(label, lines, source, attempts, author)
        self.last_snapshot_lines = lines
        return lines

    def record_snapshot(self, label, lines, source, attempts, author=None):
        from hierarchy import field_evidence
        self.sequence += 1
        evidence = {"label": label, "width": 720, "height": self.height,
                    "roi": navigation_roi(label, self.height), "source": source, "attempts": attempts, "lines": lines,
                    "fields": field_evidence(lines, label, author)}
        (self.evidence_dir / f"{self.sequence:03d}-{label}.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.dump_frames:
            self._persist_snapshot_frame(label)
        print(json.dumps({"kind": "recognitionTiming", "label": label, "source": source,
                          "attempts": attempts}, ensure_ascii=False), flush=True)

    def _persist_snapshot_frame(self, label):
        """Save the debug screenshot via Path A (UiDump JPEG) or Path B (async OCR PNG)."""
        try:
            stem = f"{self.sequence:03d}-{label}"
            frame_file = getattr(self, "snapshot_frame_file", None)
            if frame_file is not None:
                pending = getattr(self, "_pending_hierarchy_frame", None)
                consume = pending is not None and frame_file == pending
                saved = materialize_frame_file(
                    frame_file,
                    self.evidence_dir / f"{stem}.jpg",
                    consume_source=consume,
                )
                if saved is not None:
                    if getattr(self, "controls_frame_file", None) == frame_file:
                        self.controls_frame_file = saved
                    self.snapshot_frame_file = saved
                    return
            frame = self.snapshot_frame
            if frame is None:
                frame = self.debug_screencap()
            if frame is not None:
                writer = getattr(self, "_frame_writer", None)
                if writer is None:
                    writer = AsyncFrameWriter()
                    self._frame_writer = writer
                writer.submit_array(frame, self.evidence_dir / f"{stem}.png")
        except Exception:
            # Debug screenshot persistence must never interrupt collection or interaction tasks.
            pass

    def ocr_snapshot(self, label, roi=None):
        if self.tasker.stopping:
            raise RuntimeError("Collection was stopped")
        foreground()
        job = self.controller.post_screencap().wait()
        if not job.succeeded:
            raise RuntimeError("Screenshot failed")
        image = job.get()
        self.image = image
        self.height = image.shape[0]
        roi = navigation_roi(label, self.height) if roi is None else roi
        ocr_image = image
        if not self.context and label.startswith(("comments", "messages")):
            # 将橙色视频号徽章的颜色处理为白色，保持评论昵称的识别范围。
            ocr_image = image.copy()
            b, g, r = ocr_image[:, :, 0].astype("int16"), ocr_image[:, :, 1].astype("int16"), ocr_image[:, :, 2].astype("int16")
            orange = (r > 195) & (g > 105) & (g < 210) & (b < 100) & ((r - b) > 110)
            if orange.any():
                ocr_image[orange] = 255
        if self.context:
            # 单次识别保留真实的空结果，识别流程无法启动时立即报错。
            detail = self.context.run_recognition_direct("OCR", JOCR(threshold=0.55, roi=roi), ocr_image)
        else:
            result = self.tasker.post_recognition("OCR", JOCR(threshold=0.55, roi=roi), ocr_image).wait().get()
            detail = result.nodes[0].recognition if result and result.nodes else None
        self.snapshot_frame = image
        if detail is None:
            raise RuntimeError("OCR 未能启动识别")
        # Native results retain every OCR box, including ones not chosen as best_result.
        lines = detail.raw_detail["all"]
        lines = [line for line in lines if line.get("score", 0) >= 0.55 and line.get("text", "").strip()]
        return self.annotate(lines, "ocr")

    def debug_screencap(self):
        if self.context:
            return None
        job = self.controller.post_screencap().wait()
        return job.get() if job.succeeded else None

    @contextmanager
    def prepare_basic_info(self):
        if self.controls_exposed and not self.hierarchy_unavailable:
            yield None
            return
        started = time.monotonic_ns()
        with prepared_reader(Path(__file__).with_name("probe.py"), stopped=lambda: self.tasker.stopping) as reader:
            print(json.dumps({"kind": "readinessPreparationTiming", "durationMs":
                              (time.monotonic_ns() - started) // 1_000_000}), flush=True)
            yield reader

    def wait_basic_info(self, timeout, reader):
        started = time.monotonic_ns()
        source, outcome = None, "failed"

        def bounded_ocr(remaining):
            if reader is not None:
                result = reader(remaining)
                return self.annotate(result["lines"], result["source"])
            return self.ocr_snapshot("author-basic-info", [0, self.height // 4, 240, self.height // 2])

        try:
            lines, source = wait_basic_info(
                lambda remaining: page_lines(self.hierarchy(timeout=remaining)),
                bounded_ocr,
                timeout,
            )
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
        # The menu button is a control described as 设置 beside the name.
        controls = []
        if self.controls_exposed:
            if self._has_fresh_controls():
                controls = self.controls
            else:
                try:
                    controls = self.hierarchy(force=True)
                except HierarchyUnavailable:
                    pass
        button = next((line for line in controls if line.get("text") == "设置" and line.get("selector")
                       and line["box"][0] >= roi[0] and roi[1] <= line["box"][1] + line["box"][3] / 2 <= roi[1] + roi[3]),
                      None)
        if button:
            self.click(button)
            return
        if self.context:
            result = self.context.run_task("BatchProfileMore", {
                "BatchProfileMore": {"recognition": "TemplateMatch", "template": ["profile-more.png", "profile-more-dark.png"],
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
                template=["profile-more.png", "profile-more-dark.png"], roi=roi, threshold=[0.88, 0.88],
            ), self.image).wait().get()
            detail = result.nodes[0].recognition if result and result.nodes else None
        if detail is None or not detail.hit or detail.box is None:
            raise ValueError("Author profile menu icon was not recognized")
        box = detail.box
        self.click({"box": [box.x, box.y, box.w, box.h]})

    def _acknowledge_channels_intro(self, controls):
        introduction = channels_intro_acknowledgement(controls)
        if introduction is None:
            return controls
        self.record_snapshot("navigation-intro", controls, "hierarchy", [])
        navigation_stage = self.navigation_stage
        self.click(introduction, resolve=channels_intro_acknowledgement)
        self.navigation_stage = navigation_stage
        deadline = time.monotonic() + 10
        while True:
            controls = self.hierarchy(force=True)
            if channels_intro_acknowledgement(controls) is None:
                self.record_snapshot("navigation-intro-dismissed", controls, "hierarchy", [])
                return controls
            if time.monotonic() >= deadline:
                self.record_snapshot("navigation-intro-unavailable", controls, "hierarchy", [])
                raise RuntimeError("视频号介绍提示未在10秒内关闭")
            time.sleep(0.25)

    def _navigation_control(self, previous, controls):
        role = previous["navigation"]
        label = {"discover": "发现", "channels": "视频号", "recommend": "推荐"}[role]
        deadline = time.monotonic() + 10
        intro_acknowledged = False
        while True:
            introduction = channels_intro_acknowledgement(controls)
            if introduction is not None:
                if intro_acknowledged:
                    raise RuntimeError("视频号介绍提示重复出现")
                controls = self._acknowledge_channels_intro(controls)
                intro_acknowledged = True
                deadline = time.monotonic() + 10
            text = [item for item in controls if item.get("selector") and not item.get("container")]
            matches = [item for item in text if re.fullmatch(re.escape(label) + r"(?:[，,（(].*)?", item["text"])]
            if role == "discover":
                matches = [item for item in matches if item["box"][1] > self.height - 180]
                ready = bool(find(text, r"^通讯录$"))
            elif role == "channels":
                ready = bool(find(text, r"^发现$", lambda item: item["box"][1] < 250))
            else:
                matches = [item for item in matches if item["box"][1] < 250]
                ready = bool(find(text, r"^[<＜《‹«]?\s*关注", lambda item: item["box"][1] < 250)
                             and find(text, r"^朋友", lambda item: item["box"][1] < 250))
            if ready and len(matches) == 1:
                self.record_snapshot("navigation-control-resolved", text, "hierarchy", [])
                return matches[0]
            if time.monotonic() >= deadline:
                self.record_snapshot("navigation-control-unavailable", text, "hierarchy", [])
                raise RuntimeError("导航控件未在10秒内就绪")
            time.sleep(0.25)
            controls = self.hierarchy(force=True)

    def click(self, line, *, resolve=None):
        if self.tasker.stopping:
            raise RuntimeError("Collection was stopped")
        navigation = line.get("navigation")
        if resolve is not None:
            controls = self.controls if self._has_fresh_controls(max_age=2.0) else self.hierarchy(force=True)
            resolved = resolve(controls)
            if resolved is None and controls is self.controls:
                resolved = resolve(self.hierarchy(force=True))
            line = resolved
            if line is None:
                return False
            print(json.dumps({"kind": "resolvedControlClick", "resource": line.get("selector", {}).get("resource", ""),
                              "box": line["box"]}, ensure_ascii=False), flush=True)
        elif line.get("selector"):
            reused = False
            if self._has_fresh_controls():
                try:
                    if navigation in ("discover", "channels", "recommend"):
                        line = self._navigation_control(line, self.controls)
                    else:
                        line = refresh_target(self.controls, line)
                    reused = True
                except RuntimeError:
                    reused = False
            if not reused:
                controls = None
                for timeout in (6, 8):
                    try:
                        controls = self.hierarchy(timeout=timeout, force=True)
                        break
                    except HierarchyUnavailable:
                        time.sleep(0.5)
                # A control that is gone or ambiguous still sends no input. Only when the
                # dump itself keeps timing out (vivo) is the position just read used.
                if controls is not None:
                    line = self._navigation_control(line, controls) if navigation in ("discover", "channels", "recommend") else refresh_target(controls, line)
        elif line.get("templateNode"):
            current = self.match_navigation(line["templateNode"])
            if current:
                if max(abs(a - b) for a, b in zip(current["box"], line["box"])) > 16:
                    raise RuntimeError("Navigation control changed before click; no input was sent")
                line = current
            elif line["templateNode"] != "BatchRecommended":
                raise RuntimeError("Navigation control changed before click; no input was sent")
        foreground()
        x, y, width, height = line["box"]
        if not self.controller.post_click(x + width // 2, y + height // 2).wait().succeeded:
            raise RuntimeError("Click failed")
        self._invalidate_controls()
        self.navigation_stage = ({"discover": "BatchChannelsEntry", "channels": "BatchRecommended"}.get(navigation)
                                 if navigation else {"发现": "BatchChannelsEntry", "视频号": "BatchRecommended"}.get(line.get("text")))
        time.sleep(0.6)
        return True

    def back(self):
        if self.tasker.stopping:
            raise RuntimeError("Collection was stopped")
        foreground()
        if not self.controller.post_click_key(4).wait().succeeded:
            raise RuntimeError("Back failed")
        self._invalidate_controls()
        self.navigation_stage = None
        time.sleep(0.95)

    def restore_work_header(self, lines):
        # A long fast pull dragged the whole sheet closed once the list hit its top
        # (Hebe-BD00 run 4675d961); pull back in short, slow steps instead.
        start, end = self._comment_swipe(lines)
        foreground()
        if not self.controller.post_swipe(360, end, 360, start, COMMENT_SWIPE_MS).wait().succeeded:
            raise RuntimeError("Details header recovery failed")
        self._invalidate_controls()
        self.navigation_stage = None
        time.sleep(0.6)

    def _comment_swipe(self, lines):
        heading = find_comment_drawer_heading(lines, self.height)
        if not heading:
            raise ValueError("Cannot scroll an unclassified comment drawer")
        height = line_height(lines)
        top = int(heading["box"][1] + heading["box"][3] + height * 3)
        start = min(int(self.height - 140), int(self.height * 0.84))
        if start <= top + int(height * 2):
            raise ValueError("Comment drawer has no safe scroll region")
        # WeChat adds ~250px of momentum even to slow swipes (Hebe-BD00: a 560px,
        # 900ms swipe moved 804px), so swipe half the band to keep overlap.
        return start, start - max(int((start - top) * 0.5), int(height * 4))

    def scroll_comments(self, lines):
        start, end = self._comment_swipe(lines)
        foreground()
        if not self.controller.post_swipe(360, start, 360, end, COMMENT_SWIPE_MS).wait().succeeded:
            raise RuntimeError("Comment drawer scroll failed")
        self._invalidate_controls()
        self.navigation_stage = None
        time.sleep(0.7)

    def scroll_comments_back(self, lines):
        """Undo part of an overshooting scroll so the next page overlaps the last one."""
        start, end = self._comment_swipe(lines)
        foreground()
        if not self.controller.post_swipe(360, end, 360, end + (start - end) // 2,
                                          COMMENT_SWIPE_MS).wait().succeeded:
            raise RuntimeError("Comment drawer scroll back failed")
        self._invalidate_controls()
        self.navigation_stage = None
        time.sleep(0.7)

    def stopped(self):
        return bool(getattr(self.tasker, "stopping", False))

    def capture_frame(self):
        if self.stopped():
            raise InterruptedError("Collection was stopped")
        foreground()
        job = self.controller.post_screencap().wait()
        if not job.succeeded:
            raise RuntimeError("Screenshot failed")
        image = job.get()
        self.image = image
        self.height = image.shape[0]
        return image

    def set_text(self, text, timeout=20):
        if self.stopped():
            raise InterruptedError("Collection was stopped")
        foreground()
        helper = Path(__file__).with_name("ui-dump.dex")
        if not helper.is_file():
            raise RuntimeError("UI hierarchy helper is missing; reinstall plugin resources")
        self._invalidate_controls()
        cmd = ["env", "CLASSPATH=" + str(helper), "app_process", "/system/bin", "xhs.UiDump", "--set-text", str(text)]
        try:
            run(cmd, timeout=timeout, stopped=self.stopped)
        except subprocess.TimeoutExpired:
            # 超时后的输入结果由当前控件确认，避免再次输入产生重复文字。
            controls = self.hierarchy(force=True)
            edits = [line for line in controls if line.get("selector", {}).get("class", "").endswith("EditText")]
            if len(edits) != 1 or edits[0].get("selector", {}).get("value") != str(text):
                raise
        time.sleep(0.5)

    def controls_now(self):
        """Fresh controls, icons included; empty when WeChat hides its controls."""
        if not self.controls_exposed:
            return []
        if self._has_fresh_controls():
            return self.controls
        try:
            return self.hierarchy(force=True)
        except HierarchyUnavailable:
            return []

    def paste_text(self, text, timeout=20):
        if self.stopped():
            raise InterruptedError("任务已停止")
        if not isinstance(text, str) or not text:
            raise ValueError("粘贴内容不能为空")
        foreground()
        helper = Path(__file__).with_name("ui-dump.dex")
        if not helper.is_file():
            raise RuntimeError("缺少 UI 采集组件")
        self._invalidate_controls()
        run(["env", "CLASSPATH=" + str(helper), "app_process", "/system/bin",
             "xhs.UiDump", "--paste-text", text], timeout=timeout, stopped=self.stopped)

    def read_clipboard(self, timeout=8):
        """Read system clipboard text and timestamp directly via shell IClipboard."""
        if self.stopped():
            raise InterruptedError("Collection was stopped")
        helper = Path(__file__).with_name("ui-dump.dex")
        if not helper.is_file():
            return None
        try:
            out = run(["env", "CLASSPATH=" + str(helper), "app_process", "/system/bin",
                       "xhs.UiDump", "--get-clipboard"], timeout=timeout, stopped=self.stopped)
            raw = next((item.strip() for item in reversed((out or "").splitlines()) if item.strip().startswith("{")), "")
            return json.loads(raw) if raw else None
        except Exception:
            return None

    def type_ascii(self, text):
        """通过系统按键输入 ASCII 搜索文字。"""
        if self.stopped():
            raise InterruptedError("Collection was stopped")
        if not re.fullmatch(r"[A-Za-z0-9:/._~?=#-]+", text):
            raise ValueError("type_ascii only types plain link characters")
        foreground()
        self._invalidate_controls()
        command("input", "text", text)
        time.sleep(0.5)

    def hide_keyboard(self):
        """Close the input method if it is showing; back would otherwise leave the page."""
        # The keyboard can open a moment after sending, so it is checked twice.
        for check in range(2):
            state = command("dumpsys", "input_method").decode("utf-8", "replace")
            if re.search(r"\bmInputShown=true\b", state):
                self.back()
                return True
            if check == 0:
                time.sleep(0.25)
        return False

    def clear_text(self, count=80):
        """Empty the focused input: the set-text helper refuses empty text, so
        the cursor is moved to the end (MOVE_END) and the text deleted (DEL)."""
        if self.stopped():
            raise InterruptedError("Collection was stopped")
        foreground()
        self._invalidate_controls()
        command("input", "keyevent", "123")
        command("input", "keyevent", *["67"] * count, timeout=30)
        time.sleep(0.4)

    def key(self, keycode):
        if self.stopped():
            raise InterruptedError("Collection was stopped")
        foreground()
        if not self.controller.post_click_key(keycode).wait().succeeded:
            # Some controllers only send back; the agent runs as shell and can inject keys itself.
            command("input", "keyevent", str(keycode))
        self._invalidate_controls()
        self.navigation_stage = None
        time.sleep(0.3)

    def swipe(self, x1, y1, x2, y2, duration):
        if self.stopped():
            raise InterruptedError("Collection was stopped")
        foreground()
        if not self.controller.post_swipe(x1, y1, x2, y2, duration).wait().succeeded:
            raise RuntimeError("Swipe failed")
        self._invalidate_controls()
        self.navigation_stage = None
        time.sleep(0.8)

    def scroll_feed_next(self):
        if self.stopped():
            raise InterruptedError("Collection was stopped")
        foreground()
        start = int(self.height * 0.78)
        end = int(self.height * 0.22)
        if not self.controller.post_swipe(360, start, 360, end, 320).wait().succeeded:
            raise RuntimeError("Feed scroll next failed")
        self._invalidate_controls()
        self.navigation_stage = None
        time.sleep(0.9)

    def scroll_feed_prev(self):
        if self.stopped():
            raise InterruptedError("Collection was stopped")
        foreground()
        start = int(self.height * 0.22)
        end = int(self.height * 0.78)
        if not self.controller.post_swipe(360, start, 360, end, 320).wait().succeeded:
            raise RuntimeError("Feed scroll prev failed")
        self._invalidate_controls()
        self.navigation_stage = None
        time.sleep(0.9)

    def _wake_screen_if_needed(self):
        try:
            power = command("dumpsys", "power", timeout=5).decode("utf-8", "replace")
            if "mWakefulness=Awake" not in power:
                command("input", "keyevent", "224", timeout=5)
                time.sleep(0.3)
        except Exception:
            pass

    def _launch_wechat_attempt(self, attempt, *, reset_to_home):
        if attempt == 0:
            if reset_to_home:
                # FLAG_ACTIVITY_NEW_TASK | FLAG_ACTIVITY_CLEAR_TOP: LauncherUI is brought
                # back and every page opened on top of it is finished.
                command("am", "start", "-f", "0x14000000", "-n", "com.tencent.mm/.ui.LauncherUI")
            else:
                command("am", "start", "-n", "com.tencent.mm/.ui.LauncherUI")
            return
        if attempt == 1:
            try:
                if self.controller.post_start_app("com.tencent.mm").wait().succeeded:
                    return
            except Exception:
                pass
            helper = Path(__file__).with_name("ui-dump.dex")
            if not reset_to_home and helper.is_file():
                try:
                    run(
                        ["env", "CLASSPATH=" + str(helper), "app_process", "/system/bin", "xhs.UiDump", "--resume-wechat"],
                        timeout=5,
                        stopped=self.stopped,
                    )
                    return
                except Exception:
                    pass
        command("am", "start", "-f", "0x14000000", "-n", "com.tencent.mm/.ui.LauncherUI")

    def ensure_wechat_foreground(self, *, reset_to_home=False, timeout=15.0, emit=None):
        """Step 1 guard for every task: ensure WeChat is awake and in the foreground.

        Checks the real foreground Activity (bypassing the 0.4s cache). If WeChat is not
        in the foreground (or ``reset_to_home=True``), automatically launches / brings
        WeChat to the foreground with retry escalation until confirmed.
        """
        if self.stopped():
            raise InterruptedError("Collection was stopped")
        started = time.monotonic()
        invalidate_foreground_cache()
        was_in_front = is_wechat_foreground(timeout=min(3.0, timeout), force=True)
        if was_in_front and not reset_to_home:
            event = {
                "kind": "ensureWechatForeground",
                "wasInForeground": True,
                "launched": False,
                "durationMs": round((time.monotonic() - started) * 1000),
            }
            if emit is not None:
                emit(event)
            return True

        self.hierarchy_unavailable = False
        self._invalidate_controls()
        self.navigation_stage = None
        self._wake_screen_if_needed()

        deadline = time.monotonic() + timeout
        self.startup_deadline = deadline
        retry_marks = (0.0, 3.5, 7.5)
        next_attempt = 0
        last_error = None

        while True:
            if self.stopped():
                raise InterruptedError("Collection was stopped")
            elapsed = time.monotonic() - started
            if next_attempt < len(retry_marks) and elapsed >= retry_marks[next_attempt]:
                try:
                    self._launch_wechat_attempt(next_attempt, reset_to_home=reset_to_home)
                except Exception as exc:
                    last_error = str(exc)
                next_attempt += 1
            remaining = deadline - time.monotonic()
            try:
                foreground(timeout=min(2.0, max(0.2, remaining)), force=True)
                event = {
                    "kind": "ensureWechatForeground",
                    "wasInForeground": was_in_front,
                    "launched": True,
                    "attempts": next_attempt,
                    "resetToHome": reset_to_home,
                    "durationMs": round((time.monotonic() - started) * 1000),
                }
                if emit is not None:
                    emit(event)
                return True
            except RuntimeError as exc:
                last_error = str(exc)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    detail = f"（{last_error}）" if last_error else ""
                    raise RuntimeError(f"自动打开微信超时（{int(timeout)}秒），微信未进入前台{detail}")
                time.sleep(min(0.25, remaining))

    def start_wechat_home(self):
        """Open WeChat on its main tabs, closing the pages an earlier run left above them."""
        self.ensure_wechat_foreground(reset_to_home=True, timeout=15.0)

    def start_wechat(self):
        """Ensure WeChat is open and in the foreground."""
        self.ensure_wechat_foreground(reset_to_home=False, timeout=15.0)


def phone_ui(root, evidence_dir, controls_exposed=False):
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
    return MaaUi(tasker, evidence_dir, controls_exposed=controls_exposed)
