"""A shared readiness deadline; blocking OCR is supplied as a bounded reader."""

import subprocess
import time
from pathlib import Path

from layout import basic_info_ready

# From the "更多信息" click until the basic-info panel is recognized. vivo V2131A
# needs about 2.7 s and occasionally ran out of the original 5 s.
BASIC_INFO_TIMEOUT = 8
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
    try:
        lines = semantic(min(1.2, remaining))
        if time.monotonic() < deadline and basic_info_ready(lines):
            return lines, "hierarchy"
    except (ValueError, subprocess.TimeoutExpired):
        pass
    remaining = deadline - time.monotonic()
    if remaining > 0:
        try:
            lines = bounded_ocr(remaining)
            if time.monotonic() <= deadline and basic_info_ready(lines):
                return lines, "local-template" if all(line.get("source") == "local-template" for line in lines) else "local-ocr"
        except subprocess.TimeoutExpired:
            pass
    raise ValueError(BASIC_INFO_TIMEOUT_MESSAGE)
