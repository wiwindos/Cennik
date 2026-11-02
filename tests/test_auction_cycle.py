import pytest
from PIL import Image, ImageDraw

from cennik.worker.automation import END, MAGIC, VERSION_COLOR, AuctionCycle, Signal, decode_signal

TARGET = {"hwnd": 1, "pid": 2, "started": 3, "path": "game/Wow.exe", "title": "WoW"}
ROUTE = {"id": "a" * 32, "role": "auctioneer"}


class Window:
    def __init__(self):
        self.value = Signal(3, 0, 1)
        self.background = True
        self.sent = []

    def windows(self, root):
        return [TARGET]

    def ready(self, target):
        return self.background and target == TARGET

    def signal(self, target, token):
        return self.value

    def press(self, target, key):
        assert self.ready(target)
        self.sent.append(key)


def cycle(tmp_path):
    clock = [0.0]
    window = Window()
    helper = AuctionCycle(tmp_path, window, lambda: clock[0])
    helper.configure(target=TARGET, route=ROUTE, game_root="game", enabled=True)

    def tick(code=3, boot=1, *, delivered=True, stamp=(10, 1), seconds=2):
        clock[0] += seconds
        window.value = Signal(code, int(clock[0]) % 16, boot)
        helper.tick(paused=False, route=ROUTE, stamp=stamp, delivered=delivered)

    return helper, window, clock, tick


def test_reload_waits_for_live_signal_save_delivery_and_reopens_once(tmp_path):
    helper, window, clock, tick = cycle(tmp_path)
    tick()
    assert window.sent == []
    tick()
    assert window.sent == ["F8"]
    assert helper.state["phase"] == "reload"
    tick()
    assert window.sent == ["F8"]
    tick(4, 2, stamp=(11, 2), delivered=False)
    assert helper.state["phase"] == "reopen"
    tick(4, 2, stamp=(11, 2), delivered=False, seconds=8)
    assert window.sent == ["F8"]
    tick(4, 2, stamp=(11, 2))
    assert window.sent == ["F8", "F9"]
    tick(2, 2, stamp=(11, 2))
    assert helper.state["phase"] == "watch" and helper.allow_idle
    helper.disable()
    tick(3, 2, stamp=(11, 2))
    assert window.sent == ["F8", "F9"] and not helper.settings["enabled"]


def test_reopen_is_bounded_and_explicit_resume_does_not_enable_other_characters(tmp_path):
    helper, window, clock, tick = cycle(tmp_path)
    tick(4)
    tick(4)
    for _ in range(3):
        tick(4, seconds=12)
    assert window.sent == ["F9"] * 3
    tick(4, seconds=12)
    assert helper.error_code == "AUCTION_REOPEN_FAILED"
    helper.reset()
    window.value = Signal(3, 5, 1)
    helper.tick(paused=False, route={**ROUTE, "id": "b" * 32}, stamp=(1, 1), delivered=True)
    helper.tick(paused=True, route=ROUTE, stamp=(1, 1), delivered=True)
    assert window.sent == ["F9"] * 3
    assert helper.wire({**ROUTE, "role": "crafter"}) == {"enabled": False}
    with pytest.raises(ValueError, match="Аукционер"):
        helper.configure(
            target=TARGET, route={**ROUTE, "role": "crafter"}, game_root="game", enabled=True
        )
    with pytest.raises(ValueError, match="разные"):
        helper.configure(
            target=TARGET,
            route=ROUTE,
            game_root="game",
            enabled=True,
            reload_key="F8",
            interact_key="F8",
        )


def test_stale_or_missing_signal_and_foreground_never_trigger_keys(tmp_path):
    helper, window, clock, tick = cycle(tmp_path)
    helper.tick(paused=False, route=ROUTE, stamp=(1, 1), delivered=True)
    clock[0] = 20
    helper.tick(paused=False, route=ROUTE, stamp=(1, 1), delivered=True)
    assert not window.sent
    window.value = None
    helper.tick(paused=False, route=ROUTE, stamp=(1, 1), delivered=True)
    assert "индикатор" in helper.message
    window.background = False
    tick()
    assert not window.sent
    window.background = True
    tick(6)
    tick(6)
    assert helper.error_code == "ADDON_BLOCKED" and not window.sent


def test_restart_keeps_uncertain_reload_and_never_sends_it_twice(tmp_path):
    helper, window, clock, tick = cycle(tmp_path)
    tick()
    tick()
    restarted = AuctionCycle(tmp_path, window, lambda: clock[0])
    assert restarted.state["phase"] == "reload"
    clock[0] += 121
    restarted.tick(paused=False, route=ROUTE, stamp=(1, 1), delivered=True)
    assert restarted.error_code == "RELOAD_TIMEOUT" and window.sent == ["F8"]


def test_signal_requires_matching_token_inverse_and_framing():
    token = "123456abcdef"
    image = Image.new("RGB", (320, 150), "black")
    draw = ImageDraw.Draw(image)
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
    for i, color in enumerate(colors):
        draw.rectangle((12 + i * 10, 12, 21 + i * 10, 21), fill=color)
    assert decode_signal(image, token)[0] == Signal(3, 7, 2)
    assert decode_signal(image, "000000000000") is None
    draw.rectangle((52, 12, 61, 21), fill="black")
    assert decode_signal(image, token) is None
