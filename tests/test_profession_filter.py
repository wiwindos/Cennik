from datetime import UTC, datetime

from fastapi.testclient import TestClient
from test_bulk import opportunity
from test_workspace import workspace as workspace

from cennik.core.models import Run
from cennik.workspace.web import create_app


def seed(workspace):
    values=[]
    for i in range(36):
        op=opportunity(str(i),strategy=('simple_craft','craft_graph','craft_disenchant')[i%3])
        op.details['recipe']={'profession':'Tailoring' if i<32 else 'Alchemy'}
        values.append(op)
    values.append(opportunity('npc',strategy='vendor_arbitrage'))
    run=Run(run_id='bulk-fixture',market=workspace.market_key,started_at=datetime.now(UTC),as_of=datetime.now(UTC),
            generation=workspace.generation(),config=workspace.store.settings().config,strategy_versions={},
            status='completed',signal_count=len(values))
    workspace.store.begin(run,'profession-fixture')
    workspace.store.finish(run,values)


def test_profession_filter_covers_all_pages_strategies_and_bulk_candidate_query(workspace):
    seed(workspace)
    app=create_app(workspace.root,workspace=workspace)
    with TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',12345)) as client:
        base='/api/v2/analytics/opportunities'
        all_items=client.get(base,params={'limit':1}).json()
        assert all_items['total']==37
        assert all_items['professions']==['Alchemy','Tailoring']
        tail=client.get(base,params={'profession':'tailoring','limit':30,'sort':'profit'}).json()
        assert tail['total']==32 and len(tail['items'])==30
        second=client.get(base,params={'profession':'Tailoring','limit':30,'offset':30,'sort':'profit'}).json()
        assert len(second['items'])==2
        assert not {v['id'] for v in tail['items']} & {v['id'] for v in second['items']}
        for strategy in ('simple_craft','craft_graph','craft_disenchant'):
            result=client.get(base,params={'profession':'Alchemy','strategy':strategy}).json()
            assert result['items'] and all(v['details']['recipe']['profession']=='Alchemy' and v['strategy_id']==strategy for v in result['items'])
            assert result['professions']==['Alchemy','Tailoring']
        # This is the query used by bulk's “first N” selection, independent of the visible page.
        bulk=client.get(base,params={'profession':'Tailoring','limit':200,'offset':0,'sort':'roi'}).json()
        assert len(bulk['items'])==32
        assert client.get(base,params={'profession':'Alchemy','min_profit':1000}).json()['items']==[]
        assert client.get(base,params={'profession':"Alchemy' OR 1=1 --"}).json()['total']==0
        assert client.get(base,params={'profession':'Alchemy','strategy':'vendor_arbitrage'}).json()['total']==0
        assert client.get(base,params={'profession':'a'*101}).status_code==422


def test_empty_market_and_unpublished_history_do_not_supply_professions(workspace):
    seed(workspace)
    result=workspace.store.listing('other-market',profession='Tailoring')
    assert result['items']==[] and result['professions']==[]
    with workspace.store.connection() as db:
        db.execute('DELETE FROM published WHERE market=?',[workspace.market_key])
    assert workspace.store.listing(workspace.market_key)['professions']==[]
