import time

import httpx
import pytest
from test_accounting import accounting_lua, lua_value
from test_collector_binding import MARKET, binding, load
from test_collectors import ADMIN, transport
from test_workspace import scan
from test_workspace import workspace as workspace

from ahledger.util import atomic_json
from cennik.collector.auctioneers import (
    AuctioneerProposal,
    AuctioneerRegistry,
    Review,
    scan_scope,
    validate_snapshot,
)
from cennik.collector.client import CollectorClient
from cennik.collector.desktop import key_status
from cennik.collector.protocol import Connection, Observation
from cennik.collector.server import create_hub
from cennik.markets import equivalent_realms, forever_realm_identity, neutral_market, realm_matches
from cennik.workspace.service import Workspace

OLD = "CAMWOW EU5 Realm 6 PvP"
NEW = "CAMWOW EU7 Realm 99 PvP"


def profile():
    return binding(realm_names=[OLD])


@pytest.mark.parametrize(("realm", "matches"), [
    (OLD, True), (NEW, True), ("CAMWOW EU4 Realm 4 PvP", True),
    ("camwow eu4 realm 4 pvp", True), ("CAMWOW US7 Realm 99 PvP", False),
    ("CAMWOW EU7 Realm 99 PvE", False), ("CAMWOW EU7 Realm 99 RP-PvP", False),
    ("CAMWOW EU7 Realm 99 RP", False), ("Other Realm", False),
    ("CAMWOW EU7000 Realm 99 PvP", False),
])
def test_client_and_server_resolve_technical_realms_consistently(workspace, realm, matches):
    assert realm_matches(profile(), realm) is matches
    lua = accounting_lua(workspace, collector_only=True)
    lua.execute("GetRealmName=function() return " + repr(realm) + " end")
    load(lua, [profile()])
    assert lua.eval(f"Cennik:MarketBindingMatches('{MARKET}')") is matches
    assert (lua.eval("Cennik.db.binding") is not None) is matches


def approved(tmp_path, kind="ordinary"):
    registry = AuctioneerRegistry(tmp_path / "registry")
    p = AuctioneerProposal(server_id="a"*32, market=MARKET, npc_id=12345, npc_name="Auctioneer",
        realm=OLD, map_id=84, x=50, y=60, proposed_type=kind, observed_at=int(time.time()))
    observation = Observation(kind="auctioneer_proposal", data=p.model_dump(mode="json"))
    registry.suggest(observation.identity(), observation.data, "fixture")
    registry.review(observation.identity(), Review(action="approve", auction_type=kind))
    return registry, p


@pytest.mark.parametrize("kind", ["ordinary", "neutral"])
def test_realm_change_keeps_npc_approval_and_scan_reaches_correct_market(workspace, tmp_path, kind):
    registry, p = approved(tmp_path, kind)
    snapshot = validate_snapshot({**registry.wire(), "market_catalog": [profile()]})
    lua = accounting_lua(workspace, collector_only=True)
    lua.execute("GetRealmName=function() return " + repr(NEW) + " end")
    from cennik.addon.lua_data import lua_literal
    lua.execute("CennikImport.auctioneer_registry=" + lua_literal(snapshot))
    load(lua, [profile()])
    context = lua_value(lua.eval("Cennik.AuctionContext:Snapshot()"))
    assert context["scope_id"] == p.scope_id() and context["realm"] == NEW
    value = scan()
    value.update(market=MARKET if kind == "ordinary" else neutral_market(MARKET), realm=NEW, auction_context=context)
    assert scan_scope(value, snapshot) == kind
    target = Workspace(tmp_path / "isolated", market_key=MARKET, market_info=profile())
    try:
        atomic_json(target.root / "collectors/auctioneer_registry.json", snapshot)
        result = target.market.import_scan(value)
        if kind == "ordinary":
            assert result["complete"]
            assert target.market.rows("SELECT count(*) n FROM working_offers")[0]["n"] == 2
        else:
            assert result["archived_only"]
            assert not lua.eval("Cennik.AuctionContext:Check('buy')")[0]
            assert not target.market.rows("SELECT * FROM working_offers")
    finally:
        target.close()
    bad = {**value, "realm": "CAMWOW US7 Realm 99 PvP", "auction_context": {**context, "realm": "CAMWOW US7 Realm 99 PvP"}}
    assert scan_scope(bad, snapshot) == "unknown"


def test_revoke_from_a_different_backend_realm_cannot_revive_old_approval(tmp_path):
    registry, old = approved(tmp_path)
    new = old.model_copy(update={"realm": NEW, "observed_at": int(time.time())+1})
    observation = Observation(kind="auctioneer_proposal", data=new.model_dump(mode="json"))
    registry.suggest(observation.identity(), observation.data, "fixture")
    row = next(r for r in registry.overview()["items"] if r['record_id'] == observation.identity())
    assert row['registry']['scope_id'] == old.scope_id()
    result = registry.review(observation.identity(), Review(action="revoke", expected_revision=1))
    assert result['scope_id'] == old.scope_id() and not registry.wire()['entries']
    registry.review(observation.identity(), Review(action="approve", auction_type="neutral", expected_revision=2))
    entries = validate_snapshot(registry.wire())['entries']
    assert len(entries) == 1 and entries[0]['auction_type'] == 'neutral'


