"""Durable bidirectional exchange with the full server, separate from collectors."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import httpx
from pydantic import Field

from ahledger.util import atomic_json, atomic_write
from cennik.addon.limits import MAX_EXCHANGE_BYTES, MAX_SAVED_BYTES, MAX_SAVED_TOKENS
from cennik.addon.lua_data import Parser, lua_literal, read_saved_variables
from cennik.collector.agent import CollectorAgent, addon_source, game_directory
from cennik.collector.protocol import Connection
from cennik.core.models import Model
from cennik.markets import MarketBinding
from cennik.workspace.install import install_addon

from .automation import AuctionCycle
from .idle import IdleProbe

FIELDS = {"device_id", "binding", "events", "market_scans", "npc_quotes", "collector_status", "server_binding",
    "collector_npc", "collector_recipes", "collector_transforms", "auctioneer_proposals", "sniper", "bulk_checks", "accounting", "production"}

VERSION = "0.4.1"


def saved_batch(saved, stamp, previous):
    """One snapshot per durable exchange; mark the file consumed only at the end."""
    delivered = set(previous.get("scan_ids", [])) if previous.get("scan_stamp") == list(stamp) else set()
    remaining = [s for s in (saved.get("market_scans") or []) if s.get("scan_id") not in delivered]
    batch = remaining[:1]
    return {"saved": {**saved, "market_scans": batch}, "stamp": stamp, "scan_total": len(saved.get("market_scans") or []),
            "scan_ids": [s["scan_id"] for s in batch], "snapshot_final": len(remaining) <= 1}


def http_diagnostic(error):
    """Keep useful protocol details, never response inputs, headers or credentials."""
    path = error.request.url.path
    operation = {
        "/bridge/v1/characters": "character_discovery",
        "/bridge/v1/manifest": "manifest",
        "/bridge/v1/exchange": "exchange",
        "/pair/v1/poll": "pairing",
        "/pair/v1/ack": "pairing",
    }.get(path, "exchange")
    status = error.response.status_code
    diagnostic: dict = {"operation": operation, "http_status": status, "validation_fields": []}
    if status == 422:
        try:
            detail = error.response.json().get("detail", [])
        except (ValueError, AttributeError):
            detail = []
        allowed = {"body", "characters", "role", "character", "realm", "account", "faction",
                   "professions", "client_id", "sequence", "saved", "secret"}
        for item in detail[:10] if isinstance(detail, list) else []:
            loc = item.get("loc") if isinstance(item, dict) else None
            if isinstance(loc, list) and loc and all(
                (isinstance(v, str) and v in allowed) or (type(v) is int and 0 <= v < 1000)
                for v in loc
            ):
                diagnostic["validation_fields"].append(".".join(map(str, loc)))
    return diagnostic


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
        self.worker_error_code = ""
        self.error_details: dict = {}
        self.pairing = self.read("pairing.json", None)
        self.discovery_hash = ""
        self.snapshot_sources: dict[str, str] = {}
        self.scan_progress = ""
        self.idle = IdleProbe(root)
        self.cycle = AuctionCycle(root)

    def request(self, method, path, *, body=None, connection=None):
        c = connection or self.client.connection()
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode() if body is not None else None
        if payload is not None and len(payload) > MAX_EXCHANGE_BYTES:
            raise ValueError("Пакет обмена превышает 60 МБ; исходные данные оставлены на ПК")
        with httpx.Client(base_url=c.url, headers={"X-Bridge-Key": c.token, "X-Cennik-Worker":"0.2"}, transport=self.client.transport,
                          follow_redirects=False, trust_env=False, timeout=12) as http:
            response = http.request(method, path, content=payload,
                                    headers={"Content-Type": "application/json"} if payload is not None else None)
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
            url = url.strip().rstrip('/')
            supplied_key = token.strip()
            key = supplied_key or (old.token if url == old.url else "")
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
            try:
                manifest = self.fetch_manifest(connection)
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 401:
                    raise
                if not supplied_key:
                    # Only a deliberate Save may restart pairing. Background retries
                    # must never replace a revoked key or create invitations.
                    return self.begin_pair(game, url, source=source)
                raise ValueError("Указанный ключ Worker неверен или отозван. Очистите поле ключа и сохраните настройки, чтобы получить новый код подключения") from None
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
            self.worker_error_code = ""
            self.error_details = {}
            self.seen.clear()
            self.snapshot_sources.clear()
            self.discovery_hash = ""
            self.scan_progress = ""
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
        # The new owner-approved connection must not reuse the previous routes.
        # Keep queued saves and imports on disk for recovery, but stop using them.
        self.manifest = None
        (self.root/"manifest.json").unlink(missing_ok=True)
        atomic_json(self.client.root/"connection.json", Connection(enabled=False,url=url).model_dump(mode="json"))
        self.last_manifest = 0.0
        self.discovery_hash = ""
        self.seen.clear()
        self.snapshot_sources.clear()
        self.scan_progress = ""
        self.last_success = None
        self.worker_error = ""
        self.worker_error_code = ""
        self.error_details = {}
        self.errors.clear()
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
            if before.st_size > MAX_SAVED_BYTES:
                raise ValueError("Сохранение больше 256 МБ; данные не удалены")
            stamp = before.st_size, before.st_mtime_ns
            if self.seen.get(str(path)) == stamp:
                continue
            previous = self.quiet.get(str(path))
            if not previous or previous[0] != stamp:
                self.quiet[str(path)] = stamp, time.monotonic()
                continue
            if time.monotonic()-previous[1] < 1:
                continue
            data = read_saved_variables(path.read_text("utf-8-sig"), max_bytes=MAX_SAVED_BYTES, max_tokens=MAX_SAVED_TOKENS)
            after = path.stat()
            if (after.st_size, after.st_mtime_ns) != stamp:
                continue
            binding = data.get("binding") or {}
            for profile in (data.get("character_profiles") or {}).values():
                if not isinstance(profile,dict) or profile.get("role") not in {"auctioneer","crafter","unassigned"}:
                    continue
                row={k:profile[k] for k in ("role","realm","character","faction","professions") if k in profile}
                if isinstance(row.get("professions"), dict) and not row["professions"]:
                    row["professions"] = []
                row["account"]=path.parents[1].name
                characters[(row.get("realm"),row.get("character"))]=row
            if binding.get("market") != self.manifest["profile"]["market"]:
                continue
            route = next((r for r in self.manifest["clients"] if (r["realm"], r["character"]) == (binding.get("realm"), binding.get("character"))), None)
            if route:
                previous = self.read("state-"+route["id"]+".json", {})
                if previous.get("stamp") == list(stamp):
                    self.seen[str(path)] = stamp
                else:
                    found[route["id"]] = ({k: v for k, v in data.items() if k in FIELDS}, stamp)
                    self.snapshot_sources[route["id"]] = str(path)
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
            payload["automation"] = self.cycle.wire(route)
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
            self.cycle_tick()
            self.idle.tick(paused=self.settings["paused"] or (self.cycle.settings.get("enabled", False) and not self.cycle.allow_idle))
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
                                   "saved": None, "stamp": stamp}
                        if saved is not None and stamp and list(stamp) != previous["stamp"]:
                            pending.update(saved_batch(saved, stamp, previous))
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
                        payload = response["payload"]
                        scan_ids = pending.get("scan_ids", [])
                        if not set(scan_ids) <= set(payload.get("acknowledged_scan_ids", [])):
                            raise ValueError("Сервер не подтвердил сохранение снимка; пакет оставлен на ПК")
                        import_path = self.root / "imports" / (route["id"]+".json")
                        old_import = self.read("imports/"+route["id"]+".json", {})
                        payload["acknowledged_scan_ids"] = list(dict.fromkeys(
                            old_import.get("acknowledged_scan_ids", []) + payload.get("acknowledged_scan_ids", [])))[-1000:]
                        atomic_json(import_path, payload)
                        scan_stamp = pending["stamp"] if pending["saved"] is not None else previous.get("scan_stamp")
                        delivered = previous.get("scan_ids", []) if previous.get("scan_stamp") == scan_stamp or previous.get("scan_stamp") == list(scan_stamp or []) else []
                        atomic_json(self.root / ("state-"+route["id"]+".json"), {"sequence": pending["sequence"],
                            "stamp": pending["stamp"] if pending["saved"] is not None and pending.get("snapshot_final", True) else previous["stamp"],
                            "scan_stamp": scan_stamp, "scan_ids": list(dict.fromkeys(delivered + scan_ids))})
                        if scan_ids:
                            self.scan_progress = f"Снимки: {len(set(delivered + scan_ids))} / {pending.get('scan_total', 1)} подтверждено сервером"
                        if pending["saved"] is not None and pending.get("snapshot_final", True) and route["id"] in self.snapshot_sources:
                            self.seen[self.snapshot_sources[route["id"]]] = tuple(pending["stamp"])
                        request_path.unlink()
                        self.last_success = int(time.time())
                    elif state["state"] == "failed":
                        pending["sequence"] += 1
                        atomic_json(request_path, pending)
                        raise ValueError("Сервер не завершил обмен. Сохранение осталось в очереди")
                self.write_import()
                self.worker_error = ""
                self.worker_error_code = ""
                self.error_details = {}
                self.errors.clear()
                self.next_tick = time.monotonic()+8
            except httpx.HTTPStatusError as error:
                self.error_details = http_diagnostic(error)
                status = error.response.status_code
                self.worker_error_code = "WORKER_KEY_REJECTED" if status in {401, 403} else "CHARACTER_DISCOVERY_INVALID" if status == 422 and self.error_details["operation"] == "character_discovery" else "PRIVATE_HTTP_ERROR"
                if self.worker_error_code == "CHARACTER_DISCOVERY_INVALID":
                    fields = ", ".join(self.error_details["validation_fields"])
                    self.worker_error = "Сервер отклонил сведения о персонажах (HTTP 422)" + (": " + fields if fields else "") + ". Обновите Worker; сохранения не удаляйте"
                else:
                    self.worker_error = "Ключ рабочего клиента неверен или отозван. Для нового подключения оставьте поле ключа пустым и нажмите «Сохранить и установить аддон»" if status in {401, 403} else f"Сервер отклонил обмен (HTTP {status}). Данные сохранены; скопируйте отчёт поддержки"
                self.next_tick = time.monotonic()+30
            except httpx.HTTPError:
                self.worker_error = "Нет связи с сервером. Очередь сохранена; повторим позже"
                self.worker_error_code = "SERVER_UNREACHABLE"
                self.error_details = {}
                self.next_tick = time.monotonic()+30
            except (ValueError, OSError, KeyError, TypeError) as error:
                self.worker_error = "Не удалось обработать обмен. Очередь сохранена; проверьте версию аддона и сервера"
                self.worker_error_code = "PRIVATE_DATA_INVALID"
                self.error_details = {"error_type": type(error).__name__}
                self.next_tick = time.monotonic()+30

    def overview(self):
        pending = len(list((self.root / "requests").glob("*.json")))
        status = "Нужна настройка" if not self.settings.get("game_root") else "Приостановлено" if self.settings["paused"] else "Ошибка" if self.worker_error or self.errors else "Подтвердите код в вебе: "+self.pairing["code"] if self.pairing else "Работает"
        if self.cycle.error_code and status == "Работает":
            status = "Ошибка автоцикла"
        note = " · ".join((self.manifest or {}).get("notes",[]))
        if self.scan_progress:
            note = (note+" · " if note else "")+self.scan_progress
        if self.manifest and not self.manifest["clients"] and not self.pairing:
            note=note or "Войдите в персонажей, выберите роль через /cennik role, затем /reload"
        if self.idle.settings.get("enabled"):
            note=(note+" · " if note else "")+self.idle.message
        if self.cycle.settings.get("enabled"):
            note=(note+" · " if note else "")+self.cycle.message
        return {"status": status, "error": self.worker_error or next(iter(self.errors.copy().values()), "") or (self.cycle.message if self.cycle.error_code else ""),
            "paused": self.settings["paused"], "pending": pending, "rejected": 0, "checked_at": self.checked_at,
            "last_import": self.last_success, "delivery": {"at": self.last_success}, "local": [],
            "connection": self.client.public_connection(), "game_root": self.settings.get("game_root", ""), "notice":note,
            "game_code": "", "game_status": "", "game_at": None}

    def report(self):
        return json.dumps({"application": "Cennik Worker", "version": VERSION, "id": self.settings["id"],
            "state": self.overview()["status"], "pending": self.overview()["pending"], "last_success": self.last_success,
            "error_code": self.worker_error_code or self.cycle.error_code or ("PRIVATE_EXCHANGE_FAILED" if self.worker_error or self.errors else ""),
            "error": self.worker_error, "details": self.error_details, "scan_progress": self.scan_progress,
            "auction_cycle": self.cycle.report()}, ensure_ascii=False, indent=2)

    def cycle_tick(self):
        route = next((r for r in (self.manifest or {}).get("clients", []) if r["id"] == self.cycle.settings.get("client_id")), None)
        stamp, delivered = None, False
        if route:
            path = Path(self.snapshot_sources.get(route["id"], str(Path(self.settings["game_root"]) / "WTF/Account" / route["account"] / "SavedVariables/CennikAddon.lua")))
            if path.exists():
                info = path.stat()
                stamp = (info.st_size, info.st_mtime_ns)
                previous = self.read("state-"+route["id"]+".json", {})
                delivered = previous.get("stamp") == list(stamp) and not (self.root / "requests" / (route["id"]+".json")).exists()
        # Observe local reload completion even during a server outage. Network
        # failure blocks new inputs through the delivery gate, not observation.
        delivered = delivered and not bool(self.worker_error or self.errors or self.pairing)
        self.cycle.tick(paused=self.settings["paused"], route=route, stamp=stamp, delivered=delivered)
