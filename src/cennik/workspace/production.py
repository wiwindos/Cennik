"""Addressed production orders and verified hand-offs, stored outside SQL."""
from __future__ import annotations

import hashlib
import json
import threading
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import Field, model_validator

from ahledger.util import atomic_json
from cennik.addon.lua_data import lua_literal
from cennik.addon.models import ExportRequest, Task
from cennik.core.models import Model, Opportunity

from .accounting import StockLine
from .data import atomic_bytes
from .models import MARKET


class ProductionClient(Model):
    id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[a-f0-9]{32}$")
    role: str = Field(pattern=r"^(auctioneer|crafter)$")
    game_root: str = Field(min_length=1, max_length=1000)
    account: str = Field(min_length=1, max_length=100)
    character: str = Field(min_length=1, max_length=80)
    realm: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def safe_paths(self):
        if any(c in self.account for c in '/\\:') or self.account in {".", ".."}:
            raise ValueError("Укажите имя папки аккаунта без пути")
        if any(c in self.character+self.realm for c in '|\n\r:'):
            raise ValueError("Неверное имя персонажа/сервера")
        return self

    def paths(self):
        root = Path(self.game_root).resolve()
        return root / "Interface/AddOns/CennikAddon", root / "WTF/Account" / self.account / "SavedVariables/CennikAddon.lua"


class ClientRoster(Model):
    clients: list[ProductionClient] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def distinct(self):
        values = self.clients
        if values and (len(values) < 2 or sum(c.role == "auctioneer" for c in values) != 1):
            raise ValueError("Нужны один аукционер и минимум один крафтер")
        if len({c.realm.lower().replace(' ', '') for c in values}) > 1:
            raise ValueError("Для передачи предметов персонажи должны быть на одном сервере")
        for keys in ([c.id for c in values], [(c.realm.lower(), c.character.lower()) for c in values],
                     [str(c.paths()[1]).lower() for c in values]):
            if len(keys) != len(set(keys)):
                raise ValueError("Аккаунт, персонаж или ID повторяется")
        return self


class OrderCreate(Model):
    request_id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[a-zA-Z0-9_-]{8,80}$")
    crafter_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    task_ids: list[str] = Field(default_factory=list, max_length=50)
    opportunity_ids: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def selection(self):
        if bool(self.task_ids) == bool(self.opportunity_ids):
            raise ValueError("Выберите рабочие задания или предварительные возможности")
        values = self.task_ids or self.opportunity_ids
        if len(values) != len(set(values)):
            raise ValueError("Повтор задания")
        return self


class ProductionEvent(Model):
    event_id: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,120}$")
    workspace_id: str
    client_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    order_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    transfer_id: str = Field(default="", pattern=r"^([a-f0-9]{32})?$")
    part_id: str = Field(default="", max_length=80, pattern=r"^[a-zA-Z0-9:._-]*$")
    kind: str = Field(pattern=r"^(sent|received|crafted|review)$")
    mode: str = Field(default="", pattern=r"^(mail|trade)?$")
    observed_at: int = Field(gt=0, strict=True)
    lines: list[StockLine] = Field(default_factory=list, max_length=100)
    inputs: list[StockLine] = Field(default_factory=list, max_length=100)
    step_id: str = Field(default="", max_length=80)
    crafts: int = Field(default=0, ge=0, le=1000000, strict=True)
    reason: str = Field(default="", max_length=1500)


