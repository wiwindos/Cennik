from datetime import UTC, datetime

from fastapi.testclient import TestClient
from test_workspace import workspace, scan

from cennik.workspace.data import WorkspaceProvider
from cennik.workspace.web import create_app


def test_status_market_dates_match_history_without_window_sort(workspace,monkeypatch):
    workspace.market.import_scan(scan())
    at=datetime.now(UTC)
    with WorkspaceProvider(workspace.market,workspace.market_key,at) as provider:
        from cennik.data.duckdb import DuckDBProvider
        expected=DuckDBProvider.markets(provider)
        statements=[]
        real=provider.rows
        monkeypatch.setattr(provider,'rows',lambda sql,*args:(statements.append(sql),real(sql,*args))[1])
        assert provider.markets()==expected
        assert 'price_history_ranges' in statements[0] and 'UNION ALL SELECT market,observed_at FROM price_history\n' not in statements[0]


def test_game_names_cache_refreshes_after_revision_change(workspace):
    names=workspace.market.names
    names.put([('item',2589,'First')],'enUS',stamp=1)
    first=names.document()
    assert names.document()['names'] is first['names']
    names.put([('item',2589,'Changed')],'enUS',stamp=2)
    changed=names.document()
    assert changed['revision']!=first['revision']
    assert changed['names']['item']['2589']['enUS']=='Changed'
    assert names.document(changed['revision'])=={'revision':changed['revision'],'unchanged':True}


def test_large_json_responses_are_compressed(workspace):
    workspace.market.names.put([('item',i,'Test name '+str(i)) for i in range(1000,2000)],'enUS',stamp=1)
    app=create_app(workspace.root,workspace=workspace)
    with TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',12345)) as http:
        response=http.get('/api/v2/game-names',headers={'Accept-Encoding':'gzip'})
        assert response.status_code==200 and response.headers['content-encoding']=='gzip'
        assert response.json()['names']['item']['1000']['enUS']=='Test name 1000'
        assert len(response.content)>int(response.headers['content-length'])*2

# EOF
