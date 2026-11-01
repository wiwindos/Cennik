"""Separate, file-backed research packets. Only the final selection becomes tasks."""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from ahledger.util import atomic_json, dumps
from cennik.addon.models import ExportRequest, Package
from cennik.core.models import AnalyticsConfig, Model, Opportunity, Run
from cennik.engines.economics import auction_net, roi

from .models import MARKET

MAX_CANDIDATES = 200
MAX_ITEMS = 200
MAX_ROWS = 8000
MAX_ITEM_ROWS = 1000
MAX_AGE = 1800  # research freshness; executable tasks still need live revalidation
MAX_INT = 2**53 - 1


class BulkCreate(Model):
    opportunity_ids: list[str] = Field(min_length=1, max_length=MAX_CANDIDATES)

    @model_validator(mode="after")
    def distinct(self):
        if len(set(self.opportunity_ids)) != len(self.opportunity_ids):
            raise ValueError("Повтор возможности в пакете")
        return self


class BulkChoice(Model):
    opportunity_id: str = Field(min_length=1, max_length=160)
    batches: int = Field(default=1, ge=1, le=10000, strict=True)


class BulkSelection(Model):
    choices: list[BulkChoice] = Field(min_length=1, max_length=100)
    request_id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[a-zA-Z0-9_-]{8,80}$")

    @model_validator(mode="after")
    def distinct(self):
        if len({x.opportunity_id for x in self.choices}) != len(self.choices):
            raise ValueError("Повтор выбранной возможности")
        return self


class BulkKey(Model):
    itemID: int = Field(gt=0, le=2147483647, strict=True)
    itemLevel: int = Field(default=0, ge=0, le=100000, strict=True)
    itemSuffix: int = Field(default=0, ge=-2147483647, le=2147483647, strict=True)
    battlePetSpeciesID: int = Field(default=0, ge=0, le=2147483647, strict=True)


class BulkLot(Model):
    item_key: BulkKey
    commodity: bool
    quantity: int = Field(gt=0, le=1000000, strict=True)
    unit_price: int = Field(gt=0, le=MAX_INT, strict=True)
    auction_id: int | None = Field(default=None, gt=0, le=MAX_INT, strict=True)
    ownership_known: bool
    owned: bool
    vendor_sell_copper: int | None = Field(default=None, ge=0, le=MAX_INT, strict=True)
    available_quantity: int | None = Field(default=None, ge=0, le=1000000, strict=True)

    @model_validator(mode="after")
    def precise_units(self):
        if not self.commodity and (self.auction_id is None or self.quantity != 1):
            raise ValueError("Для предметного лота нужен AuctionID и одна подтверждённая единица")
        if self.unit_price * self.quantity > MAX_INT:
            raise ValueError("Стоимость лота превышает точность Lua")
        return self


class BulkBook(Model):
    item_id: int = Field(gt=0, le=2147483647, strict=True)
    status: Literal["complete", "failed"]
    observed_at: int = Field(gt=0, strict=True)
    rows: list[BulkLot] = Field(default_factory=list, max_length=MAX_ITEM_ROWS)
    variants_count: int = Field(default=0, ge=0, le=20, strict=True)
    reason: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def consistent(self):
        ids = [r.auction_id for r in self.rows if not r.commodity]
        if len(ids) != len(set(ids)) or any(r.item_key.itemID != self.item_id for r in self.rows):
            raise ValueError("Повтор AuctionID или другой предмет в результате проверки")
        return self


class BulkReport(Model):
    format_version: Literal[1] = 1
    report_id: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,120}$")
    batch_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    workspace_id: str
    market: Literal["forever.pvp.alliance.us"]
    started_at: int = Field(gt=0, strict=True)
    completed_at: int = Field(gt=0, strict=True)
    status: Literal["complete", "partial", "cancelled"]
    items: list[BulkBook] = Field(default_factory=list, max_length=MAX_ITEMS)
    reason: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def bounded(self):
        if len({b.item_id for b in self.items}) != len(self.items) or sum(len(b.rows) for b in self.items) > MAX_ROWS:
            raise ValueError("Повтор предмета или слишком большой результат bulk")
        if self.completed_at < self.started_at:
            raise ValueError("Неверное время проверки")
        return self


