"""Browser QA against an isolated real workspace; only invented recipes/scans."""

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
    parser = argparse.ArgumentParser()
    for name in ("node", "playwright", "browser"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from auction_fixture import approve_workspace
    from test_bulk import book, setup, submit
    from test_shared_graph_pipeline import graph_op
    from test_workspace import scan

    from cennik.workspace.bulk import BulkCreate

    with tempfile.TemporaryDirectory(prefix="cennik-shared-graph-ui-") as directory:
        root = Path(directory)
        app = create_app(root / "profile")
        workspace = app.state.workspace
        approve_workspace(workspace)
        workspace.save_settings(
            workspace.settings().model_copy(
                update={
                    "auto_import": False,
                    "auto_recalculate": False,
                    "auto_refresh": False,
                    "auto_fyi": False,
                }
            )
        )
        data = scan(count=1)
        data["data"] = "2589:3:30:0:3:1"
        assert workspace.market.import_scan(data)["complete"]
        value = graph_op()
        setup(workspace, root, value)
        workspace.store.listing = lambda *a, **kw: {
            "items": [value.model_dump(mode="json")],
            "total": 1,
            "limit": 30,
            "offset": 0,
        }
        identity = workspace.bulk.create(BulkCreate(opportunity_ids=[value.id]))["batch_id"]
        submit(
            workspace, identity, book(2589, ((10, 3),)), book(2996, ()), book(4238, ((100, 10),))
        )

        @app.get("/fixture/package")
        def package():
            return {
                "packages": [
                    p.model_dump(mode="json")
                    for p in workspace.exchange.packages(workspace.market_key)
                ]
            }

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        thread = threading.Thread(target=server.run)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.05)
            subprocess.run(
                [
                    args.node,
                    str(Path(__file__).with_suffix(".cjs")),
                    args.playwright,
                    args.browser,
                    str(args.output),
                    f"http://127.0.0.1:{port}",
                ],
                check=True,
                timeout=120,
            )
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            workspace.close()


if __name__ == "__main__":
    main()
