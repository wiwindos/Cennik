from __future__ import annotations

import os
import sys
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import duckdb
import pyarrow as pa

from .config import Settings
from .models import JobSpec
from .parsers import Parsed, parse
from .storage import RawArchive, State
from .util import atomic_json, copper, digest, dumps, timestamp, utc_now

COMMON = {
    "raw_id": "VARCHAR",
    "sha256": "VARCHAR",
    "retrieved_at": "TIMESTAMPTZ",
    "payload_json": "VARCHAR",
}
SCHEMAS: dict[str, dict[str, str]] = {
    "markets": {
        "market": "VARCHAR",
        "game": "VARCHAR",
        "ruleset": "VARCHAR",
        "faction": "VARCHAR",
        "region": "VARCHAR",
        "label": "VARCHAR",
        "market_status": "VARCHAR",
    },
    "items": {
        "item_id": "BIGINT",
        "name": "VARCHAR",
        "quality": "VARCHAR",
        "item_class": "VARCHAR",
        "item_subclass": "VARCHAR",
        "item_level": "BIGINT",
        "required_level": "BIGINT",
        "vendor_sell_copper": "BIGINT",
        "vendor_buy_copper": "BIGINT",
        "stack_size": "BIGINT",
        "binding": "VARCHAR",
        "is_placeholder": "BOOLEAN",
        "origin_dataset": "VARCHAR",
    },
    "item_versions": {"item_id": "BIGINT", "version_hash": "VARCHAR", "build_id": "VARCHAR"},
    "recipes": {
        "spell_id": "BIGINT",
        "name": "VARCHAR",
        "profession": "VARCHAR",
        "skill_required": "BIGINT",
        "acquisition": "VARCHAR",
        "result_item_id": "BIGINT",
        "qty_min": "BIGINT",
        "qty_max": "BIGINT",
        "version_hash": "VARCHAR",
    },
    "recipe_inputs": {
        "spell_id": "BIGINT",
        "version_hash": "VARCHAR",
        "item_id": "BIGINT",
        "quantity": "BIGINT",
    },
    "recipe_sources": {"spell_id": "BIGINT", "version_hash": "VARCHAR", "market": "VARCHAR"},
    "recipe_valuations": {
        "market": "VARCHAR",
        "spell_id": "BIGINT",
        "observed_at": "TIMESTAMPTZ",
        "generated_at": "TIMESTAMPTZ",
        "result_price_copper": "BIGINT",
        "cost_copper": "BIGINT",
        "net_value_copper": "BIGINT",
        "margin_copper": "BIGINT",
        "net_value_exact_copper": "VARCHAR",
        "margin_exact_copper": "VARCHAR",
        "sales_estimated_per_day": "DECIMAL(24,6)",
        "is_estimate": "BOOLEAN",
    },
    "price_history_ranges": {
        "market": "VARCHAR",
        "item_id": "BIGINT",
        "observed_at": "TIMESTAMPTZ",
        "range": "VARCHAR",
        "granularity": "VARCHAR",
        "resolution_seconds": "BIGINT",
        "source": "VARCHAR",
        "observation_kind": "VARCHAR",
        "currency": "VARCHAR",
        "min_copper": "BIGINT",
        "median_copper": "BIGINT",
        "p25_copper": "BIGINT",
        "p75_copper": "BIGINT",
        "quantity": "BIGINT",
        "auctions": "BIGINT",
    },
    "price_snapshots": {
        "market": "VARCHAR",
        "item_id": "BIGINT",
        "observed_at": "TIMESTAMPTZ",
        "source": "VARCHAR",
        "min_copper": "BIGINT",
        "median_copper": "BIGINT",
        "quantity": "BIGINT",
        "median7d_copper": "BIGINT",
        "median30d_copper": "BIGINT",
        "low30d_copper": "BIGINT",
        "high30d_copper": "BIGINT",
        "sales_per_day_x10": "BIGINT",
        "sales_estimated_per_day": "DECIMAL(24,6)",
        "sales_json": "VARCHAR",
    },
    "market_economy": {
        "market": "VARCHAR",
        "observed_at": "TIMESTAMPTZ",
        "granularity": "VARCHAR",
        "value_copper": "BIGINT",
        "value_low_copper": "BIGINT",
        "auctions": "BIGINT",
        "units": "BIGINT",
        "items": "BIGINT",
        "scans": "BIGINT",
        "contributors": "BIGINT",
    },
    "disenchant": {
        "market": "VARCHAR",
        "item_id": "BIGINT",
        "observed_at": "TIMESTAMPTZ",
        "min_copper": "BIGINT",
        "expected_value_copper": "BIGINT",
        "net_value_copper": "BIGINT",
        "profit_copper": "BIGINT",
        "is_estimate": "BOOLEAN",
    },
    "disenchant_mechanics": {
        "item_id": "BIGINT",
        "material_item_id": "BIGINT",
        "min_count": "BIGINT",
        "max_count": "BIGINT",
        "chance": "DECIMAL(18,12)",
        "version_hash": "VARCHAR",
    },
    "market_signals": {
        "market": "VARCHAR",
        "item_id": "BIGINT",
        "observed_at": "TIMESTAMPTZ",
        "discount": "BIGINT",
        "horizon_days": "BIGINT",
        "fires": "BIGINT",
        "wins": "BIGINT",
        "firing_now": "BOOLEAN",
        "median_copper": "BIGINT",
        "base_copper": "BIGINT",
        "base_exact_copper": "VARCHAR",
        "is_estimate": "BOOLEAN",
    },
    "market_deals": {
        "market": "VARCHAR",
        "item_id": "BIGINT",
        "observed_at": "TIMESTAMPTZ",
        "sort": "VARCHAR",
        "min_copper": "BIGINT",
        "median_copper": "BIGINT",
        "median30d_copper": "BIGINT",
        "quantity": "BIGINT",
        "saving_copper": "BIGINT",
        "sales_estimated_per_day": "DECIMAL(24,6)",
    },
    "market_structure": {
        "market": "VARCHAR",
        "item_id": "BIGINT",
        "observed_at": "TIMESTAMPTZ",
        "depth": "BIGINT",
        "median_copper": "BIGINT",
        "value_copper": "BIGINT",
        "spread": "DECIMAL(24,12)",
        "turnover": "DECIMAL(24,12)",
        "volatility": "DECIMAL(24,12)",
        "supply_shock": "DECIMAL(24,12)",
    },
    "item_sources": {"item_id": "BIGINT", "availability": "VARCHAR"},
    "build_changes": {
        "build_id": "VARCHAR",
        "title": "VARCHAR",
        "published_at": "TIMESTAMPTZ",
        "url": "VARCHAR",
        "description": "VARCHAR",
        "is_structured_diff": "BOOLEAN",
    },
    "dataset_metadata": {
        "dataset": "VARCHAR",
        "market": "VARCHAR",
        "item_id": "BIGINT",
        "range": "VARCHAR",
        "status": "VARCHAR",
        "record_count": "BIGINT",
        "minimum_at": "TIMESTAMPTZ",
        "maximum_at": "TIMESTAMPTZ",
        "granularity": "VARCHAR",
        "warnings_json": "VARCHAR",
        "gaps_json": "VARCHAR",
        "http_status": "BIGINT",
        "url": "VARCHAR",
        "params_json": "VARCHAR",
    },
    "download_jobs": {
        "job_id": "VARCHAR",
        "dataset": "VARCHAR",
        "market": "VARCHAR",
        "item_id": "BIGINT",
        "priority": "BIGINT",
        "status": "VARCHAR",
        "attempts": "BIGINT",
        "records": "BIGINT",
        "error": "VARCHAR",
        "spec_json": "VARCHAR",
    },
}
KEYS = {
    "markets": "market",
    "items": "item_id",
    "item_versions": "item_id,version_hash",
    "recipes": "spell_id,version_hash",
    "recipe_inputs": "spell_id,version_hash,item_id",
    "recipe_sources": "spell_id,version_hash,market,raw_id",
    "price_history_ranges": 'market,item_id,observed_at,"range"',
    "price_snapshots": "market,item_id,observed_at,source",
    "recipe_valuations": "market,spell_id,observed_at,generated_at",
    "market_economy": "market,observed_at,granularity",
    "disenchant": "market,item_id,observed_at",
    "disenchant_mechanics": "item_id,material_item_id,version_hash",
    "market_signals": "market,item_id,observed_at,discount,horizon_days",
    "market_deals": 'market,item_id,observed_at,"sort"',
    "market_structure": "market,item_id,observed_at",
    "build_changes": "build_id",
}


