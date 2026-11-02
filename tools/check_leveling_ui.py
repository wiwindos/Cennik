"""Isolated real HTTP/UI/task pipeline with invented profession observations."""
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
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("node", "playwright", "browser"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from test_bulk import opportunity
    from test_production import production_setup
    from test_profession_leveling import Provider, snapshot

    with tempfile.TemporaryDirectory(prefix="cennik-leveling-ui-") as temporary:
        root = Path(temporary)
        app = create_app(root / "profile")
        w = app.state.workspace
        w.save_settings(w.settings().model_copy(update={"auto_import": False, "auto_refresh": False,
                                                       "auto_recalculate": False, "auto_fyi": False}))
        clients, _, _, _ = production_setup(w, root, create=False)
        w.store.listing = lambda *a, **kw: {"items": [opportunity().model_dump(mode="json")], "total": 1, "limit": 30, "offset": 0}
        provider = Provider(w.market_key)
        w.provider = lambda *a, **kw: provider
        w.leveling.ingest(snapshot(), clients[1])

        @app.get("/fixture/tasks")
        def tasks():
            packages = [p.model_dump(mode="json") for p in w.exchange.packages(w.market_key)]
            return {"crafter": w.production.route_packages(clients[1], packages),
                    "auctioneer": w.production.route_packages(clients[0], packages),
                    "packages": len(packages), "jobs": w.jobs(), "orders": w.production.status()}

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(.05)
            subprocess.run([args.node, str(Path(__file__).with_suffix(".cjs")), args.playwright,
                            args.browser, str(args.output), f"http://127.0.0.1:{port}"], check=True, timeout=120)
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            w.close()


if __name__ == "__main__":
    main()
