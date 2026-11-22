"""Select an exact game window; titles and dropdown order are never identities."""
import tkinter as tk
from tkinter import ttk

from .idle import selected_window


def window_label(window):
    return f"PID {window['pid']} · HWND {window['hwnd']} · {window.get('monitor', 'экран')} · {window.get('title') or 'без заголовка'}"


class WindowPicker(ttk.Frame):
    def __init__(self, parent, backend, game_root, target=None):
        super().__init__(parent)
        self.backend, self.game_root, self.target = backend, game_root, target
        self.windows = []
        self.timer = None
        self.choice = ttk.Combobox(self, state='readonly')
        self.choice.pack(fill='x')
        self.choice.bind('<<ComboboxSelected>>', self.choose)
        row = ttk.Frame(self)
        row.pack(fill='x', pady=(5, 0))
        self.pick_button = ttk.Button(row, text='Выбрать окно мышью через 3 секунды', command=self.pick)
        self.pick_button.pack(side='left')
        ttk.Button(row, text='Обновить список', command=self.refresh).pack(side='left', padx=5)
        ttk.Button(row, text='Проверить окно', command=self.preview).pack(side='left')
        self.note = tk.StringVar()
        ttk.Label(self, textvariable=self.note, wraplength=640, justify='left').pack(anchor='w', pady=(5, 0))
        self.refresh()
        self.bind('<Destroy>', self.destroyed, add=True)

    def destroyed(self, event):
        if event.widget is self and self.timer:
            self.after_cancel(self.timer)
            self.timer = None

    def refresh(self):
        self.windows = self.backend.windows(self.game_root)
        self.choice.configure(values=[window_label(item) for item in self.windows])
        current = selected_window(self.target, self.windows)
        self.target = current
        if current:
            self.choice.current(self.windows.index(current))
            self.describe()
        else:
            self.choice.set('')
            self.note.set('Окно не выбрано. Нажмите «Выбрать окно мышью…», затем щёлкните нужное окно WoW на любом мониторе.')

    def choose(self, event=None):
        index = self.choice.current()
        self.target = self.windows[index] if index >= 0 else None
        self.describe()

    def describe(self):
        item = self.target
        if item:
            self.note.set(window_label(item)+f"\nПоложение: {item.get('x', '?')}, {item.get('y', '?')}; размер: {item.get('width', '?')}×{item.get('height', '?')}. Заголовок окна не подтверждает имя персонажа.")

    def selected(self):
        current = selected_window(self.target, self.backend.windows(self.game_root))
        if current is None:
            raise ValueError('Нужное окно WoW не выбрано или уже перезапущено. Выберите его заново.')
        self.target = current
        return current

    def pick(self):
        self.pick_button.configure(state='disabled')

        def countdown(left):
            self.timer = None
            if left:
                self.note.set(f'Щёлкните нужное окно WoW. Выбор через {left} с. Поддерживаются оба монитора.')
                self.timer = self.after(1000, countdown, left-1)
                return
            try:
                current = self.backend.foreground(self.game_root)
                if current is None:
                    raise ValueError('Активное окно не относится к выбранной папке WoW. Повторите выбор.')
                self.target = current
                self.refresh()
            except (OSError, ValueError) as error:
                self.note.set(str(error))
            finally:
                self.pick_button.configure(state='normal')
        countdown(3)

    def preview(self):
        try:
            from PIL import ImageTk
            target = self.selected()
            bitmap = self.backend.preview(target)
            bitmap.thumbnail((760, 430))
            dialog = tk.Toplevel(self)
            dialog.title('Проверка выбранного окна · PID '+str(target['pid']))
            ttk.Label(dialog, text=window_label(target), wraplength=760).pack(padx=12, pady=8)
            label = ttk.Label(dialog)
            label.image = ImageTk.PhotoImage(bitmap, master=dialog)
            label.configure(image=label.image)
            label.pack(padx=12, pady=8)
            ttk.Label(dialog, text='Снимок выбранного HWND. Проверьте персонажа перед сохранением настройки.', wraplength=760).pack(padx=12, pady=8)
        except (OSError, ValueError) as error:
            self.note.set(str(error))
