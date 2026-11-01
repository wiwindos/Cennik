from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable
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
from cennik.addon.lua_data import Parser, lua_literal, read_saved_variables
from cennik.addon.models import ExportRequest, Package, ReportBatch
from cennik.addon.service import AddonExchange, ExchangeConflict
from cennik.collector.client import CollectorClient
from cennik.core.models import AnalyticsConfig, AnalyticsSettings, utc
from cennik.engines.disenchant import DisenchantYield, expected_gross
from cennik.engines.economics import auction_net
from cennik.engines.npc_catalog import NpcPriceCatalog
from cennik.services.analytics import AnalyticsService

from .accounting import Accounting
from .bulk import BulkChecks
from .data import MarketDatabase, WorkspaceProvider, atomic_bytes, now
from .development import DevelopmentProvider, coverage_report
from .install import install_addon
from .models import MARKET, ItemFacts, PlanRequest, RefreshRequest, WorkspaceSettings
from .npc import NpcReferenceProvider, NpcReferences
from .production import Production
from .sniper import SniperBook


class WorkspaceExchange(AddonExchange):
    def packages(self, market: str, include_expired: bool = False) -> list[Package]:
        try:
            return super().packages(market, include_expired)
        except ExchangeConflict:
            # Scan acknowledgements/configuration work even before the first analysis.
            return []


