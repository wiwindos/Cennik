"""Real hub sessions and permissions across the separate Cennik ASGI service."""
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "funpayparsers"))
from marketwatch import auth  # noqa: E402
from marketwatch.app import create_app  # noqa: E402
from marketwatch.config import Config  # noqa: E402
from marketwatch.db import Database  # noqa: E402

from cennik.server.app import create_server
from cennik.server.bridge import RegisterDevice

SECRET = "hub-gateway-fixture-" + "x" * 40
OWNER = "old-owner-key-fixture-" + "x" * 40
PASSWORD = "hub-test-password-123"
ORIGIN = "http://127.0.0.1"


@pytest.fixture
def gateway(tmp_path):
    db = Database(tmp_path / "hub.sqlite3")
    db.initialize()
    auth.set_admin(db, "owner", PASSWORD)
    user_id = auth.save_user(db, username="trader", password=PASSWORD, enabled=True, modules=["cennik"])
    backend = create_server(tmp_path / "server", OWNER, public_origin=ORIGIN, hub_secret=SECRET)

    @backend.post("/test/large")
    async def large(request: Request):
        return {"bytes": len(await request.body())}

    hub = create_app(Config(db_path=db.path, environment="development", collect=False,
        cennik_url="http://cennik.internal", cennik_secret=SECRET))
    hub.state.cennik_transport = httpx.ASGITransport(app=backend)
    with TestClient(backend, base_url=ORIGIN) as direct, TestClient(hub, base_url=ORIGIN) as client:
        backend.state.audit_http = direct
        yield backend, hub, db, client, user_id


def login(client, username="trader"):
    assert client.post("/auth/login", json={"username": username, "password": PASSWORD},
        headers={"Origin": ORIGIN}).status_code == 200
    me = client.get("/auth/me").json()
    client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": me["csrf"]})


def test_hub_single_sign_on_paths_and_credential_boundaries(gateway):
    backend, _, _, client, _ = gateway
    assert client.get("/cennik/", follow_redirects=False).status_code == 303
    assert client.get("/cennik/server/api/state").status_code == 401
    login(client)
    assert [m["key"] for m in client.get("/api/hub").json()["modules"]] == ["cennik"]
    page = client.get("/cennik/")
    assert page.status_code == 200 and '/cennik/static/server.js' in page.text
    state = client.get("/cennik/server/api/state")
    assert state.status_code == 200 and OWNER not in state.text and SECRET not in state.text
    profile = state.json()["profiles"][0]
    response = client.get(f"/cennik/m/{profile['id']}/")
    assert response.status_code == 200 and '/cennik/static/next-hub.css' in response.text
    assert client.get('/cennik/static/next.js').status_code == 200
    assert client.get('/api/admin/users').status_code == 403
    payload = {"data": "x" * 20000}
    sent = client.post('/cennik/test/large', json=payload)
    assert sent.status_code == 200 and sent.json()["bytes"] > 20000
    client.headers.pop("X-CSRF-Token")
    assert client.post('/cennik/test/large', json=payload).status_code == 403
    # The old owner key still works directly; an unsigned trust header does not.
    direct = backend.state.audit_http
    assert direct.get('/server/api/state', headers={'X-Cennik-Hub': 'forged'}).status_code == 401
    assert direct.get('/server/api/state', headers={'X-Owner-Key': OWNER}).status_code == 200


def test_grant_revocation_and_public_clients_are_independent(gateway):
    backend, _, db, client, user_id = gateway
    login(client)
    profile = backend.state.catalog.profiles()[0]
    device = backend.state.bridge.register(profile.id, RegisterDevice(label="Game PC"))
    auth.save_user(db, user_id=user_id, password=None, enabled=True, modules=[])
    assert client.get('/cennik/server/api/state').status_code == 403
    assert client.get('/cennik/static/next.js').status_code == 403
    assert client.get('/api/hub').json()['modules'] == []
    assert client.get('/bridge/v1/manifest').status_code == 401
    result = client.get('/bridge/v1/manifest', headers={'X-Bridge-Key': device['token']})
    assert result.status_code == 200 and result.json()['profile']['id'] == profile.id
    assert client.post('/pair/v1/start', json={'label': 'New PC'}).status_code == 200


def test_hub_outage_does_not_break_goldscope_or_hub_identity(gateway):
    _, hub, _, client, _ = gateway
    login(client, "owner")
    hub.state.cennik_transport = httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ConnectError('offline')))
    assert client.get('/cennik/server/api/state').status_code == 502
    assert client.get('/auth/me').json()['is_admin']
    assert client.get('/goldscope').status_code == 200
