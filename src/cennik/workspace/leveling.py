"""Independent profession previews; chosen batches enter the existing task pipeline.

No TSM source is embedded. Unknown skill-up probabilities stay unknown unless the
owner supplies an estimate. Character snapshots are scoped by the worker route.
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from ahledger.util import atomic_json, dumps
from cennik.addon.models import MAX_INTEGER, ExportRequest, Package
from cennik.core.interfaces import EvaluationContext
from cennik.core.models import Model, Opportunity, Recipe, RecipeInput
from cennik.engines.economics import auction_net
from cennik.engines.prices import PriceEngine
from cennik.strategies.disenchant import DisenchantValuation

from .production import OrderCreate

ALIASES = {
    "164": "blacksmithing", "кузнечное дело": "blacksmithing",
    "165": "leatherworking", "кожевничество": "leatherworking",
    "171": "alchemy", "алхимия": "alchemy", "197": "tailoring", "портняжное дело": "tailoring",
    "202": "engineering", "инженерное дело": "engineering", "333": "enchanting", "наложение чар": "enchanting",
    "185": "cooking", "кулинария": "cooking", "129": "first aid", "первая помощь": "first aid",
    "186": "mining", "горное дело": "mining", "755": "jewelcrafting", "ювелирное дело": "jewelcrafting",
}


def profession_key(value):
    value = str(value).strip().lower()
    return ALIASES.get(value, value)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


class CapturedRecipe(Model):
    locale: str | None = Field(default=None, max_length=10)
    item_name: str | None = Field(default=None, max_length=300)
    item_names: dict[str, str] = Field(default_factory=dict, max_length=101)
    spell_id: int = Field(gt=0, strict=True)
    name: str = Field(default="", max_length=500)
    item_id: int | None = Field(default=None, gt=0)
    qty_min: int | None = Field(default=None, gt=0, le=10000)
    qty_max: int | None = Field(default=None, gt=0, le=10000)
    inputs: list[RecipeInput] = Field(default_factory=list, max_length=100)
    difficulty: Literal["orange", "yellow", "green", "grey", "unknown"] = "unknown"
    skill_ups: int = Field(default=1, ge=1, le=10)
    observed_skill: int = Field(ge=0, le=10000)
    observed_at: int = Field(gt=0)


class ProfessionState(Model):
    name: str = Field(min_length=1, max_length=100)
    skill: int = Field(ge=0, le=10000)
    max_skill: int = Field(ge=1, le=10000)
    recipes: dict[str, CapturedRecipe] = Field(default_factory=dict, max_length=5000)
    observed_at: int = Field(gt=0)
    coverage: Literal["visible_recipes", "levels_only"] = "levels_only"

    @model_validator(mode="after")
    def coherent(self):
        if self.skill > self.max_skill or any(k != str(r.spell_id) for k, r in self.recipes.items()):
            raise ValueError("Неверный навык или ключ рецепта")
        if any(not 0 < q.item_id <= 2**31 or not 0 < q.quantity <= 1000000 for r in self.recipes.values() for q in r.inputs):
            raise ValueError("Неверные компоненты рецепта")
        return self


class ProfessionSnapshot(Model):
    locale: str | None = Field(default=None, max_length=10)
    format_version: Literal[1] = 1
    character: str = Field(min_length=1, max_length=80)
    realm: str = Field(min_length=1, max_length=100)
    observed_at: int = Field(gt=0)
    disenchant: bool = False
    professions: dict[str, ProfessionState] = Field(default_factory=dict, max_length=20)

    @model_validator(mode="after")
    def dates(self):
        stamps = [self.observed_at, *(p.observed_at for p in self.professions.values()),
                  *(r.observed_at for p in self.professions.values() for r in p.recipes.values())]
        if max(stamps) > int(datetime.now(UTC).timestamp()) + 300:
            raise ValueError("Время отчёта профессий находится в будущем")
        return self


class RecipeAccess(Model):
    item_id: int | None = Field(default=None, gt=0)
    trainer_cost_copper: int | None = Field(default=None, ge=0, le=MAX_INTEGER)
    skill_up_confirmed: bool = False


class LevelingQuery(Model):
    profile_id: str = Field(default="", max_length=80, pattern=r"^[a-zA-Z0-9_-]*$")
    profession: str = Field(min_length=1, max_length=100)
    skill: int | None = Field(default=None, ge=0, le=10000)
    crafts: int = Field(default=1, ge=1, le=1000)
    allow_disenchant: bool = False
    disenchanter_id: str = Field(default="", max_length=80, pattern=r"^[a-zA-Z0-9_-]*$")
    include_unlearned: bool = True
    yellow_chance: Decimal | None = Field(default=None, gt=0, le=1)
    green_chance: Decimal | None = Field(default=None, gt=0, le=1)
    access: dict[int, RecipeAccess] = Field(default_factory=dict, max_length=100)


class LevelingPublish(Model):
    analysis_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    variant_id: str = Field(pattern=r"^[0-9]+:(auction|npc|disenchant)$")
    max_loss_copper: int = Field(default=0, ge=0, le=MAX_INTEGER)


class ProfessionLeveling:
    def __init__(self, workspace):
        self.w = workspace
        self.root = workspace.root / "profession_leveling"
        self.lock = threading.RLock()

    def profiles(self):
        return {p.stem: json.loads(p.read_text("utf-8")) for p in (self.root / "characters").glob("*.json")}

    def ingest(self, raw, client=None):
        if not raw:
            return {"updated": False}
        value = ProfessionSnapshot.model_validate(raw)
        if client and (value.character, value.realm) != (client.character, client.realm):
            raise ValueError("Отчёт профессий принадлежит другому персонажу")
        identity = client.id if client else "local-" + digest([value.realm, value.character])[:32]
        payload = value.model_dump(mode="json")
        with self.lock:
            path = self.root / "characters" / (identity + ".json")
            previous = json.loads(path.read_text("utf-8")) if path.exists() else None
            if previous and previous["observed_at"] > value.observed_at:
                return {"updated": False}
            atomic_json(path, payload)
        return {"updated": previous != payload, "profile_id": identity}

    def overview(self):
        clients = {c.id: c for c in self.w.production.clients()}
        return {"profiles": [{"id": identity, **value,
                              "can_purchase": identity in clients and clients[identity].role == "crafter"}
                             for identity, value in self.profiles().items()],
                "budget_copper": self.w.settings().budget_copper,
                "note": "Откройте профессию в аддоне → /reload → дождитесь Worker. Данные относятся к конкретному персонажу."}

    def analyze(self, query: LevelingQuery, *, persist=True):
        clock = self.w.development_clock()
        as_of = self.w.evaluation_now()
        with self.w.provider(self.w.market.path, self.w.market_key, as_of,
                             **({"data_as_of": datetime.now(UTC)} if clock else {})) as provider:
            config = self.w.effective_config()
            prices = PriceEngine(provider, config)
            context = EvaluationContext(provider, config, "profession-preview", as_of, prices, provider.items(),
                                        disenchant=self.w.analytics.disenchant.snapshot())
            result = self.evaluate(query, context)
        if persist:
            result["analysis_id"] = uuid4().hex
            atomic_json(self.root / "analyses" / (result["analysis_id"] + ".json"),
                        {"query": query.model_dump(mode="json"), "result": result})
            # Preview files are disposable; character observations are retained.
            for path in sorted((self.root / "analyses").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[100:]:
                path.unlink(missing_ok=True)
        return result

    def evaluate(self, query, context):
        profiles = self.profiles()
        profile = profiles.get(query.profile_id)
        if query.profile_id and not profile:
            raise ValueError("Сначала загрузите профессии выбранного персонажа")
        key = profession_key(query.profession)
        state = next((p for k, p in (profile or {}).get("professions", {}).items()
                      if profession_key(k) == key or profession_key(p["name"]) == key), None)
        skill = query.skill if query.skill is not None else state["skill"] if state else None
        if skill is None:
            raise ValueError("Укажите текущий навык для предварительного просмотра")
        known = (state or {}).get("recipes", {})
        recipes = {r.spell_id: r for r in context.provider.recipes() if profession_key(r.profession or "") == key}
        for row in known.values():
            if row.get("item_id") and row.get("qty_min") and row.get("qty_max") and row.get("inputs"):
                assert state is not None
                recipes[row["spell_id"]] = Recipe(spell_id=row["spell_id"], version_hash=digest(row),
                    result_item_id=row["item_id"], qty_min=row["qty_min"], qty_max=row["qty_max"],
                    inputs=row["inputs"], name=row["name"], profession=state["name"])
        de_profile = profiles.get(query.disenchanter_id or query.profile_id)
        de_enabled = query.allow_disenchant and bool(de_profile and de_profile.get("disenchant"))
        # This is a comparison, including unprofitable routes. Missing sales
        # estimates must not hide a disposal option; prices are still checked.
        de_valuation = DisenchantValuation(replace(context, config=context.config.model_copy(
            update={"craft_liquidity_enabled": False}))) if de_enabled else None
        variants: list[dict[str, Any]] = []
        skipped: defaultdict[str, int] = defaultdict(int)
        for recipe in sorted(recipes.values(), key=lambda r: r.spell_id):
            captured = known.get(str(recipe.spell_id))
            learned = captured is not None
            if not learned and not query.include_unlearned:
                continue
            if recipe.skill_required is not None and recipe.skill_required > skill:
                skipped["skill_required"] += 1
                continue
            if not recipe.result_item_id or not recipe.qty_min or not recipe.qty_max or recipe.qty_max < recipe.qty_min or not recipe.inputs:
                skipped["incomplete_recipe"] += 1
                continue
            difficulty = captured["difficulty"] if captured and captured["observed_skill"] == skill else "unknown"
            if difficulty == "grey" or state and skill >= state["max_skill"]:
                skipped["no_skill_gain"] += 1
                continue
            access = query.access.get(recipe.spell_id, RecipeAccess())
            can_gain = difficulty in {"orange", "yellow", "green"} or not learned and access.skill_up_confirmed
            chance = Decimal(1) if difficulty == "orange" else query.yellow_chance if difficulty == "yellow" else query.green_chance if difficulty == "green" else None
            ups = captured["skill_ups"] if captured else 1
            expected = chance * ups * query.crafts if chance is not None else None
            base_warnings = ["Сложность относится к текущему навыку; после повышения нужна повторная проверка."]
            if expected is None:
                base_warnings.append("Вероятность повышения навыка неизвестна; стоимость очка не рассчитана.")
            elif difficulty != "orange":
                base_warnings.append("Вероятность повышения задана вами; это оценка.")
            quantities: defaultdict[int, int] = defaultdict(int)
            for line in recipe.inputs:
                quantities[line.item_id] += line.quantity * query.crafts
            materials, cost, blockers, cost_unknown = [], 0, [], False
            for item_id, quantity in quantities.items():
                price = context.prices.current.get(item_id)
                if context.prices.usable(price):
                    blockers.append(f"Нет свежей цены компонента #{item_id}")
                    cost_unknown = True
                    continue
                item = context.items.get(item_id)
                materials.append({"item_id": item_id, "item_name": item.name if item and item.name else str(item_id),
                                  "quantity": quantity, "unit_price_copper": price.min_copper, "source": "auction"})
                cost += price.min_copper * quantity
                if price.quantity is None:
                    base_warnings.append(f"Объём компонента #{item_id} не подтверждён; потребуется проверка в игре.")
                elif price.quantity < quantity:
                    blockers.append(f"Недостаточно компонента #{item_id} в наблюдаемых предложениях")
            fixed_cost, acquisition, recipe_line = 0, "Изучен", None
            if not learned:
                choices: list[tuple[int, str, dict[str, Any] | None]] = []
                if access.trainer_cost_copper is not None:
                    choices.append((access.trainer_cost_copper, "Обучение: цена указана вами", None))
                book = context.prices.current.get(access.item_id) if access.item_id else None
                if book and not context.prices.usable(book):
                    choices.append((book.min_copper, f"Купить рецепт #{access.item_id} на аукционе",
                                    {"item_id": access.item_id, "item_name": f"Рецепт #{access.item_id}",
                                     "quantity": 1, "unit_price_copper": book.min_copper, "source": "auction"}))
                if choices:
                    extra, acquisition, recipe_line = min(choices, key=lambda x: x[0])
                    cost += extra
                    if recipe_line:
                        materials.append(recipe_line)
                    else:
                        fixed_cost = extra
                else:
                    acquisition = "Не изучен; укажите предмет-рецепт или цену обучения"
                    blockers.append("Стоимость получения рецепта неизвестна")
                    cost_unknown = True
                if not can_gain:
                    blockers.append("Подтвердите доступность рецепта и повышение навыка после изучения")
                base_warnings.append("Перед крафтом необходимо изучить рецепт; требования и источник нужно проверить.")
            quantity = recipe.qty_min * query.crafts
            item = context.items.get(recipe.result_item_id)
            exits: list[tuple[str, int, int, dict[str, Any] | None, list[str]]] = []
            price = context.prices.current.get(recipe.result_item_id)
            bound = item and item.binding and item.binding.lower() in {"bop", "bind on pickup", "bind_on_pickup", "on_pickup", "1", "soulbound"}
            if not bound and not context.prices.usable(price):
                gross = price.min_copper * quantity
                exits.append(("auction", gross, auction_net(gross, context.config.auction_cut_percent), None,
                              ["Цена аукциона не гарантирует продажу всей партии."]))
            if item and item.vendor_sell_copper is not None and item.vendor_sell_copper > 0:
                gross = item.vendor_sell_copper * quantity
                exits.append(("npc", gross, gross, None, []))
            if de_valuation is not None:
                valuation, problem = de_valuation.quote(recipe.result_item_id, quantity)
                if not problem and valuation is not None:
                    exits.append(("disenchant", valuation["gross"], valuation["net"], valuation,
                                  ["Результат распыления случайный; навык и распыляемость подтвердите в игре.",
                                   "Модель распыления требует проверки на релизе; спрос на материалы не гарантирован."]))
            if not exits:
                skipped["no_exit_price"] += 1
            for exit_kind, gross, net, de, warnings in exits:
                reasons = list(blockers)
                if not can_gain:
                    reasons.append("Нет актуального подтверждения повышения навыка")
                if not state or query.skill is not None and query.skill != state["skill"]:
                    reasons.append("Ручной навык: доступен только просмотр, обновите данные персонажа")
                if exit_kind == "disenchant" and (query.disenchanter_id or query.profile_id) != query.profile_id:
                    reasons.append("Для отправки выберите распыление этим же персонажем; передача другому здесь не создаётся")
                if not cost:
                    reasons.append("Нет полного расчёта стоимости закупки")
                profit = net - cost if not cost_unknown else None
                row = {"id": f"{recipe.spell_id}:{exit_kind}", "spell_id": recipe.spell_id,
                       "name": recipe.name or (item.name if item else None) or str(recipe.result_item_id),
                       "item_id": recipe.result_item_id, "learned": learned, "difficulty": difficulty,
                       "crafts": query.crafts, "quantity": quantity, "exit": exit_kind,
                       "cost_copper": cost if not cost_unknown else None, "gross_copper": gross, "net_copper": net,
                       "profit_copper": profit, "expected_skill_ups": str(expected) if expected is not None else None,
                       "cost_per_skill_copper": str(Decimal(-profit) / expected) if profit is not None and expected else None,
                       "acquisition": acquisition, "materials": materials, "fixed_cost_copper": fixed_cost,
                       "recipe": recipe.model_dump(mode="json"), "disenchant": de,
                       "warnings": base_warnings + warnings, "blockers": reasons,
                       "can_publish": not reasons and bool(query.profile_id)}
                variants.append(row)
        variants.sort(key=lambda r: (r["cost_per_skill_copper"] is None,
                                    Decimal(r["cost_per_skill_copper"]) if r["cost_per_skill_copper"] is not None else 0,
                                    -(r["profit_copper"] if r["profit_copper"] is not None else -MAX_INTEGER)))
        return {"market": self.w.market_key, "generation": self.w.generation(), "profile_hash": digest(profile),
                "skill": skill, "profession": query.profession, "variants": variants,
                "skipped": dict(skipped), "disenchant_enabled": de_enabled,
                "disenchant_warning": "Распыление не подтверждено у выбранного персонажа" if query.allow_disenchant and not de_enabled else None}

    def publish(self, request: LevelingPublish):
        path = self.root / "analyses" / (request.analysis_id + ".json")
        if not path.exists():
            raise ValueError("Предварительный расчёт не найден; обновите список")
        stored = json.loads(path.read_text("utf-8"))
        query = LevelingQuery.model_validate(stored["query"])
        with self.lock, self.w.production.lock:
            clients = self.w.production.clients()
            client = next((c for c in clients if c.id == query.profile_id and c.role == "crafter"), None)
            if not client or not any(c.role == "auctioneer" for c in clients):
                raise ValueError("Выберите подключённого крафтера Worker; нужен настроенный аукционер")
            if not self.w.analytics.lock.acquire(blocking=False):
                raise RuntimeError("Дождитесь завершения аналитики")
            try:
                status = self.w.exchange.ready()
                current = self.analyze(query, persist=False)
                if digest(current) != digest({k: v for k, v in stored["result"].items() if k != "analysis_id"}):
                    raise ValueError("Данные или профессия изменились; обновите список перед отправкой")
                row = next((r for r in current["variants"] if r["id"] == request.variant_id), None)
                if not row or not row["can_publish"]:
                    raise ValueError("Вариант неполный или навык не подтверждён")
                if row["cost_copper"] > self.w.settings().budget_copper or -row["profit_copper"] > request.max_loss_copper:
                    raise ValueError("Партия превышает бюджет или допустимый убыток прокачки")
                run = self.w.store.latest(self.w.market_key, published=True, summary=True)
                assert run is not None
                identity = digest([request.analysis_id, row["spell_id"]])[:32]
                request_id = "leveling-" + identity
                payload_hash = digest([request.model_dump(mode="json"), query.model_dump(mode="json")])
                with self.w.store.connection() as db:
                    old = db.execute("SELECT request_hash,payload FROM addon_packages WHERE request_id=?", [request_id]).fetchone()
                if old:
                    if old[0] != payload_hash:
                        raise ValueError("Этот рецепт уже отправлен из данного расчёта с другими условиями")
                    package = Package.model_validate_json(old[1])
                else:
                    details = {"materials": row["materials"], "recipe": row["recipe"],
                               "leveling": {"exit": row["exit"], "skill": current["skill"], "profile_id": client.id,
                                            "max_loss_copper": request.max_loss_copper, "fixed_cost_copper": row["fixed_cost_copper"],
                                            "vendor_sell_copper": row["gross_copper"] // row["quantity"] if row["exit"] == "npc" else None},
                               "quantity_model": "forever_units_v1"}
                    if row["disenchant"]:
                        details["disenchant"] = {**row["disenchant"], "quantity": row["quantity"]}
                    created = datetime.now(UTC)
                    opportunity = Opportunity(id=identity, run_id=run.run_id, strategy_id="profession_leveling",
                        strategy_version="1.0.0", market=self.w.market_key, item_id=row["item_id"], item_name=row["name"],
                        action="profession_leveling_" + row["exit"], observed_at=created, generated_at=created,
                        buy_price_copper=0, reference_price_copper=row["gross_copper"], quantity=row["quantity"],
                        required_capital_copper=row["cost_copper"], potential_profit_copper=row["profit_copper"],
                        roi_percent=Decimal(row["profit_copper"]) * 100 / row["cost_copper"], confidence="limited",
                        risk_flags=["skill_gain_and_sale_unconfirmed"], reason="Прокачка профессии: партия выбрана владельцем",
                        source="profession_leveling", data_age_seconds=0, costs_included=["materials", "recipe_access", "auction_cut"],
                        costs_excluded=["lost_deposits", "crafting_time"], details=details)
                    task = self.w.exchange.task(opportunity, run, ExportRequest(request_id=request_id,
                        opportunity_ids=[identity]), created)
                    package = Package(package_id=uuid4().hex, market=self.w.market_key, run_id=run.run_id,
                        generation=status["generation"], max_budget_copper=task.max_total_cost_copper,
                        created_at=created, expires_at=task.expires_at, tasks=[task])
                    with self.w.store.connection() as db:
                        db.execute("INSERT INTO addon_packages VALUES(?,?,?,?,?,?,?)", [package.package_id, request_id,
                            payload_hash, self.w.market_key, run.run_id, package.expires_at.isoformat(), package.model_dump_json()])
                        db.execute("INSERT INTO addon_tasks VALUES(?,?,?,?,?,?)", [task.task_id, identity, run.run_id,
                            self.w.market_key, package.package_id, task.model_dump_json()])
                try:
                    order = self.w.production.create(OrderCreate(request_id=identity, crafter_id=client.id,
                        task_ids=[t.task_id for t in package.tasks], procurement="self_purchase"))
                except Exception:
                    if not old:
                        with self.w.store.connection() as db:
                            db.execute("DELETE FROM addon_tasks WHERE package_id=?", [package.package_id])
                            db.execute("DELETE FROM addon_packages WHERE id=?", [package.package_id])
                    raise
                return {"package_id": package.package_id, **order,
                        "note": "Задание направлено выбранному крафтеру. Дождитесь Worker и выполните /reload; покупки подтверждает игрок."}
            finally:
                self.w.analytics.lock.release()
