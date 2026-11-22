"""Durable bidirectional exchange with the full server, separate from collectors."""
from __future__ import annotations

import hashlib
from collections import OrderedDict
import json
import threading
import time
from pathlib import Path

import httpx
from pydantic import Field

from ahledger.util import atomic_json, atomic_write
from cennik.addon.limits import MAX_EXCHANGE_BYTES, MAX_SAVED_BYTES, MAX_SAVED_TOKENS
from cennik.addon.lua_data import Parser, lua_literal, read_saved_variables
from cennik.collector.agent import CollectorAgent, addon_source, game_directory
from cennik.collector.auctioneers import validate_snapshot
from cennik.collector.protocol import Connection
from cennik.core.models import Model
from cennik.markets import MarketBinding, equivalent_realms, neutral_market
from cennik.workspace.install import install_addon

from .automation import AuctionCycle, character_key
from .idle import IdleProbe

FIELDS = {"device_id", "binding", "events", "market_scans", "npc_quotes", "collector_status", "server_binding",
    "collector_npc", "collector_recipes", "collector_transforms", "auctioneer_proposals", "sniper", "bulk_checks", "accounting", "production"}

VERSION = "0.8.2"


def scoped_saved(saved, market, realm, faction, binding=None):
    """Never relabel another market's retained raw scans or merchant quotes."""
    clean = {k: v for k, v in saved.items() if k in FIELDS}
    scans = saved.get("market_scans") or []
    clean["market_scans"] = [s for s in scans if s.get("market") in {market, neutral_market(market)}
        and (s.get("realm", realm) == realm or equivalent_realms(s.get("realm"), realm, binding))]
    for field in ("npc_quotes", "collector_npc"):
        if field in clean:
            def matching_scope(value):
                scope = str(value.get("scope", "")).rsplit("/", 1)
                return len(scope) == 2 and scope[1].casefold() == faction.casefold() and (
                    scope[0].casefold() == realm.casefold() or equivalent_realms(scope[0], realm, binding))
            clean[field] = {k: v for k, v in clean[field].items()
                if matching_scope(v)}
    return clean, [s for s in scans if s not in clean["market_scans"]]


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
                   "professions", "market", "game_version", "client_id", "sequence", "saved", "secret"}
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
    role: str = Field(pattern=r"^(auctioneer|crafter|observer)$")
    character: str = Field(min_length=1, max_length=80)
    realm: str = Field(min_length=1, max_length=100)
    account: str = Field(max_length=100, pattern=r"^[^/\\:]+$")


