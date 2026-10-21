import json
from datetime import UTC, datetime

import duckdb
import httpx
import pytest
from fastapi.testclient import TestClient
from test_cennik import HEADERS, KEY, NOW, configured, insert
from test_cennik import analytics_root as analytics_root
from test_craft_graph import add_intermediate

from ahledger.config import Settings
from ahledger.web import create_app
from cennik.addon.bridge import Bridge
from cennik.addon.local_sync import LocalBridge
from cennik.addon.lua_data import lua_literal, read_saved_variables
from cennik.addon.models import ExportRequest, ReportBatch, ReportEvent
from cennik.addon.service import AddonExchange, ExchangeConflict
from cennik.data.duckdb import fingerprint


@pytest.fixture
def exchange(analytics_root):
    add_intermediate(analytics_root)
    with duckdb.connect(str(analytics_root / "analytics/ahledger.duckdb")) as db:
        db.execute("UPDATE items SET vendor_buy_copper=NULL WHERE item_id=9")
        insert(
            db,
            "price_snapshots",
            item_id=9,
            market="market-a",
            observed_at=NOW,
            min_copper=4,
            quantity=10,
            source="fixture ore",
        )
    service = configured(analytics_root, max_price_age_seconds=31536000)
    assert service.start(background=False, as_of=NOW)["run"]["status"] == "completed"
    return AddonExchange(service)


def exported(exchange, strategy="craft_graph", **kwargs):
    identity = exchange.store.listing("market-a", strategy=strategy)["items"][0]["id"]
    return exchange.export(
        ExportRequest(request_id="request-" + strategy, opportunity_ids=[identity], **kwargs)
    )


@pytest.mark.parametrize(
    "strategy", ["vendor_arbitrage", "historical_discount", "simple_craft", "craft_graph"]
)
def test_export_four_real_strategies_copper_quantities_and_no_source_change(exchange, strategy):
    before = fingerprint(exchange.analytics.path)
    package = exported(exchange, strategy)
    (task,) = package.tasks
    assert task.market == "market-a" and task.opportunity.execution_status == "indicative"
    assert task.opportunity_id == task.opportunity.id and task.run_id == task.opportunity.run_id
    assert task.expires_at_epoch > int(datetime.now(UTC).timestamp())
    if strategy in ("simple_craft", "craft_graph"):
        assert task.opportunity.quantity == 2
        assert any(line.source == "npc" for line in task.lines)
        assert [line.quantity for line in task.lines] == [
            m["quantity"] for m in task.opportunity.details["materials"]
        ]
    if strategy == "craft_graph":
        assert len(task.opportunity.details["steps"]) == 2
    assert fingerprint(exchange.analytics.path) == before


def test_explicit_price_override_budget_idempotency_and_duplicate_procurement(exchange):
    package = exported(exchange, "simple_craft", unit_limits={10: 45}, max_budget_copper=123)
    task = package.tasks[0]
    assert task.lines[0].unit_limit_copper == 45
    assert task.max_total_cost_copper == 95 and package.max_budget_copper == 123
    request = ExportRequest(
        request_id="request-simple_craft",
        opportunity_ids=[task.opportunity_id],
        unit_limits={10: 45},
        max_budget_copper=123,
    )
    assert exchange.export(request) == package
    with pytest.raises(ExchangeConflict):
        exchange.export(request.model_copy(update={"max_budget_copper": 124}))
    with pytest.raises(ExchangeConflict):
        exchange.export(request.model_copy(update={"request_id": "duplicate-request"}))


@pytest.mark.parametrize(
    "field", ["result_generation_matches", "settings_match", "strategy_versions_match"]
)
def test_export_rejects_stale_analytics_and_unknown_scope(exchange, monkeypatch, field):
    real = exchange.analytics.status
    status = real()
    status[field] = False
    monkeypatch.setattr(exchange.analytics, "status", lambda: status)
    with pytest.raises(ExchangeConflict):
        exported(exchange, "vendor_arbitrage")


