"""Portable sniper rules, durable game reports and explicit three-day references."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from ahledger.util import dumps
from cennik.core.models import Model

from .models import MARKET

MAGIC = "CENNIK-SNIPE-1"
MAX_INTEGER = 2**53 - 1


class SnipeRule(Model):
    id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[A-Za-z0-9_-]{1,80}$")
    market: Literal["forever.pvp.alliance.us"] = "forever.pvp.alliance.us"
    item_id: int = Field(gt=0, le=2**31-1)
    quantity: int = Field(default=1, ge=1, le=1000000)
    mode: Literal["any", "limit", "median3d"] = "any"
    unit_limit_copper: int = Field(default=0, ge=0, le=MAX_INTEGER)
    discount_bps: int = Field(default=5000, ge=0, le=10000)
    priority: Literal["high", "normal", "low"] = "normal"
    use_bags: bool = False
    item_level: int | None = Field(default=None, ge=0, le=100000)
    item_suffix: int | None = Field(default=None, ge=-(2**31), le=2**31-1)
    pet_id: int | None = Field(default=None, ge=0, le=2**31-1)

    @model_validator(mode="after")
    def valid_limit(self):
        if self.use_bags and self.item_level is not None:
            raise ValueError("Для точного варианта создайте цель без вычитания сумок")
        if self.mode == "limit" and self.unit_limit_copper <= 0:
            raise ValueError("Укажите положительный предел цены за штуку")
        if any(v is not None for v in (self.item_level, self.item_suffix, self.pet_id)) and any(
            v is None for v in (self.item_level, self.item_suffix, self.pet_id)
        ):
            raise ValueError("Укажите полный ключ варианта или только ID предмета")
        return self


class SnipeImport(Model):
    text: str = Field(max_length=200000)


class SnipeBudget(Model):
    budget_copper: int = Field(gt=0, le=MAX_INTEGER)


class SnipeMaterials(Model):
    item_id: int = Field(gt=0)
    spell_id: int = Field(gt=0)
    version_hash: str = Field(max_length=128)
    batches: int = Field(default=1, ge=1, le=10000)
    use_bags: bool = False
    mode: Literal["any", "median3d"] = "any"
    discount_bps: int = Field(default=5000, ge=0, le=10000)


def encode_rules(rules: list[SnipeRule], budget: int) -> str:
    lines = [f"{MAGIC}\t{budget}"]
    for r in rules:
        lines.append("\t".join(str(v) for v in (r.id, r.market, r.item_id, r.quantity, r.mode,
            r.unit_limit_copper, r.discount_bps, r.priority, int(r.use_bags),
            "" if r.item_level is None else r.item_level,
            "" if r.item_suffix is None else r.item_suffix, "" if r.pet_id is None else r.pet_id)))
    return "\n".join(lines)


def decode_rules(text: str) -> tuple[int, list[SnipeRule]]:
    if len(text.encode()) > 200000:
        raise ValueError("Список превышает 200 КБ")
    lines = text.strip("\r\n\ufeff ").splitlines()
    if not lines or len(lines) > 501:
        raise ValueError("Список должен содержать до 500 целей")
    header = lines[0].split("\t")
    if len(header) != 2 or header[0] != MAGIC or not re.fullmatch(r"[0-9]+", header[1]):
        raise ValueError("Ожидается список CENNIK-SNIPE-1")
    budget = int(header[1])
    if not 0 < budget <= MAX_INTEGER:
        raise ValueError("Неверный бюджет")
    rules, ids = [], set()
    for line in lines[1:]:
        f = line.split("\t")
        if len(f) != 12 or f[8] not in {"0", "1"}:
            raise ValueError("Неверный формат строки цели")
        rule = SnipeRule.model_validate(dict(id=f[0], market=f[1], item_id=int(f[2]), quantity=int(f[3]), mode=f[4],
            unit_limit_copper=int(f[5]), discount_bps=int(f[6]), priority=f[7], use_bags=f[8] == "1",
            item_level=int(f[9]) if f[9] else None, item_suffix=int(f[10]) if f[10] else None,
            pet_id=int(f[11]) if f[11] else None))
        if rule.id in ids:
            raise ValueError("Повтор ID цели")
        ids.add(rule.id)
        rules.append(rule)
    return budget, rules


class SniperBook:
    def __init__(self, workspace):
        self.w = workspace
        self._catalog: dict | None = None
        with self.w.store.connection() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS sniper_rules(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sniper_meta(id INTEGER PRIMARY KEY,revision TEXT,budget INTEGER);
                CREATE TABLE IF NOT EXISTS sniper_reports(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sniper_references(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sniper_progress(id INTEGER PRIMARY KEY,payload TEXT NOT NULL);""")
            db.execute("CREATE TABLE IF NOT EXISTS sniper_recipe_requests(id TEXT PRIMARY KEY,item_id INTEGER,completed INTEGER DEFAULT 0)")
            db.execute("INSERT OR IGNORE INTO sniper_meta VALUES(1,?,?)", [uuid4().hex, self.w.settings().budget_copper])

    @staticmethod
    def check_identity(old: str | None, rule: SnipeRule):
        if old:
            previous = SnipeRule.model_validate_json(old)
            if any(getattr(previous, key) != getattr(rule, key) for key in
                   ("market", "item_id", "item_level", "item_suffix", "pet_id", "use_bags")):
                raise ValueError("Предмет, вариант и режим сумок существующей цели менять нельзя; создайте новую")

    def listing(self, *, wire=False):
        with self.w.store.connection() as db:
            meta = db.execute("SELECT revision,budget FROM sniper_meta WHERE id=1").fetchone()
            rules = [SnipeRule.model_validate_json(row[0]) for row in db.execute("SELECT payload FROM sniper_rules ORDER BY id")]
            reports = [] if wire else [json.loads(row[0]) for row in db.execute("SELECT payload FROM sniper_reports ORDER BY rowid DESC LIMIT 100")]
            references = {row[0]: json.loads(row[1]) for row in db.execute("SELECT * FROM sniper_references")}
            progress = db.execute("SELECT payload FROM sniper_progress WHERE id=1").fetchone()
        if wire:
            return {"revision": meta[0], "workspace_id": self.w.identity, "budget_copper": meta[1],
                "rules": [r.model_dump(mode="json") for r in rules], "references": references}
        names = {r["item_id"]: r["name"] for r in self.w.market.rows(
            "SELECT item_id,name FROM items WHERE item_id IN (SELECT unnest(?)) AND name IS NOT NULL",
            [[r.item_id for r in rules]])} if rules else {}
        return {"revision": meta[0], "workspace_id": self.w.identity, "budget_copper": meta[1],
            "rules": [r.model_dump(mode="json") for r in rules], "references": references, "reports": reports,
            "names": names, "game": json.loads(progress[0]) if progress else None}

    def wire(self):
        return self.listing(wire=True)

    def budget(self, value: int):
        with self.w.store.connection() as db:
            db.execute("UPDATE sniper_meta SET revision=?,budget=?", [uuid4().hex, value])
        return {"budget_copper": value}

    def save(self, rule: SnipeRule):
        with self.w.store.connection() as db:
            count = db.execute("SELECT count(*) FROM sniper_rules").fetchone()[0]
            exists = db.execute("SELECT payload FROM sniper_rules WHERE id=?", [rule.id]).fetchone()
            self.check_identity(exists[0] if exists else None, rule)
            if count >= 500 and not exists:
                raise ValueError("Максимум 500 целей")
            db.execute("INSERT OR REPLACE INTO sniper_rules VALUES(?,?)", [rule.id, rule.model_dump_json()])
            db.execute("UPDATE sniper_meta SET revision=?", [uuid4().hex])
        return rule

    def delete(self, identity: str):
        with self.w.store.connection() as db:
            db.execute("DELETE FROM sniper_rules WHERE id=?", [identity])
            db.execute("DELETE FROM sniper_references WHERE id=?", [identity])
            db.execute("UPDATE sniper_meta SET revision=?", [uuid4().hex])
        return {"removed": identity}

    def preview(self, text: str):
        budget, rules = decode_rules(text)
        current = {r["id"]: r for r in self.wire()["rules"]}
        for rule in rules:
            self.check_identity(dumps(current[rule.id]) if rule.id in current else None, rule)
        return {"budget_copper": budget, "items": [{**r.model_dump(mode="json"),
            "action": "update" if r.id in current else "add"} for r in rules],
            "note": "Покупки существующих целей сохраняются. Новые цели в игре приостановлены."}

    def apply(self, text: str):
        budget, rules = decode_rules(text)
        with self.w.store.connection() as db:
            current = {r[0] for r in db.execute("SELECT id FROM sniper_rules")}
            if len(current | {r.id for r in rules}) > 500:
                raise ValueError("После объединения будет больше 500 целей")
            for rule in rules:
                existing = db.execute("SELECT payload FROM sniper_rules WHERE id=?", [rule.id]).fetchone()
                self.check_identity(existing[0] if existing else None, rule)
                db.execute("INSERT OR REPLACE INTO sniper_rules VALUES(?,?)", [rule.id, rule.model_dump_json()])
            db.execute("UPDATE sniper_meta SET revision=?,budget=?", [uuid4().hex, budget])
        return {"imported": len(rules)}

    def export(self):
        book = self.listing()
        return {"format_version": 1, "text": encode_rules([SnipeRule.model_validate(r) for r in book["rules"]], book["budget_copper"])}

    def catalog(self):
        from .data import WorkspaceProvider

        generation = self.w.market.generation()
        if self._catalog and self._catalog["generation"] == generation:
            return self._catalog
        with self.w.market.lock, WorkspaceProvider(self.w.market, MARKET, datetime.now(UTC)) as provider:
            recipes = provider.recipes()
            items = self.w.market.rows("SELECT item_id,name,vendor_buy_copper FROM items ORDER BY item_id LIMIT 20001")
        self._catalog = {"workspace_id": self.w.identity, "generation": generation,
            "truncated": len(items) > 20000 or len(recipes) > 10000,
            "items": items[:20000], "recipes": [{"spell_id": r.spell_id, "version_hash": r.version_hash,
                "item_id": r.result_item_id, "profession": r.profession, "skill_required": r.skill_required,
                "qty_min": r.qty_min, "qty_max": r.qty_max,
                "materials": [v.model_dump() for v in r.inputs]} for r in recipes[:10000] if r.inputs]}
        return self._catalog

    def recipes_for(self, item_id: int):
        catalog = self.catalog()
        names = {r["item_id"]: r for r in catalog["items"]}
        return [{**r, "materials": [{**line, **{k: v for k, v in names.get(line["item_id"], {}).items()
            if k != "item_id"}} for line in r["materials"]]} for r in catalog["recipes"] if r["item_id"] == item_id]

    def material_preview(self, value: SnipeMaterials):
        recipe = next((r for r in self.recipes_for(value.item_id) if r["spell_id"] == value.spell_id
            and r["version_hash"] == value.version_hash), None)
        if not recipe:
            raise ValueError("Действующий рецепт не найден: обновите карточку")
        lines = []
        for line in recipe["materials"]:
            quantity = line["quantity"] * value.batches
            if not 0 < quantity <= 1000000:
                raise ValueError("Количество материала превышает 1000000")
            lines.append({**line, "quantity": quantity, "npc": bool(line.get("vendor_buy_copper"))})
        return {"recipe": recipe, "lines": lines, "note": "Добавятся непосредственные материалы для аукциона. NPC показаны отдельно. Новые цели приостановлены."}

    def add_materials(self, value: SnipeMaterials):
        preview = self.material_preview(value)
        rules = [SnipeRule(item_id=r["item_id"], quantity=r["quantity"], use_bags=value.use_bags,
            mode=value.mode, discount_bps=value.discount_bps) for r in preview["lines"] if not r["npc"]]
        if not rules:
            raise ValueError("В этом рецепте нет материалов для поиска на аукционе")
        return self.apply(encode_rules(rules, MAX_INTEGER))

    def pending_recipe_requests(self):
        with self.w.store.connection() as db:
            return [row[0] for row in db.execute("SELECT id FROM sniper_recipe_requests WHERE completed=0")]

    def complete_recipe_requests(self, ids: list[str]):
        with self.w.store.connection() as db:
            db.executemany("UPDATE sniper_recipe_requests SET completed=1 WHERE id=?", [(identity,) for identity in ids])

    def reference(self, identity: str):
        rule = next((r for r in self.wire()["rules"] if r["id"] == identity), None)
        if not rule:
            raise ValueError("Цель не найдена")
        end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=3)
        item = rule["item_id"]
        # A one-item query avoids reading the full historical archive for a watch list.
        rows = self.w.market.rows("SELECT observed_at,median_copper,raw_id FROM price_history WHERE item_id=? AND market=? AND observed_at>=? AND observed_at<? AND median_copper>0", [item, MARKET, start, end])
        source = "AHledger history"
        paths = [str(self.w.market.root / r["quote_path"]) for r in self.w.market.rows(
            "SELECT quote_path FROM scan_registry WHERE complete AND quote_path IS NOT NULL AND market=? AND observed_at>=? AND observed_at<?",
            [MARKET, start, end])]
        if paths:
            own = self.w.market.rows("SELECT CAST(observed_at AS TIMESTAMPTZ) observed_at,median_copper,raw_id FROM read_parquet(?) WHERE item_id=? AND median_copper>0", [paths, item])
            if len({r["observed_at"].astimezone(UTC).date() for r in own}) == 3:
                rows, source = own, "Cennik replicate"
        days: dict = {}
        for row in rows:
            days.setdefault(row["observed_at"].astimezone(UTC).date(), []).append(row["median_copper"])
        expected = [(start + timedelta(days=n)).date() for n in range(3)]
        valid = all(days.get(day) for day in expected)
        value = int(median([Decimal(median(days[day])) for day in expected])) if valid else None
        result = {"copper": value, "source": source, "start": start.isoformat(), "end": end.isoformat(),
            "valid_until": (end + timedelta(days=1)).timestamp(), "days": len(days),
            "reason": None if valid else "Нужна история каждого из трёх завершённых дней; нажмите «История 3 дней»"}
        if source == "AHledger history" and valid:
            with self.w.market.lock:
                self.w.market.db.executemany("INSERT OR IGNORE INTO source_pins VALUES(?)",
                    [(raw_id,) for raw_id in {r["raw_id"] for r in rows if r.get("raw_id")}])
        with self.w.store.connection() as db:
            db.execute("INSERT OR REPLACE INTO sniper_references VALUES(?,?)", [identity, dumps(result)])
            db.execute("UPDATE sniper_meta SET revision=?", [uuid4().hex])
        return result

    def accept_game(self, data: dict):
        if not isinstance(data, dict) or data.get("workspace_id") != self.w.identity:
            return []
        events = data.get("events") or []
        if not isinstance(events, list) or len(events) > 2000:
            raise ValueError("Неверный журнал снайпера")
        state = data.get("rules") or {}
        if not isinstance(state, dict) or len(state) > 500:
            raise ValueError("Неверный список целей снайпера")
        accepted = []
        with self.w.store.connection() as db:
            revision = db.execute("SELECT revision FROM sniper_meta WHERE id=1").fetchone()[0]
            changed = False
            for event in events:
                if not isinstance(event, dict) or not re.fullmatch(r"[A-Za-z0-9_:\-/]{1,160}", str(event.get("id", ""))):
                    raise ValueError("Неверный ID события снайпера")
                if len(dumps(event).encode()) > 16000:
                    raise ValueError("Событие снайпера превышает 16 КБ")
                old = db.execute("SELECT 1 FROM sniper_reports WHERE id=?", [event["id"]]).fetchone()
                if not old and event.get("kind") == "recipe_request":
                    item_id = int(event.get("item_id") or 0)
                    if not 0 < item_id <= 2**31-1:
                        raise ValueError("Неверный предмет запроса рецептов")
                    db.execute("INSERT OR IGNORE INTO sniper_recipe_requests(id,item_id) VALUES(?,?)", [event["id"], item_id])
                if not old and event.get("kind") == "rule_update":
                    rule = SnipeRule.model_validate(event.get("rule"))
                    existing = db.execute("SELECT payload FROM sniper_rules WHERE id=?", [rule.id]).fetchone()
                    if not existing or event.get("base_revision") == revision:
                        self.check_identity(existing[0] if existing else None, rule)
                        if not existing and db.execute("SELECT count(*) FROM sniper_rules").fetchone()[0] >= 500:
                            raise ValueError("Максимум 500 целей")
                        db.execute("INSERT OR REPLACE INTO sniper_rules VALUES(?,?)", [rule.id, rule.model_dump_json()])
                        changed = True
                    else:
                        event = {**event, "conflict": "Веб-правило изменено после последнего reload; сохранён вариант веба"}
                elif not old and event.get("kind") == "rule_remove" and event.get("base_revision") == revision:
                    db.execute("DELETE FROM sniper_rules WHERE id=?", [str(event.get("rule_id"))])
                    changed = True
                elif not old and event.get("kind") == "budget" and event.get("base_revision") == revision:
                    value = SnipeBudget.model_validate({"budget_copper": event.get("budget_copper")})
                    db.execute("UPDATE sniper_meta SET budget=?", [value.budget_copper])
                    changed = True
                db.execute("INSERT OR IGNORE INTO sniper_reports VALUES(?,?)", [event["id"], dumps(event)])
                accepted.append(event["id"])
            if changed:
                db.execute("UPDATE sniper_meta SET revision=?", [uuid4().hex])
            progress = {}
            for identity, rule in state.items():
                if not isinstance(rule, dict):
                    continue
                progress[str(identity)] = {key: rule.get(key) for key in ("goal", "stock", "bought", "spent", "enabled", "last_check_epoch", "reason")}
            db.execute("INSERT OR REPLACE INTO sniper_progress VALUES(1,?)", [dumps({"received_at": datetime.now(UTC).isoformat(),
                "spent": data.get("spent"), "rules": progress})])
        return accepted
