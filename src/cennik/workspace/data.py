from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import zstandard as zstd

from ahledger.analytics import COMMON, KEYS, SCHEMAS, arrow_schema, normalized
from ahledger.models import JobSpec
from ahledger.parsers import Parsed
from ahledger.util import dumps
from cennik.core.models import PriceObservation
from cennik.data.duckdb import DuckDBProvider

from .models import MARKET


def now() -> str:
    return datetime.now(UTC).isoformat()


def atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class MarketDatabase:
    def __init__(self, root: Path):
        self.root, self.path = root, root / "market.duckdb"
        root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = duckdb.connect(str(self.path), config={"memory_limit": "512MB", "threads": "2",
            "temp_directory": str(root / "temporary"), "preserve_insertion_order": "false"})
        self.db.execute("SET TimeZone='UTC'")
        for name, fields in SCHEMAS.items():
            columns = ",".join(f'"{key}" {kind}' for key, kind in {**fields, **COMMON}.items())
            self.db.execute(f'CREATE TABLE IF NOT EXISTS "{name}" ({columns})')
        self.db.execute('CREATE OR REPLACE VIEW price_history AS SELECT * EXCLUDE ("range") FROM price_history_ranges QUALIFY row_number() OVER (PARTITION BY market,item_id,observed_at,granularity ORDER BY retrieved_at DESC)=1')
        self.db.execute("""CREATE TABLE IF NOT EXISTS workspace_revision(id INTEGER PRIMARY KEY,revision BIGINT);
            INSERT INTO workspace_revision SELECT 1,0 WHERE NOT EXISTS(SELECT 1 FROM workspace_revision);
            CREATE TABLE IF NOT EXISTS scan_registry(
                scan_id VARCHAR PRIMARY KEY,market VARCHAR,observed_at TIMESTAMPTZ,received_at TIMESTAMPTZ,
                row_count BIGINT,valid_rows BIGINT,complete BOOLEAN,source VARCHAR,path VARCHAR,sha256 VARCHAR,warnings VARCHAR);
            CREATE TABLE IF NOT EXISTS working_offers(
                scan_id VARCHAR,sequence BIGINT,item_id BIGINT,variant VARCHAR,name VARCHAR,
                quantity BIGINT,buyout BIGINT,bid BIGINT,time_left BIGINT,divisible BOOLEAN,
                vendor_sell BIGINT,vendor_buy BIGINT,observed_at TIMESTAMPTZ);
            CREATE TABLE IF NOT EXISTS applied_ingestions(id VARCHAR PRIMARY KEY,observed_at TIMESTAMPTZ);
            CREATE TABLE IF NOT EXISTS source_pins(raw_id VARCHAR PRIMARY KEY);
        """)
        self.db.execute("ALTER TABLE working_offers ADD COLUMN IF NOT EXISTS owned BOOLEAN")
        self.db.execute("ALTER TABLE scan_registry ADD COLUMN IF NOT EXISTS quote_path VARCHAR")
        self.db.execute("ALTER TABLE scan_registry ADD COLUMN IF NOT EXISTS offers_path VARCHAR")
        self.db.execute("CREATE TABLE IF NOT EXISTS source_sales_daily AS SELECT * FROM price_snapshots WHERE false")
        self.db.execute("CREATE TABLE IF NOT EXISTS source_heads(source VARCHAR PRIMARY KEY,observed_at TIMESTAMPTZ,raw_id VARCHAR)")
        if not self.db.execute("SELECT 1 FROM markets WHERE market=?", [MARKET]).fetchone():
            self.db.execute("INSERT INTO markets(market,game,ruleset,faction,region,label,market_status) VALUES(?,?,?,?,?,?,?)",
                            [MARKET, "forever", "pvp", "alliance", "us", "PvP Alliance US", "NO_SCANS"])
        self.revalidate_empty_scans()

    def revalidate_empty_scans(self) -> int:
        """Repair old unconfirmed empty replicate events without altering their archives."""
        with self.lock:
            rejected = []
            for row in self.rows("SELECT scan_id,path,warnings FROM scan_registry WHERE complete AND row_count=0"):
                payload = json.loads(zstd.ZstdDecompressor().decompress(
                    (self.root / row["path"]).read_bytes(), max_output_size=64_000_000))
                if payload.get("empty_market_confirmed") is not True:
                    warnings = json.loads(row["warnings"] or "[]")
                    warnings.append("Пустое событие replicate не подтверждает пустой рынок; исключено из актуальных цен")
                    rejected.append((dumps(warnings), row["scan_id"]))
            if not rejected:
                return 0
            self.db.execute("BEGIN")
            try:
                self.db.executemany("UPDATE scan_registry SET complete=false,warnings=? WHERE scan_id=?", rejected)
                invalid_head = self.db.execute("""SELECT 1 FROM source_heads h JOIN scan_registry s ON s.scan_id=h.raw_id
                    WHERE h.source='Cennik replicate' AND NOT s.complete""").fetchone()
                if invalid_head:
                    self.db.execute("DELETE FROM source_heads WHERE source='Cennik replicate'")
                    self.db.execute("DELETE FROM working_offers")
                    self.db.execute("DELETE FROM price_snapshots WHERE source='Cennik replicate'")
                    candidate = self.db.execute("SELECT observed_at,scan_id,offers_path,quote_path FROM scan_registry WHERE complete ORDER BY observed_at DESC,received_at DESC LIMIT 1").fetchone()
                    if candidate:
                        head = dict(zip(("observed_at", "scan_id", "offers_path", "quote_path"), candidate, strict=True))
                        if head["offers_path"]:
                            self.db.execute("INSERT INTO working_offers BY NAME SELECT * FROM read_parquet(?)", [str(self.root / head["offers_path"])])
                        if head["quote_path"]:
                            self.db.execute("INSERT INTO price_snapshots BY NAME SELECT * FROM read_parquet(?)", [str(self.root / head["quote_path"])])
                        self.db.execute("INSERT INTO source_heads VALUES('Cennik replicate',?,?)", [head["observed_at"], head["scan_id"]])
                self.db.execute("UPDATE workspace_revision SET revision=revision+1")
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return len(rejected)

    def rows(self, sql: str, params=None) -> list[dict]:
        with self.lock:
            cursor = self.db.cursor()
            try:
                result = cursor.execute(sql, params or [])
                names = [column[0] for column in result.description]
                return [dict(zip(names, row, strict=True)) for row in result.fetchall()]
            finally:
                cursor.close()

    def generation(self) -> str:
        return str(self.rows("SELECT revision FROM workspace_revision")[0]["revision"])

    def archive(self, kind: str, identity: str, payload: dict, observed: datetime) -> Path:
        path = self.root / "archive" / kind / MARKET / observed.strftime("%Y/%m/%d") / (identity + ".json.zst")
        if not path.exists():
            atomic_bytes(path, zstd.ZstdCompressor(level=6).compress(dumps(payload).encode()))
        return path

    def parquet(self, kind: str, identity: str, rows: list[dict], observed: datetime, schema=None) -> str | None:
        if not rows:
            return None
        path = self.root / "archive" / kind / MARKET / observed.strftime("%Y/%m/%d") / (identity + ".parquet")
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".parquet.tmp")
            pq.write_table(pa.Table.from_pylist(rows, schema=schema), temporary, compression="zstd", row_group_size=4096)
            os.replace(temporary, path)
        return str(path.relative_to(self.root))

    def insert_rows(self, table: str, rows: list[dict]) -> None:
        if not rows:
            return
        columns = {**SCHEMAS[table], **COMMON}
        for offset in range(0, len(rows), 2048):
            batch = rows[offset:offset + 2048]
            for row in batch:
                for key, kind in columns.items():
                    if kind.startswith("DECIMAL") and row.get(key) is not None:
                        row[key] = Decimal(str(row[key])).quantize(Decimal("0.000001"))
            self.db.register("incoming", pa.Table.from_pylist(batch, schema=arrow_schema(columns)))
            row_key = KEYS.get(table, "raw_id" if table == "dataset_metadata" else None)
            select = 'SELECT * FROM incoming'
            if table == "items":
                # Sparse observations never erase item facts already learned elsewhere.
                select = 'SELECT ' + ",".join(
                    ('CASE WHEN n.origin_dataset=\'manual\' THEN coalesce(n."' + c + '",o."' + c + '") ELSE coalesce(o."' + c + '",n."' + c + '") END') if c in {"vendor_sell_copper", "vendor_buy_copper", "binding", "stack_size"}
                    else ('CASE WHEN o.origin_dataset IN (\'local_scan\',\'manual\') THEN coalesce(o.name,n.name) ELSE coalesce(n.name,o.name) END' if c == "name" else
                          ('CASE WHEN o.origin_dataset IN (\'local_scan\',\'manual\') AND n.origin_dataset<>\'manual\' THEN o.origin_dataset ELSE n.origin_dataset END' if c == "origin_dataset" else
                          ('(coalesce(n.is_placeholder,true) AND coalesce(o.is_placeholder,true))' if c == "is_placeholder" else 'n."' + c + '"'))
                          )
                    for c in columns) + ' FROM incoming n LEFT JOIN items o USING(item_id)'
            self.db.execute('CREATE TEMP TABLE import_batch AS ' + select)
            if row_key:
                match = " AND ".join(f't."{c.strip(chr(34))}" IS NOT DISTINCT FROM n."{c.strip(chr(34))}"' for c in row_key.split(","))
                self.db.execute(f'DELETE FROM "{table}" t USING import_batch n WHERE {match}')
            self.db.execute(f'INSERT INTO "{table}" SELECT * FROM import_batch')
            self.db.execute("DROP TABLE import_batch")
            self.db.unregister("incoming")

    def import_api(self, spec: JobSpec, parsed: Parsed, body: bytes, headers: dict) -> bool:
        identity = hashlib.sha256(dumps(spec.model_dump(mode="json")).encode() + body).hexdigest()
        with self.lock:
            if self.db.execute("SELECT 1 FROM applied_ingestions WHERE id=?", [identity]).fetchone():
                return False
            observed = datetime.now(UTC)
            path = self.archive("api", identity, {"spec": spec.model_dump(mode="json"), "headers": headers,
                                "data": parsed.data, "retrieved_at": observed.isoformat()}, observed)
            meta = {"id": identity, "sha256": hashlib.sha256(body).hexdigest(), "retrieved_at": observed.isoformat(),
                    "http_status": 200, "archive_path": str(path.relative_to(self.root))}
            tables = normalized(spec, parsed, meta)
            self.db.execute("BEGIN")
            try:
                head = self.db.execute("SELECT observed_at FROM source_heads WHERE source='AHL1'").fetchone()
                incoming_time = parsed.data.get("observedAt") if spec.dataset == "pricetable" else None
                replace_head = bool(incoming_time and (not head or datetime.fromisoformat(incoming_time.replace('Z', '+00:00')) >= head[0]))
                if replace_head:
                    self.db.execute("DELETE FROM price_snapshots WHERE source='AHL1'")
                for table, rows in tables.items():
                    self.insert_rows(table, rows)
                if replace_head:
                    self.db.execute("INSERT OR REPLACE INTO source_heads VALUES('AHL1',?,?)", [incoming_time, identity])
                if spec.dataset == "pricetable":
                    # One real latest estimate per item/UTC day; never sum daily estimates.
                    self.db.execute("""DELETE FROM source_sales_daily d USING price_snapshots p
                        WHERE p.raw_id=? AND p.item_id=d.item_id AND p.source=d.source
                        AND CAST(p.observed_at AS DATE)=CAST(d.observed_at AS DATE)
                        AND p.observed_at>=d.observed_at""", [identity])
                    self.db.execute("""INSERT INTO source_sales_daily SELECT p.* FROM price_snapshots p
                        WHERE p.raw_id=? AND p.sales_estimated_per_day IS NOT NULL AND NOT EXISTS(
                            SELECT 1 FROM source_sales_daily d WHERE d.item_id=p.item_id AND d.source=p.source
                            AND CAST(d.observed_at AS DATE)=CAST(p.observed_at AS DATE))""", [identity])
                    self.db.execute("DELETE FROM source_sales_daily WHERE observed_at<current_timestamp-INTERVAL 30 DAY")
                self.db.execute("""DELETE FROM price_snapshots p WHERE source='AHL1' AND observed_at<(
                    SELECT observed_at FROM source_heads WHERE source='AHL1')""")
                self.db.execute("""DELETE FROM price_snapshots WHERE rowid IN (
                    SELECT rowid FROM price_snapshots QUALIFY row_number() OVER(
                    PARTITION BY source,item_id ORDER BY observed_at DESC,retrieved_at DESC)>1)""")
                self.db.execute("INSERT INTO applied_ingestions VALUES(?,?)", [identity, observed])
                self.db.execute("UPDATE workspace_revision SET revision=revision+1")
                self.db.execute("UPDATE markets SET market_status='scanned' WHERE market=?", [MARKET])
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return True

    def import_scan(self, scan: dict) -> dict:
        if scan.get("format_version") != 1 or scan.get("market") != MARKET:
            raise ValueError("Неподдерживаемый формат или рынок скана")
        if not isinstance(scan.get("data"), str) or len(scan["data"]) > 48_000_000:
            raise ValueError("Неверный размер данных скана")
        observed = datetime.fromtimestamp(scan["observed_at"], UTC)
        if observed.timestamp() > datetime.now(UTC).timestamp() + 120:
            raise ValueError("Время скана находится в будущем; проверьте часы")
        payload = dumps(scan).encode()
        identity = hashlib.sha256(payload).hexdigest()
        with self.lock:
            if self.db.execute("SELECT 1 FROM scan_registry WHERE scan_id=?", [identity]).fetchone():
                return {"scan_id": identity, "reused": True}
            path = self.archive("scans", identity, scan, observed)
            variants = scan.get("variants", [])
            lines = scan["data"].split(";") if scan["data"] else []
            offers: list[dict[str, Any]] = []
            warnings: list[str] = []
            for index, line in enumerate(lines):
                try:
                    values = [int(value) for value in line.split(":")]
                    if len(values) not in {6, 7}:
                        raise ValueError("expected six or seven fields")
                    item, quantity, buyout, bid, remaining, variant_id = values[:6]
                    ownership = values[6] if len(values) == 7 else 0
                    if ownership not in {0, 1, 2}:
                        raise ValueError("invalid ownership flag")
                    if item <= 0 or quantity <= 0 or any(value < 0 or value > 2**53 - 1 for value in values):
                        raise ValueError("invalid numbers")
                    variant = variants[variant_id - 1] if 0 < variant_id <= len(variants) else {}
                    offers.append({"scan_id": identity, "sequence": index, "item_id": item,
                        "variant": variant.get("link"), "name": variant.get("name"), "quantity": quantity,
                        "buyout": buyout, "bid": bid, "time_left": remaining, "divisible": variant.get("divisible"),
                        "vendor_sell": variant.get("vendor_sell"), "vendor_buy": None, "observed_at": observed,
                        "owned": None if not ownership else ownership == 1})
                except (ValueError, TypeError, IndexError) as error:
                    if len(warnings) < 20:
                        warnings.append(f"Строка {index + 1}: {error}")
            complete = (bool(scan.get("full")) and len(offers) == scan.get("count") == len(lines)
                        and not scan.get("skipped", 0) and not scan.get("error")
                        and scan.get("reported_count", scan.get("count")) == len(lines)
                        and (bool(lines) or scan.get("empty_market_confirmed") is True))
            if not lines and scan.get("empty_market_confirmed") is not True:
                warnings.append("Пустой ответ API не подтверждает отсутствие товаров на рынке")
            if scan.get("error"):
                warnings.append("Сборщик: " + str(scan["error"])[:1000])
            if not complete:
                warnings.append("Покрытие неполное: предыдущая рабочая книга не заменена")
            snapshots, items = [], []
            if complete:
                grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
                for offer in offers:
                    if offer["buyout"] > 0:
                        grouped[offer["item_id"]].append(offer)
                for item, rows in grouped.items():
                    ordered = sorted(rows, key=lambda r: Decimal(r["buyout"]) / r["quantity"])
                    quantity = sum(r["quantity"] for r in ordered)
                    targets = ((quantity + 1) // 2, (quantity + 2) // 2)
                    running = 0
                    central: list[Decimal] = []
                    for row in ordered:
                        running += row["quantity"]
                        while len(central) < 2 and running >= targets[len(central)]:
                            central.append(Decimal(row["buyout"]) / row["quantity"])
                        if len(central) == 2:
                            break
                    snapshots.append({"item_id": item, "market": MARKET, "observed_at": observed.isoformat(),
                        "source": "Cennik replicate", "quantity": quantity,
                        "min_copper": ordered[0]["buyout"] // ordered[0]["quantity"], "median_copper": int(sum(central) / 2),
                        "raw_id": identity, "sha256": identity, "retrieved_at": now()})
                    row = rows[0]
                    items.append({"item_id": item, "name": row["name"], "vendor_sell_copper": row["vendor_sell"],
                        "is_placeholder": row["name"] is None, "origin_dataset": "local_scan",
                        "raw_id": identity, "sha256": identity, "retrieved_at": now()})
            # Every complete scan, including late arrivals, remains available for research.
            quote_path = self.parquet("quotes", identity, snapshots, observed,
                arrow_schema({**SCHEMAS["price_snapshots"], **COMMON}))
            offers_path = self.parquet("normalized", identity, offers, observed)
            self.db.execute("BEGIN")
            try:
                self.db.execute("INSERT INTO scan_registry(scan_id,market,observed_at,received_at,row_count,valid_rows,complete,source,path,sha256,warnings,quote_path,offers_path) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [identity, MARKET, observed, now(), len(lines), len(offers), complete, "Cennik replicate",
                     str(path.relative_to(self.root)), identity, dumps(warnings), quote_path, offers_path])
                latest_row = self.db.execute("SELECT observed_at FROM source_heads WHERE source='Cennik replicate'").fetchone()
                latest = latest_row[0] if latest_row else None
                if complete and (latest is None or observed >= latest):
                    self.db.execute("DELETE FROM working_offers")
                    self.db.execute("DELETE FROM price_snapshots WHERE source='Cennik replicate'")
                    self.db.execute("INSERT OR REPLACE INTO source_heads VALUES('Cennik replicate',?,?)", [observed, identity])
                    if offers:
                        self.db.register("scan_offers", pa.Table.from_pylist(offers))
                        self.db.execute("INSERT INTO working_offers SELECT * FROM scan_offers")
                        self.db.unregister("scan_offers")
                    self.insert_rows("price_snapshots", snapshots)
                    self.insert_rows("items", items)
                    self.db.execute("UPDATE markets SET market_status='scanned' WHERE market=?", [MARKET])
                if complete:
                    self.db.execute("UPDATE workspace_revision SET revision=revision+1")
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return {"scan_id": identity, "complete": complete, "rows": len(offers), "warnings": warnings, "reused": False}

    def close(self):
        self.db.close()

    def prune_api(self, cutoff: str) -> int:
        with self.lock:
            records = self.rows("""SELECT raw_id,payload_json FROM dataset_metadata
                WHERE retrieved_at<? AND dataset NOT IN ('item','crafting')
                AND raw_id NOT IN (SELECT raw_id FROM source_pins)
                AND NOT (dataset='pricetable' AND raw_id IN (
                    SELECT raw_id FROM dataset_metadata WHERE dataset='pricetable'
                    QUALIFY row_number() OVER(PARTITION BY market ORDER BY retrieved_at DESC)=1))""", [cutoff])
            if not records:
                return 0
            self.db.execute("BEGIN")
            try:
                for record in records:
                    self.db.execute("DELETE FROM source_sales_daily WHERE raw_id=?", [record["raw_id"]])
                    for table in SCHEMAS:
                        self.db.execute(f'DELETE FROM "{table}" WHERE raw_id=?', [record["raw_id"]])
                    self.db.execute("DELETE FROM applied_ingestions WHERE id=?", [record["raw_id"]])
                self.db.execute("UPDATE workspace_revision SET revision=revision+1")
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            api_root = (self.root / "archive/api").resolve()
            for record in records:
                relative = json.loads(record["payload_json"]).get("archive_path")
                if relative:
                    path = (self.root / relative).resolve()
                    if path.is_relative_to(api_root):
                        path.unlink(missing_ok=True)
            return len(records)


