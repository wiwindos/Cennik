import gzip
import json

import httpx
from conftest import store_response

from ahledger.analytics import build_database
from ahledger.http import cache_expiry
from ahledger.parsers import extract_items
from ahledger.runner import Downloader
from ahledger.storage import RawArchive, job_key
from ahledger.verify import verify


def test_http_compression_preserves_original_wire_bytes(state, spec, history):
    archive = RawArchive(state)
    body = json.dumps(history).encode()
    wire = gzip.compress(body)
    meta = archive.save(spec, 200, wire, {"content-encoding": "gzip"})
    assert archive.read_sha(meta["sha256"]) == wire
    assert archive.read(meta) == body


def test_broken_zstandard_is_integrity_failure(state, spec, history):
    meta = store_response(state, spec, history)
    blob = state.root / "raw/blobs" / meta["sha256"][:2] / (meta["sha256"] + ".zst")
    blob.write_bytes(b"not zstandard")
    assert verify(state)["integrity"] == "FAILED"


async def test_invalid_json_is_quarantined_without_blocking_valid_database(state, settings, spec):
    state.enqueue(spec)
    state.db.commit()
    runner = Downloader(
        settings,
        state,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"{bad json")),
    )
    runner.expand = False
    await runner.run(max_jobs=1, quiet=True)
    result = build_database(state)
    assert len(result["quarantined"]) == 1
    assert result["counts"]["price_history"] == 0
    checked = verify(state)
    assert checked["integrity"] == "COMPLETE"
    assert len(checked["rejected_responses"]) == 1 and checked["coverage"] == "PARTIAL"


def test_discovery_refresh_and_indexed_queue(state, spec):
    state.enqueue(spec)
    state.finish(job_key(spec), "COMPLETE")
    state.refresh_discovery(["markets"], None)
    assert state.counts() == {"COMPLETE": 1}
    state.refresh_discovery(["history"], None)
    assert state.counts() == {"PENDING": 1}
    statements = []
    state.db.set_trace_callback(statements.append)
    state.claim()
    state.db.set_trace_callback(None)
    claim_sql = next(sql for sql in statements if sql.startswith("SELECT * FROM jobs"))
    plan = state.db.execute("EXPLAIN QUERY PLAN " + claim_sql).fetchall()
    assert any("queue_claim" in row[3] for row in plan)


def test_old_date_never_makes_imported_response_fresh():
    assert (
        cache_expiry({"cache-control": "max-age=60", "date": "Thu, 01 Jan 1970 00:00:00 GMT"})
        <= __import__("time").time()
    )


def test_item_ids_in_shock_list_are_not_lost():
    assert set(extract_items({"supplyShocks": [2589, 123, None], "count": 99})) == {2589, 123}
