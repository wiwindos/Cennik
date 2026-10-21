import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import duckdb
import httpx
import pytest
from conftest import MARKET, store_response

from ahledger.analytics import SCHEMAS, build_database, export_parquet
from ahledger.config import API, Settings
from ahledger.incremental import parquet_status
from ahledger.models import JobSpec
from ahledger.storage import State
from ahledger.update import batch_root, fetch_update, merge_update
from ahledger.util import exclusive_lock
from ahledger.web import create_app
from ahledger.web_control import Controller, JobRequest
from cennik.data.duckdb import DuckDBProvider
from cennik.services.analytics import AnalyticsService

IDENTITY = "a" * 32
OTHER = "forever.normal.alliance.us"


def crafting(market):
    return {
        "market": market,
        "marketStatus": "scanned",
        "pricesAsOf": "2026-10-12T09:00:00Z",
        "generatedAt": "2026-10-12T09:00:00Z",
        "recipes": [
            {
                "recipe": {"spellId": 1, "name": "Cloth", "profession": "Tailoring"},
                "result": {"id": 2, "name": "Bolt", "qtyMin": 1, "qtyMax": 1},
                "reagents": [{"id": 2589, "name": "Linen Cloth", "qty": 2, "price": 5}],
                "resultPrice": 200,
                "cost": 10,
                "netValue": 190,
                "margin": 180,
            }
        ],
    }


def base_database(state, spec, history):
    store_response(
        state,
        JobSpec(dataset="markets", url=API + "/v1/markets"),
        {
            "markets": [
                {
                    "id": m,
                    "game": "forever",
                    "ruleset": "pvp",
                    "faction": "horde",
                    "region": "us",
                    "label": m,
                }
                for m in (MARKET, OTHER)
            ]
        },
    )
    store_response(state, spec, history)
    store_response(
        state,
        spec.model_copy(update={"market": OTHER, "url": f"{API}/v1/prices/{OTHER}/2589"}),
        history | {"market": OTHER},
    )
    store_response(
        state,
        JobSpec(dataset="crafting", url=f"{API}/v1/crafting/{OTHER}", market=OTHER),
        crafting(OTHER),
    )
    build_database(state)
    database = state.root / "analytics/ahledger.duckdb"
    with duckdb.connect(str(database)) as db:
        db.execute("DROP TABLE recipe_sources")  # Actual downloaded database has the old schema.
    return database


def stage_update(state):
    stage = State(batch_root(state.root, IDENTITY))
    stage.set("target_market", MARKET)
    store_response(
        stage,
        JobSpec(dataset="pricetable", url=f"{API}/v1/pricetable/{MARKET}", market=MARKET),
        f"AHL1|{MARKET}|1791795600|1\n2589:6:5:100:6:6:5:7:10".encode(),
    )
    store_response(
        stage,
        JobSpec(dataset="crafting", url=f"{API}/v1/crafting/{MARKET}", market=MARKET),
        crafting(MARKET),
    )
    stage.close()


def test_merge_preserves_history_other_market_and_recipe_provenance_and_is_idempotent(
    state, spec, history, tmp_path
):
    database = base_database(state, spec, history)
    original = tmp_path / "downloaded.duckdb"
    os.link(database, original)
    old_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    with duckdb.connect(str(database), read_only=True) as db:
        old_history = db.execute(
            "SELECT * FROM price_history ORDER BY market,item_id,observed_at"
        ).fetchall()
        old_other = db.execute("SELECT * FROM price_snapshots WHERE market=?", [OTHER]).fetchall()
    stage_update(state)
    settings = Settings(data_dir=state.root, markets=[MARKET])
    result = merge_update(settings, IDENTITY)
    assert result["updated_market"] == MARKET
    assert hashlib.sha256(original.read_bytes()).hexdigest() == old_hash
    with duckdb.connect(str(database), read_only=True) as db:
        assert (
            db.execute("SELECT * FROM price_history ORDER BY market,item_id,observed_at").fetchall()
            == old_history
        )
        assert (
            db.execute("SELECT * FROM price_snapshots WHERE market=?", [OTHER]).fetchall()
            == old_other
        )
        assert db.execute(
            "SELECT min_copper FROM price_snapshots WHERE market=? ORDER BY observed_at DESC LIMIT 1",
            [MARKET],
        ).fetchone() == (5,)
        assert db.execute("SELECT count(*) FROM recipe_sources").fetchone() == (2,)
    for market in (MARKET, OTHER):
        with DuckDBProvider(database, market, datetime.now(UTC) + timedelta(days=1)) as provider:
            assert len(provider.recipes()) == 1
    again = merge_update(settings, IDENTITY)
    assert again["counts"] == result["counts"]
    assert json.loads((state.root / "parquet/CURRENT.json").read_text())["directory"]
    assert result["mode"] == "incremental" and again["reused"]
    assert parquet_status(state.root, settings)["stale"]


