"""Small Windows tray application for public observations only."""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ahledger.util import atomic_json, exclusive_lock

from .agent import VERSION, CollectorAgent, default_root, discover_games
from .startup import set_startup, startup_enabled


def icon_image(color="#2a9d8f"):
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 62, 62), radius=16, fill=color)
    draw.arc((16, 14, 49, 49), 45, 315, fill="white", width=7)
    return image


def bind_entry_editing(entry):
    """Windows Ctrl shortcuts use physical keys, including under Russian layouts."""

    def paste(event=None):
        try:
            value = entry.clipboard_get()
            if entry.selection_present():
                entry.delete("sel.first", "sel.last")
            entry.insert("insert", value)
        except tk.TclError:
            pass
        return "break"

    def select_all():
        entry.selection_range(0, "end")
        entry.icursor("end")

    def shortcut(event):
        # AltGr is not a clipboard shortcut.
        if event.state & 0x20008:
            return None
        key = {65: "a", 67: "c", 86: "v", 88: "x"}.get(event.keycode) if os.name == "nt" else None
        key = key or event.keysym.lower()
        if key == "v":
            return paste()
        if key == "a":
            select_all()
            return "break"
        if key in {"c", "x"}:
            entry.event_generate("<<Copy>>" if key == "c" else "<<Cut>>")
            return "break"
        return None

    entry.bind("<Control-KeyPress>", shortcut)
    entry.bind("<Shift-Insert>", paste)
    menu = tk.Menu(entry, tearoff=False)
    menu.add_command(label="Вырезать", command=lambda: entry.event_generate("<<Cut>>"))
    menu.add_command(label="Копировать", command=lambda: entry.event_generate("<<Copy>>"))
    menu.add_command(label="Вставить", command=paste)
    menu.add_separator()
    menu.add_command(label="Выделить всё", command=select_all)

    def popup(event):
        entry.focus_set()
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    entry.bind("<Button-3>", popup)
    return entry