class WorkspaceProvider(DuckDBProvider):
    def __init__(self, database: MarketDatabase, market: str, as_of: datetime):
        self.database, self.market, self.as_of = database, market, as_of
        self.db = database.db.cursor()
        self.recipe_selection = {"policy": "latest_market_snapshot_as_of"}
        self._recipes = None
        self._source_references: dict[int, dict] | None = None
        self.used_raw_ids: set[str] = set()

    def __exit__(self, *args):
        if self.used_raw_ids:
            with self.database.lock:
                self.database.db.executemany("INSERT OR IGNORE INTO source_pins VALUES(?)", [(key,) for key in self.used_raw_ids])
        self.db.close()

    def track(self, rows):
        self.used_raw_ids.update(row["raw_id"] for row in rows if row.get("raw_id"))
        return rows

    def current_prices(self) -> dict[int, PriceObservation]:
        prices = super().current_prices()
        # Missing items in a newer complete source snapshot must not be resurrected.
        heads = self.rows("SELECT source,max(observed_at) observed_at FROM price_snapshots WHERE market=? AND observed_at<=? GROUP BY source", [self.market, self.as_of])
        by_source = {row["source"]: row["observed_at"] for row in heads}
        by_source.update({row["source"]: row["observed_at"] for row in self.rows(
            "SELECT source,observed_at FROM source_heads WHERE observed_at<=?", [self.as_of])})
        own = self.rows("SELECT max(observed_at) observed_at FROM scan_registry WHERE complete AND market=? AND observed_at<=?", [self.market, self.as_of])[0]["observed_at"]
        if own:
            by_source["Cennik replicate"] = own
        newest = max(by_source.values()) if by_source else None
        result = {item: price for item, price in prices.items() if newest is None or price.observed_at >= newest}
        self.used_raw_ids.update(price.raw_id for price in result.values() if price.raw_id)
        self.track(self.rows("SELECT DISTINCT raw_id FROM price_snapshots WHERE source='AHL1' AND market=? AND observed_at=(SELECT max(observed_at) FROM price_snapshots WHERE source='AHL1' AND market=? AND observed_at<=?)", [self.market, self.market, self.as_of]))
        return result

    def source_reference(self, item_id: int):
        if self._source_references is None:
            self._source_references = {row["item_id"]: row for row in self.rows(
                "SELECT item_id,median30d_copper copper,observed_at,raw_id FROM price_snapshots WHERE market=? AND source='AHL1' AND observed_at<=?", [self.market, self.as_of])}
        value = self._source_references.get(item_id)
        if value and value.get("raw_id"):
            self.used_raw_ids.add(value["raw_id"])
        return value

    def items(self):
        self.track(self.rows("SELECT DISTINCT raw_id FROM items WHERE retrieved_at<=?", [self.as_of]))
        return super().items()

    def recipes(self):
        result = super().recipes()
        self.used_raw_ids.update(recipe.raw_id for recipe in result if recipe.raw_id)
        return result

    def historical_prices(self, days):
        result = super().historical_prices(days)
        from datetime import timedelta
        paths = [str(self.database.root / row["quote_path"]) for row in self.rows(
            "SELECT quote_path FROM scan_registry WHERE complete AND quote_path IS NOT NULL AND market=? AND observed_at>=? AND observed_at<=?",
            [self.market, self.as_of-timedelta(days=days), self.as_of])]
        if paths:
            # Research reads only the requested date partitions and returns bounded daily summaries.
            rows = self.rows("""SELECT item_id,market,date_trunc('day',CAST(observed_at AS TIMESTAMPTZ)) observed_at,
                'day' AS granularity,'Cennik daily aggregate' AS source,
                CAST(floor(median(median_copper)) AS BIGINT) median_copper,
                min(min_copper) min_copper,CAST(floor(median(quantity)) AS BIGINT) quantity
                FROM read_parquet(?) GROUP BY item_id,market,date_trunc('day',CAST(observed_at AS TIMESTAMPTZ))
                LIMIT 250001""", [paths])
            if len(rows) > 250000:
                raise ValueError("История превышает 250000 дневных наблюдений. Уменьшите период или используйте ориентир AHledger 30d")
            own_days = {(row["item_id"], row["observed_at"].date()) for row in rows}
            for item_id, observations in result.items():
                result[item_id] = [p for p in observations if (item_id, p.observed_at.astimezone(UTC).date()) not in own_days]
            for row in rows:
                result.setdefault(row["item_id"], []).append(self.observation(row))
        self.used_raw_ids.update(p.raw_id for rows in result.values() for p in rows if p.raw_id)
        return result

    def sales_history(self, days):
        from datetime import timedelta
        result: dict[int, list[PriceObservation]] = defaultdict(list)
        for row in self.rows("SELECT *, 'scan' granularity FROM source_sales_daily WHERE market=? AND observed_at>=? AND observed_at<=? ORDER BY observed_at",
                             [self.market, self.as_of-timedelta(days=days), self.as_of]):
            result[row["item_id"]].append(self.observation(row))
        self.used_raw_ids.update(p.raw_id for rows in result.values() for p in rows if p.raw_id)
        return result

    def quality(self) -> dict:
        result = super().quality()
        result["local_scan_count"] = self.rows("SELECT count(*) n FROM scan_registry WHERE market=? AND observed_at<=?", [self.market, self.as_of])[0]["n"]
        return result
