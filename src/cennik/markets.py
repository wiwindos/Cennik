"""Stable internal market identifiers, independent of display names and AHledger."""
import re

from pydantic import Field, model_validator

from cennik.core.models import Model

LEGACY_MARKET = "forever.pvp.alliance.us"
MARKET_PATTERN = r"^forever\.[a-z0-9_-]{1,64}\.(alliance|horde|neutral)\.(eu|us|kr|tw|cn)$"


def forever_realm_identity(realm):
    if not isinstance(realm, str):
        return None
    # Explicit in-game confirmation of an owner-selected market when Forever
    # hides realm APIs. This is a logical market identity, never an NPC type.
    alias = re.fullmatch(r"forever-(eu|us|kr|tw|cn)-(pvp|normal|rp|rppvp)", realm)
    if alias:
        return alias[1], alias[2]
    match = re.fullmatch(r"CAMWOW (EU|US|KR|TW|CN)[0-9]{1,3} Realm [0-9]{1,3} (PvP|PvE|RP|RP-PvP)", realm, re.IGNORECASE)
    if not match:
        return None
    return match[1].lower(), {"pvp": "pvp", "pve": "normal", "rp": "rp", "rp-pvp": "rppvp"}[match[2].lower()]


def binding_ruleset(binding):
    data = binding if isinstance(binding, dict) else binding.model_dump()
    if data.get("ruleset"):
        return data["ruleset"]
    identities = {identity for realm in data.get("realm_names", []) if (identity := forever_realm_identity(realm))}
    return next(iter(identities))[1] if len(identities) == 1 and next(iter(identities))[0] == data.get("region") else None


def realm_matches(binding, realm):
    if binding is None or not isinstance(realm, str):
        return False
    data = binding if isinstance(binding, dict) else binding.model_dump()
    identity = forever_realm_identity(realm)
    ruleset = binding_ruleset(data)
    if data.get("client_family") == "forever" and identity and ruleset:
        return identity == (data.get("region"), ruleset)
    return realm in data.get("realm_names", [])


def equivalent_realms(left, right, binding):
    if isinstance(left, str) and left and left == right:
        return True
    if binding is None:
        return False
    data = binding if isinstance(binding, dict) else binding.model_dump()
    return bool(data.get("client_family") == "forever" and forever_realm_identity(left)
                and forever_realm_identity(left) == forever_realm_identity(right)
                and realm_matches(data, left) and realm_matches(data, right))


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
