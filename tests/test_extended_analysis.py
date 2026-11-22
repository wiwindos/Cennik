import json
import time
import threading
from contextlib import nullcontext
from datetime import datetime,UTC

import pytest
from fastapi.testclient import TestClient
from test_workspace import workspace,scan
from test_disenchant import Provider
from test_result_retention import run as make_run,opportunity

from cennik.core.models import AnalyticsConfig,AnalyticsSettings,Run
from cennik.core.calculation import config_key,require_current
from cennik.workspace.extended import ExtendedRequest
from cennik.workspace.web import create_app

def seed(w):
    config=AnalyticsConfig(enabled_strategies=['vendor_arbitrage','craft_graph','craft_disenchant'],
        max_price_age_seconds=31536000,craft_liquidity_enabled=False,craft_sales_window_days=0,craft_min_sales_days=0)
    config=w.analytics.registry.normalize_config(config)
    w.store.save_settings(AnalyticsSettings(active_market=w.market_key,config=config))
    r=make_run('automatic',w.market_key);r.config=config;r.generation=w.analytics.generation()
    r.strategy_versions={s.id:s.version for s in w.analytics.registry.selected(config.enabled_strategies)}
    r.diagnostics={'calculation_scope':'light'}
    item=opportunity(r,'light-row').model_copy(update={'strategy_id':'vendor_arbitrage','strategy_version':r.strategy_versions['vendor_arbitrage']})
    w.store.begin(r,'seed');w.store.finish(r,[item]);return r

def wait(w):
    w.extended.thread.join(20)
    assert not w.extended.thread.is_alive()
    return w.extended.status()

def test_automatic_run_never_calls_heavy_strategies(workspace,monkeypatch):
    seed(workspace);workspace.market.import_scan(scan())
    for key in ('craft_graph','craft_disenchant'):
        monkeypatch.setattr(workspace.analytics.registry.strategies[key],'evaluate',lambda _:pytest.fail('Heavy strategy was started automatically'))
    result=workspace.calculate()
    assert result['run']['status']=='completed'
    assert result['run']['diagnostics']['executed_strategies']==['vendor_arbitrage']

def test_child_process_publishes_heavy_and_keeps_light_then_marks_stale(workspace,monkeypatch):
    seed(workspace);monkeypatch.setattr(workspace,'provider',lambda *a,**k:nullcontext(Provider()))
    state=workspace.extended.start(ExtendedRequest())
    assert state['active'] or state['state']=='completed'
    with pytest.raises(RuntimeError):workspace.extended.start(ExtendedRequest())
    done=wait(workspace);assert done['state']=='completed',done
    assert done['count']>0 and done['published'] and not done['stale']
    rows=workspace.store.listing(workspace.market_key)['items']
    assert {r['strategy_id'] for r in rows}>={'vendor_arbitrage','craft_disenchant'}
    heavy=next(r for r in rows if r['strategy_id']=='craft_disenchant')
    assert not heavy['details']['extended_stale']
    assert (heavy['required_capital_copper'],heavy['quantity'],heavy['reference_price_copper'],heavy['potential_profit_copper'])==(65,2,360,277)
    assert 0<done['peak_memory_bytes']<workspace.extended.memory_limit
    workspace.market.import_scan(scan())
    monkeypatch.undo()
    workspace.calculate()
    rows=workspace.store.listing(workspace.market_key)['items']
    heavy=next(r for r in rows if r['strategy_id']=='craft_disenchant')
    assert heavy['details']['extended_stale'] and workspace.extended.status()['stale']
    from cennik.core.models import Opportunity
    with pytest.raises(ValueError,match='устарела'):
        require_current(Opportunity.model_validate(heavy),workspace.analytics.generation(),workspace.effective_config())

def test_memory_limit_keeps_previous_publication(workspace,monkeypatch):
    seed(workspace);monkeypatch.setattr(workspace,'provider',lambda *a,**k:nullcontext(Provider()))
    monkeypatch.setattr(workspace.extended,'rss',lambda _:workspace.extended.memory_limit+1)
    workspace.extended.start(ExtendedRequest())
    done=wait(workspace)
    assert done['state']=='failed' and 'памяти' in done['message']
    assert workspace.store.latest(workspace.market_key,published=True).run_id=='automatic'
    assert not (workspace.extended.root/'input.json').exists()

def test_empty_selection_does_not_remove_automatic_results(workspace,monkeypatch):
    seed(workspace);monkeypatch.setattr(workspace,'provider',lambda *a,**k:nullcontext(Provider()))
    workspace.extended.start(ExtendedRequest(profession='Tailoring',item_ids=[999999]))
    done=wait(workspace)
    assert done['state']=='completed' and done['count']==0
    assert workspace.store.listing(workspace.market_key)['total']==1
    assert done['published']['selection']['item_ids']==[999999]

