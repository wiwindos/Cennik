import json
import sys
from datetime import UTC, datetime, timedelta

import duckdb
import pytest
from conftest import MARKET, store_response

from ahledger import analytics
from ahledger.config import API, Settings
from ahledger.models import JobSpec
from ahledger.storage import RawArchive


def test_resource_settings_apply_to_build_and_export(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text("analytics_memory_mb = 128\nanalytics_threads = 2\n")
    monkeypatch.setenv("AHL_ANALYTICS_MEMORY_MB", "96")
    settings = Settings.read(config)
    database = tmp_path / "test.duckdb"
    with analytics.analytics_connection(database, settings) as db:
        actual = dict(db.execute("SELECT name,value FROM duckdb_settings()").fetchall())
        assert actual["memory_limit"] == "96.0 MiB"
        assert actual["threads"] == "2"
        assert actual["preserve_insertion_order"] == "false"
        assert actual["temp_directory"] == str(tmp_path / "test.duckdb.tmp")
        db.execute("CREATE TABLE sample AS SELECT 1 AS id")
    with analytics.analytics_connection(database, settings, read_only=True) as db:
        assert db.execute("SELECT current_setting('memory_limit')").fetchone() == ("96.0 MiB",)
    monkeypatch.setenv("AHL_ANALYTICS_MEMORY_MB", "invalid")
    with pytest.raises(ValueError):
        Settings.read(config)


@pytest.mark.parametrize("wide", [False, True])
def test_large_single_response_uses_bounded_arrow_batches(state, spec, history, monkeypatch, wide):
    # Both row count and wide payloads must be bounded even within one manifest.
    if wide:
        start = datetime(2026, 10, 1, tzinfo=UTC)
        history["series"] = [
            {
                **history["series"][0],
                "t": (start + timedelta(hours=i)).isoformat(),
                "extra": "x" * 16384,
            }
            for i in range(512)
        ]
        store_response(state, spec, history)
        table, expected = "price_history_ranges", 512
    else:
        spec = JobSpec(dataset="pricetable", url=API + "/v1/pricetable/" + MARKET, market=MARKET)
        body = f"AHL1|{MARKET}|1791504000|12000\n" + "\n".join(
            f"{i}:20:18:3:19:21:10:30:4" for i in range(1, 12001)
        )
        store_response(state, spec, body.encode())
        table, expected = "price_snapshots", 12000
    real_arrow = analytics.pa
    batches = []

    class ArrowProxy:
        class Table:
            @staticmethod
            def from_pylist(rows, **kwargs):
                batches.append(
                    (
                        len(rows),
                        sum(
                            sys.getsizeof(row) + sum(sys.getsizeof(value) for value in row.values())
                            for row in rows
                        ),
                    )
                )
                return real_arrow.Table.from_pylist(rows, **kwargs)

        def __getattr__(self, name):
            return getattr(real_arrow, name)

    monkeypatch.setattr(analytics, "pa", ArrowProxy())
    result = analytics.build_database(
        state,
        settings=Settings(
            analytics_memory_mb=96,
            analytics_batch_rows=128,
            analytics_batch_mb=1,
        ),
    )
    assert result["counts"][table] == expected
    assert len(batches) > 2
    assert max(size for size, _ in batches) <= 128
    assert max(memory for _, memory in batches) <= 1024 * 1024
    if wide:
        with duckdb.connect(str(state.root / "analytics/ahledger.duckdb"), read_only=True) as db:
            payload = db.execute("SELECT payload_json FROM price_history LIMIT 1").fetchone()[0]
            assert json.loads(payload)["extra"] == "x" * 16384


def test_deduplication_keeps_item_detail_even_if_other_sources_are_newer(state, spec, history):
    detail = JobSpec(dataset="item", url=API + "/v1/item/2589", item_id=2589)
    meta = store_response(state, detail, {"id": 2589, "name": "Detailed", "vendorSell": 123})
    meta["retrieved_at"] = "2026-01-01T00:00:00Z"
    RawArchive(state).write_metadata(meta)
    catalog = JobSpec(dataset="catalog", url="https://ahledger.com/api/catalog")
    store_response(
        state, catalog, {"rows": [[2589, "Catalog", "common", "Cloth", 1, 1, "icon", 0]]}
    )
    store_response(state, spec, history)
    result = analytics.build_database(state, settings=Settings(analytics_memory_mb=96))
    assert result["counts"]["items"] == 1
    with duckdb.connect(str(state.root / "analytics/ahledger.duckdb"), read_only=True) as db:
        assert db.execute(
            "SELECT name,vendor_sell_copper,origin_dataset FROM items"
        ).fetchone() == (
            "Detailed",
            123,
            "item",
        )


def test_failed_parquet_export_preserves_published_generation(state, spec, history, monkeypatch):
    store_response(state, spec, history)
    analytics.build_database(state)
    database = state.root / "analytics/ahledger.duckdb"
    pointer = state.root / "parquet/CURRENT.json"
    before_db, before_pointer = database.read_bytes(), pointer.read_bytes()
    history["series"][0]["median"] = 100
    store_response(state, spec, history)

    def no_disk_space(connection, root):
        raise OSError("No space left on device")

    monkeypatch.setattr(analytics, "_write_parquet", no_disk_space)
    with pytest.raises(OSError, match="No space"):
        analytics.build_database(state)
    assert database.read_bytes() == before_db
    assert pointer.read_bytes() == before_pointer
