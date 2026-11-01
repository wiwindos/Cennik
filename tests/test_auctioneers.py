import copy
import shutil
import time

import pytest
from auction_fixture import approve_hub, context, proposal, registry
from fastapi.testclient import TestClient
from test_accounting import accounting_lua, lua_value
from test_addon import exchange as exchange
from test_cennik import analytics_root as analytics_root
from test_collectors import ADMIN, transport
from test_market_collector import collector
from test_workspace import scan
from test_workspace import workspace as workspace

from ahledger.util import atomic_json
from cennik.addon.lua_data import Parser, lua_literal
from cennik.collector.auctioneers import Review, scan_scope, validate_snapshot
from cennik.collector.client import CollectorClient
from cennik.collector.protocol import Connection, Observation
from cennik.collector.server import create_hub
from cennik.workspace.web import create_app


def test_player_proposal_owner_decision_and_registry_delivery(tmp_path):
    app = create_hub(tmp_path / "hub", ADMIN)
    invite = app.state.hub.invite("Alice")
    with TestClient(app) as http:
        uploader = CollectorClient(tmp_path / "local", transport(http))
        uploader.configure(Connection(enabled=True, url="http://localhost:8768", token=invite["token"], admin_token=ADMIN))
        p = proposal("ordinary").model_dump(mode="json")
        uploader.capture({"auctioneer_proposals": {"npc": p}})
        assert uploader.flush()["sent"] == 2
        overview = uploader.admin(path="/v1/admin/auctioneers")
        row = overview["items"][0]
        assert row["data"]["npc_name"] == "FixtureAuctioneer" and row["data"]["x"] == 50
        assert row["contributors"] == [invite["id"]] and row["registry"] is None
        assert not uploader.registry()["entries"]
        path = "/v1/admin/auctioneers/"+row["record_id"]+"/review"
        body = {"action": "approve", "auction_type": "neutral", "expected_revision": 0}
        assert http.post(path, json=body, headers={"X-Collector-Key": invite["token"]}).status_code == 401
        assert http.get("/v1/auctioneers/registry").status_code == 401
        # The owner may correct the player's proposed type.
        result = uploader.review_auctioneer(row["record_id"], Review(**body))
        assert result["revision"] == 1
        assert uploader.registry()["entries"][0]["auction_type"] == "neutral"
        assert http.post(path, json=body, headers={"X-Admin-Key": ADMIN}).status_code == 422
        uploader.review_auctioneer(row["record_id"], Review(action="revoke", expected_revision=1))
        assert not uploader.registry()["entries"]
        history = app.state.hub.auctioneers.entry(row["scope_id"])["history"]
        assert [h["action"] for h in history] == ["approve", "revoke"]


def test_conflicting_proposals_do_not_override_approval_and_rejection_is_separate(tmp_path):
    app = create_hub(tmp_path / "hub", ADMIN)
    approve_hub(app.state.hub)
    r = app.state.hub.auctioneers
    incoming = Observation(kind="auctioneer_proposal", data=proposal("neutral").model_dump(mode="json"))
    r.suggest(incoming.identity(), incoming.data, "bob")
    r.suggest(incoming.identity(), incoming.data, "alice")
    assert r.wire()["entries"][0]["auction_type"] == "ordinary"
    with pytest.raises(ValueError, match="уже изменено"):
        r.review(incoming.identity(), Review(action="approve", auction_type="neutral", expected_revision=0))
    r.review(incoming.identity(), Review(action="reject", expected_revision=1))
    assert r.wire()["entries"][0]["approved"]
    pending = next(p for p in r.overview()["items"] if p["record_id"] == incoming.identity())
    assert pending["contributors"] == ["alice", "bob"] and pending["review"]["action"] == "reject"


