import copy
from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from auction_fixture import registry, context
from ahledger.util import atomic_json
from cennik.workspace.web import create_app
from cennik.workspace.neutral import variant_key
from test_workspace import workspace, scan
from test_server import server


def neutral_scan(workspace, *, offset=2, data="2589:4:16:0:4:1;2589:4:24:0:4:1", **changes):
    raw=scan(offset=offset)
    raw.update(market="forever.pvp.neutral.us",auction_context=context("neutral"),data=data,**changes)
    raw['count']=len(data.split(';')) if data else 0
    atomic_json(workspace.root/'collectors/auctioneer_registry.json',registry('neutral'))
    workspace.market.import_scan(raw)
    return raw


def report(workspace, **query):
    return workspace.market.neutral.report(cut=Decimal('5'),**query)


def test_resale_party_uses_whole_lots_and_destination_cut_without_changing_ordinary(workspace):
    workspace.market.import_scan(scan())
    before=workspace.market.rows('select * from working_offers')
    generation=workspace.market.generation()
    neutral_scan(workspace)
    result=report(workspace)
    row=result['items'][0]
    assert (row['quantity'],row['cost_copper'],row['profit_copper'],row['cut_copper'])==(8,40,36,4)
    assert row['sale_copper']==10 and row['roi_percent']==90
    assert 'unknown_sales' in row['flags']
    assert result['summary']['row_count']==2
    assert workspace.market.rows('select * from working_offers')==before
    assert workspace.market.generation()==generation
    assert workspace.market.rows('select count(*) n from scan_registry')[0]['n']==1
    limited=report(workspace,budget=20)['items'][0]
    assert (limited['quantity'],limited['cost_copper'],limited['profit_copper'])==(4,16,22)
    assert report(workspace,budget=15)['items']==[]
    assert report(workspace,budget=0)['items']==[]


def test_partial_late_and_empty_neutral_snapshots_do_not_resurrect_lots(workspace):
    workspace.market.import_scan(scan())
    neutral_scan(workspace)
    neutral_scan(workspace,offset=3,full=False)
    r=report(workspace)
    assert r['summary']['last_attempt']['complete'] is False
    assert r['items'][0]['quantity']==8
    neutral_scan(workspace,offset=0,data='2589:8:8:0:4:1')
    assert report(workspace)['items'][0]['quantity']==8
    neutral_scan(workspace,offset=4,data='')
    assert report(workspace)['items'][0]['quantity']==8
    neutral_scan(workspace,offset=5,data='',empty_market_confirmed=True)
    assert report(workspace)['items']==[]


def test_no_sale_price_and_own_lots_stay_out_of_deals(workspace):
    neutral_scan(workspace,data='2589:4:16:0:4:1:1;2590:1:1:0:4:1:2')
    assert report(workspace)['items']==[]
    all_rows=report(workspace,mode='all')['items']
    assert len(all_rows)==1 and all_rows[0]['item_id']==2590
    assert all_rows[0]['sale_copper'] is None
    assert all_rows[0]['quantity']==0


def test_variants_never_borrow_another_suffix_or_base_quote(workspace):
    ordinary=scan()
    ordinary['variants'][0]['link']='item:2589:0:0:0:0:0:42:0:1:0'
    workspace.market.import_scan(ordinary)
    n=scan()['variants']
    n[0].update(link='item:2589:0:0:0:0:0:43:0:60:0',name='Other suffix')
    neutral_scan(workspace,variants=n)
    assert report(workspace)['items']==[]
    assert 'no_variant_price' in report(workspace,mode='all')['items'][0]['flags']
    n=copy.deepcopy(n);n[0]['link']='item:2589:0:0:0:0:0:42:0:60:0'
    neutral_scan(workspace,offset=3,variants=n)
    row=report(workspace)['items'][0]
    assert row['special_variant'] and row['sale_copper']==10
    assert row['sales_per_day'] is None
    assert variant_key(2589,'item:2589:0:0:0')==variant_key(2589,'|Hitem:2589::::::::60:0::::::::|h[Test]|h')


def test_backfill_reads_only_neutral_archives_and_keeps_all_raw_files(workspace):
    workspace.market.import_scan(scan())
    neutral_scan(workspace)
    raw_files=list((workspace.root/'archive/neutral_auctions').rglob('*.zst'))
    assert len(raw_files)==1
    m=workspace.market
    m.db.execute('delete from neutral_scan_registry');m.db.execute('delete from neutral_offers')
    m.neutral.backfilled=False;m.neutral.cache=None
    assert report(workspace)['summary']['scans']==1
    assert report(workspace)['items'][0]['profit_copper']==36
    assert list((workspace.root/'archive/neutral_auctions').rglob('*.zst'))==raw_files
    assert len(list((workspace.root/'archive/scans').rglob('*.zst')))==1


