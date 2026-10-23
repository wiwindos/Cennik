from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ahledger.models import JobSpec
from ahledger.parsers import Parsed
from cennik.addon.lua_data import lua_literal
from cennik.workspace.data import WorkspaceProvider
from cennik.workspace.install import install_addon
from cennik.workspace.models import MARKET, PlanRequest
from cennik.workspace.service import Workspace
from cennik.workspace.web import create_app


@pytest.fixture
def workspace(tmp_path):
    value = Workspace(tmp_path / "fresh")
    try:
        yield value
    finally:
        value.close()


def scan(*, offset=0, count=2, divisible=True):
    return {"format_version": 1, "scan_id": f"fixture-{offset}-{count}", "market": MARKET,
        "observed_at": int((datetime.now(UTC) - timedelta(seconds=30-offset)).timestamp()),
        "full": True, "count": count, "data": ";".join(["2589:4:40:0:4:1"] * count),
        "variants": [{"link": "item:2589:0:0:0", "name": "Льняная ткань", "divisible": divisible, "vendor_sell": 13}]}


def test_archive_is_permanent_idempotent_and_preserves_identical_lots(workspace):
    data = scan()
    first = workspace.market.import_scan(data)
    assert first["complete"]
    assert workspace.market.import_scan(data)["reused"]
    assert workspace.overview()["archive"]["scans"] == 1
    assert len(workspace.market.rows("SELECT * FROM working_offers")) == 2
    assert list((workspace.root / "archive/scans").rglob("*.json.zst"))
    workspace.market.import_scan(scan(offset=2, count=1))
    assert len(list((workspace.root / "archive/scans").rglob("*.json.zst"))) == 2
    assert len(workspace.market.rows("SELECT * FROM working_offers")) == 1


def test_partial_scan_keeps_previous_book_and_raw_error(workspace):
    workspace.market.import_scan(scan())
    broken = scan(offset=2)
    broken["data"] += ";invalid"
    result = workspace.market.import_scan(broken)
    assert not result["complete"]
    assert result["warnings"]
    assert len(workspace.market.rows("SELECT * FROM working_offers")) == 2
    assert len(list((workspace.root / "archive/scans").rglob("*.json.zst"))) == 2


def test_complete_empty_scan_does_not_resurrect_old_local_prices(workspace):
    initial = scan()
    workspace.market.import_scan(initial)
    empty = scan(offset=2, count=0)
    empty["observed_at"] = initial["observed_at"] + 2
    empty["empty_market_confirmed"] = True
    workspace.market.import_scan(empty)
    with WorkspaceProvider(workspace.market, MARKET, datetime.now(UTC)) as provider:
        assert 2589 not in provider.current_prices()
    late = scan(offset=1, count=1)
    late["observed_at"] = initial["observed_at"] + 1
    workspace.market.import_scan(late)
    assert not workspace.market.rows("SELECT * FROM working_offers")
    assert workspace.market.rows("SELECT count(*) n FROM scan_registry WHERE quote_path IS NOT NULL")[0]["n"] == 2


def test_unconfirmed_empty_and_errored_scans_never_replace_current_prices(workspace):
    workspace.market.import_scan(scan())
    generation = workspace.analytics.generation()
    assert not workspace.market.import_scan(scan(offset=2, count=0))["complete"]
    bad = scan(offset=3)
    bad["error"] = "auction closed"
    assert not workspace.market.import_scan(bad)["complete"]
    assert workspace.analytics.generation() == generation
    with WorkspaceProvider(workspace.market, MARKET, datetime.now(UTC)) as provider:
        assert provider.current_prices()[2589].quantity == 8


def test_legacy_empty_head_repaired_without_removing_archive(workspace):
    workspace.market.import_scan(scan())
    raw = scan(offset=2, count=0)
    result = workspace.market.import_scan(raw)
    archive_count = len(list((workspace.root / "archive/scans").rglob("*.json.zst")))
    # Reproduce the old 0.2.0 acceptance bug before running its startup repair.
    workspace.market.db.execute("UPDATE scan_registry SET complete=true WHERE scan_id=?", [result["scan_id"]])
    workspace.market.db.execute("DELETE FROM working_offers")
    workspace.market.db.execute("DELETE FROM price_snapshots WHERE source='Cennik replicate'")
    workspace.market.db.execute("UPDATE source_heads SET observed_at=to_timestamp(?),raw_id=? WHERE source='Cennik replicate'", [raw["observed_at"], result["scan_id"]])
    assert workspace.market.revalidate_empty_scans() == 1
    assert workspace.market.revalidate_empty_scans() == 0
    assert len(list((workspace.root / "archive/scans").rglob("*.json.zst"))) == archive_count
    assert len(workspace.market.rows("SELECT * FROM working_offers")) == 2
    with WorkspaceProvider(workspace.market, MARKET, datetime.now(UTC)) as provider:
        assert provider.current_prices()[2589].quantity == 8


