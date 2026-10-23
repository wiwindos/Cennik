from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, Field

from cennik.core.models import Model, Opportunity

MAX_INTEGER = 2**53 - 1  # Lua's exact integer range on the game client
Money = int
StrategyId = Literal["vendor_arbitrage", "historical_discount", "simple_craft", "craft_graph"]


class ExportRequest(Model):
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,80}$")
    opportunity_ids: list[str] = Field(min_length=1, max_length=100)
    max_budget_copper: int | None = Field(default=None, gt=0, le=MAX_INTEGER)
    unit_limits: dict[int, int] = Field(default_factory=dict)
    expires_in_seconds: int = Field(default=1800, ge=60, le=7200)


class PurchaseLine(Model):
    item_id: int = Field(gt=0)
    item_name: str
    quantity: int = Field(gt=0, le=MAX_INTEGER)
    unit_limit_copper: int = Field(gt=0, le=MAX_INTEGER)
    source: Literal["auction", "npc"]


class Task(Model):
    format_version: Literal[1] = 1
    task_id: str
    opportunity_id: str
    run_id: str
    strategy_id: StrategyId
    strategy_version: str
    market: str
    item_id: int
    item_name: str
    action: str
    predicted_profit_copper: int
    max_total_cost_copper: int = Field(gt=0, le=MAX_INTEGER)
    min_profit_copper: int = Field(gt=0, le=MAX_INTEGER)
    lines: list[PurchaseLine]
    opportunity: Opportunity
    fee_numerator: int
    fee_denominator: int
    discount_numerator: int
    discount_denominator: int
    created_at: AwareDatetime
    expires_at: AwareDatetime
    expires_at_epoch: int
    allow_overbuy: bool = False  # Legacy package field; ignored by Forever's unit-based planner.


class Package(Model):
    format_version: Literal[1] = 1
    package_id: str
    market: str
    run_id: str
    generation: str
    max_budget_copper: int = Field(gt=0, le=MAX_INTEGER)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    tasks: list[Task]


class ReportEvent(Model):
    event_id: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,120}$")
    task_id: str
    opportunity_id: str
    run_id: str
    market: str
    kind: Literal["scan", "purchase", "state", "error", "limit_change"]
    observed_at: AwareDatetime
    state: str = Field(max_length=80)
    item_id: int | None = Field(default=None, gt=0)
    quantity: int = Field(default=0, ge=0, le=MAX_INTEGER)
    cost_copper: int = Field(default=0, ge=0, le=MAX_INTEGER)
    details: dict[str, Any] = Field(default_factory=dict)


class ReportBatch(Model):
    format_version: Literal[1] = 1
    events: list[ReportEvent] = Field(max_length=500)


def epoch(value: datetime) -> int:
    return int(value.timestamp())
