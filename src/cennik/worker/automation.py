"""Opt-in, addressed reload/reopen cycle driven by a live addon signal."""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from ahledger.util import atomic_json

from .idle import WindowsGame

KEYS = tuple(f"F{i}" for i in range(1, 13))
MAGIC = (219, 51, 177)
END = (41, 211, 89)
VERSION_COLOR = (16, 32, 48)


@dataclass(frozen=True)
class Signal:
    code: int
    heartbeat: int
    boot: int


def decode_signal(image, token):
    """Seven physical 10 px cells; token and inverse data reject unrelated pixels."""
    if len(token) != 12:
        return None
    colors = [tuple(bytes.fromhex(token[i : i + 6])) for i in (0, 6)]

    def near(a, b):
        return all(abs(x - y) <= 5 for x, y in zip(a[:3], b, strict=True))

    pixels = image.convert("RGB")
    for y in range(0, min(pixels.height, 220), 2):
        for x in range(0, min(pixels.width - 69, 360), 2):
            if not near(pixels.getpixel((x, y)), MAGIC):
                continue
            row = [pixels.getpixel((x + i * 10, y)) for i in range(7)]
            if not (
                near(row[1], colors[0])
                and near(row[2], colors[1])
                and near(row[5], END)
                and near(row[6], VERSION_COLOR)
            ):
                continue
            r, g, b = row[3]
            code, heartbeat, boot = round((r - 32) / 24), round((g - 16) / 14), round((b - 16) / 14)
            if not (0 <= code <= 7 and 0 <= heartbeat <= 15 and 0 <= boot <= 15):
                continue
            canonical = (32 + code * 24, 16 + heartbeat * 14, 16 + boot * 14)
            if near(row[3], canonical) and near(row[4], tuple(255 - v for v in canonical)):
                return Signal(code, heartbeat, boot), (x + 35, y)
    return None


class CycleWindows(WindowsGame):
    def signal(self, target, token):
        """Read a visible, unobscured client region; never activate or unminimize it."""
        from PIL import ImageGrab

        user, w, c = self.user, self.w, self.c
        user.IsIconic.argtypes = [w.HWND]
        if user.IsIconic(target["hwnd"]):
            return None
        user.ClientToScreen.argtypes = [w.HWND, c.POINTER(w.POINT)]
        user.GetClientRect.argtypes = [w.HWND, c.POINTER(w.RECT)]
        user.WindowFromPoint.argtypes = [w.POINT]
        user.WindowFromPoint.restype = w.HWND
        user.IsChild.argtypes = [w.HWND, w.HWND]
        user.SetThreadDpiAwarenessContext.argtypes = [c.c_void_p]
        user.SetThreadDpiAwarenessContext.restype = c.c_void_p
        previous = user.SetThreadDpiAwarenessContext(c.c_void_p(-4))
        try:
            origin, rect = w.POINT(), w.RECT()
            if not user.ClientToScreen(target["hwnd"], c.byref(origin)) or not user.GetClientRect(
                target["hwnd"], c.byref(rect)
            ):
                return None
            width, height = min(440, rect.right), min(220, rect.bottom)
            if width < 70 or height < 10:
                return None
            captured = ImageGrab.grab(
                bbox=(origin.x, origin.y, origin.x + width, origin.y + height), all_screens=True
            )
            result = decode_signal(captured, token)
            if not result:
                return None
            signal, (x, y) = result
            visible = user.WindowFromPoint(w.POINT(origin.x + x, origin.y + y))
            if visible != target["hwnd"] and not user.IsChild(target["hwnd"], visible):
                return None
            return signal
        finally:
            if previous:
                user.SetThreadDpiAwarenessContext(previous)


