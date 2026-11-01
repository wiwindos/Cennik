"""Public intake -> durable forwarding queue -> isolated market workspaces."""
from __future__ import annotations

import json
import threading
import time

import zstandard as zstd

from ahledger.util import atomic_json
from cennik.collector.auctioneers import AuctioneerProposal, Review, validate_snapshot
from cennik.collector.client import CollectorClient
from cennik.collector.protocol import Connection, Observation
from cennik.markets import MarketBinding


class ServerCollectors:
    def __init__(self, catalog, hub):
        self.catalog, self.hub = catalog, hub
        self.root = catalog.root / "forwarding"
        self.stop = threading.Event()
        self.hub.catalog_provider = self.market_catalog
        self.hub.on_accept = self.enqueue
        self.thread = threading.Thread(target=self.run, name="collector-forwarding", daemon=True)

    def market_catalog(self, member_id=None):
        profile_id = next((m.get("profile_id") for m in self.hub.members() if m["id"] == member_id), None) if member_id else None
        return [MarketBinding.model_validate({k: p.public()[k] for k in MarketBinding.model_fields}).model_dump(mode="json") for p in self.catalog.profiles() if not profile_id or p.id == profile_id]

    def registry(self):
        return validate_snapshot({**self.hub.auctioneers.wire(), "market_catalog": self.market_catalog()})

    def enqueue(self, value, receipt, contributor):
        identity = value.identity()
        path = self.root / "pending" / (identity+".json")
        if not (self.root / "done" / path.name).exists() and not path.exists():
            atomic_json(path, {"record_id": identity, "raw_path": receipt["raw_path"], "scope": receipt.get("scope"), "contributor": contributor})

    def adapter(self, workspace, profile):
        return HubAdapter(workspace, profile, self)

    def run(self):
        while not self.stop.wait(2):
            for path in sorted((self.root / "pending").glob("*.json"))[:20]:
                if self.stop.is_set():
                    break
                try:
                    receipt = json.loads(path.read_text("utf-8"))
                    raw_path = (self.hub.root / receipt["raw_path"]).resolve()
                    if not raw_path.is_relative_to(self.hub.root):
                        raise ValueError("Неверный путь архива")
                    value = Observation.model_validate_json(zstd.ZstdDecompressor().decompress(raw_path.read_bytes(), max_output_size=64_000_000))
                    data = value.data
                    identity = (data.get("auction_context") or {}).get("server_id") if value.kind == "scan" else data.get("server_id")
                    profile = next((p for p in self.catalog.profiles() if p.id == identity), None)
                    unbound = profile is None or (
                        value.kind != "scan" and (
                            data.get("market") != profile.market
                            or data.get("realm") not in profile.realm_names
                        )
                    )
                    if unbound or value.kind == "scan" and receipt.get("scope") == "unknown":
                        result = {"state": "archived_unassigned"}
                    else:
                        assert profile is not None
                        w = self.catalog.workspace(identity)
                        # Schedule on the same queue as analysis and private exchange.
                        complete = threading.Event()
                        failure: list[str] = []
                        def apply(w=w, value=value, profile=profile, failure=failure, complete=complete):
                            try:
                                if value.kind == "scan":
                                    w.collectors.registry()
                                    w.import_scans([{"format_version": 1, "scan_id": value.identity(), **value.data}])
                                elif value.kind == "npc" and not value.data.get("blocked"):
                                    w.npc_references.ingest({str(value.data["item_id"]): {**value.data,
                                        "scope": value.data.get("realm", "")+"/"+profile.faction}})
                                # Recipes and transform observations remain raw evidence;
                                # they never automatically replace reviewed reference rules.
                                return {"forwarded": value.identity()}
                            except Exception as error:
                                failure.append(type(error).__name__)
                                raise
                            finally:
                                complete.set()
                        w.submit("collector_ingest", apply, defer=True)
                        while not complete.wait(.5):
                            if self.stop.is_set():
                                return
                        if failure:
                            continue
                        result = {"state": "forwarded", "profile_id": profile.id}
                    atomic_json(self.root / "done" / path.name, {**receipt, **result})
                    path.unlink()
                except Exception:
                    # Keep the durable marker; a restart retries without losing raw data.
                    continue


