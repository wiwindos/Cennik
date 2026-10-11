from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from cennik.core.models import AnalyticsSettings
from cennik.services.analytics import AnalyticsService, DataNotReady


def analytics_router(service: AnalyticsService) -> APIRouter:
    router = APIRouter(prefix="/analytics")

    @router.get("/markets")
    def markets():
        return {"markets": service.markets()}

    @router.get("/settings")
    def settings():
        return service.store.settings()

    @router.put("/settings")
    def save_settings(value: AnalyticsSettings):
        try:
            return service.update_settings(value)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/status")
    def status():
        return service.status()

    @router.post("/recalculate", status_code=202)
    def recalculate():
        try:
            return service.start()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except DataNotReady as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/opportunities")
    def opportunities(
        strategy: str | None = Query(None, max_length=100),
        min_profit: int = Query(0, ge=0), min_roi: Decimal = Query(Decimal(0), ge=0),
        confidence: Literal["limited", "moderate", "strong"] | None = None,
        sort: Literal["rating", "profit", "roi", "freshness"] = "rating",
        limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0, le=1000000),
    ):
        return service.store.listing(service.store.settings().active_market, strategy=strategy,
            min_profit=min_profit, min_roi=str(min_roi), confidence=confidence, sort=sort,
            limit=limit, offset=offset)

    @router.get("/opportunities/{identity}")
    def detail(identity: str):
        value = service.store.detail(service.store.settings().active_market, identity)
        if value is None:
            raise HTTPException(404, "Возможность не найдена для активного рынка и опубликованного запуска.")
        return value

    return router
