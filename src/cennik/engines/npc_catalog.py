"""Offline, replaceable NPC reference data; never a merchant inventory."""
from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from ahledger.util import atomic_write
from cennik.core.models import Model

BUNDLED = Path(__file__).resolve().parents[1] / "reference/npc_catalog.json"
MAX_BYTES = 8_000_000


class NpcCatalogItem(Model):
    name: str = Field(min_length=1, max_length=500)
    buy_copper: int | None = Field(default=None, gt=0, le=2**53-1, strict=True)
    sell_copper: int | None = Field(default=None, ge=0, le=2**53-1, strict=True)
    # None means the API did not establish the purchase quantity, not one unit.
    buy_quantity: int | None = Field(default=None, gt=0, le=1000000, strict=True)


class NpcPriceDocument(Model):
    format_version: Literal[1] = 1
    game: Literal["forever"] = "forever"
    version: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$")
    phase: Literal["beta", "release"]
    build: str = Field(pattern=r"^1\.60\.\d+\.\d+$")
    source: str = Field(min_length=1, max_length=2000)
    source_url: Literal["https://auctionhouse.fyi/api/v1/items"]
    retrieved_at: AwareDatetime
    raw_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    basis: Literal["client_tables"] = "client_tables"
    items: dict[int, NpcCatalogItem] = Field(min_length=1, max_length=30000)

    @model_validator(mode="after")
    def valid_ids(self):
        if any(i <= 0 or i >= 2147483648 for i in self.items):
            raise ValueError("Справочник NPC должен содержать базовые ID предметов")
        return self

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    def metadata(self) -> dict:
        return {"version": self.version, "sha256": self.sha256, "phase": self.phase,
                "build": self.build, "source": self.source, "source_url": self.source_url,
                "retrieved_at": self.retrieved_at.isoformat(), "raw_sha256": self.raw_sha256,
                "items": len(self.items), "buy_prices": sum(x.buy_copper is not None for x in self.items.values()),
                "sell_prices": sum(x.sell_copper is not None for x in self.items.values()),
                "unknown_buy_quantity": sum(x.buy_copper is not None and x.buy_quantity is None for x in self.items.values()),
                "requires_release_refresh": self.phase == "beta", "merchant_inventory_confirmed": False}


class NpcPriceCatalog:
    def __init__(self, directory: Path, bundled: Path = BUNDLED):
        self.directory, self.bundled = directory, bundled
        self.lock = threading.RLock()
        self._cache: tuple[tuple, NpcPriceDocument] | None = None

    def snapshot(self) -> NpcPriceDocument:
        with self.lock:
            path = self.directory / "active.json"
            if not path.exists():
                path = self.bundled
            stat = path.stat()
            identity = (str(path), stat.st_mtime_ns, stat.st_size)
            if self._cache is None or identity != self._cache[0]:
                if stat.st_size > MAX_BYTES:
                    raise ValueError("Справочник NPC превышает 8 МБ")
                self._cache = identity, NpcPriceDocument.model_validate_json(path.read_bytes())
            return self._cache[1]

    def preview(self, value: NpcPriceDocument) -> dict:
        previous = self.snapshot()
        if previous.version == value.version and previous.sha256 != value.sha256:
            raise ValueError("Изменённому справочнику NPC нужен новый номер версии")
        return {"previous": previous.metadata(), "next": value.metadata(),
                "changed_items": sum(previous.items[i] != value.items[i] for i in previous.items.keys() & value.items.keys()),
                "added_items": len(value.items.keys() - previous.items.keys()),
                "removed_items": len(previous.items.keys() - value.items.keys()),
                "requires_recalculation": previous.sha256 != value.sha256}

    def install(self, value: NpcPriceDocument) -> dict:
        body = value.model_dump_json().encode()
        if len(body) > MAX_BYTES:
            raise ValueError("Справочник NPC превышает 8 МБ")
        with self.lock:
            report = self.preview(value)
            previous = self.snapshot()
            for doc in (previous, value):
                path = self.directory / "versions" / (doc.version + ".json")
                if path.exists() and NpcPriceDocument.model_validate_json(path.read_bytes()).sha256 != doc.sha256:
                    raise ValueError("Эта версия NPC уже сохранена с другим содержимым")
            for doc in (previous, value):
                atomic_write(self.directory / "versions" / (doc.version + ".json"), doc.model_dump_json().encode())
            atomic_write(self.directory / "active.json", body)
            self._cache = None
            return report
