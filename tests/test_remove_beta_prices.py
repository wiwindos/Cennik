import asyncio
import hashlib
from datetime import UTC, datetime

import httpx
from test_forever_release import seed as seed
from test_workspace import scan
from test_workspace import workspace as workspace

from ahledger.util import atomic_json
from cennik.workspace.web import create_app


def install(workspace, seed):
    atomic_json(workspace.bootstrap.path, seed[1].model_dump(mode='json'))


def test_price_switch_preserves_reference_catalog_and_hides_card_price(workspace, seed):
    install(workspace, seed)
    assert not workspace.settings().use_beta_prices
    with workspace.provider(None, workspace.market_key, datetime.now(UTC)) as provider:
        assert provider.items()[2589].vendor_sell_copper == 13
        assert provider.recipes()[0].spell_id == 100
        assert not provider.current_prices()
        assert provider.source_reference(2589) is None

    async def card():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(workspace.root, workspace=workspace)), base_url='http://127.0.0.1') as client:
            response = await client.get('/api/v2/items/2589')
            assert response.status_code == 200
            assert 'Бета ·' not in response.text
    asyncio.run(card())


def test_cleanup_retires_beta_publication_preserves_catalog_and_is_idempotent(workspace, seed):
    install(workspace, seed)
    workspace.save_settings(workspace.settings().model_copy(update={'use_beta_prices': True}))
    workspace.calculate()
    old = workspace.store.latest(workspace.market_key, published=True)
    assert old and old.signal_count
    before = workspace.bootstrap.snapshot().model_dump(mode='json')
    generation = workspace.generation()
    report = workspace.remove_beta_prices()
    assert (report['removed_prices'], report['removed_references']) == (1, 1)
    assert report['calculation']['status'] == 'waiting_for_live_prices'
    assert not workspace.store.latest(workspace.market_key, published=True)
    assert workspace.store.run_summary(old.run_id).signal_count == old.signal_count
    after = workspace.bootstrap.snapshot().model_dump(mode='json')
    for section in ('items', 'recipes', 'item_facts'):
        assert after[section] == before[section]
    assert not after['prices'] and not after['references']
    assert report['catalog']['observed_at'] is None
    assert workspace.generation() != generation
    assert not workspace.settings().use_beta_prices
    second = workspace.remove_beta_prices()
    assert second['removed_prices'] == second['removed_references'] == 0
    # Even a later explicit opt-in cannot restore deleted beta auction evidence.
    workspace.save_settings(workspace.settings().model_copy(update={'use_beta_prices': True}))
    with workspace.provider(None, workspace.market_key, datetime.now(UTC)) as provider:
        assert not provider.current_prices()


def test_queued_cleanup_preserves_live_scans_and_republishes_actual_prices(workspace, seed):
    install(workspace, seed)
    workspace.market.import_scan(scan())
    archives = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in workspace.root.rglob('*.parquet')}
    archives.update({p: hashlib.sha256(p.read_bytes()).hexdigest() for p in workspace.root.rglob('*.json.zst')})
    assert archives
    sources = workspace.market.rows('SELECT * FROM price_snapshots')

    async def remove():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(workspace.root, workspace=workspace)), base_url='http://127.0.0.1') as client:
            response = await client.post('/api/v2/bootstrap/prices/remove')
            assert response.status_code == 202
            return response.json()['job_id']
    job_id = asyncio.run(remove())
    workspace.future.result(timeout=30)
    job = next(j for j in workspace.jobs() if j['id'] == job_id)
    assert job['state'] == 'completed', job.get('error')
    assert workspace.store.latest(workspace.market_key, published=True).status == 'completed'
    assert not workspace.analytics.status()['beta_baseline_active']
    assert workspace.market.rows('SELECT * FROM price_snapshots') == sources
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in archives.items())
    with workspace.provider(None, workspace.market_key, datetime.now(UTC)) as provider:
        assert provider.current_prices()[2589].source == 'Cennik replicate'
        assert provider.recipes()[0].spell_id == 100