@pytest.mark.parametrize("change", ["missing", "revision", "coords", "realm", "market", "expired", "unapproved", "session"])
def test_untrusted_or_stale_scan_context_fails_closed(change):
    data, snapshot = scan(), registry()
    if change == "missing":
        del data["auction_context"]
    elif change == "revision":
        data["auction_context"]["revision"] += 1
    elif change == "coords":
        data["auction_context"]["x"] += 3
    elif change == "realm":
        data["realm"] = "OtherRealm"
    elif change == "market":
        data["market"] = "forever.pvp.neutral.us"
    elif change == "expired":
        snapshot["fetched_at"] -= 86401
    elif change == "unapproved":
        snapshot["entries"][0]["approved"] = False
    elif change == "session":
        del data["auction_context"]["session_id"]
    assert scan_scope(data, snapshot) == "unknown"


def test_neutral_and_unclassified_archives_never_write_market_prices(workspace):
    workspace.market.import_scan(scan())
    before = workspace.market.rows("SELECT * FROM working_offers")
    generation = workspace.market.generation()
    neutral = scan(offset=3)
    neutral.update(market="forever.pvp.neutral.us", auction_context=context("neutral"))
    atomic_json(workspace.root / "collectors/auctioneer_registry.json", registry("neutral"))
    result = workspace.market.import_scan(neutral)
    assert result["auction_type"] == "neutral" and result["archived_only"]
    assert workspace.market.import_scan(neutral)["reused"]
    unknown = scan(offset=4)
    unknown.pop("auction_context")
    assert workspace.market.import_scan(unknown)["auction_type"] == "unknown"
    assert workspace.market.rows("SELECT * FROM working_offers") == before
    assert workspace.market.generation() == generation
    assert len(workspace.market.rows("SELECT * FROM scan_registry")) == 1
    assert len(list((workspace.root / "archive/neutral_auctions").rglob("*.zst"))) == 1
    assert list((workspace.root / "archive/neutral_auctions/forever.pvp.neutral.us").rglob("*.zst"))
    assert len(list((workspace.root / "archive/unclassified_auctions").rglob("*.zst"))) == 1
    assert len(list((workspace.root / "collectors/scan_receipts").glob("*.json"))) == 2


def test_server_counts_neutral_and_unknown_separately(tmp_path):
    app = create_hub(tmp_path / "hub", ADMIN)
    approve_hub(app.state.hub, "neutral")
    member = app.state.hub.invite("Alice")
    neutral = scan()
    neutral.pop("scan_id")
    neutral.pop("format_version")
    neutral.update(market="forever.pvp.neutral.us", auction_context=context("neutral"))
    one = Observation(kind="scan", data=neutral)
    app.state.hub.accept(member["id"], one)
    app.state.hub.accept(member["id"], one)
    unknown = copy.deepcopy(neutral)
    unknown.pop("auction_context")
    app.state.hub.accept(member["id"], Observation(kind="scan", data=unknown))
    counts = app.state.hub.overview()["members"][0]["counts"]
    assert counts["records"] == 2 and counts["neutral_scans"] == counts["unclassified_scans"] == 1
    assert counts["full_scans"] == counts["partial_scans"] == 0
    assert not list((app.state.hub.root / "raw/scan").glob("*.zst"))


