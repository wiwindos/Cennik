"""Durable bidirectional exchange with the full server, separate from collectors."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import httpx
from pydantic import Field

from ahledger.util import atomic_json, atomic_write
from cennik.addon.lua_data import Parser, lua_literal, read_saved_variables
from cennik.collector.agent import CollectorAgent, addon_source, game_directory
from cennik.collector.protocol import Connection
from cennik.core.models import Model
from cennik.markets import MarketBinding
from cennik.workspace.install import install_addon

from .idle import IdleProbe

FIELDS = {"device_id", "binding", "events", "market_scans", "npc_quotes", "collector_status", "server_binding",
    "collector_npc", "collector_recipes", "collector_transforms", "auctioneer_proposals", "sniper", "bulk_checks", "accounting", "production"}


class Route(Model):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    role: str = Field(pattern=r"^(auctioneer|crafter)$")
    character: str = Field(min_length=1, max_length=80)
    realm: str = Field(min_length=1, max_length=100)
    account: str = Field(max_length=100, pattern=r"^[^/\\:]+$")


class Manifest(Model):
    format_version: int
    device_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    profile: MarketBinding
    clients: list[Route] = Field(default_factory=list, max_length=8)
    notes: list[str] = Field(default_factory=list)


class WorkerAgent(CollectorAgent):
    def __init__(self, root: Path, transport=None):
        super().__init__(root, transport)
        self.manifest = self.read("manifest.json", None)
        self.last_manifest = 0.0
        self.next_tick = 0.0
        self.last_success: int | None = None
        self.worker_error = ""
        self.pairing = self.read("pairing.json", None)
        self.discovery_hash = ""
        self.idle = IdleProbe(root)

    def request(self, method, path, *, body=None, connection=None):
        c = connection or self.client.connection()
        with httpx.Client(base_url=c.url, headers={"X-Bridge-Key": c.token, "X-Cennik-Worker":"0.2"}, transport=self.client.transport,
                          follow_redirects=False, trust_env=False, timeout=12) as http:
            response = http.request(method, path, json=body)
            response.raise_for_status()
            return response.json()

    def fetch_manifest(self, connection=None):
        value = self.request("GET", "/bridge/v1/manifest", connection=connection)
        profile = {k: value["profile"][k] for k in MarketBinding.model_fields}
        parsed = Manifest.model_validate({**value, "profile": profile})
        if parsed.format_version != 1 or len({r.id for r in parsed.clients}) != len(parsed.clients):
            raise ValueError("Неверный список персонажей от сервера")
        if any(r.account in {".", ".."} or any(c in r.character+r.realm for c in "\n\r:") for r in parsed.clients):
            raise ValueError("Неверные имена или аккаунты от сервера")
        return parsed.model_dump(mode="json")

    def owned_import(self, addon):
        path = addon / "ImportedData.lua"
        if not path.exists():
            return
        parser = Parser(path.read_text("utf-8-sig"), max_bytes=8_000_000)
        if parser.identifier() != "CennikImport":
            raise ValueError("Нераспознанный файл импорта; автоматическая замена запрещена")
        parser.require("=")
        data = parser.value()
        if data.get("worker_agent_id") == self.settings["id"]:
            return
        if data.get("remote_device_id") == (self.manifest or {}).get("device_id") and data.get("remote_device_id"):
            return
        if data.get("format_version") != 1 or data.get("packages") or data.get("market_collector") or data.get("collector_only"):
            raise ValueError("Аддон подключён к другому приложению. Для тестов используйте отдельную игру; перенос привязки описан в инструкции Worker")

    def configure(self, game_root, url, token="", *, source=None):
        with self.lock:
            game = game_directory(game_root)
            old = self.client.connection()
            key = token.strip() or (old.token if url.rstrip('/') == old.url else "")
            # Apply the same HTTPS and credential-in-URL restrictions as collectors.
            from urllib.parse import urlparse
            address = urlparse(url.strip())
            if address.path.rstrip('/') or address.query or address.fragment or address.username or address.password or not address.hostname:
                raise ValueError("Нужен адрес сервера без пути и параметров")
            if address.scheme != "https" and not (address.scheme == "http" and address.hostname in {"127.0.0.1", "localhost", "::1"}):
                raise ValueError("Для удалённого сервера нужен HTTPS")
            if not key:
                return self.begin_pair(game, url.strip(), source=source)
            connection = Connection(enabled=True, url=url.strip(), token=key)
            manifest = self.fetch_manifest(connection)
            previous = self.manifest
            self.manifest = manifest
            try:
                self.owned_import(game / "Interface/AddOns/CennikAddon")
                result = install_addon(str(game), self.root / "addon-backups", source=source or addon_source())
            except Exception:
                self.manifest = previous
                raise
            self.client.configure(connection)
            self.settings.update(game_root=str(game), paused=False, kind="worker")
            atomic_json(self.root / "agent.json", self.settings)
            atomic_json(self.root / "manifest.json", manifest)
            self.last_manifest = time.monotonic()
            self.errors.clear()
            self.worker_error = ""
            self.next_tick = 0
            self.wake.set()
            return result

    def pair_request(self, path, body, url):
        with httpx.Client(base_url=url, transport=self.client.transport, timeout=12, follow_redirects=False, trust_env=False) as http:
            response=http.post(path,json=body)
            response.raise_for_status()
            return response.json()

    def begin_pair(self, game, url, *, source=None):
        self.owned_import(game / "Interface/AddOns/CennikAddon")
        result=install_addon(str(game), self.root / "addon-backups", source=source or addon_source())
        paired=self.pair_request("/pair/v1/start", {"label":"Игровой ПК "+self.settings["id"][:6]},url)
        self.pairing={**paired,"url":url}
        atomic_json(self.root/"pairing.json",self.pairing)
        (self.root/"pairing.json").chmod(0o600)
        self.settings.update(game_root=str(game),paused=False,kind="worker")
        atomic_json(self.root/"agent.json",self.settings)
        self.write_import()
        self.next_tick=0
        self.wake.set()
        return {**result,"pair_code":paired["code"]}

    def poll_pair(self):
        result=self.pair_request("/pair/v1/poll", {"secret":self.pairing["secret"]}, self.pairing["url"])
        if result["state"]!="approved":
            return False
        connection=Connection(enabled=True,url=self.pairing["url"],token=result["token"])
        manifest=self.fetch_manifest(connection)
        self.client.configure(connection)
        self.manifest=manifest
        atomic_json(self.root/"manifest.json",manifest)
        self.pair_request("/pair/v1/ack", {"secret":self.pairing["secret"]},self.pairing["url"])
        (self.root/"pairing.json").unlink(missing_ok=True)
        self.pairing=None
        self.write_import()
        return True

    def pause(self, value):
        with self.lock:
            self.settings["paused"] = bool(value)
            atomic_json(self.root / "agent.json", self.settings)
            self.wake.set()

    def game_snapshots(self):
        found = {}
        characters={}
        root = Path(self.settings["game_root"])
        for path in sorted((root / "WTF/Account").glob("*/SavedVariables/CennikAddon.lua")):
            before = path.stat()
            if before.st_size > 64_000_000:
                raise ValueError("Сохранение больше 64 МБ")
            stamp = before.st_size, before.st_mtime_ns
            previous = self.quiet.get(str(path))
            if not previous or previous[0] != stamp:
                self.quiet[str(path)] = stamp, time.monotonic()
                continue
            if time.monotonic()-previous[1] < 1:
                continue
            data = read_saved_variables(path.read_text("utf-8-sig"), max_bytes=64_000_000)
            after = path.stat()
            if (after.st_size, after.st_mtime_ns) != stamp:
                continue
            binding = data.get("binding") or {}
            for profile in (data.get("character_profiles") or {}).values():
                if not isinstance(profile,dict) or profile.get("role") not in {"auctioneer","crafter","unassigned"}:
                    continue
                row={k:profile[k] for k in ("role","realm","character","faction","professions") if k in profile}
                row["account"]=path.parents[1].name
                characters[(row.get("realm"),row.get("character"))]=row
            if binding.get("market") != self.manifest["profile"]["market"]:
                continue
            route = next((r for r in self.manifest["clients"] if (r["realm"], r["character"]) == (binding.get("realm"), binding.get("character"))), None)
            if route:
                found[route["id"]] = ({k: v for k, v in data.items() if k in FIELDS}, stamp)
        if characters:
            values=sorted(characters.values(),key=lambda row:(row["role"]=="unassigned",row["realm"],row["character"]))[:30]
            digest=hashlib.sha256(json.dumps(values,sort_keys=True).encode()).hexdigest()
            if digest!=self.discovery_hash:
                self.request("POST","/bridge/v1/characters",body={"characters":values})
                self.manifest=self.fetch_manifest()
                atomic_json(self.root/"manifest.json",self.manifest)
                self.discovery_hash=digest
        return found

    def write_import(self):
        if not self.settings.get("game_root"):
            return
        if not self.manifest:
            addon=Path(self.settings["game_root"])/"Interface/AddOns/CennikAddon"
            self.owned_import(addon)
            atomic_write(addon/"ImportedData.lua", ("CennikImport="+lua_literal({"format_version":1,"packages":[],
                "worker_agent_id":self.settings["id"],"worker_discovery":True})+"\n").encode())
            return
        routes = {}
        for route in self.manifest["clients"]:
            path = self.root / "imports" / (route["id"]+".json")
            if not path.exists():
                continue
            payload = json.loads(path.read_text("utf-8"))
            if payload.get("client_id") != route["id"] or payload.get("market_collector", {}).get("market") != self.manifest["profile"]["market"]:
                raise ValueError("Сервер прислал импорт другого персонажа или рынка")
            routes[route["realm"]+":"+route["character"]] = payload
        addon = Path(self.settings["game_root"]) / "Interface/AddOns/CennikAddon"
        self.owned_import(addon)
        fallback={"format_version":1,"packages":[],"worker_discovery":True,"market_binding":self.manifest["profile"],
            "market_collector":{"workspace_id":"discovery-"+self.manifest["device_id"],"market":self.manifest["profile"]["market"],"auto_scan":False}}
        payload = {"format_version": 2, "remote_device_id": self.manifest["device_id"], "worker_agent_id":self.settings["id"], "routes": routes,"discovery":fallback}
        body = ("-- Cennik Worker: private addressed server import.\nCennikImport="+lua_literal(payload)+"\n").encode()
        if len(body) > 8_000_000:
            raise ValueError("Импорт больше 8 МБ; уменьшите число заданий")
        path = addon / "ImportedData.lua"
        if not path.exists() or path.read_bytes() != body:
            atomic_write(path, body)

    def tick(self, *, force=False):
        with self.lock:
            if not self.settings.get("game_root") or self.settings["paused"]:
                return
            self.idle.tick()
            if not force and time.monotonic() < self.next_tick and self.client.next_attempt != 0:
                return
            self.checked_at = int(time.time())
            self.client.next_attempt = time.monotonic()+10
            try:
                if self.pairing and not self.poll_pair():
                    self.next_tick=time.monotonic()+5
                    return
                if not self.manifest or time.monotonic()-self.last_manifest > 60:
                    self.manifest = self.fetch_manifest()
                    atomic_json(self.root / "manifest.json", self.manifest)
                    self.last_manifest = time.monotonic()
                snapshots = self.game_snapshots()
                for route in self.manifest["clients"]:
                    request_path = self.root / "requests" / (route["id"]+".json")
                    previous = self.read("state-"+route["id"]+".json", {"sequence": 0, "stamp": None})
                    if request_path.exists():
                        pending = json.loads(request_path.read_text("utf-8"))
                    else:
                        saved, stamp = snapshots.get(route["id"], (None, None))
                        pending = {"client_id": route["id"], "sequence": previous["sequence"]+1,
                            "saved": saved if stamp and list(stamp) != previous["stamp"] else None, "stamp": stamp}
                        atomic_json(request_path, pending)
                    body = {k: pending[k] for k in ("client_id", "sequence", "saved")}
                    try:
                        state = self.request("POST", "/bridge/v1/exchange", body=body)
                    except httpx.HTTPStatusError as error:
                        if error.response.status_code == 409:
                            detail = error.response.json().get("detail")
                            if isinstance(detail, dict) and detail.get("code") == "STALE_SEQUENCE":
                                pending["sequence"] = detail["next_sequence"]
                                atomic_json(request_path, pending)
                        raise
                    deadline = time.monotonic()+3
                    while state["state"] == "queued" and time.monotonic() < deadline and not self.stop.wait(.15):
                        state = self.request("GET", "/bridge/v1/exchanges/"+route["id"])
                    if state["state"] == "completed":
                        response = state["response"]
                        if response["sequence"] != pending["sequence"] or response["client_id"] != route["id"]:
                            raise ValueError("Неверное подтверждение обмена")
                        atomic_json(self.root / "imports" / (route["id"]+".json"), response["payload"])
                        atomic_json(self.root / ("state-"+route["id"]+".json"), {"sequence": pending["sequence"],
                            "stamp": pending["stamp"] if pending["saved"] is not None else previous["stamp"]})
                        request_path.unlink()
                        self.last_success = int(time.time())
                    elif state["state"] == "failed":
                        pending["sequence"] += 1
                        atomic_json(request_path, pending)
                        raise ValueError("Сервер не завершил обмен. Сохранение осталось в очереди")
                self.write_import()
                self.worker_error = ""
                self.errors.clear()
                self.next_tick = time.monotonic()+8
            except httpx.HTTPStatusError as error:
                self.worker_error = "Ключ рабочего клиента неверен или отозван" if error.response.status_code in {401, 403} else "Сервер отклонил обмен. Данные сохранены; скопируйте отчёт поддержки"
                self.next_tick = time.monotonic()+30
            except httpx.HTTPError:
                self.worker_error = "Нет связи с сервером. Очередь сохранена; повторим позже"
                self.next_tick = time.monotonic()+30
            except (ValueError, OSError, KeyError, TypeError):
                self.worker_error = "Не удалось обработать обмен. Очередь сохранена; проверьте версию аддона и сервера"
                self.next_tick = time.monotonic()+30

    def overview(self):
        pending = len(list((self.root / "requests").glob("*.json")))
        status = "Нужна настройка" if not self.settings.get("game_root") else "Приостановлено" if self.settings["paused"] else "Ошибка" if self.worker_error or self.errors else "Подтвердите код в вебе: "+self.pairing["code"] if self.pairing else "Работает"
        note = " · ".join((self.manifest or {}).get("notes",[]))
        if self.manifest and not self.manifest["clients"] and not self.pairing:
            note=note or "Войдите в персонажей, выберите роль через /cennik role, затем /reload"
        if self.idle.settings.get("enabled"):
            note=(note+" · " if note else "")+self.idle.message
        return {"status": status, "error": self.worker_error or next(iter(self.errors.copy().values()), ""),
            "paused": self.settings["paused"], "pending": pending, "rejected": 0, "checked_at": self.checked_at,
            "last_import": self.last_success, "delivery": {"at": self.last_success}, "local": [],
            "connection": self.client.public_connection(), "game_root": self.settings.get("game_root", ""), "notice":note,
            "game_code": "", "game_status": "", "game_at": None}

    def report(self):
        return json.dumps({"application": "Cennik Worker", "version": "0.2.0", "id": self.settings["id"],
            "state": self.overview()["status"], "pending": self.overview()["pending"], "last_success": self.last_success,
            "error_code": "PRIVATE_EXCHANGE_FAILED" if self.worker_error or self.errors else ""}, ensure_ascii=False, indent=2)
