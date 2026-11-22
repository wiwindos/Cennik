import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from cennik.addon.service import AddonExchange
from cennik.core.diagnostics import summarize_run
from cennik.core.models import AnalyticsConfig, Opportunity, Run
from cennik.storage.compact import rebuild_database, table_digest
from cennik.storage.sqlite import Store


def run(identity,market='market-a',status='completed'):
    return Run(run_id=identity,market=market,started_at=datetime.now(UTC),as_of=datetime.now(UTC),
        generation='test',config=AnalyticsConfig(),strategy_versions={'test':'1'},status=status,
        diagnostics={'quality':{'metadata':[{'dataset':'fixture','status':'COMPLETE','padding':'large '*1000}]}})


def opportunity(run,identity):
    return Opportunity(id=identity,run_id=run.run_id,strategy_id='test',strategy_version='1',market=run.market,
        item_id=1,item_name='Fixture',action='test',observed_at=run.as_of,generated_at=run.as_of,
        buy_price_copper=1,reference_price_copper=10,quantity=1,required_capital_copper=1,
        potential_profit_copper=9,roi_percent=900,confidence='limited',risk_flags=[],reason='test',source='fixture',
        data_age_seconds=0,costs_included=[],costs_excluded=[],details={'original':'large '*100})


def finish(store,identity,market='market-a'):
    value=run(identity,market)
    store.begin(value,'fingerprint-'+identity)
    store.finish(value,[opportunity(value,identity+'-op')])
    return value


def test_only_current_result_remains_and_old_cache_cannot_be_republished(tmp_path):
    store=Store(tmp_path/'state.sqlite')
    old=finish(store,'old')
    finish(store,'current')
    assert store.listing('market-a')['total']==1
    assert store.cached('fingerprint-old') is None
    assert store.cached('fingerprint-current').run_id=='current'
    with pytest.raises(ValueError,match='уже снят'):
        store.publish_cached(old)
    with store.connection() as db:
        assert db.execute('SELECT id FROM opportunities').fetchone()[0]=='current-op'
        row=db.execute("SELECT payload,results_retained FROM runs WHERE run_id='old'").fetchone()
        assert row['results_retained']==0
        assert json.loads(row['payload'])['diagnostics']['summary_only']
        assert len(row['payload'])<len(old.model_dump_json())


def test_publications_of_other_markets_and_plan_references_are_preserved(tmp_path):
    store=Store(tmp_path/'state.sqlite')
    old=finish(store,'old');finish(store,'other','market-b')
    with store.connection() as db:
        db.execute('CREATE TABLE workspace_plans(id TEXT PRIMARY KEY,payload TEXT)')
        db.execute('INSERT INTO workspace_plans VALUES(?,?)',['plan',json.dumps({'opportunity':{'id':'old-op'}})])
    finish(store,'current')
    with store.connection() as db:
        assert {r[0] for r in db.execute('SELECT id FROM opportunities')}=={'old-op','other-op','current-op'}
        assert db.execute("SELECT results_retained FROM runs WHERE run_id='old'").fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM workspace_plans').fetchone()[0]==1


def test_task_and_original_bulk_opportunity_are_pinned(tmp_path):
    store=Store(tmp_path/'state.sqlite');old=finish(store,'old')
    AddonExchange(SimpleNamespace(store=store))
    with store.connection() as db:
        db.execute('INSERT INTO addon_packages VALUES(?,?,?,?,?,?,?)',['p','request','hash','market-a','old','future','{}'])
        payload=json.dumps({'opportunity':{'details':{'original_opportunity_id':'old-op'}}})
        db.execute('INSERT INTO addon_tasks VALUES(?,?,?,?,?,?)',['t','derived-op','old','market-a','p',payload])
    finish(store,'current')
    with store.connection() as db:
        assert {r[0] for r in db.execute('SELECT id FROM opportunities')}=={'old-op','current-op'}
        assert db.execute('SELECT count(*) FROM addon_tasks').fetchone()[0]==1


