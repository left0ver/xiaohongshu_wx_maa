"""Let WeChat publish its controls for one run, then restore the user's settings.

WeChat returns an empty accessibility tree unless a screen-reader-class service
is enabled. Google's Select to Speak qualifies and, unlike TalkBack or vendor
screen readers, does not take over touch input, so taps and swipes still work.
Settings are changed with the shell identity the collector already runs as.
"""

import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from commands import run

PACKAGE = "com.google.android.marvin.talkback"
SERVICE = PACKAGE + "/com.google.android.accessibility.selecttospeak.SelectToSpeakService"
SERVICES_KEY = "enabled_accessibility_services"
ENABLED_KEY = "accessibility_enabled"
MARKER = "accessibility-owner.json"
# WeChat republishes its tree shortly after the service binds.
SETTLE_SECONDS = 1.5


def _shell(*args, runner=run):
    return runner(list(args), timeout=10)


def _get(key, runner=run):
    value = _shell("settings", "get", "secure", key, runner=runner).strip()
    return None if value in ("", "null") else value


def _put(key, value, runner=run):
    if value is None:
        _shell("settings", "delete", "secure", key, runner=runner)
    else:
        _shell("settings", "put", "secure", key, value, runner=runner)


def _services(value):
    return [item for item in (value or "").split(":") if item]


def installed(runner=run):
    try:
        # A system copy prints its APK path, e.g. package:/product/app/talkback/talkback.apk.
        return _shell("pm", "path", PACKAGE, runner=runner).strip().startswith("package:")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


def _restore(original, runner=run):
    _put(SERVICES_KEY, original["services"], runner=runner)
    _put(ENABLED_KEY, original["enabled"], runner=runner)


@contextmanager
def expose_controls(state_dir, emit=print, runner=run, sleep=time.sleep):
    """Yield True while Select to Speak is enabled; never fail the run over it.

    The settings that existed before the run are written to a marker first, so
    a run that was killed before restoring them is cleaned up by the next one.
    A service the user enabled themselves is left exactly as it was.
    """
    marker = Path(state_dir) / MARKER
    owned, exposed = _acquire(marker, emit, runner, sleep)
    try:
        yield exposed
    finally:
        if owned is not None:
            _release(marker, owned, emit, runner)


def _acquire(marker, emit, runner, sleep):
    """Return (settings to restore or None, whether controls are exposed)."""
    owned = None
    try:
        if marker.is_file():
            # A previous run died while it owned the setting: put it back first.
            try:
                previous = json.loads(marker.read_text(encoding="utf-8"))
            except ValueError:
                previous = None
            if isinstance(previous, dict) and {"services", "enabled"} <= previous.keys():
                _restore(previous, runner=runner)
                emit({"kind": "controlsExposure", "status": "recovered"})
            marker.unlink()
        if not installed(runner=runner):
            emit({"kind": "controlsExposure", "status": "unavailable", "reason": "Select to Speak is not installed"})
            return None, False
        original = {"services": _get(SERVICES_KEY, runner=runner), "enabled": _get(ENABLED_KEY, runner=runner)}
        if SERVICE in _services(original["services"]) and original["enabled"] == "1":
            emit({"kind": "controlsExposure", "status": "already-enabled"})
            return None, True
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(json.dumps(original), encoding="utf-8")
        os.replace(temporary, marker)
        owned = original
        services = [item for item in _services(original["services"]) if item != SERVICE] + [SERVICE]
        _put(SERVICES_KEY, ":".join(services), runner=runner)
        _put(ENABLED_KEY, "1", runner=runner)
        sleep(SETTLE_SECONDS)
        emit({"kind": "controlsExposure", "status": "enabled"})
        return owned, True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, ValueError) as error:
        # A partly applied change is still restored when the run ends.
        emit({"kind": "controlsExposure", "status": "failed", "reason": f"{type(error).__name__}: {error}"})
        return owned, False


def _release(marker, original, emit, runner):
    try:
        _restore(original, runner=runner)
        marker.unlink(missing_ok=True)
        emit({"kind": "controlsExposure", "status": "restored"})
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        # The marker stays, so the next run restores the original settings.
        emit({"kind": "controlsExposure", "status": "restore-failed", "reason": f"{type(error).__name__}: {error}"})
