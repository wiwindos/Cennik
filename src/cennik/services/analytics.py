from __future__ import annotations

import threading
import uuid
from collections.abc import Iterable
from contextlib import ExitStack
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import duckdb

from ahledger.util import digest, dumps, exclusive_lock
from cennik.core.interfaces import EvaluationContext, Strategy
from cennik.core.models import AnalyticsSettings, Opportunity, Run
from cennik.core.performance import Performance
from cennik.data.duckdb import DuckDBProvider, fingerprint
from cennik.engines.prices import PriceEngine
from cennik.storage.sqlite import Store
from cennik.strategies import default_registry
from cennik.strategies.registry import StrategyRegistry


class DataNotReady(ValueError):
    pass


def rank(opportunity: Opportunity, context: EvaluationContext) -> Opportunity:
    config = context.config
    profit = Decimal(opportunity.potential_profit_copper)
    profit_component = profit / (profit + config.ranking_profit_scale_copper)
    roi_component = min(Decimal(1), opportunity.roi_percent / config.ranking_roi_cap_percent)
    freshness = max(Decimal(0), 1 - Decimal(opportunity.data_age_seconds) / config.max_price_age_seconds)
    quality = {"limited": Decimal("0.4"), "moderate": Decimal("0.7"), "strong": Decimal(1)}[opportunity.confidence]
    penalty = 1 / (1 + config.ranking_risk_penalty * len(opportunity.risk_flags))
    price = context.prices.current.get(opportunity.item_id)
    liquidity = Decimal(1)
    # A modest tie-break proxy based on source-estimated sales / observed supply.
    # This is not a probability and missing liquidity gets no invented estimate.
    if price and price.quantity and price.sales_estimated_per_day is not None and "stale_sales_estimate" not in opportunity.risk_flags:
        proxy = min(Decimal(1), max(Decimal(0), price.sales_estimated_per_day) / price.quantity)
        liquidity += proxy / 10
    score = 100 * (profit_component * Decimal("0.45") + roi_component * Decimal("0.25") + freshness * Decimal("0.15") + quality * Decimal("0.15")) * penalty * liquidity
    opportunity.rating = score.quantize(Decimal("0.0001"))
    opportunity.details["ranking"] = {
        "formula": "100*(0.45*P/(P+scale)+0.25*min(ROI/cap,1)+0.15*freshness+0.15*quality)/(1+risk_penalty*flags)*liquidity",
        "profit_component": str(profit_component), "roi_component": str(roi_component),
        "freshness": str(freshness), "quality": str(quality), "risk_multiplier": str(penalty),
        "liquidity_multiplier": str(liquidity), "note": "deterministic ranking, not sale probability",
    }
    return opportunity