def table_contents(path):
    with duckdb.connect(str(path), read_only=True) as db:
        existing = {r[0] for r in db.execute("SHOW TABLES").fetchall()}
        return {table: sorted(db.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr)
                for table in [*SCHEMAS, "price_history", "preferred_price_history", "coverage"] if table in existing}


def test_incremental_matches_full_build_with_key_collisions_and_new_recipe(state, spec, history, tmp_path):
    database = base_database(state, spec, history)
    stage_update(state)
    stage = State(batch_root(state.root, IDENTITY))
    changed = crafting(MARKET)
    changed["recipes"][0]["reagents"][0]["qty"] = 3
    changed["generatedAt"] = "2026-10-13T09:00:00Z"
    store_response(stage, JobSpec(dataset="crafting", url=f"{API}/v1/crafting/{MARKET}", market=MARKET), changed)
    stage.set("market_status", {MARKET: "scanned"})
    # Two observations collide on the same key; latest retrieval must win.
    store_response(stage, JobSpec(dataset="pricetable", url=f"{API}/v1/pricetable/{MARKET}", market=MARKET),
                   f"AHL1|{MARKET}|1791795600|1\n2589:9:8:90:9:9:8:10:10".encode())
    stage.close()
    reference = tmp_path / "reference"
    shutil.copytree(batch_root(state.root, IDENTITY), reference)
    full = State(reference)
    try:
        build_database(full, settings=Settings(data_dir=state.root), base_database=database,
                       allowed_market=MARKET)
    finally:
        full.close()
    settings = Settings(data_dir=state.root, markets=[MARKET])
    merge_update(settings, IDENTITY)
    assert table_contents(database) == table_contents(reference / "analytics/ahledger.duckdb")
    with DuckDBProvider(database, MARKET, datetime.now(UTC) + timedelta(days=1)) as provider:
        assert provider.recipes()[0].inputs[0].quantity == 3


def test_failed_validation_rolls_back_every_changed_table_and_schema(state, spec, history, monkeypatch):
    database = base_database(state, spec, history)
    before = table_contents(database)
    stage_update(state)
    import ahledger.incremental as incremental

    def fail(db, tables, market):
        assert "price_snapshots" in tables
        assert db.execute("SELECT count(*) FROM recipe_sources").fetchone()[0] == 2
        raise ValueError("injected post-write validation failure")

    monkeypatch.setattr(incremental, "check_touched", fail)
    with pytest.raises(ValueError, match="injected"):
        merge_update(Settings(data_dir=state.root, markets=[MARKET]), IDENTITY)
    assert table_contents(database) == before
    with duckdb.connect(str(database), read_only=True) as db:
        assert not db.execute("SELECT 1 FROM information_schema.tables WHERE table_name='_applied_updates'").fetchone()


