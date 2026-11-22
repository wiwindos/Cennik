"""Opt-in, addressed reload/reopen cycle driven by a live addon signal."""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from ahledger.util import atomic_json

from .idle import WindowsGame, selected_window

KEYS = tuple(f"F{i}" for i in range(1, 13))
MAGIC = (219, 51, 177)
END = (41, 211, 89)
VERSION_COLOR = (16, 32, 48)
ANY_CHARACTER_COLOR = (16, 32, 80)


def character_key(realm, character):
    result = 5381
    for byte in (realm+':'+character).encode('utf-8'):
        result = (result*33+byte) % 4294967296
    return result


@dataclass(frozen=True)
class Signal:
    code: int
    heartbeat: int
    boot: int
    character: int | None = None


def decode_signal(image, token):
    """Read seven cells across client/DPI scales; validate token, framing and inverse."""
    if len(token) != 12:
        return None
    colors = [tuple(bytes.fromhex(token[i : i + 6])) for i in (0, 6)]

    def near(a, b, tolerance=5):
        return all(abs(x - y) <= tolerance for x, y in zip(a[:3], b, strict=True))

    pixels = image.convert("RGB")
    strides = (10, *range(4, 10), *range(11, 25))
    for y in range(0, min(pixels.height, 220), 2):
        for x in range(0, min(pixels.width - 24, 360), 2):
            if not near(pixels.getpixel((x, y)), MAGIC, 12):
                continue
            for stride in strides:
                if x + 6*stride >= pixels.width:
                    continue
                row = [pixels.getpixel((x + i * stride, y)) for i in range(7)]
                if not (
                    near(row[1], colors[0])
                    and near(row[2], colors[1])
                    and near(row[5], END)
                    and (near(row[6], VERSION_COLOR) or near(row[6], ANY_CHARACTER_COLOR))
                ):
                    continue
                r, g, b = row[3]
                code, heartbeat, boot = round((r - 32) / 24), round((g - 16) / 14), round((b - 16) / 14)
                if not (0 <= code <= 7 and 0 <= heartbeat <= 15 and 0 <= boot <= 15):
                    continue
                canonical = (32 + code * 24, 16 + heartbeat * 14, 16 + boot * 14)
                if near(row[3], canonical) and near(row[4], tuple(255 - v for v in canonical)):
                    identity = None
                    if near(row[6], ANY_CHARACTER_COLOR):
                        if x+9*stride >= pixels.width:
                            continue
                        raw = [v for i in range(7,10) for v in pixels.getpixel((x+i*stride,y))[:3]]
                        digits = [round((v-16)/14) for v in raw]
                        if any(not 0<=v<=15 or abs(raw[i]-(16+14*v))>5 for i,v in enumerate(digits)) or sum(digits[:8])%16!=digits[8]:
                            continue
                        identity = 0
                        for digit in digits[:8]:
                            identity = identity*16+digit
                    return Signal(code, heartbeat, boot, identity), (x + round(3.5*stride), y)
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
        self.events_path = root / 'auction-cycle-events.json'
        self.observation = None
        self.last_signal = None
        self.journal_error = ''

    def record(self, event, **details):
        try:
            events = json.loads(self.events_path.read_text('utf-8')) if self.events_path.exists() else []
            events = events if isinstance(events, list) else []
            events.append({'at': self.clock(), 'event': event, **details})
            atomic_json(self.events_path, events[-200:])
            self.journal_error = ''
        except (OSError, ValueError):
            self.journal_error = 'Не удалось записать журнал автоцикла'

    def observe(self, key, **details):
        if key != self.observation:
            self.observation = key
            self.record('observation', state=key, **details)

    def confirmed_signal(self, target, signal):
        current = self.engine().signal(target, self.settings['token'])
        # A fresh heartbeat may tick between captures; only state and boot must match.
        return current is not None and (current.code, current.boot, current.character) == (signal.code, signal.boot, signal.character)

    def engine(self):
        if self.backend is None:
            self.backend = CycleWindows()
        return self.backend

    def configure(
        self,
        *,
        target,
        route=None,
        game_root,
        enabled,
        reload_key="F8",
        interact_key="F9",
        minutes=15,
        delay=5,
        any_character=False,
        auto_sniper=False,
    ):
        with self.lock:
            if not any_character and (not route or route.get("role") != "auctioneer"):
                raise ValueError("Выберите персонажа с ролью «Аукционер»")
            target = selected_window(target, self.engine().windows(game_root))
            if target is None:
                raise ValueError("Выберите открытое окно WoW из папки игры")
            if reload_key not in KEYS or interact_key not in KEYS or reload_key == interact_key:
                raise ValueError("Выберите две разные клавиши F1–F12")
            if not 5 <= minutes <= 120 or not 3 <= delay <= 30:
                raise ValueError("Интервал: 5–120 минут; ожидание взаимодействия: 3–30 секунд")
            self.settings = {
                "enabled": bool(enabled),
                "target": target,
                "client_id": route['id'] if route else None,
                "any_character": bool(any_character),
                "auto_sniper": bool(auto_sniper),
                "start_revision": int(time.time()*1000),
                "game_root": game_root,
                "token": self.settings.get('token') if any_character and self.settings.get('any_character') else secrets.token_hex(6),
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
            self.last_signal = None
            self.observation = None
            self.error_code = ""
            atomic_json(self.state_path, self.state)
            self.record('reset')

    def disable(self):
        with self.lock:
            self.settings["enabled"] = False
            atomic_json(self.path, self.settings)
            self.reset()
            self.message = "Автоцикл выключен; обычный режим доступен. Аддон отпустит паузу в течение 15 секунд"

    def wire(self, route):
        with self.lock:
            if self.settings.get('enabled') and self.settings.get('any_character'):
                return {'enabled':True,'token':self.settings['token'],'client_id':route['id'] if route else None,
                    'any_character':True,'auto_sniper':self.settings.get('auto_sniper',False),
                    'start_revision':self.settings.get('start_revision',0),
                    'flush_seconds':self.settings['minutes']*60,'auto_scan':True}
            if (
                not self.settings.get("enabled")
                or not route
                or route["id"] != self.settings.get("client_id")
                or route["role"] != "auctioneer"
            ):
                return {"enabled": False}
            return {
                "enabled": True,
                "token": self.settings["token"],
                "client_id": route["id"],
                "flush_seconds": self.settings["minutes"] * 60,
                "auto_scan": True,
            }

    def transition(self, phase, **values):
        self.state.update(phase=phase, **values)
        atomic_json(self.state_path, self.state)
        self.record('transition', phase=phase, **{k: v for k, v in values.items() if k != 'token'})

    def block(self, code, message):
        self.error_code, self.message = code, message
        self.transition("blocked", error_code=code, message=message)

    def tick(self, *, paused, route, stamp, delivered, observed_signal=None):
        with self.lock:
            self.allow_idle = False
            if not self.settings.get("enabled"):
                return
            if paused:
                self.message = "Автоцикл: пауза Worker или нет подтверждённой связи с сервером"
                return
            dynamic = self.settings.get('any_character',False)
            if not dynamic and (
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
            try:
                target = self.settings["target"]
                if not self.engine().ready(target):
                    self.observe('window_unavailable')
                    self.message = (
                        "Автоцикл: окно активно, недоступно или удерживается Shift/Ctrl/Alt"
                    )
                    return
                signal = observed_signal or self.engine().signal(target, self.settings["token"])
                if not signal:
                    self.observe('indicator_missing')
                    self.message = "Автоцикл: индикатор не виден. Загрузите настройки через /reload; оставьте окно видимым и не перекрывайте его верхний левый угол"
                    self.pulse, self.live = None, False
                    return
                self.last_signal = {'code': signal.code, 'boot': signal.boot, 'heartbeat': signal.heartbeat, 'at': now,
                    'character': route.get('character') if route else None, 'realm':route.get('realm') if route else None}
                if dynamic:
                    if signal.character is None:
                        self.message='Обновите аддон и выполните /reload: нужен индикатор персонажа'
                        return
                    previous_character=self.state.get('character')
                    if previous_character!=signal.character:
                        self.transition('watch',character=signal.character,sync_remaining=2)
                        phase='watch'
                        self.pulse,self.live=None,False
                    self.state['character']=signal.character
                self.observe(f'signal:{signal.boot}:{signal.code}', code=signal.code, boot=signal.boot)
                if phase == 'reload' and now-self.state['sent_at'] > 120 and (
                    signal.boot == self.state['boot'] or stamp is None or list(stamp) == self.state['stamp']
                ):
                    self.block('RELOAD_TIMEOUT',
                               'Нет подтверждения /reload за 120 секунд. Проверьте назначение клавиши и продолжите цикл вручную')
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
                if dynamic and (signal.code == 7 or (self.state.get('sync_remaining',0)>0 and signal.code in {3,4})):
                    if not self.confirmed_signal(target, signal):
                        return
                    if phase != 'reload':
                        if route and not delivered:
                            self.message='Регистрация персонажа: ждём передачи сохранения и правил с сервера'
                            return
                        self.transition('reload',sent_at=now,boot=signal.boot,stamp=list(stamp) if stamp else [],
                            sync_remaining=max(0,self.state.get('sync_remaining',0)-1))
                        self.engine().press(target,self.settings['reload_key'])
                        self.record('key_sent',key=self.settings['reload_key'],purpose='character_registration')
                        self.message='Регистрируем текущего персонажа: отправлена клавиша /reload'
                    return
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
                    if not self.confirmed_signal(target, signal):
                        return
                    self.transition("reopen", attempts=self.state["attempts"] + 1, after=now + 12)
                    self.engine().press(target, self.settings["interact_key"])
                    self.record('key_sent', key=self.settings['interact_key'], purpose='interact')
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
                    if not self.confirmed_signal(target, signal):
                        return
                    # Persist before posting, so a Worker restart never repeats an uncertain key.
                    self.transition(
                        "reload", sent_at=now, boot=signal.boot, stamp=list(stamp) if stamp else []
                    )
                    self.engine().press(target, self.settings["reload_key"])
                    self.record('key_sent', key=self.settings['reload_key'], purpose='reload')
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
            "last_signal": self.last_signal,
            "journal_error": self.journal_error,
        }
