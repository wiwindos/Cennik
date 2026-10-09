from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

import duckdb
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .web_control import Controller, JobRequest


def create_app(settings: Settings | None = None, api_key: str | None = None) -> FastAPI:
    key = api_key if api_key is not None else os.environ.get("AHL_WEB_API_KEY", "")
    if len(key) < 32:
        raise ValueError("Set AHL_WEB_API_KEY to a random secret of at least 32 characters")
    settings = settings or Settings.read(Path(os.getenv("AHL_CONFIG", "config.toml")))
    controller = Controller(settings)
    root = controller.root

    @asynccontextmanager
    async def lifespan(app):
        yield
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
    header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def authorize(provided: str | None = Depends(header)):
        if not provided or not hmac.compare_digest(provided.encode(), key.encode()):
            raise HTTPException(401, "Неверный секретный ключ")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
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
        return {"status": "ok", "downloads_autostart": False}

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(static / "index.html")

    router = APIRouter(prefix="/api", dependencies=[Depends(authorize)])

    @router.get("/status")
    def status():
        return controller.snapshot()

    @router.post("/jobs", status_code=202)
    def start_job(request: JobRequest):
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
        if not path.exists():
            return {"items": [], "limit": limit, "offset": offset}
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
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
            generation = json.loads(current.read_text("utf-8")).get("directory", "")
            if re.fullmatch(r"[0-9a-f]{32}", generation):
                for path in (root / "parquet" / generation).glob("*.parquet"):
                    files[path.name] = path
        return {name: path for name, path in files.items() if path.is_file()}

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
        return FileResponse(path, filename=name)

    @router.get("/openapi.json")
    def openapi():
        return app.openapi()

    app.include_router(router)
    return app
