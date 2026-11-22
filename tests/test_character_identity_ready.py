from pathlib import Path

import pytest
from lupa.lua51 import LuaRuntime
from cennik.markets import equivalent_realms, realm_matches
from cennik.worker.agent import scoped_saved

ROOT = Path(__file__).resolve().parents[1]
BINDING=dict(id='a'*32,market='forever.'+'a'*32+'.alliance.eu',label='Forever PvP EU',
             region='eu',faction='alliance',client_family='forever',realm_names=['CAMWOW EU5 Realm 6 PvP'])


def addon():
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute((ROOT / 'tests/lua/wow_mock.lua').read_text('utf-8'))
    lua.execute('ns={}; function module(source) assert(loadstring(source))("CennikAddon",ns) end')
    for line in (ROOT / 'CennikAddon/CennikAddon.toc').read_text('utf-8').splitlines():
        if line and not line.startswith('#'):
            lua.globals().module((ROOT / 'CennikAddon' / line.replace('\\', '/')).read_text('utf-8'))
    lua.globals().binding=lua.table_from(BINDING,recursive=True)
    lua.execute('''
      CennikImport={format_version=2,worker_agent_id="fixture-agent",
        routes={["FixtureRealm:FixturePlayer"]={format_version=1,device_id="route-device",packages={}}},
        discovery={format_version=1,device_id="discovery-device",packages={},market_binding=binding,
          market_collector={market=binding.market,workspace_id="discovery"}}}
      envelope=CennikImport
    ''')
    return lua


@pytest.mark.parametrize('missing', [
    'UnitName=function() return nil end',
    'UnitFactionGroup=function() return nil end',
])
def test_login_waits_for_identity_and_preserves_addressed_import(missing):
    lua = addon()
    lua.execute(missing + '; Game.event("ADDON_LOADED","CennikAddon")')
    assert lua.eval('Cennik.db') is None
    assert lua.eval('CennikImport==envelope')
    lua.execute('Game.event("PLAYER_LOGIN");Game.advance(3)')
    assert lua.eval('Cennik.db') is None
    assert lua.eval('#Game.timers') == 1
    lua.execute('''
      GetRealmName=function() return "FixtureRealm" end
      UnitName=function() return "FixturePlayer" end
      UnitFactionGroup=function() return "Alliance" end
      Game.event("PLAYER_ENTERING_WORLD")
    ''')
    assert lua.eval('Cennik.initialized')
    assert lua.eval('Cennik.db.device_id') == 'route-device'
    assert lua.eval('Cennik.db.character_profiles["FixtureRealm:FixturePlayer"].realm') == 'FixtureRealm'
    assert lua.eval('Cennik.db.character_profiles[":FixturePlayer"]') is None
    lua.execute('Cennik.Initialize=function() error("duplicate initialization") end;Game.advance(2);Game.event("PLAYER_LOGIN")')


def test_retry_recovers_without_another_login_event_and_capture_never_saves_empty_realm():
    lua = addon()
    lua.execute('oldName=UnitName;UnitName=function() return nil end;Game.event("ADDON_LOADED","CennikAddon")')
    lua.execute('UnitName=oldName;Game.advance(1)')
    assert lua.eval('Cennik.initialized')
    lua.execute('''
      Cennik.CharacterRole:Capture("auctioneer")
      previous=Cennik.db.character_profiles["FixtureRealm:FixturePlayer"]
      GetRealmName=function() return "" end
      Cennik.CharacterRole:Capture("crafter")
    ''')
    assert lua.eval('Cennik.db.character_profiles["FixtureRealm:FixturePlayer"]==previous')
    assert lua.eval('previous.role') == 'auctioneer'
    assert lua.eval('Cennik.db.character_profiles[":FixturePlayer"]') is None


