import hashlib

import duckdb
import pytest
from conftest import store_response
from fastapi.testclient import TestClient
from test_analytics import markets_response

from ahledger.analytics import build_database
from ahledger.config import Settings
from ahledger.local import prepare_local
from ahledger.web import create_app


def saved_database(state, spec, history):
    markets_response(state)
    store_response(state, spec, history)
    build_database(state)
    return state.root / "analytics/ahledger.duckdb"


def local_client(settings, **kwargs):
    return TestClient(
        create_app(settings, local=True),
        base_url="http://127.0.0.1:8765",
        client=("127.0.0.1", 12345),
        **kwargs,
    )


def test_import_uses_standard_path_preserves_source_and_old_db(state, spec, history, tmp_path):
    source = saved_database(state, spec, history)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    settings = Settings(data_dir=tmp_path / "working")
    destination = settings.data_dir / "analytics/ahledger.duckdb"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"old database")
    result = prepare_local(settings, source)
    assert result["method"] in {"hardlink", "copy"}
    assert destination.read_bytes() == source.read_bytes()
    assert (destination.parent / "ahledger.previous.duckdb").read_bytes() == b"old database"
    assert (destination.parent / "imported.json").is_file()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert prepare_local(settings)["method"] == "existing"
    assert not (settings.data_dir / "local").exists()


def test_invalid_import_keeps_current_database(tmp_path):
    settings = Settings(data_dir=tmp_path / "working")
    destination = settings.data_dir / "analytics/ahledger.duckdb"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"current")
    source = tmp_path / "incompatible.duckdb"
    with duckdb.connect(str(source)) as db:
        db.execute("CREATE TABLE unrelated(id INTEGER)")
    with pytest.raises(ValueError, match="отсутствуют таблицы"):
        prepare_local(settings, source)
    assert destination.read_bytes() == b"current"


def test_local_without_key_searches_actual_db_and_protects_imported_snapshot(
    state, spec, history, tmp_path
):
    source = saved_database(state, spec, history)
    settings = Settings(data_dir=tmp_path / "working")
    prepare_local(settings, source)
    with local_client(settings) as client:
        assert client.get("/healthz").json()["local_mode"]
        status = client.get("/api/status").json()
        assert status["analytics_ready"] and status["imported_snapshot"]
        assert status["markets"][0]["id"] == spec.market
        assert client.get("/api/items?q=2589").json()["items"] == [
            {"id": 2589, "name": "Linen Cloth"}
        ]
        assert client.get("/api/items?q=linen").json()["items"][0]["id"] == 2589
        assert client.get("/api/items?q=%25").json()["items"] == []
        assert (
            client.get(f"/api/prices/{spec.market}/2589").json()["points"][0]["median_copper"] == 21
        )
        assert client.post("/api/jobs", json={"action": "build"}).status_code == 409
        assert client.post("/api/jobs", json={"action": "download"}).status_code == 409
        assert client.app.state.controller.thread is None
        assert (
            client.put(
                "/api/analytics/settings",
                json={"active_market": spec.market},
                headers={"Origin": "http://127.0.0.1:8765"},
            ).status_code
            == 200
        )
        assert client.get("/api/analytics/status").json()["active_market"] == spec.market
    assert not (settings.data_dir / "state.sqlite").exists()


def test_local_blocks_remote_clients_foreign_origins_and_host_headers(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with local_client(settings) as client:
        assert client.get("/api/status", headers={"Host": "attacker.example"}).status_code == 403
        assert (
            client.post(
                "/api/jobs/stop", headers={"Origin": "https://attacker.example"}
            ).status_code
            == 403
        )
        assert client.post("/api/jobs/stop", headers={"Origin": "null"}).status_code == 403
        assert (
            client.get("/api/status", headers={"X-Forwarded-For": "203.0.113.10"}).status_code
            == 200
        )
    with TestClient(
        create_app(settings, local=True),
        base_url="http://127.0.0.1:8765",
        client=("203.0.113.10", 12345),
    ) as remote:
        assert (
            remote.get("/api/status", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
        )