def normalized(
    spec: JobSpec,
    parsed: Parsed,
    meta: dict,
    *,
    emit: Callable[[str, dict], None] | None = None,
) -> dict[str, list[dict]]:
    tables: dict[str, list[dict]] = defaultdict(list)
    common = {
        "raw_id": meta["id"],
        "sha256": meta.get("reused_sha256") or meta["sha256"],
        "retrieved_at": meta["retrieved_at"],
    }

    def add(table, payload, **fields):
        row = {**common, "payload_json": dumps(payload), **fields}
        if emit is None:
            tables[table].append(row)
        else:
            emit(table, row)

    data, dataset = parsed.data, spec.dataset
    add(
        "dataset_metadata",
        {
            **meta,
            "response_metadata": {k: v for k, v in data.items() if not isinstance(v, list)}
            if isinstance(data, dict)
            else None,
        },
        dataset=dataset,
        market=spec.market,
        item_id=spec.item_id,
        range=spec.params.get("range"),
        status=parsed.status,
        record_count=parsed.count,
        minimum_at=parsed.minimum,
        maximum_at=parsed.maximum,
        granularity=parsed.granularity,
        http_status=meta["http_status"],
        warnings_json=dumps(parsed.warnings),
        gaps_json=dumps(parsed.gaps),
        url=spec.url,
        params_json=dumps(spec.params),
    )
    if parsed.status == "FAILED":
        return tables
    for item_id, item in parsed.items.items():
        add(
            "items",
            item,
            item_id=item_id,
            name=item.get("name"),
            quality=item.get("quality"),
            item_class=item.get("itemClass"),
            item_subclass=item.get("itemSubclass"),
            item_level=item.get("level"),
            required_level=item.get("requiredLevel"),
            vendor_sell_copper=copper(item.get("vendorSell")),
            vendor_buy_copper=copper(item.get("vendorBuy")),
            stack_size=item.get("stackSize"),
            binding=item.get("binding"),
            is_placeholder="name" not in item,
            origin_dataset=dataset,
        )
        if dataset in {"item", "catalog"}:
            add(
                "item_versions",
                item,
                item_id=item_id,
                version_hash=digest(dumps(item).encode()),
                build_id=spec.context.get("build_id"),
            )
    if dataset == "markets":
        for row in data["markets"]:
            if row["game"] == "forever":
                add(
                    "markets",
                    row,
                    market=row["id"],
                    game=row["game"],
                    ruleset=row["ruleset"],
                    faction=row["faction"],
                    region=row["region"],
                    label=row["label"],
                    market_status="NOT_CHECKED",
                )
    elif dataset == "pricetable":
        for row in data["rows"]:
            add(
                "price_snapshots",
                row,
                market=spec.market,
                item_id=row["id"],
                observed_at=data["observedAt"],
                source="AHL1",
                min_copper=copper(row.get("minBuyout")),
                median_copper=copper(row.get("median")),
                quantity=row.get("quantity"),
                median7d_copper=copper(row.get("median7d")),
                median30d_copper=copper(row.get("median30d")),
                low30d_copper=copper(row.get("low30d")),
                high30d_copper=copper(row.get("high30d")),
                sales_per_day_x10=row.get("salesPerDayX10"),
                sales_estimated_per_day=row.get("salesPerDay"),
            )
    elif dataset == "history":
        for row in data["series"]:
            add(
                "price_history_ranges",
                row,
                market=spec.market,
                item_id=spec.item_id,
                observed_at=timestamp(row["t"]),
                range=spec.params["range"],
                granularity=parsed.granularity,
                resolution_seconds=parsed.interval_seconds,
                source=row.get("source", "AHledger"),
                observation_kind="scan_aggregate"
                if parsed.granularity == "scan"
                else "time_aggregate",
                currency="copper",
                min_copper=copper(row.get("min")),
                median_copper=copper(row.get("median")),
                p25_copper=copper(row.get("p25")),
                p75_copper=copper(row.get("p75")),
                quantity=row.get("quantity"),
                auctions=row.get("auctions"),
            )
        if data["latest"]:
            row = data["latest"]
            add(
                "price_snapshots",
                row,
                market=spec.market,
                item_id=spec.item_id,
                observed_at=timestamp(row["observedAt"]),
                source=row.get("source", "Addon scan"),
                min_copper=copper(row.get("minBuyout")),
                median_copper=copper(row.get("median")),
                quantity=row.get("quantity"),
                median7d_copper=copper(row.get("median7d")),
                median30d_copper=copper(row.get("median30d")),
                sales_estimated_per_day=data.get("sales", {}).get("estimatedPerDay"),
                sales_json=dumps(data.get("sales")),
            )
    elif dataset == "crafting":
        for row in data["recipes"]:
            recipe, result = row["recipe"], row["result"]
            mechanics = {
                "recipe": recipe,
                "result": {k: v for k, v in result.items() if k not in {"url", "chartUrl"}},
                "reagents": [{k: v for k, v in r.items() if k != "price"} for r in row["reagents"]],
            }
            version = digest(dumps(mechanics).encode())
            add("recipe_sources", {"spell_id": recipe["spellId"], "version_hash": version},
                spell_id=recipe["spellId"], version_hash=version, market=spec.market)
            add(
                "recipes",
                mechanics,
                spell_id=recipe["spellId"],
                name=recipe["name"],
                profession=recipe["profession"],
                skill_required=recipe.get("skillRequired"),
                acquisition=recipe.get("acquisition"),
                result_item_id=result.get("id"),
                qty_min=result.get("qtyMin"),
                qty_max=result.get("qtyMax"),
                version_hash=version,
            )
            for reagent in mechanics["reagents"]:
                add(
                    "recipe_inputs",
                    reagent,
                    spell_id=recipe["spellId"],
                    version_hash=version,
                    item_id=reagent["id"],
                    quantity=reagent["qty"],
                )
            add(
                "recipe_valuations",
                row,
                market=spec.market,
                spell_id=recipe["spellId"],
                observed_at=timestamp(data.get("pricesAsOf")),
                generated_at=timestamp(data["generatedAt"]),
                result_price_copper=copper(row.get("resultPrice")),
                cost_copper=copper(row.get("cost"), estimated=True),
                net_value_copper=copper(row.get("netValue"), estimated=True),
                margin_copper=copper(row.get("margin"), estimated=True),
                net_value_exact_copper=str(row["netValue"])
                if row.get("netValue") is not None
                else None,
                margin_exact_copper=str(row["margin"]) if row.get("margin") is not None else None,
                sales_estimated_per_day=row.get("resultSalesPerDay"),
                is_estimate=True,
            )
    elif dataset in {"economy", "economy_market"}:
        for row in data["daily"]:
            add(
                "market_economy",
                row,
                market=row.get("market", spec.market),
                observed_at=timestamp(row["day"]),
                granularity="day",
                value_copper=copper(row.get("value")),
                value_low_copper=copper(row.get("valueLow")),
                auctions=row.get("auctions"),
                units=row.get("units"),
                items=row.get("items"),
                scans=row.get("scans"),
                contributors=row.get("contributors"),
            )
        markets = data.get("markets", [data])
        for market in markets:
            if market.get("current"):
                row = market["current"]
                add(
                    "market_economy",
                    row,
                    market=market["market"],
                    observed_at=timestamp(row["observedAt"]),
                    granularity="rolling24h",
                    value_copper=copper(row.get("value")),
                    value_low_copper=copper(row.get("valueLow")),
                    auctions=row.get("auctions"),
                    units=row.get("units"),
                    items=row.get("items"),
                    scans=row.get("scansInWindow"),
                    contributors=row.get("contributors"),
                )
    elif dataset == "disenchant":
        for row in data["opportunities"]:
            item_id = row["item"]["id"]
            add(
                "disenchant",
                row,
                market=spec.market,
                item_id=item_id,
                observed_at=timestamp(row.get("observedAt", data.get("pricesAsOf"))),
                min_copper=copper(row.get("minBuyout")),
                expected_value_copper=copper(row.get("disenchantValue"), estimated=True),
                net_value_copper=copper(row.get("netValue"), estimated=True),
                profit_copper=copper(row.get("profit"), estimated=True),
                is_estimate=True,
            )
            for material in row["yields"]:
                mechanism = {k: v for k, v in material.items() if k != "median"}
                add(
                    "disenchant_mechanics",
                    mechanism,
                    item_id=item_id,
                    material_item_id=material["id"],
                    min_count=material["minCount"],
                    max_count=material["maxCount"],
                    chance=material["chance"],
                    version_hash=digest(dumps(mechanism).encode()),
                )
    elif dataset == "signals":
        for row in data["signals"]:
            add(
                "market_signals",
                row,
                market=spec.market,
                item_id=row["item"]["id"],
                observed_at=timestamp(data.get("pricesAsOf")),
                discount=data["filters"]["discount"],
                horizon_days=data["filters"]["days"],
                fires=row.get("fires"),
                wins=row.get("wins"),
                firing_now=row.get("firingNow"),
                median_copper=copper(row.get("median")),
                base_copper=copper(row.get("base"), estimated=True),
                base_exact_copper=str(row["base"]) if row.get("base") is not None else None,
                is_estimate=True,
            )
    elif dataset == "deals":
        for row in data["deals"]:
            add(
                "market_deals",
                row,
                market=spec.market,
                item_id=row["item"]["id"],
                observed_at=timestamp(row.get("observedAt", data.get("pricesAsOf"))),
                sort=data["filters"].get("sort"),
                min_copper=copper(row.get("minBuyout")),
                median_copper=copper(row.get("median")),
                median30d_copper=copper(row.get("median30d")),
                quantity=row.get("quantity"),
                saving_copper=copper(row.get("saving"), estimated=True),
                sales_estimated_per_day=row.get("salesPerDay"),
            )
    elif dataset == "structure":
        for row in data["items"]:
            add(
                "market_structure",
                row,
                market=spec.market,
                item_id=row["item"]["id"],
                observed_at=timestamp(data.get("pricesAsOf")),
                depth=row.get("depth"),
                median_copper=copper(row.get("median")),
                value_copper=copper(row.get("value")),
                spread=row.get("spread"),
                turnover=row.get("turnover"),
                volatility=row.get("volatility"),
                supply_shock=row.get("supplyShock"),
            )
    elif dataset == "builds":
        for row in data:
            add(
                "build_changes",
                row,
                build_id=row.get("guid", row.get("link")),
                title=row.get("title"),
                published_at=timestamp(row.get("observedAt")),
                url=row.get("link"),
                description=row.get("description"),
                is_structured_diff=False,
            )
    return tables


