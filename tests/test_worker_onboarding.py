import json
import time

import httpx
import pytest
from test_accounting import accounting_lua
from test_collectors import transport
from test_server import AUTH, ORIGIN
from test_server import server as server

from cennik.addon.lua_data import Parser, lua_literal
from cennik.worker.agent import WorkerAgent


def test_pairing_needs_owner_and_discovers_two_accounts_without_manual_names(server, tmp_path):
    app, http, profile = server
    app.state.catalog.save(profile.model_copy(update={"realm_names": []}))
    game = tmp_path / "game"
    (game / "Interface/AddOns/Auctionator").mkdir(parents=True)
    agent = WorkerAgent(tmp_path / "worker", transport(http))
    agent.configure(str(game), ORIGIN)
    code = agent.pairing["code"]
    assert code in agent.overview()["status"]
    assert not app.state.bridge.devices()
    body = {"code": code, "profile_id": profile.id}
    assert (
        http.post("/server/api/pairing/approve", json=body, headers={"Origin": ORIGIN}).status_code
        == 401
    )
    assert http.post("/server/api/pairing/approve", json=body, headers=AUTH).status_code == 200
    agent.tick(force=True)
    assert agent.manifest["clients"] == [] and agent.pairing is None
    assert not list((app.state.catalog.root / "pairing").glob("*.json"))
    for account, role, name in [
        ("first", "auctioneer", "FixturePlayer"),
        ("second", "crafter", "Crafter"),
    ]:
        target = game / "WTF/Account" / account / "SavedVariables/CennikAddon.lua"
        target.parent.mkdir(parents=True)
        saved = {
            "character_profiles": {
                name: {
                    "role": role,
                    "character": name,
                    "realm": "FixtureRealm",
                    "market": profile.market,
                    "game_version": "1.60.1",
                    "faction": "alliance",
                    "professions": [] if role == "auctioneer" else ["Tailoring"],
                }
            }
        }
        target.write_text("CennikAddonDB=" + lua_literal(saved), "utf-8")
    agent.tick(force=True)
    time.sleep(1.05)
    agent.tick(force=True)
    assert agent.overview()["status"] == "Работает", agent.overview()
    assert {r["role"] for r in agent.manifest["clients"]} == {"auctioneer", "crafter"}
    ids = {r["id"] for r in agent.manifest["clients"]}
    parser = Parser((game / "Interface/AddOns/CennikAddon/ImportedData.lua").read_text("utf-8"))
    assert parser.identifier() == "CennikImport"
    parser.require("=")
    incoming = parser.value()
    assert set(incoming["routes"]) == {"FixtureRealm:FixturePlayer", "FixtureRealm:Crafter"}
    assert incoming["discovery"]["worker_discovery"]
    assert agent.client.connection().token not in json.dumps(incoming)
    agent.discovery_hash = ""
    agent.tick(force=True)
    assert {r["id"] for r in agent.manifest["clients"]} == ids
    assert len(app.state.catalog.workspace(profile.id).production.clients()) == 2
    assert app.state.catalog.profile(profile.id).realm_names == ["FixtureRealm"]


