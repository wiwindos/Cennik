from collections.abc import Callable
from contextlib import AbstractContextManager

from fastapi import APIRouter, HTTPException, Query

from cennik.addon.local_sync import LocalBridge
from cennik.addon.models import ExportRequest, ReportBatch
from cennik.addon.service import AddonExchange, ExchangeConflict


def addon_router(
    exchange: AddonExchange, lock: AbstractContextManager, updating: Callable[[], bool],
    local_bridge: LocalBridge | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/addon")

    def translate(exc):
        return HTTPException(
            409 if isinstance(exc, (ExchangeConflict, RuntimeError)) else 422, str(exc)
        )

    @router.get("/local-sync/status")
    def local_status():
        return local_bridge.status() if local_bridge else {"available": False}

    @router.post("/local-sync")
    def local_sync():
        if local_bridge is None:
            raise HTTPException(404, "Синхронизация с игрой доступна в локальном режиме.")
        with lock:
            if updating():
                raise HTTPException(409, "Дождитесь завершения обновления базы.")
            try:
                return local_bridge.synchronize()
            except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
                raise translate(exc) from exc

    @router.post("/exports", status_code=201)
    def export(request: ExportRequest):
        with lock:
            if updating():
                raise HTTPException(409, "Дождитесь завершения обновления базы.")
            try:
                return exchange.export(request)
            except (ValueError, RuntimeError) as exc:
                raise translate(exc) from exc

    @router.get("/packages")
    def packages(market: str = Query(min_length=1, max_length=200), include_expired: bool = False):
        try:
            return {"format_version": 1, "packages": exchange.packages(market, include_expired)}
        except ValueError as exc:
            raise translate(exc) from exc

    @router.post("/reports")
    def reports(batch: ReportBatch):
        try:
            return {"accepted_event_ids": exchange.reports(batch)}
        except ValueError as exc:
            raise translate(exc) from exc

    @router.post("/packages/{identity}/renew")
    def renew(identity: str):
        with lock:
            if updating():
                raise HTTPException(409, "Дождитесь обновления базы.")
            try:
                return exchange.renew(identity)
            except (ValueError, RuntimeError) as exc:
                raise translate(exc) from exc

    @router.get("/history")
    def history(limit: int = Query(100, ge=1, le=500)):
        return {
            "market": exchange.store.settings().active_market,
            "events": exchange.history(limit),
        }

    return router
