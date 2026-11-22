import threading
import time
from datetime import UTC,datetime
from decimal import Decimal

from test_neutral import workspace,scan,neutral_scan,report
from cennik.workspace.data import WorkspaceProvider


def estimate(workspace, item=2589, value=2):
    at=datetime.now(UTC).isoformat()
    workspace.market.insert_rows('price_snapshots',[{'market':workspace.market_key,'item_id':item,
        'source':'AHL1','observed_at':at,'min_copper':900,'median_copper':1000,
        'sales_estimated_per_day':value,'raw_id':'item-estimate','retrieved_at':at}])
    workspace.market.db.execute('update workspace_revision set revision=revision+1')


def test_variant_keeps_exact_price_but_shows_item_liquidity(workspace):
    ordinary=scan();ordinary['variants'][0]['link']='item:2589:0:0:0:0:0:42:0:60'
    workspace.market.import_scan(ordinary)
    neutral_scan(workspace,variants=ordinary['variants'])
    estimate(workspace,value=Decimal('2.5'))
    row=report(workspace)['items'][0]
    assert row['sale_copper']==10  # never price the suffix using AHL base price 900
    assert row['sales_per_day']=='2.500000' and row['sales_source']=='AHL1'
    assert row['sales_scope']=='all_variants' and 'unknown_sales' not in row['flags']
    assert row['sales_unavailable_reason'] is None
    assert report(workspace,min_sales=2,max_sales=3)['total']==1
    own=report(workspace,own_only=True)['items'][0]
    assert own['sales_per_day'] is None and own['sales_unavailable_reason']=='own_data_only'


def test_unpriced_variant_still_has_liquidity_and_known_zero_is_not_unknown(workspace):
    neutral_scan(workspace,variants=[{'link':'item:2589:0:0:0:0:0:42','name':'Suffix'}])
    estimate(workspace,value=0)
    row=report(workspace,mode='all')['items'][0]
    assert row['sale_copper'] is None
    assert row['sales_per_day']=='0.000000' and 'unknown_sales' not in row['flags']
    assert report(workspace,mode='all',max_sales=0)['total']==1


def test_prepared_comparison_filters_without_waiting_for_market_write(workspace):
    workspace.market.import_scan(scan());neutral_scan(workspace);estimate(workspace)
    report(workspace)
    entered=threading.Event();release=threading.Event()
    def hold():
        with workspace.market.lock:entered.set();release.wait(5)
    thread=threading.Thread(target=hold);thread.start();assert entered.wait(1)
    try:
        start=time.monotonic();result=report(workspace,min_sales=1,sort='sales',prepared=True)
        assert time.monotonic()-start<.5
        assert result['comparison_refreshing'] and result['total']==1
        assert result['items'][0]['sales_per_day']=='2.000000'
    finally:release.set();thread.join(2)
    assert not report(workspace)['comparison_refreshing']


def test_scoped_prices_match_full_provider_and_skip_unrelated_items(workspace):
    ordinary=scan();ordinary.update(data='2589:1:10:0:4:1;2590:1:20:0:4:2',count=2,
        variants=[*ordinary['variants'],{'link':'item:2590:0:0:0','name':'Second'}])
    workspace.market.import_scan(ordinary);estimate(workspace)
    for own in (False,True):
        with WorkspaceProvider(workspace.market,workspace.market_key,datetime.now(UTC),own_data_only=own) as p:
            all_prices=p.current_prices()
            assert p.current_prices([2589])=={2589:all_prices[2589]}
            assert p.current_prices([])=={}
            assert p.current_prices([987654321])=={}


def test_comparison_prices_only_base_items_and_preserves_own_mode(workspace,monkeypatch):
    workspace.market.import_scan(scan())
    neutral_scan(workspace,variants=[{'link':'item:2589:0:0:0:0:0:42','name':'Suffix'}])
    calls=[];original=WorkspaceProvider.current_prices
    def scoped(self,item_ids=None):calls.append(item_ids);return original(self,item_ids)
    monkeypatch.setattr(WorkspaceProvider,'current_prices',scoped)
    result=report(workspace,mode='all')
    assert calls==[[]] and result['items'][0]['sales_unavailable_reason']=='no_source_estimate'


def test_source_update_refreshes_in_background_without_delaying_new_filter(workspace,monkeypatch):
    workspace.market.import_scan(scan());neutral_scan(workspace);estimate(workspace,value=2)
    report(workspace)
    neutral=workspace.market.neutral
    entered=threading.Event();release=threading.Event();original=neutral._comparison
    def slow(**kwargs):entered.set();release.wait(5);return original(**kwargs)
    monkeypatch.setattr(neutral,'_comparison',slow)
    estimate(workspace,value=4)
    try:
        start=time.monotonic();previous=report(workspace,min_sales=1,prepared=True)
        assert time.monotonic()-start<.5 and entered.wait(1)
        assert previous['comparison_refreshing'] and previous['items'][0]['sales_per_day']=='2.000000'
        assert report(workspace,min_sales=3,prepared=True)['total']==0
    finally:
        release.set()
        if neutral.refresh_thread:neutral.refresh_thread.join(3)
    current=report(workspace,min_sales=3,prepared=True)
    assert not current['comparison_refreshing']
    assert current['items'][0]['sales_per_day']=='4.000000'

# EOF