def test_hot_prices_bounded_and_own_history_reads_archived_quotes(workspace):
    for offset in range(4):
        workspace.market.import_scan(scan(offset=offset))
    assert workspace.market.rows("SELECT count(*) n FROM price_snapshots")[0]["n"] == 1
    assert len(list((workspace.root / "archive/normalized").rglob("*.parquet"))) == 4
    with WorkspaceProvider(workspace.market, MARKET, datetime.now(UTC)) as provider:
        history = provider.historical_prices(7)
        assert len(history[2589]) == 1
        assert history[2589][0].median_copper == 10
        assert history[2589][0].quantity == 8


def test_backup_contains_archive_and_separate_consistent_databases(workspace):
    from pathlib import Path

    import duckdb
    workspace.market.import_scan(scan())
    result = workspace.backup()
    path = Path(result["directory"])
    assert (path / "BACKUP_COMPLETE.json").exists()
    assert len(list((path / "archive/scans").rglob("*.json.zst"))) == 1
    with duckdb.connect(str(path / "market.duckdb"), read_only=True) as copy:
        assert copy.execute("SELECT count(*) FROM scan_registry").fetchone() == (1,)


def test_new_generation_does_not_hash_legacy_database(workspace, monkeypatch):
    monkeypatch.setattr("cennik.services.analytics.fingerprint", lambda *a: pytest.fail("full file hashing"))
    initial = workspace.analytics.generation()
    workspace.market.import_scan(scan())
    workspace.analytics.start(background=False)
    assert initial != workspace.analytics.generation()
    result = workspace.store.listing(MARKET)
    assert result["items"]
    opportunity = next(item for item in result["items"] if item["strategy_id"] == "vendor_arbitrage")
    assert opportunity["potential_profit_copper"] == 3


def test_procurement_respects_indivisible_stacks(workspace):
    workspace.market.import_scan(scan(divisible=False))
    workspace.analytics.start(background=False)
    identity = workspace.store.listing(MARKET)["items"][0]["id"]
    blocked = workspace.plan(PlanRequest(opportunity_id=identity, batches=1))
    assert not blocked["complete"] and blocked["profit_copper"] is None
    allowed = workspace.plan(PlanRequest(opportunity_id=identity, batches=1, allow_overbuy=True))
    assert allowed["complete"]
    assert allowed["cost_copper"] == 40
    assert allowed["profit_copper"] == 12
    assert allowed["lines"][0]["purchased"] == 4


def test_persistent_etag_cache_does_not_create_new_observation(workspace, monkeypatch):
    calls = []
    body = f"AHL1|{MARKET.replace('.', '/')}|{int(datetime.now(UTC).timestamp())}|1\n2589:12:10:8:14:15:9:18:10\n".encode()
    def handler(request):
        calls.append(request)
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304, headers={"ETag": '"v1"', "Cache-Control": "max-age=0"})
        return httpx.Response(200, content=body, headers={"ETag": '"v1"', "Cache-Control": "max-age=0"})
    client = httpx.Client
    monkeypatch.setattr("cennik.workspace.service.httpx.Client", lambda **kw: client(transport=httpx.MockTransport(handler), **kw))
    spec = JobSpec(dataset="pricetable", url=f"https://api.ahledger.com/v1/pricetable/{MARKET}", market=MARKET)
    assert workspace.request(spec)["changed"]
    generation = workspace.analytics.generation()
    workspace.last_request = 0
    assert workspace.request(spec)["http_status"] == 304
    assert workspace.analytics.generation() == generation
    assert len(calls) == 2
    assert workspace.market.rows("SELECT count(*) n FROM price_snapshots")[0]["n"] == 1


def test_scan_sync_acknowledges_only_after_archive(workspace, tmp_path):
    game = tmp_path / "Game"
    addon = game / "Interface/AddOns/CennikAddon"
    addon.mkdir(parents=True)
    (addon / "CennikAddon.toc").write_text("ImportedData.lua")
    saved = game / "WTF/Account/fixture/SavedVariables/CennikAddon.lua"
    saved.parent.mkdir(parents=True)
    saved.write_text("CennikAddonDB=" + lua_literal({"device_id": "fixture-device", "events": [], "market_scans": [scan()]}), encoding="utf-8")
    settings = workspace.settings().model_copy(update={"game_root": str(game), "account": "fixture"})
    workspace.save_settings(settings)
    result = workspace.synchronize()
    assert result["acknowledged_scans"] == 1
    text = (addon / "ImportedData.lua").read_text("utf-8")
    assert "acknowledged_scan_ids" in text
    assert "fixture-device" in text
    assert len(list((workspace.root / "archive/scans").rglob("*.json.zst"))) == 1


