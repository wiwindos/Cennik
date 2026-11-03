import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from test_server import server as server
from test_workspace import scan
from test_workspace import workspace as workspace

from ahledger.util import atomic_json, exclusive_lock
from cennik.core.models import Item, PriceObservation, Recipe
from cennik.server.bridge import DiscoverCharacters, RegisterDevice
from cennik.server.prepare import prepare
from cennik.sources import Sources
from cennik.worker.agent import scoped_saved
from cennik.worker.idle import IdleProbe
from cennik.workspace.bootstrap import BootstrapDocument
from cennik.workspace.bulk import BulkCreate

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def seed(tmp_path):
    stamp = datetime.now(UTC) - timedelta(days=7)
    document = BootstrapDocument(version="fixture-beta", created_at=datetime.now(UTC), source_market="forever.pvp.alliance.us",
        items={2589: Item(item_id=2589, name="Cloth", vendor_sell_copper=13, observed_at=stamp)},
        prices={2589: PriceObservation(item_id=2589, market="forever.pvp.alliance.us", observed_at=stamp,
            granularity="scan", min_copper=10, median_copper=12, quantity=100, source="AHL1")},
        references={2589: 20}, recipes=[Recipe(spell_id=100, result_item_id=4238, qty_min=1, qty_max=1,
            version_hash="a"*64, profession="Tailoring", inputs=[{"item_id": 2589, "quantity": 3}])])
    folder = tmp_path / "seed"
    folder.mkdir()
    (folder / "baseline.json").write_text(document.model_dump_json(), "utf-8")
    for name in ("npc_catalog.json", "disenchant.json"):
        (folder / name).write_bytes((ROOT / "src/cennik/reference" / name).read_bytes())
    atomic_json(folder / "manifest.json", {"format_version": 1, "version": document.version,
        "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.glob('*.json')}})
    return folder, document


def test_beta_is_usable_but_keeps_dates_and_cannot_resurrect_missing_live_items(workspace, seed):
    _, document = seed
    atomic_json(workspace.bootstrap.path, document.model_dump(mode="json"))
    workspace.calculate()
    assert workspace.analytics.status()['beta_baseline_active']
    assert workspace.exchange.ready()['published_run']
    opportunities = workspace.store.listing(workspace.market_key)['items']
    assert any(o['strategy_id'] == 'vendor_arbitrage' for o in opportunities)
    assert all('beta_baseline' in o['risk_flags'] and 'Бета' in o['source'] for o in opportunities)
    assert all(o['data_age_seconds'] > 86400 for o in opportunities)
    workspace.server_mode = True
    vendor = next(o for o in opportunities if o['strategy_id'] == 'vendor_arbitrage')
    assert workspace.bulk.create(BulkCreate(opportunity_ids=[vendor['id']]))['candidates'] == 1
    own = scan()
    workspace.market.import_scan(own)
    with workspace.provider(None, workspace.market_key, datetime.now(UTC)) as provider:
        assert not provider.current_prices()[2589].beta_baseline
    empty = scan(offset=2, count=0)
    empty['empty_market_confirmed'] = True
    workspace.market.import_scan(empty)
    with workspace.provider(None, workspace.market_key, datetime.now(UTC)) as provider:
        assert provider.current_prices() == {}


def test_seed_reset_preserves_credentials_and_rejects_corruption_before_deleting(tmp_path, seed):
    package, _ = seed
    root = tmp_path / 'server'
    result = prepare(root, package)
    assert result['backup_created'] is False and not result['profile']['realm_names']
    atomic_json(root/'upstreams/credentials.json', {'ahl_token': 'fixture-private-token'})
    sentinel = root/'workers/old.json'
    atomic_json(sentinel, {'test': True})
    with exclusive_lock(root), pytest.raises(RuntimeError):
        prepare(root, package, reset=True, confirmation='RESET_TEST_DATA')
    assert sentinel.exists()
    with pytest.raises(ValueError):
        prepare(root, package, reset=True)
    assert sentinel.exists()
    (package/'baseline.json').write_text('{}', 'utf-8')
    with pytest.raises(ValueError):
        prepare(root, package, reset=True, confirmation='RESET_TEST_DATA')
    assert sentinel.exists()


def test_successful_reset_removes_only_test_state(tmp_path, seed):
    package, _ = seed
    root = tmp_path/'server'
    prepare(root, package)
    atomic_json(root/'upstreams/credentials.json', {'ahl_token': 'fixture-private-token'})
    for folder in ['workers', 'pairing', 'collector-archive', 'forwarding']:
        atomic_json(root/folder/'test.json', {'test': True})
    prepare(root, package, reset=True, confirmation='RESET_TEST_DATA')
    assert json.loads((root/'upstreams/credentials.json').read_text())['ahl_token'] == 'fixture-private-token'
    assert not (root/'workers/test.json').exists() and not (root/'collector-archive/test.json').exists()
    assert len(json.loads((root/'markets.json').read_text())['profiles']) == 1
    assert not (root/'backups').exists()


def test_new_npc_prices_replace_old_but_manual_and_sparse_values_are_preserved(workspace):
    for copper, stamp in [(13, '2026-10-20T00:00:00Z'), (26, '2026-10-21T00:00:00Z')]:
        workspace.market.insert_rows('items', [{'item_id': 2589, 'vendor_sell_copper': copper,
            'origin_dataset': 'item', 'retrieved_at': stamp}])
    assert workspace.market.rows('SELECT vendor_sell_copper v FROM items WHERE item_id=2589')[0]['v'] == 26
    workspace.market.insert_rows('items', [{'item_id': 2589, 'name': 'Cloth', 'origin_dataset': 'item',
        'retrieved_at': '2026-10-22T00:00:00Z'}])
    assert workspace.market.rows('SELECT vendor_sell_copper v FROM items WHERE item_id=2589')[0]['v'] == 26


def test_partial_first_scan_and_own_mode_do_not_break_delivery_or_export(workspace):
    workspace.save_settings(workspace.settings().model_copy(update={'auto_recalculate': True, 'own_data_only': True}))
    assert workspace.recalculate_pending.is_set()
    partial = scan()
    partial.update(full=False, error='closed')
    assert not workspace.import_scans([partial])[0]['complete']
    workspace.import_scans([scan(offset=1)])
    assert workspace.analytics.status()['settings_match']
    assert workspace.exchange.ready()['published_run']


def test_market_backup_contains_new_modules(workspace):
    for name in ['profession_leveling/characters/a.json', 'source_reports/journal.json']:
        atomic_json(workspace.root/name, {'fixture': True})
    backup = Path(workspace.backup()['directory'])
    assert (backup/'profession_leveling/characters/a.json').exists()
    assert (backup/'source_reports/journal.json').exists()


def test_owner_approved_new_worker_reclaims_revoked_auctioneer(server):
    app, _, profile = server
    bridge = app.state.bridge
    old = bridge.register(profile.id, RegisterDevice(label='Old', clients=[
        {'role': 'auctioneer', 'character': 'Old', 'realm': 'FixtureRealm', 'account': 'one'}]))
    bridge.revoke(old['id'])
    new = bridge.register(profile.id, RegisterDevice(label='New'))
    device = next(d for d in bridge.devices() if d['id'] == new['id'])
    result = bridge.discover(device, DiscoverCharacters(characters=[{'role': 'auctioneer',
        'character': 'New', 'realm': 'FixtureRealm', 'account': 'two', 'faction': 'alliance'}]))
    assert not result['notes'] and len(result['clients']) == 1
    assert result['clients'][0]['character'] == 'New'
    assert (app.state.catalog.workspace(profile.id).production.directory/'retired_clients.json').exists()


def test_space_samples_new_delay_only_after_success(tmp_path, monkeypatch):
    from test_worker_idle import GameWindow
    delays = iter([150.0, 200.0, 400.0, 151.0])
    monkeypatch.setattr('cennik.worker.idle.random.uniform', lambda a, b: next(delays))
    clock = [0.0]
    monkeypatch.setattr('cennik.worker.idle.time.monotonic', lambda: clock[0])
    game = GameWindow()
    probe = IdleProbe(tmp_path, game)
    probe.configure(game.target, True, 'fixture')
    clock[0] = 199
    assert not probe.tick()
    clock[0] = 200
    assert not probe.tick() and probe.interval == 200
    game.background = True
    assert probe.tick() and probe.interval == 400
    clock[0] = 599
    assert not probe.tick()
    clock[0] = 600
    assert probe.tick() and probe.interval == 151 and game.sent == 2


def test_forever_rejects_retail_roles_before_binding_realm(server):
    app, _, profile = server
    app.state.catalog.save(profile.model_copy(update={'realm_names': []}))
    device = app.state.bridge.register(profile.id, RegisterDevice(label='New PC'))
    current = next(d for d in app.state.bridge.devices() if d['id'] == device['id'])
    result = app.state.bridge.discover(current, DiscoverCharacters(characters=[{
        'role': 'auctioneer', 'character': 'Retail', 'realm': 'RetailRealm', 'account': 'one',
        'faction': 'alliance', 'market': profile.market, 'game_version': '12.1.5'}]))
    assert not result['clients'] and any('не Forever' in n for n in result['notes'])
    assert app.state.catalog.profile(profile.id).realm_names == []


def test_foreign_saved_scans_and_quotes_stay_out_of_new_market():
    from cennik.workspace.models import MARKET
    foreign = {**scan(), 'market': 'forever.other.alliance.eu', 'realm': 'RetailRealm'}
    fresh = scan()
    saved = {'market_scans': [foreign, fresh], 'npc_quotes': {
        '2589': {'scope': 'FixtureRealm/alliance', 'batch_price_copper': 10},
        '2592': {'scope': 'RetailRealm/alliance', 'batch_price_copper': 20}}}
    clean, isolated = scoped_saved(saved, MARKET, 'FixtureRealm', 'alliance')
    assert clean['market_scans'] == [fresh] and isolated == [foreign]
    assert list(clean['npc_quotes']) == ['2589']
    assert len(saved['market_scans']) == len(saved['npc_quotes']) == 2


@pytest.mark.parametrize('cached', [True, False])
def test_fyi_cached_or_304_beta_cannot_replace_release(tmp_path, workspace, cached):
    beta = workspace.npc_catalog.snapshot()
    release = beta.model_copy(update={'phase': 'release', 'version': 'fixture-release'})
    sources = Sources(tmp_path, httpx.MockTransport(lambda request: httpx.Response(304)))
    atomic_json(sources.root/'fyi-document.json', beta.model_dump(mode='json'))
    atomic_json(sources.root/'fyi.json', {'expires': time.time()+300, 'checked_at': time.time(), 'etag': 'fixture'})
    with pytest.raises(ValueError, match='поверх релизного'):
        sources.fyi_document(release, force=not cached)
    assert workspace.npc_catalog.snapshot().sha256 == beta.sha256


def test_release_recipe_update_rejects_incompatible_or_incomplete_and_keeps_beta(workspace, seed):
    from ahledger.models import JobSpec
    from ahledger.parsers import Parsed
    _, document = seed
    atomic_json(workspace.bootstrap.path, document.model_dump(mode='json'))
    spec = JobSpec(dataset='crafting', url='https://api.ahledger.com/v1/crafting/forever.pvp.alliance.eu',
                   market='forever.pvp.alliance.eu', params={'profession': 'Tailoring'})
    recipe = {'result': {'id': 4238, 'qtyMin': 1, 'qtyMax': 1}, 'reagents': [{'id': 2589, 'qty': 3}]}
    parsed = Parsed(data={'recipes': [recipe], 'game': 'forever', 'build': '1.60.1.70010'}, count=1)
    workspace.validate_recipe_update(spec, parsed)
    for change in [{'build': '12.1.5.70010'}, {'game': 'retail'}, {'recipes': []},
                   {'recipes': [{**recipe, 'reagents': []}]},
                   {'recipes': [{**recipe, 'result': {'id': 4238, 'qtyMin': -1, 'qtyMax': -1}}]}]:
        with pytest.raises(ValueError):
            workspace.validate_recipe_update(spec, Parsed(data={**parsed.data, **change}, count=0 if change.get('recipes') == [] else 1))
    assert workspace.bootstrap.snapshot().version == document.version
