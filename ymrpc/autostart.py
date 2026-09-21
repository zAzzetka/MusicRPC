"""Автозапуск вместе с Windows (ключ HKCU\\...\\Run — права администратора не нужны)."""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from . import APP_NAME, LEGACY_APP_NAMES

log = logging.getLogger("ymrpc.autostart")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
FLAG = "--autostart"   # по нему программа понимает, что её запустила Windows при входе


def command() -> str:
    """Команда, которую Windows выполнит при входе в систему."""
    if getattr(sys, "frozen", False):  # собранный .exe
        return f'"{sys.executable}" {FLAG}'
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")  # pythonw — без чёрного окна консоли
    if pythonw.exists():
        exe = pythonw
    script = Path(sys.argv[0]).resolve()
    return f'"{exe}" "{script}" {FLAG}'


def is_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def _delete(key, name: str) -> None:
    import winreg

    try:
        winreg.DeleteValue(key, name)
    except FileNotFoundError:
        pass


def set_enabled(enabled: bool) -> None:
    if sys.platform != "win32":
        return
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            for legacy in LEGACY_APP_NAMES:      # записи прежних версий больше не нужны
                _delete(key, legacy)
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command())
            else:
                _delete(key, APP_NAME)
    except OSError as e:
        log.warning("Не удалось изменить автозапуск: %s", e)


def wait_for_desktop(timeout: float = 90.0) -> None:
    """При запуске вместе с Windows ждём, пока появится панель задач (и даём Discord и сети подняться)."""
    if sys.platform != "win32":
        return
    import ctypes

    user32 = ctypes.windll.user32
    deadline = time.time() + timeout
    while time.time() < deadline:
        if user32.FindWindowW("Shell_TrayWnd", None):
            break
        time.sleep(1)
    time.sleep(5)