@pytest.mark.parametrize('realm', ['""','nil','"  "'])
def test_hidden_realm_keeps_ui_available_but_requires_explicit_confirmation(realm):
    lua=addon()
    lua.execute('''
      GetRealmName=function() return REALM_VALUE end
      GetNormalizedRealmName=function() return "" end
      GetBuildInfo=function() return "1.60.1" end
      UnitName=function() return "Wiw","Aust" end
      UnitGUID=function() return "Player-6948-008583F4" end
    '''.replace('REALM_VALUE',realm))
    lua.execute('Game.event("ADDON_LOADED","CennikAddon");Game.event("AUCTION_HOUSE_SHOW")')
    assert lua.eval('Cennik.initialized')
    assert lua.eval('Cennik.UI.frame~=nil')
    assert not lua.eval('Cennik:MarketBindingMatches(binding.market)')
    assert lua.eval('(Cennik.db.character_profiles or {})[":Wiw Aust"]') is None
    lua.execute('ok=Cennik:BindMarket(binding.market)')
    assert lua.eval('ok')
    assert lua.eval('Cennik:RealmName()')=='forever-eu-pvp'
    assert lua.eval('Cennik:CharacterName()')=='Wiw Aust'
    assert lua.eval('Cennik:MarketBindingMatches(binding.market)')
    assert lua.eval('Cennik.db.character_profiles["forever-eu-pvp:Wiw Aust"].realm')=='forever-eu-pvp'
    lua.execute('UnitGUID=function() return "Player-6948-ANOTHER" end')
    assert lua.eval('Cennik:RealmName()')==''
    assert not lua.eval('Cennik:MarketBindingMatches(binding.market)')


def test_confirmation_survives_reload_and_never_overrides_a_reported_different_realm():
    lua=addon()
    lua.execute('''
      GetRealmName=function() return "" end
      GetBuildInfo=function() return "1.60.1" end
      UnitGUID=function() return "Player-6948-ONE" end
      Game.event("ADDON_LOADED","CennikAddon")
      assert(Cennik:BindMarket(binding.market))
      envelope.routes["forever-eu-pvp:FixturePlayer"]={format_version=1,device_id="confirmed-route",packages={},market_binding=binding}
      CennikImport=envelope;Cennik:Initialize()
    ''')
    assert lua.eval('CennikImport.device_id')=='confirmed-route'
    lua.execute('GetRealmName=function() return "CAMWOW EU5 Realm 7 PvE" end')
    assert lua.eval('Cennik:RealmName()')=='CAMWOW EU5 Realm 7 PvE'
    assert not lua.eval('Cennik:MarketBindingMatches(binding.market)')


def test_collector_can_confirm_only_an_offered_same_faction_market():
    lua=addon()
    lua.execute('''
      CennikImport={format_version=1,packages={},collector_only=true,market_catalog={binding},
        market_collector={market=binding.market,workspace_id="collector"}}
      GetRealmName=function() return "" end
      GetBuildInfo=function() return "1.60.1" end
      UnitGUID=function() return "Player-6948-ONE" end
      Cennik:Initialize()
      assert(not Cennik:ChooseCollectorMarket("forever.wrong.alliance.eu"))
      assert(Cennik:ChooseCollectorMarket(binding.market))
    ''')
    assert lua.eval('Cennik:RealmName()')=='forever-eu-pvp'


def test_realm_second_return_on_other_clients_is_not_treated_as_a_surname():
    lua=addon()
    lua.execute('GetBuildInfo=function() return "12.0.0" end;UnitName=function() return "Name","Realm" end')
    assert lua.eval('Cennik:CharacterName()')=='Name'


def test_confirmed_market_alias_keeps_only_same_ruleset_observations():
    assert realm_matches(BINDING,'forever-eu-pvp')
    assert equivalent_realms('forever-eu-pvp','CAMWOW EU5 Realm 6 PvP',BINDING)
    assert not realm_matches(BINDING,'forever-eu-normal')
    assert not realm_matches(BINDING,'forever-us-pvp')
    good=dict(scan_id='old',market=BINDING['market'],realm='CAMWOW EU5 Realm 6 PvP')
    wrong=dict(scan_id='foreign',market=BINDING['market'],realm='CAMWOW EU5 Realm 6 PvE')
    saved=dict(market_scans=[good,wrong],npc_quotes={
        'old':{'scope':'CAMWOW EU5 Realm 6 PvP/alliance'},
        'other':{'scope':'CAMWOW EU5 Realm 6 PvE/alliance'}})
    kept,foreign=scoped_saved(saved,BINDING['market'],'forever-eu-pvp','alliance',BINDING)
    assert kept['market_scans']==[good] and foreign==[wrong]
    assert list(kept['npc_quotes'])==['old']

# EOF
