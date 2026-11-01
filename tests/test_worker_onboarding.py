import json
import time

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
                    "faction": "alliance",
                    "professions": ["Tailoring"],
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


def test_addon_role_is_per_character_and_afk_guard_stops_during_purchases(server):
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
    lua.execute(
        "Cennik.db.pending=nil;Cennik.db.idle_probe_enabled=true;CennikImport.production={client={role='auctioneer'}};Cennik.Adapter.Open=function() return true end;Cennik.AuctionContext.Check=function() return true end"
    )
    assert lua.eval("Cennik.IdleGuard:Ready()")
    lua.execute("Cennik.db.pending={}")
    assert not lua.eval("Cennik.IdleGuard:Ready()")
    lua.execute("Cennik.db.pending=nil;Cennik.MarketCollector.requested_at=1")
    assert not lua.eval("Cennik.IdleGuard:Ready()")
    lua.execute(
        "Cennik.MarketCollector.requested_at=nil;GetCurrentKeyBoardFocus=function() return {} end"
    )
    assert not lua.eval("Cennik.IdleGuard:Ready()")


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
