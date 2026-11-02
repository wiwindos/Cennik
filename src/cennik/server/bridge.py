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
    role: str = Field(pattern=r"^(auctioneer|crafter|unassigned)$")
    faction: str = Field(pattern=r"^(alliance|horde)$")
    professions: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("professions", mode="before")
    @classmethod
    def empty_lua_professions(cls, value):
        # Lua's empty table has no array/object distinction. Accept old Workers too.
        return [] if isinstance(value, dict) and not value else value


class DiscoverCharacters(Model):
    characters: list[CharacterDiscovery] = Field(max_length=30)


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
                if spec.realm not in profile.realm_names:
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

    def manifest(self, device, *, extended=False):
        device = next(d for d in self.devices() if d["id"] == device["id"])
        profile = self.catalog.profile(device["profile_id"])
        clients = self.catalog.workspace(profile.id).production.clients()
        result = {"format_version": 1, "device_id": device["id"], "profile": profile.public(), "clients": [
            {k: getattr(c, k) for k in ("id", "role", "realm", "character", "account")} for c in clients if c.id in device["clients"]]}
        if extended:
            result["notes"] = device.get("notes", [])
        return result

    def discover(self, device, value: DiscoverCharacters):
        with self.lock:
            device=next(d for d in self.devices() if d["id"]==device["id"])
            profile=self.catalog.profile(device["profile_id"])
            rows=[r for r in value.characters if r.faction==profile.faction]
            if not profile.realm_names and len({r.realm for r in rows})==1:
                profile=self.catalog.save(profile.model_copy(update={"realm_names":[rows[0].realm]}))
            w=self.catalog.workspace(profile.id)
            clients=w.production.clients()
            notes=[]
            for row in rows:
                if row.realm not in profile.realm_names:
                    notes.append("Сервер персонажа не совпадает с выбранным рынком")
                    continue
                if row.role=="unassigned":
                    continue
                existing=next((c for c in clients if (c.realm,c.character)==(row.realm,row.character)),None)
                if existing and existing.remote_device!=device["id"]:
                    notes.append("Персонаж уже назначен другому рабочему ПК")
                    continue
                if existing:
                    clients=[c.model_copy(update={"role":row.role}) if c.id==existing.id else c for c in clients]
                else:
                    client=ProductionClient(remote_device=device["id"], role=row.role, character=row.character,
                        realm=row.realm, account=row.account or uuid4().hex, game_root=str(w.root/"remote-games"/device["id"]))
                    client.game_root=str(w.root/"remote-games"/device["id"]/client.id)
                    clients.append(client)
            try:
                if clients:
                    w.production.save_clients(ClientRoster(clients=clients))
                    device["clients"]=[c.id for c in clients if c.remote_device==device["id"]]
            except ValueError as error:
                notes.append(str(error))
            device.update(discovered=[r.model_dump(mode="json") for r in value.characters],notes=notes)
            atomic_json(self.root/(device["id"]+".json"),device)
            return self.manifest(device, extended=True)

    def state_path(self, device, client_id):
        if client_id not in device["clients"]:
            raise HTTPException(403, "Этот персонаж не назначен рабочему клиенту")
        return self.root / "exchanges" / device["id"] / (client_id+".json")

    def exchange(self, device, request: ExchangeRequest):
        path = self.state_path(device, request.client_id)
        w = self.catalog.workspace(device["profile_id"])
        client = next(c for c in w.production.clients() if c.id == request.client_id)
        saved = request.saved
        if saved is not None:
            if set(saved)-SAVED_FIELDS:
                raise ValueError("Лишние поля в сохранении рабочего клиента")
            binding = saved.get("binding") or {}
            if binding and (binding.get("realm"), binding.get("character"), binding.get("market")) != (client.realm, client.character, w.market_key):
                raise ValueError("Сохранение другого персонажа или рынка")
            for event in (saved.get("accounting") or {}).get("outbox", (saved.get("accounting") or {}).get("events", [])) or []:
                if event.get("workspace_id") == w.identity and (event.get("realm"), event.get("character"), event.get("market")) != (client.realm, client.character, w.market_key):
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
                        atomic_write(source, ("CennikAddonDB="+lua_literal(saved)+"\n").encode())
                    result, payload = w._synchronize_client(client=client)
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
