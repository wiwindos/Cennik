from test_profession_leveling import snapshot
from test_server import AUTH, exchange, register
from test_server import server as server


def test_profession_snapshot_reaches_its_worker_route_and_web(server):
    app, http, profile = server
    device = register(http, profile, two=True)
    crafter = device["clients"][1]
    exchange(http, device["token"], crafter, 1)
    data = {"binding": {"character": "Crafter", "realm": "FixtureRealm", "market": profile.market},
            "production": {"profession_snapshot": snapshot()}}
    exchange(http, device["token"], crafter, 2, data)
    url = f"/m/{profile.id}/api/v2/leveling"
    assert http.get(url).status_code == 401
    response = http.get(url, headers=AUTH)
    assert response.status_code == 200
    captured = response.json()["profiles"]
    assert len(captured) == 1 and captured[0]["id"] == crafter
    assert captured[0]["can_purchase"] and captured[0]["professions"]["tailoring"]["skill"] == 100
    data["production"]["profession_snapshot"]["character"] = "Impersonated"
    exchange(http, device["token"], crafter, 3, data)
    assert http.get(url, headers=AUTH).json()["profiles"][0]["character"] == "Crafter"
