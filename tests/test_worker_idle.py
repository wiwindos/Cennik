from types import SimpleNamespace

import pytest

from cennik.worker.idle import IdleProbe, WindowsGame


class GameWindow:
    target = {
        "hwnd": 123,
        "pid": 456,
        "started": 10,
        "path": "fixture/Wow.exe",
        "title": "Fixture WoW",
    }

    def __init__(self):
        self.background = False
        self.sent = 0

    def windows(self, root):
        return [self.target]

    def ready(self, target):
        return self.background and target == self.target

    def jump(self, target):
        assert self.ready(target)
        self.sent += 1


def test_space_is_opt_in_addressed_and_blocks_active_or_paused_windows(tmp_path):
    game = GameWindow()
    probe = IdleProbe(tmp_path, game)
    assert not probe.tick(test=True)
    with pytest.raises(ValueError):
        probe.configure({**game.target, "pid": 999}, True, "fixture")
    probe.configure(game.target, True, "fixture")
    probe.last = 0
    assert not probe.tick() and game.sent == 0
    game.background = True
    assert not probe.tick(paused=True, test=True)
    assert probe.tick(test=True) and game.sent == 1
    assert not probe.tick() and game.sent == 1
    probe.disable()
    probe.last = 0
    assert not probe.tick() and game.sent == 1
    assert not IdleProbe(tmp_path, game).settings["enabled"]


def test_background_space_needs_no_addon_pixels_and_keeps_keyup_on_failure(monkeypatch):
    game = object.__new__(WindowsGame)
    target = dict(GameWindow.target)
    foreground = [999]
    modifiers = [False]
    messages = []
    game.identity = lambda hwnd: dict(target)
    game.user = SimpleNamespace(
        GetForegroundWindow=lambda: foreground[0],
        GetAsyncKeyState=lambda key: 0x8000 if modifiers[0] else 0,
        PostMessageW=lambda *args: messages.append(args) or True,
    )
    assert game.ready(target)  # No pixel access or addon permission exists.
    monkeypatch.setattr("cennik.worker.idle.time.sleep", lambda seconds: None)
    game.jump(target)
    assert [(m[0], m[1], m[2]) for m in messages] == [(123, 0x100, 0x20), (123, 0x101, 0x20)]
    foreground[0] = target["hwnd"]
    assert not game.ready(target)
    foreground[0] = 999
    modifiers[0] = True
    assert not game.ready(target)
    modifiers[0] = False
    assert not game.ready({**target, "started": 999})
    monkeypatch.setattr("cennik.worker.idle.time.sleep", lambda seconds: (_ for _ in ()).throw(OSError("fixture")))
    with pytest.raises(OSError):
        game.jump(target)
    assert messages[-1][1:3] == (0x101, 0x20)