def test_ingestion_queue_is_available_during_preparation(workspace,monkeypatch):
    seed(workspace);entered=threading.Event();release=threading.Event()
    original=workspace.extended.snapshot
    def prepare(request):entered.set();release.wait(5);return original(request)
    monkeypatch.setattr(workspace.extended,'snapshot',prepare)
    monkeypatch.setattr(workspace,'provider',lambda *a,**k:nullcontext(Provider()))
    workspace.extended.start(ExtendedRequest());assert entered.wait(2)
    try:
        delivered=threading.Event();workspace.submit('remote_exchange',lambda:delivered.set())
        assert delivered.wait(1),'Heavy preparation blocked the ingestion executor'
    finally:release.set()
    assert wait(workspace)['state']=='completed'

def test_api_start_validation_and_status(workspace):
    app=create_app(workspace.root,workspace=workspace)
    with TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',1)) as http:
        assert http.get('/api/v2/extended-analysis').json()['state']=='idle'
        assert http.post('/api/v2/extended-analysis',json={'item_ids':[-1]}).status_code==422
        assert http.post('/api/v2/extended-analysis',json={'item_ids':list(range(101))}).status_code==422


def test_history_batch_and_scan_ack_only_schedule_one_light_calculation(workspace,monkeypatch):
    workspace.save_settings(workspace.settings().model_copy(update={'auto_recalculate':True}))
    workspace.recalculate_pending.clear()
    monkeypatch.setattr(workspace,'calculate',lambda:pytest.fail('Inline calculation delayed the acknowledgement'))
    calls=[]
    monkeypatch.setattr(workspace,'request',lambda spec:(calls.append(spec.item_id) or {'changed':True}))
    monkeypatch.setattr(workspace.sniper,'listing',lambda **k:{'rules':[{'item_id':12364},{'item_id':2589}]})
    monkeypatch.setattr(workspace.exchange,'packages',lambda _:[])
    workspace.source_updates.run('history')
    assert calls==[2589,12364] and workspace.recalculate_pending.is_set()
    assert workspace.future is None
    result=workspace.import_scans([scan()])
    assert result[0]['complete'] and workspace.recalculate_pending.is_set()
    assert workspace.future is None


def test_concurrent_publications_keep_latest_light_and_extended(workspace,monkeypatch):
    from contextlib import contextmanager
    from cennik.core.models import Opportunity
    old=seed(workspace);store=workspace.store
    light=old.model_copy(deep=True,update={'run_id':'next-light','generation':'new-data','diagnostics':{'calculation_scope':'light'}})
    heavy=old.model_copy(deep=True,update={'run_id':'next-extended','generation':'new-data','diagnostics':{'calculation_scope':'extended'}})
    evidence={'generation':'new-data','config':config_key(old.config),'computed_at':old.as_of.isoformat(),'selection':{}}
    heavy.diagnostics['extended_analysis']=evidence
    one=opportunity(light,'new-light-row').model_copy(update={'strategy_id':'vendor_arbitrage','strategy_version':old.strategy_versions['vendor_arbitrage'],'potential_profit_copper':20})
    two=opportunity(heavy,'new-heavy-row').model_copy(update={'strategy_id':'craft_disenchant','strategy_version':old.strategy_versions['craft_disenchant'],'details':{'extended_analysis':evidence}})
    store.begin(light,'next-light');store.begin(heavy,'next-heavy')
    entered=threading.Event();release=threading.Event();original=store.connection;errors=[]
    @contextmanager
    def connection():
        with original() as db:
            def trace(sql):
                if threading.current_thread().name=='light-publish' and sql.startswith('SELECT coalesce(r.summary'):
                    entered.set();release.wait(5)
            db.set_trace_callback(trace);yield db
    monkeypatch.setattr(store,'connection',connection)
    def finish(run,rows):
        try:store.finish(run,rows)
        except Exception as error:errors.append(error)
    a=threading.Thread(target=finish,args=(light,[one]),name='light-publish');b=threading.Thread(target=finish,args=(heavy,[two]))
    a.start();assert entered.wait(2);b.start()
    try:time.sleep(.1);assert b.is_alive()
    finally:release.set();a.join(4);b.join(4)
    assert not errors
    rows=store.listing(workspace.market_key)['items']
    assert {o['strategy_id'] for o in rows}=={'vendor_arbitrage','craft_disenchant'}
    assert next(o for o in rows if o['strategy_id']=='vendor_arbitrage')['potential_profit_copper']==20
    assert not next(o for o in rows if o['strategy_id']=='craft_disenchant')['details']['extended_stale']

# EOF
