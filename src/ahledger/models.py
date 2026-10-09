from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from .util import timestamp

NonnegativeInt = Annotated[StrictInt, Field(ge=0, le=2**63 - 1)]
PositiveInt = Annotated[StrictInt, Field(gt=0, le=2**63 - 1)]


class OpenModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class Market(OpenModel):
    id: str
    game: str
    ruleset: str
    faction: str
    region: str
    label: str


class Item(OpenModel):
    id: PositiveInt
    name: str


class PricePoint(OpenModel):
    t: str
    min: NonnegativeInt | None
    median: NonnegativeInt | None
    p25: NonnegativeInt | None
    p75: NonnegativeInt | None
    quantity: NonnegativeInt
    auctions: NonnegativeInt

    @field_validator("t")
    @classmethod
    def valid_time(cls, value: str) -> str:
        return timestamp(value) or ""


class History(OpenModel):
    market: str
    item: Item
    range: Literal["24h", "7d", "30d", "90d"]
    latest: dict[str, Any] | None
    series: list[PricePoint]


class JobSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    dataset: str
    url: str
    params: dict[str, str] = Field(default_factory=dict)
    market: str | None = None
    item_id: int | None = None
    priority: int = 1
    context: dict[str, Any] = Field(default_factory=dict)
