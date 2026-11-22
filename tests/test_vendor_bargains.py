from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from test_workspace import workspace, scan
from test_neutral import neutral_scan

from cennik.workspace.models import ItemFacts
from cennik.workspace.vendor_bargains import VendorBargains
from cennik.workspace.web import create_app


def load(w,data='2589:4:404:0:4:1;2589:1:105:0:4:1;2590:1:204:0:4:2',**changes):
    raw=scan(offset=changes.pop('offset',0),count=len(data.split(';')) if data else 0)
    raw.update({'data':data,'variants':[{**raw['variants'][0],'vendor_sell':100},
        {'link':'item:2590:0:0:0','name':'Second item','vendor_sell':200}],**changes})
    w.market.import_scan(raw)
    return raw


def test_one_row_per_item_and_minimum_is_per_unit(workspace):
    load(workspace)
    result=VendorBargains(workspace).report(max_markup=10)
    assert result['total']==2 and result['format_version']==2
    a,b=result['items']
    assert (a['item_id'],a['min_unit_copper'],a['markup_percent'],a['available_quantity'])==(2589,101,1,5)
    assert (b['item_id'],b['min_unit_copper'],b['markup_percent'])==(2590,204,2)
    assert result['summary']=={'items':2,'quantity':6,'below_npc':0,'equal_npc':0,'above_npc':2}
    assert 'sequence' not in a and 'profit_copper' not in a


def test_huge_emerald_duplicates_collapse_and_markup_is_signed(workspace):
    load(workspace,';'.join(['12364:1:10003:0:4:1']*82),
        variants=[{'link':'item:12364:0:0:0','name':'Huge Emerald','vendor_sell':10000}])
    report=VendorBargains(workspace)
    result=report.report(max_markup=Decimal('0.03'))
    assert result['total']==1
    row=result['items'][0]
    assert row['min_unit_copper']==10003 and row['difference_copper']==3
    assert row['markup_percent']==pytest.approx(.03) and row['available_quantity']==82
    assert report.report(max_markup=Decimal('0.0299'))['total']==0


def test_fractional_unit_price_and_own_offers_are_part_of_book(workspace):
    load(workspace,'2589:3:1:0:4:1:1;2589:2:1:0:4:1:2;2589:1:0:0:4:1:2',
        variants=[{'link':'item:2589:0:0:0','vendor_sell':1}])
    row=VendorBargains(workspace).report()['items'][0]
    assert row['min_unit_copper']==pytest.approx(1/3)
    assert row['owned_at_min'] is True and row['available_quantity']==5


def test_unknown_or_zero_vendor_at_floor_cannot_be_replaced_by_a_costlier_offer(workspace):
    variants=[{'link':'item:2589:0:0:0:0:0:8','name':'Unknown suffix'},
        {'link':'item:2589:0:0:0','name':'Known base','vendor_sell':100}]
    load(workspace,'2589:1:1:0:4:1;2589:1:2:0:4:2',variants=variants)
    assert VendorBargains(workspace).report()['total']==0
    variants[0]['vendor_sell']=0
    load(workspace,'2589:1:1:0:4:1;2589:1:2:0:4:2',offset=3,variants=variants)
    assert VendorBargains(workspace).report()['total']==0


def test_vendor_value_and_filter_belong_to_cheapest_variant(workspace):
    load(workspace,'2589:1:11:0:4:1;2589:1:12:0:4:2',variants=[
        {'link':'item:2589:0:0:0:0:0:8','name':'Cheapest variant','vendor_sell':10},
        {'link':'item:2589:0:0:0','name':'Dearer variant','vendor_sell':100}])
    report=VendorBargains(workspace)
    assert report.report(max_markup=0)['total']==0
    row=report.report()['items'][0]
    assert row['min_unit_copper']==11 and row['vendor_sell_copper']==10
    assert row['special_variant'] and row['markup_percent']==10


def test_manual_reference_is_used_only_when_base_floor_has_no_vendor_quote(workspace):
    load(workspace,'2589:4:40:0:4:1',variants=[{'link':'item:2589:0:0:0','name':'Base'}])
    workspace.update_facts(2589,ItemFacts(vendor_sell_copper=100))
    report=VendorBargains(workspace)
    assert report.report()['items'][0]['markup_percent']==-90
    workspace.update_facts(2589,ItemFacts(vendor_sell_copper=1))
    assert report.report()['items'][0]['markup_percent']==900


