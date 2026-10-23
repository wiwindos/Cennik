"""Conservative source-estimate screen; never a sale probability or waiting time."""
from decimal import Decimal
from typing import Any

from cennik.core.interfaces import EvaluationContext
from cennik.core.models import PriceObservation


class CraftLiquidity:
    def __init__(self, context: EvaluationContext):
        self.context = context
        self.history = (context.prices.sales_history(context.config.craft_sales_window_days)
                        if context.config.craft_liquidity_enabled and context.config.craft_liquidity_source == "history" else {})

    def check(self, price: PriceObservation, quantity: int) -> tuple[dict[str, Any], str | None]:
        config, as_of = self.context.config, self.context.provider.as_of
        points = self.history.get(price.item_id, [])
        estimates = [p.sales_estimated_per_day for p in points if p.sales_estimated_per_day is not None]
        conservative = min(estimates) if estimates else None
        if config.craft_liquidity_source == "latest_source":
            conservative = price.sales_estimated_per_day
        sales_at = price.sales_observed_at
        age = max(0, int((as_of - sales_at).total_seconds())) if sales_at else None
        supply_days = Decimal(price.quantity) / conservative if price.quantity is not None and conservative and conservative > 0 else None
        batch_percent = Decimal(quantity) * 100 / conservative if conservative and conservative > 0 else None
        evidence = {
            "enabled": config.craft_liquidity_enabled, "sales_estimated_per_day": str(price.sales_estimated_per_day) if price.sales_estimated_per_day is not None else None,
            "method": config.craft_liquidity_source,
            "sales_observed_at": sales_at.isoformat() if sales_at else None,
            "sales_source": price.sales_source or price.source,
            "conservative_sales_per_day": str(conservative) if conservative is not None else None,
            "covered_days": len(points), "window_days": config.craft_sales_window_days,
            "observed_supply": price.quantity, "supply_observed_at": price.observed_at.isoformat(),
            "supply_to_daily_sales": str(supply_days) if supply_days is not None else None,
            "batch_daily_sales_percent": str(batch_percent) if batch_percent is not None else None,
            "observations": [{"observed_at": p.observed_at.isoformat(),
                "sales_estimated_per_day": str(p.sales_estimated_per_day),
                "source": p.source, "raw_id": p.raw_id, "sha256": p.sha256} for p in points],
            "thresholds": {"min_sales_per_day": str(config.craft_min_sales_per_day),
                "max_supply_days": str(config.craft_max_supply_days),
                "max_batch_sales_percent": str(config.craft_max_batch_sales_percent),
                "min_sales_days": config.craft_min_sales_days, "max_sales_age_seconds": config.craft_sales_max_age_seconds},
            "note": "Source estimates, not confirmed transactions. Supply/sales is a pressure proxy, not sale time. No sale guarantee.",
        }
        reason = None
        if config.craft_liquidity_enabled:
            if price.sales_estimated_per_day is None or age is None:
                reason = "liquidity_unknown_sales"
            elif age > config.craft_sales_max_age_seconds:
                reason = "liquidity_stale_sales"
            elif config.craft_liquidity_source == "history" and len(points) < config.craft_min_sales_days:
                reason = "liquidity_insufficient_sales_history"
            elif conservative is None or conservative < config.craft_min_sales_per_day or price.sales_estimated_per_day < config.craft_min_sales_per_day:
                reason = "liquidity_low_sales"
            elif price.quantity is None:
                reason = "liquidity_unknown_supply"
            elif supply_days is not None and supply_days > config.craft_max_supply_days:
                reason = "liquidity_high_supply"
            elif batch_percent is not None and batch_percent > config.craft_max_batch_sales_percent:
                reason = "liquidity_large_batch"
        evidence["passed"] = None if not config.craft_liquidity_enabled else reason is None
        return evidence, reason
