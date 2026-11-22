import threading
import time

from fastapi.testclient import TestClient
from test_workspace import workspace
from test_vendor_bargains import load
from test_result_retention import run,opportunity

from cennik.workspace.read_views import ReadViews
from cennik.workspace.web import create_app


def ready(read):
    deadline=time.monotonic()+4
    while time.monotonic()<deadline:
        value=read()
        if value['view_cache']['available']:return value
        time.sleep(.01)
    raise AssertionError('Background view did not become ready')


def test_cold_view_never_waits_for_loader_and_survives_restart(tmp_path):
    entered=threading.Event();release=threading.Event();views=ReadViews(tmp_path,ttl=60)
    def load():entered.set();release.wait(4);return {'items':[1]}
    try:
        started=time.monotonic();value=views.get('key',load)
        assert time.monotonic()-started<.5 and not value['view_cache']['available']
        assert entered.wait(1)
        release.set();assert ready(lambda:views.get('key',load))['items']==[1]
    finally:release.set();views.close()
    restarted=ReadViews(tmp_path,ttl=60)
    try:
        def forbidden():raise AssertionError('A fresh persisted snapshot must be reused')
        value=restarted.get('key',forbidden)
        assert value['view_cache']['available'] and value['items']==[1]
    finally:restarted.close()


def test_failed_refresh_keeps_last_good_view_and_cache_is_bounded(tmp_path):
    views=ReadViews(tmp_path,limit=3,ttl=60)
    try:
        ready(lambda:views.get('good',lambda:{'items':[42]}))
        views.ttl=0;done=threading.Event()
        def fail():done.set();raise ValueError('fixture')
        assert views.get('good',fail)['items']==[42]
        assert done.wait(1)
        views.ttl=60
        for i in range(7):ready(lambda i=i:views.get(['other',i],lambda:{'items':[i]}))
        assert len(list(views.root.glob('*.json')))<=3 and len(views.entries)<=3
    finally:views.close()


def hold_market(workspace):
    held=threading.Event();release=threading.Event()
    def hold():
        with workspace.market.lock:held.set();release.wait(8)
    thread=threading.Thread(target=hold);thread.start();assert held.wait(1)
    return release,thread


def test_overview_settings_and_published_results_do_not_wait_for_market(workspace,monkeypatch):
    load(workspace)
    published=run('published',workspace.market_key);published.signal_count=1
    workspace.store.begin(published,'fixture');workspace.store.finish(published,[opportunity(published,'published-op')])
    app=create_app(workspace.root,workspace=workspace)
    monkeypatch.setattr(workspace,'development_report',lambda:(_ for _ in ()).throw(AssertionError('UI startup must not calculate development coverage')))
    with TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',12345)) as http:
        release,thread=hold_market(workspace)
        try:
            started=time.monotonic()
            settings=http.get('/api/v2/settings?view=true')
            overview=http.get('/api/v2/overview?view=true')
            opportunities=http.get('/api/v2/analytics/opportunities')
            assert time.monotonic()-started<1
            assert settings.status_code==overview.status_code==opportunities.status_code==200
            assert settings.json()['development'] is None
            assert overview.json()['analytics']['published_run']['run_id']=='published'
            assert opportunities.json()['total']==1
        finally:release.set();thread.join(2)


def test_warm_report_remains_available_during_market_write(workspace):
    load(workspace);app=create_app(workspace.root,workspace=workspace)
    with TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',12345)) as http:
        first=ready(lambda:http.get('/api/v2/vendor-bargains?view=true').json())
        assert first['total']==2
        workspace.read_views.ttl=0
        release,thread=hold_market(workspace)
        try:
            start=time.monotonic();response=http.get('/api/v2/vendor-bargains?view=true')
            assert time.monotonic()-start<.5
            data=response.json();assert data['total']==2 and data['view_cache']['stale']
            pending=http.get('/api/v2/vendor-bargains?view=true&q=unknown').json()
            assert not pending['view_cache']['available'] and 'total' not in pending
        finally:release.set();thread.join(2)

# EOF
