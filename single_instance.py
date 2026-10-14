"""Allow only one running copy of the app (two copies would fight over
Photoshop and overwrite each other's profiles/config)."""
from __future__ import annotations

import os
import subprocess
import sys

LOCK_PATH = os.path.join(os.path.expanduser("~"), ".plugin-psd-render", "app.lock")


def acquire(path: str = LOCK_PATH):
    """Return an open lock handle (keep it alive) or None when already running.

    The OS releases the lock when the process exits, even after a crash.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = open(path, "a+")
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def notify_already_running() -> None:
    message = "PSD 批量生图已经在运行，请切换到已打开的窗口。"
    try:
        if sys.platform == "darwin":
            subprocess.run(["osascript", "-e",
                            f'display dialog "{message}" with title "PSD 批量生图" buttons {{"好"}} default button 1'],
                           capture_output=True, timeout=60)
        elif os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, "PSD 批量生图", 0x40)
        else:
            print(message)
    except Exception:  # noqa: BLE001 - the message is best effort
        print(message)