class CollectorWindow:
    def __init__(self, agent, *, background=False, worker=False):
        import pystray

        self.agent = agent
        self.worker = worker
        self.product = "Cennik Worker" if worker else "Cennik Collector"
        if worker:
            from cennik.worker.agent import VERSION as WORKER_VERSION
        self.version = WORKER_VERSION if worker else VERSION
        self.startup_name = "CennikWorker" if worker else "CennikCollector"
        self.root = tk.Tk()
        self.root.title(self.product)
        self.root.geometry("660x740")
        self.root.minsize(620, 720)
        self.root.configure(background="#f2f6f7")
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.commands: queue.Queue = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="collector-settings")
        self.future: Future | None = None
        self.closed = False
        self.last_status = ""
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f2f6f7")
        style.configure("TLabel", background="#f2f6f7", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 20, "bold"))
        style.configure("State.TLabel", font=("Segoe UI", 13, "bold"), foreground="#227c70")
        style.configure("TButton", font=("Segoe UI", 10), padding=(9, 6))
        style.configure("TCheckbutton", background="#f2f6f7", font=("Segoe UI", 10))
        frame = ttk.Frame(self.root, padding=22)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text=self.product, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            frame,
            text="Мост аукционера и крафтеров · аналитика на сервере"
            if worker
            else "Только сбор данных · без стратегий и закупок",
        ).grid(row=1, column=0, sticky="w", pady=(2, 16))
        self.status = tk.StringVar(value="Запуск…")
        self.detail = tk.StringVar()
        ttk.Label(frame, textvariable=self.status, style="State.TLabel").grid(
            row=2, column=0, sticky="w"
        )
        ttk.Label(frame, textvariable=self.detail, wraplength=570, justify="left").grid(
            row=3, column=0, sticky="ew", pady=(6, 15)
        )
        self.game = tk.StringVar(value=agent.settings.get("game_root", ""))
        self.url = tk.StringVar(
            value=agent.client.public_connection()["url"]
            or (getattr(agent, "pairing", None) or {}).get("url", "")
        )
        self.token = tk.StringVar()
        self.autostart = tk.BooleanVar(value=startup_enabled(name=self.startup_name))
        ttk.Label(frame, text="Папка игры (рядом с Interface и WTF)").grid(
            row=4, column=0, sticky="w"
        )
        games = ttk.Frame(frame)
        games.grid(row=5, column=0, sticky="ew", pady=(4, 10))
        games.columnconfigure(0, weight=1)
        self.game_entry = bind_entry_editing(ttk.Entry(games, textvariable=self.game))
        self.game_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(games, text="Выбрать…", command=self.choose_game).grid(
            row=0, column=1, padx=(8, 0)
        )
        ttk.Label(frame, text="Адрес сервера, который выдал владелец").grid(
            row=6, column=0, sticky="w"
        )
        self.url_entry = bind_entry_editing(ttk.Entry(frame, textvariable=self.url))
        self.url_entry.grid(row=7, column=0, sticky="ew", pady=(4, 10))
        ttk.Label(
            frame,
            text="Ключ рабочего клиента (не ключ владельца)"
            if worker
            else "Личный ключ сборщика (не ключ владельца)",
        ).grid(row=8, column=0, sticky="w")
        self.token_entry = bind_entry_editing(ttk.Entry(frame, textvariable=self.token, show="•"))
        self.token_entry.grid(row=9, column=0, sticky="ew", pady=(4, 4))
        ttk.Label(
            frame,
            text="Без ключа: получите код подключения и подтвердите его в вебе.\nЕсли прежний ключ отозван, оставьте поле пустым и сохраните настройки."
            if worker
            else "Пустое поле сохраняет прежний ключ для того же сервера.",
            wraplength=570,
        ).grid(row=10, column=0, sticky="w", pady=(0, 10))
        ttk.Checkbutton(
            frame, text="Запускать с Windows и работать в трее", variable=self.autostart
        ).grid(row=11, column=0, sticky="w", pady=(0, 8))
        self.save_button = ttk.Button(frame, text="Сохранить и установить аддон", command=self.save)
        self.save_button.grid(row=12, column=0, sticky="w", pady=(0, 10))
        ttk.Separator(frame).grid(row=13, column=0, sticky="ew", pady=7)
        ttk.Label(
            frame,
            text="После установки полностью перезапустите игру.\nДанные отправляются после /reload или штатного выхода.\nНеизвестного аукциониста сначала подтверждает владелец.",
            wraplength=570,
            justify="left",
        ).grid(row=14, column=0, sticky="w", pady=(4, 12))
        buttons = ttk.Frame(frame)
        buttons.grid(row=15, column=0, sticky="ew")
        self.pause_button = ttk.Button(buttons, text="Пауза", command=self.toggle_pause)
        self.pause_button.pack(side="left")
        ttk.Button(buttons, text="Повторить отправку", command=self.retry).pack(side="left", padx=6)
        ttk.Button(buttons, text="Скопировать отчёт", command=self.copy_report).pack(side="left")
        ttk.Button(frame, text="Свернуть в трей", command=self.hide).grid(
            row=16, column=0, sticky="w", pady=(14, 0)
        )
        if worker:
            ttk.Button(frame, text="Автоцикл аукционера…", command=self.cycle_dialog).grid(
                row=17, column=0, sticky="e", pady=(8, 0)
            )
            ttk.Button(frame, text="Прыжок / Space…", command=self.idle_dialog).grid(
                row=16, column=0, sticky="e", pady=(14, 0)
            )
        self.tray = pystray.Icon(
            self.startup_name,
            icon_image(),
            self.product,
            pystray.Menu(
                pystray.MenuItem("Открыть", lambda: self.commands.put("show"), default=True),
                pystray.MenuItem("Пауза / продолжить", lambda: self.commands.put("pause")),
                pystray.MenuItem("Скопировать отчёт", lambda: self.commands.put("report")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Выход", lambda: self.commands.put("quit")),
            ),
        )
        self.tray_error = ""

        def tray_run():
            try:
                self.tray.run()
            except Exception:
                self.tray_error = "Трей недоступен. Оставьте окно свёрнутым на панели задач"
                self.commands.put("show")

        threading.Thread(target=tray_run, name="collector-tray", daemon=True).start()
        if background and agent.settings.get("game_root"):
            self.root.withdraw()
        self.discovery: Future | None = None
        if not self.game.get():
            self.discovery = self.pool.submit(discover_games)
        else:
            self.discovery = None
        agent.start()
        self.root.after(100, self.poll)

    def choose_game(self):
        path = filedialog.askdirectory(title="Папка игры", parent=self.root)
        if path:
            self.game.set(path)

    def cycle_dialog(self):
        from cennik.worker.automation import KEYS

        helper = self.agent.cycle
        try:
            windows = helper.engine().windows(
                self.agent.settings.get("game_root") or self.game.get()
            )
            routes = [
                r
                for r in (self.agent.manifest or {}).get("clients", [])
                if r["role"] == "auctioneer"
            ]
        except Exception as error:
            messagebox.showerror("Автоцикл аукционера", str(error), parent=self.root)
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Cennik: автоцикл аукционера")
        dialog.geometry("700x645")
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="Отдельный режим; по умолчанию выключен. Настройки относятся только к выбранному персонажу и окну.\n"
            "Назначьте в игре две разные клавиши: макрос /reload и штатное «Взаимодействие с целью».\n"
            "Окно может быть неактивным, но должно оставаться видимым. Верхний левый угол нельзя перекрывать.",
            wraplength=650,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))
        ttk.Label(frame, text="Окно игры").pack(anchor="w")
        window = ttk.Combobox(
            frame,
            state="readonly",
            values=[f"{w['title']} · PID {w['pid']} · {w['hwnd']}" for w in windows],
        )
        window.pack(fill="x")
        if windows:
            window.current(
                next((i for i, w in enumerate(windows) if w == helper.settings.get("target")), 0)
            )
        ttk.Label(
            frame, text="Персонаж-аукционер (обнаруживается после выбора роли и /reload)"
        ).pack(anchor="w", pady=(10, 0))
        character = ttk.Combobox(
            frame, state="readonly", values=[r["character"] + " · " + r["realm"] for r in routes]
        )
        character.pack(fill="x")
        if routes:
            character.current(
                next(
                    (
                        i
                        for i, r in enumerate(routes)
                        if r["id"] == helper.settings.get("client_id")
                    ),
                    0,
                )
            )
        fields = ttk.Frame(frame)
        fields.pack(fill="x", pady=12)
        reload_key = tk.StringVar(value=helper.settings.get("reload_key", "F8"))
        interact_key = tk.StringVar(value=helper.settings.get("interact_key", "F9"))
        minutes = tk.StringVar(value=str(helper.settings.get("minutes", 15)))
        delay = tk.StringVar(value=str(helper.settings.get("delay", 5)))
        for i, (label, value, choices) in enumerate(
            [
                ("Клавиша /reload", reload_key, KEYS),
                ("Клавиша взаимодействия", interact_key, KEYS),
                ("Сохранение без нового полного скана, минут", minutes, None),
                ("Задержка перед взаимодействием, секунд", delay, None),
            ]
        ):
            ttk.Label(fields, text=label).grid(row=i, column=0, sticky="w", pady=3)
            widget = (
                ttk.Combobox(fields, state="readonly", values=choices, textvariable=value, width=10)
                if choices
                else ttk.Entry(fields, textvariable=value, width=12)
            )
            widget.grid(row=i, column=1, sticky="w", padx=14)
        enabled = tk.BooleanVar(value=helper.settings.get("enabled", False))
        ttk.Checkbutton(
            frame,
            text="Включить автоматическое сохранение и восстановление аукциона",
            variable=enabled,
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text="После сохранения настроек один раз сделайте /reload вручную. Затем откройте подтверждённого\n"
            "аукциониста и оставьте его выбранной целью. Включите нужные цели снайпера и сбор.\n"
            "Полный скан сохраняется после окончания обработки. После /reload ждём подтверждения сервера,\n"
            "затем открываем NPC и восстанавливаем прежние цели. После трёх неудачных открытий — остановка.",
            wraplength=650,
            justify="left",
        ).pack(anchor="w", pady=10)
        result = tk.StringVar(value=helper.message)
        ttk.Label(frame, textvariable=result, wraplength=650).pack(anchor="w", pady=5)
        buttons = ttk.Frame(frame)
        buttons.pack(anchor="w", pady=8)

        def apply():
            try:
                active = enabled.get()
                parameters = {}
                if active:
                    if window.current() < 0 or character.current() < 0:
                        raise ValueError(
                            "Запустите окно игры и назначьте аукционера через /cennik role"
                        )
                    parameters = dict(
                        target=windows[window.current()],
                        route=routes[character.current()],
                        game_root=self.agent.settings["game_root"],
                        enabled=True,
                        reload_key=reload_key.get(),
                        interact_key=interact_key.get(),
                        minutes=int(minutes.get()),
                        delay=int(delay.get()),
                    )
                else:
                    helper.disable()

                def commit():
                    with self.agent.lock:
                        if active:
                            helper.configure(**parameters)
                        self.agent.write_import()
                        self.agent.wake.set()
                    return helper.message

                pending = self.pool.submit(commit)
                result.set(
                    "Сохраняем настройки и адресный импорт. Дождитесь подтверждения перед /reload"
                )
                for b in buttons.winfo_children():
                    if isinstance(b, ttk.Button):
                        b.configure(state="disabled")

                def finish():
                    if not dialog.winfo_exists():
                        return
                    if not pending.done():
                        dialog.after(100, finish)
                        return
                    try:
                        result.set(pending.result())
                    except Exception as error:
                        result.set(str(error))
                    for b in buttons.winfo_children():
                        if isinstance(b, ttk.Button):
                            b.configure(state="normal")

                dialog.after(100, finish)
            except Exception as error:
                result.set(str(error))

        def disable():
            enabled.set(False)
            apply()

        def resume():
            helper.reset()
            self.agent.wake.set()
            result.set("Остановка сброшена. Продолжим только при живом подтверждённом индикаторе")

        ttk.Button(buttons, text="Сохранить", command=apply).pack(side="left")
        ttk.Button(buttons, text="Выключить", command=disable).pack(side="left", padx=8)
        ttk.Button(buttons, text="Продолжить цикл", command=resume).pack(side="left")

    def idle_dialog(self):
        helper = self.agent.idle
        try:
            windows = helper.windows(self.agent.settings.get("game_root") or self.game.get())
        except Exception as error:
            messagebox.showerror("Прыжок / Space", str(error), parent=self.root)
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Cennik: прыжок в фоновом окне")
        dialog.geometry("610x320")
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="Выберите окно WoW. Worker отправляет нажатие и отпускание Space\nтолько в это окно, пока оно неактивно, без переключения фокуса.\nРазрешение в аддоне не требуется. В игре Space должен быть назначен на прыжок.",
            wraplength=565,
        ).pack(anchor="w", pady=(0, 12))
        choice = ttk.Combobox(
            frame,
            state="readonly",
            values=[f"{w['title']} · PID {w['pid']} · окно {w['hwnd']}" for w in windows],
            width=70,
        )
        choice.pack(fill="x")
        if windows:
            choice.current(0)
        enabled = tk.BooleanVar(value=helper.settings.get("enabled", False))
        ttk.Checkbutton(
            frame, text="Отправлять Space примерно раз в 3 минуты", variable=enabled
        ).pack(anchor="w", pady=10)
        result = tk.StringVar(value=helper.message)
        ttk.Label(frame, textvariable=result, wraplength=565).pack(anchor="w")
        buttons = ttk.Frame(frame)
        buttons.pack(anchor="w", pady=12)

        def apply(test=False):
            try:
                if choice.current() < 0:
                    raise ValueError("Окно WoW не найдено. Запустите игру из выбранной папки")
                helper.configure(
                    windows[choice.current()], enabled.get(), self.agent.settings["game_root"]
                )
                if test:
                    helper.tick(paused=self.agent.settings["paused"], test=True)
                result.set(
                    helper.message
                    if test
                    else "Настройка сохранена. Space отправляется только в выбранное неактивное окно."
                )
            except Exception as error:
                result.set(str(error))

        ttk.Button(buttons, text="Сохранить", command=apply).pack(side="left")
        ttk.Button(buttons, text="Отправить Space сейчас", command=lambda: apply(True)).pack(
            side="left", padx=6
        )

        def disable():
            helper.disable()
            enabled.set(False)
            result.set(helper.message)

        ttk.Button(buttons, text="Выключить", command=disable).pack(side="left")

    def save(self):
        if self.future:
            return
        game, url, token, autostart = (
            self.game.get(),
            self.url.get(),
            self.token.get(),
            self.autostart.get(),
        )

        def configure():
            result = self.agent.configure(game, url, token)
            set_startup(
                autostart,
                self.agent.root,
                name=self.startup_name,
                module="cennik.worker.desktop" if self.worker else "cennik.collector.desktop",
            )
            return result

        self.save_button.configure(state="disabled")
        self.status.set("Установка аддона…")
        self.future = self.pool.submit(configure)

    def toggle_pause(self):
        if not self.future:
            self.future = self.pool.submit(self.agent.pause, not self.agent.settings["paused"])

    def retry(self):
        self.agent.client.next_attempt = 0
        self.agent.wake.set()

    def copy_report(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.agent.report())
        self.root.update_idletasks()
        self.detail.set("Отчёт скопирован. Передайте его владельцу сервера")

    def hide(self):
        if self.tray.visible and not self.tray_error:
            self.root.withdraw()
        else:
            self.root.iconify()

    def show(self):
        self.root.deiconify()
        self.root.lift()

    def poll(self):
        if self.closed:
            return
        while not self.commands.empty():
            action = self.commands.get_nowait()
            if action == "quit":
                self.quit()
                return
            {"show": self.show, "pause": self.toggle_pause, "report": self.copy_report}[action]()
        signal = self.agent.root / "show.request"
        if signal.exists():
            signal.unlink(missing_ok=True)
            self.show()
        if self.discovery and self.discovery.done():
            found = self.discovery.result()
            if found and not self.game.get():
                self.game.set(found[0])
            self.discovery = None
        if self.future and self.future.done():
            try:
                result = self.future.result()
                self.token.set("")
                if result:
                    messagebox.showinfo(
                        self.product,
                        (
                            "Подтвердите код "
                            + result["pair_code"]
                            + " в главной панели сервера.\n"
                            if result.get("pair_code")
                            else ""
                        )
                        + "Аддон установлен. Полностью перезапустите игру.\nПриложение можно свернуть в трей",
                        parent=self.root,
                    )
            except Exception as error:
                messagebox.showerror("Не удалось сохранить", str(error), parent=self.root)
            self.future = None
            self.save_button.configure(state="normal")
        d = self.agent.overview()
        if not self.future:
            self.status.set(d["status"])
        message = (
            d["error"]
            or self.tray_error
            or d.get("notice")
            or (
                "Ждём сохранения игры. При открытии аукциона аддон начнёт проверку через 3 секунды"
                if d["game_root"]
                else "Укажите игру, адрес сервера и личный ключ. Auctionator должен быть установлен"
            )
        )
        last = d["delivery"].get("at")
        stamp = time.strftime("%d.%m %H:%M", time.localtime(last)) if last else "ещё нет"
        self.detail.set(f"{message}\nВ очереди: {d['pending']} · подтверждение сервера: {stamp}")
        if d["game_code"]:
            when = time.strftime("%d.%m %H:%M", time.localtime(d["game_at"]))
            self.detail.set(
                self.detail.get()
                + f"\nИз сохранения игры {when}: {d['game_status']} [{d['game_code']}]"
            )
        self.pause_button.configure(text="Продолжить" if d["paused"] else "Пауза")
        color = (
            "#c64343"
            if d["error"]
            else "#d19a28"
            if d["paused"] or not d["game_root"]
            else "#2a9d8f"
        )
        if self.last_status != color + d["status"]:
            self.tray.icon = icon_image(color)
            self.last_status = color + d["status"]
        self.tray.title = ("Cennik · " + d["status"] + " · " + message)[:127]
        self.root.after(1000, self.poll)

    def quit(self):
        self.closed = True
        self.root.withdraw()
        self.tray.stop()
        self.agent.close()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()

    def smoke(self, path):
        from PIL import ImageGrab

        try:
            self.show()
            self.root.update()
            path.parent.mkdir(parents=True, exist_ok=True)
            ImageGrab.grab(window=self.root.winfo_id()).save(path.with_suffix(".png"))
            self.hide()
            self.root.update()
            hidden = self.root.state() == "withdrawn"
            self.show()
            self.root.update()
            atomic_json(
                path,
                {
                    "version": self.version,
                    "frozen": bool(getattr(sys, "frozen", False)),
                    "tray_visible": self.tray.visible,
                    "hide_to_tray": hidden,
                    "restored": self.root.state() == "normal",
                    "sql_loaded": any(
                        name in sys.modules for name in ("duckdb", "sqlite3", "pyarrow")
                    ),
                    "status": self.agent.overview()["status"],
                },
            )
        finally:
            self.quit()


def main():
    parser = argparse.ArgumentParser(description="Cennik Collector: сбор данных в трее")
    parser.add_argument("--root", type=Path, default=default_root())
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--smoke-test", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--report", type=Path, help="Записать безопасный отчёт диагностики и завершить работу"
    )
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Приложение в трее поддерживает Windows")
    try:
        with exclusive_lock(args.root):
            agent = CollectorAgent(args.root)
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(agent.report(), "utf-8")
                return
            app = CollectorWindow(agent, background=args.background)
            if args.smoke_test:
                app.root.after(6000, lambda: app.smoke(args.smoke_test))
            app.root.mainloop()
    except RuntimeError as error:
        if str(error) != "Another writer is using this archive":
            raise
        atomic_json(args.root / "show.request", {"show": True})
    except Exception:
        # Raw exceptions can contain file paths or secrets; use a stable code.
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Cennik Collector",
            "Не удалось запустить приложение [STARTUP_FAILED]. Проверьте доступ к папке профиля и версию приложения",
            parent=root,
        )
        root.destroy()
        raise


if __name__ == "__main__":
    main()