def test_plan_publish_preserves_batch_and_reuses_package(workspace, tmp_path):
    game = tmp_path / "Game"
    addon = game / "Interface/AddOns/CennikAddon"
    addon.mkdir(parents=True)
    (addon / "CennikAddon.toc").write_text("ImportedData.lua")
    saved = game / "WTF/Account/fixture/SavedVariables/CennikAddon.lua"
    saved.parent.mkdir(parents=True)
    saved.write_text("CennikAddonDB=" + lua_literal({"device_id": "fixture-device", "events": [], "market_scans": []}), encoding="utf-8")
    workspace.save_settings(workspace.settings().model_copy(update={"game_root": str(game), "account": "fixture"}))
    workspace.market.import_scan(scan())
    workspace.calculate()
    opportunity = workspace.store.listing(MARKET)["items"][0]
    plan = workspace.plan(PlanRequest(opportunity_id=opportunity["id"], batches=2))
    published = workspace.publish_plan(plan["id"])
    assert not published["reused"]
    package = workspace.exchange.packages(MARKET)[0]
    assert package.tasks[0].lines[0].quantity == 2
    assert package.tasks[0].max_total_cost_copper == 20
    assert workspace.publish_plan(plan["id"])["reused"]
    assert len(workspace.exchange.packages(MARKET)) == 1
    second = workspace.plan(PlanRequest(opportunity_id=opportunity["id"], batches=3))
    with pytest.raises(RuntimeError, match="уже передана"):
        workspace.publish_plan(second["id"])


def test_api_retention_never_removes_own_scans_or_pinned_data(workspace):
    workspace.market.import_scan(scan())
    spec = JobSpec(dataset="signals", url="https://api.ahledger.com/v1/signals/"+MARKET,
                   market=MARKET, params={"discount": "20", "days": "7"})
    parsed = Parsed(data={"signals": [], "filters": {"discount": 20, "days": 7}})
    workspace.market.import_api(spec, parsed, b"fixture-body", {})
    assert workspace.market.prune_api((datetime.now(UTC)+timedelta(days=1)).isoformat()) == 1
    assert len(list((workspace.root / "archive/scans").rglob("*.json.zst"))) == 1
    assert workspace.overview()["archive"]["scans"] == 1
    workspace.market.import_api(spec, parsed, b"fixture-body", {})
    identity = workspace.market.rows("SELECT raw_id FROM dataset_metadata")[0]["raw_id"]
    workspace.market.db.execute("INSERT INTO source_pins VALUES(?)", [identity])
    assert workspace.market.prune_api((datetime.now(UTC)+timedelta(days=1)).isoformat()) == 0


def test_manual_npc_facts_override_sparse_api_without_erasure(workspace):
    from cennik.workspace.models import ItemFacts
    workspace.market.import_scan(scan())
    workspace.update_facts(2589, ItemFacts(vendor_buy_copper=5, vendor_sell_copper=17))
    workspace.market.insert_rows("items", [{"item_id": 2589, "is_placeholder": True,
        "origin_dataset": "pricetable", "raw_id": "sparse", "retrieved_at": datetime.now(UTC).isoformat()}])
    item = workspace.market.rows("SELECT * FROM items WHERE item_id=2589")[0]
    assert item["vendor_buy_copper"] == 5 and item["vendor_sell_copper"] == 17
    assert item["name"] == "Льняная ткань"
    assert item["origin_dataset"] == "manual"


def test_known_own_lots_are_not_procurement_supply(workspace):
    data = scan(count=1)
    data["data"] += ":1"
    workspace.market.import_scan(data)
    workspace.calculate()
    opportunity = workspace.store.listing(MARKET)["items"][0]
    plan = workspace.plan(PlanRequest(opportunity_id=opportunity["id"]))
    assert plan["profit_copper"] is None and not plan["complete"]


def test_installer_preserves_import_and_auctionator(tmp_path):
    game = tmp_path / "Game"
    addons = game / "Interface/AddOns"
    auctionator = addons / "Auctionator"
    auctionator.mkdir(parents=True)
    (auctionator / "fixture.txt").write_text("original")
    addon = addons / "CennikAddon"
    addon.mkdir()
    (addon / "CennikAddon.toc").write_text("## Title: Cennik\nImportedData.lua")
    (addon / "ImportedData.lua").write_text("keep-existing-import")
    result = install_addon(str(game), tmp_path / "backups")
    assert (addon / "MarketCollector.lua").exists()
    assert (addon / "ImportedData.lua").read_text() == "keep-existing-import"
    assert (auctionator / "fixture.txt").read_text() == "original"
    assert result["backup"]


@pytest.mark.asyncio
async def test_local_web_settings_and_origin_isolation(tmp_path):
    app = create_app(tmp_path / "new")
    try:
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 7777))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            response = await client.get("/")
            assert response.status_code == 200 and "next.js" in response.text
            settings = (await client.get("/api/v2/settings")).json()
            assert len(settings["schemas"]["analytics"]["properties"]) >= 25
            assert "max_nodes" in settings["schemas"]["craft_graph"]["properties"]
            assert (await client.get("/api/v2/overview")).status_code == 200
            assert (await client.post("/api/v2/recalculate", headers={"Origin": "https://foreign.invalid"})).status_code == 403
    finally:
        app.state.workspace.close()
