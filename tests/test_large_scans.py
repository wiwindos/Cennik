import json
import time

from test_accounting import lua_value
from test_addon import exchange as exchange
from test_cennik import analytics_root as analytics_root
from test_collectors import transport
from test_market_collector import collector
from test_server import ORIGIN, register
from test_server import server as server
from test_workspace import workspace as workspace

from cennik.addon.limits import MAX_SAVED_BYTES, MAX_SAVED_TOKENS
from cennik.addon.lua_data import lua_literal, read_saved_variables
from cennik.worker.agent import WorkerAgent, saved_batch


def test_partial_snapshot_never_becomes_ready_during_cooldown(exchange):
    lua = collector(exchange)
    lua.execute("Cennik.MarketCollector.maxBytes=20;Cennik.MarketCollector:Tick(1);Game.event('REPLICATE_ITEM_LIST_UPDATE');Game.advance(30);Cennik.MarketCollector:Tick(1)")
    assert lua.eval("Cennik.CollectorStatus.db.current.code") == "SCAN_PARTIAL"
    assert not lua.eval("Cennik.db.collector.last_scan.full")
    assert "НЕПОЛНАЯ" in lua.eval("Cennik.CollectorStatus:Summary()")


def test_timeout_after_previous_success_is_not_reported_as_ready(exchange):
    lua = collector(exchange)
    lua.execute("Cennik.db.collector.last_scan={full=true,count=10};Cennik.MarketCollector:Tick(1);Game.advance(121);Cennik.MarketCollector:Tick(1);Game.advance(30);Cennik.MarketCollector:Tick(1)")
    assert lua.eval("Cennik.CollectorStatus.db.current.code") == "SCAN_TIMEOUT"
    assert not lua.eval("Cennik.db.collector.last_scan.full")


def test_large_scan_does_not_evict_good_queue_before_it_finishes(exchange):
    lua = collector(exchange)
    lua.execute("Cennik.db.market_scans={{scan_id='good',full=true}};C_AuctionHouse.GetNumReplicateItems=function() return 10000 end;Cennik.MarketCollector:Tick(1);Game.event('REPLICATE_ITEM_LIST_UPDATE')")
    assert lua.eval("Cennik.MarketCollector.packing~=nil")
    assert lua.eval("Cennik.db.market_scans[1].scan_id") == "good"
    lua.execute("Game.event('AUCTION_HOUSE_CLOSED');Game.advance(0)")
    assert lua.eval("Cennik.db.market_scans[1].scan_id") == "good"
    assert not lua.eval("Cennik.db.market_scans[2].full")


def test_three_snapshots_are_batched_and_file_is_finished_only_after_last_one():
    saved = {"binding": {"character": "Fixture"}, "market_scans": [{"scan_id": str(i), "data": "lot"} for i in range(3)]}
    previous = {"scan_ids": [], "scan_stamp": None}
    for i in range(3):
        result = saved_batch(saved, (100, 200), previous)
        assert result["scan_ids"] == [str(i)]
        assert len(result["saved"]["market_scans"]) == 1
        assert result["snapshot_final"] == (i == 2)
        previous = {"scan_stamp": [100, 200], "scan_ids": previous["scan_ids"] + result["scan_ids"]}
    assert len(saved["market_scans"]) == 3
    assert saved_batch(saved, (100, 201), previous)["scan_ids"] == ["0"]