class Workspace:
    def __init__(self, root: Path, *, market_key: str = MARKET, market_info=None, server_mode=False, ahl_market=None):
        self.market_key = market_key
        self.ahl_market = ahl_market or market_key
        self.server_mode = server_mode
        self.updates_guard: Callable[[], None] | None = None
        self.market_binding: dict | None = None
        self.root = root.resolve()
        self.market = MarketDatabase(self.root, market_key=market_key, market_info=market_info)
        marker = self.root / "workspace.json"
        if not marker.exists():
            atomic_bytes(marker, dumps({"id": uuid4().hex, "schema": 1, "created_at": now()}).encode())
        self.identity = json.loads(marker.read_text("utf-8"))["id"]
        self.npc_references = NpcReferences(self.root, faction=self.market_key.split('.')[-2])
        self.npc_catalog = NpcPriceCatalog(self.root / "references/npc_catalog")
        self.analytics = AnalyticsService(self.root, database=self.market.path,
            store_path=self.root / "state.sqlite", generation_reader=self.generation,
            provider_factory=self.provider, development_clock=self.development_clock)
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
            self.store.save_settings(AnalyticsSettings(active_market=self.market_key, config=config))
        self.exchange = WorkspaceExchange(self.analytics)
        self.sniper = SniperBook(self)
        self.bulk = BulkChecks(self)
        self.accounting = Accounting(self)
        self.production = Production(self)
        self.collectors = CollectorClient(self.root)
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

    def development_clock(self) -> datetime | None:
        settings = self.settings()
        return utc(settings.development_as_of) if settings.development_mode and settings.development_as_of else None

    def evaluation_now(self) -> datetime:
        return self.development_clock() or datetime.now(UTC)

    def generation(self) -> str:
        clock = self.development_clock()
        return self.identity + ":" + self.market.generation() + ":npc:" + self.npc_references.generation() + ":npc-catalog:" + self.npc_catalog.snapshot().sha256 + (":development:" + clock.isoformat() if clock else "")

    def provider(self, path, market, as_of, *, data_as_of=None):
        provider = DevelopmentProvider(self.market, market, as_of, data_as_of) if data_as_of is not None else WorkspaceProvider(self.market, market, as_of)
        return NpcReferenceProvider(provider, self.npc_references.snapshot(), data_as_of or as_of,
                                    self.npc_catalog.snapshot())

    def development_report(self):
        key = (self.market.generation(), int(time.time() // 300))
        cached = getattr(self, "_development_report", None)
        if cached is None or cached[0] != key:
            with self.market.lock:
                self._development_report = (key, coverage_report(self.market, datetime.now(UTC)))
        return self._development_report[1]

    def require_live_updates(self):
        if self.updates_guard:
            self.updates_guard()
        if self.settings().development_mode:
            raise RuntimeError("Режим разработки: запросы AHledger отключены. Снимите галочку в настройках для обновления.")

    def save_settings(self, value: WorkspaceSettings):
        if self.server_mode and (value.game_root or value.account):
            raise ValueError("Путь игры настраивается в Cennik Worker на игровом ПК")
        if value.account and (not re.fullmatch(r'[^/\\:<>"|?*]+', value.account) or value.account in {".", ".."}):
            raise ValueError("Неверное имя папки аккаунта")
        if value.game_root and not Path(value.game_root).is_absolute():
            raise ValueError("Папка игры должна быть абсолютным путём")
        if not self.analytics.lock.acquire(blocking=False):
            raise RuntimeError("Дождитесь завершения аналитики перед изменением настроек")
        try:
            future = getattr(self, "future", None)
            if future is not None and not future.done() and any(
                job["state"] in {"queued", "running"} for job in self.jobs()
            ):
                raise RuntimeError("Дождитесь завершения текущего задания перед изменением настроек")
            if value.development_mode:
                chosen = value.development_as_of or self.development_report()["selected_at"]
                if chosen is None:
                    raise ValueError("За последние четыре дня нет сохранённых цен для режима разработки")
                if chosen > datetime.now(UTC):
                    raise ValueError("Дата разработки не может быть в будущем")
                value = value.model_copy(update={"development_as_of": utc(chosen)})
            with self.store.connection() as db:
                db.execute("INSERT OR REPLACE INTO workspace_settings VALUES(1,?)", [value.model_dump_json()])
        finally:
            self.analytics.lock.release()
        return value

    def submit(self, kind: str, callback, *, coalesce=False, defer=False) -> dict:
        with self.queue_lock:
            if self.future is not None and not self.future.done():
                if coalesce:
                    running = next((j for j in self.jobs() if j["kind"] == kind and j["state"] in {"queued", "running"}), None)
                    if running:
                        return {"job_id": running["id"], "reused": True}
                if not defer:
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
        self.require_live_updates()
        if self.server_mode and spec.market not in {None, self.ahl_market}:
            raise ValueError("Разрешён только выбранный рынок AHledger")
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
        self.require_live_updates()
        api = "https://api.ahledger.com/v1"
        specs = []
        if request.dataset == "references":
            specs = [JobSpec(dataset="crafting", url=f"{api}/crafting/{self.ahl_market}", market=self.ahl_market,
                params={"profession": profession, "all": "1"}) for profession in self.settings().professions]
        else:
            params = {}
            endpoint, item = request.dataset, request.item_id
            if endpoint in {"item", "history"} and item is None:
                raise ValueError("Нужен ID предмета")
            if endpoint == "item":
                url, params = f"{api}/items/{item}", {"game": "forever"}
            elif endpoint == "history":
                url, params = f"{api}/prices/{self.ahl_market}/{item}", {"range": request.range}
            else:
                url = f"{api}/{endpoint}/{self.ahl_market}"
                if endpoint == "signals":
                    params = {"discount": "20", "days": "7"}
                elif endpoint == "deals":
                    params = {"discount": "15", "units": "1", "sells": "0", "sort": "saving", "limit": "200"}
                elif endpoint == "disenchant":
                    params = {"all": "true"}
            specs = [JobSpec(dataset=endpoint, url=url, market=self.ahl_market if endpoint != "item" else None,
                             item_id=item, params=params)]
        results = [self.request(spec) for spec in specs]
        if self.settings().auto_recalculate and any(r["changed"] for r in results):
            self.calculate()
        return {"responses": results, "full_market_history_downloaded": False}

    def import_scans(self, scans: list[dict]):
        result = [self.market.import_scan(scan) for scan in scans]
        if self.settings().auto_recalculate and any(not row["reused"] and not row.get("archived_only") for row in result):
            self.calculate()
        return result

    def calculate(self):
        result = self.analytics.start(background=False)
        latest = self.store.latest(self.market_key, summary=True)
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
        clients = self.production.clients()
        if clients:
            return next(c for c in clients if c.role == "auctioneer").paths()
        settings = self.settings()
        if not settings.game_root or not settings.account:
            raise ValueError("Укажите папку игры и аккаунт в настройках")
        game = Path(settings.game_root)
        return game / "Interface/AddOns/CennikAddon", game / "WTF/Account" / settings.account / "SavedVariables/CennikAddon.lua"

    def install_addon(self):
        clients = self.production.clients()
        if clients:
            return {"clients": [install_addon(root, self.root / "addon-backups") for root in dict.fromkeys(c.game_root for c in clients)]}
        return install_addon(self.settings().game_root, self.root / "addon-backups")

    def synchronize(self, *, include_scans: bool = True):
        if self.server_mode:
            return {"remote": True, "note": "Рабочие клиенты заберут задания при следующем соединении"}
        if self.production.clients():
            return self.production.synchronize(include_scans)
        return self._synchronize_client(include_scans=include_scans)[0]

    def deliver_collectors(self):
        result = self.collectors.flush()
        # Refresh only delivery metadata. Re-reading the game's file here could
        # hide an unimported market snapshot from the normal file watcher.
        if result.get("sent") or result.get("registry_changed"):
            self.update_collector_imports()
        return result

    def update_collector_imports(self):
        clients = self.production.clients()
        addons = {c.paths()[0] for c in clients} if clients else {self.game_paths()[0]} if self.settings().game_root and self.settings().account else set()
        for addon in addons:
            path = addon / "ImportedData.lua"
            if not path.exists():
                continue
            try:
                parser = Parser(path.read_text("utf-8-sig"), max_bytes=8_000_000)
                if parser.identifier() != "CennikImport":
                    continue
                parser.require("=")
                envelope = parser.value()
                parser.whitespace()
                if parser.pos != len(parser.text) or not isinstance(envelope, dict):
                    continue
                routes = (envelope.get("routes") or {}).values() if envelope.get("format_version") == 2 else [envelope]
                changed = False
                for payload in routes:
                    if (payload.get("market_collector") or {}).get("workspace_id") == self.identity:
                        payload["collector_delivery"] = self.collectors.delivery()
                        payload["auctioneer_registry"] = self.collectors.registry()
                        changed = True
                if changed:
                    body = ("-- Cennik addressed import / collector delivery acknowledgement\nCennikImport="+lua_literal(envelope)+"\n").encode()
                    if len(body) <= 8_000_000:
                        atomic_bytes(path, body)
            except (ValueError, OSError):
                self.log.warning("Delivery stored; addon metadata refresh will retry on next synchronization")

    def _synchronize_client(self, *, include_scans=True, client=None):
        addon, saved = client.paths() if client else self.game_paths()
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
            if client and data.get("binding") and (data["binding"].get("character"), data["binding"].get("realm")) != (client.character, client.realm):
                raise ValueError("SavedVariables относится к другому персонажу: " + client.character)
            scans = data.get("market_scans") or []
            if not isinstance(scans, list):
                raise ValueError("Неверная очередь снимков")
        npc_prices = self.npc_references.ingest(data.get("npc_quotes") or {})
        try:
            self.collectors.capture(data, client.character if client else "Локальный сборщик")
        except (ValueError, TypeError, KeyError):
            self.collectors.failed("INVALID_SAVED_OBSERVATION")
            self.log.warning("Collector diagnostics/observations require inspection; ordinary exchange continues")
        try:
            production_reports = self.production.accept(client, data.get("production") or {}) if client else {"accepted": 0}
        except (ValueError, TypeError) as error:
            production_reports = {"accepted": 0, "error": str(error)}
            self.production.capture_status(client, str(error))
            self.log.warning("Production report retained without acknowledgement: %s", error)
        try:
            accounting_reports = self.accounting.accept(data.get("accounting") or {})
        except (ValueError, TypeError) as error:
            accounting_reports = {"accepted": 0, "error": str(error)}
            self.accounting.failed(str(error))
            self.log.warning("Accounting report retained without acknowledgement: %s", error)
        try:
            bulk_reports = self.bulk.accept(data.get("bulk_checks") or {})
        except (ValueError, TypeError) as error:
            # An invalid research report must not block existing scan/task exchange.
            bulk_reports = {"accepted": 0, "error": str(error)}
            self.log.warning("Bulk report retained without acknowledgement: %s", error)
        results = self.import_scans(scans) if include_scans else []
        if npc_prices["updated"] and self.settings().auto_recalculate and not any(not r["reused"] for r in results):
            self.calculate()
        sniper_ack = self.sniper.accept_game(data.get("sniper") or {})
        recipe_requests = self.sniper.pending_recipe_requests()
        recipe_error = None
        if recipe_requests and not self.settings().development_mode:
            self.log.info("Requested recipe catalogue refresh from addon: %s requests", len(recipe_requests))
            try:
                self.refresh(RefreshRequest(dataset="references"))
                self.sniper.complete_recipe_requests(recipe_requests)
            except (ValueError, RuntimeError) as error:
                recipe_error = str(error)
                self.log.warning("Recipe refresh failed; request retained, scans still acknowledged: %s", recipe_error)
        acknowledgements = [scan["scan_id"] for scan in scans if self.market.rows(
            "SELECT 1 present FROM scan_registry WHERE scan_id=?", [hashlib.sha256(dumps(scan).encode()).hexdigest()])
            or (self.root / "collectors/scan_receipts" / (hashlib.sha256(dumps(scan).encode()).hexdigest()+".json")).exists()]
        state_path = self.root / "bridge/clients" / client.id / "state.json" if client else self.root / "bridge/state.json"
        if not state_path.exists() and isinstance(data.get("device_id"), str):
            atomic_bytes(state_path, dumps({"device_id": data["device_id"], "acknowledged": []}).encode())
        bridge = Bridge("http://127.0.0.1:8766", addon, saved, self.market_key,
                        state_path, exchange=self.exchange)
        bridge.extra_import = {"market_collector": {"market": self.market_key, "auto_scan": self.settings().auto_scan,
            "workspace_id": self.identity}, "acknowledged_scan_ids": acknowledgements,
            "sniper_book": self.sniper.wire(), "sniper_catalog": self.sniper.catalog(), "acknowledged_sniper_event_ids": sniper_ack,
            "bulk_checks": self.bulk.wire(), "accounting": self.accounting.wire(),
            "collector_delivery": self.collectors.delivery(), "auctioneer_registry": self.collectors.registry()}
        if self.market_binding:
            bridge.extra_import["market_binding"] = self.market_binding
        if client:
            bridge.write_import = False
            bridge.extra_import.update(client_id=client.id, production=self.production.wire(client))
            if client.role != "auctioneer":
                bridge.extra_import["bulk_checks"] = {"acknowledged_report_ids": []}
                bridge.extra_import["market_collector"]["auto_scan"] = False
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
            payload = bridge.import_payload
            if client:
                # Future events are account-specific; old queued IDs and bridge
                # acknowledgements are retained unchanged during migration.
                payload["device_id"] = client.id
            if client and client.role != "auctioneer":
                payload["packages"] = []
                payload.pop("sniper_book", None)
                payload.pop("sniper_catalog", None)
        finally:
            bridge.close()
        if before:
            self.last_file = (before.st_size, before.st_mtime_ns)
        return {**exchanged, "scans": results, "acknowledged_scans": len(acknowledgements),
                "saved_stamp": (before.st_mtime_ns, before.st_size) if before else None,
                "npc_prices": npc_prices, "bulk_reports": bulk_reports, "accounting_reports": accounting_reports,
                "production_reports": production_reports, "recipe_error": recipe_error,
                "note": "Подтверждения и настройки загрузятся аддоном после /reload"}, payload

    def overview(self):
        source = self.market.rows("SELECT h.source,h.observed_at,count(p.item_id) items FROM source_heads h LEFT JOIN price_snapshots p ON p.source=h.source AND p.observed_at=h.observed_at GROUP BY h.source,h.observed_at")
        coverage = self.market.rows("""SELECT dataset,item_id,status,record_count,warnings_json,
            json_extract_string(params_json,'$.profession') profession,retrieved_at FROM dataset_metadata
            QUALIFY row_number() OVER(PARTITION BY dataset,item_id,json_extract_string(params_json,'$.profession') ORDER BY retrieved_at DESC)=1
            ORDER BY retrieved_at DESC LIMIT 100""")
        scans = self.market.rows('SELECT count(*) scans,coalesce(sum(row_count),0) "rows" FROM scan_registry')[0]
        return {"workspace_id": self.identity, "market": self.market_key, "market_info": self.market.market_info,
                "server_mode": self.server_mode, "root": str(self.root), "sources": source, "coverage": coverage,
                "npc_reference_prices": len(self.npc_references.snapshot()),
                "npc_catalog": self.npc_catalog.snapshot().metadata(),
                "bulk_checks": {"format_version": 1, "max_candidates": 200, "max_unique_items": 200},
                "npc_database_prices": self.market.rows("SELECT count(*) n FROM items WHERE vendor_buy_copper>0")[0]["n"],
                "archive": {**scans, "retention": "permanent"}, "jobs": self.jobs(),
                "generation": self.analytics.generation(), "analytics": self.analytics.status(),
                "disenchant_rules": self.analytics.disenchant.snapshot().metadata(),
                "settings": self.settings().model_dump(mode="json"), "attribution": "Prices from https://ahledger.com"}

    def check_disenchant_rules(self):
        """A bounded comparison only: never imports market prices or overwrites rules."""
        directory = self.root / "references/disenchant"
        cache = directory / "api-check-cache.json"
        saved = json.loads(cache.read_text("utf-8")) if cache.exists() else None
        if saved and saved["expires"] > time.time():
            data = saved["data"]
        else:
            wait = max(self.last_request + 2.2, self.embargo) - time.monotonic()
            if wait > 300 or wait > 0 and self.stop.wait(wait):
                raise RuntimeError("Проверка AHledger отложена; повторите позже")
            self.last_request = time.monotonic()
            url = f"https://api.ahledger.com/v1/disenchant/{self.ahl_market}"
            with httpx.Client(timeout=30, follow_redirects=False) as client:
                with client.stream("GET", url, params={"all": "true", "limit": "200"},
                                   headers={"User-Agent": "Cennik disenchant rules check"}) as response:
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 2_000_000:
                            raise ValueError("Ответ проверки распыления превышает 2 МБ")
                    if response.status_code == 429:
                        self.embargo = time.monotonic() + max(60, retry_after(response.headers.get("retry-after")))
                    if response.status_code != 200:
                        raise RuntimeError(f"Проверка правил: AHledger HTTP {response.status_code}")
                    data = json.loads(body)
                    if not isinstance(data.get("opportunities"), list) or len(data["opportunities"]) > 200:
                        raise ValueError("Неожиданный ответ проверки распыления")
                    saved = {"data": data, "expires": cache_expiry(dict(response.headers)), "url": url, "checked_at": now()}
            atomic_bytes(cache, dumps(saved).encode())
        rules = self.analytics.disenchant.snapshot()
        differences, unknown, matches = [], [], 0
        for row in data["opportunities"]:
            item_id = row["item"]["id"]
            source_yields = [DisenchantYield(material_item_id=y["id"], min_count=y["minCount"],
                max_count=y["maxCount"], chance=y["chance"]) for y in row["yields"]]
            if item_id not in rules.items:
                unknown.append(item_id)
            elif (sorted(source_yields, key=lambda y: y.material_item_id) != sorted(rules.yields(item_id), key=lambda y: y.material_item_id)
                  or rules.items[item_id].item_level != row["item"]["level"]
                  or rules.items[item_id].quality != row["item"]["quality"]
                  or rules.items[item_id].item_class != row["item"]["itemClass"]):
                differences.append(item_id)
            else:
                matches += 1
        report = {"rules": rules.metadata(), "checked_at": saved["checked_at"], "compared_rows": len(data["opportunities"]),
                  "matches": matches, "differences": differences, "unknown_items": unknown,
                  "full_catalog_verified": False, "rules_updated": False,
                  "note": "Проверена ограниченная выборка API; совпадение не подтверждает весь каталог"}
        atomic_bytes(directory / "last-check.json", dumps(report).encode())
        return report

    def plan(self, request: PlanRequest):
        opportunity = self.store.detail(self.market_key, request.opportunity_id)
        if opportunity is None:
            raise ValueError("Возможность не найдена")
        vendor = opportunity.strategy_id == "vendor_arbitrage"
        npc_resale = opportunity.strategy_id == "npc_to_auction"
        disenchant = opportunity.details.get("disenchant")
        if not self.analytics.status()["result_generation_matches"]:
            raise RuntimeError("Данные изменились. Пересчитайте возможности")
        generation = self.analytics.generation()
        entries: list[dict[str, Any]] = opportunity.details.get("materials", []) if opportunity.strategy_id in {"simple_craft", "craft_graph", "craft_disenchant", "npc_to_auction"} else [
            {"item_id": opportunity.item_id, "quantity": opportunity.quantity, "source": "auction"}]
        lines, total, problems = [], 0, []
        heads = self.market.rows("SELECT source,observed_at FROM source_heads")
        own_at = next((row["observed_at"] for row in heads if row["source"] == "Cennik replicate"), None)
        warnings = []
        if npc_resale:
            warnings.append("Цена NPC справочная; наличие, запас, доступ и передаваемость предмета не подтверждены")
            if "npc_catalog_purchase_unit_unconfirmed" in opportunity.risk_flags:
                warnings.append("Условный расчёт: buy принят за цену 1 шт.; единица цены API и продавец не подтверждены")
            if "npc_catalog_release_refresh_required" in opportunity.risk_flags:
                warnings.append("Использован бета-справочник NPC: перед релизом требуется актуализация")
        if not npc_resale and own_at and any(row["observed_at"] > own_at for row in heads):
            warnings.append("Сводные цены AHledger новее книги предложений. Повторите массовый скан перед решением")
        for entry in entries or []:
            needed = entry["quantity"] * request.batches
            if entry["source"] == "npc":
                cost = needed * entry["unit_price_copper"]
                if npc_resale:
                    pack = entry.get("npc_batch_quantity", 1)
                    cost = ((needed + pack - 1) // pack) * entry.get("npc_batch_price_copper", entry["unit_price_copper"])
                lines.append({**entry, "needed": needed, "cost_copper": cost, "basis": "NPC из справочника; условия проверить"})
                total += cost
                continue
            offers = self.market.rows("SELECT * FROM working_offers WHERE item_id=? AND buyout>0 AND NOT coalesce(owned,false) ORDER BY buyout::DECIMAL(38,6)/quantity,sequence", [entry["item_id"]])
            remaining, cost, bought, selected = needed, 0, 0, []
            for offer in offers:
                if remaining <= 0:
                    break
                if (self.evaluation_now() - offer["observed_at"]).total_seconds() > self.settings().max_depth_age_seconds:
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
        if disenchant:
            gross = expected_gross(disenchant["yields"], opportunity.quantity * request.batches)
            warnings.append("Ожидаемый выход распыления; распыляемость, навык и случайный выход требуют проверки")
        if opportunity.strategy_id == "historical_discount":
            gross = opportunity.reference_price_copper * lines[0].get("purchased", lines[0]["needed"]) // opportunity.quantity
        if vendor:
            gross = sum(row["vendor_sell"] * row["purchased_quantity"] for line in lines for row in line.get("offers", []) if row["vendor_sell"] is not None)
        config = self.store.settings().config
        if config.craft_liquidity_enabled and opportunity.strategy_id in {"simple_craft", "craft_graph", "npc_to_auction"}:
            estimate = opportunity.details.get("liquidity", {}).get("conservative_sales_per_day")
            if estimate is None or Decimal(str(estimate)) <= 0:
                problems.append("Нет оценки ликвидности для выбранной партии")
            elif Decimal(opportunity.quantity * request.batches) * 100 / Decimal(str(estimate)) > config.craft_max_batch_sales_percent:
                problems.append("Партия превышает допустимую долю оценённых суточных продаж")
        if config.craft_liquidity_enabled and disenchant:
            for output in disenchant["yields"]:
                estimate = output["liquidity"]["conservative_sales_per_day"]
                expected = Decimal(output["expected_count"]) * opportunity.quantity * request.batches
                if estimate is None or Decimal(estimate) <= 0 or expected * 100 / Decimal(estimate) > config.craft_max_batch_sales_percent:
                    problems.append("Недостаточная ликвидность материала распыления: " + str(output["material_item_id"]))
        net = gross if vendor else auction_net(gross, config.auction_cut_percent)
        profit = net - total if not problems else None
        result = {"id": uuid4().hex, "opportunity_id": opportunity.id, "generation": self.analytics.generation(),
            "created_at": now(), "depth_observed_at": own_at, "warnings": warnings,
            "development_mode": self.settings().development_mode,
            "batches": request.batches, "allow_overbuy": False, "quantity_model": "forever_units_v1",
            "cost_copper": total if not problems else None, "observed_partial_cost_copper": total,
            "gross_revenue_copper": gross if not problems else None, "net_revenue_copper": net if not problems else None,
            "profit_copper": profit, "roi_percent": str(Decimal(profit) * 100 / total) if profit is not None and total else None,
            "lines": lines, "problems": sorted(set(problems)), "complete": not problems,
            "within_budget": not problems and total <= self.settings().budget_copper,
            "npc_stock_confirmed": False if npc_resale else None,
            "basis": "Закупка по справочной цене NPC; наличие и текущую цену проверить у торговца" if npc_resale else "Закупка по снимку; требуется адресная проверка в игре",
            "sale_is_forecast": not vendor, "graph_note": "Стоимость рассчитана по справочной цене NPC и упаковкам; текущее наличие не подтверждено" if npc_resale else "Стоимость пересчитана для выбранного пути; альтернативные пути партии не оптимизировались" if opportunity.strategy_id == "craft_graph" else "Стоимость партии рассчитана по предложениям снимка"}
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
        original = self.store.detail(self.market_key, plan["opportunity_id"])
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
        run = self.store.latest(self.market_key, published=True)
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
        if details.get("disenchant"):
            details["disenchant"] = {**details["disenchant"], "quantity": original.quantity * plan["batches"],
                "yields": [{**y, "expected_quantity": str(Decimal(y["expected_count"]) * original.quantity * plan["batches"])}
                           for y in details["disenchant"]["yields"]]}
        if details.get("steps"):
            details["steps"] = [{**step, **{key: step[key] * plan["batches"] for key in
                ("batches", "required_quantity", "output_quantity", "surplus_quantity") if key in step},
                "inputs": [{**entry, "quantity": entry["quantity"] * plan["batches"]} for entry in step.get("inputs", [])]}
                for step in details["steps"]]
        quantity = original.quantity * plan["batches"]
        if original.strategy_id in {"vendor_arbitrage", "historical_discount", "buy_disenchant"}:
            quantity = materials[0]["quantity"]
        modified = original.model_copy(update={"id": original.id + "-" + identity,
            "quantity": quantity, "required_capital_copper": plan["cost_copper"],
            "potential_profit_copper": plan["profit_copper"], "reference_price_copper": plan["gross_revenue_copper"],
            "buy_price_copper": materials[0]["unit_price_copper"] if original.strategy_id in {"vendor_arbitrage", "historical_discount", "buy_disenchant"} else original.buy_price_copper,
            "roi_percent": Decimal(plan["roi_percent"]), "details": details})
        request = ExportRequest(request_id="plan-" + identity, opportunity_ids=[modified.id], max_budget_copper=self.settings().budget_copper)
        created = datetime.now(UTC)
        task = self.exchange.task(modified, run, request, created)
        task.max_total_cost_copper = plan["cost_copper"]
        task.allow_overbuy = plan["allow_overbuy"]
        package = Package(package_id=uuid4().hex, market=self.market_key, run_id=run.run_id,
            generation=run.generation, max_budget_copper=self.settings().budget_copper,
            created_at=created, expires_at=task.expires_at, tasks=[task])
        payload = package.model_dump(mode="json")
        if len(dumps(payload).encode()) > 4_000_000:
            raise ValueError("Пакет превышает 4 МБ; уменьшите партию")
        with self.store.connection() as db:
            db.execute("INSERT INTO addon_packages VALUES(?,?,?,?,?,?,?)", [package.package_id, request.request_id,
                hashlib.sha256(dumps(plan).encode()).hexdigest(), self.market_key, run.run_id, package.expires_at.isoformat(), dumps(payload)])
            db.execute("INSERT INTO addon_tasks VALUES(?,?,?,?,?,?)", [task.task_id, modified.id, run.run_id, self.market_key, package.package_id, task.model_dump_json()])
        return {"package_id": package.package_id, "reused": False, "sync": self.synchronize(include_scans=False)}

    def _watch(self):
        while not self.stop.wait(5):
            try:
                settings = self.settings()
                if self.future is not None and not self.future.done():
                    continue
                if not self.server_mode and settings.auto_import and self.production.clients():
                    if self.production.needs_sync():
                        self.submit("sync", self.synchronize)
                        continue
                elif not self.server_mode and settings.auto_import and settings.game_root and settings.account:
                    addon, file = self.game_paths()
                    if file.exists() and (addon / "MarketCollector.lua").exists():
                        stat = file.stat()
                        if self.last_file != (stat.st_size, stat.st_mtime_ns):
                            self.submit("sync", self.synchronize)
                            continue
                if self.collectors.due():
                    self.submit("collector_delivery", self.deliver_collectors)
                    continue
                if settings.auto_refresh and not settings.development_mode and time.monotonic() - self.last_refresh >= settings.refresh_seconds:
                    self.last_refresh = time.monotonic()
                    self.submit("pricetable", lambda: self.refresh(RefreshRequest(dataset="pricetable")))
                elif not settings.development_mode and time.monotonic() - self.last_maintenance >= 3600:
                    self.last_maintenance = time.monotonic()
                    self.submit("cache_retention", self.prune_cache)
            except Exception:
                self.log.exception("Background source check failed")

    def prune_cache(self):
        if self.settings().development_mode:
            return {"cache_entries_removed": 0, "api_archives_removed": 0, "own_scans_removed": 0}
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
                for directory in (self.root / "archive", self.root / "bridge", self.root / "references", self.root / "bulk_checks", self.root / "accounting", self.root / "production", self.root / "collectors"):
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
                rules_path = destination / "references/disenchant/active.json"
                if not rules_path.exists():
                    atomic_bytes(rules_path, self.analytics.disenchant.snapshot().model_dump_json().encode())
                    count += 1
                npc_path = destination / "references/npc_catalog/active.json"
                if not npc_path.exists():
                    atomic_bytes(npc_path, self.npc_catalog.snapshot().model_dump_json().encode())
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
