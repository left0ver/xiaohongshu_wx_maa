import json
import faulthandler
from pathlib import Path
import sys
import time

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.pipeline import JCustomRecognition
from maa.toolkit import Toolkit

from collector import Device, collect_note, collect_notes, open_discover
from discovery import clipboard_value
from comments import inspect_comments
from interactions import interact_work_and_comments
from modes import run_auto_mode, run_city_mode, run_recommend_mode, run_search_mode
from vision import capture_raw_frame

faulthandler.enable()


def require_physical_display(context):
    image = context.tasker.controller.post_screencap().wait().get()
    guard = context.run_recognition_direct("Custom", JCustomRecognition(
        custom_recognition="MtaPhysicalDisplay"), image)
    if not guard or not guard.hit:
        raise RuntimeError("This plugin requires foreground mode on the physical display")


def make_device(context):
    def fast_screencap():
        frame = capture_raw_frame(stopped=lambda: context.tasker.stopping)
        if frame is not None:
            return frame
        return context.tasker.controller.post_screencap().wait().get()

    return Device(
        stopped=lambda: context.tasker.stopping,
        screencap=fast_screencap,
        recognizer=context.run_recognition_direct,
    )


@AgentServer.custom_action("XhsOpenDiscover")
class OpenDiscover(CustomAction):
    def run(self, context, argv):
        try:
            require_physical_display(context)
            open_discover(make_device(context))
            return True
        except Exception as error:
            print(f"Xiaohongshu navigation stopped: {type(error).__name__}: {error}", flush=True)
            return False


@AgentServer.custom_action("XhsCollectVisibleNote")
class CollectNote(CustomAction):
    def run(self, context, argv):
        device = make_device(context)

        def clipboard():
            device.command("am", "start", "--display", "0", "-n", "top.natsuu.mta.xiaohongshu/top.natsuu.mta.MainActivity")
            try:
                for _ in range(10):
                    if device.stopped():
                        raise InterruptedError("Task stopped")
                    image = context.tasker.controller.post_screencap().wait().get()
                    result = context.run_recognition_direct("Custom", JCustomRecognition(
                        custom_recognition="MtaDiscoveryClipboard"), image)
                    if result and result.hit:
                        return clipboard_value(result.raw_detail)
                    time.sleep(0.2)
                raise RuntimeError("Focused clipboard reader did not become ready")
            finally:
                device.command("am", "start", "--display", "0", "-n", "com.xingin.xhs/.index.v2.IndexActivityV2")

        try:
            require_physical_display(context)
            # cwd is <uid>/agent/pi; store drafts in <uid>/xiaohongshu-data.
            options = json.loads(argv.custom_action_param or "null") or {}
            data_dir = Path.cwd().parent.parent / "xiaohongshu-data"
            note_type = options.get("noteType")
            max_screens = options.get("maxScreens")
            count = options.get("count")
            if count is not None and count != 1:
                report = collect_notes(device, clipboard, data_dir, count=count,
                                       note_type=note_type, max_screens=6 if max_screens is None else max_screens)
            else:
                report = collect_note(device, clipboard, data_dir, note_type=note_type, max_screens=max_screens)
            print(json.dumps({"status": report["status"], "elapsedMs": report["elapsedMs"]}), flush=True)
            return True
        except Exception as error:
            print(f"Xiaohongshu collection stopped: {type(error).__name__}: {error}", flush=True)
            return False


@AgentServer.custom_action("XhsInspectComments")
class InspectComments(CustomAction):
    def run(self, context, argv):
        try:
            require_physical_display(context)
            report = inspect_comments(make_device(context),
                                      Path.cwd().parent.parent / "xiaohongshu-data")
            print(json.dumps({"status": report["status"], "elapsedMs": report["elapsedMs"]}), flush=True)
            return True
        except Exception as error:
            print(f"Xiaohongshu comment inspection stopped: {type(error).__name__}: {error}", flush=True)
            return False


@AgentServer.custom_action("XhsInteractNote")
class InteractNote(CustomAction):
    def run(self, context, argv):
        try:
            require_physical_display(context)
            options = json.loads(argv.custom_action_param or "null") or {}
            report = interact_work_and_comments(
                make_device(context),
                Path.cwd().parent.parent / "xiaohongshu-data",
                options=options,
            )
            print(json.dumps({"status": report["status"], "elapsedMs": report["elapsedMs"]}), flush=True)
            return True
        except Exception as error:
            print(f"Xiaohongshu interaction stopped: {type(error).__name__}: {error}", flush=True)
            return False


@AgentServer.custom_action("XhsRunMode")
class RunMode(CustomAction):
    def run(self, context, argv):
        try:
            require_physical_display(context)
            options = json.loads(argv.custom_action_param or "null") or {}
            data_dir = Path.cwd().parent.parent / "xiaohongshu-data"
            device = make_device(context)
            mode = (options.get("mode") or "auto").strip().lower()
            if mode in ("recommend", "discover"):
                report = run_recommend_mode(
                    device,
                    data_dir,
                    count=int(options.get("count", 2)),
                    note_type=options.get("noteType"),
                    max_screens=int(options.get("maxScreens", 6)),
                    inspect_note_comments=bool(options.get("inspectComments", False)),
                    comment_options=options.get("commentOptions"),
                    interaction_options=options.get("interactionOptions"),
                )
            elif mode == "search":
                report = run_search_mode(
                    device,
                    data_dir,
                    keyword=options.get("keyword") or "宠物",
                    sort_by=options.get("sortBy", "综合"),
                    filter_note_type=options.get("noteType", "不限"),
                    publish_time=options.get("publishTime", "不限"),
                    count=int(options.get("count", 1)),
                    max_screens=int(options.get("maxScreens", 6)),
                    inspect_note_comments=bool(options.get("inspectComments", False)),
                    comment_options=options.get("commentOptions"),
                    interaction_options=options.get("interactionOptions"),
                )
            elif mode == "city":
                report = run_city_mode(
                    device,
                    data_dir,
                    count=int(options.get("count", 1)),
                    note_type=options.get("noteType"),
                    max_screens=int(options.get("maxScreens", 6)),
                    require_location_permission=bool(options.get("requireLocationPermission", False)),
                    inspect_note_comments=bool(options.get("inspectComments", False)),
                    comment_options=options.get("commentOptions"),
                    interaction_options=options.get("interactionOptions"),
                )
            else:
                report = run_auto_mode(
                    device,
                    data_dir,
                    stages=options.get("stages"),
                )
            print(json.dumps({"status": report["status"], "elapsedMs": report["elapsedMs"]}), flush=True)
            return True
        except Exception as error:
            print(f"Xiaohongshu mode run stopped: {type(error).__name__}: {error}", flush=True)
            return False


if __name__ == "__main__":
    Toolkit.init_option(str(Path.cwd().parent.parent / "xiaohongshu-data"))
    AgentServer.start_up(sys.argv[-1])
    AgentServer.join()
    AgentServer.shut_down()