def test_addon_proposal_cannot_approve_and_owner_registry_restores_operations(workspace):
    lua = accounting_lua(workspace)
    lua.execute("Cennik.AuctionContext.registry={};Cennik.AuctionContext:Propose('ordinary')")
    assert not lua.eval("Cennik.AuctionContext:Check('collect')")[0]
    assert not lua.eval("Cennik.AuctionContext:Check('buy')")[0]
    proposed = lua_value(lua.eval("Cennik.db.auctioneer_proposals"))
    assert list(proposed.values())[0]["proposed_type"] == "ordinary"
    assert lua.eval("Cennik.CollectorStatus.db.current.code") == "AUCTION_PROPOSAL_PENDING"
    lua.execute("Cennik.AuctionContext.registry="+lua_literal(registry()))
    assert lua.eval("Cennik.AuctionContext:Check('buy')")[0]
    lua.execute("Cennik.AuctionContext.registry.entries[1].auction_type='neutral'")
    assert lua.eval("Cennik.AuctionContext:Check('collect')")[0]
    assert not lua.eval("Cennik.AuctionContext:Check('buy')")[0]
    lua.execute("Game.protectedCalls=0;C_AuctionHouse.PlaceBid=function() Game.protectedCalls=Game.protectedCalls+1 end")
    assert not lua.eval("Cennik.Adapter:Call('PlaceBid',1,1)")
    assert lua.eval("Game.protectedCalls") == 0
    lua.execute("Cennik.AuctionContext.registry.fetched_at=Game.now-86401")
    assert not lua.eval("Cennik.AuctionContext:Check('collect')")[0]


def test_player_can_propose_correction_without_changing_owner_decision(workspace):
    lua = accounting_lua(workspace)
    assert lua.eval("Cennik.AuctionContext:Propose('neutral')")
    ok, current = lua.eval("Cennik.AuctionContext:Check('buy')")
    assert ok and current["auction_type"] == "ordinary"
    saved = lua_value(lua.eval("Cennik.db.auctioneer_proposals"))
    assert next(iter(saved.values()))["proposed_type"] == "neutral"


@pytest.mark.parametrize("broken", ["UnitGUID=function() return nil end", "C_Map.GetPlayerMapPosition=function() return nil end"])
def test_missing_game_identity_is_blocked_with_diagnostic(workspace, broken):
    lua = accounting_lua(workspace)
    lua.execute(broken)
    assert not lua.eval("Cennik.AuctionContext:Propose('neutral')")
    assert not lua.eval("Cennik.AuctionContext:Check('collect')")[0]
    assert lua.eval("Cennik.CollectorStatus.db.current.code") == "AUCTION_CONTEXT_UNKNOWN"


def test_addon_neutral_full_scan_and_changed_session_response(exchange):
    lua = collector(exchange)
    lua.execute("Cennik.db.collector.market='forever.pvp.alliance.us';Cennik.db.binding.market='forever.pvp.alliance.us';Cennik.AuctionContext.registry.entries[1].auction_type='neutral';Cennik.MarketCollector:Tick(1);Game.event('REPLICATE_ITEM_LIST_UPDATE')")
    data = lua_value(lua.eval("Cennik.db.market_scans[1]"))
    assert data["full"] and data["market"] == "forever.pvp.neutral.us"
    assert data["auction_context"]["auction_type"] == "neutral"
    lua = collector(exchange)
    lua.execute("Cennik.MarketCollector:Tick(1);Cennik.AuctionContext.session='other-session';Game.event('REPLICATE_ITEM_LIST_UPDATE')")
    assert lua.eval("#Cennik.db.market_scans") == 0


def test_invalid_download_is_rejected():
    bad = registry()
    bad["entries"][0].pop("reviewed_at")
    with pytest.raises(ValueError):
        validate_snapshot(bad)
    assert validate_snapshot(registry())["fetched_at"] <= time.time()


def test_changing_collector_server_invalidates_previous_approval(tmp_path):
    client = CollectorClient(tmp_path)
    first = Connection(url="http://localhost:8768", token="first")
    client.configure(first)
    atomic_json(client.root / "auctioneer_registry.json", registry())
    client.configure(first)
    assert client.registry()["entries"]
    client.configure(Connection(url="http://localhost:8769", token="second"))
    assert not client.registry()["entries"] and client.registry()["fetched_at"] == 0


