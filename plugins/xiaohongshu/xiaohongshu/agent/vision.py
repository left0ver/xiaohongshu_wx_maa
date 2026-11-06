"""Reusable OpenCV / visual frame-stability and ROI analysis functions."""

import os
from pathlib import Path
import re
import sys
import tempfile
import time

from commands import run

try:
    import numpy as np
except ImportError:  # pragma: no cover - desktop fallback when numpy is absent
    np = None


def _maa_pipeline_cls(name):
    module = sys.modules.get("maa.pipeline")
    return getattr(module, name, None) if module is not None else None


def load_raw_screencap(source, step=1):
    if np is None:
        return None
    if isinstance(source, (bytes, bytearray, memoryview)):
        if len(source) < 12:
            return None
        header = np.frombuffer(source, dtype="<u4", count=2)
        width, height = int(header[0]), int(header[1])
        pixel_bytes = width * height * 4
        if width <= 0 or height <= 0 or len(source) < pixel_bytes + 8:
            return None
        offset = len(source) - pixel_bytes
        rgba = np.frombuffer(source, dtype=np.uint8, count=pixel_bytes, offset=offset).reshape((height, width, 4))
    else:
        path = Path(source)
        if not path.is_file():
            return None
        total = path.stat().st_size
        if total < 12:
            return None
        header = np.fromfile(path, dtype="<u4", count=2)
        if header.size < 2:
            return None
        width, height = int(header[0]), int(header[1])
        pixel_bytes = width * height * 4
        if width <= 0 or height <= 0 or total < pixel_bytes + 8:
            return None
        offset = total - pixel_bytes
        rgba = np.fromfile(path, dtype=np.uint8, count=pixel_bytes, offset=offset).reshape((height, width, 4))
    stride = max(1, int(step))
    if stride > 1:
        rgba = rgba[::stride, ::stride]
    return rgba[..., 2::-1]


def capture_raw_frame(raw_path=None, step=1, stopped=lambda: False):
    if np is None or not hasattr(sys, "getandroidapilevel"):
        return None
    target = (Path(raw_path) if raw_path is not None
              else Path(tempfile.gettempdir()) / f"xhs-frame-{os.getpid()}.raw")
    run(["screencap", str(target)], timeout=5, stopped=stopped)
    return load_raw_screencap(target, step=step)


def frame_shape(frame):
    if frame is None:
        return None
    shape = getattr(frame, "shape", None)
    if shape is not None and len(shape) >= 2:
        return int(shape[0]), int(shape[1])
    if isinstance(frame, (list, tuple)) and frame and isinstance(frame[0], (list, tuple)):
        return len(frame), len(frame[0])
    return None


def clamp_roi(frame, box=None):
    shape = frame_shape(frame)
    if shape is None:
        raise ValueError("Frame does not expose 2D image dimensions")
    height, width = shape
    if box is None:
        return 0, 0, width, height
    left, top, right, bottom = (int(v) for v in box)
    left = max(0, min(width, left))
    right = max(0, min(width, right))
    top = max(0, min(height, top))
    bottom = max(0, min(height, bottom))
    if right <= left or bottom <= top:
        raise ValueError(f"ROI {box} is empty after clamping to {(width, height)}")
    return left, top, right, bottom


def box_to_maa_roi(box):
    if box is None:
        return (0, 0, 0, 0)
    left, top, right, bottom = (int(v) for v in box)
    if right <= left or bottom <= top:
        raise ValueError(f"Invalid bounds for Maa ROI: {box}")
    return (left, top, right - left, bottom - top)


def maa_box_to_bounds(box):
    if box is None:
        return None
    if hasattr(box, "x") and hasattr(box, "y") and hasattr(box, "w") and hasattr(box, "h"):
        return (int(box.x), int(box.y), int(box.x + box.w), int(box.y + box.h))
    if isinstance(box, dict):
        x = int(box.get("x", 0))
        y = int(box.get("y", 0))
        w = int(box.get("w", 0))
        h = int(box.get("h", 0))
        return (x, y, x + w, y + h) if w > 0 and h > 0 else None
    if isinstance(box, (list, tuple)) and len(box) == 4:
        x, y, w, h = (int(v) for v in box)
        return (x, y, x + w, y + h) if w > 0 and h > 0 else None
    return None


def crop_roi(frame, box=None):
    left, top, right, bottom = clamp_roi(frame, box)
    if getattr(frame, "shape", None) is not None:
        return frame[top:bottom, left:right]
    return [row[left:right] for row in frame[top:bottom]]


def _pixel_values(patch):
    for row in patch:
        for cell in row:
            if isinstance(cell, (list, tuple)):
                for channel in cell:
                    yield float(channel)
            else:
                yield float(cell)