def test_api_filters_detail_validation_and_cache_invalidation(workspace):
    workspace.market.import_scan(scan())
    neutral_scan(workspace)
    app=create_app(workspace.root,workspace=workspace)
    with TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',1234)) as http:
        url='/api/v2/neutral'
        r=http.get(url).json();key=r['items'][0]['id']
        assert http.get(url,params={'q':'2589'}).json()['total']==1
        assert http.get(url,params={'q':'Льняная'}).json()['total']==1
        assert http.get(url,params={'q':'несуществующее'}).json()['total']==0
        assert http.get(url,params={'min_profit_copper':37}).json()['total']==0
        assert http.get(url,params={'min_roi':91}).json()['total']==0
        assert http.get(url,params={'max_age_seconds':1}).json()['total']==0
        assert http.get(url,params={'sort':'bad'}).status_code==422
        assert http.get(url,params={'budget_copper':-1}).status_code==422
        assert http.get(url,params={'selected':'../bad'}).status_code==422
        detail=http.get(url,params={'selected':key,'mode':'all','budget_copper':20}).json()['items'][0]
        assert len(detail['selected_lots'])==1
        assert detail['selected_lots'][0]['buyout_copper']==16
        assert detail['selected_lots'][0]['profit_copper']==22
        assert http.get(url,params={'offset':50}).json()['items']==[]
        neutral_scan(workspace,offset=6,data='2589:1:30:0:4:1')
        assert http.get(url).json()['total']==0
        assert http.get(url,params={'mode':'all'}).json()['total']==1
        # A newer ordinary book also invalidates resale estimates.
        atomic_json(workspace.root/'collectors/auctioneer_registry.json',registry())
        ordinary=scan(offset=7);ordinary['data']='2589:4:200:0:4:1;2589:4:200:0:4:1'
        workspace.market.import_scan(ordinary)
        assert http.get(url).json()['items'][0]['sale_copper']==50


def test_neutral_uses_existing_server_access_and_market_prefix(server):
    app,http,p=server
    path=f'/m/{p.id}'
    assert http.get(path+'/api/v2/neutral').status_code==401
    headers={'X-Owner-Key':'owner-test-'+'x'*40}
    data=http.get(path+'/api/v2/neutral',headers=headers)
    assert data.status_code==200 and data.json()['items']==[]
    html=http.get(path+'/',headers=headers).text
    assert 'src="/static/next-neutral.js"' in html
    assert 'data-page="neutral"' in html
    assert http.get(path+'/static/next-neutral.js',headers=headers).status_code==200


def test_ahl_median_caps_the_sale_and_own_only_ignores_ahl(workspace):
    workspace.market.import_scan(scan())
    neutral_scan(workspace)
    m=workspace.market
    m.insert_rows('price_snapshots',[{'market':m.market_key,'item_id':2589,'observed_at':datetime.now(UTC).isoformat(),
        'source':'AHL1','min_copper':100,'median_copper':120,'median30d_copper':6,
        'sales_estimated_per_day':2,'raw_id':'ahl-reference','retrieved_at':datetime.now(UTC).isoformat()}])
    m.db.execute('update workspace_revision set revision=revision+1')
    row=report(workspace)['items'][0]
    assert row['sale_copper']==6
    assert row['quantity']==4 and row['profit_copper']==7
    assert row['sales_per_day']=='2.000000'
    assert row['sales_source']=='AHL1'
    own=report(workspace,own_only=True)['items'][0]
    assert own['sale_copper']==10 and own['historical_median_copper'] is None
    assert own['sales_per_day'] is None
    assert own['sales_source'] is None
    assert own['quantity']==8 and own['profit_copper']==36


def test_neutral_markets_are_not_shared_between_profiles(workspace):
    from cennik.workspace.service import Workspace
    workspace.market.import_scan(scan())
    neutral_scan(workspace)
    other=Workspace(workspace.root.parent/'other',market_key='forever.other.alliance.eu')
    try:
        assert other.market.neutral.report(cut=Decimal('5'))['summary']['scans']==0
        assert report(workspace)['summary']['scans']==1
    finally:
        other.close()

# EOF
