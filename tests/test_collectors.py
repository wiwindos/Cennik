import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from test_accounting import accounting_lua, lua_value

# Fixtures required by the existing Lua collector fixture.
from test_addon import exchange as exchange  # noqa: I001
from test_cennik import analytics_root as analytics_root
from test_market_collector import collector
from test_workspace import scan
from test_workspace import workspace as workspace

from cennik.addon.lua_data import lua_literal
from cennik.collector.client import CollectorClient
from cennik.collector.protocol import Connection, Observation
from cennik.collector.server import create_hub

ADMIN = "a"*40


def sample():
    stamp = int(time.time())
    return {"device_id": "private-device", "character": "PRIVATE_CHARACTER", "workspace_id": "PRIVATE_WORKSPACE",
        "collector_status": {"collector_id": "private-device", "scans": 1, "rows": 2,
            "current": {"code": "READY", "level": "ready", "at": stamp}, "errors": {}},
        "market_scans": [scan()], "npc_quotes": {"2589": {"batch_price_copper": 20, "batch_quantity": 2,
            "observed_at": stamp, "scope": "PRIVATE_CHARACTER/realm/alliance"}},
        "collector_recipes": {"observed_at": stamp, "coverage": "visible_recipes_only", "recipes": {
            "100": {"spell_id": 100, "item_id": 4238, "inputs": [{"item_id": 2589, "quantity": 3}]}}},
        "collector_transforms": [{"observation_id": "private:obs:1", "kind": "disenchant", "observed_at": stamp,
            "inputs": [{"item_id": 4238, "quantity": 1}], "outputs": [{"item_id": 10940, "quantity": 2}]}],
        "accounting": {"events": [{"kind": "purchase", "money_copper": 123456, "character": "PRIVATE_CHARACTER"}]},
        "production": {"orders": [{"private": "PRIVATE_ORDER"}]}}


def transport(client):
    def handle(request):
        response = client.request(request.method, request.url.path, content=request.content, headers=dict(request.headers))
        return httpx.Response(response.status_code, content=response.content, headers={"Content-Type": "application/json"})
    return httpx.MockTransport(handle)


def test_delivery_privacy_idempotence_permissions_and_revoke(tmp_path):
    app = create_hub(tmp_path / "hub", ADMIN)
    with TestClient(app) as http:
        assert http.get('/v1/admin/members').status_code == 401
        invitation = http.post('/v1/admin/members', json={"label": "Alice"}, headers={"X-Admin-Key": ADMIN}).json()
        uploader = CollectorClient(tmp_path / "local", transport(http))
        uploader.configure(Connection(enabled=True, url="http://127.0.0.1:8768", token=invitation["token"], admin_token=ADMIN))
        source = sample()
        uploader.capture(source)
        assert uploader.overview()["pending"] == 5
        queued = '\n'.join(p.read_text('utf-8') for p in (uploader.root / 'outbox').glob('*.json'))
        assert all(secret not in queued for secret in ("PRIVATE_CHARACTER", "PRIVATE_ORDER", "PRIVATE_WORKSPACE", "private-device", "123456", invitation["token"], ADMIN))
        assert uploader.flush()["sent"] == 5
        uploader.capture(source)
        assert uploader.overview()["pending"] == 0
        overview = uploader.admin()
        member = overview["members"][0]
        assert member["application_online"] and member["counts"]["full_scans"] == 1
        assert member["counts"]["rows"] == 2 and member["counts"]["disenchant"] == 1
        assert member["counts"]["recipe"] == member["counts"]["npc"] == 1
        assert 'token_hash' not in json.dumps(overview)
        assert not list((tmp_path / 'hub').rglob('*.sqlite'))
        assert invitation["token"] not in json.dumps(uploader.overview())
        assert 'private:obs:1' in uploader.delivery()["acknowledged_observation_ids"]
        assert http.get('/v1/admin/members', headers={"X-Collector-Key": invitation["token"]}).status_code == 401
        assert uploader.revoke(invitation["id"])["revoked"]
        assert http.post('/v1/heartbeat', headers={"X-Collector-Key": invitation["token"]}).status_code == 401


def test_lost_ack_retries_without_double_count_and_offline_survives_restart(tmp_path):
    app = create_hub(tmp_path / "hub", ADMIN)
    invite = app.state.hub.invite("Bob")
    with TestClient(app) as http:
        healthy = transport(http)
        def lost_ack(request):
            response = healthy.handle_request(request)
            if request.url.path == '/v1/observations':
                raise httpx.ReadError('lost response', request=request)
            return response
        uploader = CollectorClient(tmp_path / 'local', httpx.MockTransport(lost_ack))
        uploader.configure(Connection(enabled=True, url="http://localhost:8768", token=invite['token']))
        uploader.queue('diagnostic', {"at": int(time.time()), "code": "SCAN_TIMEOUT"})
        assert uploader.flush()['pending'] == 1
        assert not uploader.due()
        assert app.state.hub.overview()['members'][0]['counts']['records'] == 1
        restarted = CollectorClient(tmp_path / 'local', healthy)
        assert restarted.flush()['sent'] == 1
        assert app.state.hub.overview()['members'][0]['counts']['records'] == 1
        assert restarted.overview()['pending'] == 0


