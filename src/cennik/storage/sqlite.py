from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from cennik.core.models import AnalyticsSettings, Opportunity, Run


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(
                    run_id TEXT PRIMARY KEY,market TEXT NOT NULL,fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,started_at TEXT NOT NULL,payload TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_running ON runs(status) WHERE status='running';
                CREATE INDEX IF NOT EXISTS market_runs ON runs(market,started_at DESC);
                CREATE TABLE IF NOT EXISTS opportunities(
                    id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(run_id),market TEXT NOT NULL,
                    strategy TEXT NOT NULL,profit INTEGER NOT NULL,roi REAL NOT NULL,
                    rating REAL NOT NULL,age INTEGER NOT NULL,confidence TEXT NOT NULL,payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS run_opportunities ON opportunities(run_id,market);
                CREATE TABLE IF NOT EXISTS published(market TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(run_id));
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self) -> AnalyticsSettings:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM settings WHERE id=1").fetchone()
            return AnalyticsSettings.model_validate_json(row[0]) if row else AnalyticsSettings()

    def save_settings(self, value: AnalyticsSettings) -> None:
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES(1,?)", [value.model_dump_json()])

    def recover(self) -> None:
        with self.connection() as db:
            for row in db.execute("SELECT payload FROM runs WHERE status='running'").fetchall():
                run = Run.model_validate_json(row[0])
                run.status, run.error = "interrupted", "Расчёт прерван перезапуском; запустите вручную."
                run.finished_at = datetime.now(UTC)
                db.execute("UPDATE runs SET status=?,payload=? WHERE run_id=?",
                           [run.status, run.model_dump_json(), run.run_id])

    def begin(self, run: Run, identity: str) -> None:
        with self.connection() as db:
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?)", [run.run_id, run.market, identity,
                       run.status, run.started_at.isoformat(), run.model_dump_json()])

    def cached(self, identity: str) -> Run | None:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM runs WHERE fingerprint=? AND status='completed' ORDER BY started_at DESC LIMIT 1", [identity]).fetchone()
            return Run.model_validate_json(row[0]) if row else None

    def publish_cached(self, run: Run) -> None:
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO published VALUES(?,?)", [run.market, run.run_id])

    def finish(self, run: Run, opportunities: list[Opportunity]) -> None:
        # Rows, completed run and market pointer commit together. Failure never changes
        # the last successful pointer, and partially calculated signals stay invisible.
        with self.connection() as db:
            if run.status == "completed":
                db.executemany("INSERT INTO opportunities VALUES(?,?,?,?,?,?,?,?,?,?)", [
                    (o.id, o.run_id, o.market, o.strategy_id, o.potential_profit_copper,
                     str(o.roi_percent), str(o.rating), o.data_age_seconds, o.confidence, o.model_dump_json())
                    for o in opportunities])
                db.execute("INSERT OR REPLACE INTO published VALUES(?,?)", [run.market, run.run_id])
            db.execute("UPDATE runs SET status=?,payload=? WHERE run_id=?", [run.status, run.model_dump_json(), run.run_id])

    def latest(self, market: str | None, *, published: bool = False) -> Run | None:
        with self.connection() as db:
            if published:
                row = db.execute("SELECT r.payload FROM runs r JOIN published p ON p.run_id=r.run_id WHERE p.market=?", [market]).fetchone()
            else:
                row = db.execute("SELECT payload FROM runs WHERE market=? ORDER BY started_at DESC LIMIT 1", [market]).fetchone()
            return Run.model_validate_json(row[0]) if row else None

    def listing(self, market: str | None, *, strategy: str | None = None, min_profit: int = 0,
                min_roi: str = "0", confidence: str | None = None, sort: str = "rating",
                limit: int = 50, offset: int = 0) -> dict:
        orders = {"rating": "rating DESC", "profit": "profit DESC", "roi": "roi DESC", "freshness": "age ASC"}
        if sort not in orders:
            raise ValueError("Invalid sort")
        run = self.latest(market, published=True)
        if run is None:
            return {"items": [], "total": 0, "run_id": None, "market": market, "limit": limit, "offset": offset}
        clauses = ["run_id=?", "market=?", "profit>=?", "roi>=CAST(? AS REAL)"]
        params: list = [run.run_id, market, min_profit, min_roi]
        if strategy:
            clauses.append("strategy=?")
            params.append(strategy)
        if confidence:
            clauses.append("confidence=?")
            params.append(confidence)
        where = " AND ".join(clauses)
        with self.connection() as db:
            total = db.execute("SELECT count(*) FROM opportunities WHERE " + where, params).fetchone()[0]
            rows = db.execute("SELECT payload FROM opportunities WHERE " + where + " ORDER BY " + orders[sort] + ",id LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
        return {"items": [Opportunity.model_validate_json(row[0]).model_dump(mode="json") for row in rows],
                "total": total, "run_id": run.run_id, "market": market, "limit": limit, "offset": offset}

    def detail(self, market: str | None, identity: str) -> Opportunity | None:
        with self.connection() as db:
            row = db.execute("SELECT o.payload FROM opportunities o JOIN published p ON p.run_id=o.run_id WHERE o.id=? AND p.market=? AND o.market=?", [identity, market, market]).fetchone()
            return Opportunity.model_validate_json(row[0]) if row else None