def arrow_schema(columns: dict[str, str]) -> pa.Schema:
    def type_for(sql):
        if sql == "BIGINT":
            return pa.int64()
        if sql == "BOOLEAN":
            return pa.bool_()
        if sql.startswith("DECIMAL"):
            precision, scale = map(int, sql[8:-1].split(","))
            return pa.decimal128(precision, scale)
        # Timestamps are supplied as ISO strings, cast by DuckDB in INSERT.
        return pa.string()

    return pa.schema([(key, type_for(value)) for key, value in columns.items()])


def analytics_connection(
    database: Path,
    settings: Settings,
    *,
    read_only: bool = False,
) -> duckdb.DuckDBPyConnection:
    # Spill on the archive volume, not /tmp (which may be a RAM-backed filesystem).
    spill = database.parent / (database.name + ".tmp")
    connection = duckdb.connect(
        str(database),
        read_only=read_only,
        config={
            "memory_limit": f"{settings.analytics_memory_mb}MiB",
            "threads": str(settings.analytics_threads),
            "preserve_insertion_order": "false",
            "temp_directory": str(spill),
        },
    )
    connection.execute("SET TimeZone='UTC'")
    return connection


def deduplicate(
    connection: duckdb.DuckDBPyConnection,
    source: str,
    target: str,
    key: str,
    order: str,
) -> None:
    # Materialize only row IDs and sort keys in the window operator. In particular,
    # never carry payload_json through a sort of the entire downloaded market.
    connection.execute(f'''CREATE TEMP TABLE _build_keep_rows AS
        SELECT rowid AS kept_rowid FROM "{source}"
        QUALIFY row_number() OVER (PARTITION BY {key} ORDER BY {order})=1''')
    try:
        connection.execute(f'''CREATE OR REPLACE TABLE "{target}" AS
            SELECT original.* FROM "{source}" original
            SEMI JOIN _build_keep_rows kept ON original.rowid=kept.kept_rowid''')
    finally:
        connection.execute("DROP TABLE _build_keep_rows")