def test_export_rejects_old_prices_wrong_ids_market_and_limits(exchange, monkeypatch):
    status = exchange.analytics.status()
    status["observation_stale"] = True
    real = exchange.analytics.status
    monkeypatch.setattr(exchange.analytics, "status", lambda: status)
    with pytest.raises(ExchangeConflict):
        exported(exchange)
    monkeypatch.setattr(exchange.analytics, "status", real)
    with pytest.raises(ExchangeConflict):
        exchange.export(ExportRequest(request_id="bad-op-id", opportunity_ids=["missing"]))
    with pytest.raises(ValueError):
        exported(exchange, unit_limits={999: 10})
    with pytest.raises(ExchangeConflict):
        exchange.packages("other-market")
    exchange.analytics.lock.acquire()
    try:
        with pytest.raises(ExchangeConflict):
            exported(exchange)
    finally:
        exchange.analytics.lock.release()


def event_for(task, identity="device:1", **fields):
    return ReportEvent(
        event_id=identity,
        task_id=task.task_id,
        opportunity_id=task.opportunity_id,
        run_id=task.run_id,
        market=task.market,
        kind="purchase",
        observed_at=datetime.now(UTC),
        state="bought",
        quantity=1,
        cost_copper=4,
        **fields,
    )


def test_reports_scope_idempotency_collision_atomicity_and_unchanged_opportunity(exchange):
    task = exported(exchange).tasks[0]
    before = exchange.store.detail("market-a", task.opportunity_id)
    event = event_for(task)
    batch = ReportBatch(events=[event])
    assert exchange.reports(batch) == [event.event_id]
    assert exchange.reports(batch) == [event.event_id]
    assert len(exchange.history()) == 1
    assert exchange.store.detail("market-a", task.opportunity_id) == before
    with pytest.raises(ExchangeConflict):
        exchange.reports(ReportBatch(events=[event.model_copy(update={"cost_copper": 5})]))
    with pytest.raises(ValueError):
        exchange.reports(
            ReportBatch(
                events=[
                    event.model_copy(update={"event_id": "device:2"}),
                    event.model_copy(update={"event_id": "device:3", "market": "market-b"}),
                ]
            )
        )
    assert len(exchange.history()) == 1


def test_addon_routes_authorized_in_existing_server(exchange):
    with TestClient(create_app(Settings(data_dir=exchange.analytics.root), KEY)) as client:
        for endpoint in ("packages?market=market-a", "history"):
            assert client.get("/api/addon/" + endpoint).status_code == 401
        assert client.post("/api/addon/exports", json={}).status_code == 401
        assert client.post("/api/addon/reports", json={}).status_code == 401
        identity = exchange.store.listing("market-a", strategy="vendor_arbitrage")["items"][0]["id"]
        response = client.post(
            "/api/addon/exports",
            headers=HEADERS,
            json={"request_id": "api-request", "opportunity_ids": [identity]},
        )
        assert response.status_code == 201
        assert (
            len(
                client.get("/api/addon/packages?market=market-a", headers=HEADERS).json()[
                    "packages"
                ]
            )
            == 1
        )
        assert client.get("/api/addon/packages?market=market-b", headers=HEADERS).status_code == 409


def test_saved_variables_literal_roundtrip_and_no_code_execution():
    data = {
        "device_id": "тест",
        "events": [{"event_id": "abc:1", "details": {"text": 'quotes " \\ \n\001'}, "quantity": 3}],
        "empty": {},
    }
    assert read_saved_variables("CennikAddonDB = " + lua_literal(data)) == data
    assert (
        read_saved_variables(
            'CennikAddonDB = { ["events"] = { -- comment\n [1] = {kind="scan",quantity=1}, }, }'
        )["events"][0]["kind"]
        == "scan"
    )
    for text in (
        'CennikAddonDB=os.execute("bad")',
        'CennikAddonDB={}; print("bad")',
        "OtherDB={}",
        "CennikAddonDB={x=function() end}",
        "CennikAddonDB={x=1,x=2}",
        "CennikAddonDB={" * 50,
    ):
        with pytest.raises(ValueError):
            read_saved_variables(text)


