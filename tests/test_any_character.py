from PIL import Image, ImageDraw
from test_addon import exchange as exchange
from test_auction_cycle import TARGET, Window
from test_automation_lua import automatic
from test_cennik import analytics_root as analytics_root
from test_server import exchange as remote_exchange
from test_server import register
from test_server import server as server
from test_workspace import workspace as workspace

from cennik.worker.automation import (
    ANY_CHARACTER_COLOR,
    END,
    MAGIC,
    AuctionCycle,
    Signal,
    character_key,
    decode_signal,
)
from cennik.workspace.sniper import SnipeRule


def discover(app,http,profile,device,character='New',account='third'):
    row={'character':character,'account':account,'realm':'FixtureRealm','role':'unassigned','faction':'alliance',
         'market':profile.market,'game_version':'1.60.1'}
    response=http.post('/bridge/v1/characters',headers={'X-Bridge-Key':device['token']},
        json={'automatic_observers':True,'characters':[row]})
    assert response.status_code==200,response.text
    return response.json()


def test_unassigned_character_registers_and_exchanges_without_production_role(server):
    app,http,profile=server
    device=register(http,profile)
    w=app.state.catalog.workspace(profile.id)
    before=[c.model_dump() for c in w.production.clients()]
    rule=SnipeRule(market=profile.market,item_id=2589,quantity=20)
    w.sniper.save(rule)
    manifest=discover(app,http,profile,device)
    observed=next(c for c in manifest['clients'] if c['character']=='New')
    assert observed['role']=='observer'
    assert [c.model_dump() for c in w.production.clients()]==before
    payload=remote_exchange(http,device['token'],observed['id'],1)['payload']
    assert payload['packages']==[]
    assert payload['sniper_book']['rules'][0]['id']==rule.id
    assert payload['production']['client']['role']=='observer'
    from auction_fixture import proposal
    from test_workspace import scan

    from cennik.collector.auctioneers import Review
    from cennik.collector.protocol import Observation
    npc=proposal().model_copy(update={'server_id':profile.id,'market':profile.market})
    record=Observation(kind='auctioneer_proposal',data=npc.model_dump(mode='json'))
    app.state.hub.auctioneers.suggest(record.identity(),record.data,'fixture')
    app.state.hub.auctioneers.review(record.identity(),Review(action='approve',auction_type='ordinary'))
    raw=scan()
    raw['market']=profile.market
    raw['auction_context'].update(server_id=profile.id,scope_id=npc.scope_id())
    saved={'device_id':observed['id'],'binding':{'character':'New','realm':'FixtureRealm','market':profile.market},'market_scans':[raw]}
    receipt=remote_exchange(http,device['token'],observed['id'],2,saved)['payload']
    assert raw['scan_id'] in receipt['acknowledged_scan_ids']
    assert w.market.rows('SELECT count(*) n FROM scan_registry')[0]['n']==1
    again=discover(app,http,profile,device)
    assert next(c for c in again['clients'] if c['character']=='New')['id']==observed['id']
    legacy=http.get('/bridge/v1/manifest',headers={'X-Bridge-Key':device['token'],'X-Cennik-Worker':'0.3'}).json()
    assert 'any_character' not in legacy and all(c['role']!='observer' for c in legacy['clients'])
    modern=http.get('/bridge/v1/manifest',headers={'X-Bridge-Key':device['token'],'X-Cennik-Worker':'0.4'}).json()
    assert modern['any_character'] and modern['session']['workspace_id']==w.identity
    other=http.post(f'/server/api/markets/{profile.id}/workers',
        headers={'X-Owner-Key':'owner-test-'+'x'*40},json={'label':'other','clients':[]}).json()
    forbidden=http.post('/bridge/v1/exchange',headers={'X-Bridge-Key':other['token']},
        json={'client_id':observed['id'],'sequence':1,'saved':None})
    assert forbidden.status_code==403


def test_shared_progress_is_account_scoped_monotonic_and_unknown_purchase_blocks(workspace):
    rule=SnipeRule(market=workspace.market_key,item_id=2589,quantity=20)
    workspace.sniper.save(rule)
    def report(owner,bought,events=None):
        return workspace.sniper.accept_game({'workspace_id':workspace.identity,'events':events or [],
            'rules':{rule.id:{'bought':999,'own_bought':bought,'spent':100,'own_spent':bought*10,'stock':0}}},owner)
    report('one-account',4)
    report('one-account',4)
    report('one-account',2)
    report('other-account',3)
    total=workspace.sniper.wire('one-account')['shared_progress'][rule.id]
    assert (total['bought'],total['own_bought'],total['spent'])==(7,4,70)
    report('other-account',3,[{'id':'purchase:start','kind':'purchase','attempt_id':'x','state':'unknown'}])
    assert workspace.sniper.wire()['purchase_unresolved']
    report('other-account',3,[{'id':'purchase:end','kind':'purchase','attempt_id':'x','state':'not_bought'}])
    assert not workspace.sniper.wire()['purchase_unresolved']


