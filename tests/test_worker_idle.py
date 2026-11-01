import pytest

from cennik.worker.idle import IdleProbe


class GameWindow:
    target = {
        "hwnd": 123,
        "pid": 456,
        "started": 10,
        "path": "fixture/Wow.exe",
        "title": "Fixture WoW",
    }

    def __init__(self):
        self.green = False
        self.sent = 0

    def windows(self, root):
        return [self.target]

    def ready(self, target):
        return self.green and target == self.target

    def jump(self, target):
        assert self.ready(target)
        self.sent += 1


def test_idle_is_opt_in_addressed_and_blocks_unready_or_paused_windows(tmp_path):
    game = GameWindow()
    probe = IdleProbe(tmp_path, game)
    assert not probe.tick(test=True)
    with pytest.raises(ValueError):
        probe.configure({**game.target, "pid": 999}, True, "fixture")
    probe.configure(game.target, True, "fixture")
    probe.last = 0
    assert not probe.tick() and game.sent == 0
    game.green = True
    assert not probe.tick(paused=True, test=True)
    assert probe.tick(test=True) and game.sent == 1
    assert not probe.tick() and game.sent == 1
    probe.disable()
    probe.last = 0
    assert not probe.tick() and game.sent == 1
    assert not IdleProbe(tmp_path, game).settings["enabled"]
