from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from test_accounting import accounting_lua
from test_collector_agent import game, imported, saved
from test_collectors import ADMIN, sample, transport
from test_workspace import workspace as workspace

from cennik.addon.lua_data import lua_literal
from cennik.collector.agent import CollectorAgent
from cennik.collector.protocol import Observation
from cennik.collector.server import create_hub

MARKET = "forever." + "a" * 32 + ".alliance.eu"


def binding(**changes):
    return {"id": "a" * 32, "market": MARKET, "faction": "alliance", "realm_names": ["FixtureRealm"],
            "client_family": "forever", "label": "Forever EU", "region": "eu", **changes}


def load(lua, catalog, *, faction="Alliance", version="1.60.1"):
    lua.execute("CennikImport.market_catalog=" + lua_literal(catalog))
    lua.execute(f"GetBuildInfo=function() return '{version}','70790' end; UnitFactionGroup=function() return '{faction}' end; Cennik:Initialize()")


@pytest.mark.parametrize(("catalog", "faction", "version", "reason"), [
    ([], "Alliance", "1.60.1", "catalog_missing"),
    ([binding(realm_names=["AnotherRealm"])], "Alliance", "1.60.1", "realm_mismatch"),
    ([binding()], "Horde", "1.60.1", "faction_mismatch"),
    ([binding()], "Alliance", "12.1.5", "client_mismatch"),
    ([binding(), binding(market=MARKET.replace("a" * 32, "b" * 32))], "Alliance", "1.60.1", "ambiguous"),
])
def test_unbound_reason_is_visible_and_never_bypasses_isolation(workspace, catalog, faction, version, reason):
    lua = accounting_lua(workspace, collector_only=True)
    load(lua, catalog, faction=faction, version=version)
    assert lua.eval("Cennik:CollectorBindingInfo().reason") == reason
    assert lua.eval("Cennik.db.binding") is None
    assert lua.eval("Cennik.db.collector_status.realm") == "FixtureRealm"
    assert lua.eval("Cennik.db.collector_status.faction") == faction.lower()
    lua.execute("Game.advance(2); Cennik.CollectorUI:ToggleMenu()")
    assert lua.eval("Cennik.CollectorUI.marketWindow:IsShown()")
    assert lua.eval("Cennik.CollectorUI.menuButtons[9]:IsShown()")
    assert lua.eval("Cennik.CollectorUI.menuButtons[9]:IsEnabled()")
    assert not lua.eval("Cennik.CollectorUI.menuButtons[1]:IsShown()")
    lua.execute("SlashCmdList.CENNIK('market')")
    assert lua.eval("Cennik.CollectorUI.marketText:GetText():find('FixtureRealm',1,true)")
    assert lua.eval("Cennik.db.binding") is None


def test_ambiguous_market_selection_is_trusted_persistent_and_applied_on_reload(workspace):
    lua = accounting_lua(workspace, collector_only=True)
    other = MARKET.replace("a" * 32, "b" * 32)
    load(lua, [binding(), binding(market=other, id="b" * 32)])
    assert lua.eval("Cennik:ChooseCollectorMarket('forever.pvp.horde.us')")[0] is False
    assert lua.eval(f"Cennik:ChooseCollectorMarket('{other}')")[0]
    lua.execute("Cennik:Initialize()")  # same SavedVariables, as after /reload
    assert lua.eval("Cennik.db.current_market") == other
    assert lua.eval(f"Cennik:MarketBindingMatches('{other}')")
    load(lua, [binding()])  # revoked/removed selected market cannot stay bound
    assert lua.eval("Cennik.db.current_market") == MARKET
    assert lua.eval("Cennik:ChooseCollectorMarket('forever.pvp.alliance.us')")[0] is False


def test_collector_market_command_and_button_apply_only_a_matching_catalog_entry(workspace):
    lua = accounting_lua(workspace, collector_only=True)
    load(lua, [binding()])
    assert lua.eval(f"Cennik:MarketBindingMatches('{MARKET}')")
    lua.execute("SlashCmdList.CENNIK('market'); reloads=0; ReloadUI=function() reloads=reloads+1 end")
    lua.execute("Cennik.CollectorUI.marketButtons[1]:GetScript('OnClick')()")
    assert lua.eval("reloads") == 1
    lua.execute(f"SlashCmdList.CENNIK('market {MARKET}')")
    assert lua.eval("Cennik.db.collector_market_choices['FixtureRealm:alliance']") == MARKET
    lua.execute("Cennik.CollectorUI.marketReload:GetScript('OnClick')()")
    assert lua.eval("reloads") == 2