def test_worker_delivers_three_scans_and_keeps_acknowledgements_across_restart(server, tmp_path, monkeypatch):
    from auction_fixture import proposal
    from test_workspace import scan

    from cennik.collector.auctioneers import Review
    from cennik.collector.protocol import Observation

    app, http, profile = server
    npc = proposal().model_copy(update={"server_id": profile.id, "market": profile.market})
    record = Observation(kind="auctioneer_proposal", data=npc.model_dump(mode="json"))
    app.state.hub.auctioneers.suggest(record.identity(), record.data, "fixture")
    app.state.hub.auctioneers.review(record.identity(), Review(action="approve", auction_type="ordinary"))
    device = register(http, profile)
    route = device["clients"][0]
    game = tmp_path / "game"
    (game / "Interface/AddOns/Auctionator").mkdir(parents=True)
    agent = WorkerAgent(tmp_path / "worker", transport(http))
    agent.configure(str(game), ORIGIN, device["token"])
    scans = [scan(count=3, offset=i) for i in range(3)]
    for s in scans:
        s["market"] = profile.market
        s["auction_context"].update(scope_id=npc.scope_id(), server_id=profile.id)
    saved = game / "WTF/Account/first/SavedVariables/CennikAddon.lua"
    saved.parent.mkdir(parents=True)
    saved.write_text("CennikAddonDB="+lua_literal({"binding": {"market": profile.market,
        "realm": "FixtureRealm", "character": "FixturePlayer"}, "market_scans": scans}), "utf-8")
    agent.tick(force=True)

    def settle():
        for key, (stamp, _) in list(agent.quiet.items()):
            agent.quiet[key] = stamp, time.monotonic()-2

    settle()
    agent.tick(force=True)
    assert agent.overview()["status"] == "Работает", agent.report()
    assert agent.read("state-"+route+".json", {})["stamp"] is None
    assert len(agent.read("imports/"+route+".json", {})["acknowledged_scan_ids"]) == 1
    agent = WorkerAgent(tmp_path / "worker", transport(http))
    agent.tick(force=True)  # A stable save is not yet available after restart.
    assert len(agent.read("state-"+route+".json", {})["scan_ids"]) == 1
    settle()
    agent.tick(force=True)
    assert agent.read("state-"+route+".json", {})["stamp"] is None
    assert len(agent.read("imports/"+route+".json", {})["acknowledged_scan_ids"]) == 2
    agent.tick(force=True)
    assert agent.read("state-"+route+".json", {})["stamp"] is not None
    assert agent.read("imports/"+route+".json", {})["acknowledged_scan_ids"] == [s["scan_id"] for s in scans]
    assert app.state.catalog.workspace(profile.id).market.rows("SELECT count(*) n FROM scan_registry")[0]["n"] == 3
    def unexpected_read(*args, **kwargs):
        raise AssertionError("Completed, unchanged saves must not be parsed again")
    monkeypatch.setattr("cennik.worker.agent.read_saved_variables", unexpected_read)
    agent.tick(force=True)
    assert agent.overview()["status"] == "Работает"


def test_one_million_rows_lua_save_parser_and_database(exchange, workspace, tmp_path):
    started = time.perf_counter()
    lua = collector(exchange)
    lua.execute("""
      C_AuctionHouse.GetNumReplicateItems=function() return 1000000 end
      C_AuctionHouse.GetReplicateItemInfo=function(index)
        return 'Тестовый предмет ' .. (index%50000),nil,4,nil,nil,nil,nil,nil,nil,40000+index%50000,0,nil,nil,nil,nil,nil,2589,true
      end
      C_AuctionHouse.GetReplicateItemLink=function(index) return 'item:2589:0:0:' .. (index%50000) end
      Cennik.MarketCollector:Tick(1);Game.event('REPLICATE_ITEM_LIST_UPDATE')
    """)
    assert lua.eval("Cennik.MarketCollector.packing~=nil")
    assert lua.eval("#Cennik.MarketCollector.packing.parts") <= 500
    lua.execute("Game.advance(0)")
    scan = lua_value(lua.eval("Cennik.db.market_scans[1]"))
    assert scan["full"] and scan["count"] == scan["reported_count"] == 1_000_000
    assert len(scan["variants"]) == 50_000
    assert scan["packed_bytes"] <= 48_000_000
    lua_seconds = time.perf_counter() - started
    scan["market"] = workspace.market_key
    scan["observed_at"] = int(time.time()) - 1
    saved = {"market_scans": [{**scan, "scan_id": str(i)} for i in range(3)]}
    encoded = "CennikAddonDB=" + lua_literal(saved)
    parse_start = time.perf_counter()
    parsed = read_saved_variables(encoded, max_bytes=MAX_SAVED_BYTES, max_tokens=MAX_SAVED_TOKENS)
    parse_seconds = time.perf_counter() - parse_start
    assert parsed["market_scans"][0]["data"] == scan["data"]
    assert len(encoded.encode()) > 64_000_000
    assert len(saved_batch(parsed, (1, 2), {})["saved"]["market_scans"]) == 1
    import_start = time.perf_counter()
    result = workspace.market.import_scan(parsed["market_scans"][0])
    assert result["complete"] and result["rows"] == 1_000_000
    assert workspace.market.rows("SELECT count(*) n FROM working_offers")[0]["n"] == 1_000_000
    metrics = {"rows": 1_000_000, "variants": 50_000, "lua_seconds": round(lua_seconds, 3),
               "parse_seconds": round(parse_seconds, 3), "import_seconds": round(time.perf_counter()-import_start, 3),
               "saved_bytes": len(encoded.encode()), "budget_bytes": scan["packed_bytes"]}
    (tmp_path / "large-scan-metrics.json").write_text(json.dumps(metrics, indent=2), "utf-8")
    print(json.dumps(metrics))
