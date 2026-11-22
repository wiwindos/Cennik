"""Player suggestions and owner-reviewed auctioneer registry, outside SQL."""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from pathlib import Path
from typing import Literal

from pydantic import Field

from ahledger.util import atomic_json
from cennik.core.models import Model
from cennik.markets import (
    MarketBinding,
    equivalent_realms,
    forever_realm_identity,
    neutral_market,
    valid_market,
)


class AuctioneerProposal(Model):
    locale: str | None = Field(default=None, max_length=10)
    server_id: str = Field(default="", pattern=r"^([a-f0-9]{32})?$")
    market: str = Field(default="", max_length=120)
    npc_id: int = Field(gt=0, le=2147483647, strict=True)
    npc_name: str = Field(min_length=1, max_length=200)
    realm: str = Field(min_length=1, max_length=160)
    map_id: int = Field(gt=0, le=2147483647, strict=True)
    map_name: str = Field(default="", max_length=200)
    x: float = Field(ge=0, le=100, allow_inf_nan=False)
    y: float = Field(ge=0, le=100, allow_inf_nan=False)
    proposed_type: Literal["ordinary", "neutral"]
    observed_at: int = Field(gt=0, strict=True)
    build: str = Field(default="", max_length=80)

    def scope_id(self):
        if self.server_id:
            if not valid_market(self.market) or self.market.split(".")[1] != self.server_id:
                raise ValueError("Неверная привязка аукциониста к серверу")
            return hashlib.sha256(json.dumps([self.server_id, self.realm, self.npc_id, self.map_id], ensure_ascii=False).encode()).hexdigest()
        return hashlib.sha256(json.dumps(["forever.pvp.us", self.realm, self.npc_id, self.map_id], ensure_ascii=False).encode()).hexdigest()


class Review(Model):
    server_id: str = Field(default="", pattern=r"^([a-f0-9]{32})?$")
    market: str = Field(default="", max_length=120)
    action: Literal["approve", "reject", "revoke"]
    auction_type: Literal["ordinary", "neutral"] | None = None
    expected_revision: int = Field(default=0, ge=0, strict=True)
    reason: str = Field(default="", max_length=1000)


class AuctionContext(Model):
    server_id: str = Field(default="", pattern=r"^([a-f0-9]{32})?$")
    scope_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    revision: int = Field(gt=0, strict=True)
    auction_type: Literal["ordinary", "neutral"]
    npc_id: int = Field(gt=0, strict=True)
    realm: str = Field(min_length=1, max_length=160)
    map_id: int = Field(gt=0, strict=True)
    x: float = Field(ge=0, le=100, allow_inf_nan=False)
    y: float = Field(ge=0, le=100, allow_inf_nan=False)
    session_id: str = Field(min_length=1, max_length=150, pattern=r"^[a-zA-Z0-9_.:-]+$")


def validate_snapshot(value):
    if not isinstance(value, dict) or value.get("format_version") != 1 or not isinstance(value.get("entries"), list) or len(value["entries"]) > 10000:
        raise ValueError("Неверный справочник аукционистов")
    seen = set()
    entries = []
    for e in value["entries"]:
        if not isinstance(e, dict) or not isinstance(e.get("data"), dict) or type(e.get("reviewed_at")) is not int:
            raise ValueError("Неверная запись справочника аукционистов")
        p = AuctioneerProposal.model_validate(e["data"])
        if e.get("approved") is not True or e.get("auction_type") not in {"ordinary", "neutral"} or type(e.get("revision")) is not int or e["revision"] <= 0 or e.get("scope_id") != p.scope_id() or e["scope_id"] in seen:
            raise ValueError("Неверное подтверждение аукциониста")
        seen.add(e["scope_id"])
        entries.append({k: e[k] for k in ("scope_id", "revision", "approved", "auction_type", "data", "reviewed_at")})
    result = {"format_version": 1, "version": value.get("version", ""), "entries": entries, "fetched_at": int(time.time())}
    if "market_catalog" in value:
        if not isinstance(value["market_catalog"], list) or len(value["market_catalog"]) > 256:
            raise ValueError("Неверный каталог серверов")
        result["market_catalog"] = [MarketBinding.model_validate(p).model_dump(mode="json") for p in value["market_catalog"]]
    return result


