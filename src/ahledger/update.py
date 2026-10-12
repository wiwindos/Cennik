"""Manually fetch one market and merge a verified batch into the existing snapshot."""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

import httpx

from .analytics import analytics_connection, build_database
from .config import API, Settings
from .http import RateLimiter
from .models import JobSpec
from .parsers import parse
from .runner import Downloader
from .storage import RawArchive, State, job_key
from .util import atomic_json, exclusive_lock, utc_now
from .verify import verify


def batch_root(root: Path, identity: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", identity):
        raise ValueError("Invalid update ID")
    return root / "updates" / identity


def selected_market(settings: Settings) -> str:
    if len(settings.markets) != 1:
        raise ValueError("Выберите ровно один рынок для обновления.")
    return settings.markets[0]


async def fetch_update(
    settings: Settings, identity: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict:
    market = selected_market(settings)
    base = settings.data_dir / "analytics/ahledger.duckdb"
    with analytics_connection(base, settings, read_only=True) as db:
        row = db.execute(
            "SELECT market,label,market_status FROM markets WHERE market=?", [market]
        ).fetchone()
    if row is None:
        raise ValueError("Рынок отсутствует в текущей базе.")
    root = batch_root(settings.data_dir, identity)
    batch_settings = settings.model_copy(
        update={
            "data_dir": root,
            "datasets": [],
            "full_history_check": False,
            "enrich_items": "none",
        }
    )
    state = State(root)
    state.set("markets", [{"id": row[0], "label": row[1]}])
    state.set("target_market", market)
    state.recover(retry_failed=True)
    discovery = JobSpec(
        dataset="professions",
        url=f"{API}/v1/crafting/{market}",
        market=market,
        params={"profession": "invalid", "all": "1"},
        priority=-30,
    )
    for spec in (
        discovery,
        JobSpec(
            dataset="pricetable", url=f"{API}/v1/pricetable/{market}", market=market, priority=-20
        ),
        JobSpec(dataset="economy_market", url=f"{API}/v1/economy/{market}", market=market),
    ):
        state.enqueue(spec)
    state.db.commit()
    limiter: RateLimiter | None = None

    async def download():
        nonlocal limiter
        runner = Downloader(batch_settings, state, transport=transport)
        if limiter is None:
            limiter = runner.client.limiter
        else:
            runner.client.limiter = limiter
        runner.expand = False  # Never turn a manual snapshot update into bulk history.
        await runner.run(quiet=True)
        if state.counts().get("FAILED"):
            raise ValueError("Есть ошибки загрузки; текущая база сохранена. Повторите обновление.")

    try:
        await download()
        archive = RawArchive(state)
        response = state.db.execute(
            "SELECT raw_id,status FROM jobs WHERE id=?", [job_key(discovery)]
        ).fetchone()
        if response["status"] not in {"COMPLETE", "PARTIAL"}:
            raise ValueError("Не получен список профессий; текущая база сохранена.")
        professions = parse(discovery, archive.read(archive.metadata(response["raw_id"]))).data[
            "professions"
        ]
        for profession in professions:
            state.enqueue(
                JobSpec(
                    dataset="crafting",
                    url=f"{API}/v1/crafting/{market}",
                    market=market,
                    params={"profession": profession, "all": "1"},
                )
            )
        for dataset, params in (
            ("disenchant", {"all": "true"}),
            ("structure", {}),
            *(
                (
                    "deals",
                    {"discount": "15", "units": "1", "sells": "0", "sort": sort, "limit": "200"},
                )
                for sort in ("saving", "discount", "sales")
            ),
            *(
                ("signals", {"discount": str(discount), "days": str(days)})
                for discount in (10, 20, 30, 40)
                for days in (3, 7, 14)
            ),
        ):
            state.enqueue(
                JobSpec(
                    dataset=dataset,
                    url=f"{API}/v1/{dataset}/{market}",
                    market=market,
                    params=params,
                )
            )
        state.db.commit()
        await download()
        prices = state.db.execute("SELECT status FROM jobs WHERE dataset='pricetable'").fetchone()
        if prices[0] != "COMPLETE":
            raise ValueError("Свежая таблица цен не получена; текущая база сохранена.")
        for meta in archive.manifests(ordered=False):
            if meta["spec"]["dataset"] == "economy_market" and meta["validation"] in {
                "COMPLETE",
                "PARTIAL",
            }:
                data = parse(JobSpec.model_validate(meta["spec"]), archive.read(meta)).data
                if data.get("marketStatus"):
                    state.set("market_status", {market: data["marketStatus"]})
        return {
            "update_id": identity,
            "market": market,
            "jobs": state.counts(),
            "fetched_at": utc_now(),
        }
    finally:
        state.close()


def merge_update(settings: Settings, identity: str) -> dict:
    market = selected_market(settings)
    root = batch_root(settings.data_dir, identity)
    state = State(root)
    destination = settings.data_dir / "analytics/ahledger.duckdb"
    try:
        if state.get("target_market") != market:
            raise ValueError("Пакет обновления относится к другому рынку.")
        if any(state.counts().get(key) for key in ("PENDING", "RETRY", "RUNNING", "FAILED")):
            raise ValueError("Загрузка не завершена; текущая база сохранена.")
        checked = verify(state, settings=settings)
        if checked["integrity"] != "COMPLETE":
            raise ValueError("Повреждён пакет обновления; текущая база сохранена.")
        base_stat = destination.stat()
        result = build_database(
            state, settings=settings, base_database=destination, allowed_market=market
        )
        checked = verify(state, settings=settings)
        if checked["integrity"] != "COMPLETE":
            raise ValueError("Проверка новой базы не пройдена; текущая база сохранена.")
        if (destination.stat().st_size, destination.stat().st_mtime_ns) != (
            base_stat.st_size,
            base_stat.st_mtime_ns,
        ):
            raise ValueError("База изменилась во время обновления. Повторите объединение.")
        staged = root / "analytics/ahledger.duckdb"
        # Export already succeeded. Move its generation on the same archive volume.
        pointer = json.loads((root / "parquet/CURRENT.json").read_text("utf-8"))
        generation = pointer["directory"]
        if not re.fullmatch(r"[0-9a-f]{32}", generation):
            raise ValueError("Invalid Parquet generation")
        parquet = (settings.data_dir / "parquet" / generation).resolve()
        staged_parquet = (root / "parquet" / generation).resolve()
        archive_root = settings.data_dir.resolve()
        if not parquet.is_relative_to(archive_root) or not staged_parquet.is_relative_to(
            archive_root
        ):
            raise ValueError("Parquet move would leave the archive directory")
        parquet.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged_parquet), str(parquet))
        with exclusive_lock(settings.data_dir):
            if (destination.stat().st_size, destination.stat().st_mtime_ns) != (
                base_stat.st_size,
                base_stat.st_mtime_ns,
            ):
                raise ValueError("База изменилась до публикации обновления.")
            for attempt in range(50):
                try:
                    os.replace(staged, destination)
                    break
                except PermissionError:
                    if attempt == 49:
                        raise
                    time.sleep(0.1)
            atomic_json(settings.data_dir / "parquet/CURRENT.json", pointer)
            result.update(update_id=identity, updated_market=market, updated_at=utc_now())
            atomic_json(settings.data_dir / "analytics/build_result.json", result)
        atomic_json(root / "result.json", result)
        return result
    finally:
        state.close()
