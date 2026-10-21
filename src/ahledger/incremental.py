"""Apply a validated, small snapshot delta without rebuilding historical tables."""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path

import duckdb

from .analytics import COMMON, KEYS, SCHEMAS, analytics_connection, database_update_id, deduplicate
from .config import Settings
from .util import dumps, exclusive_lock, utc_now


def separate_shared_file(path: Path) -> bool:
    """An imported hardlink must never turn a write into a change to the original."""
    if path.stat().st_nlink <= 1:
        return False
    if Path(str(path) + ".wal").exists():
        raise ValueError("Shared database has a WAL; recover it before updating.")
    temporary = path.with_name(".private-" + uuid.uuid4().hex + ".duckdb")
    try:
        with path.open("rb") as source, temporary.open("wb") as target:
            shutil.copyfileobj(source, target, 1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def key_match(key: str, left: str, right: str) -> str:
    return " AND ".join(
        f'{left}."{column.strip(chr(34))}" IS NOT DISTINCT FROM {right}."{column.strip(chr(34))}"'
        for column in key.split(",")
    )


def row_count(db: duckdb.DuckDBPyConnection, table: str) -> int:
    row = db.execute(f'SELECT count(*) FROM "{table}"').fetchone()
    assert row is not None
    return row[0]


def merge_table(db: duckdb.DuckDBPyConnection, table: str, key: str) -> None:
    """Rank only rows whose keys occur in the delta, using the full build's rules."""
    db.execute(f'CREATE TEMP TABLE affected_keys AS SELECT DISTINCT {key} FROM incoming')
    db.execute(f'''CREATE TEMP TABLE candidates AS
        SELECT old.* FROM "{table}" old SEMI JOIN affected_keys k ON {key_match(key, "old", "k")}
        UNION ALL SELECT * FROM incoming''')
    order = "retrieved_at DESC,raw_id DESC"
    if table == "items":
        order = "is_placeholder ASC, CASE origin_dataset WHEN 'item' THEN 0 WHEN 'catalog' THEN 1 ELSE 2 END, " + order
    deduplicate(db, "candidates", "winners", key, order)
    db.execute(f'DELETE FROM "{table}" old USING affected_keys k WHERE {key_match(key, "old", "k")}')
    db.execute(f'INSERT INTO "{table}" SELECT * FROM winners')
    for name in ("affected_keys", "candidates", "winners"):
        db.execute(f'DROP TABLE "{name}"')


def check_touched(db: duckdb.DuckDBPyConnection, tables: list[str], market: str) -> None:
    """Validate the resulting rows affected by this batch before committing."""
    for table in tables:
        key = KEYS.get(table, "job_id" if table == "download_jobs" else "raw_id")
        scoped = f'FROM "{table}" t SEMI JOIN delta."{table}" d ON {key_match(key, "t", "d")}'
        duplicate = db.execute(f'SELECT 1 {scoped} GROUP BY {",".join("t." + c for c in key.split(","))} HAVING count(*)>1 LIMIT 1').fetchone()
        if duplicate:
            raise ValueError("Duplicate keys after update: " + table)
        if "market" in SCHEMAS[table] and db.execute(
            f'SELECT 1 {scoped} WHERE t.market IS DISTINCT FROM ? LIMIT 1', [market]
        ).fetchone():
            raise ValueError("Update changed rows outside selected market: " + table)
        if table in {"price_snapshots", "market_economy", "recipe_valuations", "disenchant", "market_signals", "market_deals", "market_structure"}:
            if db.execute(f'SELECT 1 {scoped} WHERE t.observed_at IS NULL LIMIT 1').fetchone():
                raise ValueError("Missing observation timestamp after update: " + table)
        if table == "price_snapshots":
            if db.execute(f'SELECT 1 {scoped} WHERE t.min_copper<0 OR t.median_copper<0 OR t.quantity<0 LIMIT 1').fetchone():
                raise ValueError("Invalid price or quantity after update")
        for column in ("item_id", "result_item_id", "material_item_id"):
            if table != "items" and column in SCHEMAS[table]:
                if db.execute(f'SELECT 1 {scoped} WHERE t."{column}" IS NOT NULL AND NOT EXISTS (SELECT 1 FROM items i WHERE i.item_id=t."{column}") LIMIT 1').fetchone():
                    raise ValueError("Missing item referenced by " + table)
        if table in {"recipe_inputs", "recipe_sources"}:
            if db.execute(f'SELECT 1 {scoped} WHERE NOT EXISTS (SELECT 1 FROM recipes r WHERE r.spell_id=t.spell_id AND r.version_hash=t.version_hash) LIMIT 1').fetchone():
                raise ValueError("Missing recipe referenced by " + table)


def apply_delta(settings: Settings, delta: Path, identity: str, batch_digest: str, market: str,
                delta_counts: dict, market_status: str | None) -> dict:
    destination = settings.data_dir / "analytics/ahledger.duckdb"
    # Same lock order as analytics.start(): take its calculation lock before archive access.
    with exclusive_lock(settings.data_dir / "cennik"), exclusive_lock(settings.data_dir):
        detached = separate_shared_file(destination)
        deadline = time.monotonic() + 10
        while True:
            try:
                db = analytics_connection(destination, settings)
                break
            except duckdb.IOException as exc:
                if "lock" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)  # Let an already running short read finish.
        committed = False
        started = time.monotonic()
        try:
            db.execute("BEGIN TRANSACTION")
            db.execute("""CREATE TABLE IF NOT EXISTS _applied_updates(
                update_id VARCHAR PRIMARY KEY,batch_digest VARCHAR,market VARCHAR,
                applied_at TIMESTAMPTZ,result_json VARCHAR)""")
            previous = db.execute("SELECT batch_digest,market,result_json FROM _applied_updates WHERE update_id=?", [identity]).fetchone()
            if previous:
                if previous[:2] != (batch_digest, market):
                    raise ValueError("Applied update ID has different contents or market")
                db.execute("ROLLBACK")
                return json.loads(previous[2]) | {"reused": True}
            source = delta.resolve().as_posix().replace("'", "''")
            db.execute(f"ATTACH '{source}' AS delta (READ_ONLY)")
            if not db.execute("SELECT 1 FROM markets WHERE market=?", [market]).fetchone():
                raise ValueError("Рынок отсутствует в текущей базе.")
            tables = {r[0] for r in db.execute("SELECT table_name FROM information_schema.tables WHERE table_catalog=current_database()").fetchall()}
            if "recipe_sources" not in tables:
                columns = {**SCHEMAS["recipe_sources"], **COMMON}
                fields = ",".join(f'"{c}" {t}' for c, t in columns.items())
                db.execute(f'CREATE TABLE recipe_sources ({fields})')
                db.execute("""INSERT INTO recipe_sources SELECT DISTINCT r.spell_id,r.version_hash,m.market,
                    r.raw_id,r.sha256,r.retrieved_at,NULL FROM recipes r
                    JOIN dataset_metadata m ON r.raw_id=m.raw_id AND m.dataset='crafting'""")
            # Capture applied raw IDs before dataset_metadata changes during the loop.
            db.execute("""CREATE TEMP TABLE new_raw AS SELECT DISTINCT d.raw_id FROM delta.dataset_metadata d
                WHERE NOT EXISTS (SELECT 1 FROM dataset_metadata m WHERE m.raw_id=d.raw_id)""")
            touched = []
            for table in SCHEMAS:
                selection = f'SELECT d.* FROM delta."{table}" d'
                if table != "download_jobs":
                    selection += ' SEMI JOIN new_raw n ON d.raw_id=n.raw_id'
                db.execute('CREATE TEMP TABLE incoming AS ' + selection)
                count = row_count(db, "incoming")
                if count:
                    if table == "price_history_ranges":
                        raise ValueError("Snapshot update must not include historical ranges")
                    key = KEYS.get(table, "job_id" if table == "download_jobs" else None)
                    if key:
                        merge_table(db, table, key)
                    else:
                        db.execute(f'INSERT INTO "{table}" SELECT * FROM incoming')
                    touched.append(table)
                db.execute("DROP TABLE incoming")
            if market_status:
                db.execute("UPDATE markets SET market_status=? WHERE market=?", [market_status, market])
            check_touched(db, touched, market)
            # Snapshot-only updates never touch price_history, its view or coverage.
            # Refresh just the economy dates present in this delta.
            if "market_economy" in touched:
                db.execute("""DELETE FROM coverage c USING delta.market_economy d WHERE c.dataset='market_economy'
                    AND c.market=d.market AND c.date=CAST(d.observed_at AS DATE) AND c.granularity=d.granularity""")
                db.execute("""INSERT INTO coverage SELECT e.market,CAST(e.observed_at AS DATE),'market_economy',
                    e.granularity,count(*),0,'PARTIAL' FROM market_economy e
                    SEMI JOIN delta.market_economy d ON e.market=d.market AND CAST(e.observed_at AS DATE)=CAST(d.observed_at AS DATE)
                    AND e.granularity=d.granularity GROUP BY e.market,CAST(e.observed_at AS DATE),e.granularity""")
            result = {"generated_at": utc_now(), "update_id": identity, "updated_market": market,
                      "updated_at": utc_now(), "mode": "incremental", "reused": False,
                      "delta_counts": delta_counts, "touched_tables": touched,
                      "counts": {t: row_count(db, t) for t in touched},
                      "errors": [], "parquet_stale": True, "detached_shared_file": detached,
                      "apply_seconds": round(time.monotonic()-started, 3)}
            db.execute("INSERT INTO _applied_updates VALUES (?,?,?,current_timestamp,?)",
                       [identity, batch_digest, market, dumps(result)])
            db.execute("COMMIT")
            committed = True
            db.execute("CHECKPOINT")
            return result
        except BaseException:
            if not committed:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()


def parquet_status(root: Path, settings: Settings) -> dict:
    pointer = root / "parquet/CURRENT.json"
    if not pointer.is_file():
        return {"ready": False, "stale": False}
    metadata = json.loads(pointer.read_text("utf-8"))
    with duckdb.connect(str(root / "analytics/ahledger.duckdb"), read_only=True) as db:
        current = database_update_id(db)
    stale = metadata.get("database_update_id") != current
    return {"ready": not stale, "stale": stale, "generated_at": metadata.get("generated_at")}
