"""Versioned, offline disenchant estimates; independent of market snapshots."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from cennik.core.models import Model

BUNDLED = Path(__file__).resolve().parents[1] / "reference/disenchant.json"
MAX_BYTES = 8_000_000


class DisenchantYield(Model):
    material_item_id: int = Field(gt=0)
    min_count: int = Field(gt=0, le=1000)
    max_count: int = Field(gt=0, le=1000)
    chance: Decimal = Field(gt=0, le=1, decimal_places=6)

    @property
    def expected_count(self) -> Decimal:
        return self.chance * (self.min_count + self.max_count) / 2


class DisenchantItem(Model):
    name: str = Field(min_length=1, max_length=500)
    pool: str
    item_level: int = Field(ge=5)
    quality: Literal["UNCOMMON", "RARE", "EPIC"]
    item_class: Literal["Armor", "Weapon"]


class DisenchantRules(Model):
    format_version: Literal[1] = 1
    game: Literal["forever"] = "forever"
    version: str = Field(pattern=r"^[a-zA-Z0-9._-]{1,80}$")
    source: str = Field(min_length=1, max_length=300)
    source_url: str = Field(pattern=r"^https://", max_length=2000)
    checked_at: AwareDatetime
    build_id: str | None = Field(default=None, max_length=80)
    basis: Literal["model"] = "model"
    pools: dict[str, list[DisenchantYield]] = Field(min_length=1, max_length=200)
    items: dict[int, DisenchantItem] = Field(min_length=1, max_length=30000)
    exclusions: dict[int, str] = Field(default_factory=dict, max_length=30000)

    @model_validator(mode="after")
    def coherent(self):
        for pool, rows in self.pools.items():
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", pool) or not 1 <= len(rows) <= 30:
                raise ValueError("Invalid disenchant pool")
            if len({r.material_item_id for r in rows}) != len(rows):
                raise ValueError("Duplicate disenchant material")
            if sum((r.chance for r in rows), Decimal(0)) != 1:
                raise ValueError("Disenchant probabilities must sum to 1")
            if any(r.max_count < r.min_count for r in rows):
                raise ValueError("Invalid disenchant quantity range")
        if any(i <= 0 or item.pool not in self.pools for i, item in self.items.items()):
            raise ValueError("Unknown disenchant pool or invalid item id")
        if any(i <= 0 or not reason.strip() or len(reason) > 1000 for i, reason in self.exclusions.items()):
            raise ValueError("Invalid disenchant exclusion")
        return self

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    def yields(self, item_id: int) -> list[DisenchantYield]:
        item = self.items.get(item_id)
        return self.pools[item.pool] if item and item_id not in self.exclusions else []

    def metadata(self) -> dict:
        return {"version": self.version, "sha256": self.sha256, "source": self.source,
                "source_url": self.source_url, "checked_at": self.checked_at.isoformat(),
                "build_id": self.build_id, "basis": self.basis, "items": len(self.items),
                "eligible_candidates": sum(i not in self.exclusions for i in self.items),
                "exclusions": len(self.exclusions), "attribution": "Data from https://ahledger.com",
                "note": "Расчётная модель; распыляемость и особенности Forever требуют проверки"}


def _atomic(path: Path, body: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".disenchant-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class DisenchantCatalog:
    def __init__(self, directory: Path, bundled: Path = BUNDLED):
        self.directory, self.bundled = directory, bundled
        self.lock = threading.RLock()
        self._cache: tuple[tuple, DisenchantRules] | None = None

    def snapshot(self) -> DisenchantRules:
        with self.lock:
            path = self.directory / "active.json"
            if not path.exists():
                path = self.bundled
            stat = path.stat()
            identity = (str(path), stat.st_mtime_ns, stat.st_size)
            if self._cache is None or self._cache[0] != identity:
                if stat.st_size > MAX_BYTES:
                    raise ValueError("Справочник распыления превышает 8 МБ")
                self._cache = identity, DisenchantRules.model_validate_json(path.read_bytes())
            return self._cache[1]

    def preview(self, value: DisenchantRules) -> dict:
        previous = self.snapshot()
        if previous.version == value.version and previous.sha256 != value.sha256:
            raise ValueError("Изменённому справочнику нужен новый номер версии")
        changed = sum(previous.items.get(i) != value.items.get(i)
                      or previous.yields(i) != value.yields(i)
                      or previous.exclusions.get(i) != value.exclusions.get(i)
                      for i in previous.items.keys() & value.items.keys())
        return {"previous": previous.metadata(), "next": value.metadata(), "changed_items": changed,
                "added_items": len(value.items.keys() - previous.items.keys()),
                "removed_items": len(previous.items.keys() - value.items.keys()),
                "requires_recalculation": previous.sha256 != value.sha256}

    def install(self, value: DisenchantRules) -> dict:
        body = value.model_dump_json().encode()
        if len(body) > MAX_BYTES:
            raise ValueError("Справочник распыления превышает 8 МБ")
        with self.lock:
            report = self.preview(value)
            previous = self.snapshot()
            for document in (previous, value):
                path = self.directory / "versions" / (document.version + ".json")
                if path.exists() and DisenchantRules.model_validate_json(path.read_bytes()).sha256 != document.sha256:
                    raise ValueError("Эта версия уже сохранена с другим содержимым")
            for document in (previous, value):
                _atomic(self.directory / "versions" / (document.version + ".json"), document.model_dump_json().encode())
            _atomic(self.directory / "active.json", body)
            self._cache = None
            return report


def expected_gross(yields: list[dict], quantity: int = 1) -> int:
    """Round down each material's expected revenue; never credit missing prices."""
    return sum(int(Decimal(y["expected_count"]) * quantity * y["unit_price_copper"]) for y in yields)
