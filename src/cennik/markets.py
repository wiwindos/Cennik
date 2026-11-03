"""Stable internal market identifiers, independent of display names and AHledger."""
import re

from pydantic import Field, model_validator

from cennik.core.models import Model

LEGACY_MARKET = "forever.pvp.alliance.us"
MARKET_PATTERN = r"^forever\.[a-z0-9_-]{1,64}\.(alliance|horde|neutral)\.(eu|us|kr|tw|cn)$"


class MarketBinding(Model):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    market: str = Field(pattern=MARKET_PATTERN)
    label: str = Field(max_length=160)
    faction: str = Field(pattern=r"^(alliance|horde)$")
    region: str = Field(pattern=r"^(eu|us|kr|tw|cn)$")
    realm_names: list[str] = Field(max_length=30)
    client_family: str = Field(default="test", pattern=r"^(forever|test)$")

    @model_validator(mode="after")
    def identity(self):
        if self.market != f"forever.{self.id}.{self.faction}.{self.region}" or any(not n or len(n)>160 for n in self.realm_names):
            raise ValueError("Неверная привязка рынка")
        return self


def valid_market(value):
    return isinstance(value, str) and re.fullmatch(MARKET_PATTERN, value) is not None


def neutral_market(value):
    if not valid_market(value):
        raise ValueError("Неверный рынок")
    parts = value.split(".")
    parts[-2] = "neutral"
    return ".".join(parts)
