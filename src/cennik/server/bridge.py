"""Role-bound private device exchange, reusing all existing workspace pipelines."""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from uuid import uuid4

from fastapi import HTTPException
from pydantic import Field, field_validator

from ahledger.util import atomic_json, atomic_write, dumps
from cennik.addon.lua_data import lua_literal
from cennik.core.models import Model
from cennik.markets import MarketBinding, realm_matches
from cennik.workspace.accounting import LedgerEvent
from cennik.workspace.production import ClientRoster, ProductionClient

SAVED_FIELDS = {"device_id", "binding", "events", "market_scans", "npc_quotes", "collector_status", "server_binding",
    "collector_npc", "collector_recipes", "collector_transforms", "auctioneer_proposals", "sniper", "bulk_checks", "accounting", "production"}


class RemoteCharacter(Model):
    role: str = Field(pattern=r"^(auctioneer|crafter)$")
    character: str = Field(min_length=1, max_length=80)
    realm: str = Field(min_length=1, max_length=100)
    account: str = Field(default="", max_length=100, pattern=r"^[^/\\:]*$")


class RegisterDevice(Model):
    label: str = Field(min_length=1, max_length=100)
    clients: list[RemoteCharacter] = Field(default_factory=list, max_length=8)


class CharacterDiscovery(RemoteCharacter):
    role_chosen_at: int = Field(default=0, ge=0, le=2**53-1, strict=True)
    role: str = Field(pattern=r"^(auctioneer|crafter|unassigned)$")
    faction: str = Field(pattern=r"^(alliance|horde)$")
    professions: list[str] = Field(default_factory=list, max_length=20)
    market: str | None = Field(default=None, max_length=160)
    game_version: str | None = Field(default=None, max_length=80)

    @field_validator("professions", mode="before")
    @classmethod
    def empty_lua_professions(cls, value):
        # Lua's empty table has no array/object distinction. Accept old Workers too.
        return [] if isinstance(value, dict) and not value else value


class DiscoverCharacters(Model):
    characters: list[CharacterDiscovery] = Field(max_length=30)
    automatic_observers: bool = False


class ObservationClient(ProductionClient):
    role: str = Field(default='observer', pattern='^observer$')


class AssignAuctioneer(Model):
    character: str = Field(min_length=1, max_length=80)
    realm: str = Field(min_length=1, max_length=100)


class ExchangeRequest(Model):
    client_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    sequence: int = Field(gt=0, le=2**53-1, strict=True)
    saved: dict | None = None