class Manifest(Model):
    format_version: int
    device_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    profile: MarketBinding
    clients: list[Route] = Field(default_factory=list, max_length=64)
    notes: list[str] = Field(default_factory=list)
    auctioneer_registry: dict | None = None
    any_character: bool = False
    session: dict = Field(default_factory=dict)


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
        self.control_thread: threading.Thread | None = None
        self.current_route_id = None
        self.route_exchange_at = {}
        self.active_sources = set()
        self.snapshot_cache = OrderedDict()
        self.pending_summary = {}
        self.queue_phase = ""
        self.file_errors = {}
        self.last_scan_success = self.read('scan-delivery.json', {}).get('at')

    def read_snapshot(self, path, stamp):
        key = str(path)
        cached = self.snapshot_cache.get(key)
        if cached and cached[0] == stamp:
            self.snapshot_cache.move_to_end(key)
            return cached[1]
        self.snapshot_cache.pop(key, None)
        self.queue_phase = "Читаем сохранение игры · "+path.parents[1].name
        data = read_saved_variables(path.read_text("utf-8-sig"), max_bytes=MAX_SAVED_BYTES, max_tokens=MAX_SAVED_TOKENS)
        after = path.stat()
        if (after.st_size, after.st_mtime_ns) != stamp:
            return None
        if stamp[0] <= 64_000_000:
            self.snapshot_cache[key] = (stamp, data)
            while len(self.snapshot_cache) > 8 or sum(v[0][0] for v in self.snapshot_cache.values()) > 64_000_000:
                self.snapshot_cache.popitem(last=False)
        return data

    def drive_controls(self):
        if not self.settings.get('game_root'):
            return
        self.cycle_tick()
        self.idle.tick(paused=self.settings['paused'] or (
            self.cycle.settings.get('enabled', False) and not self.cycle.allow_idle))

    def start(self):
        def control_loop():
            while not self.stop.is_set():
                try:
                    self.drive_controls()
                    self.errors.pop('auction_cycle', None)
                except Exception:
                    self.errors['auction_cycle'] = 'Не удалось проверить автоцикл; скопируйте отчёт поддержки'
                self.stop.wait(1)
        self.control_thread = threading.Thread(target=control_loop, name='worker-auction-cycle', daemon=True)
        self.control_thread.start()
        super().start()

    def close(self):
        super().close()
        if self.control_thread:
            self.control_thread.join(timeout=5)

    def request(self, method, path, *, body=None, connection=None):
        c = connection or self.client.connection()
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode() if body is not None else None
        if payload is not None and len(payload) > MAX_EXCHANGE_BYTES:
            raise ValueError("Пакет обмена превышает 60 МБ; исходные данные оставлены на ПК")
        with httpx.Client(base_url=c.url, headers={"X-Bridge-Key": c.token, "X-Cennik-Worker":"0.4"}, transport=self.client.transport,
                          follow_redirects=False, trust_env=False, timeout=httpx.Timeout(30, connect=10, write=180)) as http:
            response = http.request(method, path, content=payload,
                                    headers={"Content-Type": "application/json"} if payload is not None else None)
            response.raise_for_status()
            return response.json()

    def fetch_manifest(self, connection=None):
        value = self.request("GET", "/bridge/v1/manifest", connection=connection)
        profile = {k: value["profile"][k] for k in MarketBinding.model_fields}
        parsed = Manifest.model_validate({**value, "profile": profile})
        if parsed.auctioneer_registry is not None:
            parsed.auctioneer_registry = validate_snapshot(parsed.auctioneer_registry)
            if any(e['data'].get('market') != parsed.profile.market for e in parsed.auctioneer_registry['entries']):
                raise ValueError('Справочник аукционистов относится к другому рынку')
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
        self.file_errors = {}
        root = Path(self.settings["game_root"])
        for path in sorted((root / "WTF/Account").glob("*/SavedVariables/CennikAddon.lua")):
            try:
                before = path.stat()
            except OSError:
                self.file_errors[path.parents[1].name] = "Файл временно недоступен"
                continue
            if before.st_size > MAX_SAVED_BYTES:
                self.file_errors[path.parents[1].name] = "Сохранение больше 256 МБ; данные не удалены"
                continue
            stamp = before.st_size, before.st_mtime_ns
            if self.seen.get(str(path)) == stamp:
                continue
            previous = self.quiet.get(str(path))
            if not previous or previous[0] != stamp:
                self.quiet[str(path)] = stamp, time.monotonic()
                continue
            if time.monotonic()-previous[1] < 1:
                continue
            try:
                data = self.read_snapshot(path, stamp)
                if data is not None and (not isinstance(data, dict) or not isinstance(data.get('binding') or {}, dict)):
                    raise ValueError('Invalid binding')
            except (ValueError, OSError, TypeError, KeyError):
                self.file_errors[path.parents[1].name] = "Не удалось прочитать сохранение; остальные аккаунты продолжают отправку"
                continue
            if data is None:
                continue
            binding = data.get("binding") or {}
            profiles=data.get("character_profiles") or {}
            for profile in profiles.values() if isinstance(profiles,dict) else []:
                if not isinstance(profile,dict) or profile.get("role") not in {"auctioneer","crafter","unassigned"}:
                    continue
                # Older addons saved profiles during ADDON_LOADED, before realm
                # or faction was ready. One incomplete identity must not reject
                # every account's discovery packet. Leave the save untouched;
                # its next stable revision will be read and registered normally.
                if (not isinstance(profile.get("realm"), str) or not profile["realm"].strip()
                    or not isinstance(profile.get("character"), str) or not profile["character"].strip()
                    or profile.get("faction") not in {"alliance", "horde"}):
                    continue
                if profile.get("market") and profile["market"] != self.manifest["profile"]["market"]:
                    continue
                row={k:profile[k] for k in ("role","role_chosen_at","realm","character","faction","professions","market","game_version") if k in profile}
                if isinstance(row.get("professions"), dict) and not row["professions"]:
                    row["professions"] = []
                row["account"]=path.parents[1].name
                characters[(row.get("realm"),row.get("character"))]=row
            if binding.get("market") != self.manifest["profile"]["market"]:
                self.active_sources.discard(str(path))
                continue
            self.active_sources.add(str(path))
            route = next((r for r in self.manifest["clients"] if (r["realm"], r["character"]) == (binding.get("realm"), binding.get("character"))), None)
            if route:
                previous = self.read("state-"+route["id"]+".json", {})
                if previous.get("stamp") == list(stamp):
                    self.seen[str(path)] = stamp
                else:
                    cleaned, foreign = scoped_saved(data, self.manifest["profile"]["market"],
                        route["realm"], self.manifest["profile"]["faction"], self.manifest["profile"])
                    for old in foreign:
                        digest = hashlib.sha256(json.dumps(old, sort_keys=True).encode()).hexdigest()
                        quarantine = self.root / "quarantine" / (digest + ".json")
                        if not quarantine.exists():
                            atomic_json(quarantine, {"reason": "foreign_market", "scan": old})
                            quarantine.chmod(0o600)
                    found[route["id"]] = (cleaned, stamp)
                    self.snapshot_sources[route["id"]] = str(path)
        if characters:
            values=sorted(characters.values(),key=lambda row:(row["role"]=="unassigned",row["realm"],row["character"]))[:30]
            digest=hashlib.sha256(json.dumps(values,sort_keys=True).encode()).hexdigest()
            if digest!=self.discovery_hash:
                discovery = {"characters": values}
                if self.manifest.get('any_character'):
                    discovery['automatic_observers'] = True
                self.request("POST","/bridge/v1/characters",body=discovery)
                self.manifest=self.fetch_manifest()
                atomic_json(self.root/"manifest.json",self.manifest)
                self.discovery_hash=digest
        return found

    def pending_counts(self):
        scans=set()
        exchanges=0
        for path in (self.root/'requests').glob('*.json'):
            try:
                stat=path.stat()
                stamp=(stat.st_size,stat.st_mtime_ns)
                cached=self.pending_summary.get(str(path))
                if cached and cached[0]==stamp:
                    scans.update(cached[1])
                    exchanges+=1
                    continue
                row=json.loads(path.read_text('utf-8'))
            except (OSError,ValueError):
                continue
            exchanges+=1
            saved=row.get('saved') if isinstance(row,dict) else None
            ids={scan.get('scan_id') or path.stem for scan in (saved.get('market_scans') or []) if isinstance(scan,dict)} if isinstance(saved,dict) else set()
            self.pending_summary[str(path)]=(stamp,ids)
            scans.update(ids)
        self.pending_summary={key:value for key,value in self.pending_summary.items() if Path(key).exists()}
        return len(scans),exchanges

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
        shared = {}
        catalog_priority = -1
        for route in self.manifest["clients"]:
            path = self.root / "imports" / (route["id"]+".json")
            if not path.exists():
                continue
            payload = json.loads(path.read_text("utf-8"))
            if payload.get("client_id") != route["id"] or payload.get("market_collector", {}).get("market") != self.manifest["profile"]["market"]:
                raise ValueError("Сервер прислал импорт другого персонажа или рынка")
            payload["automation"] = self.cycle.wire(route)
            payload['worker_automatic'] = bool(self.manifest.get('any_character'))
            catalog=payload.pop('sniper_catalog',None)
            priority=2 if route['id']==self.current_route_id else 1
            if catalog and priority>catalog_priority:
                shared['sniper_catalog']=catalog
                catalog_priority=priority
            if self.manifest.get('auctioneer_registry') is not None:
                payload['auctioneer_registry'] = self.manifest['auctioneer_registry']
            routes[route["realm"]+":"+route["character"]] = payload
        addon = Path(self.settings["game_root"]) / "Interface/AddOns/CennikAddon"
        self.owned_import(addon)
        fallback={"format_version":1,"packages":[],"worker_discovery":True,"market_binding":self.manifest["profile"],
            "market_collector":{"workspace_id":"discovery-"+self.manifest["device_id"],"market":self.manifest["profile"]["market"],"auto_scan":False}}
        if self.manifest.get('any_character'):
            session = self.manifest['session']
            fallback['market_collector']['workspace_id'] = session['workspace_id']
            fallback['sniper_book'] = session['sniper_book']
            fallback['automation'] = self.cycle.wire(None)
            fallback['worker_automatic'] = True
        if self.manifest.get('auctioneer_registry') is not None:
            fallback['auctioneer_registry'] = self.manifest['auctioneer_registry']
        payload = {"format_version": 2, "remote_device_id": self.manifest["device_id"], "worker_agent_id":self.settings["id"], "routes": routes,"discovery":fallback}
        payload['shared']=shared
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
            if self.control_thread is None or not self.control_thread.is_alive():
                self.drive_controls()
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
                    self.write_import()
                snapshots = self.game_snapshots()
                for route in sorted(self.manifest["clients"],key=lambda r:r['id']!=self.current_route_id):
                    request_path = self.root / "requests" / (route["id"]+".json")
                    previous = self.read("state-"+route["id"]+".json", {"sequence": 0, "stamp": None})
                    if request_path.exists():
                        pending = json.loads(request_path.read_text("utf-8"))
                    else:
                        saved, stamp = snapshots.get(route["id"], (None, None))
                        if (saved is None and route['id']!=self.current_route_id
                            and (self.root/'imports'/(route['id']+'.json')).exists()
                            and (route['role']=='observer' or time.monotonic()-self.route_exchange_at.get(route['id'],0)<60)):
                            continue
                        pending = {"client_id": route["id"], "sequence": previous["sequence"]+1,
                                   "saved": None, "stamp": stamp}
                        if saved is not None and stamp and list(stamp) != previous["stamp"]:
                            pending.update(saved_batch(saved, stamp, previous))
                        atomic_json(request_path, pending)
                    body = {k: pending[k] for k in ("client_id", "sequence", "saved")}
                    try:
                        state = None
                        if pending.get("submitted"):
                            state = self.request("GET", "/bridge/v1/exchanges/"+route["id"])
                            if state.get("sequence") != pending["sequence"] or state.get("state") not in {"queued", "completed", "failed"}:
                                state = None
                        if state is None:
                            if pending.get('scan_ids'):
                                self.queue_phase = "Отправляем снимок · "+route['character']
                            state = self.request("POST", "/bridge/v1/exchange", body=body)
                            pending['submitted'] = True
                            atomic_json(request_path, pending)
                    except httpx.HTTPStatusError as error:
                        if error.response.status_code == 409:
                            detail = error.response.json().get("detail")
                            if isinstance(detail, dict) and detail.get("code") == "STALE_SEQUENCE":
                                pending["sequence"] = detail["next_sequence"]
                                pending.pop('submitted', None)
                                atomic_json(request_path, pending)
                        raise
                    deadline = time.monotonic()+3
                    while state["state"] == "queued" and time.monotonic() < deadline and not self.stop.wait(.15):
                        if pending.get('scan_ids'):
                            self.queue_phase = "Снимок отправлен; сервер обрабатывает · "+route['character']
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
                        # Every account receives its ACK immediately, including when
                        # no automation window is selected or its marker is hidden.
                        self.write_import()
                        scan_stamp = pending["stamp"] if pending["saved"] is not None else previous.get("scan_stamp")
                        delivered = previous.get("scan_ids", []) if previous.get("scan_stamp") == scan_stamp or previous.get("scan_stamp") == list(scan_stamp or []) else []
                        atomic_json(self.root / ("state-"+route["id"]+".json"), {"sequence": pending["sequence"],
                            "stamp": pending["stamp"] if pending["saved"] is not None and pending.get("snapshot_final", True) else previous["stamp"],
                            "scan_stamp": scan_stamp, "scan_ids": list(dict.fromkeys(delivered + scan_ids))})
                        if scan_ids:
                            self.scan_progress = f"Снимки: {len(set(delivered + scan_ids))} / {pending.get('scan_total', 1)} подтверждено сервером"
                            self.last_scan_success=int(time.time())
                            atomic_json(self.root/'scan-delivery.json',{'at':self.last_scan_success,'scan_ids':scan_ids})
                        if pending["saved"] is not None and pending.get("snapshot_final", True) and route["id"] in self.snapshot_sources:
                            self.seen[self.snapshot_sources[route["id"]]] = tuple(pending["stamp"])
                            self.snapshot_cache.pop(self.snapshot_sources[route['id']], None)
                        request_path.unlink()
                        self.last_success = int(time.time())
                        self.route_exchange_at[route['id']] = time.monotonic()
                        if scan_ids:
                            self.queue_phase = "Снимок сохранён сервером; /reload загрузит подтверждение в игру"
                    elif state["state"] == "failed":
                        pending["sequence"] += 1
                        pending.pop('submitted', None)
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
                    self.worker_error = "Сервер отклонил сведения о персонажах (HTTP 422)" + (": " + fields if fields else "") + ". Обновите Worker и сервер; сохранения не удаляйте"
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
        pending, exchanges = self.pending_counts()
        status = "Нужна настройка" if not self.settings.get("game_root") else "Приостановлено" if self.settings["paused"] else "Ошибка" if self.worker_error or self.errors else "Подтвердите код в вебе: "+self.pairing["code"] if self.pairing else "Работает"
        if self.cycle.error_code and status == "Работает":
            status = "Ошибка автоцикла"
        note = "Передача данных работает без выбора окна. Автоскан включается в аддоне: Рынок → Автоскан."
        if pending:
            note += " "+(self.queue_phase or "Снимки ожидают отправки")
        elif self.scan_progress:
            note = (note+" · " if note else "")+self.scan_progress
        if self.file_errors:
            note += f" · Не прочитано сохранений: {len(self.file_errors)}; остальные обрабатываются"
        if self.manifest and not self.manifest["clients"] and not self.pairing:
            note += " Войдите в игру и выполните /reload; персонаж подключится автоматически." if self.manifest.get('any_character') else " Войдите в персонажей, выберите роль через /cennik role, затем /reload."
        if self.idle.settings.get("enabled"):
            note=(note+" · " if note else "")+self.idle.message
        if self.cycle.settings.get("enabled"):
            note=(note+" · " if note else "")+self.cycle.message
            signal=self.cycle.last_signal or {}
            target=self.cycle.settings.get('target') or {}
            note=f"PID {target.get('pid','?')} · {signal.get('character') or 'подключаем персонажа'} · "+note
        return {"status": status, "error": self.worker_error or next(iter(self.errors.copy().values()), "") or (self.cycle.message if self.cycle.error_code else ""),
            "paused": self.settings["paused"], "pending": pending, "rejected": 0, "checked_at": self.checked_at,
            "pending_exchanges":exchanges,"last_import": self.last_success, "delivery": {"at": self.last_success,"scan_at":self.last_scan_success}, "local": [],
            "connection": self.client.public_connection(), "game_root": self.settings.get("game_root", ""), "notice":note,
            "game_code": "", "game_status": "", "game_at": None}

    def report(self):
        return json.dumps({"application": "Cennik Worker", "version": VERSION, "id": self.settings["id"],
            "state": self.overview()["status"], "pending": self.overview()["pending"], "last_success": self.last_success,
            "error_code": self.worker_error_code or self.cycle.error_code or ("PRIVATE_EXCHANGE_FAILED" if self.worker_error or self.errors else ""),
            "error": self.worker_error, "details": self.error_details, "scan_progress": self.scan_progress,
            "queue_phase":self.queue_phase,"file_errors":self.file_errors,"last_scan_success":self.last_scan_success,
            "auction_cycle": self.cycle.report(),
            "isolated_foreign_scans": sum(1 for _ in (self.root / "quarantine").glob("*.json"))}, ensure_ascii=False, indent=2)

    def cycle_tick(self):
        route = next((r for r in (self.manifest or {}).get("clients", []) if r["id"] == self.cycle.settings.get("client_id")), None)
        signal = None
        if self.cycle.settings.get('enabled') and self.cycle.settings.get('any_character'):
            engine = self.cycle.engine()
            target = self.cycle.settings.get('target')
            windows = engine.windows(self.settings['game_root'])
            from .idle import selected_window
            if selected_window(target, windows) is None and len(windows)==1:
                self.cycle.settings['target'] = windows[0]
                atomic_json(self.cycle.path,self.cycle.settings)
                target=windows[0]
            if target and engine.ready(target):
                signal=engine.signal(target,self.cycle.settings['token'])
            matches=[r for r in (self.manifest or {}).get('clients',[]) if signal and signal.character==character_key(r['realm'],r['character'])]
            route=matches[0] if len(matches)==1 else None
            self.current_route_id=route['id'] if route else None
        stamp, delivered = None, False
        if route:
            path = Path(self.snapshot_sources.get(route["id"], str(Path(self.settings["game_root"]) / "WTF/Account" / route["account"] / "SavedVariables/CennikAddon.lua")))
            if path.exists():
                info = path.stat()
                stamp = (info.st_size, info.st_mtime_ns)
                previous = self.read("state-"+route["id"]+".json", {})
                pending=self.read('requests/'+route['id']+'.json',{})
                delivered = previous.get("stamp") == list(stamp) and pending.get('saved') is None
                if (self.cycle.settings.get('enabled') and not self.settings['paused']
                    and previous.get('stamp')!=list(stamp) and not self.worker_error):
                    # A newly saved file should not wait for the normal eight-second poll.
                    self.next_tick=0
                    self.client.next_attempt=0
                    self.wake.set()
        # Observe local reload completion even during a server outage. Network
        # failure blocks new inputs through the delivery gate, not observation.
        delivered = delivered and not bool(self.worker_error or self.errors or self.pairing)
        if delivered and self.cycle.settings.get('any_character'):
            for source in self.active_sources.copy():
                path=Path(source)
                if path.exists():
                    info=path.stat()
                    if self.seen.get(source)!=(info.st_size,info.st_mtime_ns):
                        delivered=False
                        break
        self.cycle.tick(paused=self.settings["paused"], route=route, stamp=stamp, delivered=delivered, observed_signal=signal)