def test_server_accepts_empty_lua_professions_from_old_workers(server):
    app, http, profile = server
    from cennik.server.bridge import RegisterDevice

    device = app.state.bridge.register(profile.id, RegisterDevice(label="Old Worker"))
    character = {"role": "auctioneer", "character": "FixturePlayer", "realm": "FixtureRealm",
                 "account": "first", "faction": "alliance", "professions": {}}
    headers = {"X-Bridge-Key": device["token"]}
    response = http.post("/bridge/v1/characters", json={"characters": [character]}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["clients"][0]["role"] == "auctioneer"
    assert app.state.bridge.devices()[0]["discovered"][0]["professions"] == []
    character["professions"] = {"unexpected": "not an array"}
    assert http.post("/bridge/v1/characters", json={"characters": [character]}, headers=headers).status_code == 422


def test_revoked_saved_key_restarts_pairing_only_on_explicit_save(server, tmp_path):
    app, http, profile = server
    from cennik.server.bridge import RegisterDevice

    original = app.state.bridge.register(profile.id, RegisterDevice(label="Old PC"))
    game = tmp_path / "classic_era"
    retail = tmp_path / "retail"
    for folder in (game, retail):
        (folder / "Interface/AddOns/Auctionator").mkdir(parents=True)
    agent = WorkerAgent(tmp_path / "worker", transport(http))
    agent.configure(str(game), ORIGIN, original["token"])
    agent.write_import()
    assert http.delete("/server/api/workers/" + original["id"], headers=AUTH).status_code == 200
    agent.last_manifest = 0
    agent.tick(force=True)
    assert json.loads(agent.report())["error_code"] == "WORKER_KEY_REJECTED"
    assert agent.pairing is None
    assert not list((app.state.catalog.root / "pairing").glob("*.json"))
    pending = agent.root / "requests" / ("a" * 32 + ".json")
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text('{"saved":"keep queued data"}', "utf-8")

    result = agent.configure(str(retail), ORIGIN, "")
    assert result["pair_code"] == agent.pairing["code"]
    assert agent.manifest is None and agent.discovery_hash == ""
    assert not agent.client.connection().token and not agent.client.connection().enabled
    assert agent.settings["game_root"] == str(retail.resolve())
    assert pending.read_text("utf-8") == '{"saved":"keep queued data"}'
    text = (retail / "Interface/AddOns/CennikAddon/ImportedData.lua").read_text("utf-8")
    assert "worker_discovery" in text and original["id"] not in text
    agent.tick(force=True)
    assert "Подтвердите код" in agent.overview()["status"]
    # The previous key stays revoked; only a fresh owner approval grants access.
    assert http.get("/bridge/v1/manifest", headers={"X-Bridge-Key": original["token"]}).status_code == 401
    assert http.post("/server/api/pairing/approve", json={"code": result["pair_code"],
                     "profile_id": profile.id}, headers=AUTH).status_code == 200
    agent.tick(force=True)
    assert agent.pairing is None and agent.manifest["device_id"] != original["id"]
    assert agent.overview()["status"] == "Работает"


def test_explicit_invalid_key_does_not_silently_create_pairing(server, tmp_path):
    app, http, _ = server
    game = tmp_path / "game"
    (game / "Interface/AddOns/Auctionator").mkdir(parents=True)
    agent = WorkerAgent(tmp_path / "worker", transport(http))
    with pytest.raises(ValueError, match="Очистите поле ключа"):
        agent.configure(str(game), ORIGIN, "revoked-manual-key")
    assert agent.pairing is None
    assert not list((app.state.catalog.root / "pairing").glob("*.json"))


@pytest.mark.parametrize("status", [403, 500])
def test_server_errors_do_not_replace_saved_connection_with_new_pairing(server, tmp_path, status):
    app, http, profile = server
    from cennik.server.bridge import RegisterDevice

    original = app.state.bridge.register(profile.id, RegisterDevice(label="PC"))
    game = tmp_path / "game"
    (game / "Interface/AddOns/Auctionator").mkdir(parents=True)
    agent = WorkerAgent(tmp_path / "worker", transport(http))
    agent.configure(str(game), ORIGIN, original["token"])
    agent.client.transport = httpx.MockTransport(lambda request: httpx.Response(status))
    with pytest.raises(httpx.HTTPStatusError):
        agent.configure(str(game), ORIGIN)
    assert agent.pairing is None
    assert agent.client.connection().token == original["token"]


def test_worker_reports_discovery_fields_without_server_inputs_and_clears_on_recovery(server, tmp_path):
    app, http, profile = server
    from cennik.server.bridge import RegisterDevice

    device = app.state.bridge.register(profile.id, RegisterDevice(label="PC"))
    game = tmp_path / "game"
    (game / "Interface/AddOns/Auctionator").mkdir(parents=True)
    forwarding = transport(http)
    fail = True
    sent = []

    def handle(request):
        if request.url.path == "/bridge/v1/characters":
            sent.append(json.loads(request.content))
            if fail:
                return httpx.Response(422, json={"detail": [
                    {"loc": ["body", "characters", 0, "professions"], "input": "PRIVATE_INPUT",
                     "msg": "PRIVATE_MESSAGE " + device["token"]},
                    {"loc": ["body", "PRIVATE_LOCATION"], "input": "PRIVATE_INPUT"},
                ]})
        return forwarding.handle_request(request)

    agent = WorkerAgent(tmp_path / "worker", httpx.MockTransport(handle))
    agent.configure(str(game), ORIGIN, device["token"])
    saved = game / "WTF/Account/first/SavedVariables/CennikAddon.lua"
    saved.parent.mkdir(parents=True)
    saved.write_text('CennikAddonDB={character_profiles={test={role="auctioneer",character="FixturePlayer",realm="FixtureRealm",faction="alliance",professions={}}}}', "utf-8")
    agent.tick(force=True)
    for key, (stamp, _) in list(agent.quiet.items()):
        agent.quiet[key] = stamp, time.monotonic() - 2
    agent.tick(force=True)
    report = json.loads(agent.report())
    assert report["error_code"] == "CHARACTER_DISCOVERY_INVALID"
    assert report["details"] == {"operation": "character_discovery", "http_status": 422,
                                 "validation_fields": ["body.characters.0.professions"]}
    assert "HTTP 422" in agent.overview()["error"]
    assert "PRIVATE_" not in agent.report() and device["token"] not in agent.report()
    assert sent[-1]["characters"][0]["professions"] == []
    fail = False
    agent.tick(force=True)
    assert agent.overview()["status"] == "Работает", agent.report()
    assert json.loads(agent.report())["details"] == {}
    assert json.loads(agent.report())["error_code"] == ""


def test_addon_role_is_per_character_without_jump_activation(server):
    app, _, profile = server
    lua = accounting_lua(app.state.catalog.workspace(profile.id))
    lua.execute("CennikImport.worker_discovery=true;Cennik.CharacterRole:Choose('auctioneer')")
    assert (
        lua.eval("Cennik.db.character_profiles[GetRealmName()..':'..UnitName('player')].role")
        == "auctioneer"
    )
    lua.execute("Cennik.db.pending={};Cennik.CharacterRole:Choose('crafter')")
    assert (
        lua.eval("Cennik.db.character_profiles[GetRealmName()..':'..UnitName('player')].role")
        == "auctioneer"
    )
    assert lua.eval("Cennik.IdleGuard") is None


def test_modern_recipe_requires_exact_output_and_unambiguous_reagents(server):
    app, _, profile = server
    lua = accounting_lua(app.state.catalog.workspace(profile.id))
    lua.execute(
        "C_TradeSkillUI={GetRecipeSchematic=function() return {outputItemID=4238,quantityMin=1,quantityMax=1,reagentSlotSchematics={{required=true,reagentType=1,quantityRequired=3,reagents={{itemID=2589}}}}} end}; r=Cennik:ObservedModernRecipe(100,'Bag')"
    )
    assert lua.eval("r.inputs[1].quantity") == 3 and lua.eval("r.qty_min") == 1
    lua.execute(
        "C_TradeSkillUI.GetRecipeSchematic=function() return {outputItemID=4238,quantityMin=1,quantityMax=1,reagentSlotSchematics={{required=true,reagentType=1,quantityRequired=3,reagents={{itemID=2589},{itemID=2592}}}}} end;r=Cennik:ObservedModernRecipe(100,'Bag')"
    )
    assert lua.eval("r.inputs") is None


def test_pair_secret_is_not_exposed_to_owner_state_or_other_devices(server):
    _, http, profile = server
    started = http.post("/pair/v1/start", json={"label": "Fixture"}).json()
    state = http.get("/server/api/state", headers=AUTH).text
    assert started["secret"] not in state and started["code"] in state
    assert http.post("/pair/v1/poll", json={"secret": "z" * 48}).status_code == 410
    assert (
        http.post(
            "/server/api/pairing/approve",
            json={"code": started["code"], "profile_id": profile.id},
            headers=AUTH,
        ).status_code
        == 200
    )
    paired = http.post("/pair/v1/poll", json={"secret": started["secret"]}).json()
    assert paired["state"] == "approved"
    assert paired["token"] not in http.get("/server/api/state", headers=AUTH).text
    assert (
        http.post("/pair/v1/poll", json={"secret": started["secret"]}).json()["token"]
        == paired["token"]
    )
