"""Reuse the Xiaohongshu command runner without forking the multithreaded Maa agent.

Kept alongside this independent PI bundle; source: plugins/xiaohongshu/xiaohongshu/agent/commands.py.
"""

import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


def run(args, timeout=15, stopped=lambda: False, binary=False):
    if timeout <= 0:
        raise subprocess.TimeoutExpired(args, timeout)
    if stopped():
        raise InterruptedError("Task stopped")
    if not hasattr(sys, "getandroidapilevel"):
        deadline = time.monotonic() + timeout
        with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=not binary) as child:
            try:
                while True:
                    if stopped():
                        raise InterruptedError("Task stopped")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(args, timeout)
                    try:
                        output, error = child.communicate(timeout=min(0.05, remaining))
                    except subprocess.TimeoutExpired:
                        continue
                    if child.returncode:
                        raise subprocess.CalledProcessError(child.returncode, args, output=output, stderr=error)
                    return output
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate()
    # Bionic API 28+ defines file_actions_t as a pointer; bundled Python lacks os.posix_spawn.
    libc = ctypes.CDLL("libc.so", use_errno=True)
    pointer = ctypes.POINTER(ctypes.c_void_p)
    strings = ctypes.POINTER(ctypes.c_char_p)
    libc.posix_spawn_file_actions_init.argtypes = [pointer]
    libc.posix_spawn_file_actions_destroy.argtypes = [pointer]
    libc.posix_spawn_file_actions_addopen.argtypes = [pointer, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint]
    libc.posix_spawnp.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_char_p, pointer, ctypes.c_void_p, strings, strings]

    def check(code):
        if code:
            raise OSError(code, os.strerror(code))

    argv = (ctypes.c_char_p * (len(args) + 1))(*(os.fsencode(x) for x in args), None)
    environment = [os.fsencode(k + "=" + v) for k, v in os.environ.items()]
    envp = (ctypes.c_char_p * (len(environment) + 1))(*environment, None)
    with tempfile.TemporaryDirectory(prefix="xhs-command-") as directory:
        output = Path(directory) / "stdout"
        error = Path(directory) / "stderr"
        actions = ctypes.c_void_p()
        check(libc.posix_spawn_file_actions_init(ctypes.byref(actions)))
        pid = ctypes.c_int()
        try:
            for fd, path in ((0, Path("/dev/null")), (1, output), (2, error)):
                flags = os.O_RDONLY if fd == 0 else os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                check(libc.posix_spawn_file_actions_addopen(ctypes.byref(actions), fd, os.fsencode(path), flags, 0o600))
            check(libc.posix_spawnp(ctypes.byref(pid), argv[0], ctypes.byref(actions), None, argv, envp))
        finally:
            libc.posix_spawn_file_actions_destroy(ctypes.byref(actions))
        deadline = time.monotonic() + timeout
        reaped = False
        try:
            while True:
                child, status = os.waitpid(pid.value, os.WNOHANG)
                if child:
                    reaped = True
                    code = os.waitstatus_to_exitcode(status)
                    if code:
                        raise subprocess.CalledProcessError(code, args, output=output.read_bytes(),
                                                            stderr=error.read_bytes())
                    return output.read_bytes() if binary else output.read_text(errors="replace")
                if stopped():
                    raise InterruptedError("Task stopped")
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(args, timeout)
                time.sleep(0.05)
        finally:
            if not reaped:
                try:
                    os.kill(pid.value, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(pid.value, 0)
