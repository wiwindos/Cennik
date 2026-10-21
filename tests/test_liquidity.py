from datetime import timedelta
from decimal import Decimal

import duckdb
import pytest
from test_cennik import NOW, context, insert
from test_cennik import analytics_root as analytics_root
from test_craft_graph import add_intermediate

from cennik.core.models import AnalyticsConfig
from cennik.data.duckdb import DuckDBProvider
from cennik.engines.liquidity import CraftLiquidity
from cennik.strategies.craft import SimpleCraftArbitrage
from cennik.strategies.craft_graph import CraftGraphArbitrage


def liquidity_context(root, **changes):
    config = AnalyticsConfig(craft_liquidity_enabled=True, **changes)
    provider = DuckDBProvider(root / "analytics/ahledger.duckdb", "market-a", NOW)
    return provider, context(provider, config)


def sales(root, values=("30", "25", "20"), quantity=10):
    with duckdb.connect(str(root / "analytics/ahledger.duckdb")) as db:
        db.execute("DELETE FROM price_snapshots WHERE item_id=20 AND market='market-a'")
        for index, value in enumerate(values):
            insert(db, "price_snapshots", item_id=20, market="market-a", observed_at=NOW-timedelta(hours=1, days=index),
                   min_copper=150, quantity=quantity, sales_estimated_per_day=Decimal(value) if value is not None else None,
                   source="sales fixture", raw_id=f"sales-{index}")


def test_liquidity_conservative_daily_estimates_scope_and_future(analytics_root):
    sales(analytics_root)
    with duckdb.connect(str(analytics_root / "analytics/ahledger.duckdb")) as db:
        insert(db, "price_snapshots", item_id=20, market="market-a", observed_at=NOW-timedelta(hours=2),
               min_copper=150, quantity=10, sales_estimated_per_day=Decimal("100"), source="duplicate earlier")
        insert(db, "price_snapshots", item_id=20, market="market-b", observed_at=NOW-timedelta(hours=1),
               sales_estimated_per_day=Decimal("0"), source="other market")
        insert(db, "price_snapshots", item_id=20, market="market-a", observed_at=NOW+timedelta(hours=1),
               sales_estimated_per_day=Decimal("0"), source="future")
    provider, ctx = liquidity_context(analytics_root)
    with provider:
        signal = SimpleCraftArbitrage().evaluate(ctx)[0]
        evidence = signal.details["liquidity"]
        assert evidence["passed"] and evidence["covered_days"] == 3
        assert evidence["conservative_sales_per_day"] == "20.000000"
        assert Decimal(evidence["batch_daily_sales_percent"]) == 10
        assert Decimal(evidence["supply_to_daily_sales"]) == Decimal("0.5")
        assert signal.details["price"]["sales_source"] == "sales fixture"


@pytest.mark.parametrize(("values", "quantity", "changes", "reason"), [
    ((None,), 10, {}, "liquidity_unknown_sales"),
    (("30",), 10, {}, "liquidity_insufficient_sales_history"),
    (("30", "1"), 10, {}, "liquidity_low_sales"),
    (("30", "20"), None, {}, "liquidity_unknown_supply"),
    (("30", "20"), 30, {}, "liquidity_high_supply"),
    (("30", "20"), 10, {"craft_max_batch_sales_percent": 5}, "liquidity_large_batch"),
    (("30", "20"), 10, {"craft_sales_max_age_seconds": 60}, "liquidity_stale_sales"),
])
def test_filter_rejects_unproven_and_illiquid_crafts(analytics_root, values, quantity, changes, reason):
    sales(analytics_root, values, quantity)
    provider, ctx = liquidity_context(analytics_root, **changes)
    with provider:
        assert not SimpleCraftArbitrage().evaluate(ctx)
        assert ctx.diagnostics["simple_craft"][reason] == 1


def test_liquidity_disabled_preserves_craft_and_graph_uses_same_screen(analytics_root):
    add_intermediate(analytics_root)
    sales(analytics_root, ("30", "20"))
    provider, ctx = liquidity_context(analytics_root)
    with provider:
        graph = CraftGraphArbitrage().evaluate(ctx)
        assert graph and graph[0].details["liquidity"]["passed"]
    sales(analytics_root, ("1", "1"))
    provider, ctx = liquidity_context(analytics_root)
    with provider:
        assert not CraftGraphArbitrage().evaluate(ctx)
        assert ctx.diagnostics["craft_graph"]["liquidity_low_sales"] >= 1
    with DuckDBProvider(analytics_root / "analytics/ahledger.duckdb", "market-a", NOW) as provider:
        ctx = context(provider)
        assert SimpleCraftArbitrage().evaluate(ctx)
        assert not SimpleCraftArbitrage().evaluate(ctx)[0].details["liquidity"]["enabled"]


def test_filter_requires_known_sales_timestamp_and_valid_window(analytics_root):
    sales(analytics_root)
    provider, ctx = liquidity_context(analytics_root)
    with provider:
        price = ctx.prices.current[20].model_copy(update={"sales_observed_at": None})
        assert CraftLiquidity(ctx).check(price, 2)[1] == "liquidity_unknown_sales"
    with pytest.raises(ValueError):
        AnalyticsConfig(craft_min_sales_days=4, craft_sales_window_days=3)