class Production:
    def __init__(self, workspace):
        self.w = workspace
        self.directory = workspace.root / "production"
        self.lock = threading.RLock()
        self.stamps: dict = {}
        self.acks: dict[str, list[str]] = defaultdict(list)

    def clients(self) -> list[ProductionClient]:
        path = self.directory / "clients.json"
        return ClientRoster.model_validate_json(path.read_text("utf-8")).clients if path.exists() else []

    def save_clients(self, roster: ClientRoster):
        with self.lock:
            previous = {c.id: c for c in self.clients()}
            incoming = {c.id: c for c in roster.clients}
            for order in self.orders():
                if order.get("cancelled") or order.get("closed"):
                    continue
                for identity in (order["auctioneer_id"], order["crafter_id"]):
                    if identity not in incoming or identity in previous and (incoming[identity].character, incoming[identity].realm, incoming[identity].role) != (previous[identity].character, previous[identity].realm, previous[identity].role):
                        raise ValueError("Персонажи активных заказов не могут быть удалены или заменены")
            atomic_json(self.directory / "clients.json", roster.model_dump(mode="json"))
        return {"clients": len(roster.clients)}

    def order_path(self, identity):
        if len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("Неверный ID заказа")
        return self.directory / "orders" / (identity+".json")

    def orders(self):
        return sorted((json.loads(p.read_text("utf-8")) for p in (self.directory / "orders").glob("*.json")), key=lambda o: o["created_at"], reverse=True)

    def steps(self, o: Opportunity, task_id=""):
        result = []
        source = o.details.get("steps") or []
        if not source and o.details.get("recipe"):
            recipe = o.details["recipe"]
            source = [{"recipe": recipe, "batches": o.quantity//recipe["qty_min"]}]
        for index, step in enumerate(source):
            recipe = step["recipe"]
            result.append({"step_id": f"{task_id}.{index}", "task_id": task_id, "strategy_id": o.strategy_id,
                "kind": "craft", "spell_id": recipe["spell_id"], "item_id": recipe["result_item_id"],
                "item_name": o.item_name if recipe["result_item_id"] == o.item_id else str(recipe["result_item_id"]),
                "profession": recipe.get("profession"), "crafts": step["batches"],
                "qty_min": recipe["qty_min"], "inputs": recipe["inputs"]})
        de = o.details.get("disenchant")
        if de:
            result.append({"step_id": f"{task_id}.de", "task_id": task_id, "strategy_id": o.strategy_id,
                "kind": "disenchant", "spell_id": 13262, "item_id": o.item_id, "item_name": o.item_name,
                "crafts": de["quantity"], "qty_min": 1, "inputs": [{"item_id": o.item_id, "quantity": 1}]})
        if not result or any(s["crafts"] <= 0 for s in result):
            raise ValueError(o.item_name+": нет полного производственного маршрута")
        return result

    def candidates(self):
        assigned = {t for o in self.orders() if not o.get("cancelled") for t in o["task_ids"]}
        with self.w.store.connection() as db:
            rows = db.execute("SELECT payload FROM addon_tasks ORDER BY rowid DESC LIMIT 500").fetchall()
        result = []
        for row in rows:
            task = Task.model_validate_json(row[0])
            if task.task_id in assigned:
                continue
            try:
                self.steps(task.opportunity, task.task_id)
            except (ValueError, KeyError):
                continue
            result.append({"task_id": task.task_id, "item_name": task.item_name, "strategy_id": task.strategy_id,
                           "quantity": task.opportunity.quantity, "profit_copper": task.predicted_profit_copper})
        return result

    def create(self, request: OrderCreate):
        with self.lock:
            digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
            for order in self.orders():
                if order["request_id"] == request.request_id:
                    if order["request_hash"] != digest:
                        raise ValueError("Повтор ID создания с другим выбором")
                    return {"order_id": order["order_id"], "reused": True}
            clients = self.clients()
            if sum(not o.get("closed") and not o.get("cancelled") for o in self.orders()) >= 50:
                raise ValueError("Сначала архивируйте завершённые заказы: предел 50 активных")
            crafter = next((c for c in clients if c.id == request.crafter_id and c.role == "crafter"), None)
            auctioneer = next((c for c in clients if c.role == "auctioneer"), None)
            if not crafter or not auctioneer:
                raise ValueError("Сначала настройте аккаунты аукционера и крафтера")
            assigned = {t for o in self.orders() if not o.get("cancelled") for t in o["task_ids"]}
            tasks = []
            if request.opportunity_ids:
                for identity in request.opportunity_ids:
                    opportunity = self.w.store.detail(MARKET, identity)
                    if opportunity is None:
                        raise ValueError("Возможность не найдена")
                    self.steps(opportunity)
                package = self.w.exchange.export(ExportRequest(request_id="production-"+request.request_id,
                    opportunity_ids=request.opportunity_ids, max_budget_copper=self.w.settings().budget_copper))
                tasks = package.tasks
            else:
                with self.w.store.connection() as db:
                    for identity in request.task_ids:
                        row = db.execute("SELECT payload FROM addon_tasks WHERE id=?", [identity]).fetchone()
                        if not row or identity in assigned:
                            raise ValueError("Задание не найдено или уже назначено")
                        tasks.append(Task.model_validate_json(row[0]))
            steps, names, return_ids = [], {}, set()
            quantities: dict[int, int] = defaultdict(int)
            for task in tasks:
                steps.extend(self.steps(task.opportunity, task.task_id))
                for line in task.lines:
                    quantities[line.item_id] += line.quantity
                    names[line.item_id] = line.item_name
                    return_ids.add(line.item_id)
                return_ids.add(task.item_id)
                de = task.opportunity.details.get("disenchant")
                if de:
                    return_ids.update(y["material_item_id"] for y in de["yields"])
            for step in steps:
                return_ids.add(step["item_id"])
                return_ids.update(r["item_id"] for r in step["inputs"])
            identity = uuid4().hex
            forward = {"id": uuid4().hex, "order_id": identity, "from_client": auctioneer.id, "to_client": crafter.id,
                "direction": "materials", "lines": [{"item_id": i, "quantity": q, "item_name": names[i], "variant": ""} for i, q in quantities.items()]}
            backward = {"id": uuid4().hex, "order_id": identity, "from_client": crafter.id, "to_client": auctioneer.id,
                "direction": "results", "lines": [], "allowed_items": sorted(return_ids)}
            order = {"order_id": identity, "request_id": request.request_id, "request_hash": digest,
                "created_at": int(datetime.now(UTC).timestamp()), "auctioneer_id": auctioneer.id, "crafter_id": crafter.id,
                "task_ids": [t.task_id for t in tasks], "tasks": [t.model_dump(mode="json") for t in tasks],
                "steps": steps, "transfers": [forward, backward], "cancelled": False}
            if len(json.dumps(order).encode()) > 2_000_000:
                raise ValueError("Заказ слишком большой")
            atomic_json(self.order_path(identity), order)
            return {"order_id": identity, "tasks": len(tasks), "reused": False}

    def events(self):
        return [json.loads(p.read_text("utf-8")) for p in (self.directory / "events").glob("*.json")]

    def capture_status(self, client, error=""):
        atomic_json(self.directory / "capture" / (client.id+".json"),
                    {"character": client.character, "error": error, "at": int(datetime.now(UTC).timestamp())})

    def accept(self, client: ProductionClient, raw):
        self.acks[client.id] = []
        if not raw:
            self.capture_status(client)
            return {"accepted": 0}
        incoming = raw.get("outbox", raw.get("events")) or []
        if not isinstance(incoming, list) or len(incoming) > 500:
            raise ValueError("Неверная очередь производства")
        accepted = 0
        with self.lock:
            orders = {o["order_id"]: o for o in self.orders()}
            existing = {e["event_id"]: e for e in self.events()}
            pending = []
            for payload in incoming:
                payload = {**payload, "lines": payload.get("lines") or [], "inputs": payload.get("inputs") or []}
                event = ProductionEvent.model_validate(payload)
                if event.workspace_id != self.w.identity:
                    continue
                order = orders.get(event.order_id)
                if event.client_id != client.id or not order or client.id not in (order["auctioneer_id"], order["crafter_id"]):
                    raise ValueError("Событие производства принадлежит другому персонажу")
                if event.observed_at > int(datetime.now(UTC).timestamp())+300:
                    raise ValueError("Время производства находится в будущем")
                if not event.event_id.startswith(client.id+":production:"):
                    raise ValueError("ID события не соответствует аккаунту")
                for lines in (event.lines, event.inputs):
                    if len({(r.item_id, r.variant) for r in lines}) != len(lines):
                        raise ValueError("Повтор предмета в событии")
                if event.kind in {"sent", "received"}:
                    transfer = next((t for t in order["transfers"] if t["id"] == event.transfer_id), None)
                    if not transfer or client.id != transfer["from_client" if event.kind == "sent" else "to_client"] or not event.part_id or not event.lines:
                        raise ValueError("Неверный участник передачи")
                    allowed = set(transfer.get("allowed_items") or [r["item_id"] for r in transfer["lines"]])
                    if any(r.item_id not in allowed for r in event.lines):
                        raise ValueError("В передаче есть посторонний предмет")
                    sender_id = transfer["from_client"]
                    if not event.part_id.startswith(sender_id+":") or event.mode not in {"mail", "trade"}:
                        raise ValueError("Неверный идентификатор или способ передачи")
                if event.kind == "crafted":
                    step = next((s for s in order["steps"] if s["step_id"] == event.step_id), None)
                    if client.id != order["crafter_id"] or not step or event.crafts <= 0:
                        raise ValueError("Неверный этап изготовления")
                    actual: dict[int, int] = defaultdict(int)
                    for line in event.inputs:
                        actual[line.item_id] += line.quantity
                    expected = {line["item_id"]: line["quantity"]*event.crafts for line in step["inputs"]}
                    if dict(actual) != expected or not event.lines:
                        raise ValueError("Фактические материалы не соответствуют этапу")
                    if step["kind"] == "craft" and (any(r.item_id != step["item_id"] for r in event.lines)
                            or sum(r.quantity for r in event.lines) < step["qty_min"]*event.crafts):
                        raise ValueError("Фактический результат не соответствует рецепту")
                data = event.model_dump(mode="json")
                if event.event_id in existing:
                    if existing[event.event_id] != data:
                        raise ValueError("ID события производства повторён с другим содержимым")
                else:
                    existing[event.event_id] = data
                    pending.append(data)
            # Validate the entire batch before acknowledging or writing any part.
            for order in orders.values():
                rows = [e for e in existing.values() if e["order_id"] == order["order_id"]]
                for step in order["steps"]:
                    if sum(e["crafts"] for e in rows if e["kind"] == "crafted" and e["step_id"] == step["step_id"]) > step["crafts"]:
                        raise ValueError("Изготовлено больше объёма заказа")
                for transfer in order["transfers"]:
                    parts: dict[str, dict] = {}
                    totals: dict[int, int] = defaultdict(int)
                    for e in rows:
                        if e["transfer_id"] != transfer["id"] or e["kind"] not in {"sent", "received"}:
                            continue
                        part = parts.setdefault(e["part_id"], {"sent": [], "received": [], "mode": e["mode"]})
                        if part["mode"] != e["mode"]:
                            raise ValueError("Способ передачи изменён внутри одной посылки")
                        part[e["kind"]].append(e)
                        if e["kind"] == "sent":
                            for line in e["lines"]:
                                totals[line["item_id"]] += line["quantity"]
                    if transfer["direction"] == "materials" and any(totals[r["item_id"]] > r["quantity"] for r in transfer["lines"]):
                        raise ValueError("Отправлено больше материалов, чем нужно для заказа")
                    for part in parts.values():
                        if len(part["sent"]) > 1:
                            raise ValueError("Повтор отправки одной посылки")
                        sent: dict[tuple, int] = defaultdict(int)
                        received: dict[tuple, int] = defaultdict(int)
                        for kind, target in (("sent", sent), ("received", received)):
                            for e in part[kind]:
                                for line in e["lines"]:
                                    target[(line["item_id"], line["variant"])] += line["quantity"]
                        if sent and any(q > sent[key] for key, q in received.items()):
                            raise ValueError("Получено больше состава отправленной посылки")
            for data in pending:
                path = self.directory / "events" / (hashlib.sha256(data["event_id"].encode()).hexdigest()+".json")
                atomic_json(path, data)
                accepted += 1
            self.acks[client.id] = [e["event_id"] for e in incoming if e.get("workspace_id") == self.w.identity]
            report = raw.get("recipes")
            if isinstance(report, dict) and len(json.dumps(report).encode()) <= 1_000_000:
                atomic_json(self.directory / "recipes" / (client.id+".json"), report)
            self.capture_status(client)
        return {"accepted": accepted}

    def status(self):
        events = self.events()
        result = []
        for order in self.orders():
            rows = [e for e in events if e["order_id"] == order["order_id"]]
            steps = [{**s, "done": sum(e["crafts"] for e in rows if e["kind"] == "crafted" and e["step_id"] == s["step_id"])} for s in order["steps"]]
            transfers = []
            for transfer in order["transfers"]:
                parts: dict[str, dict] = {}
                for e in rows:
                    if e["transfer_id"] != transfer["id"] or e["kind"] not in {"sent", "received"}:
                        continue
                    part = parts.setdefault(e["part_id"], {"part_id": e["part_id"], "sent": defaultdict(int), "received": defaultdict(int), "mode": e["mode"]})
                    for line in e["lines"]:
                        part[e["kind"]][str(line["item_id"])+":"+line["variant"]] += line["quantity"]
                values = []
                for part in parts.values():
                    sent, received = dict(part["sent"]), dict(part["received"])
                    values.append({**part, "sent": sent, "received": received,
                        "state": "confirmed" if sent and sent == received else "conflict" if sent and any(q > sent.get(i, 0) for i, q in received.items()) else "awaiting_peer"})
                transfers.append({**transfer, "parts": values})
            stock: dict[str, int] = defaultdict(int)
            for e in rows:
                if e["client_id"] != order["crafter_id"]:
                    continue
                if e["kind"] == "received" or e["kind"] == "crafted":
                    for line in e["lines"]:
                        stock[str(line["item_id"])] += line["quantity"]
                if e["kind"] == "crafted":
                    for line in e["inputs"]:
                        stock[str(line["item_id"])] -= line["quantity"]
            result.append({**order, "steps": steps, "transfers": transfers, "crafter_stock": dict(stock),
                           "review": [e for e in rows if e["kind"] == "review"]})
        return result

    def wire(self, client):
        events = self.events()
        sequence = max((int(e["event_id"].rsplit(":", 1)[-1]) for e in events
                        if e["client_id"] == client.id and e["event_id"].rsplit(":", 1)[-1].isdigit()), default=0)
        part_sequence = max((int(e["part_id"].rsplit(":", 1)[-1]) for e in events
                             if e["kind"] == "sent" and e["client_id"] == client.id and e["part_id"].rsplit(":", 1)[-1].isdigit()), default=0)
        return {"format_version": 1, "workspace_id": self.w.identity, "client": client.model_dump(mode="json"),
            "sequence": sequence, "part_sequence": part_sequence,
            "clients": [c.model_dump(include={"id", "role", "character", "realm"}) for c in self.clients()],
            "orders": [{k: v for k, v in o.items() if k not in {"tasks", "request_hash"}} for o in self.status()
                       if not o["cancelled"] and not o.get("closed") and client.id in (o["auctioneer_id"], o["crafter_id"])],
            "acknowledged_event_ids": self.acks[client.id]}

    def needs_sync(self):
        for client in self.clients():
            _, path = client.paths()
            stamp = (path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else None
            if self.stamps.get(client.id, "new") != stamp:
                return True
        return False

    def synchronize(self, include_scans=True):
        with self.lock:
            groups: dict[Path, dict] = defaultdict(dict)
            results = []
            for client in self.clients():
                addon, _ = client.paths()
                if not (addon / "Production.lua").is_file() or "Production.lua" not in (addon / "CennikAddon.toc").read_text("utf-8"):
                    raise ValueError("Обновите CennikAddon до 0.11.0 для обоих аккаунтов перед синхронизацией")
                result, payload = self.w._synchronize_client(include_scans=include_scans, client=client)
                # Each account has its own report cursor/device id, even when the
                # clients share Interface/AddOns and the single import file.
                addon, path = client.paths()
                groups[addon][client.realm+":"+client.character] = payload
                self.stamps[client.id] = result.pop("saved_stamp")
                results.append({"client_id": client.id, "character": client.character, **result})
            for addon, routes in groups.items():
                for payload in routes.values():
                    client = next(c for c in self.clients() if c.id == payload["client_id"])
                    payload["production"] = self.wire(client)
                body = "-- Addressed Cennik import; each character selects its own route.\nCennikImport="+lua_literal({"format_version": 2, "routes": routes})+"\n"
                if len(body.encode()) > 8_000_000:
                    raise ValueError("Импорт двух окон превышает 8 МБ")
                atomic_bytes(addon / "ImportedData.lua", body.encode())
            return {"clients": results, "note": "В каждом окне выполните /reload для получения адресных заданий"}

    def overview(self):
        return {"clients": [c.model_dump(mode="json") for c in self.clients()], "orders": self.status(),
                "capture_status": [json.loads(p.read_text("utf-8")) for p in (self.directory / "capture").glob("*.json")],
                "candidates": self.candidates(), "recipes": {c.id: json.loads(p.read_text("utf-8")) for c in self.clients()
                    if (p := self.directory / "recipes" / (c.id+".json")).exists()}}

    def close_order(self, identity: str):
        with self.lock:
            order = next((o for o in self.status() if o["order_id"] == identity), None)
            if not order:
                raise ValueError("Заказ не найден")
            if any(p["state"] != "confirmed" for t in order["transfers"] for p in t["parts"]):
                raise ValueError("Сначала подтвердите незавершённые передачи с обеих сторон")
            raw = json.loads(self.order_path(identity).read_text("utf-8"))
            raw["closed"] = True
            atomic_json(self.order_path(identity), raw)
            return {"closed": True, "note": "Остатки и финансовая история сохранены"}