def test_bridge_download_report_retry_ack_and_does_not_write_savedvariables(exchange, tmp_path):
    package = exported(exchange)
    task = package.tasks[0]
    addon = tmp_path / "CennikAddon"
    addon.mkdir()
    (addon / "CennikAddon.toc").write_text("ImportedData.lua")
    saved = tmp_path / "CennikAddon.lua"
    event = event_for(task).model_dump(mode="json")
    saved.write_text("CennikAddonDB=" + lua_literal({"events": [event]}), encoding="utf-8")
    original = saved.read_bytes()
    received = []

    def respond(request):
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"local_mode": True})
        if request.url.path == "/api/addon/reports":
            batch = ReportBatch.model_validate_json(request.content)
            received.append(batch)
            return httpx.Response(200, json={"accepted_event_ids": exchange.reports(batch)})
        return httpx.Response(
            200, json={"format_version": 1, "packages": [package.model_dump(mode="json")]}
        )

    bridge = Bridge(
        "http://127.0.0.1:8765",
        addon,
        saved,
        "market-a",
        tmp_path / "state.json",
        transport=httpx.MockTransport(respond),
    )
    try:
        assert bridge.synchronize()["packages"] == 1
        assert "CennikImport =" in (addon / "ImportedData.lua").read_text(encoding="utf-8")
        bridge.synchronize()
        assert len(received) == 1 and saved.read_bytes() == original
    finally:
        bridge.close()
    assert json.loads((tmp_path / "state.json").read_text())["acknowledged"] == [event["event_id"]]


def test_remote_bridge_refuses_missing_tls_or_key(tmp_path):
    for url in ("http://example.com", "https://example.com"):
        with pytest.raises(ValueError):
            Bridge(url, tmp_path, tmp_path / "saved", "market-a", tmp_path / "state")


def test_local_page_sync_preserves_device_reports_and_savedvariables(exchange, tmp_path):
    package = exported(exchange)
    root = exchange.analytics.root
    game = tmp_path / "Game with spaces"
    addon = game / "Interface/AddOns/CennikAddon"
    addon.mkdir(parents=True)
    (addon / "CennikAddon.toc").write_text("ImportedData.lua")
    saved = game / "WTF/Account/account#5/SavedVariables/CennikAddon.lua"
    saved.parent.mkdir(parents=True)
    event = event_for(package.tasks[0]).model_dump(mode="json")
    saved.write_text("CennikAddonDB=" + lua_literal({"events": [event]}), encoding="utf-8")
    original = saved.read_bytes()
    config = root / "bridge/local.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"game_root": str(game), "account": "account#5"}))
    state = root / "bridge/state.json"
    state.write_text(json.dumps({"device_id": "existing-device", "acknowledged": []}))
    local = LocalBridge(root, exchange)
    assert local.status()["ready"]
    before = fingerprint(exchange.analytics.path)
    with TestClient(create_app(Settings(data_dir=root), local=True),
                    base_url="http://127.0.0.1", client=("127.0.0.1", 1234)) as client:
        assert client.get("/api/addon/local-sync/status").json()["ready"]
        first = client.post("/api/addon/local-sync")
        assert first.status_code == 200, first.text
        assert first.json() == {"packages": 1, "acknowledged_events": 1}
        assert client.post("/api/addon/local-sync").json() == first.json()
    assert '"existing-device"' in (addon / "ImportedData.lua").read_text(encoding="utf-8")
    assert saved.read_bytes() == original
    assert len(exchange.history()) == 1
    assert fingerprint(exchange.analytics.path) == before


def test_local_sync_not_available_in_server_mode(exchange):
    with TestClient(create_app(Settings(data_dir=exchange.analytics.root), KEY)) as client:
        assert client.get("/api/addon/local-sync/status", headers=HEADERS).json() == {"available": False}
        assert client.post("/api/addon/local-sync", headers=HEADERS).status_code == 404


def test_local_sync_missing_configuration_and_invalid_account(exchange, tmp_path):
    local = LocalBridge(tmp_path, exchange)
    assert not local.status()["ready"]
    with pytest.raises(ValueError):
        local.synchronize()
    local.config_path.parent.mkdir(parents=True)
    local.config_path.write_text(json.dumps({"game_root": str(tmp_path), "account": "../other"}))
    assert not local.status()["ready"]


def test_manual_renew_preserves_task_ids_limits_and_reported_purchase(exchange):
    package = exported(exchange)
    task = package.tasks[0]
    exchange.reports(ReportBatch(events=[event_for(task)]))
    renewed = exchange.renew(package.package_id)
    assert renewed.package_id == package.package_id and renewed.tasks[0].task_id == task.task_id
    assert renewed.tasks[0].lines == task.lines and len(exchange.history()) == 1
    assert renewed.expires_at >= package.expires_at
    assert exchange.packages("market-a", include_expired=True)[0] == renewed