def test_web_review_savedvariables_import_and_addon_reload(tmp_path):
    from pathlib import Path
    hub = create_hub(tmp_path / "hub", ADMIN)
    invitation = hub.state.hub.invite("Player")
    app = create_app(tmp_path / "local")
    w = app.state.workspace
    game = tmp_path / "game"
    addon = game / "Interface/AddOns/CennikAddon"
    shutil.copytree(Path(__file__).resolve().parents[1] / "CennikAddon", addon)
    w.save_settings(w.settings().model_copy(update={"game_root": str(game), "account": "fixture",
        "auto_import": False, "auto_recalculate": False}))
    with TestClient(hub) as remote, TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234)) as web:
        w.collectors.transport = transport(remote)
        w.collectors.configure(Connection(enabled=True, url="http://localhost:8768", token=invitation["token"], admin_token=ADMIN))
        lua = accounting_lua(w)
        lua.execute("Cennik.AuctionContext.registry={};assert(Cennik.AuctionContext:Propose('neutral'))")
        saved = w.game_paths()[1]
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_text("CennikAddonDB="+lua_literal(lua_value(lua.globals().CennikAddonDB)), encoding="utf-8")
        w.synchronize()
        w.deliver_collectors()
        rows = web.get("/api/v2/collectors/auctioneers").json()["items"]
        assert len(rows) == 1 and rows[0]["registry"] is None
        response = web.post("/api/v2/collectors/auctioneers/"+rows[0]["record_id"]+"/review",
            json={"action": "approve", "auction_type": "ordinary", "expected_revision": 0})
        assert response.status_code == 202
        w.future.result(timeout=10)
        job = next(j for j in w.jobs() if j["id"] == response.json()["job_id"])
        assert job["state"] == "completed", job
        parser = Parser((addon / "ImportedData.lua").read_text("utf-8"))
        assert parser.identifier() == "CennikImport"
        parser.require("=")
        incoming = parser.value()
        assert incoming["auctioneer_registry"]["entries"][0]["auction_type"] == "ordinary"
        lua.execute("CennikImport="+lua_literal(incoming)+";Cennik.AuctionContext:Initialize()")
        assert lua.eval("Cennik.AuctionContext:Check('buy')")[0]
        assert not incoming.get("admin_token") and ADMIN not in (addon / "ImportedData.lua").read_text("utf-8")


def test_archived_scan_acknowledged_without_recalculation(workspace, tmp_path, monkeypatch):
    game = tmp_path / "game"
    addon = game / "Interface/AddOns/CennikAddon"
    addon.mkdir(parents=True)
    (addon / "CennikAddon.toc").write_text("## Interface: 11507\n", encoding="utf-8")
    workspace.save_settings(workspace.settings().model_copy(update={"game_root": str(game), "account": "fixture"}))
    data = scan()
    data.pop("auction_context")
    saved = workspace.game_paths()[1]
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text("CennikAddonDB="+lua_literal({"device_id": "archive-device", "market_scans": [data]}), encoding="utf-8")
    monkeypatch.setattr(workspace, "calculate", lambda: pytest.fail("Archive must not recalculate strategies"))
    result = workspace.synchronize()
    assert result["acknowledged_scans"] == 1 and result["scans"][0]["archived_only"]
    assert not workspace.market.rows("SELECT * FROM scan_registry")


def test_quote_cannot_confirm_after_context_changes(exchange):
    from test_addon import exported
    from test_addon_lua import click, runtime
    from test_addon_lua import scan as scan_task
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    scan_task(lua)
    lua.execute("Cennik.BuyQueue:Add(t)")
    scan_task(lua, prepare=True)
    assert click(lua, "Start")
    lua.execute("local p=Cennik.db.pending;Game.event('COMMODITY_PRICE_UPDATED',p.cost/p.quantity,p.cost);assert(p.stage=='quoted');Cennik.AuctionContext.registry.entries[1].auction_type='neutral'")
    before = lua.eval("#Game.calls")
    assert not click(lua, "Confirm")
    assert lua.eval("#Game.calls") == before  # cancellation is allowed but never confirms
    assert lua.eval("Cennik.db.pending") is None
