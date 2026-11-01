"""Independent file archive, per-contributor upload tokens and owner-only metrics."""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from pathlib import Path
from uuid import uuid4

import zstandard as zstd
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from ahledger.util import atomic_json
from cennik.core.models import Model
from cennik.workspace.data import atomic_bytes

from .auctioneers import AuctioneerRegistry, Review, scan_scope
from .protocol import Observation, canonical


class Invitation(Model):
    label: str = Field(min_length=1, max_length=80)


class Hub:
    def __init__(self, root: Path):
        self.root, self.lock = root.resolve(), threading.RLock()
        self.rates: dict[str, tuple[float, int]] = {}
        self.auctioneers = AuctioneerRegistry(self.root)
        self.catalog_provider = None
        self.on_accept = None

    def members(self):
        return [json.loads(p.read_text("utf-8")) for p in (self.root / "members").glob("*.json")]

    def save(self, member):
        atomic_json(self.root / "members" / (member["id"]+".json"), member)

    def invite(self, label, *, profile_id=""):
        token = secrets.token_urlsafe(36)
        member = {"id": uuid4().hex, "label": label, "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                  "created_at": int(time.time()), "revoked": False, "last_seen": None, "counts": {}, "profile_id": profile_id}
        with self.lock:
            self.save(member)
        return {"id": member["id"], "label": label, "token": token}

    def authenticate(self, token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        for m in self.members():
            if not m["revoked"] and secrets.compare_digest(digest, m["token_hash"]):
                return m["id"]
        raise HTTPException(401, "Ключ сборщика неверен или отозван")

    def accept(self, identity, value):
        with self.lock:
            member = next(m for m in self.members() if m["id"] == identity)
            claimed = (value.data.get("auction_context") or {}).get("server_id") if value.kind == "scan" else value.data.get("server_id")
            if member.get("profile_id") and claimed and claimed != member["profile_id"]:
                raise HTTPException(422, "Ключ участника привязан к другому серверу")
            key = value.identity()
            directory = self.root / "receipts" / identity
            receipt = directory / (key+".json")
            duplicate = receipt.exists()
            scope = scan_scope(value.data, {**self.auctioneers.wire(), "fetched_at": int(time.time())}) if value.kind == "scan" else None
            if not duplicate:
                folder = "neutral_auctions" if scope == "neutral" else "unclassified_auctions" if scope == "unknown" else value.kind
                server_id = (value.data.get("auction_context") or {}).get("server_id") if value.kind == "scan" else value.data.get("server_id")
                raw_root = self.root / "raw" / server_id if server_id else self.root / "raw"
                raw = raw_root / folder / (key+".json.zst")
                if not raw.exists():
                    atomic_bytes(raw, zstd.ZstdCompressor().compress(canonical(value.model_dump(mode="json"))))
                counts = value.counts()
                if scope in {"neutral", "unknown"}:
                    counts["neutral_scans" if scope == "neutral" else "unclassified_scans"] = 1
                    counts["full_scans"] = counts["partial_scans"] = 0
                atomic_json(receipt, {"record_id": key, "counts": counts, "observed_at": value.data.get("observed_at", value.data.get("at")),
                    "raw_path": str(raw.relative_to(self.root)), "scope": scope})
            if value.kind == "auctioneer_proposal":
                self.auctioneers.suggest(key, value.data, identity)
            # Derive from durable receipts: a crash after raw write or before ACK
            # cannot double counters or lose a successfully stored observation.
            paths = list(directory.glob("*.json"))
            counts = member["counts"]
            if not duplicate and counts.get("records", 0) == len(paths)-1:
                for name, n in json.loads(receipt.read_text("utf-8"))["counts"].items():
                    counts[name] = counts.get(name, 0)+n
            elif counts.get("records", 0) != len(paths):
                # Only crash recovery needs to read every durable receipt.
                counts = {}
                for path in paths:
                    for name, n in json.loads(path.read_text("utf-8"))["counts"].items():
                        counts[name] = counts.get(name, 0)+n
            member["counts"], member["last_seen"] = counts, int(time.time())
            member["last_observed_at"] = max(member.get("last_observed_at", 0), value.data.get("observed_at", value.data.get("at")))
            if value.kind == "diagnostic" and value.data["at"] >= member.get("diagnostic", {}).get("at", 0):
                member["diagnostic"] = value.data
            self.save(member)
            if self.on_accept:
                stored = json.loads(receipt.read_text("utf-8"))
                if stored.get("raw_path"):
                    self.on_accept(value, stored, identity)
            return {"record_id": key, "duplicate": duplicate, "stored": True, "received_at": int(time.time())}

    def overview(self):
        with self.lock:
            rows = [{k: v for k, v in m.items() if k != "token_hash"} for m in self.members()]
            for row in rows:
                row["application_online"] = bool(row["last_seen"] and time.time()-row["last_seen"] < 120)
            return {"members": rows, "raw_records": sum(1 for _ in (self.root / "raw").rglob("*.json.zst")),
                    "note": "На связи — приложение, не доказательство нового скана. Строки — наблюдения, не суммарный объём рынка."}


def create_hub(root: Path, admin_token: str):
    if len(admin_token) < 32:
        raise ValueError("Нужен отдельный административный ключ минимум 32 символа")
    hub = Hub(root)
    app = FastAPI(title="Cennik collector archive", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.hub = hub

    def owner(x_admin_key: str = Header(default="")):
        if not secrets.compare_digest(x_admin_key.encode(), admin_token.encode()):
            raise HTTPException(401, "Нужен административный ключ")

    def contributor(x_collector_key: str = Header(default="")):
        return hub.authenticate(x_collector_key)

    @app.middleware("http")
    async def limits(request: Request, call_next):
        if request.url.path in {"/v1/observations", "/v1/heartbeat"}:
            try:
                identity = hub.authenticate(request.headers.get("X-Collector-Key", ""))
            except HTTPException as error:
                return JSONResponse({"detail": error.detail}, status_code=error.status_code)
            with hub.lock:
                start, count = hub.rates.get(identity, (time.monotonic(), 0))
                if time.monotonic()-start >= 60:
                    start, count = time.monotonic(), 0
                if count >= 300:
                    return JSONResponse({"detail": "Лимит запросов участника; повторите позже"}, status_code=429, headers={"Retry-After": "60"})
                hub.rates[identity] = start, count+1
        # Includes chunked requests; do not trust Content-Length alone.
        if request.method == "POST":
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 64_000_000:
                    return JSONResponse({"detail": "Пакет превышает 64 МБ"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    def health():
        return {"service": "cennik-collector", "format_version": 1}

    @app.post("/v1/observations")
    def ingest(value: Observation, identity: str = Depends(contributor)):
        return hub.accept(identity, value)

    @app.post("/v1/heartbeat")
    def heartbeat(identity: str = Depends(contributor)):
        with hub.lock:
            m = next(m for m in hub.members() if m["id"] == identity)
            m["last_seen"] = int(time.time())
            hub.save(m)
        return {"application_online": True}

    @app.post("/v1/admin/members", dependencies=[Depends(owner)])
    def invite(value: Invitation):
        return hub.invite(value.label)

    @app.delete("/v1/admin/members/{identity}", dependencies=[Depends(owner)])
    def revoke(identity: str):
        with hub.lock:
            m = next((m for m in hub.members() if m["id"] == identity), None)
            if m is None:
                raise HTTPException(404, "Сборщик не найден")
            m["revoked"] = True
            hub.save(m)
        return {"revoked": True}

    @app.get("/v1/admin/members", dependencies=[Depends(owner)])
    def members():
        return hub.overview()

    @app.get("/v1/auctioneers/registry")
    def auctioneer_registry(identity: str = Depends(contributor)):
        result = hub.auctioneers.wire()
        if hub.catalog_provider:
            result["market_catalog"] = hub.catalog_provider(identity)
        return result

    @app.get("/v1/admin/auctioneers", dependencies=[Depends(owner)])
    def auctioneers():
        return hub.auctioneers.overview()

    @app.post("/v1/admin/auctioneers/{record_id}/review", dependencies=[Depends(owner)])
    def review_auctioneer(record_id: str, value: Review):
        try:
            return hub.auctioneers.review(record_id, value)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    return app