def test_server_rejects_partial_claim_private_fields_and_fake_ack(tmp_path):
    app = create_hub(tmp_path / 'hub', ADMIN)
    invite = app.state.hub.invite('Tester')
    with TestClient(app) as http:
        headers = {'X-Collector-Key': invite['token']}
        assert http.post('/v1/observations', json={'kind': 'diagnostic', 'data': {'at': int(time.time()), 'character': 'PRIVATE'}}, headers=headers).status_code == 422
        assert http.post('/v1/observations', json={'kind': 'scan', 'data': {'observed_at': int(time.time()), 'market': 'forever.pvp.alliance.us', 'count': 0, 'data': '', 'full': True}}, headers=headers).status_code == 422
        uploader = CollectorClient(tmp_path / 'local', httpx.MockTransport(lambda r: httpx.Response(200, json={'record_id': 'wrong', 'stored': True})))
        uploader.configure(Connection(enabled=True, url='http://localhost:8768', token=invite['token']))
        uploader.queue('diagnostic', {'at': int(time.time()), 'code': 'READY'})
        assert uploader.flush()['pending'] == 1
        assert not list((uploader.root / 'receipts').glob('*.json'))


def test_connection_requires_https_and_does_not_echo_secrets(tmp_path):
    c = CollectorClient(tmp_path)
    with pytest.raises(ValueError):
        c.configure(Connection(enabled=True, url='http://example.org', token='secret'))
    c.configure(Connection(enabled=True, url='https://example.org', token='secret'))
    c.configure(Connection(enabled=False, url='https://example.org'))
    assert c.connection().token == 'secret'
    assert 'secret' not in json.dumps(c.overview())
    c.capture(sample())
    assert c.overview()['pending'] == 0


def test_addon_open_delay_shared_request_status_and_copy_report(exchange):
    lua = collector(exchange)
    lua.execute("Game.event('AUCTION_HOUSE_SHOW');Cennik.MarketCollector:Tick(1)")
    assert lua.eval('Game.replicates') == 0
    assert lua.eval('Cennik.CollectorStatus.db.current.code') == 'OPEN_DELAY'
    lua.execute("C_AuctionHouse.ReplicateItems();Game.event('REPLICATE_ITEM_LIST_UPDATE');Game.advance(3);Cennik.MarketCollector:Tick(1)")
    assert lua.eval('Game.replicates') == 1
    assert lua.eval('Cennik.CollectorStatus.db.current.code') == 'READY'
    lua.execute("SlashCmdList.CENNIK('diagnostics')")
    report = lua.eval('Cennik.CollectorUI.edit:GetText()')
    assert 'Cennik Collector support v1' in report and 'READY' in report
    assert 'FixturePlayer' not in report
    assert lua.eval('Cennik.CollectorUI.window:IsShown()')


def test_addon_errors_are_grouped_visible_and_retained_in_support_report(exchange):
    lua = collector(exchange)
    lua.execute("Cennik.CollectorStatus:Set('SCAN_TIMEOUT');for i=1,10 do Cennik.CollectorStatus:Set('SCAN_TIMEOUT') end")
    assert lua.eval('Cennik.CollectorStatus.db.errors.SCAN_TIMEOUT.count') == 1
    lua.execute("Cennik.CollectorStatus:Set('COOLDOWN');Cennik.CollectorStatus:Set('SCAN_TIMEOUT')")
    assert lua.eval('Cennik.CollectorStatus.db.errors.SCAN_TIMEOUT.count') == 2
    lua.execute("Cennik.CollectorUI:Show(true)")
    assert 'SCAN_TIMEOUT' in lua.eval('Cennik.CollectorUI.edit:GetText()')
    assert 'SCAN_TIMEOUT' in lua.eval('Cennik.CollectorStatus.db.last_error.code')


