"""A shared readiness deadline; blocking OCR is supplied as a bounded reader."""

import subprocess
import time
from pathlib import Path

from layout import basic_info_ready


def framework_directory(environment):
    # The agent core can contain only AgentServer/Client. The actual framework
    # and its control units are then provided by the host APK's nativeLib dir.
    paths = [environment.get("MAAFW_BINARY_PATH", ""), *environment.get("LD_LIBRARY_PATH", "").split(":")]
    for path in paths:
        if path and (Path(path) / "libMaaFramework.so").is_file():
            return Path(path)
    raise RuntimeError("Native MaaFramework is unavailable for the bounded readiness probe")


def wait_basic_info(semantic, bounded_ocr, timeout=5):
    deadline = time.monotonic() + timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("Author basic information was not ready within 5 seconds")
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
    raise ValueError("Author basic information was not ready within 5 seconds")
