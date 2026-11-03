"""Collector-only file watcher: no analytics, SQL, trading or owner credentials."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

from ahledger.util import atomic_json, atomic_write
from cennik.addon.limits import MAX_SAVED_BYTES, MAX_SAVED_TOKENS
from cennik.addon.lua_data import Parser, lua_literal, read_saved_variables
from cennik.workspace.install import install_addon

from .client import CollectorClient
from .protocol import Connection, Observation
from .status import GAME_STATUS

VERSION = "0.4.4"
MARKET = "forever.pvp.alliance.us"


def default_root():
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CennikCollector"


def addon_source():
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))
    return base / "CennikAddon"


def game_directory(path):
    root = Path(path).expanduser().resolve(strict=True)
    for candidate in (root, *(root / name for name in ("_classic_", "_classic_era_", "_retail_"))):
        if (candidate / "Interface/AddOns").is_dir():
            return candidate
    raise ValueError("В выбранной папке не найдена Interface/AddOns. Выберите папку установленной игры")


def discover_games():
    candidates = []
    if os.name == "nt":
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for key in (r"SOFTWARE\Blizzard Entertainment\World of Warcraft", r"SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft"):
                try:
                    with winreg.OpenKey(hive, key) as handle:
                        candidates.append(Path(winreg.QueryValueEx(handle, "InstallPath")[0]))
                except OSError:
                    pass
    for prefix in (os.environ.get("ProgramFiles", "C:/Program Files"), os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"), "C:/Games", "D:/Games"):
        candidates.extend(Path(prefix) / name for name in ("World of Warcraft", "WoW", "WoW Forever"))
    found = set()
    for path in candidates:
        try:
            found.add(str(game_directory(path)))
        except (OSError, ValueError):
            pass
    return sorted(found)


class CollectorAgent:
    def __init__(self, root: Path, transport=None):
        self.root = root.resolve()
        self.client = CollectorClient(self.root, transport)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread: threading.Thread | None = None
        self.errors: dict[str, str] = {}
        self.checked_at: int | None = None
        self.last_import: int | None = None
        self.settings = self.read("agent.json", {"game_root": "", "paused": False, "id": uuid4().hex})
        self.files = self.read("files.json", {})
        self.seen: dict[str, tuple[int, int]] = {}
        self.quiet: dict[str, tuple[tuple[int, int], float]] = {}
        # The standalone agent never needs or retains an administrative key.
        if self.client.connection().admin_token:
            raise ValueError("Эта папка принадлежит полной версии Cennik. Выберите отдельный профиль сборщика")

    def read(self, name, default):
        p = self.root / name
        return json.loads(p.read_text("utf-8")) if p.exists() else default

    def owned_import(self, addon):
        path = addon / "ImportedData.lua"
        if not path.exists():
            return
        parser = Parser(path.read_text("utf-8-sig"), max_bytes=8_000_000)
        if parser.identifier() != "CennikImport":
            raise ValueError("Нераспознанный ImportedData.lua; автоматическая замена запрещена")
        parser.require("=")
        data = parser.value()
        parser.whitespace()
        if parser.pos != len(parser.text) or not isinstance(data, dict):
            raise ValueError("Нераспознанный ImportedData.lua; автоматическая замена запрещена")
        if data.get("collector_only") and data.get("collector_agent_id") == self.settings["id"]:
            return
        if data.get("format_version") != 1 or data.get("packages") or data.get("market_collector") or data.get("collector_only") or data.get("client_id"):
            raise ValueError("Этот аддон подключён к другому Cennik. Не подключайте сборщик к рабочему клиенту владельца")

    def configure(self, game_root, url, token="", *, source=None):
        with self.lock:
            game = game_directory(game_root)
            addon = game / "Interface/AddOns/CennikAddon"
            self.owned_import(addon)
            if not token and not self.client.connection().token:
                raise ValueError("Введите личный ключ, выданный владельцем сервера")
            if not token and url.strip().rstrip('/') != self.client.connection().url:
                raise ValueError("Для другого сервера нужен новый личный ключ")
            # Validate connection before changing game files.
            probe = Connection(enabled=True, url=url.strip(), token=token.strip())
            from urllib.parse import urlparse
            address = urlparse(probe.url)
            if address.scheme != "https" and not (address.scheme == "http" and address.hostname in {"localhost", "127.0.0.1", "::1"}):
                raise ValueError("Для удалённого сервера нужен HTTPS")
            if address.username or address.password or address.query or address.fragment or address.path.rstrip('/') not in {"", "/collect"} or not address.hostname:
                raise ValueError("Нужен адрес сервера без пути и параметров")
            result = install_addon(str(game), self.root / "addon-backups", source=source or addon_source())
            self.client.configure(probe)
            self.settings.update(game_root=str(game), paused=False)
            atomic_json(self.root / "agent.json", self.settings)
            self.errors.clear()
            self.write_import()
            self.wake.set()
            return result

    def pause(self, value):
        with self.lock:
            self.settings["paused"] = bool(value)
            atomic_json(self.root / "agent.json", self.settings)
            if self.settings.get("game_root"):
                self.write_import()
            self.wake.set()

    def ingest(self, path):
        before = path.stat()
        if before.st_size > MAX_SAVED_BYTES:
            raise ValueError("Сохранение больше 256 МБ. Очередь оставлена в игре; передайте отчёт поддержки")
        text = path.read_text("utf-8-sig")
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("Игра ещё записывает файл; повторим чтение")
        data = read_saved_variables(text, max_bytes=MAX_SAVED_BYTES, max_tokens=MAX_SAVED_TOKENS)
        if not self.client.capture(data, "Сборщик"):
            raise ValueError("Неверный формат сохранённых наблюдений. Они оставлены в игре; скопируйте отчёт поддержки")
        if not self.client.failures:
            self.client.next_attempt = 0
        ack = []
        for scan in data.get("market_scans") or []:
            observation = Observation(kind="scan", data=self.client.scan_data(scan))
            key = observation.identity()
            if any((self.client.root / folder / (key+".json")).exists() for folder in ("outbox", "receipts")):
                if isinstance(scan.get("scan_id"), str):
                    ack.append(scan["scan_id"])
        identity = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
        self.files[identity] = {"scan_ids": ack, "at": int(time.time())}
        atomic_json(self.root / "files.json", self.files)
        self.seen[str(path)] = (after.st_size, after.st_mtime_ns)
        self.last_import = int(time.time())

    def write_import(self):
        if not self.settings.get("game_root"):
            return
        addon = Path(self.settings["game_root"]) / "Interface/AddOns/CennikAddon"
        self.owned_import(addon)
        payload = {"format_version": 1, "collector_only": True, "collector_agent_id": self.settings["id"],
            "packages": [], "market_collector": {"workspace_id": "collector-"+self.settings["id"],
                "market": MARKET, "auto_scan": not self.settings["paused"]},
            "acknowledged_scan_ids": sorted({s for f in self.files.values() for s in f["scan_ids"]}),
            "collector_delivery": {**self.client.delivery(), "enabled": not self.settings["paused"]},
            "auctioneer_registry": self.client.registry()}
        if "market_catalog" in self.client.registry():
            payload["market_catalog"] = self.client.registry()["market_catalog"]
        body = ("-- Cennik Collector; data only, loaded by the game on /reload.\nCennikImport="+lua_literal(payload)+"\n").encode()
        if len(body) > 8_000_000:
            raise ValueError("Подтверждения больше 8 МБ; передайте отчёт поддержки")
        path = addon / "ImportedData.lua"
        if not path.exists() or path.read_bytes() != body:
            atomic_write(path, body)

    def tick(self, *, force=False):
        with self.lock:
            self.checked_at = int(time.time())
            if not self.settings.get("game_root"):
                return
            self.errors.pop("files", None)
            game = Path(self.settings["game_root"])
            if not (game / "Interface/AddOns/CennikAddon/CennikAddon.toc").exists():
                self.errors["files"] = "Аддон не найден. Проверьте папку игры и нажмите «Сохранить и установить аддон»"
                return
            if not self.settings["paused"]:
                for path in sorted((game / "WTF/Account").glob("*/SavedVariables/CennikAddon.lua")):
                    try:
                        stat = path.stat()
                        stamp = stat.st_size, stat.st_mtime_ns
                        if self.seen.get(str(path)) == stamp:
                            continue
                        previous = self.quiet.get(str(path))
                        if not force and (not previous or previous[0] != stamp):
                            self.quiet[str(path)] = (stamp, time.monotonic())
                            continue
                        if not force and previous is not None and time.monotonic()-previous[1] < 1:
                            continue
                        self.ingest(path)
                    except (OSError, ValueError, TypeError, KeyError) as error:
                        self.errors["files"] = str(error)
                if force or self.client.due():
                    self.client.flush()
            try:
                self.write_import()
            except (OSError, ValueError) as error:
                self.errors["files"] = str(error)

    def overview(self):
        # Reads atomic files without waiting for the worker's network lock.
        d = self.client.overview()
        error = next(iter(self.errors.copy().values()), "") or (d["delivery"]["message"] if d["delivery"]["error_code"] else "")
        if d["rejected"]:
            error = error or "Сервер отклонил часть наблюдений. Данные сохранены; скопируйте отчёт поддержки"
        status = "Нужна настройка" if not self.settings.get("game_root") else "Приостановлено" if self.settings["paused"] else "Ошибка" if error else "Работает"
        latest = max(d["local"], key=lambda v: v["diagnostic"].get("at", 0), default=None)
        diagnostic = latest["diagnostic"] if latest else {}
        game_code = diagnostic.get("code", "")
        return {"status": status, "error": error, "paused": self.settings["paused"], "pending": d["pending"],
            "rejected": d["rejected"], "last_import": self.last_import, "checked_at": self.checked_at,
            "delivery": d["delivery"], "local": d["local"], "connection": d["connection"],
            "game_root": self.settings.get("game_root", ""), "game_code": game_code,
            "game_status": GAME_STATUS.get(game_code, game_code), "game_at": diagnostic.get("at")}

    def report(self):
        d = self.overview()
        # No credentials, character names, game directories or raw game saves.
        return json.dumps({"application": "Cennik Collector", "version": VERSION, "id": self.settings["id"],
            **{k: d[k] for k in ("status", "paused", "pending", "rejected", "last_import", "checked_at")},
            "delivery_code": d["delivery"]["error_code"],
            "file_error": "FILE_IMPORT_FAILED" if self.errors else "",
            "diagnostics": [v["diagnostic"] for v in d["local"]]}, ensure_ascii=False, indent=2)

    def start(self):
        def run():
            while not self.stop.is_set():
                try:
                    self.tick()
                    self.errors.pop("worker", None)
                except Exception:
                    self.errors["worker"] = "Не удалось обработать очередь; данные сохранены. Скопируйте отчёт поддержки"
                self.wake.wait(2)
                self.wake.clear()
        self.thread = threading.Thread(target=run, name="collector-worker", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=15)
