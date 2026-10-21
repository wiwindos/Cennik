from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import os
import re
import sqlite3
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import duckdb
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from starlette.responses import JSONResponse

from cennik.addon.api import addon_router
from cennik.addon.local_sync import LocalBridge
from cennik.addon.service import AddonExchange
from cennik.api.routes import analytics_router
from cennik.services.analytics import AnalyticsService

from .config import Settings
from .incremental import parquet_status
from .util import exclusive_lock
from .web_control import Controller, JobRequest


def create_app(settings: Settings | None = None, api_key: str | None = None, *, local: bool = False) -> FastAPI:
    key = api_key if api_key is not None else os.environ.get("AHL_WEB_API_KEY", "")
    if not local and len(key) < 32:
        raise ValueError("Set AHL_WEB_API_KEY to a random secret of at least 32 characters")
    settings = settings or Settings.read(Path(os.getenv("AHL_CONFIG", "config.toml")))
    controller = Controller(settings)
    root = controller.root
    analytics = AnalyticsService(root)
    operation_lock = threading.RLock()

    @asynccontextmanager
    async def lifespan(app):
        yield
        await asyncio.to_thread(analytics.shutdown)
        await asyncio.to_thread(controller.shutdown)

    app = FastAPI(
        title="AHledger Archive API",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.controller = controller
    app.state.analytics = analytics
    exchange = AddonExchange(analytics)
    app.state.addon = exchange
    local_bridge = LocalBridge(root, exchange) if local else None
    app.state.local_bridge = local_bridge
    header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def authorize(provided: str | None = Depends(header)):
        if local:
            return
        if not provided or not hmac.compare_digest(provided.encode(), key.encode()):
            raise HTTPException(401, "Неверный секретный ключ")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if local:
            try:
                loopback = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
            except ValueError:
                loopback = False
            origin = request.headers.get("origin")
            if (not loopback or request.url.hostname not in {"127.0.0.1", "localhost", "::1"}
                or (origin is not None and origin != str(request.base_url).rstrip("/"))):
                return JSONResponse({"detail": "Локальный режим доступен только с этой машины и страницы приложения."}, status_code=403)
        writing = controller.info.get("action") == "update" and controller.info.get("state") in {"building", "stopping"}
        if writing and request.url.path.startswith("/api/") and request.url.path not in {"/api/status", "/api/logs", "/api/jobs/stop"}:
            return JSONResponse({"detail": "Применяется пакет обновления. Дождитесь завершения записи базы."}, status_code=409)
        response = await call_next(request)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                "Cache-Control": "no-store",
            }
        )
        return response

    @app.get("/healthz")
    def health():
        value = {"status": "ok", "downloads_autostart": False, "local_mode": local}
        if local:
            value["workspace"] = str(root)
        return value

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(static / "index.html")

    router = APIRouter(prefix="/api", dependencies=[Depends(authorize)])

    @router.get("/status")
    def status():
        value = controller.snapshot()
        writing = value["run"].get("action") == "update" and value["run"].get("state") in {"building", "stopping"}
        if local and analytics.path.exists() and not writing:
            value["markets"] = [{"id": m["market"], "label": m["label"]} for m in analytics.markets()]
            if (root / "analytics/imported.json").exists() and not (root / "analytics/build_result.json").exists():
                value["analytics_updated_at"] = json.loads((root / "analytics/imported.json").read_text("utf-8")).get("imported_at")
        value["local_mode"] = local
        value["imported_snapshot"] = local and (root / "analytics/imported.json").exists()
        value["raw_available"] = (root / "raw/metadata").exists() and any((root / "raw/metadata").glob("*.json"))
        value["market_update_available"] = analytics.path.exists()
        value["parquet"] = {"ready": False, "stale": False} if writing or not analytics.path.exists() else parquet_status(root, settings)
        return value

    @router.post("/jobs", status_code=202)
    def start_job(request: JobRequest):
        with operation_lock:
            return launch_job(request)

    def launch_job(request: JobRequest):
        if request.action == "update":
            if analytics.lock.locked():
                raise HTTPException(409, "Дождитесь завершения расчёта аналитики перед обновлением базы.")
            if request.markets[0] not in {row["market"] for row in analytics.markets()}:
                raise HTTPException(422, "Рынок отсутствует в текущей базе.")
        if local and request.action in {"download", "build"} and (root / "analytics/imported.json").exists():
            raise HTTPException(409, "Подключена серверная DuckDB без её raw-архива. Автоматическая пересборка из прежних локальных ответов заменила бы серверные данные. Для обновления этой базы нужен её raw-архив либо отдельное добавление новых данных.")
        if local and request.action == "build" and not any((root / "raw/metadata").glob("*.json")):
            raise HTTPException(409, "Подключена готовая DuckDB, но исходный raw-архив отсутствует. Пересборка невозможна; аналитика и история уже доступны.")
        try:
            return controller.start(request)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/jobs/stop")
    def stop_job():
        return controller.stop()

    @router.get("/logs")
    def logs():
        return {"text": controller.logs()}

    @router.get("/items")
    def items(
        q: str = Query("", max_length=100),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0, le=1000000),
    ):
        path = root / "state.sqlite"
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        if local and analytics.path.exists():
            with duckdb.connect(str(analytics.path), read_only=True) as source_db:
                source_rows = source_db.execute("""SELECT item_id,name FROM items
                    WHERE CAST(item_id AS VARCHAR)=? OR coalesce(name,'') ILIKE ? ESCAPE '\\'
                    ORDER BY item_id LIMIT ? OFFSET ?""", [q, "%" + escaped + "%", limit, offset]).fetchall()
            return {"items": [{"id": row[0], "name": row[1]} for row in source_rows], "limit": limit, "offset": offset}
        if not path.exists():
            return {"items": [], "limit": limit, "offset": offset}
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            rows = connection.execute(
                "SELECT id,payload FROM item_candidates WHERE CAST(id AS TEXT)=? OR coalesce(json_extract(payload,'$.name'),'') LIKE ? ESCAPE '\\' ORDER BY id LIMIT ? OFFSET ?",
                [q, "%" + escaped + "%", limit, offset],
            )
            return {
                "items": [{"id": row[0], **json.loads(row[1])} for row in rows],
                "limit": limit,
                "offset": offset,
            }
        finally:
            connection.close()

    @router.get("/prices/{market}/{item_id}")
    def prices(
        market: str, item_id: int, granularity: str = "hour", limit: int = Query(500, ge=1, le=1000)
    ):
        if item_id <= 0 or granularity not in {"scan", "hour", "day"}:
            raise HTTPException(
                422, "item_id должен быть положительным; granularity: scan, hour, day"
            )
        path = root / "analytics/ahledger.duckdb"
        if not path.exists():
            raise HTTPException(
                503, "Аналитическая база ещё не построена; статус доступен в /api/status"
            )
        connection = duckdb.connect(str(path), read_only=True)
        try:
            result = connection.execute(
                "SELECT observed_at,min_copper,median_copper,p25_copper,p75_copper,quantity,auctions,source FROM price_history WHERE market=? AND item_id=? AND granularity=? ORDER BY observed_at DESC LIMIT ?",
                [market, item_id, granularity, limit],
            )
            columns = [column[0] for column in result.description]
            rows = [dict(zip(columns, row, strict=True)) for row in result.fetchall()]
            return {
                "market": market,
                "item_id": item_id,
                "granularity": granularity,
                "currency": "copper",
                "points": list(reversed(rows)),
                "limit": limit,
                "attribution": "Prices from https://ahledger.com",
            }
        finally:
            connection.close()

    def available_files() -> dict[str, Path]:
        files = {
            "ahledger.duckdb": root / "analytics/ahledger.duckdb",
            "report.json": root / "reports/download_report.json",
            "verification.json": root / "reports/verification.json",
        }
        current = root / "parquet/CURRENT.json"
        if current.exists():
            generation = json.loads(current.read_text("utf-8")).get("directory", "") if parquet_status(root, settings)["ready"] else ""
            if re.fullmatch(r"[0-9a-f]{32}", generation):
                for path in (root / "parquet" / generation).glob("*.parquet"):
                    files[path.name] = path
        return {name: path for name, path in files.items() if path.is_file() and not (name == "ahledger.duckdb" and Path(str(path) + ".wal").exists())}

    @router.get("/files")
    def files():
        return {
            "files": [
                {"name": name, "bytes": path.stat().st_size, "url": "/api/files/" + name}
                for name, path in available_files().items()
            ]
        }

    @router.get("/files/{name}")
    def download_file(name: str):
        path = available_files().get(name)
        if path is None:
            raise HTTPException(404, "Файл не найден")
        if name == "ahledger.duckdb":
            def stream_database():
                with exclusive_lock(root), path.open("rb") as source:
                    if Path(str(path) + ".wal").exists():
                        raise RuntimeError("Database recovery must finish before downloading a snapshot")
                    while chunk := source.read(1024 * 1024):
                        yield chunk
            return StreamingResponse(stream_database(), media_type="application/octet-stream",
                                     headers={"Content-Disposition": 'attachment; filename="ahledger.duckdb"'})
        return FileResponse(path, filename=name)

    @router.get("/openapi.json")
    def openapi():
        return app.openapi()

    router.include_router(analytics_router(analytics, updating=controller.updating, operation_lock=operation_lock))
    router.include_router(addon_router(exchange,operation_lock,controller.updating,local_bridge))
    app.include_router(router)
    return app
