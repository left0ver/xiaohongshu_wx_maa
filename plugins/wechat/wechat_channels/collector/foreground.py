import json
import re
import time

from commands import run

FOREGROUND_SETTLE_SECONDS = 3.0


def foreground(timeout=15):
    started = time.monotonic()
    deadline = started + timeout
    settle_deadline = None
    checks = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("等待微信页面进入前台超时；未执行操作")
        activity = run(["dumpsys", "activity", "activities"], timeout=remaining)
        checks += 1
        resumed = re.findall(r"(?:mResumedActivity|topResumedActivity):[^\n]+", activity)
        if resumed:
            if not all(" com.tencent.mm/" in line for line in resumed):
                raise RuntimeError("微信不在前台；未执行操作：" + "；".join(line.strip() for line in resumed))
            if checks > 1:
                print(json.dumps({"kind": "foregroundReady", "checks": checks,
                                  "durationMs": round((time.monotonic() - started) * 1000)},
                                 ensure_ascii=False), flush=True)
            return
        # 页面切换期间没有 resumed Activity，等待系统完成切换。
        if settle_deadline is None:
            settle_deadline = time.monotonic() + FOREGROUND_SETTLE_SECONDS
            deadline = min(deadline, settle_deadline)
        time.sleep(min(0.15, max(0, deadline - time.monotonic())))