def build_database(
    state: State,
    *,
    restore_state: bool = False,
    settings: Settings | None = None,
    base_database: Path | None = None,
    allowed_market: str | None = None,
    write_parquet: bool = True,
) -> dict:
    settings = settings or Settings.read(root=state.root)
    archive = RawArchive(state)
    if restore_state:
        archive.restore_state()
    directory = state.root / "analytics"
    directory.mkdir(parents=True, exist_ok=True)
    generation = uuid.uuid4().hex
    temp = directory / (".build-" + generation + ".duckdb")
    connection = analytics_connection(temp, settings)
    columns_by_table = {name: {**columns, **COMMON} for name, columns in SCHEMAS.items()}
    buffer: dict[str, list[dict]] = defaultdict(list)
    buffer_sizes: dict[str, int] = defaultdict(int)
    buffered_bytes = 0
    batch_bytes = settings.analytics_batch_mb * 1024 * 1024
    errors, quarantined, manifests = [], [], 0

    def flush(table):
        nonlocal buffered_bytes
        rows = buffer[table]
        if not rows:
            return
        columns = columns_by_table[table]
        for row in rows:
            for column, sql in columns.items():
                if sql.startswith("DECIMAL") and row.get(column) is not None:
                    scale = int(sql[8:-1].split(",")[1])
                    row[column] = Decimal(str(row[column])).quantize(Decimal(1).scaleb(-scale))
        arrow = pa.Table.from_pylist(rows, schema=arrow_schema(columns))
        connection.register("incoming_batch", arrow)
        try:
            connection.execute(f'INSERT INTO "{table}" SELECT * FROM incoming_batch')
        finally:
            connection.unregister("incoming_batch")
        buffer[table] = []
        buffered_bytes -= buffer_sizes[table]
        buffer_sizes[table] = 0

    def append(table: str, row: dict) -> None:
        nonlocal buffered_bytes
        size = sys.getsizeof(row) + sum(sys.getsizeof(value) for value in row.values())
        # Flush before adding a large row: the bound covers all table buffers together.
        if buffered_bytes + size > batch_bytes:
            for name in columns_by_table:
                flush(name)
        buffer[table].append(row)
        buffer_sizes[table] += size
        buffered_bytes += size
        if len(buffer[table]) >= settings.analytics_batch_rows or buffered_bytes >= batch_bytes:
            flush(table)

    def ingest(spec: JobSpec, meta: dict) -> None:
        # Parsed responses are released before reading the next compressed blob.
        parsed = parse(spec, archive.read(meta))
        normalized(spec, parsed, meta, emit=append)

    try:
        for table, columns in columns_by_table.items():
            fields = ",".join(f'"{column}" {sql}' for column, sql in columns.items())
            connection.execute(f'CREATE TABLE "{table}" ({fields})')
        if base_database is not None:
            source = base_database.resolve().as_posix().replace("'", "''")
            connection.execute(f"ATTACH '{source}' AS baseline (READ_ONLY)")
            baseline_tables = {row[0] for row in connection.execute("SELECT table_name FROM information_schema.tables WHERE table_catalog='baseline'").fetchall()}
            for table, columns in columns_by_table.items():
                if table not in baseline_tables:
                    if table == "recipe_sources":
                        connection.execute("""INSERT INTO recipe_sources SELECT r.spell_id,r.version_hash,m.market,
                            r.raw_id,r.sha256,r.retrieved_at,NULL FROM baseline.recipes r
                            JOIN baseline.dataset_metadata m ON r.raw_id=m.raw_id AND m.dataset='crafting'""")
                        continue
                    raise ValueError("Baseline is missing table: " + table)
                projection = ",".join(f'"{name}"' for name in columns)
                connection.execute(f'INSERT INTO "{table}" SELECT {projection} FROM baseline."{table}"')
            connection.execute("DETACH baseline")
        for meta in archive.manifests(ordered=False):
            manifests += 1
            spec = JobSpec.model_validate(meta["spec"])
            if allowed_market is not None and spec.market != allowed_market:
                raise ValueError("Update contains a response outside the selected market")
            if base_database is not None and connection.execute(
                "SELECT 1 FROM dataset_metadata WHERE raw_id=? LIMIT 1", [meta["id"]]
            ).fetchone():
                continue
            if meta["validation"] == "FAILED":
                # A rejected API response is a valid archived artifact, never a price observation.
                archive.read_sha(meta["sha256"])
                quarantined.append(
                    {"raw_id": meta["id"], "dataset": spec.dataset, "error": meta.get("error")}
                )
                normalized(
                    spec,
                    Parsed({}, status="FAILED", warnings=[meta.get("error", "Rejected response")]),
                    meta,
                    emit=append,
                )
                continue
            if meta["http_status"] != 200 and not (
                spec.dataset == "professions" and meta["http_status"] == 400
            ):
                continue
            try:
                ingest(spec, meta)
            except (ValueError, KeyError, TypeError, OSError) as exc:
                errors.append(
                    {"raw_id": meta["id"], "dataset": spec.dataset, "error": str(exc)[:2000]}
                )
        for row in state.db.execute("SELECT * FROM jobs"):
            append(
                "download_jobs",
                {
                    "job_id": row["id"],
                    "dataset": row["dataset"],
                    "market": row["market"],
                    "item_id": row["item_id"],
                    "priority": row["priority"],
                    "status": row["status"],
                    "attempts": row["attempts"],
                    "records": row["records"],
                    "error": row["error"],
                    "spec_json": row["spec"],
                    "raw_id": row["raw_id"],
                    "retrieved_at": row["updated_at"],
                },
            )
        for table in columns_by_table:
            flush(table)
        # Keep old build if any response failed to normalize; report all failures explicitly.
        for table, key in KEYS.items():
            order = "retrieved_at DESC,raw_id DESC"
            if table == "items":
                order = (
                    "is_placeholder ASC, CASE origin_dataset WHEN 'item' THEN 0 WHEN 'catalog' THEN 1 ELSE 2 END, "
                    + order
                )
            deduplicate(connection, table, table, key, order)
        if base_database is not None:
            deduplicate(connection, "download_jobs", "download_jobs", "job_id", "retrieved_at DESC,raw_id DESC")
        deduplicate(
            connection,
            "price_history_ranges",
            "price_history",
            "market,item_id,observed_at,granularity",
            "retrieved_at DESC, CASE \"range\" WHEN '90d' THEN 0 ELSE 1 END",
        )
        connection.execute("""CREATE VIEW preferred_price_history AS SELECT p.* FROM price_history p
            WHERE p.granularity='scan'
            OR (p.granularity='hour' AND NOT EXISTS (SELECT 1 FROM price_history s WHERE
                s.market=p.market AND s.item_id=p.item_id AND s.granularity='scan'
                AND date_trunc('hour',s.observed_at AT TIME ZONE 'UTC')=p.observed_at AT TIME ZONE 'UTC'))
            OR (p.granularity='day' AND NOT EXISTS (SELECT 1 FROM price_history s WHERE
                s.market=p.market AND s.item_id=p.item_id AND s.granularity IN ('scan','hour')
                AND date_trunc('day',s.observed_at AT TIME ZONE 'UTC')=p.observed_at AT TIME ZONE 'UTC'))""")
        connection.execute("""CREATE TABLE coverage AS
            SELECT market, CAST(observed_at AS DATE) AS date,'price_history' AS dataset,granularity,
                count(*) AS observations,count(DISTINCT item_id) AS items,
                'PARTIAL' AS status
            FROM price_history GROUP BY market,date,granularity
            UNION ALL SELECT market,CAST(observed_at AS DATE),'market_economy',granularity,count(*),0,'PARTIAL'
            FROM market_economy GROUP BY market,CAST(observed_at AS DATE),granularity""")
        for market, status in state.get("market_status", {}).items():
            connection.execute(
                "UPDATE markets SET market_status=? WHERE market=?", (status, market)
            )
        # No public bulk endpoint exists for these facts. Empty tables are intentional.
        connection.execute(
            "CREATE TABLE source_availability(dataset VARCHAR,status VARCHAR,reason VARCHAR,contact_url VARCHAR)"
        )
        for name, reason in (
            (
                "raw_auction_scans",
                "No documented public bulk export; server archive is not a public endpoint",
            ),
            (
                "item_sources",
                "NPC/vendors/drops/quests/zones shown on site; no documented bulk API",
            ),
            (
                "historical_item_build_diffs",
                "RSS summaries only; no documented structured historical item/recipe dump",
            ),
        ):
            connection.execute(
                "INSERT INTO source_availability VALUES (?,?,?,?)",
                (name, "NOT_AVAILABLE", reason, "https://ahledger.com/contact"),
            )
        counts = {}
        for table in [*SCHEMAS, "price_history", "coverage"]:
            row = connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()
            assert row is not None
            counts[table] = row[0]
        connection.execute("CHECKPOINT")
        # Finish even the expensive preferred-history export before publishing the DB.
        parquet_output = _write_parquet(connection, state.root) if write_parquet and not errors else None
    finally:
        connection.close()
    result = {
        "generated_at": utc_now(),
        "manifests": manifests,
        "counts": counts,
        "errors": errors,
        "quarantined": quarantined,
        "merged_from": str(base_database) if base_database else None,
        "resources": {
            "memory_limit_mb": settings.analytics_memory_mb,
            "threads": settings.analytics_threads,
            "batch_rows": settings.analytics_batch_rows,
            "batch_memory_mb": settings.analytics_batch_mb,
            "spill_directory": str(temp.parent / (temp.name + ".tmp")),
        },
    }
    atomic_json(directory / "build_result.json", result)
    if errors:
        # Failed rebuild is retained for diagnostics; never replace a sound previous generation.
        raise ValueError(
            f"Rebuild has {len(errors)} invalid raw response(s); see analytics/build_result.json and {temp.name}"
        )
    for attempt in range(30):
        try:
            os.replace(temp, directory / "ahledger.duckdb")
            break
        except PermissionError:
            if attempt == 29:
                raise
            time.sleep(0.1)
    if parquet_output is not None:
        _publish_parquet(state.root, parquet_output)
    return result


