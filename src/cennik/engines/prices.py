from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median

from cennik.core.interfaces import MarketDataProvider
from cennik.core.models import AnalyticsConfig, PriceObservation
from cennik.engines.economics import capped_volatility


@dataclass
class HistoricalReference:
    copper: int | None
    relative_mad: Decimal | None
    relative_volatility: Decimal | None
    recent_copper: int | None
    sample_count: int
    day_count: int
    span_days: int
    latest_at: datetime | None
    granularities: list[str]
    flags: list[str]


class PriceEngine:
    def __init__(self, provider: MarketDataProvider, config: AnalyticsConfig):
        self.provider, self.config = provider, config
        self.current = provider.current_prices()
        self.history = provider.historical_prices(config.history_days)
        self.quality = provider.quality()

    def age(self, price: PriceObservation) -> int:
        return max(0, int((self.provider.as_of - price.observed_at).total_seconds()))

    def flags(self, price: PriceObservation) -> list[str]:
        result = ["aggregated_prices_no_executable_lot", "partial_coverage"]
        if self.age(price) > self.config.max_price_age_seconds:
            result.append("stale_price")
        if price.quantity is None:
            result.append("unknown_supply")
        elif price.quantity <= 0:
            result.append("no_observed_supply")
        if price.granularity == "day":
            result.append("daily_aggregate_price")
        if price.sales_estimated_per_day is None:
            result.append("unknown_sales")
        elif price.sales_observed_at and (
            self.provider.as_of - price.sales_observed_at
        ).total_seconds() > self.config.max_price_age_seconds:
            result.append("stale_sales_estimate")
        for row in self.quality.get("metadata", []):
            if row.get("item_id") not in (None, price.item_id):
                continue
            if row.get("status") in {"FAILED", "NOT_AVAILABLE", "NO_SCANS"}:
                result.append("incomplete_source_coverage")
            if row.get("warnings") or row.get("gaps"):
                result.append("source_quality_warnings")
        return sorted(set(result))

    def usable(self, price: PriceObservation | None) -> str | None:
        if price is None or price.min_copper is None or price.min_copper <= 0:
            return "missing_positive_buy_price"
        if self.age(price) > self.config.max_price_age_seconds:
            return "stale_price"
        if price.quantity is not None and price.quantity <= 0:
            return "no_observed_supply"
        return None

    def reference(self, item_id: int) -> HistoricalReference:
        current = self.current.get(item_id)
        # Use complete prior UTC days. A current day's aggregate cannot be its own
        # historical baseline. Each covered day has equal weight, regardless of scans.
        boundary = (current.observed_at if current else self.provider.as_of).replace(
            hour=0, minute=0, second=0, microsecond=0)
        by_day: dict[datetime, list[PriceObservation]] = defaultdict(list)
        for row in self.history.get(item_id, []):
            if row.observed_at < boundary and row.median_copper is not None and row.median_copper > 0:
                by_day[row.observed_at.replace(hour=0, minute=0, second=0, microsecond=0)].append(row)
        points, values = [], []
        granularities: set[str] = set()
        for day in sorted(by_day):
            rows = by_day[day]
            scans = {r.observed_at.replace(minute=0, second=0, microsecond=0) for r in rows if r.granularity == "scan"}
            selected = [r for r in rows if r.granularity == "scan" or
                        (r.granularity == "hour" and r.observed_at.replace(minute=0, second=0, microsecond=0) not in scans)]
            if not selected:
                selected = [r for r in rows if r.granularity == "day"]
            # Equal timestamps/granularity are also deduplicated for provider plugins.
            selected = list({(r.observed_at, r.granularity): r for r in selected}.values())
            if selected:
                values.append(median([Decimal(r.median_copper) for r in selected if r.median_copper is not None]))
                points.extend(selected)
                granularities.update(r.granularity for r in selected)
        center = median(values) if values else None
        mad = median([abs(v - center) for v in values]) / center if center else None
        volatility = capped_volatility(values, self.config.history_outlier_cap, center) if center else None
        recent = median(values[-3:]) if values else None
        span = (max(by_day) - min(by_day)).days if by_day else 0
        latest = max((p.observed_at for p in points), default=None)
        flags = []
        if len(values) < self.config.min_history_days or span < self.config.min_history_span_days:
            flags.append("insufficient_history")
        if latest is None or boundary - latest > timedelta(days=self.config.max_history_gap_days):
            flags.append("stale_history")
        if (mad is not None and mad > self.config.max_relative_mad) or (
            volatility is not None and volatility > self.config.max_relative_volatility
        ):
            flags.append("unstable_history")
        if center and recent and (center - recent) * 100 / center > self.config.max_recent_decline_percent:
            flags.append("recent_price_decline")
        return HistoricalReference(int(center) if center else None, mad, volatility,
                                   int(recent) if recent else None, len(points), len(values),
                                   span, latest, sorted(granularities), flags)
