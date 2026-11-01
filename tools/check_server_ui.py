"""Real Chrome login, market configuration and every full-server web section."""
import argparse
import subprocess
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from cennik.server.app import create_server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("node", "playwright", "browser"):
        parser.add_argument("--"+key, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    app = create_server(args.output / "server", "ui-fixture-"+"x"*40, public_origin="http://127.0.0.1:8772")
    def upstream(request):
        if request.url.path.endswith('/markets'):
            return httpx.Response(200,json={'markets':[{'id':'forever.pvp.alliance.eu','label':'PvP Alliance EU'}, {'id':'forever.normal.horde.us','label':'Normal Horde US'}]})
        if '/pricetable/' in request.url.path:
            return httpx.Response(200,text='AHL1|forever/pvp/alliance/eu|'+str(int(time.time()))+'|0\n')
        return httpx.Response(200,json={'recipes':[]})
    app.state.catalog.sources.transport=httpx.MockTransport(upstream)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8772, log_level="warning", proxy_headers=False))
    thread = threading.Thread(target=server.run)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        assert server.started
        subprocess.run([args.node, str(Path(__file__).with_suffix('.cjs')), args.playwright, args.browser, str(args.output.resolve())], check=True, timeout=180)
    finally:
        server.should_exit = True
        thread.join(timeout=30)


if __name__ == "__main__":
    main()
