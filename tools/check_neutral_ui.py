"""Exercise the neutral research page against an isolated real workspace."""
import argparse
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from cennik.workspace.web import create_app


def main():
    parser=argparse.ArgumentParser()
    for key in ('node','playwright','browser'):
        parser.add_argument('--'+key,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--cold-lock',action='store_true')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
    from auction_fixture import approve_workspace
    from test_workspace import scan
    from test_neutral import neutral_scan
    with tempfile.TemporaryDirectory(prefix='cennik-neutral-ui-') as directory:
        app=create_app(Path(directory));w=app.state.workspace;approve_workspace(w)
        w.save_settings(w.settings().model_copy(update={k:False for k in (
            'auto_import','auto_recalculate','auto_refresh','auto_fyi','auto_references','auto_history','auto_disenchant_check')}))
        ordinary=scan();ordinary['variants'][0]['link']='item:2589:0:0:0:0:0:42'
        w.market.import_scan(ordinary)
        neutral_scan(w,data='2589:4:16:0:4:1;2589:4:24:0:4:1;2590:1:1:0:4:2',variants=[
            ordinary['variants'][0],{'link':'item:2590:0:0:0','name':'Нет цены продажи'}])
        from test_neutral_liquidity_performance import estimate
        estimate(w,value=2.5)
        @app.post('/fixture/lock-market')
        def lock_market():
            held=threading.Event()
            def hold():
                with w.market.lock:held.set();time.sleep(4)
            threading.Thread(target=hold,daemon=True).start();assert held.wait(1)
            return {'held':True}
        with socket.socket() as s:
            s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning'))
        thread=threading.Thread(target=server.run);thread.start()
        try:
            for _ in range(100):
                if server.started:break
                time.sleep(.05)
            if args.cold_lock:
                held=threading.Event()
                def cold_hold():
                    with w.market.lock:held.set();time.sleep(8)
                threading.Thread(target=cold_hold,daemon=True).start();assert held.wait(1)
            subprocess.run([args.node,str(Path(__file__).with_suffix('.cjs')),args.playwright,args.browser,
                str(args.output.resolve()),f'http://127.0.0.1:{port}'],check=True,timeout=120)
        finally:
            server.should_exit=True;thread.join(timeout=20);w.close()


if __name__=='__main__':main()
# EOF
