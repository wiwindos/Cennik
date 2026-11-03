"""Versioned beta fallback. Original evidence dates never become fresh scans."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from pydantic import AwareDatetime, Field, model_validator

from cennik.core.models import Item, Model, PriceObservation, Recipe


class BootstrapDocument(Model):
    format_version: int = Field(default=1, ge=1, le=1)
    version: str = Field(pattern=r"^[a-zA-Z0-9._-]{1,80}$")
    phase: str = Field(default="beta", pattern="^beta$")
    game: str = Field(default="forever", pattern="^forever$")
    created_at: AwareDatetime
    source_market: str = Field(pattern=r"^forever\.[a-z0-9_.-]+$")
    items: dict[int, Item] = Field(max_length=100000)
    recipes: list[Recipe] = Field(max_length=20000)
    prices: dict[int, PriceObservation] = Field(max_length=100000)
    references: dict[int, int] = Field(default_factory=dict, max_length=100000)
    item_facts: dict[int, dict] = Field(default_factory=dict, max_length=100000)
    provenance: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_evidence(self):
        if not self.items or not self.recipes or not self.prices:
            raise ValueError("Стартовый пакет должен содержать предметы, рецепты и цены")
        if len({r.spell_id for r in self.recipes}) != len(self.recipes):
            raise ValueError("Повтор рецепта в стартовом пакете")
        for identity, item in self.items.items():
            if not 0 < identity < 2**31 or identity != item.item_id:
                raise ValueError("Неверный ID предмета стартового пакета")
            if any(v is not None and (type(v) is not int or not 0 <= v <= 2**53-1)
                   for v in (item.vendor_buy_copper, item.vendor_sell_copper)):
                raise ValueError("Неверная NPC-цена стартового пакета")
        for identity, price in self.prices.items():
            if identity != price.item_id or price.market != self.source_market or price.observed_at > datetime.now(UTC):
                raise ValueError("Неверный рынок/время цены стартового пакета")
            if price.min_copper is None or not 0 < price.min_copper <= 2**53-1:
                raise ValueError("Неверная цена стартового пакета")
            if price.quantity is not None and not 0 <= price.quantity <= 2**53-1:
                raise ValueError("Неверный объём стартового пакета")
        if any(not r.result_item_id or r.result_item_id <= 0 or r.spell_id <= 0
               or not r.qty_min or r.qty_min <= 0 or not r.qty_max or r.qty_max < r.qty_min
               or not r.inputs or any(i.quantity <= 0 or i.item_id <= 0 for i in r.inputs) for r in self.recipes):
            raise ValueError("Неполный рецепт стартового пакета")
        return self


class BootstrapCatalog:
    def __init__(self, root):
        self.path = root / "bootstrap/active.json"
        self.cached, self.stamp, self.sha256 = None, None, "empty"

    def snapshot(self):
        stat = self.path.stat() if self.path.exists() else None
        key = (stat.st_mtime_ns, stat.st_size) if stat else None
        if key != self.stamp:
            body = self.path.read_bytes() if stat else b""
            if len(body) > 64_000_000:
                raise ValueError("Стартовый пакет превышает 64 МБ")
            self.cached = BootstrapDocument.model_validate_json(body) if body else None
            self.sha256 = hashlib.sha256(body).hexdigest() if body else "empty"
            self.stamp = key
        return self.cached

    def metadata(self):
        doc = self.snapshot()
        return {"installed": bool(doc), "version": doc.version if doc else None, "sha256": self.sha256,
            "phase": "beta", "source_market": doc.source_market if doc else None,
            "items": len(doc.items) if doc else 0, "recipes": len(doc.recipes) if doc else 0,
            "prices": len(doc.prices) if doc else 0,
            "observed_at": max(p.observed_at for p in doc.prices.values()) if doc else None,
            "note": "Бета — стартовый ориентир, не подтверждение наличия лотов. Даты наблюдений сохранены."}


class BootstrapProvider:
    def __init__(self, provider, document, *, max_age=3600):
        self.provider, self.document, self.max_age = provider, document, max_age
        self._prices = None

    def __getattr__(self, key):
        return getattr(self.provider, key)

    def __enter__(self):
        self.provider.__enter__()
        return self

    def __exit__(self, *args):
        return self.provider.__exit__(*args)

    def markets(self):
        result = self.provider.markets()
        for row in result:
            if (not self.market or row["market"] == self.market) and not row.get("observed_at"):
                row.update(observed_at=max(p.observed_at for p in self.document.prices.values()), market_status="BETA_BASELINE")
        return result

    def current_prices(self):
        if self._prices is not None:
            return self._prices
        live = self.provider.current_prices()
        fresh = any((self.as_of-p.observed_at).total_seconds() <= self.max_age for p in live.values())
        head = self.provider.rows("SELECT max(observed_at) t FROM source_heads WHERE observed_at<=?", [self.as_of])[0]["t"]
        # A complete fresh market never resurrects absent beta auctions.
        if fresh or head and (self.as_of-head).total_seconds() <= self.max_age:
            self._prices = live
        else:
            self._prices = {i: p.model_copy(update={"market": self.market, "source": "Бета · " + self.document.source_market,
                "beta_baseline": True, "baseline_version": self.document.version,
                "reference_market": self.document.source_market}) for i, p in self.document.prices.items() if p.observed_at <= self.as_of}
        return self._prices

    def items(self):
        result = {i: item.model_copy(update={"source": "beta", "source_phase": "beta"}) for i, item in self.document.items.items()}
        for identity, item in self.provider.items().items():
            previous = result.get(identity)
            if previous:
                item = item.model_copy(update={k: getattr(previous, k) for k in
                    ("name", "vendor_buy_copper", "vendor_sell_copper", "binding") if getattr(item, k) is None})
            result[identity] = item
        return result

    def recipes(self):
        path = self.provider.database.root / "references/release_recipes.json"
        trusted = json.loads(path.read_text("utf-8")) if path.exists() else {}
        result = {r.spell_id: r.model_copy(update={"beta_baseline": True, "baseline_version": self.document.version}) for r in self.document.recipes
                  if (r.profession or "").casefold() not in trusted}
        for recipe in self.provider.recipes():
            result[recipe.spell_id] = recipe
        return list(result.values())

    def source_reference(self, item_id):
        previous = self.provider.source_reference(item_id)
        if previous and (self.as_of-previous["observed_at"]).total_seconds() <= self.max_age:
            return previous
        value, price = self.document.references.get(item_id), self.document.prices.get(item_id)
        return {"copper": value, "observed_at": price.observed_at, "beta_baseline": True,
            "source": "Бета · " + self.document.source_market} if value and price else previous

    def quality(self):
        return {**self.provider.quality(), "beta_baseline": {"version": self.document.version,
            "market": self.document.source_market, "enabled": True,
            "price_items": sum(p.beta_baseline for p in self.current_prices().values())}}
