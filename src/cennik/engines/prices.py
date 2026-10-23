from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
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
    method: str = "local_daily_medians"


class PriceEngine:
    def __init__(self, provider: MarketDataProvider, config: AnalyticsConfig,
                 measure: Callable[[str], AbstractContextManager] | None = None):
        self.provider, self.config = provider, config
        self.measure = measure or (lambda _: nullcontext())
        with self.measure("current_prices"):
            self.current = provider.current_prices()
        self._history: dict[int, list[PriceObservation]] | None = None
        self._sales_history: dict[int, dict[int, list[PriceObservation]]] = {}
        with self.measure("source_quality"):
            self.quality = provider.quality()
            self._global_flags: set[str] = set()
            self._item_flags: dict[int, set[str]] = defaultdict(set)
            for row in self.quality.get("metadata", []):
                target = self._global_flags if row.get("item_id") is None else self._item_flags[row["item_id"]]
                if row.get("status") in {"FAILED", "NOT_AVAILABLE", "NO_SCANS"}:
                    target.add("incomplete_source_coverage")
                if row.get("warnings") or row.get("gaps"):
                    target.add("source_quality_warnings")

    @property
    def history(self) -> dict[int, list[PriceObservation]]:
        if self._history is None:
            with self.measure("historical_prices"):
                self._history = self.provider.historical_prices(self.config.history_days)
        return self._history

    def sales_history(self, days: int) -> dict[int, list[PriceObservation]]:
        if days not in self._sales_history:
            with self.measure("sales_history"):
                self._sales_history[days] = self.provider.sales_history(days)
        return self._sales_history[days]

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
        result.extend(self._global_flags)
        result.extend(self._item_flags.get(price.item_id, ()))
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
        if self.config.history_reference_mode == "ahl_median30d":
            lookup = getattr(self.provider, "source_reference", None)
            value = lookup(item_id) if lookup else None
            flags = []
            if not value or not value.get("copper"):
                flags.append("missing_source_reference")
            elif (self.provider.as_of - value["observed_at"]).total_seconds() > self.config.max_price_age_seconds:
                flags.append("stale_source_reference")
            return HistoricalReference(value["copper"] if value else None, None, None, None,
                0, 0, 30, value["observed_at"] if value else None, ["source_aggregate"], flags,
                "AHledger median30d; local stability checks not applied")
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
