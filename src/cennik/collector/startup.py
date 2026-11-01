"""Opt-in Windows logon startup, scoped to the current user."""
import os
import subprocess
import sys
from pathlib import Path

NAME = "CennikCollector"
KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def startup_command(root, *, module="cennik.collector.desktop"):
    executable = Path(sys.executable)
    if getattr(sys, "frozen", False):
        command = [str(executable)]
    else:
        windowless = executable.with_name("pythonw.exe")
        command = [str(windowless if windowless.exists() else executable), "-m", module]
    return subprocess.list2cmdline([*command, "--root", str(Path(root).resolve()), "--background"])


def startup_enabled(*, name=NAME):
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as handle:
            return bool(winreg.QueryValueEx(handle, name)[0])
    except FileNotFoundError:
        return False


def set_startup(enabled, root, *, name=NAME, module="cennik.collector.desktop"):
    if os.name != "nt":
        raise ValueError("Автозапуск доступен только в Windows")
    import winreg
    command = startup_command(root, module=module)
    if enabled and len(command) > 260:
        raise ValueError("Слишком длинный путь для автозапуска Windows. Переместите EXE в папку с более коротким именем")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, KEY) as handle:
        if enabled:
            winreg.SetValueEx(handle, name, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(handle, name)
            except FileNotFoundError:
                pass
