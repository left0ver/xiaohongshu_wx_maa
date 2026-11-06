"""Prepare a read-only native recognizer before starting the page-wait budget."""

import json
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from commands import run


@contextmanager
def prepared_reader(script, stopped=lambda: False):
    with tempfile.TemporaryDirectory(prefix="wechat-ready-") as directory:
        root = Path(directory)
        output = root / "result.json"
        cancel = threading.Event()
        errors = []

        def worker():
            try:
                run([sys.executable, str(script), str(output), "--prepare"], timeout=20, stopped=cancel.is_set)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=worker, name="wechat-readiness-probe", daemon=True)

        def wait_for(path, timeout):
            deadline = time.monotonic() + timeout
            while True:
                if stopped():
                    raise InterruptedError("Collection was stopped")
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired("readiness probe", timeout)
                if path.is_file():
                    return
                if errors:
                    raise errors[0]
                if not thread.is_alive():
                    raise RuntimeError("Readiness probe exited without a result")
                time.sleep(min(0.025, max(0, deadline - time.monotonic())))

        def read(timeout):
            if stopped():
                raise InterruptedError("Collection was stopped")
            if timeout <= 0:
                raise subprocess.TimeoutExpired("readiness probe", timeout)
            request = root / "request.tmp"
            request.write_text(str(timeout), encoding="utf-8")
            request.replace(root / "request")
            wait_for(output, timeout)
            return json.loads(output.read_text(encoding="utf-8"))

        thread.start()
        try:
            wait_for(root / "prepared", 10)
            yield read
        finally:
            cancel.set()
            # commands.run kills and reaps the child before the temporary files
            # disappear. This thread never calls Maa or sends an input event.
            thread.join()
