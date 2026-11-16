import argparse
import json
import sys
import time
from pathlib import Path


parser = argparse.ArgumentParser(description="使用真机截图检查 OCR 空结果和文字识别")
parser.add_argument("--collector", type=Path, required=True)
parser.add_argument("--framework", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--navigation-runs", type=int, default=0)
args = parser.parse_args()
if args.navigation_runs < 0:
    raise ValueError("导航检查次数不能为负数")
args.output = args.output / str(time.time_ns())
sys.path.insert(0, str(args.collector))

from maa.custom_action import CustomAction
from maa.library import Library
from maa.resource import Resource
from maa.tasker import Tasker
from android_ui import MaaUi, PhoneController
from accessibility import expose_controls
from collection import ensure_feed
from modes import verify_stage_page


class OcrEmptyRegression(CustomAction):
    def run(self, context, argv):
        ui = MaaUi(context.tasker, args.output, context)
        started = time.monotonic()
        # 使用真机截图中的小区域，检查真实 OCR 的空结果。
        lines = ui.ocr_snapshot("empty-region-regression", [0, 0, 5, 5])
        if lines != []:
            raise AssertionError(lines)
        empty_duration = round((time.monotonic() - started) * 1000)
        text = ui.ocr_snapshot("text-region-regression")
        if not text:
            raise RuntimeError("真机页面没有取得文字识别结果")
        if args.navigation_runs:
            with expose_controls(args.output) as exposed:
                navigator = MaaUi(context.tasker, args.output / "navigation", context, controls_exposed=exposed)
                for _ in range(args.navigation_runs):
                    ensure_feed(navigator, launch=True)
                    verify_stage_page(navigator, "recommend")
        result = {"emptyResultAccepted": True, "emptyDurationMs": empty_duration,
                  "textLineCount": len(text), "navigationRuns": args.navigation_runs,
                  "durationMs": round((time.monotonic() - started) * 1000)}
        (args.output / "result.json").write_text(json.dumps(result, ensure_ascii=False))
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return True


args.output.mkdir(parents=True, exist_ok=True)
Library.open(args.framework)
Tasker.set_log_dir(args.output / "maa-log")
controller = PhoneController()
controller.set_screenshot_target_short_side(720)
if not controller.post_connection().wait().succeeded:
    raise RuntimeError("真机控制器连接失败")
resource = Resource()
if not resource.post_bundle(args.collector.parent / "resource/base").wait().succeeded:
    raise RuntimeError("真机资源加载失败")
if not resource.register_custom_action("OcrEmptyRegression", OcrEmptyRegression()):
    raise RuntimeError("真机检查动作注册失败")
tasker = Tasker()
if not tasker.bind(resource, controller):
    raise RuntimeError("真机识别器绑定失败")
job = tasker.post_task("OcrEmptyRegression", {
    "OcrEmptyRegression": {"recognition": "DirectHit", "action": "Custom", "custom_action": "OcrEmptyRegression", "next": []},
    "BatchText": {"recognition": "OCR", "timeout": 1, "rate_limit": 1, "action": "DoNothing", "next": []},
}).wait()
if not job.succeeded:
    raise RuntimeError("真机 OCR 检查未通过")
result_path = args.output / "result.json"
if not result_path.is_file():
    raise RuntimeError("真机 OCR 没有取得业务结果")
if json.loads(result_path.read_text())["emptyResultAccepted"] is not True:
    raise RuntimeError("真机 OCR 空结果检查失败")
