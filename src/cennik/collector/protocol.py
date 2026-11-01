from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field, model_validator

from cennik.core.models import Model

KINDS = Literal["scan", "npc", "recipe", "disenchant", "craft", "diagnostic"]
ALLOWED = {
    "scan": {"market", "realm", "faction", "build", "observed_at", "requested_at", "count", "reported_count", "skipped", "full", "error", "variants", "data"},
    "npc": {"item_id", "npc_id", "npc_name", "map_id", "batch_price_copper", "batch_quantity", "available", "observed_at", "blocked", "build"},
    "recipe": {"spell_id", "item_id", "name", "inputs", "observed_at", "coverage", "build"},
    "disenchant": {"observed_at", "spell_id", "inputs", "outputs", "build"},
    "craft": {"observed_at", "spell_id", "inputs", "outputs", "build"},
    "diagnostic": {"at", "code", "level", "scans", "rows", "queue", "error_counts", "addon_version", "build"},
}


def canonical(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


class Observation(Model):
    format_version: Literal[1] = 1
    kind: KINDS
    data: dict

    @model_validator(mode="after")
    def validate_data(self):
        if set(self.data)-ALLOWED[self.kind]:
            raise ValueError("Личные или неизвестные поля в наблюдении")
        stamp = self.data.get("observed_at", self.data.get("at"))
        if type(stamp) is not int or not 0 < stamp <= int(datetime.now(UTC).timestamp())+300:
            raise ValueError("Неверное время наблюдения")
        if self.kind == "scan":
            body = self.data.get("data")
            if not isinstance(body, str) or len(body) > 48_000_000:
                raise ValueError("Неверные строки скана")
            rows = body.split(";") if body else []
            if type(self.data.get("count")) is not int or self.data["count"] != len(rows):
                raise ValueError("Количество строк скана не совпадает")
            if self.data.get("market") != "forever.pvp.alliance.us" or self.data.get("faction") not in {None, "alliance"}:
                raise ValueError("Неподдерживаемый рынок")
            variants = self.data.get("variants") or []
            if not isinstance(variants, list) or len(variants) > 300000:
                raise ValueError("Неверные варианты скана")
            for v in variants:
                if not isinstance(v, dict) or set(v)-{"link", "name", "divisible", "vendor_sell"}:
                    raise ValueError("Неверные поля варианта")
                if any(v.get(k) is not None and (not isinstance(v[k], str) or len(v[k]) > n) for k, n in (("link", 1500), ("name", 300))):
                    raise ValueError("Неверное описание варианта")
                if v.get("vendor_sell") is not None and (type(v["vendor_sell"]) is not int or not 0 <= v["vendor_sell"] <= 2**53-1):
                    raise ValueError("Неверная цена продажи NPC")
            for row in rows:
                fields = row.split(":")
                if len(fields) != 6 or any(not f.isdigit() or len(f)>16 for f in fields):
                    raise ValueError("Неверная строка скана")
                numbers = [int(f) for f in fields]
                if any(n > 2**53-1 for n in numbers) or min(numbers[:2]) <= 0 or not 1 <= numbers[5] <= len(variants):
                    raise ValueError("Неверные числа скана")
            if self.data.get("full") and (not rows or self.data.get("error") or self.data.get("skipped", 0)
                                         or self.data.get("reported_count", len(rows)) != len(rows)):
                raise ValueError("Неполный снимок помечен полным")
        if self.kind in {"npc", "recipe"}:
            name = "item_id" if self.kind == "npc" else "spell_id"
            if type(self.data.get(name)) is not int or self.data[name] <= 0:
                raise ValueError("Неверный ID наблюдения")
        if self.kind == "npc" and not self.data.get("blocked"):
            for name in ("batch_price_copper", "batch_quantity"):
                if type(self.data.get(name)) is not int or not 0 <= self.data[name] <= 2**53-1:
                    raise ValueError("Неверная цена или размер упаковки NPC")
            if self.data["batch_quantity"] <= 0:
                raise ValueError("Размер упаковки должен быть положительным")
        if self.kind in {"recipe", "disenchant", "craft"}:
            for name in ("inputs", "outputs"):
                lines = self.data.get(name) or []
                if not isinstance(lines, list) or len(lines)>100:
                    raise ValueError("Неверный список материалов")
                for line in lines:
                    if not isinstance(line, dict) or set(line)-{"item_id", "quantity", "variant"} or type(line.get("item_id")) is not int or type(line.get("quantity")) is not int or min(line["item_id"], line["quantity"]) <= 0 or max(line["item_id"], line["quantity"]) > 2**31-1:
                        raise ValueError("Неверный состав наблюдения")
                    if not isinstance(line.get("variant", ""), str) or len(line.get("variant", ""))>300:
                        raise ValueError("Неверный вариант материала")
        if self.kind == "diagnostic":
            if not re.fullmatch(r"[A-Z_]{1,80}", str(self.data.get("code", ""))):
                raise ValueError("Неверный код диагностики")
            for name in ("scans", "rows", "queue"):
                if name in self.data and (type(self.data[name]) is not int or not 0 <= self.data[name] <= 2**53-1):
                    raise ValueError("Неверный счётчик диагностики")
            errors = self.data.get("error_counts") or {}
            if not isinstance(errors, dict) or len(errors)>100 or any(not re.fullmatch(r"[A-Z_]{1,80}", k) or type(v) is not int or not 0 <= v <= 2**53-1 for k, v in errors.items()):
                raise ValueError("Неверные счётчики ошибок")
        if len(canonical(self.data)) > 60_000_000:
            raise ValueError("Наблюдение превышает лимит")
        return self

    def identity(self):
        return hashlib.sha256(canonical(self.model_dump(mode="json"))).hexdigest()

    def counts(self):
        return {"records": 1, "rows": self.data.get("count", 0) if self.kind == "scan" else 0,
                "full_scans": int(self.kind == "scan" and self.data.get("full") is True),
                "partial_scans": int(self.kind == "scan" and self.data.get("full") is not True),
                **{k: int(self.kind == k) for k in ("npc", "recipe", "disenchant", "craft", "diagnostic")}}


class Connection(Model):
    enabled: bool = False
    url: str = Field(default="", max_length=1000)
    token: str = Field(default="", max_length=200)
    admin_token: str = Field(default="", max_length=200)