def frame_diff_score(frame_a, frame_b, roi=None):
    shape_a = frame_shape(frame_a)
    shape_b = frame_shape(frame_b)
    if shape_a is None or shape_b is None or shape_a != shape_b:
        return float("inf")
    patch_a = crop_roi(frame_a, roi)
    patch_b = crop_roi(frame_b, roi)
    if np is not None and isinstance(patch_a, np.ndarray) and isinstance(patch_b, np.ndarray):
        if patch_a.size == 0 or patch_a.shape != patch_b.shape:
            return float("inf")
        return float(np.abs(patch_a.astype(np.int16) - patch_b.astype(np.int16)).mean())
    values_a = list(_pixel_values(patch_a))
    values_b = list(_pixel_values(patch_b))
    if not values_a or len(values_a) != len(values_b):
        return float("inf")
    return sum(abs(a - b) for a, b in zip(values_a, values_b)) / len(values_a)


def is_frame_stable(frame_a, frame_b, roi=None, threshold=2.0):
    return frame_diff_score(frame_a, frame_b, roi=roi) <= float(threshold)


def wait_frame_stable(screencap, roi=None, baseline=None, threshold=2.0,
                      timeout=2.0, interval=0.06, stopped=lambda: False):
    deadline = time.monotonic() + max(0.0, float(timeout))
    previous = baseline
    samples = 0
    last_diff = float("inf")
    while True:
        if stopped():
            raise InterruptedError("Task stopped")
        current = screencap()
        samples += 1
        if current is not None and previous is not None:
            last_diff = frame_diff_score(previous, current, roi=roi)
            if last_diff <= float(threshold):
                return dict(stable=True, frame=current, diff=last_diff, samples=samples)
        previous = current
        if time.monotonic() >= deadline:
            return dict(stable=False, frame=current, diff=last_diff, samples=samples)
        if interval > 0:
            time.sleep(interval)


def wait_transition_settled(screencap, baseline=None, roi=None, change_threshold=3.5,
                            stable_threshold=2.0, timeout=2.0, interval=0.06,
                            stopped=lambda: False):
    now = time.monotonic()
    deadline = now + max(0.0, float(timeout))
    change_deadline = now + min(max(0.0, float(timeout)) * 0.4, 0.45)
    changed = baseline is None
    previous = baseline
    samples = 0
    last_diff = float("inf")
    while True:
        if stopped():
            raise InterruptedError("Task stopped")
        current = screencap()
        samples += 1
        if current is not None and previous is not None:
            if not changed:
                delta = frame_diff_score(baseline, current, roi=roi)
                if delta >= float(change_threshold) or time.monotonic() >= change_deadline:
                    changed = True
            else:
                last_diff = frame_diff_score(previous, current, roi=roi)
                if last_diff <= float(stable_threshold):
                    return dict(changed=changed, stable=True, frame=current, diff=last_diff, samples=samples)
        previous = current
        if time.monotonic() >= deadline:
            return dict(changed=changed, stable=False, frame=current, diff=last_diff, samples=samples)
        if interval > 0:
            time.sleep(interval)


def _bgr_to_hsv_opencv(patch):
    if np is not None and isinstance(patch, np.ndarray):
        arr = patch.astype(np.float32) / 255.0
        b, g, r = arr[..., 0], arr[..., 1], arr[..., 2]
        cmax = np.maximum(np.maximum(r, g), b)
        cmin = np.minimum(np.minimum(r, g), b)
        delta = cmax - cmin
        hue = np.zeros_like(cmax)
        nonzero = delta > 1e-6
        r_max = nonzero & (cmax == r)
        g_max = nonzero & (cmax == g) & ~r_max
        b_max = nonzero & ~r_max & ~g_max
        hue[r_max] = (60.0 * ((g[r_max] - b[r_max]) / delta[r_max]) + 360.0) % 360.0
        hue[g_max] = 60.0 * ((b[g_max] - r[g_max]) / delta[g_max]) + 120.0
        hue[b_max] = 60.0 * ((r[b_max] - g[b_max]) / delta[b_max]) + 240.0
        sat = np.where(cmax > 1e-6, delta / np.maximum(cmax, 1e-6), 0.0)
        h_cv = np.round(hue / 2.0).astype(np.int16)
        s_cv = np.round(sat * 255.0).astype(np.int16)
        v_cv = np.round(cmax * 255.0).astype(np.int16)
        return h_cv, s_cv, v_cv
    h_list, s_list, v_list = [], [], []
    for row in patch:
        for b_raw, g_raw, r_raw in row:
            b, g, r = b_raw / 255.0, g_raw / 255.0, r_raw / 255.0
            cmax = max(r, g, b)
            cmin = min(r, g, b)
            delta = cmax - cmin
            if delta <= 1e-6:
                hue = 0.0
            elif cmax == r:
                hue = (60.0 * ((g - b) / delta) + 360.0) % 360.0
            elif cmax == g:
                hue = 60.0 * ((b - r) / delta) + 120.0
            else:
                hue = 60.0 * ((r - g) / delta) + 240.0
            sat = 0.0 if cmax <= 1e-6 else delta / cmax
            h_list.append(round(hue / 2.0))
            s_list.append(round(sat * 255.0))
            v_list.append(round(cmax * 255.0))
    return h_list, s_list, v_list