def materials(o: Opportunity) -> list[dict]:
    if o.strategy_id in {"vendor_arbitrage", "historical_discount", "buy_disenchant"}:
        return [{"item_id": o.item_id, "quantity": o.quantity, "source": "auction"}]
    return o.details.get("materials", [])


def needed_items(o: Opportunity) -> set[int]:
    ids = {r["item_id"] for r in materials(o) if r["source"] == "auction"}
    if o.details.get("disenchant"):
        ids.update(r["material_item_id"] for r in o.details["disenchant"]["yields"])
    elif o.strategy_id not in {"vendor_arbitrage", "historical_discount"}:
        ids.add(o.item_id)
    return ids


class BulkChecks:
    def __init__(self, workspace):
        self.w = workspace
        self.directory = workspace.root / "bulk_checks"
        self.lock = threading.RLock()
        self.recent_acknowledgements: list[str] = []

    def path(self, identity: str) -> Path:
        if len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("Неверный ID пакета проверки")
        return self.directory / (identity + ".json")

    def load(self, identity: str) -> dict:
        path = self.path(identity)
        if not path.exists():
            raise ValueError("Пакет проверки не найден")
        if path.stat().st_size > 16_000_000:
            raise ValueError("Пакет проверки превышает 16 МБ")
        return json.loads(path.read_text("utf-8"))

    def listing(self) -> dict:
        rows = []
        for path in self.directory.glob("*.json"):
            if path.stem == "active":
                continue
            data = self.load(path.stem)
            report = data.get("report")
            rows.append({"batch_id": data["batch_id"], "created_at": data["created_at"],
                         "created_at_ms": data.get("created_at_ms", data["created_at"]*1000),
                         "candidates": len(data["candidates"]), "unique_items": len(data["item_ids"]),
                         "status": report["status"] if report else "awaiting_scan"})
        rows.sort(key=lambda r: (r["created_at_ms"], r["batch_id"]), reverse=True)
        return {"items": rows[:30], "limits": {"candidates": MAX_CANDIDATES, "unique_items": MAX_ITEMS,
                "rows": MAX_ROWS, "rows_per_item": MAX_ITEM_ROWS, "research_max_age_seconds": MAX_AGE}}

    def create(self, request: BulkCreate) -> dict:
        addon, _ = self.w.game_paths()
        toc = addon / "CennikAddon.toc"
        if not (addon / "Bulk.lua").is_file() or not toc.is_file() or "Bulk.lua" not in toc.read_text("utf-8"):
            raise ValueError("Обновите аддон через «Источники / Настройки»: для массовой проверки нужен CennikAddon 0.9.0")
        if not self.w.analytics.lock.acquire(blocking=False):
            raise RuntimeError("Дождитесь завершения расчёта")
        try:
            status = self.w.exchange.ready()
            run = Run.model_validate(status["published_run"])
            candidates, ids = [], set()
            for identity in request.opportunity_ids:
                o = self.w.store.detail(MARKET, identity)
                if o is None or o.run_id != run.run_id:
                    raise ValueError("Выбор относится к прежнему расчёту; обновите список")
                if not materials(o):
                    raise ValueError("Нет полного состава закупки: " + o.item_name)
                candidates.append(o.model_dump(mode="json"))
                ids.update(needed_items(o))
            if not ids or len(ids) > MAX_ITEMS:
                raise ValueError(f"Пакету нужно {len(ids)} уникальных предметов; предел {MAX_ITEMS}. Уменьшите выбор")
            created_ms = int(datetime.now(UTC).timestamp()*1000)
            created = created_ms // 1000
            batch = {"format_version": 1, "batch_id": uuid4().hex, "workspace_id": self.w.identity,
                     "market": MARKET, "run_id": run.run_id, "generation": run.generation,
                     "created_at": created, "created_at_ms": created_ms, "expires_at": created + 86400, "item_ids": sorted(ids),
                     "candidates": candidates, "report": None, "acknowledged_reports": {},
                     "publications": []}
            with self.lock:
                atomic_json(self.path(batch["batch_id"]), batch)
                atomic_json(self.directory / "active.json", {"batch_id": batch["batch_id"]})
            return {"batch_id": batch["batch_id"], "candidates": len(candidates), "unique_items": len(ids)}
        finally:
            self.w.analytics.lock.release()

    def wire(self) -> dict:
        with self.lock:
            active = self.directory / "active.json"
            packet = None
            if active.exists():
                batch = self.load(json.loads(active.read_text("utf-8"))["batch_id"])
                packet = {k: batch[k] for k in ("format_version", "batch_id", "workspace_id", "market",
                           "run_id", "created_at", "expires_at", "item_ids")}
                packet.update(max_rows=MAX_ROWS, max_item_rows=MAX_ITEM_ROWS, max_variants=20)
            acknowledgements = self.recent_acknowledgements + [identity for row in self.listing()["items"]
                                for identity in self.load(row["batch_id"])["acknowledged_reports"]]
            return {"packet": packet, "acknowledged_report_ids": sorted(set(acknowledgements))}

    def accept(self, raw) -> dict:
        self.recent_acknowledgements = []
        if raw in (None, {}):
            return {"accepted": 0}
        if not isinstance(raw, dict):
            raise ValueError("Неверная коллекция отчётов bulk")
        reports = raw.get("reports") or []
        if not isinstance(reports, list) or len(reports) > 2:
            raise ValueError("Неверная очередь результатов bulk")
        accepted = 0
        for entry in reports:
            # Empty Lua tables have no array/dictionary distinction.
            entry = dict(entry)
            entry["items"] = entry.get("items") or []
            for book in entry["items"]:
                book["rows"] = book.get("rows") or []
            report = BulkReport.model_validate(entry)
            if report.workspace_id != self.w.identity or report.market != MARKET:
                continue  # leave foreign reports unacknowledged
            with self.lock:
                batch = self.load(report.batch_id)
                digest = hashlib.sha256(report.model_dump_json().encode()).hexdigest()
                previous = batch["acknowledged_reports"].get(report.report_id)
                if previous:
                    if previous != digest:
                        raise ValueError("ID отчёта bulk повторён с другим содержимым")
                    self.recent_acknowledgements.append(report.report_id)
                    continue
                now = int(datetime.now(UTC).timestamp())
                if (report.started_at < batch["created_at"] - 60 or report.started_at > batch["expires_at"]
                    or report.completed_at > now + 300 or report.completed_at - report.started_at > 86400
                    or any(b.item_id not in batch["item_ids"] or b.observed_at < report.started_at
                           or b.observed_at > report.completed_at for b in report.items)):
                    raise ValueError("Предметы или время отчёта bulk не соответствуют пакету")
                if report.status == "complete" and (len(report.items) != len(batch["item_ids"])
                    or any(b.status != "complete" for b in report.items)):
                    raise ValueError("Полный отчёт bulk не содержит всех полных результатов")
                old = batch.get("report")
                if old is None or (report.started_at, report.completed_at) >= (old["started_at"], old["completed_at"]):
                    batch["report"] = report.model_dump(mode="json")
                atomic_json(self.directory / "reports" / (hashlib.sha256(report.report_id.encode()).hexdigest()+".json"),
                            report.model_dump(mode="json"))
                batch["acknowledged_reports"][report.report_id] = digest
                atomic_json(self.path(report.batch_id), batch)
                self.recent_acknowledgements.append(report.report_id)
                accepted += 1
        return {"accepted": accepted}

    def _books(self, batch: dict) -> dict[int, BulkBook]:
        report = batch.get("report")
        return {b["item_id"]: BulkBook.model_validate(b) for b in report["items"]} if report else {}

    @staticmethod
    def _rows(book: BulkBook) -> list[BulkLot]:
        return sorted((r for r in book.rows if r.ownership_known and not r.owned),
                      key=lambda r: (r.unit_price, r.auction_id or 0))

    @staticmethod
    def liquidity_limit(o: Opportunity) -> int | None:
        disenchant = o.details.get("disenchant")
        outputs = [(r.get("liquidity", {}), Decimal(r["expected_count"]) * disenchant["quantity"])
                   for r in disenchant["yields"]] if disenchant else [(o.details.get("liquidity", {}), Decimal(o.quantity))]
        limits = []
        for evidence, quantity in outputs:
            if evidence.get("enabled"):
                sales = Decimal(evidence.get("conservative_sales_per_day") or "0")
                percent = Decimal(evidence.get("thresholds", {}).get("max_batch_sales_percent", "0"))
                limits.append(int(sales * percent / 100 / quantity) if quantity > 0 else 0)
        return min(limits) if limits else None

    def calculate(self, o: Opportunity, batches: int, books: dict[int, BulkBook],
                  used: dict[int, int] | None = None, *, inspect_capacity=True,
                  policy: tuple[AnalyticsConfig, int] | None = None) -> dict:
        used = used if used is not None else {}
        config, budget = policy or (self.w.store.settings().config, self.w.settings().budget_copper)
        errors, warnings, lines = [], ["Сбыт и ликвидность не проверялись повторно; объём AH не гарантирует продажу"], []
        current = int(datetime.now(UTC).timestamp())
        for item in needed_items(o):
            book = books.get(item)
            if book is None or book.status != "complete":
                errors.append(f"Нет полной проверки #{item}")
            elif current - book.observed_at > MAX_AGE:
                errors.append(f"Проверка #{item} старше 30 минут")
        if errors:
            return {"opportunity_id": o.id, "item_name": o.item_name, "strategy_id": o.strategy_id,
                    "batches": batches, "complete": False, "problems": errors, "warnings": warnings,
                    "profit_copper": None, "available_batches": None, "budget_batches": None}
        capacities, total = [], 0
        vendor_gross = 0
        for entry in materials(o):
            item_id, required = entry["item_id"], entry["quantity"] * batches
            if entry["source"] == "npc":
                pack, cost = entry.get("npc_batch_quantity", 1), entry.get("npc_batch_price_copper")
                cost = ((required + pack - 1) // pack) * cost if cost else required * entry["unit_price_copper"]
                warnings.append(f"Цена и наличие NPC #{item_id} не подтверждены")
                lines.append({**entry, "quantity": required, "cost_copper": cost,
                              "unit_price_copper": (cost + required - 1) // required})
                total += cost
                continue
            book, skip, left, cost, offers = books[item_id], used.get(item_id, 0), required, 0, []
            rows = self._rows(book)
            capacities.append(max(0, sum(r.quantity for r in rows) - skip) // entry["quantity"])
            if len(rows) < len(book.rows):
                warnings.append(f"Собственные лоты и предложения с неизвестным владельцем #{item_id} исключены")
            if any(r.available_quantity and r.available_quantity > r.quantity for r in rows):
                warnings.append(f"#{item_id}: объём поисковых групп больше числа точных AuctionID; учтены только точные лоты")
            for row in rows:
                ignored = min(skip, row.quantity)
                skip -= ignored
                take = min(left, row.quantity - ignored)
                if take <= 0:
                    continue
                cost += take * row.unit_price
                offers.append({**row.model_dump(mode="json"), "quantity": take, "cost_copper": take * row.unit_price})
                if o.strategy_id == "vendor_arbitrage":
                    sell = row.vendor_sell_copper
                    if sell is None:
                        sell = o.details.get("vendor_sell_copper")
                        warnings.append("Цена продажи NPC взята из прежнего справочника; проверить в игре")
                    if sell is None:
                        errors.append("Неизвестна цена продажи NPC")
                    else:
                        vendor_gross += take * sell
                left -= take
                if left == 0:
                    break
            if left:
                errors.append(f"Не хватает доступных предложений #{item_id}: {required-left}/{required}")
            total += cost
            lines.append({**entry, "quantity": required, "cost_copper": cost, "offers": offers,
                          "unit_price_copper": max((r["unit_price"] for r in offers), default=1)})
        outputs = []
        def minimum(item_id):
            rows = self._rows(books[item_id])
            if not rows:
                errors.append(f"Нет положительной цены продажи на AH #{item_id}")
                return 0
            if books[item_id].variants_count > 1:
                warnings.append(f"#{item_id}: несколько вариантов; цена минимального варианта, точный вариант проверить в аддоне")
            return rows[0].unit_price
        disenchant = o.details.get("disenchant")
        if o.strategy_id == "vendor_arbitrage":
            gross = vendor_gross
        elif o.strategy_id == "historical_discount":
            gross = o.reference_price_copper * batches
            warnings.append("Цена будущей продажи — исторический ориентир, не текущая сделка")
        elif disenchant:
            for row in disenchant["yields"]:
                unit = minimum(row["material_item_id"])
                outputs.append({**row, "unit_price_copper": unit})
            gross = sum(int(Decimal(r["expected_count"]) * disenchant["quantity"] * batches * r["unit_price_copper"]) for r in outputs)
            warnings.append("Распыление: ожидаемый случайный выход по сохранённой модели")
        else:
            gross = minimum(o.item_id) * o.quantity * batches
        net = gross if o.strategy_id == "vendor_arbitrage" else auction_net(gross, config.auction_cut_percent)
        if "npc_catalog_purchase_unit_unconfirmed" in o.risk_flags:
            warnings.append("Условная цена NPC: buy за 1 шт.; единица цены не подтверждена")
        if "npc_catalog_release_refresh_required" in o.risk_flags:
            warnings.append("Справочник NPC беты требует актуализации на релизе")
        if any(x > MAX_INT for x in (total, gross, net)):
            errors.append("Суммы превышают точность Lua")
        limit = o.details.get("max_batch_by_liquidity")
        if limit is not None and o.quantity * batches > limit:
            errors.append("Партия превышает сохранённый лимит ликвидности")
        limit_batches = self.liquidity_limit(o)
        if limit_batches is not None and batches > limit_batches:
            errors.append("Партия превышает сохранённый лимит ликвидности выхода")
        result = {"opportunity_id": o.id, "item_name": o.item_name, "strategy_id": o.strategy_id,
                  "batches": batches, "quantity": o.quantity * batches, "complete": not errors,
                  "problems": sorted(set(errors)), "warnings": sorted(set(warnings)), "materials": lines,
                  "cost_copper": total, "gross_copper": gross, "net_copper": net,
                  "profit_copper": net-total if not errors else None,
                  "available_batches": min(capacities) if capacities else None,
                  "oldest_observed_at": min(books[i].observed_at for i in needed_items(o)),
                  "disenchant_outputs": outputs, "budget_batches": None}
        if inspect_capacity:
            high = min(min(capacities) if capacities else 10000, 10000)
            if limit is not None:
                high = min(high, limit // o.quantity)
            if limit_batches is not None:
                high = min(high, limit_batches)
            low = 0
            while low < high:
                middle = (low + high + 1) // 2
                trial = self.calculate(o, middle, books, dict(used), inspect_capacity=False, policy=(config, budget))
                if trial["complete"] and trial["cost_copper"] <= budget:
                    low = middle
                else:
                    high = middle-1
            result["budget_batches"] = low
            if low:
                trial = self.calculate(o, low, books, dict(used), inspect_capacity=False, policy=(config, budget))
                result["at_budget"] = {k: trial[k] for k in ("batches", "quantity", "cost_copper", "profit_copper")}
        if not errors:
            for entry in materials(o):
                if entry["source"] == "auction":
                    used[entry["item_id"]] = used.get(entry["item_id"], 0) + entry["quantity"] * batches
        return result

    def detail(self, identity: str, choices: list[BulkChoice] | None = None) -> dict:
        batch = self.load(identity)
        books = self._books(batch)
        originals = {o["id"]: Opportunity.model_validate(o) for o in batch["candidates"]}
        requested = choices or [BulkChoice(opportunity_id=i) for i in originals]
        if any(c.opportunity_id not in originals for c in requested):
            raise ValueError("Возможность не входит в пакет проверки")
        results = [self.calculate(originals[c.opportunity_id], c.batches, books) for c in requested]
        fresh_run = self.w.store.latest(MARKET, published=True, summary=True)
        current = (self.w.analytics.generation() == batch["generation"] and fresh_run is not None
                   and fresh_run.run_id == batch["run_id"] and self.w.analytics.status()["settings_match"])
        return {"batch_id": identity, "created_at": batch["created_at"], "current": current,
                "report": {k: v for k, v in batch["report"].items() if k != "items"} if batch["report"] else None,
                "books": [{"item_id": b.item_id, "status": b.status, "observed_at": b.observed_at,
                           "reason": b.reason, "rows": len(b.rows)} for b in books.values()],
                "items": results, "publications": batch["publications"]}

    def publish(self, identity: str, selection: BulkSelection) -> dict:
        if not self.w.analytics.lock.acquire(blocking=False):
            raise RuntimeError("Дождитесь завершения расчёта")
        try:
            with self.lock:
                batch = self.load(identity)
                digest = hashlib.sha256(dumps({"batch_id": identity,
                    "report_id": (batch.get("report") or {}).get("report_id"),
                    "selection": selection.model_dump(mode="json")}).encode()).hexdigest()
                with self.w.store.connection() as db:
                    old = db.execute("SELECT request_hash,payload FROM addon_packages WHERE request_id=?", [selection.request_id]).fetchone()
                if old:
                    if old[0] != digest:
                        raise ValueError("ID отправки повторён с другим выбором")
                    package = Package.model_validate_json(old[1])
                    if package.generation != self.w.analytics.generation() or package.expires_at <= datetime.now(UTC):
                        raise ValueError("Рабочий пакет устарел; создайте новую проверку")
                    return {"package_id": package.package_id, "reused": True}
                status = self.w.exchange.ready()
                if batch["generation"] != status["generation"] or batch["run_id"] != status["published_run"]["run_id"]:
                    raise RuntimeError("Изменился расчёт; создайте новую проверку")
                run = Run.model_validate(status["published_run"])
                originals = {o["id"]: Opportunity.model_validate(o) for o in batch["candidates"]}
                books, tasks, total = self._books(batch), [], 0
                used: dict[int, int] = {}
                created = datetime.now(UTC)
                request = ExportRequest(request_id=selection.request_id, opportunity_ids=[c.opportunity_id for c in selection.choices])
                for choice in selection.choices:
                    if choice.opportunity_id not in originals:
                        raise ValueError("Возможность не входит в пакет")
                    o = originals[choice.opportunity_id]
                    result = self.calculate(o, choice.batches, books, used, inspect_capacity=False)
                    if (not result["complete"] or result["profit_copper"] <= 0
                            or result["profit_copper"] < run.config.min_profit_copper):
                        raise ValueError(o.item_name + ": неполная или убыточная совместная закупка; проверьте общий расход компонентов")
                    with self.w.store.connection() as db:
                        duplicate = db.execute("""SELECT 1 FROM addon_tasks t JOIN addon_packages p ON p.id=t.package_id
                            WHERE p.expires_at>? AND (t.opportunity_id=? OR json_extract(t.payload,'$.opportunity.details.original_opportunity_id')=?)
                            AND NOT EXISTS(SELECT 1 FROM addon_events e WHERE e.task_id=t.id AND json_extract(e.payload,'$.state') IN ('completed','cancelled'))""",
                            [created.isoformat(), o.id, o.id]).fetchone()
                    if duplicate:
                        raise ValueError(o.item_name + ": уже передана в действующее рабочее задание")
                    total += result["cost_copper"]
                    if total > self.w.settings().budget_copper:
                        raise ValueError("Общая закупка превышает бюджет")
                    details = {**o.details, "materials": result["materials"], "original_opportunity_id": o.id,
                               "bulk_check": {"batch_id": identity, "report_id": batch["report"]["report_id"],
                                              "observed_at": result["oldest_observed_at"], "warnings": result["warnings"]},
                               "quantity_model": "forever_units_v1"}
                    details.update(gross_revenue_copper=result["gross_copper"], net_revenue_copper=result["net_copper"],
                                   auction_cut_copper=result["gross_copper"]-result["net_copper"])
                    if details.get("disenchant"):
                        details["disenchant"] = {**details["disenchant"], "quantity": details["disenchant"]["quantity"] * choice.batches,
                                                 "yields": result["disenchant_outputs"]}
                        details["disenchant"]["yields"] = [{**y, "expected_quantity": str(Decimal(y["expected_count"]) * details["disenchant"]["quantity"])}
                                                            for y in details["disenchant"]["yields"]]
                    if details.get("steps"):
                        details["steps"] = [{**step, **{k: step[k]*choice.batches for k in
                            ("batches", "required_quantity", "output_quantity", "surplus_quantity") if k in step},
                            "inputs": [{**x, "quantity": x["quantity"] * choice.batches} for x in step.get("inputs", [])]}
                            for step in details["steps"]]
                    if o.strategy_id not in {"vendor_arbitrage", "historical_discount", "buy_disenchant"}:
                        details["result_unit_price_copper"] = result["gross_copper"] // result["quantity"] if not details.get("disenchant") else o.details.get("result_unit_price_copper")
                    modified = o.model_copy(update={"id": o.id + "-bulk-" + selection.request_id,
                        "quantity": result["quantity"], "required_capital_copper": result["cost_copper"],
                        "buy_price_copper": result["materials"][0]["unit_price_copper"] if o.strategy_id in
                            {"vendor_arbitrage", "historical_discount", "buy_disenchant"} else o.buy_price_copper,
                        "reference_price_copper": result["gross_copper"], "potential_profit_copper": result["profit_copper"],
                        "reason": f"Массовая проверка: {result['quantity']} шт.; закупка {result['cost_copper']} copper, "
                                  f"ожидаемая прибыль {result['profit_copper']} copper. Перед покупкой требуется новая проверка в игре.",
                        "risk_flags": sorted(set(o.risk_flags + ["bulk_snapshot_requires_live_revalidation"])),
                        "roi_percent": roi(result["profit_copper"], result["cost_copper"]), "details": details})
                    task = self.w.exchange.task(modified, run, request, created)
                    task.max_total_cost_copper = result["cost_copper"]
                    tasks.append(task)
                package = Package(package_id=uuid4().hex, market=MARKET, run_id=run.run_id,
                    generation=run.generation, max_budget_copper=self.w.settings().budget_copper,
                    created_at=created, expires_at=created + timedelta(minutes=30), tasks=tasks)
                if len(package.model_dump_json().encode()) > 4_000_000:
                    raise ValueError("Рабочий пакет превышает 4 МБ; уменьшите выбор")
                with self.w.store.connection() as db:
                    db.execute("INSERT INTO addon_packages VALUES(?,?,?,?,?,?,?)", [package.package_id, selection.request_id,
                        digest, MARKET, run.run_id, package.expires_at.isoformat(), package.model_dump_json()])
                    db.executemany("INSERT INTO addon_tasks VALUES(?,?,?,?,?,?)", [(t.task_id, t.opportunity_id,
                        run.run_id, MARKET, package.package_id, t.model_dump_json()) for t in tasks])
                batch["publications"].append({"package_id": package.package_id, "at": int(created.timestamp()), "tasks": len(tasks)})
                atomic_json(self.path(identity), batch)
                return {"package_id": package.package_id, "tasks": len(tasks), "cost_copper": total, "reused": False}
        finally:
            self.w.analytics.lock.release()
