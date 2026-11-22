"""Check profession filters against a real, isolated workspace API."""
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
    for name in ('node','playwright','browser'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
    from auction_fixture import approve_workspace
    from test_profession_filter import seed

    with tempfile.TemporaryDirectory(prefix='cennik-profession-ui-') as folder:
        app=create_app(Path(folder))
        workspace=app.state.workspace
        approve_workspace(workspace)
        workspace.save_settings(workspace.settings().model_copy(update={
            'auto_import':False,'auto_recalculate':False,'auto_refresh':False,
            'auto_fyi':False,'auto_references':False,'auto_history':False,'auto_disenchant_check':False}))
        seed(workspace)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0))
            port=probe.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning'))
        thread=threading.Thread(target=server.run)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(.05)
            subprocess.run([args.node,str(Path(__file__).with_suffix('.cjs')),args.playwright,args.browser,
                            str(args.output.resolve()),f'http://127.0.0.1:{port}'],check=True,timeout=120)
        finally:
            server.should_exit=True
            thread.join(timeout=15)
            workspace.close()


if __name__=='__main__':
    main()
