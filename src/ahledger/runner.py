from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from rich.console import Console
from rich.live import Live
from rich.table import Table

from .config import Settings
from .http import Client, Deferred
from .models import JobSpec
from .parsers import parse
from .planner import absorb
from .storage import TERMINAL, RawArchive, State
from .util import dumps, utc_now


class Downloader:
    def __init__(
        self,
        settings: Settings,
        state: State,
        *,
        console: Console | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings, self.state = settings, state
        self.archive = RawArchive(state)
        self.client = Client(settings, state, self.archive, transport)
        self.console = console or Console()
        self.claimed = 0
        self.completed = 0
        self.current = ""
        self.expand = True
        self.datasets: list[str] | None = None
        self.started = time.monotonic()
        self.state.set(
            "compressed_bytes",
            sum(p.stat().st_size for p in (self.archive.root / "blobs").glob("*/*.zst")),
        )
        self.state.set(
            "records_saved",
            self.state.db.execute(
                "SELECT coalesce(sum(records),0) FROM jobs WHERE status IN ('COMPLETE','PARTIAL','NO_DATA')"
            ).fetchone()[0],
        )
        self.log_path = state.root / "logs" / "jobs.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, **data):
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(dumps({"at": utc_now(), "event": event, **data}) + "\n")

    def progress(self) -> Table:
        counts = self.state.counts(datasets=self.datasets, markets=self.settings.markets)
        table = Table(title="AHledger Historical Downloader · Prices from ahledger.com")
        table.add_column("Показатель")
        table.add_column("Значение")
        done = sum(counts.get(k, 0) for k in TERMINAL)
        remaining = sum(counts.get(k, 0) for k in ("PENDING", "RETRY", "RUNNING"))
        seconds = time.monotonic() - self.started
        rate = self.completed / seconds if seconds and self.completed else 0
        values: dict[str, Any] = {
            "Рынки": ", ".join(self.settings.markets) or "все",
            "Сохранено / осталось": f"{done:,} / {remaining:,}",
            "Ошибки / повтор": f"{counts.get('FAILED', 0)} / {counts.get('RETRY', 0)}",
            "HTTP / из кеша": f"{self.client.requests} / {self.client.hits}",
            "Лимит req/s": f"{self.client.limiter.rate:.2f}",
            "Скачано": f"{self.client.bytes / 1024**2:.2f} MiB",
            "Raw на диске": f"{self.state.get('compressed_bytes', 0) / 1024**2:.2f} MiB",
            "Записей сохранено": str(self.state.get("records_saved", 0)),
            "ETA": f"~{remaining / rate / 3600:.1f} ч (очередь может расти)"
            if rate
            else "пока неизвестно",
            "Сейчас": self.current,
        }
        for key, value in values.items():
            table.add_row(key, str(value))
        return table

    async def process(self, row):
        spec = JobSpec.model_validate_json(row["spec"])
        self.current = f"{spec.market or ''} {spec.dataset} {spec.item_id or ''}"
        self.log("started", job_id=row["id"], dataset=spec.dataset, market=spec.market)
        meta = None
        try:
            body, meta, cached = await self.client.fetch(spec, row["attempts"] + 1)
            status = meta["http_status"]
            permitted = status in {200, 304} or spec.dataset == "professions" and status == 400
            if not permitted:
                outcome = "NOT_AVAILABLE" if status in {404, 410} else "FAILED"
                meta.update(validation=outcome, error=f"HTTP {status}")
                self.archive.write_metadata(meta)
                self.state.finish(row["id"], outcome, meta["id"], error=f"HTTP {status}")
                self.log("finished", job_id=row["id"], status=outcome, http_status=status)
                return
            parsed = parse(spec, body)
            signature_key = "schema:" + spec.dataset
            previous = self.state.get(signature_key)
            if parsed.schema and previous and dumps(previous) != dumps(parsed.schema):
                parsed.warnings.append(
                    "Schema shape differs from earlier response; unknown fields preserved"
                )
            if parsed.schema:
                self.state.set(signature_key, parsed.schema)
            if not cached:
                meta.update(
                    validation=parsed.status,
                    records=parsed.count,
                    observed_at=parsed.minimum,
                    latest_observed_at=parsed.maximum,
                    granularity=parsed.granularity,
                    interval_seconds=parsed.interval_seconds,
                    warnings=parsed.warnings,
                    gaps=parsed.gaps,
                    pricesAsOf=parsed.data.get("pricesAsOf")
                    if isinstance(parsed.data, dict)
                    else None,
                    generatedAt=parsed.data.get("generatedAt")
                    if isinstance(parsed.data, dict)
                    else None,
                    schema=parsed.schema,
                )
                self.archive.write_metadata(meta)
                self.client.validated_cache(spec, meta)
            if self.expand:
                absorb(self.settings, self.state, spec, parsed)
            self.state.finish(row["id"], parsed.status, meta["id"], parsed.count)
            self.state.set("records_saved", self.state.get("records_saved", 0) + parsed.count)
            self.log(
                "finished",
                job_id=row["id"],
                status=parsed.status,
                raw_id=meta["id"],
                records=parsed.count,
                cached=cached,
                warnings=parsed.warnings,
            )
        except Deferred as exc:
            attempts = row["attempts"] + 1
            status = "FAILED" if attempts >= self.settings.max_attempts else "RETRY"
            self.state.finish(
                row["id"], status, exc.raw_id, error=str(exc), not_before=time.time() + exc.delay
            )
            self.log("retry", job_id=row["id"], status=status, error=str(exc), delay=exc.delay)
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            message = str(exc)[:2000]
            if meta:
                meta.update(validation="FAILED", error=message)
                self.archive.write_metadata(meta)
            self.state.finish(row["id"], "FAILED", meta["id"] if meta else None, error=message)
            self.log("failed", job_id=row["id"], error=message)
        self.completed += 1

    async def run(
        self,
        *,
        max_jobs: int | None = None,
        max_seconds: float | None = None,
        datasets: list[str] | None = None,
        quiet: bool = False,
    ):
        datasets = datasets if datasets is not None else self.settings.download_datasets()
        self.datasets = datasets
        self.state.recover()
        self.started = time.monotonic()

        async def worker():
            while max_jobs is None or self.claimed < max_jobs:
                row = self.state.claim(datasets, self.settings.markets)
                if row is None:
                    counts = self.state.counts(datasets=datasets, markets=self.settings.markets)
                    pending = sum(
                        counts.get(status, 0) for status in ("PENDING", "RETRY", "RUNNING")
                    )
                    if pending == 0:
                        break
                    await asyncio.sleep(0.25)
                    continue
                self.claimed += 1
                await self.process(row)

        async def task_group():
            async with asyncio.TaskGroup() as group:
                for _ in range(self.settings.concurrency):
                    group.create_task(worker())

        async def execute():
            if max_seconds is None:
                await task_group()
            else:
                async with asyncio.timeout(max_seconds):
                    await task_group()

        try:
            if quiet:
                await execute()
            else:
                with Live(self.progress(), console=self.console, refresh_per_second=2) as live:
                    task = asyncio.create_task(execute())
                    try:
                        while not task.done():
                            live.update(self.progress())
                            await asyncio.sleep(0.5)
                        await task
                    finally:
                        if not task.done():
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                        live.update(self.progress())
        except TimeoutError:
            self.log("time_budget_reached", max_seconds=max_seconds)
        finally:
            self.state.recover()
            await self.client.close()
        return self.state.counts(datasets=datasets, markets=self.settings.markets)