class AuctionCycle:
    def __init__(self, root, backend=None, clock=None):
        self.root = root
        self.path = root / "auction-cycle.json"
        self.settings: dict[str, Any] = (
            json.loads(self.path.read_text("utf-8")) if self.path.exists() else {"enabled": False}
        )
        self.state_path = root / "auction-cycle-state.json"
        self.state: dict[str, Any] = (
            json.loads(self.state_path.read_text("utf-8"))
            if self.state_path.exists()
            else {"phase": "watch"}
        )
        self.backend = backend
        self.clock = clock or time.time
        self.lock = threading.RLock()
        self.pulse = None
        self.changed = 0.0
        self.live = False
        self.message = "Автоцикл выключен"
        self.error_code = ""
        self.allow_idle = False

    def engine(self):
        if self.backend is None:
            self.backend = CycleWindows()
        return self.backend

    def configure(
        self,
        *,
        target,
        route,
        game_root,
        enabled,
        reload_key="F8",
        interact_key="F9",
        minutes=15,
        delay=5,
    ):
        with self.lock:
            if route.get("role") != "auctioneer":
                raise ValueError("Выберите персонажа с ролью «Аукционер»")
            if target not in self.engine().windows(game_root):
                raise ValueError("Выберите открытое окно WoW из папки игры")
            if reload_key not in KEYS or interact_key not in KEYS or reload_key == interact_key:
                raise ValueError("Выберите две разные клавиши F1–F12")
            if not 5 <= minutes <= 120 or not 3 <= delay <= 30:
                raise ValueError("Интервал: 5–120 минут; ожидание взаимодействия: 3–30 секунд")
            self.settings = {
                "enabled": bool(enabled),
                "target": target,
                "client_id": route["id"],
                "token": secrets.token_hex(6),
                "reload_key": reload_key,
                "interact_key": interact_key,
                "minutes": minutes,
                "delay": delay,
            }
            atomic_json(self.path, self.settings)
            self.reset()
            self.message = "Сохранено. /reload в окне аукционера загрузит индикатор автоцикла"

    def reset(self):
        with self.lock:
            self.state = {"phase": "watch", "token": self.settings.get("token")}
            self.pulse, self.live = None, False
            self.error_code = ""
            atomic_json(self.state_path, self.state)

    def disable(self):
        with self.lock:
            self.settings["enabled"] = False
            atomic_json(self.path, self.settings)
            self.reset()
            self.message = "Автоцикл выключен; обычный режим доступен. Аддон отпустит паузу в течение 15 секунд"

    def wire(self, route):
        with self.lock:
            if (
                not self.settings.get("enabled")
                or route["id"] != self.settings.get("client_id")
                or route["role"] != "auctioneer"
            ):
                return {"enabled": False}
            return {
                "enabled": True,
                "token": self.settings["token"],
                "client_id": route["id"],
                "flush_seconds": self.settings["minutes"] * 60,
            }

    def transition(self, phase, **values):
        self.state.update(phase=phase, **values)
        atomic_json(self.state_path, self.state)

    def block(self, code, message):
        self.error_code, self.message = code, message
        self.transition("blocked", error_code=code, message=message)

    def tick(self, *, paused, route, stamp, delivered):
        with self.lock:
            self.allow_idle = False
            if not self.settings.get("enabled"):
                return
            if paused:
                self.message = "Автоцикл: пауза Worker или нет подтверждённой связи с сервером"
                return
            if (
                not route
                or route.get("id") != self.settings.get("client_id")
                or route.get("role") != "auctioneer"
            ):
                self.message = "Автоцикл: выбранный аукционер не найден в подключении"
                return
            if self.state.get("phase") == "blocked":
                self.error_code = self.state.get("error_code", "CYCLE_BLOCKED")
                self.message = self.state.get(
                    "message",
                    "Автоцикл остановлен; проверьте состояние и нажмите «Продолжить цикл»",
                )
                return
            now = self.clock()
            phase = self.state["phase"]
            if phase == "reload" and now - self.state["sent_at"] > 120:
                self.block(
                    "RELOAD_TIMEOUT",
                    "Нет подтверждения /reload за 120 секунд. Проверьте назначение клавиши и продолжите цикл вручную",
                )
                return
            try:
                target = self.settings["target"]
                if not self.engine().ready(target):
                    self.message = (
                        "Автоцикл: окно активно, недоступно или удерживается Shift/Ctrl/Alt"
                    )
                    return
                signal = self.engine().signal(target, self.settings["token"])
                if not signal:
                    self.message = "Автоцикл: индикатор не виден. Загрузите настройки через /reload; оставьте окно видимым и не перекрывайте его верхний левый угол"
                    self.pulse, self.live = None, False
                    return
                pulse = (signal.boot, signal.heartbeat)
                if pulse != self.pulse:
                    self.live = self.pulse is not None
                    self.pulse, self.changed = pulse, now
                if not self.live or now - self.changed > 5:
                    self.message = "Автоцикл: ждём обновления живого индикатора"
                    return
                if signal.code == 0:
                    self.message = "Автоцикл остановлен в аддоне. Повторное включение: сохраните настройки Worker и /reload"
                    return
                if signal.code == 6:
                    self.block(
                        "ADDON_BLOCKED",
                        "Аддон заблокировал автоцикл: проверьте цель NPC, рынок, тип аукциона и неизвестную покупку. Подробности — Cennik → Статус",
                    )
                    return
                if phase == "reload":
                    if (
                        signal.boot == self.state["boot"]
                        or stamp is None
                        or list(stamp) == self.state["stamp"]
                    ):
                        self.message = "Автоцикл: ожидаем загрузку аддона и новое сохранение игры"
                        return
                    self.transition("reopen", after=now + self.settings["delay"], attempts=0)
                    phase = "reopen"
                if signal.code == 4:
                    if phase == "watch":
                        self.transition("reopen", after=now + self.settings["delay"], attempts=0)
                    if not delivered:
                        self.message = "Автоцикл: ждём подтверждения отправки сохранения на сервер"
                        return
                    if now < self.state["after"]:
                        self.message = "Автоцикл: ждём готовности NPC перед взаимодействием"
                        return
                    if self.state["attempts"] >= 3:
                        self.block(
                            "AUCTION_REOPEN_FAILED",
                            "Аукцион не открылся после трёх попыток. Проверьте цель, дистанцию и диалог NPC; затем продолжите цикл",
                        )
                        return
                    # Re-read the signal immediately before injecting input.
                    if self.engine().signal(target, self.settings["token"]) != signal:
                        return
                    self.transition("reopen", attempts=self.state["attempts"] + 1, after=now + 12)
                    self.engine().press(target, self.settings["interact_key"])
                    self.message = "Автоцикл: взаимодействие отправлено; ждём открытия аукциона"
                    return
                if phase == "reopen" and signal.code in {2, 3}:
                    self.transition("watch")
                    phase = "watch"
                    self.message = "Автоцикл: аукцион открыт; аддон восстанавливает сбор и снайпер"
                if phase == "watch" and signal.code == 3:
                    if not delivered:
                        self.message = (
                            "Автоцикл: сначала дождёмся подтверждения предыдущего сохранения"
                        )
                        return
                    if self.engine().signal(target, self.settings["token"]) != signal:
                        return
                    # Persist before posting, so a Worker restart never repeats an uncertain key.
                    self.transition(
                        "reload", sent_at=now, boot=signal.boot, stamp=list(stamp) if stamp else []
                    )
                    self.engine().press(target, self.settings["reload_key"])
                    self.message = "Автоцикл: клавиша /reload отправлена; ждём сохранения"
                elif signal.code in {1, 2}:
                    self.allow_idle = phase == "watch" and signal.code == 2
                    self.message = (
                        "Автоцикл: работа продолжается; ждём безопасного момента сохранения"
                    )
                elif signal.code == 5:
                    self.message = (
                        "Автоцикл: сначала вручную откройте подтверждённого NPC-аукциониста"
                    )
            except (OSError, ValueError, KeyError) as error:
                self.block("CYCLE_INPUT_FAILED", "Автоцикл остановлен: " + str(error))

    def report(self):
        return {
            "enabled": self.settings.get("enabled", False),
            "client_id": self.settings.get("client_id"),
            "phase": self.state.get("phase"),
            "error_code": self.error_code,
            "message": self.message,
        }
