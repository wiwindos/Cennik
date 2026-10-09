from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_dir: Path = Path("data")
    markets: list[str] = Field(default_factory=list)
    datasets: list[str] = Field(default_factory=list)
    requests_per_second: float = Field(default=1.0, gt=0, le=5)
    tools_per_second: float = Field(default=0.5, gt=0, le=1)
    concurrency: int = Field(default=2, ge=1, le=8)
    connect_timeout: float = Field(default=10, gt=0)
    read_timeout: float = Field(default=30, gt=0)
    max_attempts: int = Field(default=5, ge=1, le=20)
    max_response_bytes: int = Field(default=64 * 1024 * 1024, ge=1024)
    full_history_check: bool = True
    enrich_items: str = "all"  # none | referenced | all
    signal_variants: bool = True  # 4 discounts x 3 horizons, each changes the model
    discovery_ttl_hours: int = Field(default=24, ge=1)

    def selected_markets(self, available: list[str]) -> list[str]:
        return [market for market in available if not self.markets or market in self.markets]

    def download_datasets(self) -> list[str] | None:
        if not self.datasets:
            return None
        dependencies = {"markets", "economy", "catalog_index", "catalog"}
        if "history" in self.datasets:
            dependencies.add("pricetable")
        if "crafting" in self.datasets:
            dependencies.add("professions")
        return sorted(dependencies | set(self.datasets))

    @classmethod
    def read(cls, file: Path | None = None, root: Path | None = None) -> Settings:
        values = tomllib.loads(file.read_text("utf-8")) if file and file.exists() else {}
        if root is not None:
            values["data_dir"] = root
        elif os.getenv("AHL_DATA_DIR"):
            values["data_dir"] = os.environ["AHL_DATA_DIR"]
        settings = cls(**values)
        if settings.enrich_items not in {"none", "referenced", "all"}:
            raise ValueError("enrich_items must be none, referenced or all")
        return settings


API = "https://api.ahledger.com"
SITE = "https://ahledger.com"
RANGES = ("24h", "7d", "30d", "90d")
TOOLS = frozenset({"crafting", "deals", "signals", "disenchant", "structure"})
PARSER_VERSION = "1.0.0"
