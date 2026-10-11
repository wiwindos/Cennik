"""Build a bounded, real-data development archive without expanding the full queue."""

from __future__ import annotations

import argparse
import asyncio
import re
import shutil
import time
from pathlib import Path

import duckdb

from ahledger.analytics import build_database
from ahledger.config import API, SITE, Settings
from ahledger.models import JobSpec
from ahledger.parsers import parse
from ahledger.report import create_report
from ahledger.runner import Downloader
from ahledger.storage import TERMINAL, RawArchive, State, job_key
from ahledger.util import atomic_json, dumps, exclusive_lock, loads, utc_now
from ahledger.verify import verify

DEFAULT_MARKET = "forever.normal.alliance.us"
SHARED = {
    "markets",
    "catalog_index",
    "catalog",
    "professions",
    "item",
    "builds",
    "documentation",
    "llms",
}


def import_saved(source: Path, state: State, market: str) -> int:
    """Read source files only; do not open or recover its live SQLite queue."""
    archive = RawArchive(state)
    available = {
        meta["id"]: meta
        for path in (source / "raw/metadata").glob("*.json")
        if (meta := loads(path.read_bytes())).get("validation") in TERMINAL
    }
    imported: set[str] = set()

    def copy(meta):
        raw_id = meta["id"]
        if raw_id in imported:
            return
        if not re.fullmatch(r"[0-9a-f]{32}", raw_id):
            raise ValueError("Invalid saved raw ID")
        if reused := meta.get("reused_raw_id"):
            copy(available[reused])
        for sha in {meta["sha256"], meta.get("reused_sha256")} - {None}:
            if not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise ValueError("Invalid saved SHA-256")
            relative = Path("blobs") / sha[:2] / (sha + ".zst")
            dest = archive.root / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copyfile(source / "raw" / relative, dest)
        archive.write_metadata(meta)
        imported.add(raw_id)

    for meta in available.values():
        spec = JobSpec.model_validate(meta["spec"])
        if spec.market == market or (spec.market is None and spec.dataset in SHARED):
            copy(meta)
    archive.restore_state()
    state.set("active_markets", [market])
    return len(imported)


def small_jobs(state: State, market: str) -> list[JobSpec]:
    jobs = []
    for slug, info in state.get("catalog_classes", {}).items():
        jobs.append(
            JobSpec(
                dataset="catalog",
                url=SITE + "/api/catalog",
                params={"class": slug},
                context={"class_name": info["name"], "expected_count": info["count"]},
            )
        )
    for profession in state.get("professions", []):
        jobs.append(
            JobSpec(
                dataset="crafting",
                url=f"{API}/v1/crafting/{market}",
                market=market,
                params={"profession": profession, "all": "1"},
            )
        )
    for dataset, endpoint, params in (
        ("pricetable", "pricetable", {}),
        ("economy_market", "economy", {}),
        ("disenchant", "disenchant", {"all": "true"}),
        ("structure", "structure", {}),
        *(
            (
                "deals",
                "deals",
                {"discount": "15", "units": "1", "sells": "0", "sort": sort, "limit": "200"},
            )
            for sort in ("saving", "discount", "sales")
        ),
        *(
            ("signals", "signals", {"discount": str(discount), "days": str(days)})
            for discount in (10, 20, 30, 40)
            for days in (3, 7, 14)
        ),
    ):
        jobs.append(
            JobSpec(
                dataset=dataset, url=f"{API}/v1/{endpoint}/{market}", market=market, params=params
            )
        )
    jobs.append(JobSpec(dataset="builds", url=SITE + "/feed/builds.xml"))
    return jobs