@pytest.mark.parametrize("after_commit", [False, True])
def test_process_crash_recovers_atomic_update_and_retry(state, spec, history, after_commit):
    database = base_database(state, spec, history)
    before = table_contents(database)
    analytics = AnalyticsService(state.root)
    previous_generation = analytics.generation()
    stage_update(state)
    script = '''
import os,sys
from pathlib import Path
import ahledger.incremental as inc
from ahledger.config import Settings
from ahledger.update import merge_update
if sys.argv[4] == 'True':
    real = inc.analytics_connection
    class CrashConnection:
        def __init__(self, db): self.db=db
        def __getattr__(self, name): return getattr(self.db,name)
        def execute(self, sql, *args):
            result=self.db.execute(sql,*args)
            if sql == 'COMMIT': os._exit(23)
            return result
    inc.analytics_connection=lambda *args,**kwargs: CrashConnection(real(*args,**kwargs))
else:
    real=inc.check_touched
    def crash(*args):
        real(*args)
        os._exit(23)
    inc.check_touched=crash
merge_update(Settings(data_dir=Path(sys.argv[1]),markets=[sys.argv[2]]),sys.argv[3])
'''
    process = subprocess.run([sys.executable, "-c", script, str(state.root), MARKET, IDENTITY, str(after_commit)],
                             capture_output=True, text=True, timeout=40)
    assert process.returncode == 23, process.stdout + process.stderr
    if after_commit:
        assert analytics.generation() != previous_generation
    # Opening a writer performs DuckDB's WAL recovery after the killed process.
    with duckdb.connect(str(database)) as db:
        db.execute("CHECKPOINT")
    assert not os.path.exists(str(database) + ".wal")
    analytics.generation(refresh=True)
    if not after_commit:
        assert table_contents(database) == before
    result = merge_update(Settings(data_dir=state.root, markets=[MARKET]), IDENTITY)
    assert result["reused"] is after_commit
    with duckdb.connect(str(database), read_only=True) as db:
        assert db.execute("SELECT count(*) FROM _applied_updates").fetchone() == (1,)
    assert table_contents(database)["price_history"] == before["price_history"]


def test_changed_batch_id_is_rejected_and_corrupt_raw_does_not_write(state, spec, history):
    database = base_database(state, spec, history)
    stage_update(state)
    settings = Settings(data_dir=state.root, markets=[MARKET])
    merge_update(settings, IDENTITY)
    before = table_contents(database)
    stage = State(batch_root(state.root, IDENTITY))
    store_response(stage, JobSpec(dataset="pricetable", url=f"{API}/v1/pricetable/{MARKET}", market=MARKET),
                   f"AHL1|{MARKET}|1791795600|1\n2589:7:6:90:7:7:6:8:10".encode())
    stage.close()
    with pytest.raises(ValueError, match="different contents"):
        merge_update(settings, IDENTITY)
    assert table_contents(database) == before
    blob = next((batch_root(state.root, IDENTITY) / "raw/blobs").glob("*/*.zst"))
    blob.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="Повреждён"):
        merge_update(settings, IDENTITY)
    assert table_contents(database) == before


@pytest.mark.parametrize("lock_dir", ["", "cennik"])
def test_incremental_respects_writer_and_calculation_locks(state, spec, history, lock_dir):
    database = base_database(state, spec, history)
    stage_update(state)
    before = table_contents(database)
    with exclusive_lock(state.root / lock_dir), pytest.raises(RuntimeError, match="Another writer"):
        merge_update(Settings(data_dir=state.root, markets=[MARKET]), IDENTITY)
    assert table_contents(database) == before


def test_manual_export_restores_current_parquet_and_api_hides_old_export(state, spec, history):
    from fastapi.testclient import TestClient

    database = base_database(state, spec, history)
    stage_update(state)
    settings = Settings(data_dir=state.root, markets=[MARKET])
    old_pointer = (state.root / "parquet/CURRENT.json").read_bytes()
    merge_update(settings, IDENTITY)
    assert (state.root / "parquet/CURRENT.json").read_bytes() == old_pointer
    app = create_app(settings, "a" * 40)
    headers = {"X-API-Key": "a" * 40}
    with TestClient(app) as client:
        assert client.get("/api/status", headers=headers).json()["parquet"]["stale"]
        names = [f["name"] for f in client.get("/api/files", headers=headers).json()["files"]]
        assert "ahledger.duckdb" in names and not any(n.endswith(".parquet") for n in names)
        assert client.get("/api/files/price_history.parquet", headers=headers).status_code == 404
        app.state.controller.info = {"action": "update", "state": "building"}
        assert client.get("/api/analytics/markets", headers=headers).status_code == 409
        assert client.get("/api/status", headers=headers).status_code == 200
        app.state.controller.info = {"state": "idle"}
        export_parquet(state, settings=settings)
        assert client.get("/api/status", headers=headers).json()["parquet"]["ready"]
        assert client.get("/api/files/price_history.parquet", headers=headers).status_code == 200
    assert database.is_file()