def test_closeness_sort_and_optional_range(workspace):
    load(workspace,'2589:1:90:0:4:1;2590:1:204:0:4:2;12364:1:10003:0:4:3',variants=[
        {'link':'item:2589:0:0:0','vendor_sell':100},
        {'link':'item:2590:0:0:0','vendor_sell':200},
        {'link':'item:12364:0:0:0','vendor_sell':10000}])
    report=VendorBargains(workspace)
    assert [r['item_id'] for r in report.report()['items']]==[12364,2590,2589]
    assert [r['item_id'] for r in report.report(sort='markup')['items']]==[2589,12364,2590]
    assert report.report(min_markup=-5,max_markup=5)['total']==2
    assert report.report(min_markup=0,max_markup=1)['total']==1
    assert report.report(max_markup=None)['total']==3


def test_partial_and_neutral_scans_never_replace_ordinary_floor(workspace):
    load(workspace)
    load(workspace,'2589:1:1:0:4:1',offset=4,full=False)
    neutral_scan(workspace,offset=5,data='2589:1:1:0:4:1',variants=[{'link':'item:2589:0:0:0','vendor_sell':100}])
    assert VendorBargains(workspace).report()['items'][0]['min_unit_copper']==101


def test_search_age_and_item_pagination(workspace):
    load(workspace);report=VendorBargains(workspace)
    workspace.market.names.put([('item',2589,'Linen cloth')],'enUS',stamp=1)
    assert report.report(q='Linen cloth')['total']==1
    assert report.report(q='%')['total']==0
    assert report.report(max_age_seconds=1)['total']==0
    assert report.report(sort='price',limit=1,offset=1)['items'][0]['item_id']==2590


def test_no_book_means_no_invented_ahl_or_beta_supply(workspace):
    result=VendorBargains(workspace).report()
    assert result['total']==0 and result['snapshot'] is None


def test_large_price_floor_is_chosen_before_float_conversion(workspace):
    load(workspace,'2589:2:9007199254740991:0:4:1:1;2589:1:4503599627370496:0:4:1:2',
        variants=[{'link':'item:2589:0:0:0','vendor_sell':9007199254740991}])
    assert VendorBargains(workspace).report()['items'][0]['owned_at_min'] is True


def test_route_filters_and_new_navigation(workspace):
    load(workspace);app=create_app(workspace.root,workspace=workspace)
    with TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',12345)) as http:
        result=http.get('/api/v2/vendor-bargains?limit=1').json()
        assert result['format_version']==2 and result['total']==2 and len(result['items'])==1
        assert http.get('/api/v2/vendor-bargains?max_markup=1.5').json()['total']==1
        assert http.get('/api/v2/vendor-bargains?min_markup=2&max_markup=1').status_code==422
        assert http.get('/api/v2/vendor-bargains?max_markup=0.00001').status_code==422
        assert http.get('/api/v2/vendor-bargains?sort=sql').status_code==422
        assert http.get('/api/v2/vendor-bargains?stackable_only=true').json()['total']==1
        assert http.get('/api/v2/vendor-bargains?stackable_only=unknown').status_code==422
        assert 'Рядом с ценой NPC' in http.get('/').text


def test_stackable_filter_is_item_property_not_number_of_lots_or_cheapest_stack(workspace):
    from datetime import datetime,UTC
    variants=[{'link':f'item:{i}:0:0:0','name':str(i),'vendor_sell':v,'divisible':False}
        for i,v in ((2589,100),(2590,200),(12364,10000),(7777,100))]
    data=';'.join(['2589:1:101:0:4:1',*['2590:1:204:0:4:2']*50,
        '12364:1:10003:0:4:3','12364:20:200060:0:4:3','7777:1:101:0:4:4'])
    load(workspace,data,variants=variants)
    workspace.market.insert_rows('items',[{'item_id':i,'stack_size':size,'raw_id':'stack-reference',
        'origin_dataset':'item','retrieved_at':datetime.now(UTC).isoformat()} for i,size in ((2589,20),(2590,1))])
    report=VendorBargains(workspace)
    assert report.report()['total']==4
    result=report.report(stackable_only=True)
    assert {r['item_id'] for r in result['items']}=={2589,12364}
    assert all(r['stackable'] is True for r in result['items'])
    assert result['summary']['quantity']==22
    rows={r['item_id']:r for r in report.report()['items']}
    assert rows[2590]['available_quantity']==50 and rows[2590]['stackable'] is False
    assert rows[7777]['stackable'] is None
    assert rows[12364]['min_unit_copper']==10003


def test_bid_only_stack_confirms_single_unit_buyout_item(workspace):
    load(workspace,'2589:1:101:0:4:1;2589:20:0:0:4:1',
        variants=[{'link':'item:2589:0:0:0','vendor_sell':100,'divisible':False}])
    row=VendorBargains(workspace).report(stackable_only=True)['items'][0]
    assert row['stackable'] is True and row['available_quantity']==1

# EOF
