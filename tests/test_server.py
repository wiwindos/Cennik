import json
import time

import pytest
from auction_fixture import proposal
from fastapi.testclient import TestClient
from test_accounting import accounting_lua
from test_bulk import book, opportunity, setup
from test_collectors import transport

from cennik.addon.lua_data import Parser, lua_literal
from cennik.collector.client import CollectorClient
from cennik.collector.protocol import Connection
from cennik.server.app import create_server
from cennik.server.catalog import MarketProfile
from cennik.worker.agent import WorkerAgent

OWNER = "owner-test-"+"x"*40
ORIGIN = "http://127.0.0.1"
AUTH = {"X-Owner-Key": OWNER}


@pytest.fixture
def server(tmp_path):
    app = create_server(tmp_path / "server", OWNER, public_origin=ORIGIN)
    with TestClient(app, base_url=ORIGIN, client=("203.0.113.2", 1234)) as http:
        p = app.state.catalog.profiles()[0]
        p = p.model_copy(update={"realm_names": ["FixtureRealm"]})
        assert http.post("/server/api/markets", json={"profile": p.model_dump(mode="json"), "ahl_source": True}, headers=AUTH).status_code == 200
        yield app, http, p


def register(http, p, *, two=False):
    routes = [{"role": "auctioneer", "character": "FixturePlayer", "realm": "FixtureRealm", "account": "first"}]
    if two:
        routes.append({"role": "crafter", "character": "Crafter", "realm": "FixtureRealm", "account": "second"})
    r = http.post(f"/server/api/markets/{p.id}/workers", json={"label": "PC", "clients": routes}, headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def exchange(http, token, route, sequence, saved=None):
    headers = {"X-Bridge-Key": token}
    r = http.post("/bridge/v1/exchange", headers=headers, json={"client_id": route, "sequence": sequence, "saved": saved})
    assert r.status_code == 202, r.text
    result = r.json()
    for _ in range(100):
        if result["state"] != "queued":
            break
        time.sleep(.02)
        result = http.get("/bridge/v1/exchanges/"+route, headers=headers).json()
    assert result["state"] == "completed", result
    return result["response"]


def test_owner_auth_sessions_csrf_and_role_isolation(server):
    app, http, p = server
    assert http.get("/server/api/state").status_code == 401
    assert http.get(f"/m/{p.id}/api/v2/overview").status_code == 401
    assert http.post("/auth/login", json={"key": OWNER}).status_code == 403
    assert http.post("/auth/login", json={"key": "bad"}, headers={"Origin": ORIGIN}).status_code == 401
    assert http.post("/auth/login", json={"key": OWNER}, headers={"Origin": ORIGIN}).status_code == 200
    cookie = http.cookies.get("cennik_owner")
    assert http.get("/server/api/state").status_code == 200
    assert http.get(f"/m/{p.id}/api/v2/overview").json()["market"] == p.market
    assert http.post("/auth/logout", json={}, headers={"Origin": "https://evil.invalid"}).status_code == 403
    assert http.post("/auth/logout", json={}, headers={"Origin": ORIGIN}).status_code == 200
    assert http.get("/server/api/state", headers={"Cookie": "cennik_owner="+cookie}).status_code == 401
    worker = register(http, p)
    assert http.get("/server/api/state", headers={"X-Bridge-Key": worker["token"]}).status_code == 401
    invite = http.post("/server/api/collectors", json={"label": "Collector", "profile_id": p.id}, headers=AUTH).json()
    assert http.get("/bridge/v1/manifest", headers={"X-Bridge-Key": invite["token"]}).status_code == 401
    assert worker["token"] not in http.get("/server/api/state", headers=AUTH).text


def test_private_exchange_preserves_full_payloads_accounting_and_retries(server):
    app, http, p = server
    device = register(http, p, two=True)
    first, second = device["clients"]
    initial = exchange(http, device["token"], first, 1)
    payload = initial["payload"]
    assert payload["production"]["client"]["role"] == "auctioneer"
    assert payload["market_binding"]["id"] == p.id
    assert payload["market_collector"]["market"] == p.market
    assert payload["device_id"] == first
    w = app.state.catalog.workspace(p.id)
    event = {"event_id": first+":ledger:1", "workspace_id": w.identity, "market": p.market,
        "realm": "FixtureRealm", "character": "FixturePlayer", "observed_at": int(time.time()),
        "kind": "purchase", "outputs": [{"item_id": 2589, "quantity": 3}], "money_copper": 30}
    saved = {"device_id": first, "binding": {"realm": "FixtureRealm", "character": "FixturePlayer", "market": p.market},
        "accounting": {"events": [event]}}
    response = exchange(http, device["token"], first, 2, saved)
    assert event["event_id"] in response["payload"]["accounting"]["acknowledged_event_ids"]
    assert w.accounting.overview()["summary"]["purchase_copper"] == 30
    assert exchange(http, device["token"], first, 2, saved) == response
    reordered = json.loads(json.dumps(saved, sort_keys=True))
    assert exchange(http, device["token"], first, 2, reordered) == response
    assert w.accounting.overview()["summary"]["purchase_copper"] == 30
    crafted = exchange(http, device["token"], second, 1)
    assert crafted["payload"]["packages"] == [] and "sniper_book" not in crafted["payload"]
    assert crafted["payload"]["production"]["client"]["role"] == "crafter"
    bad = http.post("/bridge/v1/exchange", headers={"X-Bridge-Key": device["token"]},
        json={"client_id": second, "sequence": 2, "saved": saved})
    assert bad.status_code == 422
    assert http.delete("/server/api/workers/"+device["id"], headers=AUTH).json()["revoked"]
    assert http.get("/bridge/v1/manifest", headers={"X-Bridge-Key": device["token"]}).status_code == 401


def test_profiles_are_isolated_and_only_one_ahl_source_is_enabled(server):
    app, http, p = server
    other = MarketProfile(label="Другой сервер", region="us", realm_names=["OtherRealm"], ahl_market="forever.pvp.alliance.us")
    assert http.post("/server/api/markets", json={"profile": other.model_dump(mode="json")}, headers=AUTH).status_code == 200
    w = app.state.catalog.workspace(p.id)
    other_w = app.state.catalog.workspace(other.id)
    assert w.root != other_w.root and w.market_key != other_w.market_key
    assert w.settings().auto_recalculate and other_w.settings().auto_recalculate
    other_w.save_settings(other_w.settings().model_copy(update={"auto_recalculate": False}))
    assert not other_w.settings().auto_recalculate
    with pytest.raises(RuntimeError, match="другой сервер"):
        other_w.require_live_updates()
    p2 = p.model_copy(update={"label": "Новое имя"})
    assert http.post("/server/api/markets", json={"profile": p2.model_dump(mode="json")}, headers=AUTH).status_code == 200
    assert app.state.catalog.profile(p.id).market == p.market
    assert app.state.catalog.workspace(p.id).root == w.root
    assert not other_w.market.rows("SELECT * FROM working_offers")


def test_collector_registry_scopes_and_crowd_scan_reaches_only_its_market(server):
    app, http, p = server
    invitation = http.post("/server/api/collectors", json={"label": "Collector", "profile_id": p.id}, headers=AUTH).json()
    uploader = CollectorClient(app.state.catalog.root / "fixture-uploader", transport(http))
    uploader.configure(Connection(enabled=True, url=ORIGIN+"/collect", token=invitation["token"]))
    data = proposal().model_dump(mode="json")
    uploader.queue("auctioneer_proposal", data)
    assert uploader.flush()["sent"] == 1
    prefix = f"/m/{p.id}/api/v2/collectors/auctioneers"
    row = http.get(prefix, headers=AUTH).json()["items"][0]
    r = http.post(prefix+"/"+row["record_id"]+"/review", json={"action": "approve", "auction_type": "ordinary"}, headers=AUTH)
    assert r.status_code == 202
    w = app.state.catalog.workspace(p.id)
    w.future.result(timeout=10)
    uploader.flush()
    snapshot = uploader.registry()
    assert snapshot["market_catalog"][0]["id"] == p.id
    entry = snapshot["entries"][0]
    scan = {"market": p.market, "realm": "FixtureRealm", "faction": "alliance", "observed_at": int(time.time())-1,
        "count": 1, "full": True, "data": "2589:4:40:0:4:1", "variants": [{"link": "item:2589", "divisible": True}],
        "auction_context": {"scope_id": entry["scope_id"], "revision": 1, "server_id": p.id, "auction_type": "ordinary",
            "npc_id": 12345, "realm": "FixtureRealm", "map_id": 84, "x": 50, "y": 60, "session_id": "fixture"}}
    uploader.queue("scan", scan)
    assert uploader.flush()["sent"] == 1
    for _ in range(100):
        if w.market.rows("SELECT * FROM working_offers"):
            break
        time.sleep(.05)
    assert len(w.market.rows("SELECT * FROM working_offers")) == 1
    w.future.result(timeout=10)
    latest = w.store.latest(p.market, summary=True)
    assert latest is not None and latest.status == "completed"
    assert list((app.state.hub.root / "raw" / p.id / "scan").glob("*.zst"))


def test_worker_agent_roundtrip_writes_addressed_import_without_local_db(server, tmp_path):
    app, http, p = server
    device = register(http, p, two=True)
    game = tmp_path / "game"
    (game / "Interface/AddOns/Auctionator").mkdir(parents=True)
    agent = WorkerAgent(tmp_path / "worker", transport(http))
    agent.configure(str(game), ORIGIN, device["token"])
    agent.tick(force=True)
    path = game / "Interface/AddOns/CennikAddon/ImportedData.lua"
    parser = Parser(path.read_text("utf-8"))
    assert parser.identifier() == "CennikImport"
    parser.require("=")
    incoming = parser.value()
    assert incoming["format_version"] == 2 and len(incoming["routes"]) == 2
    assert incoming["remote_device_id"] == device["id"]
    assert device["token"] not in path.read_text("utf-8")
    assert not list(agent.root.rglob("*.duckdb")) and not list(agent.root.rglob("*.sqlite"))
    assert agent.overview()["status"] == "Работает", agent.overview()


def test_npc_observations_require_matching_realm_market_and_faction(server):
    app, http, p = server
    horde = MarketProfile(label="Орда", faction="horde", realm_names=["HordeRealm"], ahl_market=None)
    app.state.catalog.save(horde)
    invite = app.state.hub.invite("NPC fixture")
    uploader = CollectorClient(app.state.catalog.root / "npc-uploader", transport(http))
    uploader.configure(Connection(enabled=True, url=ORIGIN+"/collect", token=invite["token"]))
    for item_id, realm, market in [(2589, "HordeRealm", horde.market),
                                    (2592, "WrongRealm", horde.market),
                                    (4306, "HordeRealm", p.market)]:
        uploader.queue("npc", {"server_id": horde.id, "market": market, "realm": realm,
            "item_id": item_id, "batch_price_copper": 10, "batch_quantity": 1,
            "observed_at": int(time.time())-1})
    assert uploader.flush()["sent"] == 3
    w = app.state.catalog.workspace(horde.id)
    for _ in range(150):
        if len(list((app.state.collectors.root / "done").glob("*.json"))) >= 3:
            break
        time.sleep(.05)
    assert set(w.npc_references.snapshot()) == {2589}
    assert not app.state.catalog.workspace(p.id).npc_references.snapshot()
    states = [json.loads(path.read_text("utf-8"))["state"]
              for path in (app.state.collectors.root / "done").glob("*.json")]
    assert states.count("archived_unassigned") == 2


def test_bulk_publish_production_and_transfer_reports_all_run_on_server(server, tmp_path):
    app, http, p = server
    device = register(http, p, two=True)
    auctioneer, crafter = device["clients"]
    w = app.state.catalog.workspace(p.id)
    value = opportunity().model_copy(update={"market": p.market})
    value.details["recipe"] = {"spell_id": 100, "result_item_id": 4238, "qty_min": 1,
        "inputs": [{"item_id": 2589, "quantity": 3}], "profession": "Tailoring"}
    run, status, _, _ = setup(w, tmp_path, value)
    run.market = p.market
    status.update(active_market=p.market, published_run=run.model_dump(mode="json"))
    prefix = f"/m/{p.id}/api/v2"
    result = http.post(prefix+"/bulk", json={"opportunity_ids": [value.id]}, headers=AUTH)
    assert result.status_code == 202
    w.future.result(timeout=10)
    first = exchange(http, device["token"], auctioneer, 1)
    packet = first["payload"]["bulk_checks"]["packet"]
    assert packet["market"] == p.market
    report = {"report_id": auctioneer+":bulk:1", "batch_id": packet["batch_id"], "workspace_id": w.identity,
        "market": p.market, "started_at": packet["created_at"], "completed_at": int(time.time()), "status": "complete",
        "items": [book(2589), book(4238, ((100, 10),))]}
    source = {"device_id": auctioneer, "binding": {"market": p.market, "realm": "FixtureRealm", "character": "FixturePlayer"},
        "bulk_checks": {"reports": [report]}}
    second = exchange(http, device["token"], auctioneer, 2, source)
    assert report["report_id"] in second["payload"]["bulk_checks"]["acknowledged_report_ids"]
    result = http.post(prefix+"/bulk/"+packet["batch_id"]+"/publish", json={"choices": [{"opportunity_id": value.id, "batches": 2}]}, headers=AUTH)
    assert result.status_code == 202
    w.future.result(timeout=10)
    tasks = [t for package in w.exchange.packages(p.market) for t in package.tasks]
    assert tasks, w.jobs()
    created = http.post(prefix+"/production/orders", json={"crafter_id": crafter, "task_ids": [tasks[0].task_id]}, headers=AUTH)
    assert created.status_code == 202
    w.future.result(timeout=10)
    response = exchange(http, device["token"], crafter, 1)
    order = response["payload"]["production"]["orders"][0]
    assert order["crafter_id"] == crafter and response["payload"]["packages"] == []
    raw = w.production.orders()[0]
    forward = raw["transfers"][0]
    event = {"event_id": auctioneer+":production:1", "workspace_id": w.identity, "client_id": auctioneer,
        "order_id": raw["order_id"], "observed_at": int(time.time()), "kind": "sent", "transfer_id": forward["id"],
        "part_id": auctioneer+":1", "mode": "mail", "lines": forward["lines"]}
    source["production"] = {"events": [event]}
    sent = exchange(http, device["token"], auctioneer, 3, source)
    assert event["event_id"] in sent["payload"]["production"]["acknowledged_event_ids"]
    received = {**event, "client_id": crafter, "event_id": crafter+":production:1", "kind": "received"}
    exchange(http, device["token"], crafter, 2, {"device_id": crafter, "binding": {"market": p.market,
        "realm": "FixtureRealm", "character": "Crafter"}, "production": {"events": [received]}})
    assert w.production.status()[0]["transfers"][0]["parts"][0]["state"] == "confirmed"


def test_private_exchange_hands_purchasing_plan_to_crafter(server, tmp_path):
    from cennik.addon.models import ExportRequest

    app, http, p = server
    device = register(http, p, two=True)
    auctioneer, crafter = device["clients"]
    w = app.state.catalog.workspace(p.id)
    value = opportunity().model_copy(update={"market": p.market})
    value.details["recipe"] = {"spell_id": 100, "result_item_id": 4238, "qty_min": 1,
        "inputs": [{"item_id": 2589, "quantity": 3}], "profession": "Tailoring"}
    run, status, _, _ = setup(w, tmp_path, value)
    run.market = p.market
    status.update(active_market=p.market, published_run=run.model_dump(mode="json"))
    package = w.exchange.export(ExportRequest(request_id="self-purchase-http", opportunity_ids=[value.id]))
    task = package.tasks[0]
    request = {"request_id": "addon-self-purchase-http", "crafter_id": crafter,
        "task_ids": [task.task_id], "procurement": "self_purchase",
        "purchase_limits": {task.task_id: {str(task.lines[0].item_id): 15}},
        "task_budgets": {task.task_id: 100}}
    saved = {"device_id": auctioneer, "binding": {"market": p.market, "realm": "FixtureRealm", "character": "FixturePlayer"},
        "production": {"plan_requests": [request]}}
    result = exchange(http, device["token"], auctioneer, 1, saved)
    assert result["payload"]["packages"] == []
    assert result["payload"]["production"]["plan_results"][0]["state"] == "accepted"
    # A new exchange carrying the same persisted SavedVariables is idempotent.
    exchange(http, device["token"], auctioneer, 2, saved)
    target = exchange(http, device["token"], crafter, 1)["payload"]
    assert len(w.production.orders()) == 1
    assert target["packages"][0]["tasks"][0]["task_id"] == task.task_id
    assert target["packages"][0]["tasks"][0]["lines"][0]["unit_limit_copper"] == 15
    assert target["production"]["orders"][0]["transfers"] == []
    assert target["production"]["orders"][0]["procurement"] == "self_purchase"
    assert exchange(http, device["token"], auctioneer, 3)["payload"]["packages"] == []


def test_catalog_binding_in_addon_selects_eu_and_refuses_ambiguous_servers(server):
    app, _, p = server
    w = app.state.catalog.workspace(p.id)
    lua = accounting_lua(w, collector_only=True)
    lua.execute("GetBuildInfo=function() return '1.60.1','fixture' end")
    catalog = app.state.collectors.market_catalog()
    lua.execute("CennikImport.market_collector={workspace_id='catalog',auto_scan=true};CennikImport.market_catalog="+lua_literal(catalog)+";Cennik:Initialize()")
    assert lua.eval("Cennik.db.current_market") == p.market
    assert lua.eval("Cennik.db.server_binding.id") == p.id
    assert lua.eval("Cennik.db.binding.market") == p.market
    other = p.model_copy(update={"id": "a"*32})
    duplicate = {**catalog[0], "id": other.id, "market": f"forever.{other.id}.alliance.eu"}
    lua.execute("CennikImport.market_catalog="+lua_literal([catalog[0], duplicate])+";Cennik:Initialize()")
    assert lua.eval("Cennik.db.binding") is None
    assert not lua.eval("Cennik.AuctionContext:Check('collect')")[0]
