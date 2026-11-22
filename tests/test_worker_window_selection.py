from types import SimpleNamespace

import pytest
from PIL import Image

from cennik.worker.automation import AuctionCycle
from cennik.worker.idle import IdleProbe, WindowsGame, same_window, selected_window
from cennik.worker.window_picker import WindowPicker

FIRST = {'hwnd': 123, 'pid': 456, 'started': 10, 'path': 'game/Wow.exe', 'title': 'wiw',
         'x': 0, 'y': 0, 'width': 1920, 'height': 1080, 'monitor': 'DISPLAY1'}
SECOND = {**FIRST, 'hwnd': 987, 'pid': 9563, 'started': 20, 'title': 'holy',
          'x': -1920, 'monitor': 'DISPLAY2'}


class Windows:
    def __init__(self):
        self.items = [dict(FIRST), dict(SECOND)]
        self.active = self.items[1]

    def windows(self, root):
        return self.items

    def foreground(self, root):
        return self.active


@pytest.fixture(scope='module')
def tk_root():
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def picker(tk_root):
    backend = Windows()
    control = WindowPicker(tk_root, backend, 'game')
    yield control, backend
    control.destroy()


def test_unselected_dialog_never_silently_targets_the_first_window(picker):
    control, backend = picker
    assert control.choice.current() == -1
    with pytest.raises(ValueError, match='не выбрано'):
        control.selected()
    control.choice.current(1)
    control.choose()
    assert control.selected()['pid'] == 9563
    backend.items.reverse()
    backend.items[0].update(title='renamed', x=50, monitor='DISPLAY1')
    control.refresh()
    assert control.choice.current() == 0
    assert control.selected()['hwnd'] == SECOND['hwnd']
    assert 'PID 9563' in control.note.get()
    assert selected_window(SECOND, backend.items)['title'] == 'renamed'


def test_countdown_selects_actual_foreground_window_on_second_monitor(picker, monkeypatch):
    control, backend = picker
    callbacks = []
    monkeypatch.setattr(control, 'after', lambda delay, fn, value: callbacks.append((fn, value)))
    control.pick()
    while callbacks:
        fn, value = callbacks.pop(0)
        fn(value)
    assert control.selected() == SECOND
    assert 'DISPLAY2' in control.note.get() and '-1920' in control.note.get()
    backend.active = None
    control.pick()
    while callbacks:
        fn, value = callbacks.pop(0)
        fn(value)
    assert control.selected() == SECOND
    assert 'не относится' in control.note.get()


def test_closed_or_reused_window_requires_explicit_reselection(picker):
    control, backend = picker
    control.target = dict(SECOND)
    backend.items[1] = {**SECOND, 'started': 999}
    control.refresh()
    assert control.choice.current() == -1 and control.target is None
    assert not same_window(SECOND, backend.items[1])
    assert not same_window(SECOND, {**SECOND, 'hwnd': 666})


def test_background_and_cycle_config_keep_identity_across_title_and_monitor_changes(tmp_path):
    backend = Windows()
    backend.items[1] = {**SECOND, 'title': 'new title', 'x': 40, 'monitor': 'DISPLAY1'}
    idle = IdleProbe(tmp_path, backend)
    idle.configure(SECOND, True, 'game')
    assert idle.settings['target'] == backend.items[1]
    cycle = AuctionCycle(tmp_path, backend)
    cycle.configure(target=SECOND, route={'id': 'a'*32, 'role': 'auctioneer'}, game_root='game', enabled=True)
    assert cycle.settings['target']['pid'] == 9563
    assert cycle.settings['target']['hwnd'] == 987


def test_preview_addresses_exact_window_instead_of_pixels_on_another_monitor(monkeypatch):
    backend = object.__new__(WindowsGame)
    backend.identity = lambda hwnd: dict(SECOND)
    backend.geometry = lambda hwnd: dict(SECOND)
    backend.c = SimpleNamespace(c_void_p=lambda value: value)
    backend.user = SimpleNamespace(IsIconic=lambda hwnd: False, SetThreadDpiAwarenessContext=lambda value: -4)
    calls = []
    monkeypatch.setattr('PIL.ImageGrab.grab', lambda **kwargs: calls.append(kwargs) or Image.new('RGB', (1920, 1080), 'white'))
    backend.preview(SECOND)
    assert calls == [{'window': SECOND['hwnd'], 'all_screens': True}]
    backend.identity = lambda hwnd: {**SECOND, 'pid': FIRST['pid']}
    with pytest.raises(ValueError):
        backend.preview(SECOND)
    assert len(calls) == 1
