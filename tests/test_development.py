from concurrent.futures import Future
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from test_workspace import scan

from ahledger.models import JobSpec
from cennik.workspace.development import DevelopmentProvider, coverage_report
from cennik.workspace.models import MARKET, RefreshRequest
from cennik.workspace.service import Workspace
from cennik.workspace.web import create_app


@pytest.fixture
def development_workspace(tmp_path):
    value = Workspace(tmp_path / "development")
    try:
        yield value
    finally:
        value.close()


def saved_scan(workspace, when, items):
    value = scan(count=len(items))
    value["observed_at"] = int(when.timestamp())
    value["data"] = ";".join(f"{item}:4:40:0:4:1" for item in items)
    return workspace.market.import_scan(value)


def rich_and_recent(workspace):
    real_now = datetime.now(UTC)
    rich = (real_now - timedelta(days=2)).replace(microsecond=0)
    saved_scan(workspace, rich, [2589, 2592])
    for index in range(3):
        saved_scan(workspace, real_now - timedelta(days=1, minutes=index), [2589])
    return rich


def test_clock_selects_item_coverage_instead_of_duplicate_scan_volume(development_workspace):
    workspace = development_workspace
    rich = rich_and_recent(workspace)
    report = coverage_report(workspace.market, datetime.now(UTC))
    assert report["selected_at"] == rich
    assert max(day["complete_scans"] for day in report["days"]) == 3
    assert max(day["priced_items"] for day in report["days"]) == 2
    saved = workspace.save_settings(workspace.settings().model_copy(update={"development_mode": True}))
    assert saved.development_as_of == rich
    assert workspace.evaluation_now() == rich


def test_saved_evidence_after_frozen_date_is_available_and_live_mode_is_restored(development_workspace):
    workspace = development_workspace
    rich = rich_and_recent(workspace)
    original = workspace.market.rows("SELECT observed_at FROM scan_registry ORDER BY observed_at")
    workspace.save_settings(workspace.settings().model_copy(update={"development_mode": True}))
    generation = workspace.analytics.generation()
    result = workspace.analytics.start(background=False)
    assert result["run"]["status"] == "completed"
    assert result["run"]["development_mode"]
    assert datetime.fromisoformat(result["run"]["as_of"]) == rich
    assert datetime.fromisoformat(result["run"]["started_at"]) > rich
    assert result["run"]["signal_count"] == 2
    assert workspace.analytics.start(background=False)["reused"]
    assert workspace.analytics.status()["data_age_seconds"] == 0
    signals = workspace.store.listing(MARKET)["items"]
    assert all(signal["details"]["development"]["enabled"] for signal in signals)
    assert any(datetime.fromisoformat(signal["observed_at"]) > rich for signal in signals)
    assert original == workspace.market.rows("SELECT observed_at FROM scan_registry ORDER BY observed_at")
    workspace.save_settings(workspace.settings().model_copy(update={"development_mode": False}))
    assert workspace.analytics.generation() != generation
    assert not workspace.analytics.status()["result_generation_matches"]
    assert workspace.analytics.status()["observation_stale"]
    live = workspace.analytics.start(background=False)
    assert not live["reused"] and not live["run"]["development_mode"]
    assert live["run"]["signal_count"] == 0
    assert workspace.settings().development_as_of == rich


