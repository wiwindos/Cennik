from __future__ import annotations

import ipaddress
import json
from decimal import Decimal
from functools import wraps
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import zstandard as zstd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from cennik.addon.api import addon_router
from cennik.api.routes import analytics_router
from cennik.collector.auctioneers import Review
from cennik.collector.protocol import Connection
from cennik.collector.server import Invitation
from cennik.core.models import AnalyticsConfig
from cennik.engines.disenchant import DisenchantRules
from cennik.engines.npc_catalog import NpcPriceDocument
from cennik.sources import TokenUpdate
from cennik.strategies.craft_graph import CraftGraphSettings

from .accounting import JournalRevision, LedgerEvent
from .bulk import BulkCreate, BulkSelection
from .leveling import LevelingPublish, LevelingQuery
from .models import ImportRequest, ItemFacts, PlanRequest, RefreshRequest, WorkspaceSettings
from .presentation import page
from .production import ClientRoster, OrderCreate
from .runtime import revision
from .service import Workspace
from .sniper import SnipeBudget, SnipeImport, SnipeMaterials, SnipeRule
from .vendor_bargains import VendorBargains
from .read_views import ReadViews
from .extended import ExtendedRequest


def create_app(root: Path, *, workspace=None, remote=False) -> FastAPI:
    loaded_revision = revision()
    workspace = workspace or Workspace(root)

    @asynccontextmanager
    async def lifespan(app):
        workspace.thread.start()
        try:
            yield
        finally:
            workspace.close()

    app = FastAPI(title="Cennik Local", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.workspace = workspace
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=4)
    vendor_bargains = VendorBargains(workspace)
    views=workspace.read_views=getattr(workspace,'read_views',None) or ReadViews(workspace.root)

    def cached_read(function):
        @wraps(function)
        def wrapped(*args,**kwargs):
            use_view=kwargs.pop('view',False)
            if not use_view:return function(*args,**kwargs)
            return views.get([function.__name__,kwargs],lambda:function(*args,**kwargs,view=True))
        return wrapped

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        try:
            loopback = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            loopback = False
        origin = request.headers.get("origin")
        if remote and not getattr(request.state, "cennik_owner", False):
            return JSONResponse({"detail": "Нужна авторизация владельца"}, status_code=401)
        if not remote and (not loopback or request.url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or origin is not None and origin != str(request.base_url).rstrip("/")):
            return JSONResponse({"detail": "Доступ только с этой машины и страницы Cennik"}, status_code=403)
        if int(request.headers.get("content-length", "0")) > 64_000_000:
            return JSONResponse({"detail": "Запрос превышает 64 МБ"}, status_code=413)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'",
            "Referrer-Policy": "no-referrer"})
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({"detail": str(error)}, status_code=422)

    @app.exception_handler(RuntimeError)
    async def conflict(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    static = Path(__file__).resolve().parents[2] / "ahledger/static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index(request: Request):
        return page(static / "next.html", request)

    @app.get("/healthz")
    def health():
        return {"status": "ok", "local_mode": not remote, "workspace": str(workspace.root), "profile": "cennik-server-market" if remote else "cennik-next",
                "runtime_revision": loaded_revision, "restart_required": loaded_revision != revision()}

    @app.get("/api/v2/overview")
    def overview(view: bool = False):
        if not view:return workspace.overview()
        def fallback():
            return {'workspace_id':workspace.identity,'market':workspace.market_key,'market_info':workspace.market.market_info,
                'server_mode':workspace.server_mode,'sources':[],'coverage':[],'archive':{'scans':None,'rows':None},
                'bootstrap':workspace.bootstrap.metadata(),'npc_catalog':{'loading':True},'source_updates':{},'source_changes':{},
                'npc_reference_prices':None,'npc_database_prices':None,'observed_recipes':None,'analytics':{},
                'disenchant_rules':workspace.analytics.disenchant.snapshot().metadata(),'disenchant_check':{},
                'bulk_checks':{'max_candidates':200,'max_unique_items':200}}
        result=views.get('overview',workspace.overview,fallback=fallback)
        result['analytics']=workspace.analytics.ui_status(result.get('analytics'))
        result['analytics']['automatic_recalculation']=workspace.settings().auto_recalculate
        result['generation']=result['analytics']['generation']
        result['jobs']=workspace.jobs()
        result['settings']=workspace.settings().model_dump(mode='json')
        result['upstreams']=workspace.sources.public()
        result['source_updates']=workspace.source_updates.status()
        return result

    @app.get("/api/v2/neutral")
    @cached_read
    def neutral_auctions_v2(mode: Literal["deals", "all"] = "deals", q: str = Query("", max_length=100),
                         min_profit_copper: int = Query(0, ge=0, le=2**53-1),
                         min_roi: float = Query(0, ge=0, le=1000000),
                         budget_copper: int | None = Query(None, ge=0, le=2**53-1),
                         max_age_seconds: int = Query(0, ge=0, le=31536000),
                         sort: Literal["profit", "roi", "unit", "sales", "buy_price", "sale_price"] = "profit",
                         order: Literal["asc", "desc"] = "desc",
                         min_sales: float | None = Query(None, ge=0, le=1000000000000),
                         max_sales: float | None = Query(None, ge=0, le=1000000000000),
                         price_basis: Literal["buy", "sale"] = "buy",
                         min_price_copper: int | None = Query(None, ge=0, le=2**53-1),
                         max_price_copper: int | None = Query(None, ge=0, le=2**53-1),
                         max_roi: float | None = Query(None, ge=0, le=1000000),
                         offset: int = Query(0, ge=0, le=100000), limit: int = Query(50, ge=1, le=100),
                         selected: str | None = Query(None, pattern=r"^[a-f0-9]{64}$"), view: bool = False):
        config = workspace.effective_config()
        result = workspace.market.neutral.report(cut=config.auction_cut_percent,
            own_only=workspace.settings().own_data_only, mode=mode, q=q,
            min_profit=min_profit_copper, min_roi=min_roi, budget=budget_copper,
            max_age=max_age_seconds, sort=sort, order=order, offset=offset, limit=limit, selected=selected,
            min_sales=min_sales, max_sales=max_sales, price_basis=price_basis,
            min_price=min_price_copper, max_price=max_price_copper, max_roi=max_roi,prepared=view)
        result["stale_after_seconds"] = config.max_price_age_seconds
        return result

    @app.get('/api/v2/vendor-bargains')
    @cached_read
    def vendor_comparison(q: str = Query('', max_length=100),
                     sort: Literal['closeness','markup','price','vendor','quantity','name'] = 'closeness',
                     order: Literal['asc','desc'] = 'asc',
                     min_markup: Decimal | None = Query(None,ge=-100,le=1000000,decimal_places=4),
                     max_markup: Decimal | None = Query(None,ge=-100,le=1000000,decimal_places=4),
                     stackable_only: bool = False,
                     max_age_seconds: int = Query(0,ge=0,le=31536000),
                     offset: int = Query(0,ge=0,le=1000000),limit: int = Query(50,ge=1,le=100),view: bool = False):
        return vendor_bargains.report(q=q,sort=sort,order=order,min_markup=min_markup,max_markup=max_markup,
            max_age_seconds=max_age_seconds,offset=offset,limit=limit,stackable_only=stackable_only)

    @app.get('/api/v2/extended-analysis')
    def extended_status():return workspace.extended.status()

    @app.post('/api/v2/extended-analysis',status_code=202)
    def extended_start(value:ExtendedRequest):return workspace.extended.start(value)

    @app.get("/api/v2/game-names")
    def game_names(revision: str | None = Query(None, max_length=30)):
        workspace.market.names.seed(workspace.bootstrap.snapshot())
        return workspace.market.names.document(revision)

    @app.get("/api/v2/price-gaps")
    def price_gaps(q: str = Query("", max_length=100), limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0, le=5000)):
        run = workspace.store.latest(workspace.market_key, published=True)
        report = run.diagnostics.get("price_gaps", {}) if run else {}
        rows = report.get("items", [])
        if q:
            translated = set(workspace.market.names.search(q, 5000))
            rows = [r for r in rows if r["item_id"] in translated or str(r["item_id"]) == q or q.casefold() in (r.get("name") or "").casefold()]
        return {"items": rows[offset:offset + limit], "total": len(rows), "limit": limit, "offset": offset,
                "run_id": run.run_id if run else None, "as_of": run.as_of if run else None,
                "truncated": report.get("truncated", False)}

    @app.get("/api/v2/production")
    def production():
        return workspace.production.overview()

    @app.get("/api/v2/leveling")
    def leveling():
        return workspace.leveling.overview()

    @app.post("/api/v2/leveling/analyze")
    def leveling_analyze(value: LevelingQuery):
        return workspace.leveling.analyze(value)

    @app.post("/api/v2/leveling/publish", status_code=202)
    def leveling_publish(value: LevelingPublish):
        def publish():
            result = workspace.leveling.publish(value)
            return {**result, "sync": workspace.synchronize(include_scans=False)}
        return workspace.submit("profession_leveling", publish)

    @app.get("/api/v2/collectors")
    def collectors():
        return workspace.collectors.overview()

    @app.put("/api/v2/collectors/connection")
    def collector_connection(value: Connection):
        result = workspace.collectors.configure(value)
        workspace.submit("collector_registry", workspace.update_collector_imports, coalesce=True, defer=True)
        return result

    @app.post("/api/v2/collectors/send", status_code=202)
    def collector_send():
        return workspace.submit("collector_delivery", workspace.deliver_collectors, coalesce=True, defer=True)

    @app.get("/api/v2/collectors/members")
    def collector_members():
        return workspace.collectors.admin()

    @app.get("/api/v2/collectors/auctioneers")
    def collector_auctioneers():
        return workspace.collectors.admin(path="/v1/admin/auctioneers")

    @app.post("/api/v2/collectors/auctioneers/{identity}/review", status_code=202)
    def collector_auctioneer_review(identity: str, value: Review):
        def execute():
            result = workspace.collectors.review_auctioneer(identity, value)
            workspace.update_collector_imports()
            return {**result, "note": "Решение сохранено. Аддоны применят обновление после синхронизации приложения и /reload"}
        return workspace.submit("auctioneer_review", execute, defer=True)

    @app.post("/api/v2/collectors/members")
    def collector_invite(value: Invitation):
        return workspace.collectors.invite(value.label)

    @app.delete("/api/v2/collectors/members/{identity}")
    def collector_revoke(identity: str):
        return workspace.collectors.revoke(identity)

    @app.put("/api/v2/production/clients")
    def production_clients(value: ClientRoster):
        if remote:
            raise ValueError("Рабочие клиенты настраиваются в панели сервера")
        return workspace.production.save_clients(value)

    @app.post("/api/v2/production/orders", status_code=202)
    def production_create(value: OrderCreate):
        def execute():
            result = workspace.production.create(value)
            return {**result, "sync": workspace.synchronize(include_scans=False)}
        return workspace.submit("production_order", execute)

    @app.post("/api/v2/production/orders/{identity}/close")
    def production_close(identity: str):
        return workspace.production.close_order(identity)

    @app.get("/api/v2/accounting")
    def accounting_overview(days: int = Query(default=30, ge=0, le=36500), character: str = "",
                            item_id: int | None = Query(default=None, gt=0), kind: str = "",
                            offset: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500)):
        return workspace.accounting.overview(days=days, character=character, item_id=item_id, kind=kind, offset=offset, limit=limit)

    @app.get("/api/v2/accounting/events/{identity}")
    def accounting_event(identity: str):
        return workspace.accounting.load(identity)

    @app.post("/api/v2/accounting/events")
    def accounting_manual(event: LedgerEvent):
        return workspace.accounting.create_manual(event)

    @app.post("/api/v2/accounting/events/{identity}/revise")
    def accounting_revision(identity: str, value: JournalRevision):
        return workspace.accounting.revise(identity, value)

    @app.get("/api/v2/bulk")
    def bulk_listing():
        return workspace.bulk.listing()

    @app.get("/api/v2/bulk/{identity}")
    def bulk_detail(identity: str):
        return workspace.bulk.detail(identity)

    @app.post("/api/v2/bulk", status_code=202)
    def bulk_create(value: BulkCreate):
        def execute():
            result = workspace.bulk.create(value)
            return {**result, "sync": workspace.synchronize(include_scans=False)}
        return workspace.submit("bulk_export", execute)

    @app.post("/api/v2/bulk/{identity}/evaluate")
    def bulk_evaluate(identity: str, value: BulkSelection):
        return workspace.bulk.detail(identity, value.choices)

    @app.post("/api/v2/bulk/{identity}/publish", status_code=202)
    def bulk_publish(identity: str, value: BulkSelection):
        def execute():
            result = workspace.bulk.publish(identity, value)
            return {**result, "sync": workspace.synchronize(include_scans=False)}
        return workspace.submit("bulk_publish", execute)

    @app.get("/api/v2/settings")
    def settings(view: bool = False):
        return {"workspace": workspace.settings(), "analytics": workspace.store.settings(),
            "upstreams": workspace.sources.public(), "source_updates": workspace.source_updates.status(),
            "development": None if view else workspace.development_report(),
            "schemas": {"workspace": WorkspaceSettings.model_json_schema(), "analytics": AnalyticsConfig.model_json_schema(),
                        "craft_graph": CraftGraphSettings.model_json_schema()}}

    @app.get('/api/v2/development-report')
    @cached_read
    def development_report(view: bool = False):
        return workspace.development_report()

    @app.put("/api/v2/settings")
    def save(value: WorkspaceSettings):
        return workspace.save_settings(value)

    @app.put("/api/v2/sources/ahl/token")
    def save_ahl_token(value: TokenUpdate):
        return workspace.sources.save_token(value)

    @app.post("/api/v2/sources/ahl/check", status_code=202)
    def check_ahl_token():
        return workspace.submit("ahl_token_check", workspace.sources.check_token)

    @app.post("/api/v2/sources/fyi/update", status_code=202)
    def update_fyi():
        return workspace.submit("source_fyi", lambda: workspace.source_updates.run("fyi", force=True))

    @app.post("/api/v2/refresh", status_code=202)
    def refresh(value: RefreshRequest):
        workspace.require_live_updates()
        return workspace.submit(value.dataset, lambda: workspace.refresh(value))

    @app.post("/api/v2/recalculate", status_code=202)
    @app.post("/api/v2/analytics/recalculate", status_code=202)
    def recalculate():
        return workspace.submit("analytics", workspace.calculate)

    @app.post('/api/v2/bootstrap/prices/remove', status_code=202)
    def remove_beta_prices():
        return workspace.submit('remove_beta_prices', workspace.remove_beta_prices, coalesce=True, defer=True)

    @app.get("/api/v2/disenchant/rules")
    def disenchant_rules():
        return workspace.analytics.disenchant.snapshot().metadata()

    @app.get("/api/v2/npc/catalog/document")
    def npc_catalog_document():
        return workspace.npc_catalog.snapshot()

    @app.post("/api/v2/npc/catalog/preview")
    def preview_npc_catalog(value: NpcPriceDocument):
        return workspace.npc_catalog.preview(value)

    @app.put("/api/v2/npc/catalog")
    def update_npc_catalog(value: NpcPriceDocument):
        if not workspace.analytics.lock.acquire(blocking=False):
            raise RuntimeError("Дождитесь завершения расчёта перед сменой справочника NPC")
        try:
            return workspace.npc_catalog.install(value)
        finally:
            workspace.analytics.lock.release()
            workspace.request_calculation()

    @app.get("/api/v2/disenchant/rules/document")
    def disenchant_document():
        return workspace.analytics.disenchant.snapshot()

    @app.post("/api/v2/disenchant/rules/preview")
    def preview_disenchant_rules(value: DisenchantRules):
        return workspace.analytics.disenchant.preview(value)

    @app.put("/api/v2/disenchant/rules")
    def update_disenchant_rules(value: DisenchantRules):
        if not workspace.analytics.lock.acquire(blocking=False):
            raise RuntimeError("Дождитесь завершения расчёта перед сменой правил распыления")
        try:
            return workspace.analytics.disenchant.install(value)
        finally:
            workspace.analytics.lock.release()
            workspace.request_calculation()

    @app.post("/api/v2/disenchant/rules/check", status_code=202)
    def check_disenchant_rules():
        return workspace.submit("disenchant_rules_check", workspace.check_disenchant_rules)

    @app.post("/api/v2/sync", status_code=202)
    def synchronize():
        return workspace.submit("sync", workspace.synchronize)

    @app.post("/api/v2/install-addon", status_code=202)
    def install_addon():
        if remote:
            raise ValueError("Установите аддон через Cennik Worker на игровом ПК")
        return workspace.submit("install_addon", workspace.install_addon)

    @app.post("/api/v2/backup", status_code=202)
    def backup():
        return workspace.submit("backup", workspace.backup)

    @app.post("/api/v2/scans", status_code=202)
    def import_scans(value: ImportRequest):
        return workspace.submit("import", lambda: workspace.import_scans(value.scans))

    @app.get("/api/v2/scans")
    def scans(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        return {"items": workspace.market.rows("SELECT * FROM scan_registry ORDER BY observed_at DESC LIMIT ? OFFSET ?", [limit, offset]),
                "total": workspace.market.rows("SELECT count(*) n FROM scan_registry")[0]["n"], "retention": "permanent"}

    @app.get("/api/v2/scans/{identity}")
    def scan(identity: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        rows = workspace.market.rows("SELECT * FROM scan_registry WHERE scan_id=?", [identity])
        if not rows:
            raise HTTPException(404, "Скан не найден")
        with (workspace.root / rows[0]["path"]).open("rb") as stream:
            with zstd.ZstdDecompressor().stream_reader(stream) as reader:
                payload = reader.read(64_000_001)
        if len(payload) > 64_000_000:
            raise ValueError("Скан превышает размер просмотра; исходник сохранён")
        data = json.loads(payload)
        packed = data["data"].split(";") if data["data"] else []
        records = []
        for line in packed[offset:offset + limit]:
            try:
                item, quantity, buyout, bid, remaining, variant = list(map(int, line.split(":")))[:6]
                variants = data.get("variants", [])
                info = variants[variant - 1] if 0 < variant <= len(variants) else {}
                records.append({"item_id": item, "quantity": quantity, "buyout_copper": buyout,
                    "bid_copper": bid, "time_left_category": remaining, **info})
            except (ValueError, TypeError):
                records.append({"invalid_raw_line": line[:1000]})
        return {"scan": rows[0], "items": records, "total": len(packed), "offset": offset, "limit": limit}

    @app.get("/api/v2/items")
    def items(q: str = Query("", max_length=100), limit: int = Query(50, ge=1, le=200)):
        workspace.market.names.seed(workspace.bootstrap.snapshot())
        translated = workspace.market.names.search(q, limit) if q else []
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        result = workspace.market.rows("SELECT item_id,name,vendor_sell_copper FROM items WHERE CAST(item_id AS VARCHAR)=? OR coalesce(name,'') ILIKE ? ESCAPE '\\' ORDER BY item_id LIMIT ?", [q, "%" + escaped + "%", limit])
        baseline = workspace.bootstrap.snapshot()
        known = {row["item_id"] for row in result}
        if translated:
            extra = workspace.market.rows("SELECT item_id,name,vendor_sell_copper FROM items WHERE item_id IN (SELECT unnest(?)) ORDER BY item_id", [translated])
            for row in extra:
                if row["item_id"] not in known and len(result) < limit:
                    result.append(row)
                    known.add(row["item_id"])
        if baseline:
            for identity, value in baseline.items.items():
                if len(result) >= limit:
                    break
                if identity not in known and (identity in translated or str(identity) == q or q.casefold() in (value.name or "").casefold()):
                    result.append({"item_id": identity, "name": value.name, "vendor_sell_copper": value.vendor_sell_copper, "source": "beta"})
        return {"items": result}

    @app.get("/api/v2/items/{item_id}")
    def item(item_id: int):
        if item_id <= 0:
            raise ValueError("Неверный ID")
        facts = workspace.market.rows("SELECT * FROM items WHERE item_id=?", [item_id])
        observations = workspace.market.rows("SELECT * EXCLUDE(payload_json) FROM price_snapshots WHERE item_id=? QUALIFY row_number() OVER(PARTITION BY source ORDER BY observed_at DESC)=1", [item_id])
        baseline = workspace.bootstrap.snapshot()
        if baseline and item_id in baseline.items:
            if not facts:
                facts = [{**baseline.item_facts.get(item_id, {}), **baseline.items[item_id].model_dump(), "origin_dataset": "beta"}]
            if not observations and workspace.settings().use_beta_prices and item_id in baseline.prices:
                observations = [{**baseline.prices[item_id].model_dump(mode="json"), "source": "Бета · " + baseline.source_market, "beta_baseline": True}]
        return {"item_id": item_id, "item": facts, "observations": observations,
                "offers": workspace.market.rows("SELECT * FROM working_offers WHERE item_id=? ORDER BY buyout::DECIMAL(38,6)/quantity LIMIT 500", [item_id]),
                "history": workspace.market.rows("SELECT observed_at,min_copper,median_copper,quantity,granularity FROM price_history WHERE item_id=? ORDER BY observed_at DESC LIMIT 100", [item_id]),
                "recipes": workspace.sniper.recipes_for(item_id)}

    @app.put("/api/v2/items/{item_id}/facts", status_code=202)
    def item_facts(item_id: int, value: ItemFacts):
        if item_id <= 0:
            raise ValueError("Неверный ID")
        return workspace.submit("item_facts", lambda: workspace.update_facts(item_id, value))

    @app.post("/api/v2/plans")
    def plan(value: PlanRequest):
        return workspace.plan(value)

    @app.get("/api/v2/plans")
    def plans():
        with workspace.store.connection() as db:
            return {"items": [json.loads(row[0]) for row in db.execute("SELECT payload FROM workspace_plans ORDER BY created_at DESC LIMIT 50")]}

    @app.post("/api/v2/plans/{identity}/publish", status_code=202)
    def publish_plan(identity: str):
        return workspace.submit("publish_plan", lambda: workspace.publish_plan(identity))

    @app.get("/api/v2/logs")
    def logs():
        path = workspace.root / "logs/workspace.log"
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 16000))
            return {"text": stream.read().decode("utf-8", errors="replace")}

    @app.get("/api/v2/sniper")
    def sniper():
        return workspace.sniper.listing()

    @app.get("/api/v2/sniper/recipes/{item_id}")
    def sniper_recipes(item_id: int):
        return {"items": workspace.sniper.recipes_for(item_id)}

    @app.post("/api/v2/sniper/materials/preview")
    def sniper_material_preview(value: SnipeMaterials):
        return workspace.sniper.material_preview(value)

    @app.post("/api/v2/sniper/materials")
    def sniper_materials(value: SnipeMaterials):
        return workspace.sniper.add_materials(value)

    @app.put("/api/v2/sniper/rules")
    def sniper_save(value: SnipeRule):
        return workspace.sniper.save(value)

    @app.put("/api/v2/sniper/budget")
    def sniper_budget(value: SnipeBudget):
        return workspace.sniper.budget(value.budget_copper)

    @app.delete("/api/v2/sniper/rules/{identity}")
    def sniper_delete(identity: str):
        return workspace.sniper.delete(identity)

    @app.get("/api/v2/sniper/export")
    def sniper_export():
        return workspace.sniper.export()

    @app.post("/api/v2/sniper/preview")
    def sniper_preview(value: SnipeImport):
        return workspace.sniper.preview(value.text)

    @app.post("/api/v2/sniper/import")
    def sniper_import(value: SnipeImport):
        return workspace.sniper.apply(value.text)

    @app.post("/api/v2/sniper/rules/{identity}/reference", status_code=202)
    def sniper_reference(identity: str):
        def refresh_reference():
            rule = next((r for r in workspace.sniper.wire()["rules"] if r["id"] == identity), None)
            if not rule:
                raise ValueError("Цель не найдена")
            local = workspace.sniper.reference(identity)
            if local["copper"] and local["source"] == "Cennik replicate":
                return local
            workspace.refresh(RefreshRequest(dataset="history", item_id=rule["item_id"], range="7d"))
            return workspace.sniper.reference(identity)
        return workspace.submit("sniper_reference", refresh_reference)

    app.include_router(analytics_router(workspace.analytics,recalculate_callback=lambda:workspace.submit('analytics',workspace.calculate)), prefix="/api/v2")
    app.include_router(addon_router(workspace.exchange, workspace.market.lock, lambda: False), prefix="/api/v2")
    return app
