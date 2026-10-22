import threading
from unittest.mock import patch

from test_cennik import NOW, configured, context
from test_cennik import analytics_root as analytics_root
from test_craft_graph import add_intermediate

from cennik.core.models import AnalyticsConfig, AnalyticsSettings
from cennik.data.duckdb import DuckDBProvider
from cennik.engines.prices import PriceEngine
from cennik.strategies.craft import SimpleCraftArbitrage
from cennik.strategies.craft_graph import CraftGraphArbitrage
from cennik.strategies.vendor import VendorArbitrage


def test_history_is_lazy_and_still_available_to_plugins_regardless_enabled_ids(analytics_root):
    with DuckDBProvider(analytics_root / "analytics/ahledger.duckdb", "market-a", NOW) as provider:
        with patch.object(provider, "historical_prices", wraps=provider.historical_prices) as history:
            prices = PriceEngine(provider, AnalyticsConfig(enabled_strategies=["vendor_arbitrage"]))
            assert history.call_count == 0
            reference = prices.reference(2)
            assert reference.copper == 100
            assert history.call_count == 1
            assert prices.reference(2) == reference
            assert history.call_count == 1


def test_indexed_source_flags_equal_previous_scan_with_global_and_item_scopes(analytics_root):
    rows = [
        {"item_id": None, "status": "FAILED", "warnings": [], "gaps": []},
        {"item_id": 1, "status": "COMPLETE", "warnings": ["missing"], "gaps": []},
        {"item_id": 2, "status": "NO_SCANS", "warnings": [], "gaps": ["gap"]},
        {"item_id": 999, "status": "NOT_AVAILABLE", "warnings": ["unrelated"], "gaps": []},
    ]
    class ReadOnce(list):
        reads = 0

        def __iter__(self):
            self.reads += 1
            assert self.reads == 1, "Metadata re-scanned for each price"
            return super().__iter__()

    with DuckDBProvider(analytics_root / "analytics/ahledger.duckdb", "market-a", NOW) as provider:
        with patch.object(provider, "quality", return_value={"metadata": ReadOnce(rows)}):
            prices = PriceEngine(provider, AnalyticsConfig())
            for item in (1, 2, 3, 20):
                for _ in range(3):
                    actual = set(prices.flags(prices.current[item]))
                    expected = set()
                    for row in rows:
                        if row["item_id"] in (None, item):
                            if row["status"] in {"FAILED", "NOT_AVAILABLE", "NO_SCANS"}:
                                expected.add("incomplete_source_coverage")
                            if row["warnings"] or row["gaps"]:
                                expected.add("source_quality_warnings")
                    assert actual & {"incomplete_source_coverage", "source_quality_warnings"} == expected


def test_two_craft_strategies_share_sales_history_only_inside_one_run(analytics_root):
    add_intermediate(analytics_root)
    with DuckDBProvider(analytics_root / "analytics/ahledger.duckdb", "market-a", NOW) as provider:
        with patch.object(provider, "sales_history", wraps=provider.sales_history) as sales:
            config = AnalyticsConfig(craft_liquidity_enabled=True)
            ctx = context(provider, config)
            SimpleCraftArbitrage().evaluate(ctx)
            CraftGraphArbitrage().evaluate(ctx)
            assert sales.call_count == 1
            ctx.prices.sales_history(5)
            assert sales.call_count == 2
            next_context = context(provider, config)
            SimpleCraftArbitrage().evaluate(next_context)
            assert sales.call_count == 3


def test_running_phase_is_visible_and_timings_survive_summary_reload(analytics_root):
    entered, release = threading.Event(), threading.Event()

    class Slow(VendorArbitrage):
        id = "slow"

        def evaluate(self, context):
            entered.set()
            assert release.wait(10)
            return super().evaluate(context)

    service = configured(analytics_root)
    service.registry.register(Slow())
    service.update_settings(AnalyticsSettings(active_market="market-a", config=AnalyticsConfig(enabled_strategies=["slow"])))
    try:
        service.start(as_of=NOW)
        assert entered.wait(10)
        status = service.status()
        assert status["running"]
        assert status["progress"]["stage"] == "strategy_slow"
        assert status["progress"]["elapsed_seconds"] >= 0
        assert service.store.listing("market-a")["items"] == []
    finally:
        release.set()
        service.shutdown()
    status = service.status()
    assert not status["running"] and status["progress"]["stage"] is None
    timings = status["latest_run"]["diagnostics"]["performance"]["timings_seconds"]
    assert "initial_fingerprint" in timings and "final_fingerprint" in timings
    assert "historical_prices" not in timings
    assert "save_results" in status["progress"]["timings_seconds"]
    assert status["latest_run"]["signal_count"] == 1
