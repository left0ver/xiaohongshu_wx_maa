"""Desktop recognition-test equivalent of the host's MtaOcrFallback callback."""

import json

import numpy as np
from maa.controller import CustomController
from maa.custom_recognition import CustomRecognition
from maa.pipeline import JAnd, JOr


class OcrFallback(CustomRecognition):
    def analyze(self, context, argv):
        spec = json.loads(argv.custom_recognition_param)
        kind = spec["type"]
        param = {"And": JAnd, "Or": JOr}[kind](**spec["param"])
        result = context.run_recognition_direct(kind, param, argv.image)
        if result and result.hit:
            return self.AnalyzeResult(box=result.box, detail=result.raw_detail)
        return None


def register(resource):
    assert resource.register_custom_recognition("MtaOcrFallback", OcrFallback())



class RecognitionOnlyController(CustomController):
    """Bind recognition jobs offline and reject every input action."""

    def connect(self):
        return True

    def request_uuid(self):
        return "saved-capture-recognition-only"

    def screencap(self):
        return np.zeros((1280, 720, 3), dtype=np.uint8)

    def reject_input(self, *args):
        return False

    start_app = stop_app = click = swipe = touch_down = touch_move = reject_input
    touch_up = click_key = input_text = key_down = key_up = reject_input
