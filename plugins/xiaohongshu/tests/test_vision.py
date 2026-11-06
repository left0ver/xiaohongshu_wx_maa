from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "xiaohongshu" / "agent"))
from collector import Device, collect_author_red_id, collect_note, settled_image_note
from comments import settled_comments
from ui import Page, PageMismatch
from vision import (box_to_maa_roi, clamp_roi, crop_roi, frame_diff_score, is_frame_stable,
                    is_red_heart_active, is_yellow_star_active, load_raw_screencap,
                    maa_box_to_bounds, match_feature_roi, match_template_roi,
                    ocr_author_identity, ocr_texts_roi, roi_hsv_ratio,
                    wait_frame_stable, wait_transition_settled)


def make_grid(width=20, height=20, bgr=(30, 30, 30), patch_box=None, patch_bgr=None):
    frame = [[list(bgr) for _ in range(width)] for _ in range(height)]
    if patch_box is not None and patch_bgr is not None:
        left, top, right, bottom = patch_box
        for y in range(max(0, top), min(height, bottom)):
            for x in range(max(0, left), min(width, right)):
                frame[y][x] = list(patch_bgr)
    return frame


class VisionTests(unittest.TestCase):
    def test_crop_and_frame_diff_score_in_dynamic_roi(self):
        frame_a = make_grid(20, 20, bgr=(40, 40, 40))
        frame_b = make_grid(20, 20, bgr=(40, 40, 40), patch_box=(2, 2, 8, 8), patch_bgr=(140, 140, 140))
        self.assertEqual(clamp_roi(frame_a, (-5, 2, 30, 10)), (0, 2, 20, 10))
        self.assertEqual(frame_diff_score(frame_a, frame_b, roi=(10, 10, 18, 18)), 0.0)
        self.assertTrue(is_frame_stable(frame_a, frame_b, roi=(10, 10, 18, 18), threshold=1.0))
        self.assertEqual(frame_diff_score(frame_a, frame_b, roi=(2, 2, 8, 8)), 100.0)
        self.assertFalse(is_frame_stable(frame_a, frame_b, roi=(2, 2, 8, 8), threshold=2.0))

    def test_wait_frame_stable_and_transition_settled(self):
        frames = [
            make_grid(16, 16, bgr=(20, 20, 20)),
            make_grid(16, 16, bgr=(90, 90, 90)),
            make_grid(16, 16, bgr=(180, 180, 180)),
            make_grid(16, 16, bgr=(180, 180, 180)),
        ]
        idx = {"i": 0}

        def screencap():
            val = frames[min(idx["i"], len(frames) - 1)]
            idx["i"] += 1
            return val

        settled = wait_transition_settled(screencap, baseline=frames[0], interval=0, timeout=1.0)
        self.assertTrue(settled["changed"])
        self.assertTrue(settled["stable"])
        self.assertEqual(settled["diff"], 0.0)

        idx["i"] = 2
        direct = wait_frame_stable(screencap, baseline=frames[2], interval=0, timeout=1.0)
        self.assertTrue(direct["stable"])
        self.assertEqual(direct["samples"], 1)

    def test_hsv_color_ratio_detects_red_heart_and_yellow_star_in_xml_bounds(self):
        inactive = make_grid(20, 20, bgr=(180, 180, 180))
        liked = make_grid(20, 20, bgr=(180, 180, 180), patch_box=(4, 4, 14, 14), patch_bgr=(45, 35, 245))
        collected = make_grid(20, 20, bgr=(180, 180, 180), patch_box=(4, 4, 14, 14), patch_bgr=(20, 210, 250))
        box = (2, 2, 16, 16)
        self.assertFalse(is_red_heart_active(inactive, box))
        self.assertTrue(is_red_heart_active(liked, box))
        self.assertFalse(is_yellow_star_active(liked, box))
        self.assertTrue(is_yellow_star_active(collected, box))
        self.assertGreater(roi_hsv_ratio(liked, box, (168, 90, 120), (12, 255, 255)), 0.4)

    def test_maa_opencv_template_and_feature_roi_wrappers(self):
        self.assertEqual(box_to_maa_roi((10, 20, 110, 70)), (10, 20, 100, 50))
        self.assertEqual(maa_box_to_bounds(SimpleNamespace(x=15, y=25, w=40, h=30)), (15, 25, 55, 55))
        calls = []

        def recognizer(kind, param, frame):
            calls.append((kind, param))
            return SimpleNamespace(hit=True, box=SimpleNamespace(x=120, y=300, w=80, h=60))

        frame = make_grid(10, 10)
        tpl_bounds = match_template_roi(recognizer, frame, "copy_link.png", roi=(100, 250, 300, 450), threshold=0.8)
        feat_bounds = match_feature_roi(recognizer, frame, ["share_icon.png"], roi=(100, 250, 300, 450), detector="SIFT")
        self.assertEqual(tpl_bounds, (120, 300, 200, 360))
        self.assertEqual(feat_bounds, (120, 300, 200, 360))
        self.assertEqual(calls[0][0], "TemplateMatch")
        self.assertEqual(calls[1][0], "FeatureMatch")

        ocr_calls = []

        def ocr_recognizer(kind, param, frame):
            ocr_calls.append((kind, param))
            return SimpleNamespace(
                hit=True,
                all_results=[
                    SimpleNamespace(text="迷人大蒜头", score=0.98),
                    SimpleNamespace(text="小红书号：95623511621", score=0.96),
                ],
            )

        texts = ocr_texts_roi(ocr_recognizer, frame, roi=(0, 140, 1080, 680))
        self.assertEqual(texts, ["迷人大蒜头", "小红书号：95623511621"])
        self.assertEqual(
            ocr_author_identity(ocr_recognizer, frame, "迷人大蒜头", roi=(0, 140, 1080, 680)),
            "95623511621",
        )
        self.assertIsNone(ocr_author_identity(ocr_recognizer, frame, "其他作者", roi=(0, 140, 1080, 680)))
        self.assertEqual(ocr_calls[0][0], "OCR")

    def test_load_raw_screencap_converts_android_rgba_to_bgr(self):
        import struct
        from vision import np
        if np is None:
            return
        header_16 = struct.pack("<IIII", 2, 2, 1, 0)
        pixels = bytes([
            10, 20, 30, 255,   40, 50, 60, 255,
            70, 80, 90, 255,   100, 110, 120, 255,
        ])
        bgr = load_raw_screencap(header_16 + pixels)
        self.assertEqual(bgr.shape, (2, 2, 3))
        self.assertEqual(list(bgr[0, 0]), [30, 20, 10])
        self.assertEqual(list(bgr[1, 1]), [120, 110, 100])

    def test_vision_enabled_device_reduces_redundant_uidumps_in_collection(self):
        def xml_feed():
            root = ET.Element("hierarchy")
            ET.SubElement(root, "node", {
                "package": "com.xingin.xhs",
                "content-desc": "发现",
                "selected": "true",
                "bounds": "[0,0][1080,100]",
            })
            ET.SubElement(root, "node", {
                "package": "com.xingin.xhs",
                "content-desc": "首页",
                "bounds": "[0,2200][1080,2350]",
            })
            card = ET.SubElement(root, "node", {
                "package": "com.xingin.xhs",
                "class": "android.widget.FrameLayout",
                "bounds": "[10,120][500,900]",
                "clickable": "true",
                "content-desc": "笔记  视觉加速图文 来自作者A 12赞",
            })
            ET.SubElement(card, "node", {
                "package": "com.xingin.xhs",
                "class": "android.widget.ImageView",
                "bounds": "[10,120][500,700]",
            })
            ET.SubElement(card, "node", {
                "package": "com.xingin.xhs",
                "class": "android.widget.TextView",
                "text": "作者A",
                "bounds": "[20,720][200,780]",
            })
            return ET.tostring(root, encoding="unicode")

        def xml_note():
            root = ET.Element("hierarchy")
            ET.SubElement(root, "node", {
                "package": "com.xingin.xhs",
                "resource-id": "com.xingin.xhs:id/nickNameTV",
                "text": "作者A",
                "bounds": "[80,80][220,130]",
            })
            ET.SubElement(root, "node", {
                "package": "com.xingin.xhs",
                "resource-id": "com.xingin.xhs:id/moreOperateIV",
                "bounds": "[900,80][980,130]",
            })
            ET.SubElement(root, "node", {
                "package": "com.xingin.xhs",
                "content-desc": "图片,第1张,共2张",
                "bounds": "[0,140][1000,190]",
            })
            body = ET.SubElement(root, "node", {
                "package": "com.xingin.xhs",
                "class": "android.widget.LinearLayout",
                "bounds": "[20,200][1000,600]",
            })
            ET.SubElement(body, "node", {
                "package": "com.xingin.xhs",
                "class": "android.widget.TextView",
                "text": "视觉加速图文",
                "bounds": "[30,220][900,280]",
            })
            ET.SubElement(body, "node", {
                "package": "com.xingin.xhs",
                "class": "android.widget.TextView",
                "text": "正文内容",
                "bounds": "[30,300][900,420]",
            })
            ET.SubElement(root, "node", {
                "package": "com.xingin.xhs",
                "content-desc": "评论框",
                "bounds": "[20,2100][500,2200]",
            })
            return ET.tostring(root, encoding="unicode")

        def xml_author():
            root = ET.Element("hierarchy")
            ET.SubElement(root, "node", {
                "package": "com.xingin.xhs",
                "text": "作者A",
                "bounds": "[200,250][400,320]",
            })
            ET.SubElement(root, "node", {
                "package": "com.xingin.xhs",
                "text": "小红书号：88899900",
                "bounds": "[200,340][500,390]",
            })
            return ET.tostring(root, encoding="unicode")

        def xml_share():
            root = ET.Element("hierarchy")
            ET.SubElement(root, "node", {
                "package": "com.xingin.xhs",
                "text": "分享至",
                "bounds": "[400,1600][600,1660]",
            })
            ET.SubElement(root, "node", {
                "package": "com.xingin.xhs",
                "content-desc": "复制链接",
                "bounds": "[100,1800][260,1960]",
            })
            return ET.tostring(root, encoding="unicode")

        state = {"screen": "feed", "dumps": 0}
        frames = {
            "feed": make_grid(12, 12, bgr=(10, 20, 30)),
            "note": make_grid(12, 12, bgr=(40, 50, 60)),
            "author": make_grid(12, 12, bgr=(70, 80, 90)),
            "share": make_grid(12, 12, bgr=(100, 110, 120)),
        }
        xmls = {
            "feed": xml_feed(),
            "note": xml_note(),
            "author": xml_author(),
            "share": xml_share(),
        }

        class VisionTestDevice(Device):
            def command(self, *args):
                if len(args) >= 5 and args[0] == "env" and any("xhs.UiDump" in str(a) for a in args):
                    state["dumps"] += 1
                    Path(args[-1]).write_text(xmls[state["screen"]])
                    return ""
                if args and args[0] in ("input", "am"):
                    self._pre_action_frame = self._last_frame
                    self._transition_pending = True
                    self._last_page = None
                    self._last_frame = None
                if args[:2] == ("input", "tap"):
                    x, y = int(args[2]), int(args[3])
                    if state["screen"] == "feed":
                        state["screen"] = "note"
                    elif state["screen"] == "note" and x < 500:
                        state["screen"] = "author"
                    elif state["screen"] == "note" and x >= 500:
                        state["screen"] = "share"
                    elif state["screen"] == "share":
                        state["screen"] = "note"
                elif args[:3] == ("input", "keyevent", "4"):
                    if state["screen"] == "author":
                        state["screen"] = "note"
                    elif state["screen"] == "note":
                        state["screen"] = "feed"
                return ""

        device = VisionTestDevice(screencap=lambda: frames[state["screen"]])
        with tempfile.TemporaryDirectory() as tmp:
            report = collect_note(
                device,
                lambda: dict(text="https://xhslink.cn/o/AbCdEf123", timestamp=int(1e18)),
                tmp,
            )
        self.assertEqual(report["payload"]["authorDouyinId"], "88899900")
        self.assertEqual(report["payload"]["title"], "视觉加速图文")
        # With OpenCV visual stability & page reuse, a full note collection only needs 7 dumps instead of 13+.
        self.assertLessEqual(state["dumps"], 7)

    def test_collect_author_red_id_falls_back_to_maa_ocr_when_xml_misses_red_id(self):
        root_video = ET.Element("hierarchy")
        ET.SubElement(root_video, "node", {
            "package": "com.xingin.xhs",
            "resource-id": "com.xingin.xhs:id/matrixNickNameView",
            "text": "迷人大蒜头",
            "bounds": "[24,1936][264,1993]",
        })
        ET.SubElement(root_video, "node", {
            "package": "com.xingin.xhs",
            "content-desc": "作者迷人大蒜头",
            "bounds": "[0,1890][426,2034]",
        })
        ET.SubElement(root_video, "node", {
            "package": "com.xingin.xhs",
            "content-desc": "分享",
            "bounds": "[921,168][1065,300]",
        })
        ET.SubElement(root_video, "node", {
            "package": "com.xingin.xhs",
            "resource-id": "com.xingin.xhs:id/noteContentText",
            "text": "五百万根麦子... 展开",
            "content-desc": "五百万根麦子一根一根捏",
            "bounds": "[48,2046][1032,2112]",
        })
        video_xml = ET.tostring(root_video, encoding="unicode")

        class OcrAuthorDevice(Device):
            def command(self, *args):
                if len(args) >= 5 and args[0] == "env" and any("xhs.UiDump" in str(a) for a in args):
                    Path(args[-1]).write_text(video_xml)
                    return ""
                return ""

        def recognizer(kind, param, frame):
            self.assertEqual(kind, "OCR")
            return SimpleNamespace(
                hit=True,
                all_results=[
                    SimpleNamespace(text="迷人大蒜头", score=0.99),
                    SimpleNamespace(text="小红书号：95623511621", score=0.97),
                ],
            )

        frame = make_grid(12, 12, bgr=(50, 60, 70))
        device = OcrAuthorDevice(screencap=lambda: frame, recognizer=recognizer)
        detail = dict(
            noteType="VIDEO",
            authorNickname="迷人大蒜头",
            title="五百万根麦子一根一根捏",
            description="五百万根麦子一根一根捏\n#steam游戏",
        )
        red_id = collect_author_red_id(device, detail)
        self.assertEqual(red_id, "95623511621")


if __name__ == "__main__":
    unittest.main()
