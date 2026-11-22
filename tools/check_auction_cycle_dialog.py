"""Render only an isolated settings window; no game input or server connection."""

import json
import tempfile
import time
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

from PIL import ImageGrab

from cennik.collector.desktop import CollectorWindow
from cennik.worker.agent import WorkerAgent


def main():
    output = Path("outputs/worker-0.6.4-dialog")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cennik-cycle-dialog-") as directory:
        agent = WorkerAgent(Path(directory))
        game = Path(directory) / "game"
        game.mkdir()
        agent.settings.update(game_root=str(game), paused=True)
        agent.manifest = {
            "clients": [
                {
                    "id": "a" * 32,
                    "role": "auctioneer",
                    "character": "Аукционер",
                    "realm": "Тестовый сервер",
                }
            ]
        }
        agent.cycle.backend = SimpleNamespace(
            windows=lambda root: [{"hwnd": 1, "pid": 2, 'started': 3, 'path': 'fixture/Wow.exe', "title": "Тестовое окно WoW", 'monitor': 'DISPLAY2', 'x': -1920, 'y': 0}]
        )
        app = CollectorWindow(agent, worker=True)
        try:
            app.root.update()

            def main_widgets(parent):
                for child in parent.winfo_children():
                    if not isinstance(child, tk.Toplevel):
                        yield child
                        yield from main_widgets(child)

            cycle_button = next(
                w
                for w in main_widgets(app.root)
                if w.winfo_class() == "TButton" and w.cget("text") == "Автоцикл аукционера…"
            )
            assert (
                cycle_button.winfo_rooty() + cycle_button.winfo_height()
                <= app.root.winfo_rooty() + app.root.winfo_height()
            )
            ImageGrab.grab(
                bbox=(
                    app.root.winfo_rootx(),
                    app.root.winfo_rooty(),
                    app.root.winfo_rootx() + app.root.winfo_width(),
                    app.root.winfo_rooty() + app.root.winfo_height(),
                )
            ).save(output / "worker.png")
            app.cycle_dialog()
            app.root.update()
            dialog = next(w for w in app.root.winfo_children() if isinstance(w, tk.Toplevel))
            from cennik.worker.idle import WindowsGame
            native = WindowsGame()
            native.user.GetAncestor.argtypes = [native.w.HWND, native.w.UINT]
            native.user.GetAncestor.restype = native.w.HWND

            def widgets(parent):
                for child in parent.winfo_children():
                    yield child
                    yield from widgets(child)

            all_widgets = list(widgets(dialog))
            toggles = [w for w in all_widgets if w.winfo_class() == "TCheckbutton"]
            assert len(toggles) == 1 and not bool(app.root.getvar(toggles[0].cget("variable")))
            buttons = [w for w in all_widgets if w.winfo_class() == "TButton"]
            assert {w.cget("text") for w in buttons} == {
                "Сохранить",
                "Выключить",
                "Продолжить цикл",
                'Выбрать окно мышью через 3 секунды',
                'Обновить список',
                'Проверить окно',
                'Обновить персонажей',
            }
            for widget in all_widgets:
                assert (
                    widget.winfo_rooty() + widget.winfo_height()
                    <= dialog.winfo_rooty() + dialog.winfo_height()
                ), widget
            image = ImageGrab.grab(
                window=native.user.GetAncestor(dialog.winfo_id(),2), all_screens=True,
            )
            image.save(output / "settings.png")
            # Exercise the async checkbox/save/disable path without writing a live import.
            agent.write_import = lambda: None
            agent.fetch_manifest = lambda: {'clients': [{**agent.manifest['clients'][0], 'character': 'Holy'}]}
            next(w for w in buttons if w.cget('text') == 'Обновить персонажей').invoke()
            deadline = time.monotonic()+4
            while agent.manifest['clients'][0]['character'] != 'Holy' and time.monotonic() < deadline:
                app.root.update()
                time.sleep(.05)
            while any(str(w.cget('state')) == 'disabled' for w in buttons) and time.monotonic() < deadline:
                app.root.update()
                time.sleep(.05)
            assert agent.manifest['clients'][0]['character'] == 'Holy'
            from cennik.worker.window_picker import WindowPicker
            picker = next(w for w in all_widgets if isinstance(w, WindowPicker))
            assert picker.choice.current() == -1
            picker.choice.current(0)
            picker.choose()
            app.root.setvar(toggles[0].cget("variable"), True)
            next(w for w in buttons if w.cget("text") == "Сохранить").invoke()
            deadline = time.monotonic() + 4
            while not agent.cycle.settings.get("enabled") and time.monotonic() < deadline:
                app.root.update()
                time.sleep(0.05)
            assert agent.cycle.settings["enabled"]
            while (
                any(str(w.cget("state")) == "disabled" for w in buttons)
                and time.monotonic() < deadline
            ):
                app.root.update()
                time.sleep(0.05)
            next(w for w in buttons if w.cget("text") == "Выключить").invoke()
            assert not agent.cycle.settings["enabled"]
            app.root.update()
            print(
                json.dumps({"dialog": "passed", "default_enabled": False, "buttons_visible": True})
            )
        finally:
            app.quit()


if __name__ == "__main__":
    main()
