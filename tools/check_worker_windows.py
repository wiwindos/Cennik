"""Native selection QA against two owned fixture processes; never posts game keys."""
import argparse
import json
import os
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path

from cennik.worker.idle import WindowsGame
from cennik.worker.window_picker import WindowPicker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--fixture')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.fixture:
        root = tk.Tk()
        root.title(args.fixture)
        root.geometry('580x300+40+40' if args.fixture == 'wiw' else '580x300+650+40')
        tk.Label(root, text=args.fixture, font=('Segoe UI', 50)).pack(expand=True)
        root.update()
        backend = WindowsGame()
        backend.user.GetAncestor.argtypes = [backend.w.HWND, backend.w.UINT]
        backend.user.GetAncestor.restype = backend.w.HWND
        hwnd = backend.user.GetAncestor(root.winfo_id(), 2)
        monitors = []
        callback_type = backend.c.WINFUNCTYPE(backend.w.BOOL, backend.w.HANDLE, backend.w.HDC,
                                             backend.c.POINTER(backend.w.RECT), backend.w.LPARAM)
        def collect_monitor(handle, dc, rect, data):
            monitors.append((rect.contents.left, rect.contents.top))
            return True
        callback = callback_type(collect_monitor)
        backend.user.EnumDisplayMonitors.argtypes = [backend.w.HDC, backend.c.c_void_p, callback_type, backend.w.LPARAM]
        backend.user.EnumDisplayMonitors(None, None, callback, 0)
        position = monitors[-1 if args.fixture == 'holy' else 0]
        backend.user.SetWindowPos.argtypes = [backend.w.HWND, backend.w.HWND, backend.c.c_int, backend.c.c_int,
                                             backend.c.c_int, backend.c.c_int, backend.w.UINT]
        backend.user.SetWindowPos(hwnd, None, position[0]+40, position[1]+40, 600, 340, 0x14)
        root.update()
        info = backend.identity(hwnd)
        info.update(backend.geometry(hwnd))
        info['owner_pid'] = os.getpid()
        (args.output/(args.fixture+'.json')).write_text(json.dumps(info), 'utf-8')
        def poll():
            if (args.output/(args.fixture+'.activate')).exists():
                root.lift()
                root.focus_force()
                (args.output/(args.fixture+'.activate')).unlink()
            root.after(50, poll)
        poll()
        root.after(45000, root.destroy)
        root.mainloop()
        return
    children = []
    root = None
    try:
        for name in ('wiw', 'holy'):
            children.append(subprocess.Popen([sys.executable, __file__, '--output', str(args.output.resolve()), '--fixture', name]))
        deadline = time.monotonic()+15
        while not all((args.output/(name+'.json')).exists() for name in ('wiw','holy')):
            if time.monotonic() > deadline:
                raise RuntimeError('Fixture windows did not open')
            time.sleep(.1)
        values = [json.loads((args.output/(name+'.json')).read_text('utf-8')) for name in ('wiw','holy')]
        assert all(value['pid'] == value['owner_pid'] for value in values)
        assert [value['title'] for value in values] == ['wiw','holy']

        class Fixtures(WindowsGame):
            def windows(self, game_root):
                return [{**self.identity(value['hwnd']), **self.geometry(value['hwnd'])} for value in values]

        backend = Fixtures()
        root = tk.Tk()
        root.title('Cennik Worker · проверка выбора окна')
        root.geometry('740x210+40+380')
        picker = WindowPicker(root, backend, 'fixtures')
        picker.pack(fill='both', expand=True, padx=15, pady=15)
        root.update()
        assert picker.choice.current() == -1
        picker.choice.current(1)
        picker.choose()
        chosen = picker.selected()
        holy_pid = values[1]['pid']
        assert chosen['pid'] == holy_pid and chosen['title'] == 'holy', chosen
        values.reverse()
        picker.refresh()
        assert picker.selected()['pid'] == holy_pid
        assert picker.choice.current() == 0
        bitmap = backend.preview(chosen)
        bitmap.save(args.output/'selected-window.png')
        root.lift()
        root.update()
        from PIL import ImageGrab
        backend.user.GetAncestor.argtypes = [backend.w.HWND, backend.w.UINT]
        backend.user.GetAncestor.restype = backend.w.HWND
        ImageGrab.grab(window=backend.user.GetAncestor(root.winfo_id(),2),all_screens=True).save(args.output/'window-picker.png')
        result = {'native_two_processes': True, 'dropdown_pick': 'holy', 'same_after_reorder': True,
                  'fixture_monitors': sorted({value['monitor'] for value in values}),
                  'pid': chosen['pid'], 'hwnd': chosen['hwnd'], 'monitor': chosen['monitor'], 'game_keys_sent': 0}
        (args.output/'result.json').write_text(json.dumps(result,indent=2), 'utf-8')
        print(json.dumps(result))
    finally:
        if root is not None:
            root.destroy()
        for process in children:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)


if __name__ == '__main__':
    main()
