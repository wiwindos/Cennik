"""Immutable operation journal and deterministic FIFO accounting; no SQL writes."""
from __future__ import annotations

import hashlib
import heapq
import json
import threading
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from ahledger.util import atomic_json
from cennik.core.models import Model

from .models import MARKET

MAX_INTEGER = 2**53 - 1
KINDS = Literal["purchase", "sale", "craft", "disenchant", "opening", "listing", "return", "transfer", "receipt", "issue", "observation", "expense"]


class StockLine(Model):
    item_id: int = Field(gt=0, le=2147483647, strict=True)
    quantity: int = Field(gt=0, le=1000000, strict=True)
    variant: str = Field(default="", max_length=300)
    item_name: str = Field(default="", max_length=300)
    link: str = Field(default="", max_length=1500)
    reference_unit_copper: int | None = Field(default=None, ge=0, le=MAX_INTEGER, strict=True)


class LedgerEvent(Model):
    format_version: Literal[1] = 1
    event_id: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,120}$")
    workspace_id: str = Field(min_length=1, max_length=120)
    market: Literal["forever.pvp.alliance.us"] = "forever.pvp.alliance.us"
    realm: str = Field(min_length=1, max_length=160)
    character: str = Field(min_length=1, max_length=160)
    observed_at: int = Field(gt=0, strict=True)
    sequence: int = Field(default=0, ge=0, le=MAX_INTEGER, strict=True)
    kind: KINDS
    confirmation: Literal["client", "manual", "review"] = "client"
    source: str = Field(default="client", max_length=80)
    inputs: list[StockLine] = Field(default_factory=list, max_length=100)
    outputs: list[StockLine] = Field(default_factory=list, max_length=100)
    money_copper: int | None = Field(default=None, ge=0, le=MAX_INTEGER, strict=True)
    cash_delta_copper: int | None = Field(default=None, ge=-MAX_INTEGER, le=MAX_INTEGER, strict=True)
    deposit_copper: int | None = Field(default=None, ge=0, le=MAX_INTEGER, strict=True)
    returned_deposit_copper: int = Field(default=0, ge=0, le=MAX_INTEGER, strict=True)
    listing_id: str = Field(default="", max_length=120)
    target_character: str = Field(default="", max_length=160)
    task_id: str = Field(default="", max_length=180)
    strategy_id: str = Field(default="", max_length=100)
    reason: str = Field(default="", max_length=1500)
    details: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def complete(self):
        if len(json.dumps(self.details, ensure_ascii=False).encode()) > 24000:
            raise ValueError("Слишком большие подробности операции")
        if self.confirmation == "review":
            return self
        for lines in (self.inputs, self.outputs):
            if len({(x.item_id, x.variant) for x in lines}) != len(lines):
                raise ValueError("Повтор предмета/варианта в составе операции")
        if self.kind in {"purchase", "opening"} and len(self.outputs) != 1:
            raise ValueError("Для поступления нужна одна строка предмета")
        if self.kind == "purchase" and self.money_copper is None:
            raise ValueError("Неизвестна стоимость покупки")
        if self.kind == "expense" and self.money_copper is None:
            raise ValueError("Неизвестна сумма расхода")
        if self.kind == "sale" and (len(self.inputs) != 1 or self.money_copper is None):
            raise ValueError("Для продажи нужны предмет, количество и чистая выручка")
        if self.kind in {"craft", "disenchant", "transfer"} and (not self.inputs or not self.outputs):
            raise ValueError("Для преобразования нужны фактические вход и выход")
        if self.kind == "transfer":
            if not self.target_character or self.target_character == self.character:
                raise ValueError("Нужен другой собственный персонаж")
            if sorted((x.item_id, x.variant, x.quantity) for x in self.inputs) != sorted((x.item_id, x.variant, x.quantity) for x in self.outputs):
                raise ValueError("Перенос должен сохранять предметы и количество")
        if self.kind == "listing" and (len(self.outputs) != 1 or not self.listing_id):
            raise ValueError("Неизвестен выставленный лот")
        if self.kind == "return" and (not self.listing_id or len(self.outputs) != 1):
            raise ValueError("Неизвестен возвращённый лот")
        return self


