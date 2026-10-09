import json
import subprocess
import threading

import pytest
from conftest import store_response
from fastapi.testclient import TestClient

from ahledger.analytics import build_database
from ahledger.config import Settings
from ahledger.storage import job_key
from ahledger.web import create_app
from ahledger.web_control import Controller, JobRequest

KEY = "test-secret-" + "a" * 40
HEADERS = {"X-API-Key": KEY}


def test_startup_is_idle_and_does_not_create_queue_or_spawn_downloader(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Startup must not launch a downloader")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    root = tmp_path / "empty"
    with TestClient(create_app(Settings(data_dir=root), KEY)) as client:
        assert client.get("/healthz").json()["downloads_autostart"] is False
        status = client.get("/api/status", headers=HEADERS).json()
        assert status["run"]["state"] == "idle" and status["jobs"] == {}
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
    assert not (root / "state.sqlite").exists()


def test_private_api_requires_key_for_reads_and_controls(settings):
    with TestClient(create_app(settings, KEY)) as client:
        for route in ("/api/status", "/api/items", "/api/logs", "/api/files", "/api/openapi.json"):
            assert client.get(route).status_code == 401
            assert client.get(route, headers={"X-API-Key": "wrong"}).status_code == 401
        assert client.post("/api/jobs", json={}).status_code == 401
        assert client.post("/api/jobs/stop").status_code == 401
        assert client.get("/api/openapi.json", headers=HEADERS).status_code == 200


def test_missing_secret_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("AHL_WEB_API_KEY", raising=False)
    with pytest.raises(ValueError, match="AHL_WEB_API_KEY"):
        create_app(Settings(data_dir=tmp_path), "short")


def test_job_validation_does_not_allow_arbitrary_commands_or_high_rates(settings):
    with TestClient(create_app(settings, KEY)) as client:
        for payload in (
            {"action": "shell"},
            {"requests_per_second": 6},
            {"tools_per_second": 2},
            {"markets": ["; rm -rf /"]},
            {"datasets": ["upload"]},
            {"command": "anything"},
        ):
            assert client.post("/api/jobs", json=payload, headers=HEADERS).status_code == 422


def test_manual_commands_preserve_options_and_do_not_request_refresh(settings, monkeypatch):
    monkeypatch.delenv("AHL_DATA_DIR", raising=False)
    controller = Controller(settings)
    request = JobRequest(
        markets=["forever.pvp.horde.us"],
        requests_per_second=2,
        datasets=["history"],
        retry_failed=True,
    )
    controller.run_settings = controller.job_settings(request)
    commands = controller.commands(request)
    assert commands[0][-3:] == ["download", "--no-build", "--retry-failed"]
    assert all("--refresh" not in command for command in commands)
    config = Settings.read(controller.root / "web-config.toml")
    assert config.requests_per_second == 2 and config.markets == request.markets
    assert config.datasets == ["history"]
    assert config.download_datasets() == [
        "catalog",
        "catalog_index",
        "economy",
        "history",
        "markets",
        "pricetable",
    ]


def test_only_one_job_can_run_and_stop_prevents_later_commands(settings, monkeypatch):
    entered, released = threading.Event(), threading.Event()

    def execute(self, request):
        entered.set()
        released.wait(timeout=1)
        self.save(state="stopped")

    monkeypatch.setattr(Controller, "execute", execute)
    controller = Controller(settings)
    controller.start(JobRequest())
    assert entered.wait(timeout=1)
    with pytest.raises(RuntimeError, match="Уже выполняется"):
        controller.start(JobRequest())
    assert controller.stop()["state"] == "stopping"
    assert controller.cancelled.is_set()
    released.set()
    controller.shutdown()
    assert controller.snapshot()["run"]["state"] == "stopped"


def test_restart_records_interruption_and_never_autoresumes(settings, monkeypatch):
    settings.data_dir.mkdir(exist_ok=True)
    (settings.data_dir / "web-run.json").write_text(
        json.dumps({"state": "running", "action": "download", "request": JobRequest().model_dump()})
    )
    controller = Controller(settings)
    assert controller.snapshot()["run"]["state"] == "interrupted"
    assert controller.process is None and controller.thread is None


def test_status_reads_writer_queue_without_modifying_it(state, settings, spec):
    state.enqueue(spec)
    state.db.commit()
    state.claim()
    with TestClient(create_app(settings, KEY)) as client:
        response = client.get("/api/status", headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["jobs"] == {"RUNNING": 1}
    assert state.counts() == {"RUNNING": 1}


def test_items_search_escapes_wildcards_and_bounds_page_size(state, settings):
    state.candidates(
        {2589: {"id": 2589, "name": "Linen Cloth"}, 111: {"id": 111, "name": "Not a match"}},
        catalogue=True,
    )
    state.db.commit()
    with TestClient(create_app(settings, KEY)) as client:
        assert len(client.get("/api/items?q=Linen", headers=HEADERS).json()["items"]) == 1
        assert client.get("/api/items?q=2589", headers=HEADERS).json()["items"][0]["id"] == 2589
        assert client.get("/api/items?q=%25", headers=HEADERS).json()["items"] == []
        assert client.get("/api/items?limit=1001", headers=HEADERS).status_code == 422


def test_price_api_returns_actual_saved_copper_and_no_synthetic_data(
    state, settings, spec, history
):
    store_response(state, spec, history)
    state.set("markets", [{"id": spec.market}])
    build_database(state)
    with TestClient(create_app(settings, KEY)) as client:
        result = client.get(f"/api/prices/{spec.market}/2589", headers=HEADERS).json()
        assert result["currency"] == "copper"
        assert result["points"][0]["median_copper"] == 21
        assert (
            client.get(f"/api/prices/{spec.market}/99999", headers=HEADERS).json()["points"] == []
        )
        assert (
            client.get(
                f"/api/prices/{spec.market}/2589?granularity=invalid", headers=HEADERS
            ).status_code
            == 422
        )


def test_analytics_not_ready_and_download_file_allowlist(state, settings):
    (state.root / "private.txt").write_text("secret")
    with TestClient(create_app(settings, KEY)) as client:
        assert (
            client.get("/api/prices/forever.pvp.horde.us/2589", headers=HEADERS).status_code == 503
        )
        assert client.get("/api/files/private.txt", headers=HEADERS).status_code == 404
        assert client.get("/api/files", headers=HEADERS).json()["files"] == []


def test_manual_download_runs_build_only_after_success(settings, monkeypatch):
    controller = Controller(settings)
    calls = []

    class SuccessfulProcess:
        def __init__(self, command, **kwargs):
            calls.append(command)
            assert "AHL_WEB_API_KEY" not in kwargs["env"]

        def wait(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", SuccessfulProcess)
    controller.execute(JobRequest())
    assert [command[-1] for command in calls] == ["--no-build", "build-db", "verify", "report"]
    assert controller.info["state"] == "completed"


def test_stop_during_download_never_launches_analytics_commands(settings, monkeypatch):
    controller = Controller(settings)
    calls = []

    class InterruptedProcess:
        def __init__(self, command, **kwargs):
            calls.append(command)

        def wait(self):
            controller.cancelled.set()
            return 0

    monkeypatch.setattr(subprocess, "Popen", InterruptedProcess)
    controller.execute(JobRequest())
    assert len(calls) == 1
    assert controller.info["state"] == "stopped"


async def test_dataset_selection_skips_already_queued_other_types(state, settings, spec, history):
    import httpx

    from ahledger.models import JobSpec
    from ahledger.runner import Downloader

    settings.datasets = ["history"]
    other = JobSpec(
        dataset="deals", url="https://api.ahledger.com/v1/deals/forever.pvp.horde.us", priority=-100
    )
    state.enqueue(other)
    state.enqueue(spec)
    state.db.commit()
    runner = Downloader(
        settings,
        state,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=history)),
    )
    runner.expand = False
    await runner.run(quiet=True)
    assert state.counts() == {"COMPLETE": 1, "PENDING": 1}
    assert (
        state.db.execute("SELECT status FROM jobs WHERE id=?", (job_key(other),)).fetchone()[0]
        == "PENDING"
    )
