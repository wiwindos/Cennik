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
    parser.add_argument("--focus", choices=["all", "sniper", "disenchant", "npc", "npc-catalog", "npc-legacy"], default="all")
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
        if args.focus == "disenchant":
            from ahledger.models import JobSpec
            from ahledger.parsers import parse
            from cennik.core.models import AnalyticsSettings

            spec = JobSpec(dataset="pricetable", url="https://fixture.invalid/prices", market=MARKET)
            stamp = int(datetime.now(UTC).timestamp()) - 5
            body = (f"AHL1|forever/pvp/alliance/us|{stamp}|4\n"
                    "4239:50:50:100:50:50:50:50:1000\n"
                    "10940:100:100:100:100:100:100:100:1000\n"
                    "10938:200:200:100:200:200:200:200:1000\n"
                    "2589:10:10:100:10:10:10:10:1000\n").encode()
            workspace.market.import_api(spec, parse(spec, body), body, {})
            settings = workspace.store.settings()
            settings.config.craft_liquidity_enabled = False
            workspace.analytics.update_settings(AnalyticsSettings(active_market=MARKET, config=settings.config))
        if args.focus in {"npc", "npc-catalog", "npc-legacy"}:
            from ahledger.models import JobSpec
            from ahledger.parsers import parse
            from cennik.workspace.models import ItemFacts

            if args.focus == "npc":
                workspace.update_facts(2589, ItemFacts(vendor_buy_copper=20))
            spec = JobSpec(dataset="pricetable", url="https://fixture.invalid/prices", market=MARKET)
            stamp = int(datetime.now(UTC).timestamp()) - 5
            body = f"AHL1|forever/pvp/alliance/us|{stamp}|1\n2589:101:101:100:101:101:101:101:2000\n".encode()
            workspace.market.import_api(spec, parse(spec, body), body, {})
        if args.focus == "npc-legacy":
            settings = workspace.store.settings()
            settings.config.enabled_strategies.remove("npc_to_auction")
            workspace.analytics.update_settings(settings)
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
            script = Path(__file__).with_name("check_disenchant_ui.cjs") if args.focus == "disenchant" else Path(__file__).with_suffix(".cjs")
            if args.focus in {"npc", "npc-catalog", "npc-legacy"}:
                script = Path(__file__).with_name("check_npc_ui.cjs")
            subprocess.run([args.node, str(script), args.playwright,
                args.browser, str(args.output), args.focus], check=True, timeout=120)
        finally:
            server.should_exit = True
            thread.join(timeout=30)


if __name__ == "__main__":
    main()
