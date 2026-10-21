from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from .models import AnalyticsConfig, Item, Opportunity, PriceObservation, Recipe

if TYPE_CHECKING:
    from cennik.engines.prices import PriceEngine


class MarketDataProvider(Protocol):
    market: str
    as_of: datetime

    def current_prices(self) -> dict[int, PriceObservation]: ...
    def historical_prices(self, days: int) -> dict[int, list[PriceObservation]]: ...
    def sales_history(self, days: int) -> dict[int, list[PriceObservation]]: ...
    def items(self) -> dict[int, Item]: ...
    def recipes(self) -> list[Recipe]: ...
    def quality(self) -> dict[str, Any]: ...


@dataclass
class EvaluationContext:
    provider: MarketDataProvider
    config: AnalyticsConfig
    run_id: str
    generated_at: datetime
    prices: PriceEngine
    items: dict[int, Item]
    diagnostics: dict[str, dict[str, int]] = field(default_factory=dict)

    def reject(self, strategy: str, reason: str) -> None:
        counts = self.diagnostics.setdefault(strategy, {})
        counts[reason] = counts.get(reason, 0) + 1


class Strategy(Protocol):
    id: str
    version: str

    def evaluate(self, context: EvaluationContext) -> list[Opportunity]: ...
