from __future__ import annotations

import ipaddress
import json
from contextlib import asynccontextmanager
from pathlib import Path

import zstandard as zstd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from cennik.addon.api import addon_router
from cennik.api.routes import analytics_router
from cennik.core.models import AnalyticsConfig
from cennik.strategies.craft_graph import CraftGraphSettings

from .models import ImportRequest, ItemFacts, PlanRequest, RefreshRequest, WorkspaceSettings
from .service import Workspace
from .sniper import SnipeBudget, SnipeImport, SnipeMaterials, SnipeRule


def create_app(root: Path) -> FastAPI:
    workspace = Workspace(root)

    @asynccontextmanager
    async def lifespan(app):
        workspace.thread.start()
        try:
            yield
        finally:
            workspace.close()

    app = FastAPI(title="Cennik Local", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.workspace = workspace

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        try:
            loopback = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            loopback = False
        origin = request.headers.get("origin")
        if (not loopback or request.url.hostname not in {"localhost", "127.0.0.1", "::1"}
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
    def index():
        return FileResponse(static / "next.html")

    @app.get("/healthz")
    def health():
        return {"status": "ok", "local_mode": True, "workspace": str(workspace.root), "profile": "cennik-next"}

    @app.get("/api/v2/overview")
    def overview():
        return workspace.overview()

    @app.get("/api/v2/settings")
    def settings():
        return {"workspace": workspace.settings(), "analytics": workspace.store.settings(),
            "schemas": {"workspace": WorkspaceSettings.model_json_schema(), "analytics": AnalyticsConfig.model_json_schema(),
                        "craft_graph": CraftGraphSettings.model_json_schema()}}

    @app.put("/api/v2/settings")
    def save(value: WorkspaceSettings):
        return workspace.save_settings(value)

    @app.post("/api/v2/refresh", status_code=202)
    def refresh(value: RefreshRequest):
        return workspace.submit(value.dataset, lambda: workspace.refresh(value))

    @app.post("/api/v2/recalculate", status_code=202)
    @app.post("/api/v2/analytics/recalculate", status_code=202)
    def recalculate():
        return workspace.submit("analytics", workspace.calculate)

    @app.post("/api/v2/sync", status_code=202)
    def synchronize():
        return workspace.submit("sync", workspace.synchronize)

    @app.post("/api/v2/install-addon", status_code=202)
    def install_addon():
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
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return {"items": workspace.market.rows("SELECT item_id,name,vendor_sell_copper FROM items WHERE CAST(item_id AS VARCHAR)=? OR coalesce(name,'') ILIKE ? ESCAPE '\\' ORDER BY item_id LIMIT ?", [q, "%" + escaped + "%", limit])}

    @app.get("/api/v2/items/{item_id}")
    def item(item_id: int):
        if item_id <= 0:
            raise ValueError("Неверный ID")
        return {"item_id": item_id, "item": workspace.market.rows("SELECT * FROM items WHERE item_id=?", [item_id]),
                "observations": workspace.market.rows("SELECT * EXCLUDE(payload_json) FROM price_snapshots WHERE item_id=? QUALIFY row_number() OVER(PARTITION BY source ORDER BY observed_at DESC)=1", [item_id]),
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

    app.include_router(analytics_router(workspace.analytics), prefix="/api/v2")
    app.include_router(addon_router(workspace.exchange, workspace.market.lock, lambda: False), prefix="/api/v2")
    return app
