import time

from auction_fixture import proposal
from test_accounting import accounting_lua
from test_collectors import transport
from test_server import AUTH, ORIGIN, exchange, register
from test_server import server as server

from ahledger.util import atomic_json
from cennik.addon.lua_data import Parser, lua_literal
from cennik.collector.auctioneers import Review
from cennik.collector.client import CollectorClient
from cennik.collector.protocol import Connection, Observation
from cennik.worker.agent import WorkerAgent


def read_import(path):
    parser = Parser(path.read_text('utf-8'))
    assert parser.identifier() == 'CennikImport'
    parser.require('=')
    return parser.value()


def test_one_owner_approval_updates_both_accounts_without_another_exchange(server, tmp_path):
    app, http, profile = server
    device = register(http, profile, two=True)
    game = tmp_path / 'game'
    (game / 'Interface/AddOns/Auctionator').mkdir(parents=True)
    agent = WorkerAgent(tmp_path / 'worker', transport(http))
    agent.configure(str(game), ORIGIN, device['token'])
    for route in agent.manifest['clients']:
        payload = exchange(http, device['token'], route['id'], 1)['payload']
        assert not payload['auctioneer_registry']['entries']
        atomic_json(agent.root / 'imports' / (route['id']+'.json'), payload)
    first = proposal().model_copy(update={'server_id': profile.id, 'market': profile.market})
    for index in range(3):
        record = Observation(kind='auctioneer_proposal', data=first.model_copy(update={'observed_at': int(time.time())-10+index}).model_dump(mode='json'))
        app.state.hub.auctioneers.suggest(record.identity(), record.data, 'account-'+str(index))
    endpoint = '/m/'+profile.id+'/api/v2/collectors/auctioneers'
    rows = http.get(endpoint, headers=AUTH).json()['items']
    assert len(rows) == 1 and len(rows[0]['contributors']) == 3
    response = http.post(endpoint+'/'+rows[0]['record_id']+'/review', headers=AUTH,
                         json={'action': 'approve', 'auction_type': 'ordinary', 'expected_revision': 0})
    assert response.status_code == 202, response.text
    workspace = app.state.catalog.workspace(profile.id)
    workspace.future.result(timeout=10)
    entries = app.state.hub.auctioneers.wire()['entries']
    assert len(entries) == 1 and entries[0]['revision'] == 1
    for index in range(2):
        invitation = app.state.hub.invite('Collector '+str(index), profile_id=profile.id)
        collector = CollectorClient(tmp_path / ('collector-'+str(index)), transport(http))
        collector.configure(Connection(enabled=True, url=ORIGIN+'/collect', token=invitation['token']))
        collector.flush()
        assert collector.registry()['entries'][0]['scope_id'] == entries[0]['scope_id']
    # Only the common manifest is refreshed: stale per-character exchange files stay untouched.
    agent.manifest = agent.fetch_manifest()
    agent.write_import()
    incoming = read_import(game / 'Interface/AddOns/CennikAddon/ImportedData.lua')
    assert len(incoming['routes']) == 2
    for payload in [*incoming['routes'].values(), incoming['discovery']]:
        assert payload['auctioneer_registry']['entries'][0]['scope_id'] == entries[0]['scope_id']
        lua = accounting_lua(workspace)
        lua.execute('CennikImport='+lua_literal(payload)+';Cennik:Initialize()')
        assert lua.eval("Cennik.AuctionContext:Check('collect')")[0]
        assert lua.eval("Cennik.AuctionContext:Propose('ordinary')")
        assert lua.eval('next(Cennik.db.auctioneer_proposals)') is None
    repeated = first.model_copy(update={'observed_at': int(time.time())})
    record = Observation(kind='auctioneer_proposal', data=repeated.model_dump(mode='json'))
    app.state.hub.auctioneers.suggest(record.identity(), record.data, 'another-pc')
    rows = http.get(endpoint, headers=AUTH).json()['items']
    assert len(rows) == 1 and rows[0]['registry']['approved']
    assert len(rows[0]['contributors']) == 4
    app.state.hub.auctioneers.review(record.identity(), Review(action='revoke', expected_revision=1))
    agent.manifest = agent.fetch_manifest()
    agent.write_import()
    incoming = read_import(game / 'Interface/AddOns/CennikAddon/ImportedData.lua')
    assert all(not p['auctioneer_registry']['entries'] for p in incoming['routes'].values())
    # Already distributed Workers still receive their original manifest contract.
    legacy = http.get('/bridge/v1/manifest', headers={'X-Bridge-Key': device['token'], 'X-Cennik-Worker': '0.2'}).json()
    assert 'auctioneer_registry' not in legacy


def test_realm_alias_proposals_share_one_row_and_existing_decision(server):
    app, http, profile = server
    old, new = 'CAMWOW EU5 Realm 6 PvP', 'CAMWOW EU9 Realm 99 PvP'
    app.state.catalog.save(profile.model_copy(update={'realm_names': [old]}))
    first = proposal().model_copy(update={'realm': old, 'server_id': profile.id, 'market': profile.market})
    record = Observation(kind='auctioneer_proposal', data=first.model_dump(mode='json'))
    app.state.hub.auctioneers.suggest(record.identity(), record.data, 'first')
    app.state.hub.auctioneers.review(record.identity(), Review(action='approve', auction_type='ordinary'))
    second = first.model_copy(update={'realm': new, 'observed_at': first.observed_at+1})
    record = Observation(kind='auctioneer_proposal', data=second.model_dump(mode='json'))
    app.state.hub.auctioneers.suggest(record.identity(), record.data, 'second')
    rows = http.get('/m/'+profile.id+'/api/v2/collectors/auctioneers', headers=AUTH).json()['items']
    assert len(rows) == 1 and rows[0]['registry']['approved']
    assert rows[0]['registry']['scope_id'] == first.scope_id()
    assert rows[0]['contributors'] == ['first', 'second']
