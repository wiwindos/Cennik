"""Browser QA of source reports on invented responses, without upstream requests."""
import argparse
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn

from cennik.engines.npc_catalog import NpcCatalogItem
from cennik.workspace.web import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("node", "playwright", "browser"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from test_source_reports import prices, spec

    with tempfile.TemporaryDirectory(prefix="cennik-source-reports-ui-") as temporary:
        app = create_app(Path(temporary) / "profile")
        w = app.state.workspace
        w.save_settings(w.settings().model_copy(update={"auto_import": False, "auto_refresh": False,
                                                       "auto_recalculate": False, "auto_fyi": False}))
        request = spec()
        w.source_reports.applied(request, prices([(1, 10), (2, 20)]))
        w.source_reports.applied(request, prices([(1, 11), (3, 30)]))
        w.source_reports.checked(request)
        previous = w.npc_catalog.snapshot()
        updated = previous.model_copy(update={"version": "fixture-source-report", "items": {
            **previous.items, 9999999: NpcCatalogItem(name="Fixture NPC item", buy_copper=100, sell_copper=20)}})
        w.npc_catalog.install(updated)
        w.source_reports.fyi(previous, updated)

        @app.post("/fixture/source-error")
        def error():
            w.source_reports.failed(request, message="Тестовая ошибка AHL; прежние данные сохранены")
            return {"ok": True}

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