def test_failed_run_does_not_remove_last_published_opportunities(tmp_path):
    store=Store(tmp_path/'state.sqlite');finish(store,'good')
    failed=run('failed',status='failed');failed.error='test failure'
    store.begin(failed,'bad');store.finish(failed,[])
    assert store.latest('market-a',published=True).run_id=='good'
    assert store.listing('market-a')['items'][0]['id']=='good-op'
    assert store.latest('market-a').error=='test failure'


def legacy_database(path):
    store=Store(path);finish(store,'current')
    with store.connection() as db:
        for i in range(6):
            value=run('legacy-'+str(i));summary=summarize_run(value).model_dump_json()
            db.execute('INSERT INTO runs(run_id,market,fingerprint,status,started_at,payload,summary) VALUES(?,?,?,?,?,?,?)',
                [value.run_id,value.market,'legacy-cache','completed',value.started_at.isoformat(),value.model_dump_json(),summary])
            for j in range(3):
                o=opportunity(value,value.run_id+'-'+str(j))
                db.execute('INSERT INTO opportunities VALUES(?,?,?,?,?,?,?,?,?,?)',
                    [o.id,o.run_id,o.market,o.strategy_id,o.potential_profit_copper,float(o.roi_percent),float(o.rating),0,'limited',o.model_dump_json()])
        db.execute('CREATE TABLE workspace_plans(id TEXT PRIMARY KEY,payload TEXT)')
        db.execute('INSERT INTO workspace_plans VALUES(?,?)',['p',json.dumps({'opportunity_id':'legacy-0-0'})])
        db.execute('CREATE TABLE accounting_fixture(id TEXT PRIMARY KEY,payload BLOB)')
        db.execute('INSERT INTO accounting_fixture VALUES(?,?)',['purchase',b'\x00\xff exact accounting'])
        db.execute('ALTER TABLE runs DROP COLUMN results_retained')
    with store.connection() as db:
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    return store


def test_offline_rebuild_preserves_every_other_table_and_source_bytes(tmp_path):
    source=tmp_path/'state.sqlite';store=legacy_database(source)
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    target=tmp_path/'compact.sqlite';report=rebuild_database(source,target)
    assert report['state']=='verified'
    assert report['runs_copied']==7 and report['opportunities_kept']==2
    assert report['target_bytes']<report['source_bytes']
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before
    rebuilt=Store(target)
    assert rebuilt.cached('legacy-cache') is None
    assert rebuilt.listing('market-a')['total']==1
    with rebuilt.connection() as db:
        assert db.execute('SELECT payload FROM accounting_fixture').fetchone()[0]==b'\x00\xff exact accounting'
        assert db.execute('SELECT count(*) FROM runs').fetchone()[0]==7
        assert {r[0] for r in db.execute('SELECT id FROM opportunities')}=={'current-op','legacy-0-0'}
        assert [r[0] for r in db.execute('PRAGMA integrity_check')]==['ok']


def test_failed_compaction_never_replaces_or_changes_original(tmp_path,monkeypatch):
    import cennik.storage.compact as module
    source=tmp_path/'state.sqlite';legacy_database(source)
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    real=module.table_digest
    def mismatch(db,schema,table):
        result=real(db,schema,table)
        return {**result,'sha256':'invalid'} if schema=='main' else result
    monkeypatch.setattr(module,'table_digest',mismatch)
    with pytest.raises(RuntimeError,match='Не совпали'):
        rebuild_database(source,tmp_path/'failed.sqlite')
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before


def test_compaction_validation_is_confined_to_new_database(tmp_path,monkeypatch):
    import cennik.storage.compact as module
    source=tmp_path/'state.sqlite';legacy_database(source)
    connect=sqlite3.connect
    def guarded_connect(*args,**kwargs):
        db=connect(*args,**kwargs)
        def authorize(action,arg1,arg2,database,trigger):
            if action==sqlite3.SQLITE_PRAGMA and arg1 in {'integrity_check','foreign_key_check'} and database!='main':
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        db.set_authorizer(authorize)
        return db
    monkeypatch.setattr(module.sqlite3,'connect',guarded_connect)
    assert rebuild_database(source,tmp_path/'new.sqlite')['state']=='verified'

# EOF
