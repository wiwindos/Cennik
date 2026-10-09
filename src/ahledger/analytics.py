from __future__ import annotations

import os
import uuid
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import duckdb
import pyarrow as pa

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


def normalized(spec: JobSpec, parsed: Parsed, meta: dict) -> dict[str, list[dict]]:
    tables: dict[str, list[dict]] = defaultdict(list)
    common = {
        "raw_id": meta["id"],
        "sha256": meta.get("reused_sha256") or meta["sha256"],
        "retrieved_at": meta["retrieved_at"],
    }

    def add(table, payload, **fields):
        tables[table].append({**common, "payload_json": dumps(payload), **fields})

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


def build_database(state: State, *, restore_state: bool = False) -> dict:
    archive = RawArchive(state)
    if restore_state:
        archive.restore_state()
    directory = state.root / "analytics"
    directory.mkdir(parents=True, exist_ok=True)
    generation = uuid.uuid4().hex
    temp = directory / (".build-" + generation + ".duckdb")
    connection = duckdb.connect(str(temp))
    connection.execute("SET TimeZone='UTC'")
    columns_by_table = {name: {**columns, **COMMON} for name, columns in SCHEMAS.items()}
    buffer: dict[str, list[dict]] = defaultdict(list)
    errors, quarantined, manifests, points = [], [], 0, 0

    def flush(table):
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
        connection.execute(f'INSERT INTO "{table}" SELECT * FROM incoming_batch')
        connection.unregister("incoming_batch")
        buffer[table] = []

    try:
        for table, columns in columns_by_table.items():
            fields = ",".join(f'"{column}" {sql}' for column, sql in columns.items())
            connection.execute(f'CREATE TABLE "{table}" ({fields})')
        for meta in archive.manifests():
            manifests += 1
            spec = JobSpec.model_validate(meta["spec"])
            if meta["validation"] == "FAILED":
                # A rejected API response is a valid archived artifact, never a price observation.
                archive.read_sha(meta["sha256"])
                quarantined.append(
                    {"raw_id": meta["id"], "dataset": spec.dataset, "error": meta.get("error")}
                )
                buffer["dataset_metadata"].extend(
                    normalized(
                        spec,
                        Parsed(
                            {}, status="FAILED", warnings=[meta.get("error", "Rejected response")]
                        ),
                        meta,
                    )["dataset_metadata"]
                )
                if len(buffer["dataset_metadata"]) >= 10000:
                    flush("dataset_metadata")
                continue
            if meta["http_status"] != 200 and not (
                spec.dataset == "professions" and meta["http_status"] == 400
            ):
                continue
            try:
                parsed = parse(spec, archive.read(meta))
                tables = normalized(spec, parsed, meta)
                points += len(tables.get("price_history_ranges", []))
                for table, rows in tables.items():
                    buffer[table].extend(rows)
                    if len(buffer[table]) >= 10000:
                        flush(table)
            except (ValueError, KeyError, TypeError, OSError) as exc:
                errors.append(
                    {"raw_id": meta["id"], "dataset": spec.dataset, "error": str(exc)[:2000]}
                )
        for row in state.db.execute("SELECT * FROM jobs"):
            buffer["download_jobs"].append(
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
                }
            )
            if len(buffer["download_jobs"]) >= 10000:
                flush("download_jobs")
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
            connection.execute(f'''CREATE OR REPLACE TABLE "{table}" AS
                SELECT * FROM "{table}" QUALIFY row_number() OVER (PARTITION BY {key} ORDER BY {order})=1''')
        connection.execute("""CREATE TABLE price_history AS SELECT * FROM price_history_ranges
            QUALIFY row_number() OVER (PARTITION BY market,item_id,observed_at,granularity
                ORDER BY retrieved_at DESC, CASE "range" WHEN '90d' THEN 0 ELSE 1 END)=1""")
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
    finally:
        connection.close()
    result = {
        "generated_at": utc_now(),
        "manifests": manifests,
        "counts": counts,
        "errors": errors,
        "quarantined": quarantined,
    }
    atomic_json(directory / "build_result.json", result)
    if errors:
        # Failed rebuild is retained for diagnostics; never replace a sound previous generation.
        raise ValueError(
            f"Rebuild has {len(errors)} invalid raw response(s); see analytics/build_result.json and {temp.name}"
        )
    os.replace(temp, directory / "ahledger.duckdb")
    export_parquet(state)
    return result


def export_parquet(state: State) -> Path:
    directory = state.root / "parquet"
    generation = uuid.uuid4().hex
    output = directory / generation
    output.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(state.root / "analytics" / "ahledger.duckdb"), read_only=True)
    try:
        for table in [
            *SCHEMAS,
            "price_history",
            "preferred_price_history",
            "coverage",
            "source_availability",
        ]:
            file = (output / (table + ".parquet")).resolve().as_posix().replace("'", "''")
            connection.execute(
                f'''COPY (SELECT * FROM "{table}") TO '{file}' (FORMAT PARQUET,COMPRESSION ZSTD)'''
            )
    finally:
        connection.close()
    # Generation directories + atomic pointer prevent a mixed old/new set after Ctrl+C.
    atomic_json(directory / "CURRENT.json", {"directory": generation, "generated_at": utc_now()})
    return output