class JournalRevision(Model):
    request_id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[a-zA-Z0-9_-]{8,80}$")
    expected_revision: int = Field(default=0, ge=0, strict=True)
    reason: str = Field(min_length=3, max_length=1500)
    replacement: LedgerEvent | None = None


class Accounting:
    def __init__(self, workspace):
        self.w = workspace
        self.directory = workspace.root / "accounting"
        self.lock = threading.RLock()
        self.acknowledged: list[str] = []
        self._cache_key = None
        self._cache = None

    def _path(self, identity: str):
        return self.directory / "events" / (hashlib.sha256(identity.encode()).hexdigest() + ".json")

    def load(self, identity: str) -> dict:
        path = self._path(identity)
        if not path.exists():
            raise ValueError("Операция не найдена")
        return json.loads(path.read_text("utf-8"))

    def _prices(self, event: LedgerEvent) -> dict[int, int]:
        result = {}
        for line in event.outputs:
            rows = self.w.market.rows("SELECT median_copper FROM price_snapshots WHERE market=? AND item_id=? AND median_copper>0 AND observed_at<=? ORDER BY observed_at DESC LIMIT 1", [MARKET, line.item_id, datetime.fromtimestamp(event.observed_at, UTC)])
            if rows:
                result[line.item_id] = int(rows[0]["median_copper"])
        return result

    def accept(self, raw) -> dict:
        self.acknowledged = []
        if not raw:
            return {"accepted": 0}
        if not isinstance(raw, dict) or not isinstance(raw.get("outbox", raw.get("events")) or [], list):
            raise ValueError("Неверная очередь учёта")
        entries = raw.get("outbox", raw.get("events")) or []
        if len(entries) > 500:
            raise ValueError("За синхронизацию принимается до 500 операций учёта")
        parsed = []
        for original in entries:
            data = dict(original)
            for field in ("inputs", "outputs"):
                data[field] = data.get(field) or []
            event = LedgerEvent.model_validate(data)
            if event.workspace_id != self.w.identity:
                continue
            if event.observed_at > int(datetime.now(UTC).timestamp()) + 300:
                raise ValueError("Время операции находится в будущем")
            parsed.append(event)
        accepted = 0
        with self.lock:
            # Validate the entire retry batch before writing any files.
            unique: dict[str, dict] = {}
            for event in parsed:
                payload = event.model_dump(mode="json")
                old = self.load(event.event_id) if self._path(event.event_id).exists() else None
                if old and old["original"] != payload or event.event_id in unique and unique[event.event_id] != payload:
                    raise ValueError("ID операции повторён с другим содержимым")
                unique[event.event_id] = payload
            for event in parsed:
                if not self._path(event.event_id).exists():
                    prices = self._prices(event) if event.kind == "disenchant" else {}
                    atomic_json(self._path(event.event_id), {"original": event.model_dump(mode="json"),
                        "received_at": int(datetime.now(UTC).timestamp()), "allocation_prices": prices, "revisions": []})
                    accepted += 1
                self.acknowledged.append(event.event_id)
            atomic_json(self.directory / "status.json", {"last_sync_at": int(datetime.now(UTC).timestamp()),
                "remaining": raw.get("remaining", 0), "overflow": bool(raw.get("overflow")),
                "missed": raw.get("missed", 0), "unbound": bool(raw.get("unbound")),
                "pending_operations": bool(raw.get("interrupted") or raw.get("mail_pending"))})
        return {"accepted": accepted, "remaining": raw.get("remaining", 0), "overflow": bool(raw.get("overflow"))}

    def wire(self) -> dict:
        return {"format_version": 1, "workspace_id": self.w.identity, "market": MARKET,
                "acknowledged_event_ids": list(dict.fromkeys(self.acknowledged))}

    def failed(self, message: str):
        with self.lock:
            atomic_json(self.directory / "status.json", {"last_sync_at": int(datetime.now(UTC).timestamp()), "error": message[:1500]})

    def create_manual(self, event: LedgerEvent) -> dict:
        if event.workspace_id != self.w.identity:
            raise ValueError("Операция относится к другому профилю")
        event = event.model_copy(update={"confirmation": "manual", "source": "manual"})
        return self.accept({"events": [event.model_dump(mode="json")]})

    def revise(self, identity: str, value: JournalRevision) -> dict:
        with self.lock:
            data = self.load(identity)
            for old in data["revisions"]:
                if old["request_id"] == value.request_id:
                    if old["request"] != value.model_dump(mode="json"):
                        raise ValueError("ID исправления повторён с другим содержимым")
                    return {"revision": old["revision"], "reused": True}
            if len(data["revisions"]) != value.expected_revision:
                raise ValueError("Операция уже исправлена; обновите страницу")
            replacement = value.replacement
            if replacement and (replacement.event_id != identity or replacement.workspace_id != self.w.identity):
                raise ValueError("Исправление относится к другой операции или профилю")
            if replacement and replacement.observed_at > int(datetime.now(UTC).timestamp()) + 300:
                raise ValueError("Время исправления находится в будущем")
            revision = {"request_id": value.request_id, "request": value.model_dump(mode="json"),
                        "revision": len(data["revisions"])+1, "at": int(datetime.now(UTC).timestamp())}
            data["revisions"].append(revision)
            atomic_json(self._path(identity), data)
            return {"revision": revision["revision"], "reused": False}

    def records(self) -> list[dict]:
        values = []
        for path in (self.directory / "events").glob("*.json"):
            raw = json.loads(path.read_text("utf-8"))
            effective = raw["revisions"][-1]["request"]["replacement"] if raw["revisions"] else raw["original"]
            if effective is None:
                effective = {**raw["original"], "confirmation": "review", "reason": raw["revisions"][-1]["request"]["reason"],
                             "details": {**raw["original"]["details"], "excluded": True}}
            if effective is not None:
                event = LedgerEvent.model_validate(effective)
                if raw["revisions"] and not event.details.get("excluded"):
                    event = event.model_copy(update={"confirmation": "manual"})
                values.append({"event": event, "prices": raw.get("allocation_prices", {}),
                               "revision": len(raw["revisions"])})
        def ordering(record):
            event = record["event"]
            prefix, _, suffix = event.event_id.rpartition(":")
            return (event.observed_at, prefix, event.sequence or (int(suffix) if suffix.isdigit() else 0), event.event_id)
        values.sort(key=ordering)
        # Seconds alone cannot order two game clients. Preserve each device's
        # sequence and place a linked receipt after its sender's transfer. This
        # also fixes replay when the receiver's file is imported first.
        dependencies: list[set[int]] = [set() for _ in values]
        devices: dict[str, list[tuple[int, int]]] = defaultdict(list)
        transfers = {}
        for index, record in enumerate(values):
            e = record["event"]
            prefix, _, suffix = e.event_id.rpartition(":")
            sequence = e.sequence or (int(suffix) if suffix.isdigit() else 0)
            if prefix and sequence:
                devices[prefix].append((sequence, index))
            part = e.details.get("production_part_id")
            if e.kind == "transfer" and part and e.confirmation != "review":
                transfers[part] = index
        for sequence in devices.values():
            sequence.sort()
            for (_, before), (_, after) in zip(sequence, sequence[1:], strict=False):
                dependencies[after].add(before)
        for index, record in enumerate(values):
            e = record["event"]
            sender_index = transfers.get(e.details.get("production_part_id"))
            if e.kind == "receipt" and sender_index is not None:
                dependencies[index].add(sender_index)
        children: dict[int, list[int]] = defaultdict(list)
        ready: list[int] = []
        for index, parents in enumerate(dependencies):
            if not parents:
                heapq.heappush(ready, index)
            for parent in parents:
                children[parent].append(index)
        result = []
        while ready:
            index = heapq.heappop(ready)
            result.append(values[index])
            for child in children[index]:
                dependencies[child].discard(index)
                if not dependencies[child]:
                    heapq.heappush(ready, child)
        if len(result) != len(values):
            raise ValueError("Противоречивый порядок передач; проверьте события производственных заказов")
        return result

    def replay(self) -> dict:
        with self.lock:
            paths = list((self.directory / "events").glob("*.json"))
            key = tuple(sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in paths))
            if key == self._cache_key:
                assert self._cache is not None
                return self._cache
            stocks: dict[tuple, list] = defaultdict(list)
            unresolved_stock: dict[tuple, int] = defaultdict(int)
            rows, deposits = [], {}

            def take(event, line):
                bucket = (event.realm, event.character, line.item_id)
                lots = stocks[bucket]
                variants = {lot["variant"] for lot in lots if lot["remaining"] > 0}
                ambiguous = not line.variant and len(variants) > 1
                if ambiguous:
                    unresolved_stock[bucket] += line.quantity
                possible = [] if ambiguous else [lot for lot in lots if lot["remaining"] > 0 and (lot["variant"] == line.variant or not line.variant)]
                preferred = set(event.details.get("production_task_ids") or [])
                if event.task_id:
                    preferred = {event.task_id}
                possible.sort(key=lambda lot: (0 if lot["task_id"] in preferred else 1, lot["index"]))
                left, known_cost, unknown, allocations = line.quantity, 0, 0, []
                for lot in possible:
                    count = min(left, lot["remaining"])
                    # Integer allocation preserves the final copper on partial consumption.
                    cost = lot["cost"] * count // lot["remaining"] if lot["cost"] is not None and not unresolved_stock[bucket] else None
                    lot["remaining"] -= count
                    if cost is not None:
                        lot["cost"] -= cost
                        known_cost += cost
                    else:
                        unknown += count
                    allocations.append({"origin_event_id": lot["event_id"], "task_id": lot["task_id"],
                                        "strategy_id": lot["strategy_id"], "quantity": count, "cost_copper": cost})
                    left -= count
                    if not left:
                        break
                unknown += left
                return known_cost, unknown, allocations, ambiguous

            for index, record in enumerate(self.records()):
                e = record["event"]
                row = {**e.model_dump(mode="json"), "revision": record["revision"], "cost_copper": None,
                       "profit_copper": None, "problems": [], "allocations": [], "cash_effect_copper": None}
                if e.confirmation == "review":
                    row["problems"].append(e.reason or "Требуется сверка")
                    rows.append(row)
                    continue
                if e.kind in {"craft", "disenchant", "sale", "transfer"}:
                    known, unknown = 0, 0
                    input_allocations = {}
                    for line in e.inputs:
                        cost, missing, allocations, ambiguous = take(e, line)
                        known += cost
                        unknown += missing
                        input_allocations[(line.item_id, line.variant, line.quantity)] = allocations
                        row["allocations"].extend(allocations)
                        if ambiguous:
                            row["problems"].append(f"Неоднозначный вариант #{line.item_id}")
                    row["cost_copper"] = known if not unknown else None
                    row["known_cost_copper"] = known
                    if unknown:
                        row["problems"].append(f"Неизвестна себестоимость {unknown} единиц входа")
                if e.kind in {"purchase", "opening"}:
                    row["cost_copper"] = e.money_copper
                origins = {(a["task_id"], a["strategy_id"]) for a in row["allocations"]}
                inherited = next(iter(origins)) if len(origins) == 1 else ("", "")
                task, strategy = e.task_id or inherited[0], e.strategy_id or inherited[1]
                row["attributed_task_id"], row["attributed_strategy_id"] = task, strategy
                if e.kind in {"purchase", "opening", "craft", "disenchant", "transfer"}:
                    prices = [x.reference_unit_copper if x.reference_unit_copper is not None else record["prices"].get(str(x.item_id)) for x in e.outputs]
                    weights = [Decimal(price or 0) * x.quantity for price, x in zip(prices, e.outputs, strict=True)]
                    remaining_cost = row["cost_copper"]
                    for position, line in enumerate(e.outputs):
                        if e.kind == "transfer":
                            allocations = input_allocations[(line.item_id, line.variant, line.quantity)]
                            left = line.quantity
                            for allocation in allocations:
                                stocks[(e.realm, e.target_character, line.item_id)].append({
                                    "event_id": e.event_id, "index": index, "variant": line.variant, "name": line.item_name,
                                    "remaining": allocation["quantity"], "cost": allocation["cost_copper"],
                                    "task_id": allocation["task_id"], "strategy_id": allocation["strategy_id"]})
                                left -= allocation["quantity"]
                            if left:
                                stocks[(e.realm, e.target_character, line.item_id)].append({
                                    "event_id": e.event_id, "index": index, "variant": line.variant, "name": line.item_name,
                                    "remaining": left, "cost": None, "task_id": task, "strategy_id": strategy})
                            continue
                        elif len(e.outputs) == 1:
                            cost = remaining_cost
                        elif remaining_cost is None or any(price is None for price in prices) or sum(weights) <= 0:
                            cost = None
                            row["problems"].append("Неизвестны цены для распределения стоимости выходов")
                        else:
                            cost = remaining_cost if position == len(e.outputs)-1 else int(Decimal(row["cost_copper"]) * weights[position] / sum(weights))
                            remaining_cost -= cost
                        stocks[(e.realm, e.target_character if e.kind == "transfer" else e.character, line.item_id)].append({
                            "event_id": e.event_id, "index": index, "variant": line.variant, "name": line.item_name,
                            "remaining": line.quantity, "cost": cost, "task_id": task, "strategy_id": strategy})
                if e.kind == "sale":
                    row["profit_copper"] = e.money_copper - row["cost_copper"] if row["cost_copper"] is not None else None
                if e.kind == "expense":
                    row["profit_copper"] = -e.money_copper
                if e.kind == "listing":
                    if e.listing_id in deposits:
                        row["problems"].append("Повтор выставленного AuctionID")
                    else:
                        deposits[e.listing_id] = {"amount": e.deposit_copper, "state": "held", "event_id": e.event_id,
                            "quantity": e.outputs[0].quantity, "resolved_quantity": 0, "refunded": 0,
                            "lost": 0, "character": e.character, "realm": e.realm}
                if e.kind in {"sale", "return"}:
                    deposit = deposits.get(e.listing_id)
                    if deposit:
                        deposit["resolved_quantity"] += (e.inputs if e.kind == "sale" else e.outputs)[0].quantity
                        deposit["refunded"] += e.returned_deposit_copper
                        if deposit["resolved_quantity"] > deposit["quantity"]:
                            row["problems"].append("Количество исходов превышает выставленный лот")
                        elif deposit["resolved_quantity"] == deposit["quantity"]:
                            if deposit["amount"] is not None and deposit["refunded"] <= deposit["amount"]:
                                lost = deposit["amount"]-deposit["refunded"]
                                row["deposit_loss_copper"] = lost
                                deposit["lost"], deposit["state"] = lost, "lost" if lost else "refunded"
                            else:
                                deposit["state"] = "unknown"
                                row["problems"].append("Неизвестен полный исход залога")
                    elif e.kind == "return":
                        row["problems"].append("Неизвестен залог возвращённого лота")
                if e.cash_delta_copper is not None:
                    row["cash_effect_copper"] = e.cash_delta_copper
                elif e.kind == "purchase":
                    row["cash_effect_copper"] = -e.money_copper
                elif e.kind == "sale":
                    row["cash_effect_copper"] = e.money_copper + e.returned_deposit_copper
                elif e.kind == "listing" and e.deposit_copper is not None:
                    row["cash_effect_copper"] = -e.deposit_copper
                elif e.kind == "expense":
                    row["cash_effect_copper"] = -e.money_copper
                row["problems"] = sorted(set(row["problems"]))
                rows.append(row)
            inventory = []
            for (realm, character, item), lots in stocks.items():
                for lot in lots:
                    if lot["remaining"]:
                        uncertain = bool(unresolved_stock[(realm, character, item)])
                        inventory.append({"realm": realm, "character": character, "item_id": item, **lot,
                                          "cost": None if uncertain else lot["cost"], "quantity_uncertain": uncertain})
            result = {"events": rows, "inventory": inventory, "deposits": list(deposits.values())}
            self._cache_key, self._cache = key, result
            return result

    def overview(self, *, days=30, character="", item_id=None, kind="", offset=0, limit=100) -> dict:
        all_data = self.replay()
        since = int(datetime.now(UTC).timestamp()) - days*86400 if days else 0
        rows = [r for r in all_data["events"] if r["observed_at"] >= since and (not character or r["character"] == character)
                and (not kind or r["kind"] == kind) and (item_id is None or any(x["item_id"] == item_id for x in r["inputs"]+r["outputs"]))]
        inventory = [r for r in all_data["inventory"] if (not character or r["character"] == character) and (item_id is None or r["item_id"] == item_id)]
        profit_rows = [r for r in rows if r["kind"] == "sale" and r["confirmation"] != "review"]
        losses = sum(r.get("deposit_loss_copper", 0) for r in rows)
        expenses = sum(r["money_copper"] for r in rows if r["kind"] == "expense" and r["confirmation"] != "review")
        parties: dict[str, dict] = {}
        for r in rows:
            identity = r.get("attributed_task_id") or "unassigned"
            p = parties.setdefault(identity, {"task_id": identity, "strategy_id": r.get("attributed_strategy_id", ""),
                "purchase_copper": 0, "sales_net_copper": 0, "profit_copper": 0, "unknown_sales": 0,
                "crafts": 0, "disenchants": 0, "events": 0, "predicted_profit_copper": r["details"].get("predicted_profit_copper")})
            p["events"] += 1
            if r["confirmation"] == "review":
                continue
            if r["kind"] == "purchase":
                p["purchase_copper"] += r["money_copper"]
            if r["kind"] == "sale":
                p["sales_net_copper"] += r["money_copper"]
                p["unknown_sales"] += r["profit_copper"] is None
                p["profit_copper"] += r["profit_copper"] or 0
            p["profit_copper"] -= r.get("deposit_loss_copper", 0)
            if r["kind"] == "expense":
                p["profit_copper"] -= r["money_copper"]
            if r["kind"] == "craft":
                p["crafts"] += 1
            if r["kind"] == "disenchant":
                p["disenchants"] += 1
        summary = {"events": len(rows), "purchase_copper": sum(r["money_copper"] or 0 for r in rows if r["kind"] == "purchase" and r["confirmation"] != "review"),
            "sales_net_copper": sum(r["money_copper"] for r in profit_rows),
            "known_profit_copper": sum(r["profit_copper"] or 0 for r in profit_rows)-losses-expenses,
            "other_expenses_copper": expenses,
            "unknown_sales": sum(r["profit_copper"] is None for r in profit_rows), "deposit_loss_copper": losses,
            "cash_flow_copper": sum(r["cash_effect_copper"] or 0 for r in rows),
            "cash_unknown_events": sum(r["cash_effect_copper"] is None for r in rows if r["kind"] in {"purchase", "sale", "listing"} and r["confirmation"] != "review"),
            "inventory_cost_copper": sum(r["cost"] or 0 for r in inventory),
            "inventory_unknown_units": sum(r["remaining"] for r in inventory if r["cost"] is None),
            "review_events": sum(bool(r["problems"]) and not r["details"].get("excluded") for r in rows)}
        deposits = [d for d in all_data["deposits"] if not character or d["character"] == character]
        summary["unknown_deposits"] = sum(d["amount"] is None or d["state"] == "unknown" for d in deposits)
        status_path = self.directory / "status.json"
        return {"summary": summary, "events": list(reversed(rows))[offset:offset+limit], "total": len(rows),
            "inventory": inventory, "parties": list(parties.values()), "deposits": deposits,
            "capture_status": json.loads(status_path.read_text("utf-8")) if status_path.exists() else None,
            "characters": sorted({r["character"] for r in all_data["events"]}), "format_version": 1}
