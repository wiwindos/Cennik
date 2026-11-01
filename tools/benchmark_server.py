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
    args = parser.parse_args()
    if args.root.exists() and any(args.root.iterdir()):
        parser.error("Нужна новая пустая папка, не рабочий профиль")
    if any(n < 1 or n > 2_000_000 for n in args.lots):
        parser.error("Допустимо от 1 до 2 000 000 лотов")
    results = []
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
        tracemalloc.start()
        try:
            started = time.perf_counter()
            imported = w.market.import_scan(scan)
            imported_at = time.perf_counter()
            run = w.calculate()
            ended = time.perf_counter()
            _, peak = tracemalloc.get_traced_memory()
            row = {"lots": n, "complete": imported.get("complete"), "import_seconds": round(imported_at-started, 3),
                "analytics_seconds": round(ended-imported_at, 3), "python_heap_peak_mb": round(peak/1024/1024, 2),
                "run_status": run.get("run", {}).get("status"), "at": datetime.now(UTC).isoformat()}
            results.append(row)
            print(json.dumps(row), flush=True)
        finally:
            tracemalloc.stop()
            w.close()
    atomic_json(args.root / "results.json", results)


if __name__ == "__main__":
    main()
