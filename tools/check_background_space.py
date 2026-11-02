"""Verify WM_KEYDOWN/UP delivery to an owned background window without changing focus."""

import ctypes as c
import json
import os
import time
from ctypes import wintypes as w

from cennik.worker.idle import WindowsGame


def main():
    game = WindowsGame()
    user = game.user
    kernel = game.kernel
    callback = c.WINFUNCTYPE(c.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
    messages = []
    user.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user.DefWindowProcW.restype = c.c_ssize_t

    @callback
    def receive(hwnd, message, key, data):
        if message in (0x100, 0x101):
            messages.append((message, key))
        return user.DefWindowProcW(hwnd, message, key, data)

    class WindowClass(c.Structure):
        _fields_ = [("style", w.UINT), ("procedure", callback), ("class_extra", c.c_int),
                    ("window_extra", c.c_int), ("instance", w.HINSTANCE), ("icon", w.HICON),
                    ("cursor", w.HANDLE), ("background", w.HBRUSH), ("menu", w.LPCWSTR),
                    ("name", w.LPCWSTR)]

    kernel.GetModuleHandleW.argtypes = [w.LPCWSTR]
    kernel.GetModuleHandleW.restype = w.HINSTANCE
    user.RegisterClassW.argtypes = [c.POINTER(WindowClass)]
    user.RegisterClassW.restype = w.WORD
    user.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
        c.c_int, c.c_int, c.c_int, c.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
    user.CreateWindowExW.restype = w.HWND
    user.ShowWindow.argtypes = [w.HWND, c.c_int]
    user.DestroyWindow.argtypes = [w.HWND]
    user.UnregisterClassW.argtypes = [w.LPCWSTR, w.HINSTANCE]
    user.PeekMessageW.argtypes = [c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
    user.TranslateMessage.argtypes = [c.POINTER(w.MSG)]
    user.DispatchMessageW.argtypes = [c.POINTER(w.MSG)]
    name = "CennikSpaceTest" + str(os.getpid())
    instance = kernel.GetModuleHandleW(None)
    spec = WindowClass(0, receive, 0, 0, instance, None, None, None, None, name)
    assert user.RegisterClassW(c.byref(spec))
    hwnd = None
    foreground = user.GetForegroundWindow()
    try:
        # WS_EX_NOACTIVATE + WS_EX_TOOLWINDOW, offscreen, SW_SHOWNOACTIVATE.
        hwnd = user.CreateWindowExW(0x08000080, name, "Cennik Space test", 0x00CF0000,
            -32000, -32000, 100, 100, None, None, instance, None)
        assert hwnd
        user.ShowWindow(hwnd, 4)
        target = game.identity(hwnd)
        assert target and game.ready(target)
        game.jump(target)
        message = w.MSG()
        deadline = time.monotonic() + 2
        while len(messages) < 2 and time.monotonic() < deadline:
            while user.PeekMessageW(c.byref(message), hwnd, 0, 0, 1):
                user.TranslateMessage(c.byref(message))
                user.DispatchMessageW(c.byref(message))
            time.sleep(.01)
        assert messages == [(0x100, 0x20), (0x101, 0x20)], messages
        assert user.GetForegroundWindow() == foreground
        print(json.dumps({"background_space": "passed", "keydown_keyup": True, "focus_changed": False}))
    finally:
        if hwnd:
            user.DestroyWindow(hwnd)
        user.UnregisterClassW(name, instance)


if __name__ == "__main__":
    main()
