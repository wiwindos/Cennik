"""Browser QA on an isolated synthetic workspace; never reads existing databases."""
import argparse
import subprocess
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from cennik.workspace.models import MARKET
from cennik.workspace.web import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True)
    parser.add_argument("--playwright", required=True)
    parser.add_argument("--browser", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--focus", choices=["all", "sniper"], default="all")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cennik-next-ui-") as temporary:
        app = create_app(Path(temporary) / "fresh")
        workspace = app.state.workspace
        workspace.market.import_scan({"format_version": 1, "scan_id": "ui-synthetic",
            "market": MARKET, "observed_at": int(datetime.now(UTC).timestamp())-10,
            "full": True, "count": 120, "data": ";".join(["2589:4:40:0:4:1:2"]*120),
            "variants": [{"link": "item:2589:0:0:0", "name": "UI fixture: Linen Cloth",
                "divisible": True, "vendor_sell": 13}]})
        workspace.calculate()
        # Isolated catalogue fixture: direct cloth reagent + NPC thread, no network.
        workspace.sniper._catalog = {"workspace_id": workspace.identity, "generation": workspace.market.generation(),
            "items": [{"item_id": 2589, "name": "Fixture result"},
                {"item_id": 2592, "name": "Fixture cloth"}, {"item_id": 2321, "name": "Fixture thread", "vendor_buy_copper": 50}],
            "recipes": [{"item_id": 2589, "spell_id": 100, "version_hash": "fixture-version", "profession": "Tailoring",
                "qty_min": 1, "qty_max": 1, "skill_required": 10,
                "materials": [{"item_id": 2592, "quantity": 3}, {"item_id": 2321, "quantity": 1}]}]}
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8767,
            proxy_headers=False, log_level="warning"))
        thread = threading.Thread(target=server.run)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(.1)
            subprocess.run([args.node, str(Path(__file__).with_suffix(".cjs")), args.playwright,
                args.browser, str(args.output), args.focus], check=True, timeout=120)
        finally:
            server.should_exit = True
            thread.join(timeout=30)


if __name__ == "__main__":
    main()
