import json
import re
import time

from commands import run

FOREGROUND_SETTLE_SECONDS = 3.0
FOREGROUND_CACHE_SECONDS = 0.4
_last_foreground_ok_at = -1.0


def invalidate_foreground_cache():
    global _last_foreground_ok_at
    _last_foreground_ok_at = -1.0


def foreground(timeout=15, force=False):
    global _last_foreground_ok_at
    now = time.monotonic()
    if not force and _last_foreground_ok_at >= 0 and 0 <= now - _last_foreground_ok_at <= FOREGROUND_CACHE_SECONDS:
        return
    started = now
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
                _last_foreground_ok_at = -1.0
                raise RuntimeError("微信不在前台；未执行操作：" + "；".join(line.strip() for line in resumed))
            _last_foreground_ok_at = time.monotonic()
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


def is_wechat_foreground(timeout=5, force=True):
    """Return True if WeChat (com.tencent.mm) is currently the resumed foreground app."""
    try:
        foreground(timeout=timeout, force=force)
        return True
    except RuntimeError:
        return False