def test_merge_rejects_foreign_market_without_changing_published_db(state, spec, history):
    database = base_database(state, spec, history)
    before = database.read_bytes()
    stage = State(batch_root(state.root, IDENTITY))
    stage.set("target_market", MARKET)
    foreign = spec.model_copy(update={"market": OTHER, "url": f"{API}/v1/prices/{OTHER}/2589"})
    store_response(stage, foreign, history | {"market": OTHER})
    stage.close()
    with pytest.raises(ValueError, match="outside the selected market"):
        merge_update(Settings(data_dir=state.root, markets=[MARKET]), IDENTITY)
    assert database.read_bytes() == before


async def test_fetch_update_is_bounded_fresh_and_selected_market_only(
    state, spec, history, monkeypatch
):
    base_database(state, spec, history)
    calls = []
    settings = Settings(data_dir=state.root, markets=[MARKET], concurrency=1, max_attempts=1)
    from ahledger.http import RateLimiter

    async def immediate(self, tool):
        pass

    monkeypatch.setattr(RateLimiter, "acquire", immediate)

    def respond(request):
        calls.append(request)
        assert MARKET in str(request.url)
        if request.url.path.endswith("/pricetable/" + MARKET):
            return httpx.Response(
                200, content=f"AHL1|{MARKET}|1791795600|1\n2589:6:5:100:6:6:5:7:10".encode()
            )
        if request.url.params.get("profession") == "invalid":
            return httpx.Response(400, json={"professions": ["Tailoring"]})
        if "/crafting/" in request.url.path:
            return httpx.Response(200, json=crafting(MARKET))
        if "/economy/" in request.url.path:
            return httpx.Response(
                200, json={"market": MARKET, "marketStatus": "scanned", "daily": []}
            )
        key = next(
            (
                key
                for dataset, key in (
                    ("signals", "signals"),
                    ("deals", "deals"),
                    ("structure", "items"),
                    ("disenchant", "opportunities"),
                )
                if f"/{dataset}/" in request.url.path
            ),
            None,
        )
        return httpx.Response(
            200,
            json={
                "market": MARKET,
                "marketStatus": "scanned",
                "generatedAt": "2026-10-12T09:00:00Z",
                "pricesAsOf": "2026-10-12T09:00:00Z",
                key: [],
            },
        )

    result = await fetch_update(settings, IDENTITY, transport=httpx.MockTransport(respond))
    assert sum(result["jobs"].values()) == 21
    assert len(calls) == 21
    assert not any("/prices/" in r.url.path or "/items/" in r.url.path for r in calls)
    await fetch_update(
        settings,
        IDENTITY,
        transport=httpx.MockTransport(
            lambda r: pytest.fail("Completed update was downloaded twice")
        ),
    )


def test_update_commands_and_validation(settings):
    with pytest.raises(ValueError):
        JobRequest(action="update", markets=[])
    with pytest.raises(ValueError):
        JobRequest(action="update", markets=[MARKET, OTHER])
    controller = Controller(settings)
    controller.info = {"action": "update", "update_id": IDENTITY, "state": "running"}
    commands = controller.commands(JobRequest(action="update", markets=[MARKET]))
    assert [c[-3] for c in commands] == ["update-fetch", "update-merge"]
    assert all(c[-2:] == ["--update-id", IDENTITY] for c in commands)
    assert controller.updating()
    assert controller.progress_root() == batch_root(controller.root, IDENTITY)


def test_api_update_requires_one_market_and_blocks_concurrent_analysis(state, spec, history, monkeypatch):
    import threading

    from fastapi.testclient import TestClient

    base_database(state, spec, history)
    app = create_app(Settings(data_dir=state.root), "a" * 40)
    release = threading.Event()

    def waiting(request):
        release.wait(3)
        app.state.controller.save(state="stopped")

    monkeypatch.setattr(app.state.controller, "execute", waiting)
    headers = {"X-API-Key": "a" * 40}
    with TestClient(app) as client:
        assert client.post("/api/jobs", json={"action": "update", "markets": []}, headers=headers).status_code == 422
        assert client.post("/api/jobs", json={"action": "update", "markets": [MARKET]}, headers=headers).status_code == 202
        assert client.get("/api/status", headers=headers).json()["run"]["update_id"]
        assert client.post("/api/analytics/recalculate", headers=headers).status_code == 409
        client.post("/api/jobs/stop", headers=headers)
        release.set()
