import time

from test_accounting import accounting_lua
from test_server import AUTH
from test_server import server as server

from ahledger.util import atomic_json
from cennik.server.bridge import RegisterDevice, RemoteCharacter


def setup(server):
    app, http, profile = server
    profile = profile.model_copy(update={'realm_names': ['CAMWOW EU5 Realm 6 PvP']})
    app.state.catalog.save(profile)
    device = app.state.bridge.register(profile.id, RegisterDevice(label='roles', clients=[
        RemoteCharacter(role='auctioneer', character='Wiw', realm=profile.realm_names[0], account='first')]))
    return app, http, profile, device


def row(profile, name, realm, at, role='auctioneer'):
    return {'character':name, 'realm':realm, 'account':'first' if name=='Wiw' else 'second',
            'role':role, 'role_chosen_at':at, 'market':profile.market, 'faction':'alliance', 'game_version':'1.60.1'}


def test_changing_auctioneer_across_technical_realms_keeps_ids_and_ignores_stale_roles(server):
    app,http,profile,device=setup(server)
    old_id=device['clients'][0]
    clock=int(time.time()*1000)-5000
    def discover(*rows):
        response=http.post('/bridge/v1/characters',headers={'X-Bridge-Key':device['token']},json={'characters':list(rows)})
        assert response.status_code==200,response.text
        return response.json()
    holy=row(profile,'Holy','CAMWOW EU5 Realm 4 PvP',clock)
    manifest=discover(holy)
    assert {(c['character'],c['role']) for c in manifest['clients']}=={('Holy','auctioneer'),('Wiw','crafter')}
    ids={c['character']:c['id'] for c in manifest['clients']}
    assert ids['Wiw']==old_id
    stale=row(profile,'Wiw','CAMWOW EU5 Realm 6 PvP',0)
    manifest=discover(stale)
    assert next(c for c in manifest['clients'] if c['character']=='Holy')['role']=='auctioneer'
    manifest=discover(holy,stale)
    assert not manifest['notes']
    newer={**stale,'role_chosen_at':clock+1}
    manifest=discover(newer,holy)
    assert {(c['character'],c['role']) for c in manifest['clients']}=={('Holy','crafter'),('Wiw','auctioneer')}
    assert {c['character']:c['id'] for c in manifest['clients']}==ids
    foreign=row(profile,'Other','CAMWOW US5 Realm 4 PvP',clock+2)
    manifest=discover(foreign)
    assert len(manifest['clients'])==2
    assert next(c for c in manifest['clients'] if c['role']=='auctioneer')['character']=='Wiw'


def test_active_production_order_prevents_automatic_role_switch(server):
    app,http,profile,device=setup(server)
    w=app.state.catalog.workspace(profile.id)
    identity=device['clients'][0]
    atomic_json(w.production.directory/'orders'/('a'*32+'.json'),
                {'created_at':'2026-11-06', 'auctioneer_id':identity,'crafter_id':identity,'closed':False})
    response=http.post('/bridge/v1/characters',headers={'X-Bridge-Key':device['token']},json={
        'characters':[row(profile,'Holy','CAMWOW EU5 Realm 4 PvP',int(time.time()*1000))]})
    assert response.status_code==200
    assert [c['character'] for c in response.json()['clients']]==['Wiw']
    assert 'активных заказов' in ' '.join(response.json()['notes'])


def test_addon_new_role_overrides_old_import_and_accepts_newer_server_role(server):
    app,http,profile=server
    lua=accounting_lua(app.state.catalog.workspace(profile.id))
    lua.execute("CennikImport.worker_discovery=true; Cennik.CharacterRole:Choose('auctioneer')")
    chosen=lua.eval("Cennik.db.character_profiles[GetRealmName()..':'..UnitName('player')].role_chosen_at")
    assert chosen>0
    lua.execute("CennikImport.production={client={role='crafter',role_chosen_at="+str(chosen-1)+"}}; Cennik.CharacterRole:Capture()")
    assert lua.eval("Cennik.db.character_profiles[GetRealmName()..':'..UnitName('player')].role")=='auctioneer'
    lua.execute("CennikImport.production.client.role_chosen_at="+str(chosen+1)+";Cennik.CharacterRole:Capture()")
    assert lua.eval("Cennik.db.character_profiles[GetRealmName()..':'..UnitName('player')].role")=='crafter'


def test_owner_can_assign_reported_legacy_holy_without_another_addon_reload(server):
    app,http,profile,device=setup(server)
    holy=row(profile,'Holy','CAMWOW EU5 Realm 4 PvP',0)
    headers={'X-Bridge-Key':device['token']}
    response=http.post('/bridge/v1/characters',headers=headers,json={'characters':[holy]})
    assert response.json()['clients'][0]['character']=='Wiw'
    endpoint='/server/api/workers/'+device['id']+'/auctioneer'
    choice={'character':'Holy','realm':holy['realm']}
    assert http.post(endpoint,headers=headers,json=choice).status_code==401
    response=http.post(endpoint,headers=AUTH,json=choice)
    assert response.status_code==200,response.text
    target=response.json()['client_id']
    assert response.json()['changed']
    assert not http.post(endpoint,headers=AUTH,json=choice).json()['changed']
    assert len(list((app.state.bridge.root/'role-decisions').glob('*.json')))==1
    # Both old and new Workers keep their manifest contract, with the correct role.
    manifest=http.get('/bridge/v1/manifest',headers=headers).json()
    assert {(c['character'],c['role']) for c in manifest['clients']}=={('Holy','auctioneer'),('Wiw','crafter')}
    # Replayed, timestamp-free old records cannot undo the explicit owner decision.
    http.post('/bridge/v1/characters',headers=headers,json={'characters':[
        row(profile,'Wiw','CAMWOW EU5 Realm 6 PvP',0),holy]})
    manifest=http.get('/bridge/v1/manifest',headers=headers).json()
    assert next(c for c in manifest['clients'] if c['role']=='auctioneer')['id']==target
    assert http.post(endpoint,headers=AUTH,json={'character':'Unknown','realm':holy['realm']}).status_code==422


def test_owner_assignment_respects_active_orders_and_market_boundary(server):
    app,http,profile,device=setup(server)
    holy=row(profile,'Holy','CAMWOW EU5 Realm 4 PvP',0)
    http.post('/bridge/v1/characters',headers={'X-Bridge-Key':device['token']},json={'characters':[holy]})
    w=app.state.catalog.workspace(profile.id)
    identity=device['clients'][0]
    atomic_json(w.production.directory/'orders'/('a'*32+'.json'),
                {'created_at':'2026-11-06','auctioneer_id':identity,'crafter_id':identity,'closed':False})
    endpoint='/server/api/workers/'+device['id']+'/auctioneer'
    response=http.post(endpoint,headers=AUTH,json={'character':'Holy','realm':holy['realm']})
    assert response.status_code==422 and 'активном' in response.text
    assert len(w.production.clients())==1
