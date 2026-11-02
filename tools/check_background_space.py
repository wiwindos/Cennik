"""Verify WM_KEYDOWN/UP delivery to an owned background window without changing focus."""

import ctypes as c
import json
import os
import sys
import time
from ctypes import wintypes as w

from cennik.worker.idle import WindowsGame


def main():
    cycle = "--cycle" in sys.argv
    if cycle:
        from cennik.worker.automation import CycleWindows

        game = CycleWindows()
    else:
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
        _fields_ = [
            ("style", w.UINT),
            ("procedure", callback),
            ("class_extra", c.c_int),
            ("window_extra", c.c_int),
            ("instance", w.HINSTANCE),
            ("icon", w.HICON),
            ("cursor", w.HANDLE),
            ("background", w.HBRUSH),
            ("menu", w.LPCWSTR),
            ("name", w.LPCWSTR),
        ]

    kernel.GetModuleHandleW.argtypes = [w.LPCWSTR]
    kernel.GetModuleHandleW.restype = w.HINSTANCE
    user.RegisterClassW.argtypes = [c.POINTER(WindowClass)]
    user.RegisterClassW.restype = w.WORD
    user.CreateWindowExW.argtypes = [
        w.DWORD,
        w.LPCWSTR,
        w.LPCWSTR,
        w.DWORD,
        c.c_int,
        c.c_int,
        c.c_int,
        c.c_int,
        w.HWND,
        w.HMENU,
        w.HINSTANCE,
        w.LPVOID,
    ]
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
        hwnd = user.CreateWindowExW(
            0x08000088 if cycle else 0x08000080,
            name,
            "Cennik owned input test",
            0x80000000 if cycle else 0x00CF0000,
            20 if cycle else -32000,
            20 if cycle else -32000,
            320 if cycle else 100,
            140 if cycle else 100,
            None,
            None,
            instance,
            None,
        )
        assert hwnd
        user.ShowWindow(hwnd, 4)
        target = game.identity(hwnd)
        assert target and game.ready(target)
        if cycle:
            from cennik.worker.automation import END, MAGIC, VERSION_COLOR, Signal

            gdi = c.WinDLL("gdi32", use_last_error=True)
            user.GetDC.argtypes = [w.HWND]
            user.GetDC.restype = w.HDC
            user.ReleaseDC.argtypes = [w.HWND, w.HDC]
            user.FillRect.argtypes = [w.HDC, c.POINTER(w.RECT), w.HBRUSH]
            user.UpdateWindow.argtypes = [w.HWND]
            gdi.CreateSolidBrush.argtypes = [w.DWORD]
            gdi.CreateSolidBrush.restype = w.HBRUSH
            gdi.DeleteObject.argtypes = [w.HANDLE]
            user.UpdateWindow(hwnd)
            dc = user.GetDC(hwnd)
            try:
                data = (32 + 3 * 24, 16 + 7 * 14, 16 + 2 * 14)
                colors = [
                    MAGIC,
                    (0x12, 0x34, 0x56),
                    (0xAB, 0xCD, 0xEF),
                    data,
                    tuple(255 - v for v in data),
                    END,
                    VERSION_COLOR,
                ]
                for i, (r, g, b) in enumerate(colors):
                    brush = gdi.CreateSolidBrush(r | (g << 8) | (b << 16))
                    rectangle = w.RECT(12 + i * 10, 12, 22 + i * 10, 22)
                    user.FillRect(dc, c.byref(rectangle), brush)
                    gdi.DeleteObject(brush)
            finally:
                user.ReleaseDC(hwnd, dc)
            assert game.signal(target, "123456abcdef") == Signal(3, 7, 2)
            assert game.signal(target, "000000000000") is None
            game.press(target, "F8")
            game.press(target, "F9")
        else:
            game.jump(target)
        message = w.MSG()
        deadline = time.monotonic() + 2
        while len(messages) < (4 if cycle else 2) and time.monotonic() < deadline:
            while user.PeekMessageW(c.byref(message), hwnd, 0, 0, 1):
                user.TranslateMessage(c.byref(message))
                user.DispatchMessageW(c.byref(message))
            time.sleep(0.01)
        assert messages == (
            [(0x100, 0x77), (0x101, 0x77), (0x100, 0x78), (0x101, 0x78)]
            if cycle
            else [(0x100, 0x20), (0x101, 0x20)]
        ), messages
        assert user.GetForegroundWindow() == foreground
        print(
            json.dumps(
                {
                    "background_cycle" if cycle else "background_space": "passed",
                    "keydown_keyup": True,
                    "focus_changed": False,
                }
            )
        )
    finally:
        if hwnd:
            user.DestroyWindow(hwnd)
        user.UnregisterClassW(name, instance)


if __name__ == "__main__":
    main()
