import threading

from test_auction_cycle import ROUTE, TARGET, Signal, Window

from ahledger.util import atomic_json
from cennik.worker.agent import WorkerAgent


def test_cycle_continues_while_network_worker_is_blocked(tmp_path,monkeypatch):
    agent=WorkerAgent(tmp_path)
    agent.settings['game_root']='fixture'
    network_entered=threading.Event()
    release_network=threading.Event()
    control_after_network=threading.Event()
    window=Window()
    clock=[0]
    agent.cycle.backend=window
    agent.cycle.clock=lambda: clock[0]
    agent.cycle.configure(target=TARGET,route=ROUTE,game_root='fixture',enabled=True,reload_key='F7')
    def blocked_network():
        network_entered.set()
        release_network.wait(10)
    def control():
        if network_entered.is_set():
            clock[0]+=1
            window.value=Signal(3,clock[0]%16,1)
            agent.cycle.tick(paused=False,route=ROUTE,stamp=(1,1),delivered=True)
        if window.sent:
            control_after_network.set()
    monkeypatch.setattr(agent,'tick',blocked_network)
    monkeypatch.setattr(agent,'drive_controls',control)
    try:
        agent.start()
        assert network_entered.wait(2)
        assert control_after_network.wait(3)
        assert window.sent==['F7']
    finally:
        release_network.set()
        agent.close()
    assert not agent.control_thread.is_alive()


def test_empty_poll_does_not_block_delivered_file_but_upload_still_blocks(tmp_path,monkeypatch):
    agent=WorkerAgent(tmp_path/'worker')
    game=tmp_path/'game'
    source=game/'WTF/Account/one/SavedVariables/CennikAddon.lua'
    source.parent.mkdir(parents=True)
    source.write_text('fixture')
    stamp=[source.stat().st_size,source.stat().st_mtime_ns]
    route={**ROUTE,'account':'one','realm':'FixtureRealm','character':'Fixture'}
    agent.settings['game_root']=str(game)
    agent.manifest={'clients':[route]}
    agent.cycle.settings.update(enabled=True,client_id=route['id'])
    received=[]
    monkeypatch.setattr(agent.cycle,'tick',lambda **kwargs:received.append(kwargs))
    atomic_json(agent.root/('state-'+route['id']+'.json'),{'stamp':stamp})
    pending=agent.root/'requests'/(route['id']+'.json')
    atomic_json(pending,{'saved':None})
    agent.cycle_tick()
    assert received[-1]['delivered']
    atomic_json(pending,{'saved':{}})
    agent.cycle_tick()
    assert not received[-1]['delivered']
    source.write_text('new saved fixture')
    agent.next_tick=100000
    agent.wake.clear()
    agent.cycle_tick()
    assert agent.next_tick==0 and agent.client.next_attempt==0 and agent.wake.is_set()


def test_selected_import_is_written_before_file_is_acknowledged(tmp_path,monkeypatch):
    import time
    agent=WorkerAgent(tmp_path/'worker')
    route={**ROUTE,'account':'one','realm':'FixtureRealm','character':'Fixture'}
    agent.settings['game_root']=str(tmp_path/'game')
    agent.manifest={'clients':[route]}
    agent.last_manifest=time.monotonic()
    agent.current_route_id=route['id']
    monkeypatch.setattr(agent,'game_snapshots',lambda:{route['id']:({},(10,20))})
    response={'state':'completed','response':{'sequence':1,'client_id':route['id'],'payload':{'acknowledged_scan_ids':[]}}}
    monkeypatch.setattr(agent,'request',lambda *a,**kw:response)
    observations=[]
    def write():
        assert (agent.root/'imports'/(route['id']+'.json')).exists()
        observations.append(agent.read('state-'+route['id']+'.json',{}).get('stamp'))
    monkeypatch.setattr(agent,'write_import',write)
    agent.tick(force=True)
    assert not agent.worker_error
    assert observations[0] is None and observations[-1]==[10,20]
