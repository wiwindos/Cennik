from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from cennik.core.models import Item, PriceObservation, Recipe, RecipeInput, utc


def fingerprint(path: Path) -> str:
    """Identity of the actual atomic DuckDB file, independent of Parquet pointers."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class DuckDBProvider:
    def __init__(self, path: Path, market: str, as_of: datetime):
        self.market, self.as_of = market, utc(as_of)
        self.db = duckdb.connect(str(path), read_only=True)
        self.db.execute("SET TimeZone='UTC'")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def rows(self, sql: str, params: list | None = None) -> list[dict[str, Any]]:
        result = self.db.execute(sql, params or [])
        return [dict(zip([c[0] for c in result.description], row, strict=True)) for row in result.fetchall()]

    def markets(self) -> list[dict[str, Any]]:
        return self.rows("""SELECT m.market,m.label,m.market_status,
            (SELECT max(observed_at) FROM (
                SELECT market,observed_at FROM price_snapshots
                UNION ALL SELECT market,observed_at FROM price_history
            ) p WHERE p.market=m.market AND p.observed_at<=?) AS observed_at
            FROM markets m ORDER BY m.market""", [self.as_of])

    @staticmethod
    def observation(row: dict) -> PriceObservation:
        fields = PriceObservation.model_fields
        return PriceObservation.model_validate({k: v for k, v in row.items() if k in fields})

    def current_prices(self) -> dict[int, PriceObservation]:
        rows = self.rows("""WITH candidates AS (
            SELECT item_id,market,observed_at,'scan' AS granularity,min_copper,
                median_copper,quantity,NULL::BIGINT AS auctions,source,raw_id,sha256,0 AS priority
            FROM price_snapshots WHERE market=? AND observed_at<=?
            UNION ALL SELECT item_id,market,observed_at,granularity,min_copper,
                median_copper,quantity,auctions,source,raw_id,sha256,1 AS priority
            FROM price_history WHERE market=? AND observed_at<=?
        ) SELECT * EXCLUDE(priority) FROM candidates QUALIFY row_number() OVER (
            PARTITION BY item_id ORDER BY observed_at DESC,
            CASE granularity WHEN 'scan' THEN 0 WHEN 'hour' THEN 1 ELSE 2 END,
            priority,source,raw_id)=1""", [self.market, self.as_of, self.market, self.as_of])
        # Sales are source estimates, with their own observation timestamp. Never infer
        # them from supply changes and never backfill a missing field with zero.
        sales = {r["item_id"]: r for r in self.rows("""SELECT item_id,observed_at,sales_estimated_per_day
            FROM price_snapshots WHERE market=? AND observed_at<=?
            AND sales_estimated_per_day IS NOT NULL
            QUALIFY row_number() OVER (PARTITION BY item_id ORDER BY observed_at DESC,source,raw_id)=1""",
            [self.market, self.as_of])}
        result = {}
        for row in rows:
            sale = sales.get(row["item_id"])
            if sale:
                row.update(sales_estimated_per_day=sale["sales_estimated_per_day"],
                           sales_observed_at=sale["observed_at"])
            result[row["item_id"]] = self.observation(row)
        return result

    def historical_prices(self, days: int) -> dict[int, list[PriceObservation]]:
        rows = self.rows("""SELECT * FROM price_history WHERE market=?
            AND observed_at>=? AND observed_at<=? ORDER BY item_id,observed_at,granularity""",
            [self.market, self.as_of - timedelta(days=days), self.as_of])
        result: dict[int, list[PriceObservation]] = {}
        for row in rows:
            result.setdefault(row["item_id"], []).append(self.observation(row))
        return result

    def items(self) -> dict[int, Item]:
        return {row["item_id"]: Item(**row) for row in self.rows("""SELECT item_id,name,
            vendor_sell_copper,vendor_buy_copper,binding,origin_dataset AS source
            FROM items WHERE retrieved_at<=?""", [self.as_of])}

    def recipes(self) -> list[Recipe]:
        # Mechanics are global, but only use versions with provenance for this market.
        # Valuations have no version_hash and are intentionally never used for costs.
        rows = self.rows("""SELECT DISTINCT r.spell_id,r.version_hash,r.result_item_id,
            r.qty_min,r.qty_max,r.name,r.profession,r.skill_required,r.acquisition,
            r.raw_id,r.payload_json,m.market AS source_market FROM recipes r
            JOIN dataset_metadata m ON m.raw_id=r.raw_id AND m.dataset='crafting'
            WHERE (m.market=? OR m.market IS NULL) AND r.retrieved_at<=?
            AND m.status NOT IN ('FAILED','NOT_AVAILABLE') ORDER BY r.spell_id,r.version_hash""",
            [self.market, self.as_of])
        inputs = self.rows("""SELECT spell_id,version_hash,item_id,quantity
            FROM recipe_inputs WHERE retrieved_at<=? ORDER BY spell_id,version_hash,item_id""", [self.as_of])
        grouped: dict[tuple, list[RecipeInput]] = {}
        for row in inputs:
            grouped.setdefault((row["spell_id"], row["version_hash"]), []).append(
                RecipeInput(item_id=row["item_id"], quantity=row["quantity"]))
        result = []
        for row in rows:
            materials = grouped.get((row["spell_id"], row["version_hash"]), [])
            mechanics = json.loads(row.pop("payload_json") or "{}")
            expected = sorted((r.get("id"), r.get("qty")) for r in mechanics.get("reagents", []))
            actual = sorted((r.item_id, r.quantity) for r in materials)
            # A partial join is not a cheaper recipe: all archived reagents must match.
            if not expected or actual != expected:
                materials = []
            result.append(Recipe(**row, inputs=materials))
        return result

    def quality(self) -> dict[str, Any]:
        metadata = self.rows("""SELECT dataset,item_id,status,warnings_json,gaps_json,raw_id
            FROM dataset_metadata WHERE (market=? OR market IS NULL) AND retrieved_at<=?
            ORDER BY dataset,item_id,raw_id""", [self.market, self.as_of])
        for row in metadata:
            for key in ("warnings_json", "gaps_json"):
                row[key.removesuffix("_json")] = json.loads(row.pop(key) or "[]")
        return {"metadata": metadata, "raw_order_book_available": False,
                "sales_are_source_estimates": True, "coverage_is_partial": True}
