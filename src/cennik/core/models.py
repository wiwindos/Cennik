from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnalyticsConfig(Model):
    enabled_strategies: list[str] = Field(
        default_factory=lambda: ["vendor_arbitrage", "historical_discount", "simple_craft", "craft_graph"]
    )
    strategy_settings: dict[str, dict[str, JsonValue]] = Field(default_factory=dict)
    discount_percent: Decimal = Field(default=Decimal("15"), ge=0, le=100)
    auction_cut_percent: Decimal = Field(default=Decimal("5"), ge=0, lt=100)
    history_days: int = Field(default=30, ge=2, le=365)
    min_history_days: int = Field(default=4, ge=2, le=365)
    min_history_span_days: int = Field(default=3, ge=1, le=365)
    max_history_gap_days: int = Field(default=3, ge=1, le=365)
    max_price_age_seconds: int = Field(default=86400, ge=1, le=31536000)
    max_relative_mad: Decimal = Field(default=Decimal("0.30"), ge=0, le=10)
    max_relative_volatility: Decimal = Field(default=Decimal("0.50"), ge=0, le=10)
    history_outlier_cap: Decimal = Field(default=Decimal("0.10"), ge=0, lt=Decimal("0.5"))
    max_recent_decline_percent: Decimal = Field(default=Decimal("15"), ge=0, le=100)
    min_profit_copper: int = Field(default=1, ge=1)
    ranking_profit_scale_copper: int = Field(default=10000, ge=1)
    ranking_roi_cap_percent: Decimal = Field(default=Decimal("100"), gt=0)
    ranking_risk_penalty: Decimal = Field(default=Decimal("0.08"), ge=0, le=1)

    @model_validator(mode="after")
    def valid_window(self):
        if self.min_history_days > self.history_days:
            raise ValueError("min_history_days exceeds history_days")
        if len(self.enabled_strategies) != len(set(self.enabled_strategies)):
            raise ValueError("Duplicate enabled strategies")
        return self


class AnalyticsSettings(Model):
    active_market: str | None = Field(default=None, min_length=1, max_length=200)
    config: AnalyticsConfig = Field(default_factory=AnalyticsConfig)


class Item(Model):
    item_id: int
    name: str | None = None
    vendor_sell_copper: int | None = None
    vendor_buy_copper: int | None = None
    binding: str | None = None
    source: str | None = None


class PriceObservation(Model):
    item_id: int
    market: str
    observed_at: AwareDatetime
    granularity: Literal["scan", "hour", "day"]
    min_copper: int | None = None
    median_copper: int | None = None
    quantity: int | None = None
    auctions: int | None = None
    sales_estimated_per_day: Decimal | None = None
    sales_observed_at: AwareDatetime | None = None
    source: str
    raw_id: str | None = None
    sha256: str | None = None


class RecipeInput(Model):
    item_id: int
    quantity: int


class Recipe(Model):
    spell_id: int
    version_hash: str
    result_item_id: int | None
    qty_min: int | None
    qty_max: int | None
    name: str | None = None
    profession: str | None = None
    skill_required: int | None = None
    acquisition: str | None = None
    inputs: list[RecipeInput]
    source_market: str | None = None
    raw_id: str | None = None
    source_retrieved_at: AwareDatetime | None = None
    source_generated_at: AwareDatetime | None = None


class Opportunity(Model):
    id: str
    run_id: str
    strategy_id: str
    strategy_version: str
    market: str
    item_id: int
    item_name: str
    action: str
    observed_at: AwareDatetime
    generated_at: AwareDatetime
    buy_price_copper: int = Field(ge=0)
    reference_price_copper: int = Field(ge=0)
    quantity: int = Field(gt=0)
    required_capital_copper: int = Field(gt=0)
    potential_profit_copper: int = Field(gt=0)
    roi_percent: Decimal
    confidence: Literal["limited", "moderate", "strong"]
    risk_flags: list[str]
    reason: str
    source: str
    data_age_seconds: int = Field(ge=0)
    costs_included: list[str]
    costs_excluded: list[str]
    execution_status: Literal["indicative"] = "indicative"
    rating: Decimal = Decimal(0)
    details: dict[str, Any] = Field(default_factory=dict)


class Run(Model):
    run_id: str
    market: str
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    as_of: AwareDatetime
    generation: str
    config: AnalyticsConfig
    strategy_versions: dict[str, str]
    status: Literal["running", "completed", "failed", "interrupted"] = "running"
    signal_count: int = 0
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


def utc(value: datetime) -> datetime:
    from datetime import UTC

    if value.tzinfo is None:
        raise ValueError("Timezone required")
    return value.astimezone(UTC)
