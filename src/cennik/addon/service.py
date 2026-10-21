from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from ahledger.util import exclusive_lock
from cennik.addon.models import (
    MAX_INTEGER,
    ExportRequest,
    Package,
    PurchaseLine,
    ReportBatch,
    StrategyId,
    Task,
    epoch,
)
from cennik.core.models import Opportunity, Run
from cennik.services.analytics import AnalyticsService


class ExchangeConflict(ValueError):
    pass


class AddonExchange:
    def __init__(self, analytics: AnalyticsService):
        self.analytics, self.store = analytics, analytics.store
        with self.store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS addon_packages(
                    id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,request_hash TEXT NOT NULL,
                    market TEXT NOT NULL,run_id TEXT NOT NULL,expires_at TEXT NOT NULL,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS addon_tasks(
                    id TEXT PRIMARY KEY,opportunity_id TEXT NOT NULL,run_id TEXT NOT NULL,market TEXT NOT NULL,
                    package_id TEXT NOT NULL REFERENCES addon_packages(id),payload TEXT NOT NULL,
                    UNIQUE(run_id,opportunity_id));
                CREATE TABLE IF NOT EXISTS addon_events(
                    id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES addon_tasks(id),market TEXT NOT NULL,
                    observed_at TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS addon_events_market ON addon_events(market,observed_at);
            """)

    def ready(self) -> dict:
        status = self.analytics.status()
        if (
            not status["published_run"]
            or not status["active_market"]
            or not all(
                status[key]
                for key in (
                    "result_generation_matches",
                    "settings_match",
                    "strategy_versions_match",
                )
            )
            or status["observation_stale"]
        ):
            raise ExchangeConflict(
                "Расчёт устарел или не соответствует базе/настройкам. Выполните ручной пересчёт и обновите данные, если цены устарели."
            )
        return status

    @staticmethod
    def task(opportunity: Opportunity, run: Run, request: ExportRequest, created: datetime) -> Task:
        supported = {"vendor_arbitrage", "historical_discount", "simple_craft", "craft_graph"}
        if opportunity.strategy_id not in supported:
            raise ValueError("Unsupported addon strategy")
        if any(
            v > MAX_INTEGER
            for v in (
                opportunity.quantity,
                opportunity.buy_price_copper,
                opportunity.reference_price_copper,
                opportunity.required_capital_copper,
                opportunity.potential_profit_copper,
            )
        ):
            raise ValueError("Opportunity exceeds Lua's exact integer range")
        lines = []
        if opportunity.strategy_id in {"vendor_arbitrage", "historical_discount"}:
            entries: list[dict[str, Any]] = [
                {
                    "item_id": opportunity.item_id,
                    "item_name": opportunity.item_name,
                    "quantity": opportunity.quantity,
                    "unit_price_copper": opportunity.buy_price_copper,
                    "source": "auction",
                }
            ]
        else:
            entries = opportunity.details.get("materials", [])
            if not entries or (
                opportunity.strategy_id == "craft_graph" and not opportunity.details.get("steps")
            ):
                raise ValueError("Incomplete craft procurement plan")
        for entry in entries:
            if entry.get("source") not in {"auction", "npc"}:
                raise ValueError("Unknown material source")
            item_id = entry["item_id"]
            source = entry["source"]
            limit = (
                request.unit_limits.get(item_id, entry["unit_price_copper"])
                if source == "auction"
                else entry["unit_price_copper"]
            )
            lines.append(
                PurchaseLine(
                    item_id=item_id,
                    item_name=entry.get("item_name") or str(item_id),
                    quantity=entry["quantity"],
                    unit_limit_copper=limit,
                    source=source,
                )
            )
        cost = sum(line.quantity * line.unit_limit_copper for line in lines)
        if not 0 < cost <= MAX_INTEGER:
            raise ValueError("Procurement cost exceeds supported integer range")
        fee = Fraction(run.config.auction_cut_percent / 100)
        discount = Fraction(run.config.discount_percent / 100)
        if max(fee.denominator, discount.denominator) > 1_000_000:
            raise ValueError(
                "Addon supports at most four decimal places in fee/discount percentages"
            )
        expires = created + timedelta(seconds=request.expires_in_seconds)
        return Task(
            task_id=uuid5(NAMESPACE_URL, f"cennik/{run.run_id}/{opportunity.id}").hex,
            opportunity_id=opportunity.id,
            run_id=run.run_id,
            strategy_id=cast(StrategyId, opportunity.strategy_id),
            strategy_version=opportunity.strategy_version,
            market=opportunity.market,
            item_id=opportunity.item_id,
            item_name=opportunity.item_name,
            action=opportunity.action,
            predicted_profit_copper=opportunity.potential_profit_copper,
            max_total_cost_copper=cost,
            min_profit_copper=run.config.min_profit_copper,
            lines=lines,
            opportunity=opportunity,
            fee_numerator=fee.numerator,
            fee_denominator=fee.denominator,
            discount_numerator=discount.numerator,
            discount_denominator=discount.denominator,
            created_at=created,
            expires_at=expires,
            expires_at_epoch=epoch(expires),
        )

    def export(self, request: ExportRequest) -> Package:
        if len(request.opportunity_ids) != len(set(request.opportunity_ids)):
            raise ValueError("Duplicate opportunities")
        if any(not 0 < v <= MAX_INTEGER for v in request.unit_limits.values()):
            raise ValueError("Price limits must be positive integer copper")
        digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        if not self.analytics.lock.acquire(blocking=False):
            raise ExchangeConflict("Дождитесь завершения расчёта.")
        try:
            with exclusive_lock(self.analytics.directory):
                status = self.ready()
                with self.store.connection() as db:
                    old = db.execute(
                        "SELECT request_hash,payload FROM addon_packages WHERE request_id=?",
                        [request.request_id],
                    ).fetchone()
                    if old:
                        if old[0] != digest:
                            raise ExchangeConflict("Request id reused with different limits")
                        return Package.model_validate_json(old[1])
                    run = self.store.latest(status["active_market"], published=True, summary=True)
                    assert run is not None
                    now = datetime.now(UTC)
                    tasks = []
                    for identity in request.opportunity_ids:
                        value = self.store.detail(run.market, identity)
                        if value is None or value.run_id != run.run_id:
                            raise ExchangeConflict(
                                "Возможность больше не относится к опубликованному расчёту активного рынка."
                            )
                        if db.execute(
                            "SELECT 1 FROM addon_tasks WHERE opportunity_id=? AND run_id=?",
                            [identity, run.run_id],
                        ).fetchone():
                            raise ExchangeConflict(
                                "Эта возможность уже экспортирована. Bridge повторно получает существующее задание; дублирование закупки запрещено."
                            )
                        tasks.append(self.task(value, run, request, now))
                    allowed = {
                        line.item_id
                        for task in tasks
                        for line in task.lines
                        if line.source == "auction"
                    }
                    if set(request.unit_limits) - allowed:
                        raise ValueError(
                            "Limit specified for an item outside the AH procurement plan"
                        )
                    package = Package(
                        package_id=uuid4().hex,
                        market=run.market,
                        run_id=run.run_id,
                        generation=run.generation,
                        max_budget_copper=request.max_budget_copper
                        or sum(t.max_total_cost_copper for t in tasks),
                        created_at=now,
                        expires_at=tasks[0].expires_at,
                        tasks=tasks,
                    )
                    if len(package.model_dump_json().encode("utf-8")) > 4_000_000:
                        raise ValueError("Пакет превышает 4 MB: выберите меньше возможностей.")
                    db.execute(
                        "INSERT INTO addon_packages VALUES(?,?,?,?,?,?,?)",
                        [
                            package.package_id,
                            request.request_id,
                            digest,
                            package.market,
                            package.run_id,
                            package.expires_at.isoformat(),
                            package.model_dump_json(),
                        ],
                    )
                    db.executemany(
                        "INSERT INTO addon_tasks VALUES(?,?,?,?,?,?)",
                        [
                            (
                                t.task_id,
                                t.opportunity_id,
                                t.run_id,
                                t.market,
                                package.package_id,
                                t.model_dump_json(),
                            )
                            for t in tasks
                        ],
                    )
                    return package
        finally:
            self.analytics.lock.release()

    def packages(self, market: str, include_expired: bool = False) -> list[Package]:
        status = self.ready()
        if market != status["active_market"]:
            raise ExchangeConflict("Bridge market differs from the active market")
        with self.store.connection() as db:
            sql = "SELECT payload FROM addon_packages WHERE market=? AND run_id=?"
            params = [market, status["published_run"]["run_id"]]
            if not include_expired:
                sql += " AND expires_at>?"
                params.append(datetime.now(UTC).isoformat())
            rows = db.execute(sql + " ORDER BY id", params).fetchall()
        return [Package.model_validate_json(row[0]) for row in rows]

    def renew(self, identity: str) -> Package:
        if not self.analytics.lock.acquire(blocking=False):
            raise ExchangeConflict("Дождитесь завершения расчёта.")
        try:
            with exclusive_lock(self.analytics.directory):
                status = self.ready()
                with self.store.connection() as db:
                    row = db.execute(
                        "SELECT payload FROM addon_packages WHERE id=?", [identity]
                    ).fetchone()
                    if row is None:
                        raise ValueError("Unknown package")
                    package = Package.model_validate_json(row[0])
                    if (
                        package.market != status["active_market"]
                        or package.run_id != status["published_run"]["run_id"]
                    ):
                        raise ExchangeConflict(
                            "Пакет не относится к текущему опубликованному запуску."
                        )
                    package.expires_at = datetime.now(UTC) + timedelta(seconds=1800)
                    for task in package.tasks:
                        task.expires_at = package.expires_at
                        task.expires_at_epoch = epoch(package.expires_at)
                        db.execute(
                            "UPDATE addon_tasks SET payload=? WHERE id=?",
                            [task.model_dump_json(), task.task_id],
                        )
                    db.execute(
                        "UPDATE addon_packages SET expires_at=?,payload=? WHERE id=?",
                        [package.expires_at.isoformat(), package.model_dump_json(), identity],
                    )
                    return package
        finally:
            self.analytics.lock.release()

    def reports(self, batch: ReportBatch) -> list[str]:
        accepted = []
        with self.store.connection() as db:
            for event in batch.events:
                task = db.execute(
                    "SELECT payload FROM addon_tasks WHERE id=?", [event.task_id]
                ).fetchone()
                if task is None:
                    raise ValueError("Unknown task in report")
                expected = Task.model_validate_json(task[0])
                if (event.opportunity_id, event.run_id, event.market) != (
                    expected.opportunity_id,
                    expected.run_id,
                    expected.market,
                ):
                    raise ValueError("Report does not match task market/run/opportunity")
                payload = event.model_dump_json()
                old = db.execute(
                    "SELECT payload FROM addon_events WHERE id=?", [event.event_id]
                ).fetchone()
                if old and old[0] != payload:
                    raise ExchangeConflict("Event id reused with different content")
                if not old:
                    db.execute(
                        "INSERT INTO addon_events VALUES(?,?,?,?,?,?)",
                        [
                            event.event_id,
                            event.task_id,
                            event.market,
                            event.observed_at.isoformat(),
                            event.kind,
                            payload,
                        ],
                    )
                accepted.append(event.event_id)
        return accepted

    def history(self, limit: int = 100) -> list[dict]:
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT payload FROM addon_events WHERE market=? ORDER BY observed_at DESC,id LIMIT ?",
                [self.store.settings().active_market, limit],
            ).fetchall()
        return [json.loads(row[0]) for row in rows]
