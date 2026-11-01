"""One isolated workspace per stable server/faction ID; no legacy DB migration."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field

from ahledger.util import atomic_json
from cennik.core.models import Model
from cennik.markets import MARKET_PATTERN
from cennik.workspace.service import Workspace


class MarketProfile(Model):
    id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[a-f0-9]{32}$")
    label: str = Field(default="PvP · Альянс · EU", min_length=1, max_length=160)
    ruleset: str = Field(default="pvp", pattern=r"^[a-z0-9_-]{1,40}$")
    faction: Literal["alliance", "horde"] = "alliance"
    region: Literal["eu", "us", "kr", "tw", "cn"] = "eu"
    realm_names: list[str] = Field(default_factory=list, max_length=30)
    ahl_market: str | None = Field(default="forever.pvp.alliance.eu", pattern=MARKET_PATTERN)

    @property
    def market(self):
        return f"forever.{self.id}.{self.faction}.{self.region}"

    def public(self):
        return {**self.model_dump(mode="json"), "market": self.market}


class Catalog:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.lock = threading.RLock()
        self.workspaces: dict[str, Workspace] = {}
        self.running = False
        if not self.path.exists():
            p = MarketProfile()
            atomic_json(self.path, {"profiles": [p.model_dump(mode="json")], "ahl_profile_id": p.id})

    @property
    def path(self):
        return self.root / "markets.json"

    def config(self):
        return json.loads(self.path.read_text("utf-8"))

    def profiles(self):
        return [MarketProfile.model_validate(p) for p in self.config()["profiles"]]

    def profile(self, identity):
        value = next((p for p in self.profiles() if p.id == identity), None)
        if value is None:
            raise ValueError("Сервер/рынок не найден")
        return value

    def save(self, profile: MarketProfile, *, source=False):
        with self.lock:
            config = self.config()
            existing = next((p for p in self.profiles() if p.id == profile.id), None)
            if existing and (existing.faction, existing.region, existing.ruleset) != (profile.faction, profile.region, profile.ruleset):
                raise ValueError("Регион, фракция и тип существующего рынка неизменны. Создайте отдельный профиль")
            names = [v.strip() for v in profile.realm_names]
            if any(not v or len(v)>160 or any(c in v for c in "\n\r:") for v in names):
                raise ValueError("Неверное имя игрового сервера")
            if profile.ahl_market and profile.ahl_market.split('.')[-2:] != [profile.faction, profile.region]:
                raise ValueError("Рынок AHledger должен соответствовать региону и фракции профиля")
            profile = profile.model_copy(update={"realm_names": names})
            config["profiles"] = [p for p in config["profiles"] if p["id"] != profile.id]+[profile.model_dump(mode="json")]
            if source:
                if not profile.ahl_market:
                    raise ValueError("Укажите рынок AHledger")
                config["ahl_profile_id"] = profile.id
            atomic_json(self.path, config)
            if profile.id in self.workspaces:
                self.workspaces[profile.id].ahl_market = profile.ahl_market or profile.market
                self.workspaces[profile.id].market.market_info = profile.public()
            return profile

    def workspace(self, identity):
        with self.lock:
            if identity not in self.workspaces:
                p = self.profile(identity)
                w = Workspace(self.root / "markets" / p.id, market_key=p.market, market_info=p.public(),
                    server_mode=True, ahl_market=p.ahl_market)
                def guard():
                    if self.config().get("ahl_profile_id") != identity or not self.profile(identity).ahl_market:
                        raise RuntimeError("AHledger настроен на другой сервер/рынок. Измените выбор в панели сервера")
                w.updates_guard = guard
                w.market_binding = {"id": p.id, "market": p.market, "realm_names": p.realm_names,
                    "faction": p.faction, "region": p.region}
                self.workspaces[identity] = w
                if self.running:
                    w.thread.start()
            w = self.workspaces[identity]
            p = self.profile(identity)
            w.market_binding = {"id": p.id, "market": p.market, "realm_names": p.realm_names,
                "faction": p.faction, "region": p.region}
            return w

    def public(self):
        return [p.public() for p in self.profiles()]

    def close(self):
        for w in self.workspaces.values():
            w.close()