def test_unbound_transition_cannot_move_old_observations_to_another_market(workspace):
    lua = accounting_lua(workspace, collector_only=True)
    load(lua, [binding()])
    lua.execute("Cennik.db.npc_quotes={old_marker=true}")
    load(lua, [])
    other = MARKET.replace("a" * 32, "b" * 32)
    load(lua, [binding(market=other, id="b" * 32)])
    assert lua.eval("(Cennik.db.npc_quotes or {}).old_marker") is None
    assert lua.eval(f"Cennik.db.observation_profiles['{MARKET}'].npc_quotes.old_marker")
    load(lua, [binding()])
    assert lua.eval("Cennik.db.npc_quotes.old_marker")


def test_first_install_without_catalog_cannot_fall_back_to_us(tmp_path):
    agent = CollectorAgent(tmp_path / "agent")
    root = game(tmp_path)
    agent.configure(str(root), "http://localhost", "fixture-key")
    data = imported(root)
    assert data["market_collector"]["market"] == "unassigned"
    assert not data["market_catalog"]


def test_upgrade_refreshes_the_old_unbound_hint_without_resetting_error_history(workspace):
    lua = accounting_lua(workspace, collector_only=True)
    lua.execute("Cennik.CollectorStatus.db.current={code='UNBOUND',level='error',action='OLD_HINT'}; Cennik.CollectorStatus.db.errors.UNBOUND={count=70}")
    load(lua, [])
    assert "Подключение / рынок" in lua.eval("Cennik.CollectorStatus.db.current.action")
    assert lua.eval("Cennik.CollectorStatus.db.errors.UNBOUND.count") == 70


@pytest.mark.parametrize("realm", ["CAMWOW EU4 Realm 4 PvP", "CAMWOW EU5 Realm 6 PvP"])
def test_approved_technical_realm_aliases_resolve_to_the_same_ruleset_market(workspace, realm):
    lua = accounting_lua(workspace, collector_only=True)
    lua.execute("GetRealmName=function() return " + lua_literal(realm) + " end")
    load(lua, [binding(realm_names=["CAMWOW EU4 Realm 4 PvP", "CAMWOW EU5 Realm 6 PvP"])])
    assert lua.eval("Cennik:CollectorBindingInfo().reason") == "bound"
    assert lua.eval(f"Cennik:MarketBindingMatches('{MARKET}')")
    assert lua.eval("Cennik.db.binding.realm") == realm


def test_unbound_diagnostic_preserves_actual_realm_and_faction_without_private_character(tmp_path):
    app = create_hub(tmp_path / "hub", ADMIN)
    invitation = app.state.hub.invite("Collector")
    root = game(tmp_path)
    with TestClient(app) as http:
        agent = CollectorAgent(tmp_path / "agent", transport(http))
        agent.configure(str(root), "http://localhost", invitation["token"])
        source = sample()
        source.pop("server_binding", None)
        source.pop("binding", None)
        source["market_scans"] = []
        source["collector_status"].update(realm="Actual Realm", faction="horde", binding_reason="faction_mismatch", addon_version="0.24.0",
            current={"at": int(datetime.now(UTC).timestamp()), "code": "UNBOUND", "level": "error"})
        saved(root, source)
        agent.tick(force=True)
        status = agent.overview()
        assert status["status"] == "Сбор заблокирован" and status["game_error"]
        assert "Actual Realm" in status["notice"]
        diagnostic = app.state.hub.members()[0]["diagnostic"]
        assert diagnostic["realm"] == "Actual Realm" and diagnostic["faction"] == "horde"
        assert diagnostic["binding_reason"] == "faction_mismatch"
        assert "character" not in diagnostic
        assert diagnostic["addon_version"]


@pytest.mark.parametrize("changes", [{"binding_reason": "anything"}, {"faction": "fake"}, {"realm": "x" * 161}])
def test_new_diagnostic_fields_are_bounded(changes):
    with pytest.raises(ValueError):
        Observation(kind="diagnostic", data={"at": int(datetime.now(UTC).timestamp()), "code": "UNBOUND", **changes})