class PrivateBridge:
    def __init__(self, catalog):
        self.catalog = catalog
        self.root = catalog.root / "workers"
        self.lock = threading.RLock()
        self.rates = {}

    def devices(self):
        return [json.loads(p.read_text("utf-8")) for p in self.root.glob("*.json")]

    def register(self, profile_id, value: RegisterDevice):
        with self.lock:
            profile = self.catalog.profile(profile_id)
            w = self.catalog.workspace(profile_id)
            identity, token = uuid4().hex, secrets.token_urlsafe(36)
            clients = w.production.clients()
            created = []
            for spec in value.clients:
                if not realm_matches(profile, spec.realm):
                    raise ValueError("Сначала привяжите имя игрового сервера в панели сервера")
                account = spec.account or uuid4().hex
                if account in {".", ".."}:
                    raise ValueError("Неверная папка аккаунта")
                client = ProductionClient(remote_device=identity, role=spec.role, character=spec.character,
                    realm=spec.realm, account=account, game_root=str(w.root / "remote-games" / identity))
                client.game_root = str(w.root / "remote-games" / identity / client.id)
                clients.append(client)
                created.append(client.id)
            w.production.save_clients(ClientRoster(clients=clients))
            device = {"id": identity, "label": value.label, "profile_id": profile_id, "clients": created,
                "token_hash": hashlib.sha256(token.encode()).hexdigest(), "revoked": False, "last_seen": None}
            atomic_json(self.root / (identity+".json"), device)
            return {"id": identity, "token": token, "clients": created}

    def authenticate(self, token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            device = next((d for d in self.devices() if not d["revoked"] and secrets.compare_digest(digest, d["token_hash"])), None)
            if device is None:
                raise HTTPException(401, "Неверный или отозванный ключ рабочего клиента")
            start, count = self.rates.get(device["id"], (time.monotonic(), 0))
            if time.monotonic()-start > 60:
                start, count = time.monotonic(), 0
            if count >= 600:
                raise HTTPException(429, "Слишком частые запросы рабочего клиента")
            self.rates[device["id"]] = start, count+1
            return device

    def revoke(self, identity):
        with self.lock:
            device = next((d for d in self.devices() if d["id"] == identity), None)
            if device is None:
                raise ValueError("Рабочий клиент не найден")
            device["revoked"] = True
            atomic_json(self.root / (identity+".json"), device)
            return {"revoked": True}

    def manifest(self, device, *, extended=False, include_registry=False, automatic=False):
        device = next(d for d in self.devices() if d["id"] == device["id"])
        profile = self.catalog.profile(device["profile_id"])
        workspace = self.catalog.workspace(profile.id)
        clients = self.clients(device, workspace)
        result = {"format_version": 1, "device_id": device["id"], "profile": profile.public(), "clients": [
            {k: getattr(c, k) for k in ("id", "role", "realm", "character", "account")} for c in clients if c.id in device["clients"] and (automatic or c.role!='observer')]}
        if extended:
            result["notes"] = device.get("notes", [])
        if automatic:
            result['any_character'] = True
            result['session'] = {'workspace_id': workspace.identity, 'sniper_book': workspace.sniper.wire()}
        if include_registry:
            registry = self.catalog.workspace(profile.id).collectors.registry()
            result['auctioneer_registry'] = {**registry,
                'entries': [e for e in registry['entries'] if e['data'].get('market') == profile.market],
                'market_catalog': [{k: profile.public()[k] for k in MarketBinding.model_fields}]}
        return result

    def clients(self, device, workspace):
        production = [c for c in workspace.production.clients() if c.id in device['clients']]
        known = {(c.realm, c.character) for c in production}
        return production + [ObservationClient.model_validate(row) for row in device.get('observers', [])
                             if (row['realm'], row['character']) not in known]

    def assign_auctioneer(self, identity, value: AssignAuctioneer):
        """An explicit owner decision can adopt roles from older addon reports."""
        with self.lock:
            device = next((d for d in self.devices() if d['id'] == identity and not d['revoked']), None)
            if device is None:
                raise ValueError('Рабочий клиент не найден или его ключ отозван')
            profile = self.catalog.profile(device['profile_id'])
            rows = [CharacterDiscovery.model_validate(r) for r in device.get('discovered', [])]
            chosen = next((r for r in rows if (r.character, r.realm) == (value.character, value.realm)), None)
            if chosen is None:
                raise ValueError('Персонаж ещё не обнаружен этим Worker')
            if chosen.market != profile.market or chosen.faction != profile.faction or not realm_matches(profile, chosen.realm):
                raise ValueError('Персонаж не относится к выбранному рынку')
            if profile.client_family == 'forever' and not (chosen.game_version or '').startswith('1.60.'):
                raise ValueError('Персонаж не подтверждён клиентом Forever')
            workspace = self.catalog.workspace(profile.id)
            current = next((c for c in workspace.production.clients() if c.role == 'auctioneer'), None)
            if current and current.remote_device != identity:
                raise ValueError('Текущий аукционер назначен другому Worker')
            if current and (current.character, current.realm) == (chosen.character, chosen.realm):
                return {'assigned': True, 'changed': False, 'client_id': current.id, 'character': current.character}
            if current and any(not o.get('closed') and not o.get('cancelled')
                               and current.id in (o['auctioneer_id'], o['crafter_id']) for o in workspace.production.orders()):
                raise ValueError('Текущий аукционер участвует в активном производственном заказе')
            at = max(int(time.time()*1000), max((c.role_chosen_at for c in workspace.production.clients()), default=0)+1)
            rows = [r.model_copy(update={'role': 'auctioneer', 'role_chosen_at': at}) if r is chosen else r for r in rows]
            result = self.discover(device, DiscoverCharacters(characters=rows))
            target = next((c for c in result['clients'] if c['role'] == 'auctioneer'
                           and (c['character'], c['realm']) == (chosen.character, chosen.realm)), None)
            if target is None:
                raise ValueError('; '.join(result.get('notes', [])) or 'Не удалось назначить аукционера')
            atomic_json(self.root/'role-decisions'/(uuid4().hex+'.json'), {
                'source': 'owner', 'at': at, 'device_id': identity, 'client_id': target['id'],
                'character': chosen.character, 'realm': chosen.realm, 'previous_client_id': current.id if current else None})
            return {'assigned': True, 'changed': True, 'client_id': target['id'], 'character': target['character']}

    def discover(self, device, value: DiscoverCharacters):
        with self.lock:
            device=next(d for d in self.devices() if d["id"]==device["id"])
            profile=self.catalog.profile(device["profile_id"])
            rows=[]
            discovery_notes=[]
            for row in value.characters:
                if row.faction != profile.faction:
                    continue
                if row.market and row.market != profile.market:
                    discovery_notes.append("Сохранённая роль относится к другому рынку; выберите её заново в аддоне")
                    continue
                if profile.client_family == "forever" and row.game_version and not row.game_version.startswith("1.60."):
                    discovery_notes.append("Клиент не Forever: Retail/Classic тестируется только в отдельном тестовом рынке")
                    continue
                if not profile.realm_names and not row.market:
                    discovery_notes.append("Обновите аддон и повторно выберите роль: старые роли без привязки к рынку не подтверждают имя релизного сервера")
                    continue
                rows.append(row)
            if not profile.realm_names and len({r.realm for r in rows})==1:
                profile=self.catalog.save(profile.model_copy(update={"realm_names":[rows[0].realm]}))
            w=self.catalog.workspace(profile.id)
            clients=w.production.clients()
            notes=discovery_notes
            revoked = {d["id"] for d in self.devices() if d["revoked"]}
            active = {identity for order in w.production.orders() if not order.get("closed") and not order.get("cancelled")
                      for identity in (order["auctioneer_id"], order["crafter_id"])}
            retired = []
            # Owner approval of a new device may reclaim revoked, idle roles.
            # History stays in the ledger/orders and the retired roster snapshot.
            for old in list(clients):
                reclaim = any((r.realm, r.character) == (old.realm, old.character) for r in rows if r.role != "unassigned")
                replace_auctioneer = old.role == "auctioneer" and any(r.role == "auctioneer" for r in rows)
                if old.remote_device in revoked and (reclaim or replace_auctioneer):
                    if old.id in active:
                        notes.append("Персонаж отозванного ПК занят активным заказом; сначала завершите заказ")
                        continue
                    clients.remove(old)
                    retired.append(old.model_dump(mode="json"))
            for row in rows:
                if row.role=="unassigned":
                    continue
                if not realm_matches(profile, row.realm):
                    notes.append("Сервер персонажа не совпадает с выбранным рынком")
                    continue
                if row.role_chosen_at > int(time.time()*1000)+300000:
                    notes.append('Время выбора роли находится в будущем; повторно выберите роль в аддоне')
                    continue
                current = next((c for c in clients if c.role == 'auctioneer'), None)
                if row.role == 'auctioneer' and current and (current.realm, current.character) != (row.realm, row.character):
                    if current.remote_device != device['id']:
                        notes.append('Аукционер назначен другому ПК; смените его через владельца')
                        continue
                    if row.role_chosen_at <= max((c.role_chosen_at for c in clients), default=0):
                        if not row.role_chosen_at and not current.role_chosen_at:
                            notes.append('Повторно выберите роль нужного аукционера в обновлённом аддоне')
                        continue
                    clients = [c.model_copy(update={'role': 'crafter', 'role_chosen_at': row.role_chosen_at})
                               if c.id == current.id else c for c in clients]
                existing=next((c for c in clients if (c.realm,c.character)==(row.realm,row.character)),None)
                if existing and existing.remote_device!=device["id"]:
                    notes.append("Персонаж уже назначен другому рабочему ПК")
                    continue
                if existing:
                    if row.role_chosen_at < existing.role_chosen_at:
                        continue
                    clients=[c.model_copy(update={"role":row.role, 'role_chosen_at': row.role_chosen_at}) if c.id==existing.id else c for c in clients]
                else:
                    observer = next((o for o in device.get('observers', []) if (o['realm'],o['character']) == (row.realm,row.character)), None)
                    client=ProductionClient(id=observer['id'] if observer else uuid4().hex, remote_device=device["id"], role=row.role, character=row.character,
                        realm=row.realm, role_chosen_at=row.role_chosen_at, account=row.account or uuid4().hex, game_root=str(w.root/"remote-games"/device["id"]))
                    client.game_root=str(w.root/"remote-games"/device["id"]/client.id)
                    clients.append(client)
            try:
                if clients:
                    w.production.save_clients(ClientRoster(clients=clients))
                    device["clients"]=[c.id for c in clients if c.remote_device==device["id"]]
                    if retired:
                        archive = w.production.directory / "retired_clients.json"
                        history = json.loads(archive.read_text("utf-8")) if archive.exists() else []
                        atomic_json(archive, history + retired)
            except ValueError as error:
                notes.append(str(error))
            if value.automatic_observers:
                observers = [ObservationClient.model_validate(r) for r in device.get('observers', [])]
                production = w.production.clients()
                for row in rows:
                    if not realm_matches(profile, row.realm) or row.market != profile.market:
                        continue
                    if any((c.realm, c.character) == (row.realm, row.character) and c.remote_device==device['id'] for c in production):
                        continue
                    if not any((c.realm, c.character) == (row.realm, row.character) for c in observers):
                        if len(observers) >= 30:
                            notes.append('Достигнут предел 30 персонажей для сбора на этом Worker')
                            continue
                        observer = ObservationClient(remote_device=device['id'], account=row.account,
                            character=row.character, realm=row.realm, game_root=str(w.root/'remote-games'/device['id']))
                        observer.game_root = str(w.root/'remote-games'/device['id']/observer.id)
                        observers.append(observer)
                device['observers'] = [c.model_dump(mode='json') for c in observers]
                device['clients'] = list(dict.fromkeys(device['clients']+[c.id for c in observers]))
            device['clients'] = list(dict.fromkeys(device['clients']+[c['id'] for c in device.get('observers', [])]))
            device.update(discovered=[r.model_dump(mode="json") for r in value.characters],notes=notes)
            atomic_json(self.root/(device["id"]+".json"),device)
            return self.manifest(device, extended=True, automatic=value.automatic_observers)

    def state_path(self, device, client_id):
        if client_id not in device["clients"]:
            raise HTTPException(403, "Этот персонаж не назначен рабочему клиенту")
        return self.root / "exchanges" / device["id"] / (client_id+".json")

    def exchange(self, device, request: ExchangeRequest):
        path = self.state_path(device, request.client_id)
        w = self.catalog.workspace(device["profile_id"])
        client = next((c for c in self.clients(device, w) if c.id == request.client_id), None)
        if client is None:
            raise HTTPException(403, 'Этот персонаж не принадлежит Worker')
        saved = request.saved
        unresolved_accounting = []
        if saved is not None:
            if set(saved)-SAVED_FIELDS:
                raise ValueError("Лишние поля в сохранении рабочего клиента")
            binding = saved.get("binding") or {}
            if binding and (binding.get("realm"), binding.get("character"), binding.get("market")) != (client.realm, client.character, w.market_key):
                raise ValueError("Сохранение другого персонажа или рынка")
            # SavedVariables (and its financial outbox) belong to an account,
            # not just the last logged-in character. Accept retained events of
            # that account's other registered characters on this Worker only.
            accounting_owners = {(c.realm, c.character, w.market_key) for c in self.clients(device, w)
                                 if c.account == client.account}
            for event in (saved.get("accounting") or {}).get("outbox", (saved.get("accounting") or {}).get("events", [])) or []:
                if (event.get("workspace_id") == w.identity and event.get("market") == w.market_key
                    and isinstance(event.get("realm"), str) and not event["realm"].strip()):
                    # Old clients recorded account-wide events before realm APIs
                    # were available. Preserve the original, never assign it to
                    # today's character, and do not block unrelated scan delivery.
                    LedgerEvent.model_validate({**event, "realm": "unresolved",
                        "inputs": event.get("inputs") or [], "outputs": event.get("outputs") or []})
                    unresolved_accounting.append(event)
                    continue
                if event.get("workspace_id") == w.identity and (event.get("realm"), event.get("character"), event.get("market")) not in accounting_owners:
                    raise ValueError("Финансовый отчёт другого персонажа или рынка")
            if any(s.get("market") not in {w.market_key, w.market_key.replace(".alliance.", ".neutral.").replace(".horde.", ".neutral.")} for s in saved.get("market_scans") or []):
                raise ValueError("Снимок другого рынка")
        digest = hashlib.sha256(dumps(request.model_dump(mode="json")).encode()).hexdigest()
        with self.lock:
            old = json.loads(path.read_text("utf-8")) if path.exists() else None
            if old and request.sequence == old["sequence"]:
                if digest != old["digest"]:
                    raise HTTPException(409, "Номер обмена повторён с другим содержимым")
                if old["state"] != "interrupted":
                    return self.public_state(old, w)
            elif old:
                if request.sequence < old["sequence"]:
                    raise HTTPException(409, {"code": "STALE_SEQUENCE", "next_sequence": old["sequence"]+1})
                if old["state"] == "queued":
                    raise HTTPException(409, "Предыдущий обмен ещё выполняется")
            state = {"sequence": request.sequence, "digest": digest, "state": "queued", "request": request.model_dump(mode="json")}
            atomic_json(path, state)

            def execute():
                try:
                    # Recheck revocation when a queued operation actually starts.
                    if next(d for d in self.devices() if d["id"] == device["id"])["revoked"]:
                        raise ValueError("Ключ рабочего клиента отозван")
                    addon, source = client.paths()
                    addon.mkdir(parents=True, exist_ok=True)
                    if not (addon / "CennikAddon.toc").exists():
                        atomic_write(addon / "CennikAddon.toc", b"Production.lua\n")
                    if saved is not None:
                        sync_saved = saved
                        if unresolved_accounting:
                            for event in unresolved_accounting:
                                identity = hashlib.sha256(dumps(event).encode()).hexdigest()
                                archive = w.root / "accounting/unresolved" / (identity+".json")
                                if not archive.exists():
                                    atomic_json(archive, {"reason": "realm_unavailable", "original": event,
                                        "received_at": int(time.time()), "delivered_via": client.id})
                            accounting = dict(saved["accounting"])
                            for field in ("outbox", "events"):
                                if isinstance(accounting.get(field), list):
                                    accounting[field] = [e for e in accounting[field] if e not in unresolved_accounting]
                            sync_saved = {**saved, "accounting": accounting}
                        atomic_write(source, ("CennikAddonDB="+lua_literal(sync_saved)+"\n").encode())
                    result, payload = w._synchronize_client(client=client)
                    if unresolved_accounting:
                        report = payload["accounting"]
                        report["acknowledged_event_ids"] = list(dict.fromkeys(report["acknowledged_event_ids"]
                            + [event["event_id"] for event in unresolved_accounting]))
                        report["unresolved_identity_events"] = len(unresolved_accounting)
                        result["accounting_reports"]["unresolved_identity_events"] = len(unresolved_accounting)
                        w.log.warning("Preserved %s accounting events without realm separately; scans continue for %s",
                                      len(unresolved_accounting), client.id)
                    state.update(state="completed", response={"format_version": 1, "sequence": request.sequence,
                        "client_id": client.id, "payload": payload, "summary": {k: result[k] for k in ("acknowledged_scans", "accounting_reports", "bulk_reports", "production_reports")}})
                    with self.lock:
                        fresh = next(d for d in self.devices() if d["id"] == device["id"])
                        fresh["last_seen"] = int(time.time())
                        atomic_json(self.root / (device["id"]+".json"), fresh)
                except Exception as error:
                    state.update(state="failed", error=type(error).__name__, message="Обмен не завершён. Сохранение осталось на рабочем ПК; проверьте серверный журнал")
                    w.log.exception("Remote exchange failed for %s", client.id)
                with self.lock:
                    atomic_json(path, state)
                return {"state": state["state"], "client_id": client.id}
            try:
                job = w.submit("remote_exchange", execute, defer=True)
            except Exception:
                state["state"] = "interrupted"
                atomic_json(path, state)
                raise
            state["job_id"] = job["job_id"]
            # Do not overwrite a response that a fast worker already completed.
            current = json.loads(path.read_text("utf-8"))
            if current["state"] == "queued":
                atomic_json(path, state)
            return self.public_state(state, w)

    def public_state(self, state, workspace=None):
        if workspace is not None and state.get("state") == "completed" and state.get("job_id"):
            jobs = workspace.jobs()
            job = next((j for j in jobs if j["id"] == state["job_id"]), None)
            # The callback writes its receipt just before the workspace commits
            # completion. Do not announce completion while the queue is busy
            # finishing that same job (the next action would otherwise get 409).
            finishing = workspace.future is not None and not workspace.future.done() and not any(j["state"] in {"queued", "running"} for j in jobs)
            if job and (job["state"] in {"queued", "running"} or finishing):
                return {"sequence": state["sequence"], "state": "queued"}
        return {k: state[k] for k in ("sequence", "state", "response", "error", "message") if k in state}

    def status(self, device, client_id):
        with self.lock:
            path = self.state_path(device, client_id)
            return self.public_state(json.loads(path.read_text("utf-8")), self.catalog.workspace(device["profile_id"])) if path.exists() else {"state": "empty", "sequence": 0}

    def recover(self):
        for path in (self.root / "exchanges").glob("*/*.json"):
            state = json.loads(path.read_text("utf-8"))
            if state["state"] == "queued":
                state["state"] = "interrupted"
                atomic_json(path, state)
