"""Opt-in Space messages to a selected background game window; never steals focus."""

from __future__ import annotations

import json
import os
import random
import threading
import time
from pathlib import Path

from ahledger.util import atomic_json


class WindowsGame:
    def __init__(self):
        import ctypes
        from ctypes import wintypes as w

        self.c, self.w = ctypes, w
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        self.user.IsWindow.argtypes = [w.HWND]
        self.user.IsWindow.restype = w.BOOL
        self.user.IsWindowVisible.argtypes = [w.HWND]
        self.user.IsWindowVisible.restype = w.BOOL
        self.user.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        self.kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.kernel.OpenProcess.restype = w.HANDLE
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.QueryFullProcessImageNameW.argtypes = [
            w.HANDLE,
            w.DWORD,
            w.LPWSTR,
            ctypes.POINTER(w.DWORD),
        ]
        self.kernel.GetProcessTimes.argtypes = [w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)]
        self.user.GetForegroundWindow.restype = w.HWND
        self.user.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user.GetAsyncKeyState.restype = self.c.c_short
        self.user.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        self.user.PostMessageW.restype = w.BOOL

    def identity(self, hwnd):
        if (
            not self.user.IsWindow(hwnd)
            or not self.user.IsWindowVisible(hwnd)
        ):
            return None
        pid = self.w.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, self.c.byref(pid))
        handle = self.kernel.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return None
        try:
            text = self.c.create_unicode_buffer(32768)
            size = self.w.DWORD(len(text))
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, text, self.c.byref(size)):
                return None
            times = [self.w.FILETIME() for _ in range(4)]
            if not self.kernel.GetProcessTimes(handle, *[self.c.byref(t) for t in times]):
                return None
            title = self.c.create_unicode_buffer(512)
            self.user.GetWindowTextW(hwnd, title, len(title))
            return {
                "hwnd": int(hwnd),
                "pid": pid.value,
                "started": (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime,
                "path": text.value,
                "title": title.value,
            }
        finally:
            self.kernel.CloseHandle(handle)

    def windows(self, game_root):
        found = []
        callback_type = self.c.WINFUNCTYPE(self.w.BOOL, self.w.HWND, self.w.LPARAM)
        self.user.EnumWindows.argtypes = [callback_type, self.w.LPARAM]

        def inspect(hwnd, _):
            item = self.identity(hwnd)
            if item:
                path = Path(item["path"]).resolve()
                if path.is_relative_to(Path(game_root).resolve()) and path.name.lower() in {
                    "wow.exe",
                    "wowclassic.exe",
                    "wowforever.exe",
                    "wow-64.exe",
                }:
                    found.append(item)
            return True

        callback = callback_type(inspect)
        self.user.EnumWindows(callback, 0)
        return found

    def ready(self, target):
        current = self.identity(target["hwnd"])
        if not current or any(current[k] != target[k] for k in ("pid", "started", "path")):
            return False
        if self.user.GetForegroundWindow() == target["hwnd"]:
            return False
        if any(self.user.GetAsyncKeyState(key) & 0x8000 for key in (0x10, 0x11, 0x12)):
            return False
        return True

    def jump(self, target):
        return self.press(target, "SPACE")

    def press(self, target, key):
        if not self.ready(target):
            raise ValueError("Выбранное окно недоступно или активно")
        hwnd = target["hwnd"]
        if key == "SPACE":
            virtual, scan = 0x20, 0x39
        elif key in [f"F{i}" for i in range(1, 13)]:
            number = int(key[1:])
            virtual = 0x6F + number
            scan = 0x3A + number if number <= 10 else 0x56 + number - 10
        else:
            raise ValueError("Поддерживаются Space и F1–F12")
        code = 1 | (scan << 16)
        if not self.user.PostMessageW(hwnd, 0x100, virtual, code):
            raise OSError("Не удалось отправить клавишу")
        try:
            time.sleep(0.08)
        finally:
            self.user.PostMessageW(hwnd, 0x101, virtual, code | (1 << 30) | (1 << 31))


class IdleProbe:
    def __init__(self, root, backend=None):
        self.path = root / "idle-probe.json"
        self.settings = (
            json.loads(self.path.read_text("utf-8"))
            if self.path.exists()
            else {"enabled": False, "target": None}
        )
        self.backend = backend
        self.last = time.monotonic()
        self.interval = random.uniform(150, 400)
        self.message = "Прыжок в фоновом окне выключен"
        self.lock = threading.RLock()

    def engine(self):
        if self.backend is None:
            if os.name != "nt":
                raise ValueError("Отправка Space доступна только на Windows")
            self.backend = WindowsGame()
        return self.backend

    def windows(self, game_root):
        return self.engine().windows(game_root)

    def configure(self, target, enabled, game_root):
        with self.lock:
            allowed = self.windows(game_root)
            if target not in allowed:
                raise ValueError("Выберите открытое окно WoW из выбранной папки игры")
            self.settings = {"target": target, "enabled": bool(enabled)}
            atomic_json(self.path, self.settings)
            self.last = time.monotonic()
            self.interval = random.uniform(150, 400)

    def disable(self):
        with self.lock:
            self.settings["enabled"] = False
            atomic_json(self.path, self.settings)
            self.message = "Прыжок в фоновом окне выключен"

    def tick(self, *, paused=False, test=False):
        with self.lock:
            if paused or not self.settings.get("target") or not (test or self.settings["enabled"]):
                return False
            if not test and time.monotonic() - self.last < self.interval:
                return False
            try:
                if not self.engine().ready(self.settings["target"]):
                    self.message = "Space: пауза — окно недоступно, активно или нажата клавиша Shift/Ctrl/Alt"
                    return False
                self.engine().jump(self.settings["target"])
                self.last = time.monotonic()
                self.interval = random.uniform(150, 400)
                self.message = "Space отправлен в выбранное фоновое окно"
                return True
            except (ValueError, OSError):
                self.message = "Space не отправлен; выбранное окно недоступно"
                return False