def test_repeat_approval_of_the_same_npc_does_not_invalidate_scan_revision(tmp_path):
    registry, old = approved(tmp_path)
    new = old.model_copy(update={'realm': NEW, 'x': old.x+.1})
    observation = Observation(kind='auctioneer_proposal', data=new.model_dump(mode='json'))
    registry.suggest(observation.identity(), observation.data, 'fixture')
    result = registry.review(observation.identity(), Review(action='approve', auction_type='ordinary', expected_revision=1))
    assert result['revision'] == 1
    assert registry.wire()['entries'][0]['data']['realm'] == OLD


def test_legacy_duplicate_approvals_remain_readable_but_share_revocation(tmp_path):
    registry, old = approved(tmp_path)
    legacy = registry.entry(old.scope_id())
    new = old.model_copy(update={'realm': NEW})
    duplicate = {**legacy, 'scope_id': new.scope_id(), 'data': new.model_dump(mode='json'),
                 'reviewed_at_ns': legacy['reviewed_at_ns']+1}
    atomic_json(registry.root / 'registry' / (new.scope_id()+'.json'), duplicate)
    assert len(validate_snapshot(registry.wire())['entries']) == 2
    observation = Observation(kind='auctioneer_proposal', data=old.model_dump(mode='json'))
    registry.review(observation.identity(), Review(action='revoke', expected_revision=1))
    assert not registry.wire()['entries']


def test_market_choice_survives_backend_number_change(workspace):
    lua = accounting_lua(workspace, collector_only=True)
    lua.execute("GetRealmName=function() return " + repr(OLD) + " end")
    other = MARKET.replace('a'*32, 'b'*32)
    catalog = [profile(), {**profile(), 'market': other, 'id': 'b'*32}]
    load(lua, catalog)
    assert lua.eval(f"Cennik:ChooseCollectorMarket('{other}')")[0]
    lua.execute("GetRealmName=function() return " + repr(NEW) + " end; Cennik:Initialize()")
    assert lua.eval("Cennik.db.current_market") == other
    assert equivalent_realms(OLD, NEW, profile())
    assert forever_realm_identity(NEW) == ('eu','pvp')


def test_key_is_saved_but_not_assumed_verified_and_empty_field_keeps_it(tmp_path):
    c = CollectorClient(tmp_path)
    c.configure(Connection(enabled=True, url="https://collector.example/collect", token="secret"))
    saved = c.public_connection()
    assert saved['token_set'] and not saved['token_verified']
    text = key_status(saved, address=saved['url'])
    assert "Ключ сохранён" in text and "Ожидаем" in text and "secret" not in text
    c.configure(Connection(enabled=True, url=saved['url']))
    assert c.connection().token == "secret"
    assert "ещё не сохранён" in key_status(saved, entered="new-secret", address=saved['url'])
    assert "прежнему серверу" in key_status(saved, address='https://other.example/collect')


def test_heartbeat_verifies_key_without_pretending_to_upload_a_scan(tmp_path):
    from fastapi.testclient import TestClient
    app = create_hub(tmp_path / "hub", ADMIN)
    invitation = app.state.hub.invite("fixture")
    with TestClient(app) as http:
        c = CollectorClient(tmp_path / "client", transport(http))
        c.configure(Connection(enabled=True, url="http://localhost", token=invitation['token']))
        assert c.flush()['sent'] == 0
        assert c.public_connection()['token_verified']
        assert not c.delivery().get('at')
        assert "Сервер подтвердил" in key_status(c.public_connection(), address='http://localhost')
        c.configure(Connection(enabled=True, url="http://localhost", token="wrong"))
        assert not c.public_connection()['token_verified']
        c.flush()
        assert c.public_connection()['token_rejected']
        assert "отклонил" in key_status(c.public_connection(), address='http://localhost')
        c.configure(Connection(enabled=True, url="http://localhost", token=invitation['token']))
        c.flush()
        assert c.public_connection()['token_verified'] and not c.public_connection()['token_rejected']


def test_network_failure_does_not_mean_the_saved_key_was_lost(tmp_path):
    def offline(request):
        raise httpx.ConnectError('offline', request=request)
    c = CollectorClient(tmp_path, httpx.MockTransport(offline))
    c.configure(Connection(enabled=True, url="http://localhost", token="secret"))
    c.flush()
    assert c.public_connection()['token_set'] and not c.public_connection()['token_rejected']
