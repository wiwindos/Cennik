import time
from unittest.mock import Mock

from ahledger.util import atomic_json
from cennik.worker.agent import WorkerAgent


def worker(tmp_path, monkeypatch, clients=1):
    agent = WorkerAgent(tmp_path / 'worker')
    agent.settings['game_root'] = str(tmp_path / 'game')
    routes = [dict(id=str(i)*32, account='account'+str(i), realm='Realm', character='Char'+str(i), role='observer') for i in range(1, clients+1)]
    agent.manifest = {'clients': routes, 'profile': {'market': 'forever.pvp.alliance.eu', 'faction': 'alliance'}}
    agent.last_manifest = time.monotonic()
    monkeypatch.setattr(agent, 'drive_controls', lambda: None)
    monkeypatch.setattr(agent, 'write_import', Mock())
    return agent, routes


def test_all_accounts_upload_and_publish_ack_without_window(tmp_path, monkeypatch):
    agent, routes = worker(tmp_path, monkeypatch, clients=2)
    assert not agent.cycle.settings['enabled'] and agent.current_route_id is None
    monkeypatch.setattr(agent, 'game_snapshots', lambda: {
        route['id']: ({'market_scans': [{'scan_id': route['id']}]}, (10, 20)) for route in routes})
    seen = []
    def request(method, path, *, body=None):
        seen.append(body['client_id'])
        return {'state': 'completed', 'response': {**{k: body[k] for k in ('client_id','sequence')},
            'payload': {'acknowledged_scan_ids': [body['client_id']]}}}
    monkeypatch.setattr(agent, 'request', request)
    def publish():
        for route_id in seen:
            assert agent.read('imports/'+route_id+'.json', {})['acknowledged_scan_ids'] == [route_id]
    agent.write_import.side_effect = publish
    agent.tick(force=True)
    assert not agent.worker_error
    assert set(seen) == {r['id'] for r in routes}
    assert agent.write_import.call_count == 3
    assert agent.last_scan_success
    assert agent.pending_counts() == (0, 0)
    for route in routes:
        assert agent.read('state-'+route['id']+'.json', {})['stamp'] == [10, 20]


def test_submitted_scan_polls_receipt_without_resending_large_payload(tmp_path, monkeypatch):
    agent, routes = worker(tmp_path, monkeypatch)
    route = routes[0]
    monkeypatch.setattr(agent, 'game_snapshots', lambda: {})
    pending = {'client_id':route['id'], 'sequence':7, 'stamp':[10,20], 'saved':{'market_scans':[{'scan_id':'full'}]},
        'scan_ids':['full'], 'submitted':True}
    atomic_json(agent.root/'requests'/(route['id']+'.json'), pending)
    calls=[]
    def request(method,path,**kwargs):
        calls.append(method)
        return {'sequence':7, 'state':'completed', 'response':{'client_id':route['id'],'sequence':7,
            'payload':{'acknowledged_scan_ids':['full']}}}
    monkeypatch.setattr(agent,'request',request)
    agent.tick(force=True)
    assert calls == ['GET'] and not agent.worker_error
    assert agent.pending_counts() == (0,0)


def test_interrupted_receipt_reposts_original_durable_scan(tmp_path,monkeypatch):
    agent,routes=worker(tmp_path,monkeypatch)
    route=routes[0]
    monkeypatch.setattr(agent,'game_snapshots',lambda:{})
    pending={'client_id':route['id'],'sequence':1,'stamp':[10,20],'saved':{'market_scans':[{'scan_id':'full'}]},
        'scan_ids':['full'],'submitted':True}
    atomic_json(agent.root/'requests'/(route['id']+'.json'),pending)
    calls=[]
    def request(method,path,**kwargs):
        calls.append(method)
        if method=='GET': return {'sequence':1,'state':'interrupted'}
        assert kwargs['body']['saved']==pending['saved']
        return {'state':'completed','response':{'client_id':route['id'],'sequence':1,'payload':{'acknowledged_scan_ids':['full']}}}
    monkeypatch.setattr(agent,'request',request)
    agent.tick(force=True)
    assert calls==['GET','POST'] and not agent.worker_error


