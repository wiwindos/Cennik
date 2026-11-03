"""Synthetic full-scan import + all enabled analytics, in a NEW isolated profile."""
import argparse
import json
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ahledger.util import atomic_json
from cennik.collector.auctioneers import (
    AuctioneerProposal,
    AuctioneerRegistry,
    Review,
    validate_snapshot,
)
from cennik.workspace.service import Workspace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--lots", type=int, nargs="+", default=[10000, 100000])
    parser.add_argument("--seed", type=Path, help="Use actual beta items, recipes, NPC rules and prices")
    parser.add_argument("--rss", action="store_true", help="Linux process RSS instead of Python tracemalloc")
    args = parser.parse_args()
    if args.root.exists() and any(args.root.iterdir()):
        parser.error("Нужна новая пустая папка, не рабочий профиль")
    if any(n < 1 or n > 2_000_000 for n in args.lots):
        parser.error("Допустимо от 1 до 2 000 000 лотов")
    results = []
    baseline = npc = rules = None
    if args.seed:
        from cennik.server.prepare import validate_package
        _, baseline, npc, rules = validate_package(args.seed)
    for n in args.lots:
        identity = uuid4().hex
        market = f"forever.{identity}.alliance.eu"
        root = args.root / str(n)
        root.mkdir(parents=True)
        registry = AuctioneerRegistry(root / "benchmark-registry")
        proposal = AuctioneerProposal(server_id=identity, market=market, npc_id=1, npc_name="Synthetic", realm="Synthetic",
            map_id=1, x=50, y=50, proposed_type="ordinary", observed_at=int(time.time()))
        registry.suggest("a"*64, proposal.model_dump(mode="json"), "synthetic")
        registry.review("a"*64, Review(action="approve", auction_type="ordinary"))
        snapshot = validate_snapshot(registry.wire())
        atomic_json(root / "collectors/auctioneer_registry.json", snapshot)
        entry = snapshot["entries"][0]
        scan = {"format_version": 1, "market": market, "realm": "Synthetic", "observed_at": int(time.time()), "full": True,
            "count": n, "data": ";".join(f"{1000+i%1000}:1:1:0:4:1" for i in range(n)),
            "variants": [{"name": "Synthetic", "divisible": True, "vendor_sell": 100}],
            "auction_context": {"scope_id": entry["scope_id"], "server_id": identity, "revision": 1, "auction_type": "ordinary",
                "npc_id": 1, "realm": "Synthetic", "map_id": 1, "x": 50, "y": 50, "session_id": "synthetic"}}
        w = Workspace(root, market_key=market, server_mode=True)
        if baseline:
            atomic_json(w.bootstrap.path, baseline.model_dump(mode="json"))
            w.npc_catalog.install(npc)
            w.analytics.disenchant.install(rules)
            w.save_settings(w.settings().model_copy(update={"own_data_only": True}))
            prices = list(baseline.prices.items())
            patterns, variants = [], []
            for item_id, price in prices:
                item = baseline.items.get(item_id)
                variants.append({"name": item.name if item else str(item_id), "divisible": True,
                    "vendor_sell": item.vendor_sell_copper if item else None})
                patterns.append(f"{item_id}:1:{price.min_copper}:0:4:{len(variants)}")
            scan["variants"] = variants
            scan["data"] = ";".join(patterns[i % len(patterns)] for i in range(n))
        if not args.rss:
            tracemalloc.start()
        try:
            started = time.perf_counter()
            imported = w.market.import_scan(scan)
            imported_at = time.perf_counter()
            run = w.calculate()
            ended = time.perf_counter()
            peak = None if args.rss else tracemalloc.get_traced_memory()[1]
            rss = None
            if args.rss:
                import resource
                rss = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2)
            row = {"lots": n, "complete": imported.get("complete"), "import_seconds": round(imported_at-started, 3),
                "analytics_seconds": round(ended-imported_at, 3), "python_heap_peak_mb": round(peak/1024/1024, 2) if peak is not None else None,
                "process_max_rss_mib": rss,
                "run_status": run.get("run", {}).get("status"),
                "opportunities": run.get("run", {}).get("signal_count"),
                "seed_version": baseline.version if baseline else None,
                "recipes": len(baseline.recipes) if baseline else 0,
                "at": datetime.now(UTC).isoformat()}
            results.append(row)
            print(json.dumps(row), flush=True)
        finally:
            if not args.rss:
                tracemalloc.stop()
            w.close()
    atomic_json(args.root / "results.json", results)


if __name__ == "__main__":
    main()
