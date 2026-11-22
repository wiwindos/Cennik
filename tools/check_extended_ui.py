"""Real browser/child-process checks on an isolated, gated test workspace."""
import argparse,socket,subprocess,sys,tempfile,threading,time
from contextlib import nullcontext
from pathlib import Path
import uvicorn
from cennik.workspace.web import create_app

def main():
    p=argparse.ArgumentParser()
    for key in ('node','playwright','browser'):p.add_argument('--'+key,required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
    from auction_fixture import approve_workspace
    from test_extended_analysis import seed,Provider
    from test_workspace import scan
    with tempfile.TemporaryDirectory(prefix='cennik-extended-ui-') as directory:
        app=create_app(Path(directory));w=app.state.workspace;approve_workspace(w)
        w.save_settings(w.settings().model_copy(update={k:False for k in ('auto_import','auto_recalculate','auto_refresh','auto_fyi','auto_references','auto_history','auto_disenchant_check')}))
        w.market.import_scan(scan())
        seed(w)
        def provider(*args,**kwargs):
            value=Provider();value.markets=lambda:[{'market':w.market_key,'observed_at':value.as_of,'label':'Fixture'}]
            return nullcontext(value)
        w.provider=provider
        release=threading.Event();original=w.extended.snapshot
        def gated(request):release.wait(25);return original(request)
        w.extended.snapshot=gated
        @app.post('/fixture/release')
        def unblock():release.set();return {'ok':True}
        @app.post('/fixture/deliver')
        def deliver():
            w.submit('remote_exchange',lambda:{'saved':True});w.future.result(timeout=2);return {'ok':True}
        @app.post('/fixture/change-prices')
        def change():
            w.market.db.execute('UPDATE workspace_revision SET revision=revision+1');w.calculate();return {'ok':True}
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning'))
        t=threading.Thread(target=server.run);t.start()
        try:
            for _ in range(100):
                if server.started:break
                time.sleep(.05)
            subprocess.run([a.node,str(Path(__file__).with_suffix('.cjs')),a.playwright,a.browser,str(a.output.resolve()),f'http://127.0.0.1:{port}'],check=True,timeout=120)
        finally:release.set();server.should_exit=True;t.join(20);w.close()

if __name__=='__main__':main()
# EOF