def roi_hsv_ratio(frame, box, lower_hsv, upper_hsv):
    patch = crop_roi(frame, box)
    h_low, s_low, v_low = (int(v) for v in lower_hsv)
    h_high, s_high, v_high = (int(v) for v in upper_hsv)
    h_cv, s_cv, v_cv = _bgr_to_hsv_opencv(patch)
    if np is not None and isinstance(h_cv, np.ndarray):
        if h_cv.size == 0:
            return 0.0
        if h_low <= h_high:
            h_mask = (h_cv >= h_low) & (h_cv <= h_high)
        else:
            h_mask = (h_cv >= h_low) | (h_cv <= h_high)
        mask = h_mask & (s_cv >= s_low) & (s_cv <= s_high) & (v_cv >= v_low) & (v_cv <= v_high)
        return float(mask.mean())
    if not h_cv:
        return 0.0
    matched = 0
    for h_val, s_val, v_val in zip(h_cv, s_cv, v_cv):
        h_ok = (h_low <= h_val <= h_high) if h_low <= h_high else (h_val >= h_low or h_val <= h_high)
        if h_ok and s_low <= s_val <= s_high and v_low <= v_val <= v_high:
            matched += 1
    return matched / len(h_cv)


def is_red_heart_active(frame, box, threshold=0.04):
    return roi_hsv_ratio(frame, box, (168, 90, 120), (12, 255, 255)) >= float(threshold)


def is_yellow_star_active(frame, box, threshold=0.04):
    return roi_hsv_ratio(frame, box, (14, 90, 130), (38, 255, 255)) >= float(threshold)


def match_template_roi(recognizer, frame, templates, roi=None, threshold=0.75):
    if recognizer is None or frame is None:
        return None
    template_list = [templates] if isinstance(templates, str) else list(templates)
    thresholds = [float(threshold)] * len(template_list)
    maa_roi = box_to_maa_roi(roi)
    cls = _maa_pipeline_cls("JTemplateMatch")
    param = (cls(template=template_list, roi=maa_roi, threshold=thresholds)
             if cls is not None
             else dict(template=template_list, roi=maa_roi, threshold=thresholds))
    result = recognizer("TemplateMatch", param, frame)
    if not result or not getattr(result, "hit", False):
        return None
    return maa_box_to_bounds(getattr(result, "box", None))


def match_feature_roi(recognizer, frame, templates, roi=None, detector="SIFT", count=4, ratio=0.6):
    if recognizer is None or frame is None:
        return None
    template_list = [templates] if isinstance(templates, str) else list(templates)
    maa_roi = box_to_maa_roi(roi)
    cls = _maa_pipeline_cls("JFeatureMatch")
    param = (cls(template=template_list, roi=maa_roi, detector=detector, count=int(count), ratio=float(ratio))
             if cls is not None
             else dict(template=template_list, roi=maa_roi, detector=detector, count=int(count), ratio=float(ratio)))
    result = recognizer("FeatureMatch", param, frame)
    if not result or not getattr(result, "hit", False):
        return None
    return maa_box_to_bounds(getattr(result, "box", None))


def _extract_ocr_text_items(items):
    texts = []
    if not items:
        return texts
    for item in items:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = str(item.get("text", "")).strip()
        else:
            text = str(getattr(item, "text", "")).strip()
        if text:
            texts.append(text)
    return texts


def ocr_texts_roi(recognizer, frame, roi=None, expected=None, threshold=0.3):
    if recognizer is None or frame is None:
        return []
    expected_list = [] if expected is None else ([expected] if isinstance(expected, str) else list(expected))
    maa_roi = box_to_maa_roi(roi)
    cls = _maa_pipeline_cls("JOCR")
    param = (cls(expected=expected_list, roi=maa_roi, threshold=float(threshold))
             if cls is not None
             else dict(expected=expected_list, roi=maa_roi, threshold=float(threshold)))
    result = recognizer("OCR", param, frame)
    if not result:
        return []
    for attr in ("filtered_results", "all_results"):
        texts = _extract_ocr_text_items(getattr(result, attr, None))
        if texts:
            return texts
    best = getattr(result, "best_result", None)
    if best is not None:
        texts = _extract_ocr_text_items([best])
        if texts:
            return texts
    raw = getattr(result, "raw_detail", None)
    if isinstance(raw, dict):
        for key in ("filtered", "all"):
            texts = _extract_ocr_text_items(raw.get(key))
            if texts:
                return texts
        if raw.get("best"):
            texts = _extract_ocr_text_items([raw["best"]])
            if texts:
                return texts
    return []


def ocr_author_identity(recognizer, frame, expected_name, roi=(0, 140, 1080, 680), name_confirmed=False):
    expected_norm = re.sub(r"\s+", "", (expected_name or "")).strip()
    if not expected_norm:
        return None
    texts = ocr_texts_roi(recognizer, frame, roi=roi)
    if not texts:
        return None
    has_name = bool(name_confirmed) or any(
        expected_norm in re.sub(r"\s+", "", line) for line in texts
    )
    if not has_name:
        return None
    ids = []
    for line in texts:
        match = re.search(r"小红书号[：:\s]*([A-Za-z0-9_.-]+)", line)
        if match and match[1] and match[1] not in ids:
            ids.append(match[1])
    if len(ids) == 1:
        return ids[0]
    return None
