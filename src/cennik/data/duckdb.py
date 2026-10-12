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
        self.recipe_selection: dict[str, int | str] = {"policy": "latest_market_snapshot_as_of"}
        self._recipes: list[Recipe] | None = None

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
        if self._recipes is not None:
            return self._recipes
        # recipe_sources records when THIS market saw immutable mechanics. The
        # deduplicated recipes/inputs rows may have been retrieved later elsewhere.
        bridge = bool(self.db.execute("SELECT 1 FROM information_schema.tables WHERE table_name='recipe_sources'").fetchone())
        bridge = bridge and bool(self.db.execute("SELECT 1 FROM recipe_sources LIMIT 1").fetchone())
        source = """SELECT s.spell_id,s.version_hash,s.market,s.raw_id,s.retrieved_at,
            m.payload_json,m.params_json FROM recipe_sources s
            JOIN dataset_metadata m ON m.raw_id=s.raw_id AND m.dataset='crafting'
            AND m.market IS NOT DISTINCT FROM s.market
            WHERE s.retrieved_at<=? AND m.retrieved_at<=?
            AND m.status NOT IN ('FAILED','NOT_AVAILABLE')""" if bridge else """SELECT r.spell_id,r.version_hash,
            m.market,m.raw_id,m.retrieved_at,m.payload_json,m.params_json FROM recipes r
            JOIN dataset_metadata m ON m.raw_id=r.raw_id AND m.dataset='crafting'
            WHERE m.retrieved_at<=? AND r.retrieved_at<=?
            AND m.status NOT IN ('FAILED','NOT_AVAILABLE')"""
        rows = self.rows(f"""WITH provenance AS ({source})
            SELECT DISTINCT r.spell_id,r.version_hash,r.result_item_id,
            r.qty_min,r.qty_max,r.name,r.profession,r.skill_required,r.acquisition,
            m.raw_id,r.payload_json,m.market AS source_market,
            m.retrieved_at AS source_retrieved_at,
            coalesce(try_cast(json_extract_string(m.payload_json,'$.response_metadata.generatedAt') AS TIMESTAMPTZ),
                     try_cast(json_extract_string(m.payload_json,'$.generatedAt') AS TIMESTAMPTZ),
                     m.retrieved_at) AS source_generated_at
            FROM recipes r JOIN provenance m USING(spell_id,version_hash)
            WHERE m.market=? OR m.market IS NULL ORDER BY r.spell_id,r.version_hash,m.raw_id""",
            [self.as_of, self.as_of, self.market])
        # A new all=1 snapshot supersedes its profession's older membership, even
        # when capped/PARTIAL. Absence never resurrects a removed old recipe.
        snapshots = self.rows("""SELECT raw_id,market,retrieved_at,
            json_extract_string(params_json,'$.profession') AS profession,
            coalesce(try_cast(json_extract_string(payload_json,'$.response_metadata.generatedAt') AS TIMESTAMPTZ),
                     try_cast(json_extract_string(payload_json,'$.generatedAt') AS TIMESTAMPTZ),
                     retrieved_at) AS generated_at FROM dataset_metadata
            WHERE dataset='crafting' AND (market=? OR market IS NULL) AND retrieved_at<=?
            AND status NOT IN ('FAILED','NOT_AVAILABLE')
            AND json_extract_string(params_json,'$.all') IN ('1','true')""", [self.market, self.as_of])
        latest: dict[tuple, tuple[datetime, set[str]]] = {}
        for snapshot in snapshots:
            if snapshot['generated_at'] > self.as_of or not snapshot['profession']:
                continue
            key = (snapshot['market'], snapshot['profession'].casefold())
            stamp = snapshot['generated_at']
            old = latest.get(key)
            if old is None or stamp > old[0]:
                latest[key] = (stamp, {snapshot['raw_id']})
            elif stamp == old[0]:
                old[1].add(snapshot['raw_id'])
        eligible: dict[int, list[dict]] = {}
        excluded = 0
        for row in rows:
            profession = (row['profession'] or '').casefold()
            scope = latest.get((self.market, profession)) or latest.get((row['source_market'], profession))
            if row['source_generated_at'] > self.as_of or (scope and row['raw_id'] not in scope[1]):
                excluded += 1
                continue
            eligible.setdefault(row['spell_id'], []).append(row)
        selected, ambiguous, superseded = [], 0, 0
        for candidates in eligible.values():
            # Prefer this market over unscoped provenance; never borrow another
            # market's latest version or break equal-time conflicts by hash/profit.
            local = [r for r in candidates if r['source_market'] == self.market]
            scoped = local or candidates
            stamp = max(r['source_generated_at'] for r in scoped)
            newest = [r for r in scoped if r['source_generated_at'] == stamp]
            superseded += len({r['version_hash'] for r in candidates}) - len({r['version_hash'] for r in newest})
            if len({r['version_hash'] for r in newest}) != 1:
                ambiguous += 1
                continue
            selected.append(max(newest, key=lambda r: (r['source_retrieved_at'], r['raw_id'] or '')))
        self.recipe_selection.update(selected=len(selected), ambiguous=ambiguous,
                                     superseded_versions=superseded, outside_latest_snapshot=excluded)
        inputs = self.rows("""SELECT spell_id,version_hash,item_id,quantity
            FROM recipe_inputs ORDER BY spell_id,version_hash,item_id""")
        grouped: dict[tuple, list[RecipeInput]] = {}
        for row in inputs:
            grouped.setdefault((row["spell_id"], row["version_hash"]), []).append(
                RecipeInput(item_id=row["item_id"], quantity=row["quantity"]))
        result = []
        for row in selected:
            materials = grouped.get((row["spell_id"], row["version_hash"]), [])
            mechanics = json.loads(row.pop("payload_json") or "{}")
            expected = sorted((r.get("id"), r.get("qty")) for r in mechanics.get("reagents", []))
            actual = sorted((r.item_id, r.quantity) for r in materials)
            # A partial join is not a cheaper recipe: all archived reagents must match.
            if not expected or actual != expected:
                materials = []
            result.append(Recipe(**row, inputs=materials))
        self._recipes = result
        return result

    def quality(self) -> dict[str, Any]:
        metadata = self.rows("""SELECT dataset,item_id,status,warnings_json,gaps_json,raw_id
            FROM dataset_metadata WHERE (market=? OR market IS NULL) AND retrieved_at<=?
            ORDER BY dataset,item_id,raw_id""", [self.market, self.as_of])
        for row in metadata:
            for key in ("warnings_json", "gaps_json"):
                row[key.removesuffix("_json")] = json.loads(row.pop(key) or "[]")
        return {"metadata": metadata, "raw_order_book_available": False,
                "sales_are_source_estimates": True, "coverage_is_partial": True,
                "recipe_selection": self.recipe_selection}
