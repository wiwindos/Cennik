"""Offline before/after analysis at a fixed as_of; production DB is read only."""
import argparse
import json
import subprocess
import sys
import tempfile
import types
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from ahledger.util import atomic_json, digest, dumps, exclusive_lock
from cennik.core.models import AnalyticsConfig, Run
from cennik.data.duckdb import DuckDBProvider, fingerprint
from cennik.engines.prices import PriceEngine
from cennik.services.analytics import AnalyticsService
from cennik.storage.sqlite import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--strategies", nargs="+")
    parser.add_argument("--legacy", action="store_true", help="Use committed pre-optimization engines for comparison")
    args = parser.parse_args()
    root = args.root.resolve()
    baseline = json.loads(args.compare.read_text("utf-8")) if args.compare else None
    timings: dict = defaultdict(lambda: {"calls": 0, "seconds": 0.0})

    def timed(name, fn):
        def call(*a, **kw):
            start = perf_counter()
            try:
                return fn(*a, **kw)
            finally:
                timings[name]["calls"] += 1
                timings[name]["seconds"] += perf_counter() - start
        return call

    with exclusive_lock(root / "cennik"), exclusive_lock(root):
        source = root / "analytics/ahledger.duckdb"
        started = perf_counter()
        generation = fingerprint(source)
        hash_seconds = perf_counter() - started
        settings = Store(root / "cennik/cennik.sqlite").settings()
        market = baseline["market"] if baseline else settings.active_market
        with DuckDBProvider(source, market, datetime.now(UTC)) as provider:
            latest = next(m["observed_at"] for m in provider.markets() if m["market"] == market)
        as_of = datetime.fromisoformat(baseline["as_of"]) if baseline else latest + timedelta(seconds=1)
        config = AnalyticsConfig.model_validate(baseline["config"]) if baseline else settings.config
        if args.strategies:
            assert not baseline, "Comparison must keep baseline strategies"
            config = config.model_copy(update={"enabled_strategies": args.strategies})
        if baseline:
            assert generation == baseline["generation"], "Source DB changed; cannot compare"
        with tempfile.TemporaryDirectory(prefix="analytics-benchmark-") as temporary:
            service = AnalyticsService(Path(temporary))
            service.path = source
            run = Run(run_id="benchmark-fixed", market=market, started_at=as_of, as_of=as_of,
                      generation=generation, config=config,
                      strategy_versions={s.id: s.version for s in service.registry.selected(config.enabled_strategies)})
            service.store.begin(run, "isolated-benchmark")
            from contextlib import ExitStack
            with ExitStack() as stack:
                if args.legacy:
                    def module(name, path):
                        source = subprocess.run(["git", "show", "HEAD:" + path], check=True, capture_output=True).stdout.decode("utf-8")
                        value = types.ModuleType(name)
                        sys.modules[name] = value
                        exec(compile(source, path, "exec"), value.__dict__)
                        return value
                    old_prices = module("benchmark_old_prices", "src/cennik/engines/prices.py")
                    old_liquidity = module("benchmark_old_liquidity", "src/cennik/engines/liquidity.py")

                    class LegacyPrices(old_prices.PriceEngine):
                        def __init__(self, provider, config, measure=None):
                            super().__init__(provider, config)

                    stack.enter_context(patch("cennik.services.analytics.PriceEngine", LegacyPrices))
                    stack.enter_context(patch("cennik.strategies.craft.CraftLiquidity", old_liquidity.CraftLiquidity))
                    stack.enter_context(patch("cennik.strategies.craft_graph.CraftLiquidity", old_liquidity.CraftLiquidity))
                for name in ("current_prices", "historical_prices", "quality", "items", "recipes", "sales_history"):
                    stack.enter_context(patch.object(DuckDBProvider, name, timed(name, getattr(DuckDBProvider, name))))
                stack.enter_context(patch.object(PriceEngine, "flags", timed("flags", PriceEngine.flags)))
                for strategy in service.registry.selected(config.enabled_strategies):
                    stack.enter_context(patch.object(type(strategy), "evaluate", timed(strategy.id, strategy.evaluate.__func__)))
                started = perf_counter()
                service.calculate(run)
                seconds = perf_counter() - started
            assert run.status == "completed", run.error
            values = service.store.listing(market, limit=100000)["items"]
            diagnostics = {k: v for k, v in run.diagnostics.items() if k != "performance"}
            signature = digest(dumps({"items": values, "diagnostics": diagnostics}).encode())
            result = {"market": market, "generation": generation, "as_of": as_of.isoformat(),
                      "config": config.model_dump(mode="json"), "calculation_seconds": seconds,
                      "initial_hash_seconds": hash_seconds, "total_seconds": seconds + hash_seconds,
                      "legacy_engines": args.legacy,
                      "signal_count": len(values), "result_signature": signature, "timings": dict(timings),
                      "performance": run.diagnostics.get("performance")}
            if baseline:
                result["results_identical"] = signature == baseline["result_signature"]
                result["speedup"] = baseline["total_seconds"] / result["total_seconds"]
            atomic_json(args.output, result)
            print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
            if baseline:
                assert result["results_identical"], "Opportunities or diagnostics differ"


if __name__ == "__main__":
    main()