def choose_items(state: State, market: str, count: int) -> list[int]:
    """Evenly sample priced catalog items across class/quality, with a known reference item."""
    archive = RawArchive(state)
    snapshots = [
        m
        for m in archive.manifests()
        if m["spec"]["dataset"] == "pricetable"
        and m["spec"].get("market") == market
        and m["http_status"] == 200
        and m["validation"] in TERMINAL
    ]
    if not snapshots:
        raise ValueError("No valid price table for the selected market")
    meta = max(snapshots, key=lambda m: (m["retrieved_at"], m["id"]))
    parsed = parse(JobSpec.model_validate(meta["spec"]), archive.read(meta))
    catalog = {
        row["id"]: loads(row["payload"])
        for row in state.db.execute("SELECT id,payload FROM item_candidates")
    }
    ids = sorted(
        parsed.items,
        key=lambda item: (
            str(catalog.get(item, {}).get("itemClass", "")),
            str(catalog.get(item, {}).get("quality", "")),
            item,
        ),
    )
    if not ids:
        raise ValueError("Selected market has no priced items")
    count = min(count, len(ids))
    result = [ids[(i * len(ids)) // count] for i in range(count)]
    if 2589 in ids and 2589 not in result:
        result[-1] = 2589
    return result


async def fetch_jobs(downloader: Downloader, jobs: list[JobSpec], deadline: float):
    state = downloader.state
    for spec in jobs:
        state.enqueue(spec)
    state.db.commit()
    # Explicit finite list; never call planner/absorb or Downloader.run here.
    for spec in jobs:
        row = state.db.execute("SELECT * FROM jobs WHERE id=?", (job_key(spec),)).fetchone()
        if row["status"] in TERMINAL or row["status"] == "FAILED":
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if row["not_before"] > time.time():
            continue
        try:
            async with asyncio.timeout(remaining):
                await downloader.process(row)
        except TimeoutError:
            break
        if downloader.completed % 20 == 0:
            print(
                dumps(
                    {
                        "network_attempts": downloader.completed,
                        "remaining_seconds": round(deadline - time.monotonic(), 1),
                    }
                ),
                flush=True,
            )
    RawArchive(state).restore_state()


async def collect(state: State, market: str, count: int, seconds: float) -> list[int]:
    settings = Settings(
        data_dir=state.root,
        markets=[market],
        concurrency=1,
        full_history_check=False,
        enrich_items="none",
        max_attempts=2,
    )
    downloader = Downloader(settings, state)
    downloader.expand = False
    deadline = time.monotonic() + seconds
    try:
        await fetch_jobs(
            downloader,
            [
                JobSpec(dataset="markets", url=API + "/v1/markets"),
                JobSpec(dataset="catalog_index", url=SITE + "/wow-forever/items"),
                JobSpec(
                    dataset="professions",
                    url=f"{API}/v1/crafting/{market}",
                    params={"profession": "invalid", "all": "1"},
                ),
            ],
            deadline,
        )
        if market not in {m["id"] for m in state.get("markets", [])}:
            raise ValueError("Unknown market")
        await fetch_jobs(downloader, small_jobs(state, market), deadline)
        selected = state.get("sample_item_ids") or choose_items(state, market, count)
        state.set("sample_item_ids", selected)
        histories = [
            JobSpec(
                dataset="history",
                url=f"{API}/v1/prices/{market}/{item}",
                market=market,
                item_id=item,
                params={"range": "90d"},
            )
            for item in selected
        ]
        # Interleave detailed cards so a bounded interruption leaves useful joins.
        jobs = []
        for spec in histories:
            jobs.extend(
                [
                    spec,
                    JobSpec(
                        dataset="item",
                        url=f"{API}/v1/items/{spec.item_id}",
                        item_id=spec.item_id,
                        params={"game": "forever"},
                    ),
                ]
            )
        # A few finer series exercise scan/hour/day schema without downloading every range.
        jobs.extend(
            JobSpec(
                dataset="history",
                url=f"{API}/v1/prices/{market}/{item}",
                market=market,
                item_id=item,
                params={"range": range_name},
            )
            for item in selected[:3]
            for range_name in ("7d", "24h")
        )
        await fetch_jobs(downloader, jobs, deadline)
        state.set("active_markets", [market])
        return selected
    finally:
        await downloader.client.close()


def describe(state: State, market: str, selected: list[int]) -> dict:
    db = duckdb.connect(str(state.root / "analytics/ahledger.duckdb"), read_only=True)
    db.execute("SET TimeZone='UTC'")
    try:
        history = db.execute(
            "SELECT count(*),count(DISTINCT item_id),min(observed_at),max(observed_at) FROM price_history"
        ).fetchall()[0]
        foreign = db.execute(
            "SELECT DISTINCT market FROM price_history UNION SELECT DISTINCT market FROM price_snapshots UNION SELECT DISTINCT market FROM market_economy UNION SELECT DISTINCT market FROM recipe_valuations"
        ).fetchall()
        if any(row[0] != market for row in foreign):
            raise ValueError("Another market leaked into the sample")
        counts = {
            name: db.execute(f'SELECT count(*) FROM "{name}"').fetchall()[0][0]
            for name in (
                "items",
                "item_versions",
                "recipes",
                "recipe_inputs",
                "price_snapshots",
                "market_economy",
                "market_deals",
                "market_signals",
                "market_structure",
                "disenchant",
                "disenchant_mechanics",
                "build_changes",
            )
        }
        selected_rows = db.execute(
            "SELECT count(DISTINCT item_id) FROM price_history_ranges WHERE \"range\"='90d' AND item_id IN (SELECT unnest(?))",
            [selected],
        ).fetchall()[0][0]
        missing_catalog = state.db.execute(
            "SELECT count(*) FROM jobs WHERE dataset='catalog' AND status NOT IN ('COMPLETE','PARTIAL')"
        ).fetchone()[0]
        return {
            "generated_at": utc_now(),
            "market": market,
            "synthetic": False,
            "selected_item_ids": selected,
            "selected_items_with_90d_history": selected_rows,
            "history_points": history[0],
            "history_items": history[1],
            "earliest_utc": history[2].isoformat() if history[2] else None,
            "latest_utc": history[3].isoformat() if history[3] else None,
            "counts": counts,
            "jobs": state.counts(),
            "unsuccessful_catalog_jobs": missing_catalog,
            "notes": [
                "Development sample, not a full market archive.",
                "90d is a requested range, not a guarantee of 90 days of scans.",
                "Compact catalog is global; detailed cards cover the sample only.",
                "API top-N limits and gaps are recorded in dataset_metadata.",
                "All prices are integer copper; timestamps are UTC.",
                "Prices from AHledger: https://ahledger.com",
            ],
        }
    finally:
        db.close()


def write_guide(root: Path, summary: dict):
    """Portable schema and starter queries alongside the actual development database."""
    db = duckdb.connect(str(root / "analytics/ahledger.duckdb"), read_only=True)
    try:
        schemas = db.execute(
            "SELECT sql FROM duckdb_tables() WHERE NOT internal AND schema_name='main' ORDER BY table_name"
        ).fetchall()
        schemas += db.execute(
            "SELECT sql FROM duckdb_views() WHERE NOT internal AND schema_name='main' ORDER BY view_name"
        ).fetchall()
        (root / "schema.sql").write_text(
            "\n\n".join(row[0].rstrip(";") + ";" for row in schemas) + "\n", encoding="utf-8"
        )
    finally:
        db.close()
    (root / "queries.sql").write_text(
        """-- Prices from AHledger: https://ahledger.com
SET TimeZone='UTC';

-- Actual coverage; do not assume the requested 90 days are populated.
SELECT granularity, count(*) AS points, count(DISTINCT item_id) AS items,
       min(observed_at) AS earliest, max(observed_at) AS latest
FROM price_history GROUP BY granularity ORDER BY granularity;

-- Latest saved price per item, including its catalog name.
WITH latest AS (
    SELECT * FROM price_snapshots
    QUALIFY row_number() OVER (PARTITION BY market,item_id
        ORDER BY observed_at DESC,retrieved_at DESC,source)=1
)
SELECT p.item_id,i.name,p.market,p.observed_at,p.min_copper,p.median_copper,p.quantity
FROM latest p JOIN items i USING(item_id)
ORDER BY p.quantity DESC NULLS LAST LIMIT 100;

-- Linen Cloth: day/hour/scan points stay separate; amounts are integer copper.
SELECT observed_at,granularity,min_copper,median_copper,quantity,auctions
FROM price_history WHERE item_id=2589 ORDER BY observed_at,granularity;

-- Recipe composition and named reagents, independently of market valuation.
SELECT r.spell_id,r.name,r.profession,result.name AS result_name,
       reagent.name AS reagent_name,inputs.quantity
FROM recipes r JOIN recipe_inputs inputs USING(spell_id,version_hash)
LEFT JOIN items result ON result.item_id=r.result_item_id
LEFT JOIN items reagent ON reagent.item_id=inputs.item_id
ORDER BY r.profession,r.spell_id,inputs.item_id LIMIT 100;

-- Endpoint truncation, missing time buckets and unavailable source data.
SELECT dataset,market,status,record_count,warnings_json,gaps_json
FROM dataset_metadata WHERE status<>'COMPLETE' ORDER BY dataset,market;
SELECT * FROM source_availability;
""",
        encoding="utf-8",
    )
    counts = summary["counts"]
    (root / "README.txt").write_text(
        f"""Реальная база для разработки аналитики: {summary["market"]}
Источник цен: https://ahledger.com
Синтетических цен и заполнения пропусков нет. Это выборка, а не полная выкачка рынка.

Основной файл: analytics/ahledger.duckdb
Экспорт: parquet/<generation>/*.parquet; актуальная папка указана в parquet/CURRENT.json.
Схема: schema.sql. Примеры запросов: queries.sql. Покрытие: SAMPLE.json.
Raw-ответы с SHA-256: raw/. Очередь только этой выборки: state.sqlite.
Для аналитического сервера читай DuckDB или Parquet, а не очередь SQLite.
Можно перенести всю эту папку на Linux; пути в исходных данных не зависят от Windows.

Предметы: {counts["items"]}; рецепты: {counts["recipes"]}; компоненты рецептов: {counts["recipe_inputs"]}.
История: {summary["history_points"]} точек для {summary["history_items"]} предметов,
из выбранных {len(summary["selected_item_ids"])} предметов дневная история 90d непустая у {summary["selected_items_with_90d_history"]}.
Фактический период UTC: {summary["earliest_utc"]} — {summary["latest_utc"]}.

Компактный каталог содержит названия/качество/категорию/уровень и исходный payload.
Подробные карточки загружены для выборки; неизвестные свойства остаются NULL.
Рецепты и рыночные инструменты ограничены доступными ответами API (500/200/100/60 строк).
PARTIAL в dataset_metadata обозначает лимит, пропуски или отсутствие полного структурированного diff.
Справочник markets содержит все рынки; ценовые факты относятся только к выбранному.
Деньги хранятся в copper (10 000 = 1 gold), время — UTC. Нельзя складывать day/hour/scan
за один период: используй отдельную детализацию либо preferred_price_history с учётом её оговорок.

Python (установить duckdb, выполнить из этой папки):
    import duckdb
    db = duckdb.connect('analytics/ahledger.duckdb', read_only=True)
    db.execute("SET TimeZone='UTC'")
    print(db.execute('SELECT COUNT(*) FROM items').fetchone())
    db.close()

Для подключения существующей веб-панели задай AHL_DATA_DIR равным пути этой папки.
Панель сама ничего не скачивает. После завершения полной выкачки сервер сможет читать
тот же формат таблиц из основного архива.
""",
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, default=Path("data/samples/normal-alliance-us"))
    parser.add_argument("--market", default=DEFAULT_MARKET)
    parser.add_argument("--items", type=int, choices=range(1, 501), metavar="1..500", default=100)
    parser.add_argument("--max-seconds", type=float, default=300)
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    if source == output or output in source.parents:
        parser.error("Output must be separate from the source archive")
    if not 1 <= args.max_seconds <= 300:
        parser.error("Network budget must be between 1 and 300 seconds")
    marker = output / "SAMPLE.json"
    if output.exists() and any(output.iterdir()) and not marker.exists():
        parser.error("Refusing to modify a directory that is not a sample archive")
    if marker.exists():
        old = loads(marker.read_bytes())
        if old["market"] != args.market or old["requested_items"] != args.items:
            parser.error("Existing sample has a different market or size; use another output")
    with exclusive_lock(output):
        atomic_json(
            marker, {"market": args.market, "requested_items": args.items, "status": "preparing"}
        )
        state = State(output)
        try:
            print(
                dumps({"imported_saved_responses": import_saved(source, state, args.market)}),
                flush=True,
            )
            selected = asyncio.run(collect(state, args.market, args.items, args.max_seconds))
            result = build_database(state)
            checks = verify(state)
            summary = describe(state, args.market, selected)
            summary.update(
                requested_items=args.items,
                verification=checks["integrity"],
                status="ready" if not checks["errors"] else "failed",
            )
            atomic_json(marker, summary)
            write_guide(output, summary)
            create_report(state)
            print(
                dumps(
                    {"sample": str(output), "summary": summary, "build_errors": result["errors"]}
                ),
                flush=True,
            )
            if checks["errors"]:
                raise SystemExit(1)
        finally:
            state.close()


if __name__ == "__main__":
    main()
