"""Client-observed NPC reference prices, kept outside the working DB schema."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import AwareDatetime, Field

from cennik.core.models import Item, Model
from cennik.engines.npc_catalog import NpcPriceDocument

from .data import atomic_bytes


class NpcQuote(Model):
    batch_price_copper: int = Field(gt=0, le=2**53-1)
    batch_quantity: int = Field(gt=0, le=1000000)
    observed_at: AwareDatetime
    scope: str = Field(min_length=1, max_length=500)
    available: int | None = Field(default=None, ge=0, le=2**53-1)
    name: str | None = Field(default=None, max_length=500)

    @property
    def unit_price_copper(self) -> int:
        return (self.batch_price_copper + self.batch_quantity - 1) // self.batch_quantity


class NpcReferences:
    def __init__(self, root: Path):
        self.path = root / "references/npc_prices.json"
        self._cache: tuple[tuple | None, dict[int, NpcQuote], str] | None = None

    def snapshot(self) -> dict[int, NpcQuote]:
        stat = self.path.stat() if self.path.exists() else None
        identity = (stat.st_size, stat.st_mtime_ns) if stat else None
        if self._cache is None or identity != self._cache[0]:
            if stat and stat.st_size > 8_000_000:
                raise ValueError("Справочник NPC превышает 8 МБ")
            body = self.path.read_bytes() if stat else b""
            data = json.loads(body) if body else {"format_version": 1, "quotes": {}}
            if not isinstance(data, dict) or data.get("format_version") != 1:
                raise ValueError("Неверный справочник NPC")
            rows = data.get("quotes")
            if not isinstance(rows, dict) or len(rows) > 30000:
                raise ValueError("Неверный справочник NPC")
            quotes = {int(i): NpcQuote.model_validate(q) for i, q in rows.items()}
            if any(i <= 0 for i in quotes):
                raise ValueError("Неверный ID предмета NPC")
            self._cache = identity, quotes, hashlib.sha256(body).hexdigest()
        return self._cache[1]

    def generation(self) -> str:
        self.snapshot()
        assert self._cache is not None
        return self._cache[2]

    def ingest(self, raw) -> dict:
        if not isinstance(raw, dict) or len(raw) > 30000:
            raise ValueError("Неверная коллекция NPC-цен аддона")
        quotes = dict(self.snapshot())
        changed = rejected = 0
        for identity, value in raw.items():
            try:
                item = int(identity)
                if item <= 0 or not isinstance(value, dict) or value.get("blocked"):
                    raise ValueError("Invalid NPC item")
                quote = NpcQuote.model_validate({k: value[k] for k in NpcQuote.model_fields if k in value})
                if not quote.scope.lower().endswith("/alliance") or quote.observed_at > datetime.now(UTC):
                    raise ValueError("Wrong faction or future NPC observation")
            except (ValueError, TypeError):
                rejected += 1
                continue
            previous = quotes.get(item)
            if previous is None or quote.observed_at > previous.observed_at:
                quotes[item] = quote
                changed += 1
        if changed:
            body = json.dumps({"format_version": 1, "quotes": {i: q.model_dump(mode="json") for i, q in sorted(quotes.items())}},
                              ensure_ascii=False, separators=(",", ":")).encode()
            if len(quotes) > 30000 or len(body) > 8_000_000:
                raise ValueError("Справочник NPC превышает допустимый размер")
            atomic_bytes(self.path, body)
            self._cache = None
        return {"updated": changed, "rejected": rejected, "total": len(quotes)}


class NpcReferenceProvider:
    def __init__(self, provider, quotes: dict[int, NpcQuote], cutoff: datetime,
                 catalog: NpcPriceDocument | None = None):
        self.provider = provider
        self.quotes = {i: q for i, q in quotes.items() if q.observed_at <= cutoff}
        self.catalog = catalog
        self.catalog_metadata = catalog.metadata() if catalog else None

    def __enter__(self):
        self.provider.__enter__()
        return self

    def __exit__(self, *args):
        return self.provider.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def items(self):
        items = self.provider.items()
        for item_id, quote in self.quotes.items():
            item = items.get(item_id) or Item(item_id=item_id, name=quote.name)
            if item.source == "manual" and item.vendor_buy_copper is not None:
                continue
            items[item_id] = item.model_copy(update={"vendor_buy_copper": quote.unit_price_copper,
                                                    "source": "NPC captured in game"})
        return items

    def npc_reference(self, item_id: int):
        quote = self.quotes.get(item_id)
        return quote.model_dump(mode="json") if quote else None

    def npc_catalog_reference(self, item_id: int):
        item = self.catalog.items.get(item_id) if self.catalog else None
        return {**item.model_dump(), "catalog": self.catalog_metadata} if item else None
