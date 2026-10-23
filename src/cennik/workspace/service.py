from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import zstandard as zstd

from ahledger.http import cache_expiry, retry_after
from ahledger.models import JobSpec
from ahledger.parsers import parse
from ahledger.util import dumps
from cennik.addon.bridge import Bridge
from cennik.addon.lua_data import read_saved_variables
from cennik.addon.models import ExportRequest, Package, ReportBatch
from cennik.addon.service import AddonExchange, ExchangeConflict
from cennik.core.models import AnalyticsConfig, AnalyticsSettings
from cennik.engines.economics import auction_net
from cennik.services.analytics import AnalyticsService

from .data import MarketDatabase, WorkspaceProvider, atomic_bytes, now
from .install import install_addon
from .models import MARKET, ItemFacts, PlanRequest, RefreshRequest, WorkspaceSettings
from .sniper import SniperBook


class WorkspaceExchange(AddonExchange):
    def packages(self, market: str, include_expired: bool = False) -> list[Package]:
        try:
            return super().packages(market, include_expired)
        except ExchangeConflict:
            # Scan acknowledgements/configuration work even before the first analysis.
            return []


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.market = MarketDatabase(self.root)
        marker = self.root / "workspace.json"
        if not marker.exists():
            atomic_bytes(marker, dumps({"id": uuid4().hex, "schema": 1, "created_at": now()}).encode())
        self.identity = json.loads(marker.read_text("utf-8"))["id"]
        self.analytics = AnalyticsService(self.root, database=self.market.path,
            store_path=self.root / "state.sqlite", generation_reader=lambda: self.identity + ":" + self.market.generation(),
            provider_factory=lambda path, market, as_of: WorkspaceProvider(self.market, market, as_of))
        self.store = self.analytics.store
        with self.store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS workspace_settings(id INTEGER PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS workspace_jobs(id TEXT PRIMARY KEY,kind TEXT,state TEXT,
                    created_at TEXT,finished_at TEXT,payload TEXT,error TEXT);
                CREATE TABLE IF NOT EXISTS source_cache(id TEXT PRIMARY KEY,body BLOB,headers TEXT,expires REAL,
                    checked_at TEXT,source_time TEXT,url TEXT);
                CREATE TABLE IF NOT EXISTS workspace_plans(id TEXT PRIMARY KEY,created_at TEXT,payload TEXT);
            """)
            db.execute("UPDATE workspace_jobs SET state='interrupted',error='Прервано перезапуском',finished_at=? WHERE state IN ('queued','running')", [now()])
        if self.store.settings().active_market is None:
            config = self.analytics.registry.normalize_config(AnalyticsConfig(
                history_reference_mode="ahl_median30d", craft_liquidity_source="latest_source",
                craft_liquidity_enabled=True, max_price_age_seconds=3600))
            self.store.save_settings(AnalyticsSettings(active_market=MARKET, config=config))
        self.exchange = WorkspaceExchange(self.analytics)
        self.sniper = SniperBook(self)
        seed = self.root.parent / "bridge/local.json"
        with self.store.connection() as db:
            configured = db.execute("SELECT 1 FROM workspace_settings").fetchone()
        if not configured and seed.is_file():
            try:
                values = json.loads(seed.read_text("utf-8"))
                self.save_settings(WorkspaceSettings(game_root=values["game_root"], account=values["account"]))
            except (ValueError, OSError, KeyError):
                pass
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cennik-workspace")
        self.stop = threading.Event()
        self.future: Future | None = None
        self.queue_lock = threading.Lock()
        self.last_request = 0.0
        self.embargo = 0.0
        self.last_file: tuple[int, int] | None = None
        self.last_refresh = 0.0
        self.last_maintenance = time.monotonic()
        self.log = logging.getLogger("cennik.workspace." + self.identity)
        self.log.setLevel(logging.INFO)
        (self.root / "logs").mkdir(exist_ok=True)
        handler = RotatingFileHandler(self.root / "logs/workspace.log", maxBytes=2_000_000, backupCount=4, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.log.addHandler(handler)
        self.thread = threading.Thread(target=self._watch, name="cennik-source-watch", daemon=True)

    def settings(self) -> WorkspaceSettings:
        with self.store.connection() as db:
            row = db.execute("SELECT payload FROM workspace_settings WHERE id=1").fetchone()
        return WorkspaceSettings.model_validate_json(row[0]) if row else WorkspaceSettings()

    def save_settings(self, value: WorkspaceSettings):
        if value.account and (not re.fullmatch(r'[^/\\:<>"|?*]+', value.account) or value.account in {".", ".."}):
            raise ValueError("Неверное имя папки аккаунта")
        if value.game_root and not Path(value.game_root).is_absolute():
            raise ValueError("Папка игры должна быть абсолютным путём")
        with self.store.connection() as db:
            db.execute("INSERT OR REPLACE INTO workspace_settings VALUES(1,?)", [value.model_dump_json()])
        return value

    def submit(self, kind: str, callback) -> dict:
        with self.queue_lock:
            if self.future is not None and not self.future.done():
                raise RuntimeError("Дождитесь завершения текущего задания")
            identity = uuid4().hex
            with self.store.connection() as db:
                db.execute("INSERT INTO workspace_jobs VALUES(?,?, 'queued',?,NULL,NULL,NULL)", [identity, kind, now()])

            def execute():
                with self.store.connection() as db:
                    db.execute("UPDATE workspace_jobs SET state='running' WHERE id=?", [identity])
                try:
                    self.log.info("Job %s started: %s", identity, kind)
                    result = callback()
                    with self.store.connection() as db:
                        db.execute("UPDATE workspace_jobs SET state='completed',finished_at=?,payload=? WHERE id=?", [now(), dumps(result), identity])
                    self.log.info("Job %s completed", identity)
                except Exception as error:
                    self.log.exception("Job %s failed", identity)
                    with self.store.connection() as db:
                        db.execute("UPDATE workspace_jobs SET state='failed',finished_at=?,error=? WHERE id=?", [now(), f"{type(error).__name__}: {error}"[:2000], identity])

            self.future = self.pool.submit(execute)
            return {"job_id": identity}

    def jobs(self):
        with self.store.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM workspace_jobs ORDER BY created_at DESC LIMIT 20")]

    def request(self, spec: JobSpec):
        key = hashlib.sha256((spec.url + dumps(spec.params)).encode()).hexdigest()
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM source_cache WHERE id=?", [key]).fetchone()
        cached = dict(row) if row else None
        if cached and cached["expires"] > time.time():
            self.log.info("Cache hit: %s", spec.dataset)
            return {"dataset": spec.dataset, "cached": True, "changed": False}
        headers = {"User-Agent": "Cennik/0.2 local analytics", "Accept-Encoding": "identity"}
        if cached:
            previous = json.loads(cached["headers"])
            if previous.get("etag"):
                headers["If-None-Match"] = previous["etag"]
        for attempt in range(4):
            if self.stop.is_set():
                raise RuntimeError("Приложение останавливается")
            wait = max(self.last_request + 2.2, self.embargo) - time.monotonic()
            if wait > 300:
                raise RuntimeError("AHledger требует длительную паузу; повторите позже")
            if wait > 0 and self.stop.wait(wait):
                raise RuntimeError("Приложение останавливается")
            self.last_request = time.monotonic()
            self.log.info("GET %s %s attempt=%s", spec.url, spec.params, attempt + 1)
            try:
                with httpx.Client(timeout=45, follow_redirects=False) as client:
                    with client.stream("GET", spec.url, params=spec.params, headers=headers) as response:
                        body = bytearray()
                        for chunk in response.iter_bytes():
                            body.extend(chunk)
                            if len(body) > 64 * 1024 * 1024:
                                raise ValueError("Ответ превышает 64 МБ")
                        response_headers, status = dict(response.headers), response.status_code
                self.log.info("HTTP %s %s bytes=%s", status, spec.dataset, len(body))
                if status in {429, 500, 502, 503, 504, 408}:
                    self.embargo = time.monotonic() + max(2 ** (attempt + 1), retry_after(response_headers.get("retry-after")))
                    if attempt < 3:
                        continue
                if status == 304 and cached:
                    combined = {**json.loads(cached["headers"]), **response_headers}
                    with self.store.connection() as db:
                        db.execute("UPDATE source_cache SET expires=?,checked_at=?,headers=? WHERE id=?", [cache_expiry(combined), now(), dumps(combined), key])
                    return {"dataset": spec.dataset, "changed": False, "http_status": 304}
                if status != 200:
                    raise ValueError(f"AHledger HTTP {status}: {bytes(body[:300]).decode('utf-8', errors='replace')}")
                parsed = parse(spec, bytes(body))
                changed = self.market.import_api(spec, parsed, bytes(body), response_headers)
                with self.store.connection() as db:
                    db.execute("INSERT OR REPLACE INTO source_cache VALUES(?,?,?,?,?,?,?)", [key,
                        zstd.ZstdCompressor().compress(bytes(body)), dumps(response_headers), cache_expiry(response_headers), now(),
                        parsed.maximum, spec.url])
                return {"dataset": spec.dataset, "changed": changed, "records": parsed.count,
                        "status": parsed.status, "warnings": parsed.warnings}
            except httpx.HTTPError as error:
                self.log.warning("Network error: %s", type(error).__name__)
                if attempt == 3:
                    raise RuntimeError("Ошибка сети AHledger: " + type(error).__name__) from error
                self.embargo = time.monotonic() + 2 ** (attempt + 1)
        raise RuntimeError("Не удалось получить ответ")

    def refresh(self, request: RefreshRequest):
        api = "https://api.ahledger.com/v1"
        specs = []
        if request.dataset == "references":
            specs = [JobSpec(dataset="crafting", url=f"{api}/crafting/{MARKET}", market=MARKET,
                params={"profession": profession, "all": "1"}) for profession in self.settings().professions]
        else:
            params = {}
            endpoint, item = request.dataset, request.item_id
            if endpoint in {"item", "history"} and item is None:
                raise ValueError("Нужен ID предмета")
            if endpoint == "item":
                url, params = f"{api}/items/{item}", {"game": "forever"}
            elif endpoint == "history":
                url, params = f"{api}/prices/{MARKET}/{item}", {"range": request.range}
            else:
                url = f"{api}/{endpoint}/{MARKET}"
                if endpoint == "signals":
                    params = {"discount": "20", "days": "7"}
                elif endpoint == "deals":
                    params = {"discount": "15", "units": "1", "sells": "0", "sort": "saving", "limit": "200"}
                elif endpoint == "disenchant":
                    params = {"all": "true"}
            specs = [JobSpec(dataset=endpoint, url=url, market=MARKET if endpoint != "item" else None,
                             item_id=item, params=params)]
        results = [self.request(spec) for spec in specs]
        if self.settings().auto_recalculate and any(r["changed"] for r in results):
            self.calculate()
        return {"responses": results, "full_market_history_downloaded": False}

    def import_scans(self, scans: list[dict]):
        result = [self.market.import_scan(scan) for scan in scans]
        if self.settings().auto_recalculate and any(not row["reused"] for row in result):
            self.calculate()
        return result

    def calculate(self):
        result = self.analytics.start(background=False)
        latest = self.store.latest(MARKET, summary=True)
        if latest and latest.status == "failed":
            raise ValueError(latest.error or "Расчёт завершился ошибкой")
        return result

    def update_facts(self, item_id: int, value: ItemFacts):
        payload = {"item_id": item_id, **value.model_dump(), "observed_at": now(), "source": "manual"}
        identity = hashlib.sha256(dumps(payload).encode()).hexdigest()
        self.market.archive("facts", identity, payload, datetime.now(UTC))
        with self.market.lock:
            self.market.db.execute("BEGIN")
            try:
                self.market.insert_rows("items", [{"item_id": item_id, **value.model_dump(),
                    "is_placeholder": True, "origin_dataset": "manual", "raw_id": identity,
                    "sha256": identity, "retrieved_at": payload["observed_at"]}])
                self.market.db.execute("UPDATE workspace_revision SET revision=revision+1")
                self.market.db.execute("COMMIT")
            except Exception:
                self.market.db.execute("ROLLBACK")
                raise
        return payload

    def game_paths(self):
        settings = self.settings()
        if not settings.game_root or not settings.account:
            raise ValueError("Укажите папку игры и аккаунт в настройках")
        game = Path(settings.game_root)
        return game / "Interface/AddOns/CennikAddon", game / "WTF/Account" / settings.account / "SavedVariables/CennikAddon.lua"

    def install_addon(self):
        return install_addon(self.settings().game_root, self.root / "addon-backups")

    def synchronize(self, *, include_scans: bool = True):
        addon, saved = self.game_paths()
        scans: list[dict] = []
        data: dict = {}
        before = saved.stat() if saved.exists() else None
        if before:
            if before.st_size > 64_000_000:
                raise ValueError("Сохранение больше 64 МБ; сканы не удалены, нужен импорт большего формата")
            text = saved.read_text("utf-8-sig")
            after = saved.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("Игра записывает файл. Повторите синхронизацию")
            data = read_saved_variables(text, max_bytes=64_000_000)
            scans = data.get("market_scans") or []
            if not isinstance(scans, list):
                raise ValueError("Неверная очередь снимков")
        results = self.import_scans(scans) if include_scans else []
        sniper_ack = self.sniper.accept_game(data.get("sniper") or {})
        recipe_requests = self.sniper.pending_recipe_requests()
        recipe_error = None
        if recipe_requests:
            self.log.info("Requested recipe catalogue refresh from addon: %s requests", len(recipe_requests))
            try:
                self.refresh(RefreshRequest(dataset="references"))
                self.sniper.complete_recipe_requests(recipe_requests)
            except (ValueError, RuntimeError) as error:
                recipe_error = str(error)
                self.log.warning("Recipe refresh failed; request retained, scans still acknowledged: %s", recipe_error)
        acknowledgements = [scan["scan_id"] for scan in scans if self.market.rows(
            "SELECT 1 present FROM scan_registry WHERE scan_id=?", [hashlib.sha256(dumps(scan).encode()).hexdigest()])]
        state_path = self.root / "bridge/state.json"
        if not state_path.exists() and isinstance(data.get("device_id"), str):
            atomic_bytes(state_path, dumps({"device_id": data["device_id"], "acknowledged": []}).encode())
        bridge = Bridge("http://127.0.0.1:8766", addon, saved, MARKET,
                        state_path, exchange=self.exchange)
        bridge.extra_import = {"market_collector": {"market": MARKET, "auto_scan": self.settings().auto_scan,
            "workspace_id": self.identity}, "acknowledged_scan_ids": acknowledgements,
            "sniper_book": self.sniper.wire(), "sniper_catalog": self.sniper.catalog(), "acknowledged_sniper_event_ids": sniper_ack}
        def accept_reports(batch):
            with self.store.connection() as db:
                known = {row[0] for row in db.execute("SELECT id FROM addon_tasks")}
            current = [event for event in batch.events if event.task_id in known]
            foreign = [event for event in batch.events if event.task_id not in known]
            accepted = self.exchange.reports(ReportBatch(events=current)) if current else []
            if foreign:
                payload = {"events": [event.model_dump(mode="json") for event in foreign],
                           "note": "События другого рабочего пространства; не считаются сделками этого профиля"}
                identity = hashlib.sha256(dumps(payload).encode()).hexdigest()
                self.market.archive("foreign_reports", identity, payload, datetime.now(UTC))
                accepted.extend(event.event_id for event in foreign)
            return accepted
        bridge.report_handler = accept_reports
        try:
            exchanged = bridge.synchronize()
        finally:
            bridge.close()
        if before:
            self.last_file = (before.st_size, before.st_mtime_ns)
        return {**exchanged, "scans": results, "acknowledged_scans": len(acknowledgements),
                "recipe_error": recipe_error,
                "note": "Подтверждения и настройки загрузятся аддоном после /reload"}

    def overview(self):
        source = self.market.rows("SELECT h.source,h.observed_at,count(p.item_id) items FROM source_heads h LEFT JOIN price_snapshots p ON p.source=h.source AND p.observed_at=h.observed_at GROUP BY h.source,h.observed_at")
        coverage = self.market.rows("""SELECT dataset,item_id,status,record_count,warnings_json,
            json_extract_string(params_json,'$.profession') profession,retrieved_at FROM dataset_metadata
            QUALIFY row_number() OVER(PARTITION BY dataset,item_id,json_extract_string(params_json,'$.profession') ORDER BY retrieved_at DESC)=1
            ORDER BY retrieved_at DESC LIMIT 100""")
        scans = self.market.rows('SELECT count(*) scans,coalesce(sum(row_count),0) "rows" FROM scan_registry')[0]
        return {"workspace_id": self.identity, "market": MARKET, "root": str(self.root), "sources": source, "coverage": coverage,
                "archive": {**scans, "retention": "permanent"}, "jobs": self.jobs(),
                "generation": self.analytics.generation(), "analytics": self.analytics.status(),
                "settings": self.settings().model_dump(mode="json"), "attribution": "Prices from https://ahledger.com"}

    def plan(self, request: PlanRequest):
        opportunity = self.store.detail(MARKET, request.opportunity_id)
        if opportunity is None:
            raise ValueError("Возможность не найдена")
        vendor = opportunity.strategy_id == "vendor_arbitrage"
        if not self.analytics.status()["result_generation_matches"]:
            raise RuntimeError("Данные изменились. Пересчитайте возможности")
        generation = self.analytics.generation()
        entries: list[dict[str, Any]] = opportunity.details.get("materials", []) if opportunity.strategy_id in {"simple_craft", "craft_graph"} else [
            {"item_id": opportunity.item_id, "quantity": opportunity.quantity, "source": "auction"}]
        lines, total, problems = [], 0, []
        heads = self.market.rows("SELECT source,observed_at FROM source_heads")
        own_at = next((row["observed_at"] for row in heads if row["source"] == "Cennik replicate"), None)
        warnings = []
        if own_at and any(row["observed_at"] > own_at for row in heads):
            warnings.append("Сводные цены AHledger новее книги предложений. Повторите массовый скан перед решением")
        for entry in entries or []:
            needed = entry["quantity"] * request.batches
            if entry["source"] == "npc":
                cost = needed * entry["unit_price_copper"]
                lines.append({**entry, "needed": needed, "cost_copper": cost, "basis": "NPC из справочника; условия проверить"})
                total += cost
                continue
            offers = self.market.rows("SELECT * FROM working_offers WHERE item_id=? AND buyout>0 AND NOT coalesce(owned,false) ORDER BY buyout::DECIMAL(38,6)/quantity,sequence", [entry["item_id"]])
            remaining, cost, bought, selected = needed, 0, 0, []
            for offer in offers:
                if remaining <= 0:
                    break
                if (datetime.now(UTC) - offer["observed_at"]).total_seconds() > self.settings().max_depth_age_seconds:
                    problems.append("Устарел собственный снимок: " + str(entry["item_id"]))
                    break
                if offer["divisible"] is None or not offer["variant"]:
                    problems.append("Неизвестен вариант/тип торговли: " + str(entry["item_id"]))
                    break
                quantity = min(offer["quantity"], remaining)
                # Replicate stores the total value of the raw row. It describes
                # observed depth, not a mandatory bundle the buyer must take.
                price = (offer["buyout"] * quantity + offer["quantity"] - 1) // offer["quantity"]
                selected.append({**offer, "purchased_quantity": quantity, "cost_copper": price})
                cost += price
                remaining -= quantity
                bought += quantity
            if remaining > 0:
                problems.append("Не хватает подтверждённого предложения: " + str(entry["item_id"]))
            if len({row["variant"] for row in selected}) > 1 and not vendor:
                problems.append("Найдены разные варианты: нужна адресная проверка")
            if vendor and any(row["vendor_sell"] is None for row in selected):
                problems.append("Неизвестна цена продажи выбранного варианта NPC")
            total += cost
            lines.append({"item_id": entry["item_id"], "source": "auction", "needed": needed,
                          "purchased": bought, "cost_copper": cost, "offers": selected})
        if self.analytics.generation() != generation:
            problems.append("Данные изменились во время расчёта партии; повторите")
        gross = opportunity.reference_price_copper * request.batches
        if opportunity.strategy_id == "historical_discount":
            gross = opportunity.reference_price_copper * lines[0].get("purchased", lines[0]["needed"]) // opportunity.quantity
        if vendor:
            gross = sum(row["vendor_sell"] * row["purchased_quantity"] for line in lines for row in line.get("offers", []) if row["vendor_sell"] is not None)
        config = self.store.settings().config
        if config.craft_liquidity_enabled and opportunity.strategy_id in {"simple_craft", "craft_graph"}:
            estimate = opportunity.details.get("liquidity", {}).get("conservative_sales_per_day")
            if estimate is None or Decimal(str(estimate)) <= 0:
                problems.append("Нет оценки ликвидности для выбранной партии")
            elif Decimal(opportunity.quantity * request.batches) * 100 / Decimal(str(estimate)) > config.craft_max_batch_sales_percent:
                problems.append("Партия превышает допустимую долю оценённых суточных продаж")
        net = gross if vendor else auction_net(gross, config.auction_cut_percent)
        profit = net - total if not problems else None
        result = {"id": uuid4().hex, "opportunity_id": opportunity.id, "generation": self.analytics.generation(),
            "created_at": now(), "depth_observed_at": own_at, "warnings": warnings,
            "batches": request.batches, "allow_overbuy": False, "quantity_model": "forever_units_v1",
            "cost_copper": total if not problems else None, "observed_partial_cost_copper": total,
            "gross_revenue_copper": gross if not problems else None, "net_revenue_copper": net if not problems else None,
            "profit_copper": profit, "roi_percent": str(Decimal(profit) * 100 / total) if profit is not None and total else None,
            "lines": lines, "problems": sorted(set(problems)), "complete": not problems,
            "within_budget": not problems and total <= self.settings().budget_copper,
            "basis": "Закупка по снимку; требуется адресная проверка в игре",
            "sale_is_forecast": not vendor, "graph_note": "Стоимость пересчитана для выбранного пути; альтернативные пути партии не оптимизировались" if opportunity.strategy_id == "craft_graph" else "Стоимость партии рассчитана по предложениям снимка"}
        with self.store.connection() as db:
            db.execute("INSERT INTO workspace_plans VALUES(?,?,?)", [result["id"], result["created_at"], dumps(result)])
        return result

    def publish_plan(self, identity: str):
        # Read newly saved scans before deciding whether a plan is still current.
        self.synchronize()
        with self.store.connection() as db:
            existing = db.execute("SELECT payload FROM addon_packages WHERE request_id=?", ["plan-" + identity]).fetchone()
            row = db.execute("SELECT payload FROM workspace_plans WHERE id=?", [identity]).fetchone()
        if not row:
            raise ValueError("План не найден")
        plan = json.loads(row[0])
        if plan.get("quantity_model") != "forever_units_v1":
            raise RuntimeError("План использует прежнюю модель количества; пересчитайте партию")
        if existing:
            package = Package.model_validate_json(existing[0])
            if package.generation != self.analytics.generation() or package.expires_at <= datetime.now(UTC):
                raise RuntimeError("Пакет устарел; рассчитайте новый план")
            return {"package_id": package.package_id, "reused": True, "sync": self.synchronize(include_scans=False)}
        status = self.exchange.ready()
        if plan["generation"] != status["generation"]:
            raise RuntimeError("План относится к прежним данным; пересчитайте партию")
        if not plan["complete"] or not plan["within_budget"] or plan["profit_copper"] <= 0:
            raise ValueError("План неполный, убыточный или превышает бюджет")
        original = self.store.detail(MARKET, plan["opportunity_id"])
        assert original is not None
        with self.store.connection() as db:
            duplicate = db.execute("""SELECT t.id FROM addon_tasks t JOIN addon_packages p ON p.id=t.package_id
                WHERE p.expires_at>? AND (t.opportunity_id=? OR json_extract(t.payload,'$.opportunity.details.original_opportunity_id')=?)
                AND (json_extract(t.payload,'$.opportunity.details.workspace_plan_id') IS NULL
                    OR json_extract(t.payload,'$.opportunity.details.quantity_model')='forever_units_v1')
                AND NOT EXISTS(SELECT 1 FROM addon_events e WHERE e.task_id=t.id
                    AND json_extract(e.payload,'$.state') IN ('completed','cancelled')) LIMIT 1""",
                [now(), original.id, original.id]).fetchone()
        if duplicate:
            raise RuntimeError("Эта возможность уже передана в действующее задание; завершите или отмените его в игре")
        run = self.store.latest(MARKET, published=True)
        assert run is not None
        materials = []
        for line in plan["lines"]:
            quantity = line.get("purchased", line["needed"])
            offers = line.get("offers", [])
            unit = max(((offer["cost_copper"] + offer["purchased_quantity"] - 1) // offer["purchased_quantity"] for offer in offers),
                       default=(line["cost_copper"] + quantity - 1) // quantity)
            materials.append({"item_id": line["item_id"], "item_name": line.get("item_name", str(line["item_id"])),
                "quantity": quantity, "unit_price_copper": unit, "source": line["source"], "cost_copper": line["cost_copper"]})
        details = {**original.details, "materials": materials, "workspace_plan_id": identity,
                   "original_opportunity_id": original.id, "quantity_model": "forever_units_v1"}
        if details.get("steps"):
            details["steps"] = [{**step, **{key: step[key] * plan["batches"] for key in
                ("batches", "required_quantity", "output_quantity", "surplus_quantity") if key in step},
                "inputs": [{**entry, "quantity": entry["quantity"] * plan["batches"]} for entry in step.get("inputs", [])]}
                for step in details["steps"]]
        quantity = original.quantity * plan["batches"]
        if original.strategy_id in {"vendor_arbitrage", "historical_discount"}:
            quantity = materials[0]["quantity"]
        modified = original.model_copy(update={"id": original.id + "-" + identity,
            "quantity": quantity, "required_capital_copper": plan["cost_copper"],
            "potential_profit_copper": plan["profit_copper"], "reference_price_copper": plan["gross_revenue_copper"],
            "buy_price_copper": materials[0]["unit_price_copper"] if original.strategy_id in {"vendor_arbitrage", "historical_discount"} else original.buy_price_copper,
            "roi_percent": Decimal(plan["roi_percent"]), "details": details})
        request = ExportRequest(request_id="plan-" + identity, opportunity_ids=[modified.id], max_budget_copper=self.settings().budget_copper)
        created = datetime.now(UTC)
        task = self.exchange.task(modified, run, request, created)
        task.max_total_cost_copper = plan["cost_copper"]
        task.allow_overbuy = plan["allow_overbuy"]
        package = Package(package_id=uuid4().hex, market=MARKET, run_id=run.run_id,
            generation=run.generation, max_budget_copper=self.settings().budget_copper,
            created_at=created, expires_at=task.expires_at, tasks=[task])
        payload = package.model_dump(mode="json")
        if len(dumps(payload).encode()) > 4_000_000:
            raise ValueError("Пакет превышает 4 МБ; уменьшите партию")
        with self.store.connection() as db:
            db.execute("INSERT INTO addon_packages VALUES(?,?,?,?,?,?,?)", [package.package_id, request.request_id,
                hashlib.sha256(dumps(plan).encode()).hexdigest(), MARKET, run.run_id, package.expires_at.isoformat(), dumps(payload)])
            db.execute("INSERT INTO addon_tasks VALUES(?,?,?,?,?,?)", [task.task_id, modified.id, run.run_id, MARKET, package.package_id, task.model_dump_json()])
        return {"package_id": package.package_id, "reused": False, "sync": self.synchronize(include_scans=False)}

    def _watch(self):
        while not self.stop.wait(5):
            try:
                settings = self.settings()
                if self.future is not None and not self.future.done():
                    continue
                if settings.auto_import and settings.game_root and settings.account:
                    addon, file = self.game_paths()
                    if file.exists() and (addon / "MarketCollector.lua").exists():
                        stat = file.stat()
                        if self.last_file != (stat.st_size, stat.st_mtime_ns):
                            self.submit("sync", self.synchronize)
                            continue
                if settings.auto_refresh and time.monotonic() - self.last_refresh >= settings.refresh_seconds:
                    self.last_refresh = time.monotonic()
                    self.submit("pricetable", lambda: self.refresh(RefreshRequest(dataset="pricetable")))
                elif time.monotonic() - self.last_maintenance >= 3600:
                    self.last_maintenance = time.monotonic()
                    self.submit("cache_retention", self.prune_cache)
            except Exception:
                self.log.exception("Background source check failed")

    def prune_cache(self):
        cutoff = (datetime.now(UTC) - timedelta(days=self.settings().cache_history_days)).isoformat()
        with self.store.connection() as db:
            removed = db.execute("DELETE FROM source_cache WHERE checked_at<?", [cutoff]).rowcount
        ingestions = self.market.prune_api(cutoff)
        self.log.info("Retention: expired API cache=%s, unpinned archives=%s; own scans retained", removed, ingestions)
        return {"cache_entries_removed": removed, "api_archives_removed": ingestions, "own_scans_removed": 0}

    def backup(self):
        if not self.analytics.lock.acquire(blocking=False):
            raise RuntimeError("Дождитесь завершения аналитики перед резервным копированием")
        destination = self.root / "backups" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        destination.mkdir(parents=True)
        try:
            with self.market.lock:
                self.market.db.execute("CHECKPOINT")
                database_row = self.market.db.execute("SELECT current_database()").fetchone()
                assert database_row is not None
                database_name = database_row[0].replace('"', '""')
                backup_path = str(destination / "market.duckdb").replace("'", "''")
                self.market.db.execute("ATTACH '" + backup_path + "' AS workspace_backup")
                try:
                    self.market.db.execute(f'COPY FROM DATABASE "{database_name}" TO workspace_backup')
                finally:
                    self.market.db.execute("DETACH workspace_backup")
                with sqlite3.connect(self.store.path) as source:
                    with sqlite3.connect(destination / "state.sqlite") as target_db:
                        source.backup(target_db)
                count = 2
                for directory in (self.root / "archive", self.root / "bridge"):
                    if not directory.exists():
                        continue
                    for path in directory.rglob("*"):
                        if path.is_symlink():
                            raise ValueError("Резервное копирование не следует по символическим ссылкам")
                        if not path.is_file():
                            continue
                        target = destination / path.relative_to(self.root)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, target)
                        count += 1
                shutil.copyfile(self.root / "workspace.json", destination / "workspace.json")
                atomic_bytes(destination / "BACKUP_COMPLETE.json", dumps({"completed_at": now(),
                    "workspace_id": self.identity, "generation": self.analytics.generation(), "files": count+1,
                    "includes_full_archive": True}).encode())
            self.log.info("Backup completed: %s, files=%s", destination, count+1)
            return {"directory": str(destination), "files": count+1,
                "note": "Копия находится на этом диске. Для защиты от его отказа перенесите готовую папку на другой носитель."}
        finally:
            self.analytics.lock.release()

    def close(self):
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=10)
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.analytics.shutdown()
        self.market.close()
        for handler in list(self.log.handlers):
            handler.close()
            self.log.removeHandler(handler)