class AnalyticsService:
    def __init__(self, root: Path, registry: StrategyRegistry | None = None):
        self.root, self.path = root, root / "analytics/ahledger.duckdb"
        self.directory = root / "cennik"
        self.store = Store(self.directory / "cennik.sqlite")
        self.registry = registry or default_registry()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self._identity: tuple[int, int, int, int, str] | None = None
        self._performance: Performance | None = None
        try:
            with exclusive_lock(self.directory):
                self.store.recover()
        except RuntimeError:
            pass  # A different application instance owns a live manual calculation.

    def generation(self, *, refresh: bool = False) -> str:
        if not self.path.is_file():
            raise DataNotReady("Готовая DuckDB отсутствует. Постройте базу существующей кнопкой AHledger.")
        stat = self.path.stat()
        wal = Path(str(self.path) + ".wal")
        wal_stat = wal.stat() if wal.is_file() else None
        identity = (stat.st_mtime_ns, stat.st_size, wal_stat.st_mtime_ns if wal_stat else 0, wal_stat.st_size if wal_stat else 0)
        if refresh or self._identity is None or self._identity[:4] != identity:
            self._identity = (*identity, fingerprint(self.path))
        return self._identity[4]

    def markets(self) -> list[dict]:
        if not self.path.is_file():
            return []
        with DuckDBProvider(self.path, "", datetime.now(UTC)) as provider:
            return provider.markets()

    def update_settings(self, settings: AnalyticsSettings) -> AnalyticsSettings:
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("Расчёт уже выполняется; дождитесь завершения перед сменой рынка или настроек.")
        try:
            with exclusive_lock(self.directory):
                settings.config = self.registry.normalize_config(settings.config)
                if settings.active_market is not None and settings.active_market not in {m["market"] for m in self.markets()}:
                    raise ValueError("Рынок отсутствует в готовой DuckDB.")
                self.store.save_settings(settings)
        finally:
            self.lock.release()
        return settings

    def start(self, *, background: bool = True, as_of: datetime | None = None) -> dict:
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("Аналитика уже выполняется.")
        self._performance = Performance()
        stack = ExitStack()
        try:
            stack.enter_context(exclusive_lock(self.directory))
            # The lock proves an abandoned 'running' record has no live owner.
            self.store.recover()
            settings = self.store.settings()
            settings.config = self.registry.normalize_config(settings.config)
            if settings.active_market is None:
                raise ValueError("Выберите один активный рынок.")
            with self._performance.measure("initial_fingerprint"):
                generation = self.generation(refresh=True)
            with self._performance.measure("market_check"):
                markets = self.markets()
            if settings.active_market not in {m["market"] for m in markets}:
                raise DataNotReady("Выбранный рынок отсутствует в текущем поколении DuckDB.")
            strategies = self.registry.selected(settings.config.enabled_strategies)
            versions = {s.id: s.version for s in strategies}
            identity = digest(dumps({"market": settings.active_market, "generation": generation,
                                     "config": settings.config.model_dump(mode="json"),
                                     "strategies": versions, "engine_version": "0.2.0"}).encode())
            cached = self.store.cached(identity, summary=True)
            if cached:
                self.store.publish_cached(cached)
                self._performance.stop()
                stack.close()
                self.lock.release()
                return {"run": cached.model_dump(mode="json"), "reused": True}
            now = datetime.now(UTC)
            run = Run(run_id=uuid.uuid4().hex, market=settings.active_market, started_at=now,
                      as_of=as_of or now, generation=generation, config=settings.config,
                      strategy_versions=versions)
            self.store.begin(run, identity)
        except Exception:
            self._performance.stop()
            stack.close()
            self.lock.release()
            raise
        response = {"run": run.model_dump(mode="json"), "reused": False}

        def execute():
            try:
                self.calculate(run)
            finally:
                stack.close()
                self.lock.release()

        if background:
            self.thread = threading.Thread(target=execute, name="cennik-manual-analysis", daemon=True)
            try:
                self.thread.start()
            except Exception as exc:
                self._performance.stop()
                run.status, run.error, run.finished_at = "failed", str(exc), datetime.now(UTC)
                self.store.finish(run, [])
                stack.close()
                self.lock.release()
                raise
        else:
            execute()
            response["run"] = run.model_dump(mode="json")
        return response

    def calculate(self, run: Run) -> None:
        opportunities: list[Opportunity] = []
        performance = self._performance or Performance()
        self._performance = performance
        try:
            with DuckDBProvider(self.path, run.market, run.as_of) as provider:
                prices = PriceEngine(provider, run.config, performance.measure)
                run.diagnostics["quality"] = prices.quality
                run.diagnostics["observed_items"] = len(prices.current)
                if not any(p.min_copper is not None and p.min_copper > 0 for p in prices.current.values()):
                    raise DataNotReady("Для выбранного рынка нет наблюдаемых цен. Это отсутствие данных, а не отсутствие прибыльных возможностей.")
                with performance.measure("items"):
                    items = provider.items()
                context = EvaluationContext(provider, run.config, run.run_id, run.started_at, prices, items)
                run.diagnostics["rejections"] = context.diagnostics
                unique: dict[tuple, Opportunity] = {}
                for strategy in self.registry.selected(run.config.enabled_strategies):
                    with performance.measure("strategy_" + strategy.id):
                        values = strategy.evaluate(context)
                    with performance.measure("ranking"):
                        self._rank_candidates(values, strategy, context, unique, run)
                opportunities = sorted(unique.values(), key=lambda o: (-o.rating, o.id))
            with performance.measure("final_fingerprint"):
                final_generation = fingerprint(self.path)
            if final_generation != run.generation:
                raise DataNotReady("Поколение DuckDB изменилось во время расчёта; запустите пересчёт вручную.")
            run.status, run.signal_count = "completed", len(opportunities)
            run.finished_at = datetime.now(UTC)
            run.diagnostics["performance"] = performance.snapshot()
            with performance.measure("save_results"):
                self.store.finish(run, opportunities)
        except Exception as exc:
            run.status, run.error, run.finished_at = "failed", str(exc)[:2000], datetime.now(UTC)
            run.signal_count = 0
            run.diagnostics["performance"] = performance.snapshot()
            self.store.finish(run, [])
        finally:
            performance.stop()

    @staticmethod
    def _rank_candidates(values: Iterable[Opportunity], strategy: Strategy, context: EvaluationContext,
                         unique: dict[tuple, Opportunity], run: Run) -> None:
        for value in values:
            # All plugins obey the same scope and integer-money contract.
            candidate = Opportunity.model_validate(value.model_dump())
            if (candidate.market != run.market or candidate.run_id != run.run_id
                or candidate.strategy_id != strategy.id or candidate.strategy_version != strategy.version
                or candidate.observed_at > run.as_of):
                raise ValueError("Strategy returned an opportunity outside its run context")
            key = (candidate.strategy_id, candidate.item_id, candidate.action,
                   candidate.details.get("spell_id"), candidate.details.get("version_hash"))
            if key not in unique:
                unique[key] = rank(candidate, context)

    def status(self) -> dict:
        settings = self.store.settings()
        generation, error, observed = None, None, None
        ready = False
        try:
            generation = self.generation()
            markets = self.markets()
            ready = True
            chosen = next((m for m in markets if m["market"] == settings.active_market), None)
            observed = chosen["observed_at"] if chosen else None
            if settings.active_market is None:
                error = "Выберите активный рынок."
            elif chosen is None:
                error = "Выбранный рынок отсутствует в текущей базе."
            elif observed is None:
                error = "Для выбранного рынка нет наблюдаемых цен."
        except (ValueError, OSError, duckdb.Error) as exc:
            error = str(exc)
        latest = self.store.latest(settings.active_market, summary=True)
        published = self.store.latest(settings.active_market, published=True, summary=True)
        if latest and latest.error and latest.config == settings.config and latest.generation == generation:
            error = latest.error
        age = max(0, int((datetime.now(UTC) - observed).total_seconds())) if observed else None
        return {"ready": ready, "active_market": settings.active_market, "generation": generation,
                "observed_at": observed, "data_age_seconds": age,
                "observation_stale": age is not None and age > settings.config.max_price_age_seconds,
                "running": self.lock.locked() or (latest is not None and latest.status == "running"),
                "progress": self._performance.snapshot() if self._performance else None,
                "latest_run": latest.model_dump(mode="json") if latest else None,
                "published_run": published.model_dump(mode="json") if published else None,
                "opportunity_count": published.signal_count if published else 0,
                "result_generation_matches": published.generation == generation if published else False,
                "settings_match": published.config == settings.config if published else False,
                "strategy_versions_match": published.strategy_versions == {
                    s.id: s.version for s in self.registry.selected(settings.config.enabled_strategies)
                } if published else False,
                "error": error, "automatic_recalculation": False,
                "attribution": "Prices from https://ahledger.com"}

    def shutdown(self) -> None:
        if self.thread:
            self.thread.join()