def test_npc_recipes_and_actual_transform_are_sanitized_then_acked(workspace, tmp_path):
    lua = accounting_lua(workspace)
    lua.execute("""
      Cennik.db.collector.enabled=true;Cennik.db.collector.market='forever.pvp.alliance.us'
      UnitGUID=function() return 'Creature-0-0-0-0-12345-111' end
      local originalName=UnitName;UnitName=function(unit) if unit=='npc' then return 'FixtureVendor' end;return originalName(unit) end
      GetNumTradeSkills=function() return 1 end
      Game.event('MERCHANT_SHOW');Game.event('TRADE_SKILL_SHOW');Game.advance(1)
      Cennik.Accounting:Emit('disenchant',{inputs={Cennik.Accounting:Line(4238,1)},outputs={Cennik.Accounting:Line(10940,2)}})
    """)
    saved = lua_value(lua.globals().CennikAddonDB)
    assert saved['collector_npc']['12345:2589']['npc_id'] == 12345
    assert saved['collector_recipes']['recipes']['100']['inputs'][0]['quantity'] == 3
    sender = CollectorClient(tmp_path / 'outgoing')
    sender.configure(Connection(enabled=True, url='http://localhost:8768', token='fixture'))
    sender.capture(saved)
    assert sender.overview()['pending'] >= 4
    raw = '\n'.join(p.read_text('utf-8') for p in (sender.root / 'outbox').glob('*.json'))
    assert 'FixturePlayer' not in raw and 'money_copper' not in raw
    lua.execute('CennikImport.collector_delivery='+lua_literal(sender.delivery())+';Cennik.CollectorStatus:Initialize()')
    assert lua.eval('#Cennik.db.collector_transforms') == 0
    assert lua.eval('next(Cennik.db.collector_npc)') is None


def test_local_routes_do_not_expose_keys_and_capture_works_without_server(workspace):
    from cennik.workspace.web import create_app
    app = create_app(workspace.root.parent / 'local-http')
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 1234)) as http:
        assert http.get('/api/v2/collectors').status_code == 200
        assert http.put('/api/v2/collectors/connection', json={'enabled': True, 'url': 'https://example.org', 'token': 'PRIVATE_TOKEN'}).status_code == 200
        assert http.get('/api/v2/collectors').json()['connection']['enabled']
        assert 'PRIVATE_TOKEN' not in http.get('/api/v2/collectors').text
        assert 'PRIVATE_TOKEN' not in http.get('/api/v2/settings').text


def test_two_members_same_market_snapshot_does_not_duplicate_raw_archive(tmp_path):
    app = create_hub(tmp_path / 'hub', ADMIN)
    observation = Observation(kind='diagnostic', data={'at': int(time.time()), 'code': 'READY'})
    ids = [app.state.hub.invite(n)['id'] for n in ('Alice', 'Bob')]
    for identity in ids:
        app.state.hub.accept(identity, observation)
        assert app.state.hub.accept(identity, observation)['duplicate']
    data = app.state.hub.overview()
    assert data['raw_records'] == 1
    assert [m['counts']['records'] for m in data['members']] == [1, 1]


def test_delivery_ack_refresh_preserves_routed_tasks_and_watcher_stamp(workspace, tmp_path):
    from cennik.addon.lua_data import Parser
    game = tmp_path / 'game'
    addon = game / 'Interface/AddOns/CennikAddon'
    addon.mkdir(parents=True)
    workspace.save_settings(workspace.settings().model_copy(update={'game_root': str(game), 'account': 'fixture'}))
    route = {'format_version': 1, 'market_collector': {'workspace_id': workspace.identity}, 'packages': [{'fixture': 'keep'}]}
    path = addon / 'ImportedData.lua'
    path.write_text('CennikImport='+lua_literal({'format_version': 2, 'routes': {'Realm:Character': route}}), encoding='utf-8')
    app = create_hub(tmp_path / 'hub', ADMIN)
    invitation = app.state.hub.invite('Uploader')
    with TestClient(app) as http:
        workspace.collectors.transport = transport(http)
        workspace.collectors.configure(Connection(enabled=True, url='http://localhost:8768', token=invitation['token']))
        workspace.collectors.queue('diagnostic', {'at': int(time.time()), 'code': 'READY'})
        before = workspace.last_file
        assert workspace.deliver_collectors()['sent'] == 1
        assert workspace.last_file == before
        parser = Parser(path.read_text('utf-8'))
        assert parser.identifier() == 'CennikImport'
        parser.require('=')
        payload = parser.value()['routes']['Realm:Character']
        assert payload['packages'] == [{'fixture': 'keep'}]
        assert payload['collector_delivery']['at']


def test_malformed_materials_are_rejected_and_upload_rate_is_limited(tmp_path):
    app = create_hub(tmp_path / 'hub', ADMIN)
    invite = app.state.hub.invite('Limited')
    with TestClient(app) as http:
        headers = {'X-Collector-Key': invite['token']}
        malformed = {'kind': 'disenchant', 'data': {'observed_at': int(time.time()), 'inputs': [42]}}
        assert http.post('/v1/observations', json=malformed, headers=headers).status_code == 422
        app.state.hub.rates[invite['id']] = time.monotonic(), 300
        assert http.post('/v1/heartbeat', headers=headers).status_code == 429