def test_dynamic_marker_encodes_character_and_is_checksum_protected():
    token='123456abcdef'
    identity=character_key('Вечная Песня','Игрок')
    digits=[int(v,16) for v in f'{identity:08x}']
    digits.append(sum(digits)%16)
    colors=[MAGIC,(0x12,0x34,0x56),(0xab,0xcd,0xef),(104,30,44),(151,225,211),END,ANY_CHARACTER_COLOR]
    colors += [tuple(16+14*n for n in digits[i:i+3]) for i in range(0,9,3)]
    image=Image.new('RGB',(440,220),'black')
    draw=ImageDraw.Draw(image)
    for i,color in enumerate(colors):
        draw.rectangle((12+i*10,12,21+i*10,21),fill=color)
    assert decode_signal(image,token)[0]==Signal(3,1,2,identity)
    draw.rectangle((102,12,111,21),fill=(16,16,16))
    assert decode_signal(image,token) is None


def test_bootstrap_without_route_reloads_then_waits_for_registration(tmp_path):
    window=Window()
    clock=[0]
    cycle=AuctionCycle(tmp_path,window,lambda:clock[0])
    cycle.configure(target=TARGET,game_root='game',enabled=True,any_character=True)
    for i in range(1,3):
        clock[0]=i
        window.value=Signal(7,i,1,123)
        cycle.tick(paused=False,route=None,stamp=None,delivered=False)
    assert window.sent==['F8']
    for i in range(3,5):
        clock[0]=i
        window.value=Signal(7,i,2,123)
        cycle.tick(paused=False,route=None,stamp=None,delivered=False)
    assert window.sent==['F8']


def test_dynamic_addon_accepts_observer_and_requests_automatic_bootstrap(workspace,tmp_path):
    lua=automatic(workspace,tmp_path)
    lua.execute("""
      CennikImport.automation.any_character=true
      CennikImport.worker_discovery=true
      CennikImport.production.client.role='observer'
      Cennik.Automation:Initialize();Game.advance(4);Cennik.Automation:Tick(1)
      assert(Cennik.Automation.config and Cennik.Automation.config.any_character)
      CennikImport.automation.client_id=nil;Cennik.Automation:Initialize()
      Game.advance(4);Cennik.Automation:Tick(1)
      assert(Cennik.Automation.code==7)
      Cennik.CharacterRole:Capture()
    """)
    assert lua.eval("Cennik.db.character_profiles[GetRealmName() .. ':' .. UnitName('player')].role") in ('auctioneer','unassigned')


def test_registration_two_reload_handshake_reopens_only_after_delivery(tmp_path):
    window=Window()
    clock=[0]
    cycle=AuctionCycle(tmp_path,window,lambda:clock[0])
    cycle.configure(target=TARGET,game_root='game',enabled=True,any_character=True)
    route={'id':'a'*32,'role':'observer','character':'New','realm':'FixtureRealm'}
    def tick(code,boot,*,registered=True,delivered=True,stamp=(10,1)):
        clock[0]+=2
        window.value=Signal(code,int(clock[0])%16,boot,123)
        cycle.tick(paused=False,route=route if registered else None,stamp=stamp if registered else None,delivered=delivered)
    tick(7,1,registered=False,delivered=False)
    tick(7,1,registered=False,delivered=False)
    assert window.sent==['F8']
    tick(7,2,delivered=False)
    assert window.sent==['F8']
    tick(7,2)
    assert window.sent==['F8','F8']
    tick(4,3,delivered=False,stamp=(11,2))
    tick(4,3,delivered=False,stamp=(11,2))
    assert window.sent==['F8','F8']
    for _ in range(3):
        tick(4,3,stamp=(11,2))
    assert window.sent==['F8','F8','F9']
    tick(2,3,stamp=(11,2))
    assert cycle.state['phase']=='watch'


def test_sniper_shared_counter_does_not_double_count_unacknowledged_local_purchase(exchange):
    from test_sniper import game
    lua=game(exchange)
    lua.execute("""
      r.own_bought=2;r.bought=2;r.own_spent=20;r.spent=20
      CennikImport.sniper_book={workspace_id='test-scope',revision='new',budget_copper=10000,
        rules={definition},references={},shared_progress={['fixture-rule']={bought=5,own_bought=1,spent=50,own_spent=10,stock=0}}}
      Cennik.Sniper:Initialize()
      assert(r.bought==6 and r.spent==60 and r.own_bought==2)
      Cennik.Sniper:Initialize()
      assert(r.bought==6 and r.spent==60 and r.own_bought==2)
      local total=CennikImport.sniper_book.shared_progress['fixture-rule']
      total.bought=6;total.own_bought=2;total.spent=60;total.own_spent=20
      Cennik.Sniper:Initialize()
      assert(r.bought==6 and r.spent==60)
    """)