class HubAdapter(CollectorClient):
    def __init__(self, workspace, profile, server):
        super().__init__(workspace.root)
        self.workspace, self.profile, self.server = workspace, profile, server
        identity_path = self.root / "internal_member.json"
        if identity_path.exists():
            self.member_id = json.loads(identity_path.read_text("utf-8"))["id"]
        else:
            member = server.hub.invite("Рабочие клиенты · "+profile.label)
            self.member_id = member["id"]
            atomic_json(identity_path, {"id": self.member_id})

    def connection(self):
        return Connection(enabled=True, url="https://managed.invalid", token="server-managed")

    def configure(self, value):
        raise ValueError("Приёмник управляется сервером. Здесь подключение не требуется")

    def public_connection(self):
        return {"enabled": True, "url": "", "token_set": False, "admin_token_set": True, "managed": True}

    def registry(self):
        snapshot = self.server.registry()
        atomic_json(self.root / "auctioneer_registry.json", snapshot)
        return snapshot

    def capture(self, saved, label="Рабочий клиент"):
        # Private full-depth scans are already imported by the private pipeline,
        # retaining ownership flags; do not replace them with sanitized copies.
        public = {**saved, "market_scans": []}
        result = super().capture(public, label)
        self.flush()
        return result

    def flush(self, limit=20):
        sent = 0
        for path in sorted((self.root / "outbox").glob("*.json"))[:limit]:
            value = Observation.model_validate_json(path.read_text("utf-8"))
            ack = self.server.hub.accept(self.member_id, value)
            atomic_json(self.root / "receipts" / path.name, ack)
            path.unlink()
            sent += 1
        self.next_attempt = time.monotonic()+30
        self.registry()
        return {"sent": sent, "registry_changed": True, "pending": len(list((self.root / "outbox").glob("*.json")))}

    def admin(self, method="GET", path="/v1/admin/members", body=None):
        hub = self.server.hub
        if path == "/v1/admin/members":
            return hub.invite(body["label"], profile_id=self.profile.id) if method == "POST" else hub.overview()
        if path.startswith("/v1/admin/members/") and method == "DELETE":
            identity = path.rsplit('/', 1)[-1]
            with hub.lock:
                member = next((m for m in hub.members() if m["id"] == identity), None)
                if member is None:
                    raise ValueError("Участник не найден")
                member["revoked"] = True
                hub.save(member)
            return {"revoked": True}
        if path == "/v1/admin/auctioneers":
            profile = self.server.catalog.profile(self.profile.id)
            result = hub.auctioneers.overview()
            rows = []
            for p in result["items"]:
                if p["data"]["realm"] not in profile.realm_names or p["data"].get("server_id") not in {None, "", profile.id}:
                    continue
                bound = AuctioneerProposal.model_validate({**p["data"], "server_id": profile.id, "market": profile.market})
                rows.append({**p, "registry": hub.auctioneers.entry(bound.scope_id())})
            return {**result, "items": rows, **self.registry()}
        if path.endswith("/review"):
            identity = path.split('/')[-2]
            profile = self.server.catalog.profile(self.profile.id)
            proposal = next((p for p in hub.auctioneers.proposals() if p["record_id"] == identity), None)
            if not proposal or proposal["data"]["realm"] not in profile.realm_names or proposal["data"].get("server_id") not in {None, "", profile.id}:
                raise ValueError("Аукционист не относится к выбранному серверу")
            return hub.auctioneers.review(identity, Review.model_validate({**body, "server_id": profile.id, "market": profile.market}))
        raise ValueError("Неизвестная административная операция")
