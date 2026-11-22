from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from test_neutral import workspace, scan, neutral_scan, report
from cennik.workspace.web import create_app


@pytest.fixture
def prices(workspace):
    ids=list(range(2589,2594))
    buy=[10,20,50,100,25];sale=[100,100,200,200,100];sales=[2,15,100,None,0]
    variants=[{'link':f'item:{i}:0:0:0','name':f'Item {i}'} for i in ids]
    ordinary=scan(count=5)
    ordinary.update(data=';'.join(f'{i}:1:{p}:0:4:{n}' for n,(i,p) in enumerate(zip(ids,sale),1)),variants=variants)
    workspace.market.import_scan(ordinary)
    neutral_scan(workspace,data=';'.join(f'{i}:1:{p}:0:4:{n}' for n,(i,p) in enumerate(zip(ids,buy),1)),variants=variants)
    stamp=datetime.now(UTC).isoformat()
    workspace.market.insert_rows('price_snapshots',[{'market':workspace.market_key,'item_id':i,'observed_at':stamp,
        'source':'AHL1','min_copper':p,'median_copper':p,'sales_estimated_per_day':s,
        'raw_id':'filters','retrieved_at':stamp} for i,p,s in zip(ids,sale,sales)])
    workspace.market.db.execute('update workspace_revision set revision=revision+1')
    return workspace


def ids(result):return [r['item_id'] for r in result['items']]


def test_sales_sort_is_numeric_and_unknown_last_in_both_directions(prices):
    assert ids(report(prices,sort='sales'))==[2591,2590,2589,2593,2592]
    assert ids(report(prices,sort='sales',order='asc'))==[2593,2589,2590,2591,2592]
    assert ids(report(prices,sort='sales',offset=2,limit=2))==[2589,2593]
    assert ids(report(prices,min_sales=0))==[2591,2589,2590,2593]
    assert ids(report(prices,min_sales=3,max_sales=20))==[2590]
    assert ids(report(prices,max_sales=0))==[2593]


def test_price_and_roi_ranges_combine_before_pagination(prices):
    assert ids(report(prices,sort='buy_price',order='asc'))==[2589,2590,2593,2591,2592]
    assert ids(report(prices,sort='sale_price'))==[2591,2592,2589,2590,2593]
    assert set(ids(report(prices,min_price=20,max_price=50)))=={2590,2591,2593}
    assert set(ids(report(prices,price_basis='sale',min_price=150,max_price=200)))=={2591,2592}
    assert ids(report(prices,min_roi=100,max_roi=300,sort='buy_price'))==[2591,2593]
    assert ids(report(prices,min_sales=10,price_basis='sale',min_price=150,min_roi=200,max_roi=300))==[2591]
    assert report(prices,min_sales=10,price_basis='sale',min_price=150,min_roi=200,max_roi=300,offset=1)['total']==1


def test_filter_api_ranges_validation_and_order(prices):
    app=create_app(prices.root,workspace=prices)
    with TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',1)) as http:
        url='/api/v2/neutral'
        response=http.get(url,params={'sort':'sales','order':'asc','min_sales':0,'max_sales':15})
        assert response.status_code==200
        assert ids(response.json())==[2593,2589,2590]
        assert ids(http.get(url,params={'price_basis':'sale','min_price_copper':150,'max_roi':300}).json())==[2591,2592]
        for bad in ({'min_sales':2,'max_sales':1},{'min_price_copper':20,'max_price_copper':10},
                    {'min_roi':50,'max_roi':10},{'max_sales':-1},{'min_sales':'NaN'},
                    {'price_basis':'unknown'},{'order':'unknown'}):
            assert http.get(url,params=bad).status_code==422,bad

# EOF
