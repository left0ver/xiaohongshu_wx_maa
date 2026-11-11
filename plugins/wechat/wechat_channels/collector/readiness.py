"""A shared readiness deadline; blocking OCR is supplied as a bounded reader."""

import subprocess
import time
from pathlib import Path

from layout import basic_info_ready

# From the "更多信息" click until the basic-info panel is recognized. vivo V2131A
# needs about 2.7 s and occasionally ran out of the original 5 s.
BASIC_INFO_TIMEOUT = 8
SEMANTIC_BUDGET = 4
SEMANTIC_RETRY_DELAY = 0.3
BASIC_INFO_TIMEOUT_MESSAGE = f"Author basic information was not ready within {BASIC_INFO_TIMEOUT} seconds"


def framework_directory(environment):
    # The agent core can contain only AgentServer/Client. The actual framework
    # and its control units are then provided by the host APK's nativeLib dir.
    paths = [environment.get("MAAFW_BINARY_PATH", ""), *environment.get("LD_LIBRARY_PATH", "").split(":")]
    for path in paths:
        if path and (Path(path) / "libMaaFramework.so").is_file():
            return Path(path)
    raise RuntimeError("Native MaaFramework is unavailable for the bounded readiness probe")


def wait_basic_info(semantic, bounded_ocr, timeout=BASIC_INFO_TIMEOUT):
    deadline = time.monotonic() + timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError(BASIC_INFO_TIMEOUT_MESSAGE)
    # The panel loads after the click; while controls are exposed, re-read them
    # for part of the budget before paying for OCR. A skipped probe (controls
    # known to be unavailable) goes straight to OCR.
    semantic_deadline = time.monotonic() + min(SEMANTIC_BUDGET, remaining / 2)
    while True:
        try:
            lines = semantic(max(0.01, min(1.2, deadline - time.monotonic())))
            if time.monotonic() < deadline and basic_info_ready(lines):
                return lines, "hierarchy"
        except subprocess.TimeoutExpired:
            break
        except ValueError as error:
            if getattr(error, "skipped", False):
                break
        if time.monotonic() + SEMANTIC_RETRY_DELAY >= semantic_deadline:
            break
        time.sleep(SEMANTIC_RETRY_DELAY)
    remaining = deadline - time.monotonic()
    if remaining > 0:
        try:
            lines = bounded_ocr(remaining)
            if time.monotonic() <= deadline and basic_info_ready(lines):
                return lines, "local-template" if all(line.get("source") == "local-template" for line in lines) else "local-ocr"
        except subprocess.TimeoutExpired:
            pass
    raise ValueError(BASIC_INFO_TIMEOUT_MESSAGE)