def checked_id(identity):
    if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
        raise ValueError("Неверный идентификатор заявки")
    return identity


class AuctioneerRegistry:
    def __init__(self, root: Path):
        self.root, self.lock = root / "auctioneers", threading.RLock()

    def proposals(self):
        return [json.loads(p.read_text("utf-8")) for p in (self.root / "proposals").glob("*.json")]

    def entry(self, identity):
        path = self.root / "registry" / (checked_id(identity)+".json")
        return json.loads(path.read_text("utf-8")) if path.exists() else None

    @staticmethod
    def group(data):
        identity = forever_realm_identity(data.get("realm"))
        market = data.get("market", "")
        if identity and data.get("server_id") and valid_market(market) and market.split('.')[1] == data['server_id'] and market.split('.')[-1] == identity[0]:
            return data['server_id'], identity, data['npc_id'], data['map_id']
        return AuctioneerProposal.model_validate(data).scope_id()

    def effective_entries(self):
        groups = {}
        orders = {}
        for path in (self.root / "registry").glob("*.json"):
            row = json.loads(path.read_text("utf-8"))
            key = self.group(row['data'])
            order = (row['reviewed_at'], row.get('reviewed_at_ns', path.stat().st_mtime_ns), row['revision'], row['scope_id'])
            if key not in orders or order > orders[key]:
                groups[key] = row
                orders[key] = order
        return list(groups.values())

    def for_proposal(self, data):
        key = self.group(data)
        return next((row for row in self.effective_entries() if self.group(row['data']) == key), None)

    def suggest(self, record_id, data, contributor):
        proposal = AuctioneerProposal.model_validate(data)
        if proposal.observed_at > int(time.time())+300:
            raise ValueError("Время заявки находится в будущем")
        with self.lock:
            path = self.root / "proposals" / (checked_id(record_id)+".json")
            value = {"record_id": record_id, "scope_id": proposal.scope_id(), "data": proposal.model_dump(mode="json"), "contributors": [contributor]}
            if path.exists():
                old = json.loads(path.read_text("utf-8"))
                if old["data"] != value["data"]:
                    raise ValueError("Заявка повторена с другим содержимым")
                value = {**old, "contributors": sorted(set(old["contributors"]+[contributor]))}
            atomic_json(path, value)

    def review(self, record_id, value: Review):
        with self.lock:
            proposal = next((p for p in self.proposals() if p["record_id"] == record_id), None)
            if proposal is None:
                raise ValueError("Заявка не найдена")
            data = AuctioneerProposal.model_validate({**proposal["data"], **({"server_id": value.server_id, "market": value.market} if value.server_id else {})})
            old = self.for_proposal(data.model_dump(mode="json"))
            if old:
                data = data.model_copy(update={"realm": old['data']['realm']})
            scope_id = data.scope_id()
            revision = old["revision"] if old else 0
            if revision != value.expected_revision:
                raise ValueError("Решение уже изменено; обновите страницу")
            action = {"at": int(time.time()), "record_id": record_id, "scope_id": scope_id, **value.model_dump(mode="json")}
            unchanged = bool(old and old['approved'] and value.action == 'approve' and old['auction_type'] == value.auction_type
                             and abs(old['data']['x']-data.x) <= 2 and abs(old['data']['y']-data.y) <= 2)
            next_revision = revision + (value.action != 'reject' and not unchanged)
            if value.action != "reject":
                if value.action == "approve" and value.auction_type is None:
                    raise ValueError("Выберите подтверждённый тип")
                entry = {"scope_id": scope_id, "revision": next_revision, "approved": value.action == "approve",
                    "auction_type": value.auction_type, "data": old['data'] if unchanged else data.model_dump(mode="json"), "reviewed_at": action["at"], "reviewed_at_ns": time.time_ns(),
                    "history": [*(old.get("history", []) if old else []), action]}
                atomic_json(self.root / "registry" / (entry["scope_id"]+".json"), entry)
            atomic_json(self.root / "reviews" / (checked_id(record_id)+".json"), action)
            return {"scope_id": scope_id, "revision": next_revision, "action": value.action}

    def wire(self):
        with self.lock:
            effective = {self.group(e['data']): e for e in self.effective_entries()}
            entries = [json.loads(p.read_text("utf-8")) for p in (self.root / "registry").glob("*.json")]
            entries = [e for e in entries if effective[self.group(e['data'])]['approved']
                       and e['auction_type'] == effective[self.group(e['data'])]['auction_type']]
            public = [{k: v for k, v in e.items() if k not in {"history", "reviewed_at_ns"}} for e in entries if e["approved"]]
            # Locale is observation evidence, not part of the execution registry.
            # Keep the old registry contract readable by already distributed clients.
            for entry in public:
                entry["data"] = {k: v for k, v in entry["data"].items() if k != "locale"}
            public.sort(key=lambda e: e["scope_id"])
            return {"format_version": 1, "version": hashlib.sha256(json.dumps(public, sort_keys=True).encode()).hexdigest(), "entries": public}

    def overview(self, proposals=None):
        groups = {}
        for p in self.proposals() if proposals is None else proposals:
            path = self.root / "reviews" / (p["record_id"]+".json")
            review = json.loads(path.read_text("utf-8")) if path.exists() else None
            source = self.root / 'proposals' / (p['record_id']+'.json')
            order = (p['data']['observed_at'], source.stat().st_mtime_ns, p['record_id'])
            key = self.group(p['data'])
            group = groups.setdefault(key, {'row': p, 'order': order, 'contributors': set(), 'reviews': [], 'count': 0})
            if order > group['order']:
                group.update(row=p, order=order)
            group['contributors'].update(p['contributors'])
            group['count'] += 1
            if review:
                group['reviews'].append(((review['at'], path.stat().st_mtime_ns), review))
        rows = []
        for group in groups.values():
            p = group['row']
            rows.append({**p, 'contributors': sorted(group['contributors']), 'observation_count': group['count'],
                         'registry': self.for_proposal(p['data']),
                         'review': max(group['reviews'], key=lambda r: r[0])[1] if group['reviews'] else None})
        return {"items": sorted(rows, key=lambda p: p["data"]["observed_at"], reverse=True), **self.wire()}


