from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from cennik.core.interfaces import EvaluationContext, Strategy
from cennik.core.models import Opportunity, PriceObservation
from cennik.engines.economics import roi


def opportunity(
    context: EvaluationContext, strategy: Strategy, price: PriceObservation,
    reference: int, capital: int, profit: int, action: str, reason: str,
    *, quantity: int = 1, flags: list[str] | None = None,
    included: list[str] | None = None, excluded: list[str] | None = None,
    details: dict[str, Any] | None = None, observed_at: datetime | None = None,
    identity: str = "", confidence: str = "limited",
) -> Opportunity:
    item = context.items.get(price.item_id)
    observed = observed_at or price.observed_at
    risks = sorted(set(context.prices.flags(price) + (flags or [])))
    return Opportunity(
        id=uuid5(NAMESPACE_URL, f"{context.run_id}/{strategy.id}/{price.item_id}/{identity}").hex,
        run_id=context.run_id, strategy_id=strategy.id, strategy_version=strategy.version,
        market=context.provider.market, item_id=price.item_id,
        item_name=item.name if item and item.name else str(price.item_id),
        action=action, observed_at=observed, generated_at=context.generated_at,
        buy_price_copper=capital, reference_price_copper=reference, quantity=quantity,
        required_capital_copper=capital, potential_profit_copper=profit,
        roi_percent=roi(profit, capital), confidence=confidence,  # type: ignore[arg-type]
        risk_flags=risks, reason=reason, source=price.source,
        data_age_seconds=max(0, int((context.provider.as_of - observed).total_seconds())),
        costs_included=included or ["purchase"], costs_excluded=excluded or [],
        details={"price": price.model_dump(mode="json"), "as_of": context.provider.as_of.isoformat(),
                 "scope": "one indicative unit or one craft; minimum-price depth is unknown",
                 **(details or {})},
    )