def test_queued_upload_survives_restart_and_retrieves_ack(tmp_path,monkeypatch):
    agent,routes=worker(tmp_path,monkeypatch)
    route=routes[0]
    monkeypatch.setattr(agent,'game_snapshots',lambda:{route['id']:({'market_scans':[{'scan_id':'full'}]},(10,20))})
    calls=[]
    def queued(method,path,**kwargs):
        calls.append(method)
        return {'sequence':1,'state':'queued'}
    monkeypatch.setattr(agent,'request',queued)
    agent.stop.set()  # Skip the short in-tick receipt polling wait.
    agent.tick(force=True)
    assert calls==['POST'] and agent.pending_counts()==(1,1)
    assert agent.read('requests/'+route['id']+'.json',{})['submitted']
    restarted,routes=worker(tmp_path,monkeypatch)
    monkeypatch.setattr(restarted,'game_snapshots',lambda:{})
    def completed(method,path,**kwargs):
        calls.append(method)
        return {'sequence':1,'state':'completed','response':{'client_id':route['id'],'sequence':1,
            'payload':{'acknowledged_scan_ids':['full']}}}
    monkeypatch.setattr(restarted,'request',completed)
    restarted.tick(force=True)
    assert calls==['POST','GET'] and not restarted.worker_error
    assert restarted.pending_counts()==(0,0)


def test_unpublished_ack_keeps_durable_request_and_file_unconsumed(tmp_path,monkeypatch):
    agent,routes=worker(tmp_path,monkeypatch)
    route=routes[0]
    monkeypatch.setattr(agent,'game_snapshots',lambda:{route['id']:({},(10,20))})
    monkeypatch.setattr(agent,'request',lambda *a,**kw:{'state':'completed','response':{
        'client_id':route['id'],'sequence':1,'payload':{'acknowledged_scan_ids':[]}}})
    agent.write_import.side_effect=OSError('fixture locked import')
    agent.tick(force=True)
    assert agent.worker_error
    assert (agent.root/'requests'/(route['id']+'.json')).exists()
    assert agent.read('state-'+route['id']+'.json',{}).get('stamp') is None


def test_queue_count_ignores_heartbeat_files_without_removing_them(tmp_path):
    agent=WorkerAgent(tmp_path)
    atomic_json(tmp_path/'requests/heartbeat.json',{'saved':None})
    atomic_json(tmp_path/'requests/full.json',{'saved':{'market_scans':[{'scan_id':'one'},{'scan_id':'two'}]}})
    assert agent.pending_counts()==(2,2)
    assert (tmp_path/'requests/heartbeat.json').exists()


def test_unchanged_game_save_is_parsed_once_while_awaiting_server(tmp_path,monkeypatch):
    agent=WorkerAgent(tmp_path/'worker')
    path=tmp_path/'game/WTF/Account/one/SavedVariables/CennikAddon.lua'
    path.parent.mkdir(parents=True)
    path.write_text('CennikAddonDB={binding={market="fixture"}}',encoding='utf-8')
    import cennik.worker.agent as module
    original=module.read_saved_variables
    parser=Mock(side_effect=original)
    monkeypatch.setattr(module,'read_saved_variables',parser)
    stamp=lambda:(path.stat().st_size,path.stat().st_mtime_ns)
    first=agent.read_snapshot(path,stamp())
    assert agent.read_snapshot(path,stamp()) is first and parser.call_count==1
    path.write_text('CennikAddonDB={binding={market="changed-fixture"}}',encoding='utf-8')
    assert agent.read_snapshot(path,stamp())['binding']['market']=='changed-fixture'
    assert parser.call_count==2


def test_bad_account_save_does_not_block_another_accounts_scan(tmp_path,monkeypatch):
    from cennik.addon.lua_data import lua_literal
    agent,routes=worker(tmp_path,monkeypatch)
    route=routes[0]
    for account,body in [('broken','CennikAddonDB={invalid'),(route['account'],'CennikAddonDB='+lua_literal({
        'binding':{'market':agent.manifest['profile']['market'],'character':route['character'],'realm':route['realm']},
        'market_scans':[{'scan_id':'good','market':agent.manifest['profile']['market'],'realm':route['realm']}]}))]:
        path=tmp_path/'game/WTF/Account'/account/'SavedVariables/CennikAddon.lua'
        path.parent.mkdir(parents=True)
        path.write_text(body,encoding='utf-8')
        stat=path.stat()
        agent.quiet[str(path)]=((stat.st_size,stat.st_mtime_ns),time.monotonic()-2)
    # Use the real discovery routine; no cycle or window is configured.
    saved=agent.game_snapshots()
    assert saved[route['id']][0]['market_scans'][0]['scan_id']=='good'
    assert 'broken' in agent.file_errors