def scan_scope(scan: dict, snapshot: dict) -> str:
    """No inference from character faction or a player's proposed type."""
    context = scan.get("auction_context") or {}
    if not isinstance(context, dict) or snapshot.get("format_version") != 1:
        return "unknown"
    try:
        context = AuctionContext.model_validate(context).model_dump(mode="json")
        at = snapshot.get("fetched_at", 0)
        if not 0 <= time.time()-at <= 86400:
            return "unknown"
        entry = next((e for e in snapshot.get("entries", []) if e["scope_id"] == context.get("scope_id")), None)
        if not entry or not entry.get("approved") or entry["revision"] != context.get("revision"):
            return "unknown"
        d = entry["data"]
        if context.get("server_id", "") != d.get("server_id", ""):
            return "unknown"
        binding = next((b for b in snapshot.get('market_catalog', []) if b.get('id') == context.get('server_id')), None)
        if any(context.get(k) != d[k] for k in ("npc_id", "map_id")) or not equivalent_realms(context.get('realm'), d['realm'], binding):
            return "unknown"
        x, y = float(context["x"]), float(context["y"])
        if not math.isfinite(x) or not math.isfinite(y) or not 0 <= x <= 100 or not 0 <= y <= 100:
            return "unknown"
        if abs(x-d["x"])>2 or abs(y-d["y"])>2:
            return "unknown"
        if context.get("auction_type") != entry["auction_type"] or scan.get("realm") != context.get('realm'):
            return "unknown"
        base_market = d.get("market") or "forever.pvp.alliance.us"
        market = neutral_market(base_market) if entry["auction_type"] == "neutral" else base_market
        if scan.get("market") != market:
            return "unknown"
        return entry["auction_type"]
    except (KeyError, TypeError, ValueError, OverflowError):
        return "unknown"