def database_update_id(connection: duckdb.DuckDBPyConnection) -> str | None:
    if not connection.execute("SELECT 1 FROM information_schema.tables WHERE table_name='_applied_updates'").fetchone():
        return None
    row = connection.execute("SELECT update_id FROM _applied_updates ORDER BY applied_at DESC, rowid DESC LIMIT 1").fetchone()
    return row[0] if row else None


def _write_parquet(connection: duckdb.DuckDBPyConnection, root: Path) -> Path:
    directory = root / "parquet"
    generation = uuid.uuid4().hex
    output = directory / generation
    output.mkdir(parents=True, exist_ok=True)
    existing_tables = {row[0] for row in connection.execute("SHOW TABLES").fetchall()}
    for table in [
        *SCHEMAS,
        "price_history",
        "preferred_price_history",
        "coverage",
        "source_availability",
    ]:
        if table == "recipe_sources" and table not in existing_tables:
            continue  # Imported databases built before source-provenance tracking.
        file = (output / (table + ".parquet")).resolve().as_posix().replace("'", "''")
        connection.execute(
            f'''COPY (SELECT * FROM "{table}") TO '{file}'
                (FORMAT PARQUET,COMPRESSION ZSTD,ROW_GROUP_SIZE 16384)'''
        )
    return output


def _publish_parquet(root: Path, output: Path, *, update_id: str | None = None) -> None:
    # Generation directories + atomic pointer prevent a mixed old/new set after Ctrl+C.
    atomic_json(
        root / "parquet" / "CURRENT.json",
        {
            "directory": output.name,
            "generated_at": utc_now(),
            "database_update_id": update_id,
        },
    )


def export_parquet(state: State, *, settings: Settings | None = None) -> Path:
    settings = settings or Settings.read(root=state.root)
    connection = analytics_connection(
        state.root / "analytics" / "ahledger.duckdb",
        settings,
        read_only=True,
    )
    try:
        output = _write_parquet(connection, state.root)
        update_id = database_update_id(connection)
    finally:
        connection.close()
    _publish_parquet(state.root, output, update_id=update_id)
    return output
