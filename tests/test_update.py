import hashlib
import json
import os
from datetime import UTC, datetime, timedelta

import duckdb
import httpx
import pytest
from conftest import MARKET, store_response

from ahledger.analytics import build_database
from ahledger.config import API, Settings
from ahledger.models import JobSpec
from ahledger.storage import State
from ahledger.update import batch_root, fetch_update, merge_update
from ahledger.web import create_app
from ahledger.web_control import Controller, JobRequest
from cennik.data.duckdb import DuckDBProvider

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
