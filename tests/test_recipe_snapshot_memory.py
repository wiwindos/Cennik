from test_cennik import analytics_root,NOW
from cennik.data.duckdb import DuckDBProvider
import duckdb

def test_hourly_snapshots_do_not_materialize_old_mechanics_in_python(analytics_root,monkeypatch):
    path=analytics_root/'analytics/ahledger.duckdb'
    with duckdb.connect(str(path)) as db:
        db.execute("""INSERT INTO dataset_metadata(dataset,market,raw_id,status,retrieved_at,params_json,payload_json)
            SELECT 'crafting','market-a','hour-'||n,'COMPLETE',CAST(? AS TIMESTAMPTZ)-n*INTERVAL 1 MINUTE,
            '{"profession":"Tailoring","all":"1"}','{}' FROM range(2000) t(n)""",[NOW])
        db.execute("""INSERT INTO recipe_sources(spell_id,version_hash,market,raw_id,retrieved_at)
            SELECT 100,'v1','market-a','hour-'||n,CAST(? AS TIMESTAMPTZ)-n*INTERVAL 1 MINUTE
            FROM range(2000) t(n)""",[NOW])
    with DuckDBProvider(path,'market-a',NOW) as provider:
        original=provider.rows;wide=[]
        def rows(sql,params=None):
            result=original(sql,params)
            if 'r.payload_json' in sql:wide.append(len(result))
            return result
        monkeypatch.setattr(provider,'rows',rows)
        recipes=provider.recipes()
        assert len(recipes)==1 and recipes[0].raw_id=='hour-0'
        assert len(recipes[0].inputs)==2
        assert provider.recipe_selection['outside_latest_snapshot']==1999
        assert wide==[1], 'Old snapshot mechanics must stay outside Python allocations'

# EOF
