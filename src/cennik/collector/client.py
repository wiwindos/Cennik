"""Durable opt-in delivery. Never forwards an entire SavedVariables or ledger."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ahledger.util import atomic_json

from .auctioneers import Review, validate_snapshot
from .protocol import ALLOWED, Connection, Observation

ERRORS = {
    "INVALID_SAVED_OBSERVATION": "Сохранённое наблюдение имеет неверный формат; данные оставлены для диагностики",
    "SERVER_REJECTED_OBSERVATION": "Сервер отклонил формат наблюдения; исходник сохранён в отклонённых",
    "KEY_REJECTED": "Ключ сборщика неверен или отозван; проверьте подключение",
    "RATE_LIMITED": "Сервер ограничил частоту запросов; отправка повторится позже",
    "SERVER_UNAVAILABLE": "Сервер временно недоступен; очередь сохранена, повторим позже",
    "NETWORK_UNREACHABLE": "Не удалось соединиться с сервером; проверьте интернет и адрес",
    "UPLOAD_TIMEOUT": "Сервер не ответил вовремя; очередь сохранена, повторим позже",
    "INVALID_ACK": "Сервер не подтвердил именно этот пакет; исходник остался в очереди",
    "DELIVERY_FAILED": "Сервер не принял запрос; проверьте подключение и версию приёмника",
}


class CollectorClient:
    def __init__(self, root: Path, transport=None):
        self.root = root / "collectors"
        self.lock = threading.RLock()
        self.transport = transport
        self.next_attempt = 0.0
        self.failures = 0

    def read(self, name, default=None):
        path = self.root / name
        return json.loads(path.read_text("utf-8")) if path.exists() else default

    def connection(self):
        return Connection.model_validate(self.read("connection.json", {}))

    def configure(self, value: Connection):
        if value.url:
            parsed = urlparse(value.url)
            if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path.rstrip('/') not in {"", "/collect"}:
                raise ValueError("Нужен адрес сервера без пути, пароля и параметров")
            if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}):
                raise ValueError("Удалённый сервер должен использовать HTTPS")
        old = self.connection()
        same_url = value.url.rstrip('/') == old.url
        value = value.model_copy(update={"url": value.url.rstrip('/'), "token": value.token or (old.token if same_url else ''),
                                        "admin_token": value.admin_token or (old.admin_token if same_url else '')})
        if value.enabled and (not value.url or not value.token):
            raise ValueError("Для отправки нужны адрес и личный ключ сборщика")
        with self.lock:
            atomic_json(self.root / "connection.json", value.model_dump(mode="json"))
            if not same_url:
                atomic_json(self.root / "auctioneer_registry.json", {"format_version": 1, "entries": [], "version": "", "fetched_at": 0})
            self.next_attempt = 0
        return self.public_connection()

    def public_connection(self):
        c = self.connection()
        return {"enabled": c.enabled, "url": c.url, "token_set": bool(c.token), "admin_token_set": bool(c.admin_token)}

    def queue(self, kind, data):
        value = Observation(kind=kind, data={k: v for k, v in data.items() if k in ALLOWED[kind]})
        key = value.identity()
        if any((self.root / directory / (key+".json")).exists() for directory in ("outbox", "receipts", "rejected")):
            return False
        atomic_json(self.root / "outbox" / (key+".json"), value.model_dump(mode="json"))
        return True

    @staticmethod
    def stock(lines):
        return [{k: line[k] for k in ("item_id", "quantity", "variant") if k in line} for line in lines or []]

    @staticmethod
    def scan_data(scan):
        data = {k: v for k, v in scan.items() if k in ALLOWED["scan"]}
        if data.get("auction_context"):
            context = dict(data["auction_context"])
            context["session_id"] = hashlib.sha256(str(context.get("session_id", "")).encode()).hexdigest()
            data["auction_context"] = context
        data["data"] = ";".join(":".join(row.split(":")[:6]) for row in scan.get("data", "").split(";") if row)
        data["variants"] = [{k: v for k, v in variant.items() if k in {"link", "name", "divisible", "vendor_sell"}} for variant in scan.get("variants") or []]
        data["error"] = "SCAN_PARTIAL" if data.get("error") else None
        return data

    def capture(self, saved, label="Локальный сборщик"):
        with self.lock:
            status = saved.get("collector_status") or {}
            device = str(status.get("collector_id") or saved.get("client_id") or saved.get("device_id") or "local")
            key = hashlib.sha256(device.encode()).hexdigest()[:32]
            current = status.get("current") or {}
            binding = saved.get("server_binding") or {}
            public_scope = {"server_id": binding["id"], "market": binding["market"], "realm": (saved.get("binding") or {}).get("realm", "")} if binding.get("id") and binding.get("market") else {}
            # Only codes and numeric counters leave the PC; no stack traces,
            # credentials, file paths, character names, task IDs or money ledger.
            diagnostics = {"at": int(current.get("at") or time.time()),
                "code": str(current.get("code") or "NO_SAVED_STATUS")[:80],
                "level": str(current.get("level") or "wait")[:20],
                "scans": int(status.get("scans") or 0), "rows": int(status.get("rows") or 0),
                "queue": len(saved.get("market_scans") or []),
                "error_counts": {k: int(v.get("count", 0)) for k, v in (status.get("errors") or {}).items() if k.isupper() and len(k)<80}}
            for name in ("addon_version", "build"):
                if status.get(name) is not None:
                    diagnostics[name] = str(status[name])[:120]
            atomic_json(self.root / "local" / (key+".json"), {"id": key, "label": label, "diagnostic": diagnostics,
                "saved_at": int(time.time()), "last_success": status.get("last_success") or {}})
            if not self.connection().enabled:
                return False
            try:
                ack = []
                for proposal in (saved.get("auctioneer_proposals") or {}).values():
                    self.queue("auctioneer_proposal", proposal)
                self.queue("diagnostic", {**diagnostics, **public_scope})
                for scan in saved.get("market_scans") or []:
                    self.queue("scan", self.scan_data(scan))
                for item, raw in (saved.get("collector_npc", saved.get("npc_quotes")) or {}).items():
                    if raw.get("scope") and not raw["scope"].lower().endswith(('/alliance', '/horde')):
                        continue
                    data = {k: v for k, v in raw.items() if k in ALLOWED["npc"]}
                    data["item_id"] = int(raw.get("item_id") or str(item).rsplit(":", 1)[-1])
                    stamp = data.get("observed_at")
                    if isinstance(stamp, str):
                        data["observed_at"] = int(datetime.fromisoformat(stamp).timestamp())
                    self.queue("npc", {**data, **public_scope})
                    if raw.get("observation_id"):
                        ack.append(raw["observation_id"])
                recipes = saved.get("collector_recipes") or {}
                for raw in (recipes.get("recipes") or {}).values():
                    data = {**raw, "observed_at": raw.get("observed_at", recipes.get("observed_at")), "coverage": recipes.get("coverage")}
                    data["inputs"] = self.stock(raw.get("inputs"))
                    self.queue("recipe", {**data, **public_scope})
                for event in saved.get("collector_transforms") or []:
                    if event.get("kind") in {"craft", "disenchant"}:
                        self.queue(event["kind"], {**public_scope, "observed_at": event["observed_at"], "spell_id": event.get("spell_id"),
                            "inputs": self.stock(event.get("inputs")), "outputs": self.stock(event.get("outputs"))})
                        if event.get("observation_id"):
                            ack.append(event["observation_id"])
                # These acknowledgements prove a durable local outbox, not remote
                # delivery. Each snapshot keeps its own explicit IDs.
                atomic_json(self.root / "ack" / (key+".json"), ack)
                return True
            except (ValueError, TypeError, KeyError):
                self.failed("INVALID_SAVED_OBSERVATION")
                return False

    def failed(self, code):
        previous = self.read("delivery.json", {})
        atomic_json(self.root / "delivery.json", {**previous, "error": code, "checked_at": int(time.time())})

    def due(self):
        c = self.connection()
        return c.enabled and time.monotonic() >= self.next_attempt

    def flush(self, limit=20):
        with self.lock:
            c = self.connection()
            if not c.enabled:
                return {"sent": 0}
            sent = 0
            changed = False
            deadline = time.monotonic()+10
            try:
                with httpx.Client(base_url=c.url, headers={"X-Collector-Key": c.token},
                                  timeout=httpx.Timeout(60, connect=10, write=180), follow_redirects=False, trust_env=False, transport=self.transport) as http:
                    http.post("/v1/heartbeat").raise_for_status()
                    registry_response = http.get("/v1/auctioneers/registry")
                    registry_response.raise_for_status()
                    registry = validate_snapshot(registry_response.json())
                    changed = registry != self.registry()
                    atomic_json(self.root / "auctioneer_registry.json", registry)
                    for path in sorted((self.root / "outbox").glob("*.json"))[:limit]:
                        if time.monotonic()>deadline:
                            break
                        response = http.post("/v1/observations", content=path.read_bytes(), headers={"Content-Type": "application/json"})
                        if response.status_code == 422:
                            value = json.loads(path.read_text("utf-8"))
                            atomic_json(self.root / "rejected" / path.name, value)
                            path.unlink()
                            self.failed("SERVER_REJECTED_OBSERVATION")
                            continue
                        response.raise_for_status()
                        ack = response.json()
                        if ack.get("record_id") != path.stem or ack.get("stored") is not True:
                            raise ValueError("Invalid server acknowledgement")
                        atomic_json(self.root / "receipts" / path.name, ack)
                        path.unlink()
                        sent += 1
                        atomic_json(self.root / "delivery.json", {"at": ack["received_at"], "error": ""})
                self.failures = 0
                self.next_attempt = time.monotonic()+(5 if any((self.root / "outbox").glob("*.json")) else 30)
            except (httpx.HTTPError, ValueError) as error:
                self.failures += 1
                self.next_attempt = time.monotonic()+min(900, 30*2**min(self.failures, 5))
                code = "DELIVERY_FAILED"
                if isinstance(error, httpx.HTTPStatusError):
                    status = error.response.status_code
                    code = "KEY_REJECTED" if status in {401, 403} else "RATE_LIMITED" if status == 429 else "SERVER_UNAVAILABLE" if status >= 500 else code
                elif isinstance(error, httpx.TimeoutException):
                    code = "UPLOAD_TIMEOUT"
                elif isinstance(error, httpx.ConnectError):
                    code = "NETWORK_UNREACHABLE"
                elif isinstance(error, ValueError):
                    code = "INVALID_ACK"
                self.failed(code)
            return {"sent": sent, "registry_changed": changed, "pending": len(list((self.root / "outbox").glob("*.json")))}

    def registry(self):
        return self.read("auctioneer_registry.json", {"format_version": 1, "version": "", "entries": [], "fetched_at": 0})

    def review_auctioneer(self, identity, value: Review):
        from .auctioneers import checked_id
        result = self.admin("POST", "/v1/admin/auctioneers/"+checked_id(identity)+"/review", value.model_dump(mode="json"))
        registry = validate_snapshot(self.admin(path="/v1/admin/auctioneers"))
        atomic_json(self.root / "auctioneer_registry.json", registry)
        return result

    def delivery(self):
        d = self.read("delivery.json", {})
        enabled = self.connection().enabled
        return {"enabled": enabled, "local_at": max((v.get("saved_at", 0) for v in self.local()), default=0) or None,
            "acknowledged_observation_ids": [identity for p in (self.root / "ack").glob("*.json") for identity in json.loads(p.read_text("utf-8"))],
            "at": d.get("at"), "error_code": d.get("error", ""), "message": ERRORS.get(d["error"], "Ошибка доставки") if enabled and d.get("error") else
            "Отправка выключена" if not enabled else "Есть подтверждение сервера" if d.get("at") else "Ожидаем первую отправку"}

    def local(self):
        return [json.loads(p.read_text("utf-8")) for p in (self.root / "local").glob("*.json")]

    def overview(self):
        return {"connection": self.public_connection(), "local": self.local(), "delivery": self.delivery(),
                "pending": len(list((self.root / "outbox").glob("*.json"))),
                "rejected": len(list((self.root / "rejected").glob("*.json")))}

    def admin(self, method="GET", path="/v1/admin/members", body=None):
        c = self.connection()
        if not c.url or not c.admin_token:
            raise ValueError("Для списка участников нужен адрес сервера и отдельный ключ владельца")
        with httpx.Client(base_url=c.url, headers={"X-Admin-Key": c.admin_token}, timeout=20,
                          follow_redirects=False, trust_env=False, transport=self.transport) as http:
            response = http.request(method, path, json=body)
            if response.is_error:
                raise ValueError("Сервер не подтвердил административный запрос: " + str(response.status_code))
            return response.json()

    def invite(self, label):
        return self.admin("POST", body={"label": label})

    def revoke(self, identity):
        if len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("Неверный ID участника")
        return self.admin("DELETE", "/v1/admin/members/"+identity)