def test_development_blocks_even_cached_http_and_preserves_refresh_preferences(development_workspace, monkeypatch):
    workspace = development_workspace
    rich_and_recent(workspace)
    workspace.save_settings(workspace.settings().model_copy(update={"development_mode": True, "auto_refresh": True}))
    client = httpx.Client
    def forbidden(**kwargs):
        pytest.fail("Development mode must not make HTTP requests")
    monkeypatch.setattr("cennik.workspace.service.httpx.Client", forbidden)
    spec = JobSpec(dataset="pricetable", market=MARKET, url=f"https://api.ahledger.com/v1/pricetable/{MARKET}")
    with pytest.raises(RuntimeError, match="Режим разработки"):
        workspace.request(spec)
    with pytest.raises(RuntimeError, match="Режим разработки"):
        workspace.refresh(RefreshRequest(dataset="references"))
    assert workspace.prune_cache()["api_archives_removed"] == 0
    workspace.save_settings(workspace.settings().model_copy(update={"development_mode": False}))
    assert workspace.settings().auto_refresh
    monkeypatch.undo()
    body = f"AHL1|{MARKET}|{int(datetime.now(UTC).timestamp())}|1\n2589:12:10:8:14:15:9:18:10\n".encode()
    monkeypatch.setattr("cennik.workspace.service.httpx.Client", lambda **kwargs:
        client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)), **kwargs))
    assert workspace.request(spec)["changed"]


def test_invalid_and_missing_development_dates_do_not_enable_mode(development_workspace):
    workspace = development_workspace
    with pytest.raises(ValueError, match="нет сохранённых цен"):
        workspace.save_settings(workspace.settings().model_copy(update={"development_mode": True}))
    with pytest.raises(ValueError, match="будущем"):
        workspace.save_settings(workspace.settings().model_copy(update={"development_mode": True,
            "development_as_of": datetime.now(UTC) + timedelta(days=1)}))
    assert not workspace.settings().development_mode


def test_mode_can_change_after_job_commit_while_worker_is_finishing(development_workspace):
    workspace = development_workspace
    rich_and_recent(workspace)
    workspace.future = Future()
    with workspace.store.connection() as db:
        db.execute("INSERT INTO workspace_jobs(id,kind,state,created_at) VALUES('test-finishing','analytics','running',?)",
            [datetime.now(UTC).isoformat()])
    with pytest.raises(RuntimeError, match="текущего задания"):
        workspace.save_settings(workspace.settings().model_copy(update={"development_mode": True}))
    with workspace.store.connection() as db:
        db.execute("UPDATE workspace_jobs SET state='completed' WHERE id='test-finishing'")
    workspace.save_settings(workspace.settings().model_copy(update={"development_mode": True}))
    assert workspace.settings().development_mode
    workspace.future.set_result(None)


def test_api_clock_applies_to_both_recalculate_routes_and_blocks_updates(tmp_path):
    app = create_app(tmp_path / "api")
    workspace = app.state.workspace
    rich_and_recent(workspace)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 4567)) as client:
        config = client.get("/api/v2/settings").json()
        assert config["development"]["selected_at"]
        assert client.put("/api/v2/settings", json={**config["workspace"], "development_mode": True}).status_code == 200
        assert client.post("/api/v2/refresh", json={"dataset": "pricetable"}).status_code == 409
        response = client.post("/api/v2/analytics/recalculate")
        assert response.status_code == 202
        workspace.future.result(timeout=10)
        overview = client.get("/api/v2/overview").json()
        assert overview["analytics"]["development_mode"]
        assert overview["analytics"]["latest_run"]["development_mode"]
        assert overview["analytics"]["opportunity_count"] == 2
        response = workspace.calculate()
        assert response["reused"] and response["run"]["development_mode"]


def test_provider_retains_latest_saved_sales_and_monthly_reference(development_workspace):
    workspace = development_workspace
    rich = rich_and_recent(workspace)
    workspace.market.db.execute("""INSERT INTO source_sales_daily(item_id,market,observed_at,source,
        min_copper,median30d_copper,sales_estimated_per_day,raw_id) VALUES(2592,?,?,'AHL1',5,100,50,'saved-source')""",
        [MARKET, rich + timedelta(hours=1)])
    with DevelopmentProvider(workspace.market, MARKET, rich, datetime.now(UTC)) as provider:
        price = provider.current_prices()[2592]
        assert price.min_copper == 5 and price.sales_estimated_per_day == 50
        assert provider.source_reference(2592)["copper"] == 100
        assert provider.as_of == rich
