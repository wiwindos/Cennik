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
    changes.setdefault("craft_allow_unknown_sales", False)
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


@pytest.mark.parametrize(("values", "changes", "unknown_reason"), [
    ((None,), {}, "liquidity_unknown_sales"),
    (("30",), {}, "liquidity_insufficient_sales_history"),
    (("30", "20"), {"craft_sales_max_age_seconds": 60}, "liquidity_stale_sales"),
])
def test_startup_mode_keeps_profitable_unknown_demand_without_fabricating_a_limit(analytics_root, values, changes, unknown_reason):
    from cennik.engines.liquidity import batch_sales_limit

    sales(analytics_root, values)
    provider, ctx = liquidity_context(analytics_root, craft_allow_unknown_sales=True, **changes)
    with provider:
        signal = SimpleCraftArbitrage().evaluate(ctx)[0]
        evidence = signal.details["liquidity"]
        assert signal.potential_profit_copper > 0
        assert "liquidity_unknown" in signal.risk_flags
        assert evidence["status"] == "unknown" and evidence["passed"] is None
        assert evidence["unknown_reason"] == unknown_reason
        assert batch_sales_limit(evidence) is None


def test_zero_settings_disable_minima_and_history_but_keep_latest_sales(analytics_root):
    from cennik.engines.liquidity import batch_sales_limit

    config = AnalyticsConfig(craft_min_sales_per_day=0, craft_sales_window_days=0, craft_min_sales_days=0)
    assert config.craft_allow_unknown_sales
    for field in ("craft_min_sales_per_day", "craft_sales_window_days", "craft_min_sales_days"):
        with pytest.raises(ValueError):
            AnalyticsConfig(**{field: -1})
    sales(analytics_root, ("30",))
    provider, ctx = liquidity_context(analytics_root, craft_sales_window_days=0, craft_min_sales_days=0,
                                     craft_min_sales_per_day=0)
    with provider:
        ctx.prices.sales_history = lambda _: pytest.fail("zero window must not load history")
        evidence, reason = CraftLiquidity(ctx).check(ctx.prices.current[20], 2)
        assert reason is None and evidence["method"] == "latest_source"
        assert batch_sales_limit(evidence) == Decimal(30)
        evidence["conservative_sales_per_day"] = "0"
        assert batch_sales_limit(evidence) == 0  # known zero is not unknown


@pytest.mark.parametrize(("values", "quantity", "reason"), [
    (("1",), 10, "liquidity_low_sales"),
    (("1", "1"), 10, "liquidity_low_sales"),
    (("20", "20"), 100, "liquidity_high_supply"),
])
def test_startup_mode_preserves_known_demand_filters(analytics_root, values, quantity, reason):
    sales(analytics_root, values, quantity)
    provider, ctx = liquidity_context(analytics_root, craft_allow_unknown_sales=True)
    with provider:
        assert not SimpleCraftArbitrage().evaluate(ctx)
        assert ctx.diagnostics["simple_craft"][reason] == 1
