import json

import duckdb
import pyarrow.parquet as pq
from conftest import MARKET, store_response

from ahledger.analytics import build_database
from ahledger.config import API
from ahledger.models import JobSpec
from ahledger.report import create_report
from ahledger.storage import State
from ahledger.verify import verify


def markets_response(state):
    store_response(
        state,
        JobSpec(dataset="markets", url=API + "/v1/markets"),
        {
            "markets": [
                {
                    "id": MARKET,
                    "game": "forever",
                    "ruleset": "pvp",
                    "faction": "horde",
                    "region": "us",
                    "label": "PvP Horde",
                }
            ]
        },
    )


def test_merge_ranges_keeps_resolution_and_exact_raw(state, spec, history):
    markets_response(state)
    for range_name in ("24h", "7d", "30d", "90d"):
        data = json.loads(json.dumps(history))
        data["range"] = range_name
        data["series"][0]["t"] = (
            "2026-10-09T07:00:00Z"
            if range_name == "7d"
            else "2026-10-09T07:12:00Z"
            if range_name == "24h"
            else "2026-10-09T00:00:00Z"
        )
        candidate = spec.model_copy(update={"params": {"range": range_name}})
        store_response(state, candidate, data)
    result = build_database(state)
    assert result["counts"]["price_history_ranges"] == 4
    assert result["counts"]["price_history"] == 3
    connection = duckdb.connect(str(state.root / "analytics" / "ahledger.duckdb"), read_only=True)
    try:
        assert connection.execute("SELECT count(*) FROM preferred_price_history").fetchone()[0] == 1
        assert {
            row[0] for row in connection.execute("SELECT granularity FROM price_history").fetchall()
        } == {"scan", "hour", "day"}
        assert (
            connection.execute(
                "SELECT typeof(median_copper) FROM price_history LIMIT 1"
            ).fetchone()[0]
            == "BIGINT"
        )
    finally:
        connection.close()
    pointer = json.loads((state.root / "parquet" / "CURRENT.json").read_text())
    table = pq.read_table(state.root / "parquet" / pointer["directory"] / "price_history.parquet")
    assert table.num_rows == 3
    assert verify(state)["integrity"] == "COMPLETE"


def test_rebuild_from_raw_without_original_sqlite(state, spec, history, tmp_path):
    markets_response(state)
    store_response(state, spec, history)
    expected = build_database(state)["counts"]
    other = State(tmp_path / "recovered")
    import shutil

    shutil.copytree(state.root / "raw", other.root / "raw")
    try:
        restored = build_database(other, restore_state=True)
        assert restored["counts"] == expected
        assert other.counts() == {"COMPLETE": 2}
        assert verify(other)["integrity"] == "COMPLETE"
    finally:
        other.close()


def test_corrupted_raw_detected(state, spec, history):
    meta = store_response(state, spec, history)
    path = state.root / "raw" / "blobs" / meta["sha256"][:2] / (meta["sha256"] + ".zst")
    import zstandard

    path.write_bytes(zstandard.ZstdCompressor().compress(b"different original bytes"))
    assert verify(state)["integrity"] == "FAILED"


def test_repeat_rebuild_and_report_not_full_download(state, spec, history):
    markets_response(state)
    store_response(state, spec, history)
    first = build_database(state)
    second = build_database(state)
    assert first["counts"] == second["counts"]
    report = create_report(state)
    assert report["download_status"] == "PARTIAL"
    assert report["raw_auction_scans"] == "unavailable"
    text = (state.root / "reports" / "download_report.html").read_text("utf-8")
    assert 'href="https://ahledger.com"' in text
    assert "NOT_CHECKED" in text


def test_recipes_mechanics_do_not_include_market_prices(state):
    markets_response(state)
    spec = JobSpec(dataset="crafting", url=API + "/v1/crafting/" + MARKET, market=MARKET)
    data = {
        "market": MARKET,
        "marketStatus": "scanned",
        "pricesAsOf": "2026-10-09T00:00:00Z",
        "generatedAt": "2026-10-09T01:00:00Z",
        "recipes": [
            {
                "recipe": {
                    "spellId": 1,
                    "name": "Cloth",
                    "profession": "Tailoring",
                    "skillRequired": 1,
                },
                "result": {"id": 2, "name": "Bolt", "qtyMin": 1, "qtyMax": 1},
                "reagents": [{"id": 2589, "name": "Linen", "qty": 2, "price": 28}],
                "resultPrice": 79,
                "cost": 56,
                "netValue": 75.05,
                "margin": 19.049999999999997,
            }
        ],
    }
    store_response(state, spec, data)
    build_database(state)
    db = duckdb.connect(str(state.root / "analytics" / "ahledger.duckdb"), read_only=True)
    try:
        inputs = db.execute("SELECT payload_json FROM recipe_inputs").fetchone()[0]
        assert '"price"' not in inputs
        amount, exact = db.execute(
            "SELECT net_value_copper,net_value_exact_copper FROM recipe_valuations"
        ).fetchone()
        assert amount == 75 and exact == "75.05"
    finally:
        db.close()


def test_no_scans_generates_no_price_rows(state, spec, history):
    markets_response(state)
    history.update(latest=None, series=[])
    store_response(state, spec, history)
    counts = build_database(state)["counts"]
    assert (
        counts["markets"] == 1 and counts["price_history"] == 0 and counts["price_snapshots"] == 0
    )
