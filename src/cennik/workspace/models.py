from typing import Literal

from pydantic import Field

from cennik.core.models import Model

MARKET = "forever.pvp.alliance.us"


class WorkspaceSettings(Model):
    auto_scan: bool = True
    auto_import: bool = True
    auto_refresh: bool = False
    auto_recalculate: bool = False
    refresh_seconds: int = Field(default=300, ge=60, le=86400)
    cache_history_days: int = Field(default=30, ge=1, le=365)
    max_depth_age_seconds: int = Field(default=900, ge=1, le=86400)
    budget_copper: int = Field(default=300000, ge=1, le=2**53 - 1)
    min_roi_percent: float = Field(default=0, ge=0, le=1000000)
    game_root: str = ""
    account: str = ""
    professions: list[str] = Field(default_factory=lambda: ["Alchemy", "Blacksmithing", "Engineering", "Enchanting", "Leatherworking", "Tailoring", "Cooking"], max_length=20)


class RefreshRequest(Model):
    dataset: Literal["pricetable", "references", "item", "history", "structure", "signals", "deals", "disenchant"]
    item_id: int | None = Field(default=None, gt=0)
    range: Literal["24h", "7d", "30d", "90d"] = "7d"


class PlanRequest(Model):
    opportunity_id: str
    batches: int = Field(default=1, ge=1, le=10000)
    allow_overbuy: bool = False  # Legacy import/API field; Forever plans buy only the requested units.


class ImportRequest(Model):
    scans: list[dict] = Field(max_length=20)


class ItemFacts(Model):
    vendor_buy_copper: int | None = Field(default=None, ge=0, le=2**53 - 1)
    vendor_sell_copper: int | None = Field(default=None, ge=0, le=2**53 - 1)
