from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console

from .analytics import build_database, export_parquet
from .config import API, SITE, Settings
from .models import JobSpec
from .planner import bootstrap
from .planner import plan as create_plan
from .report import create_report
from .runner import Downloader
from .storage import State
from .util import atomic_json, dumps, exclusive_lock, utc_now
from .verify import verify as check_archive

app = typer.Typer(
    help="AHledger Historical Downloader. Prices from https://ahledger.com", no_args_is_help=True
)
console = Console()
DISCOVERY = ["markets", "economy", "catalog_index", "professions"]


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[Path | None, typer.Option(help="TOML конфигурация")] = Path("config.toml"),
    data_dir: Annotated[Path | None, typer.Option(help="Каталог архива")] = None,
    market: Annotated[
        list[str] | None,
        typer.Option("--market", help="ID рынка; можно повторить. По умолчанию все"),
    ] = None,
    dataset: Annotated[
        list[str] | None,
        typer.Option("--dataset", help="Тип данных; можно повторить. По умолчанию все"),
    ] = None,
    requests_per_second: Annotated[
        float | None, typer.Option(min=0.01, max=5, help="Глобальный лимит запросов/сек")
    ] = None,
    tools_per_second: Annotated[
        float | None, typer.Option(min=0.01, max=1, help="Лимит запросов/сек к market tools")
    ] = None,
):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        settings = Settings.read(config, data_dir)
        overrides = {
            "markets": market,
            "datasets": dataset,
            "requests_per_second": requests_per_second,
            "tools_per_second": tools_per_second,
        }
        ctx.obj = Settings.model_validate(
            settings.model_dump()
            | {key: value for key, value in overrides.items() if value is not None}
        )
    except (ValidationError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc


def open_state(ctx):
    return State(ctx.obj.data_dir)


def show(value):
    console.print_json(dumps(value))


@app.command()
def discover(
    ctx: typer.Context,
    max_seconds: Annotated[float, typer.Option(min=1)] = 45,
    dry_run: bool = False,
    refresh: bool = False,
):
    """Обнаружить рынки, классы и профессии (4 запроса, без массовой истории)."""
    settings: Settings = ctx.obj
    if dry_run:
        show({"requests": DISCOVERY, "network": False})
        return
    with exclusive_lock(settings.data_dir):
        state = open_state(ctx)
        try:
            bootstrap(state)
            state.refresh_discovery(
                DISCOVERY,
                None
                if refresh
                else (
                    datetime.now(UTC) - timedelta(hours=settings.discovery_ttl_hours)
                ).isoformat(),
            )
            asyncio.run(
                Downloader(settings, state).run(max_seconds=max_seconds, datasets=DISCOVERY)
            )
            registry(state)
            show(
                {
                    "markets": state.get("markets"),
                    "classes": state.get("catalog_classes"),
                    "professions": state.get("professions"),
                }
            )
        finally:
            state.close()


@app.command("plan")
def plan_command(ctx: typer.Context, dry_run: bool = False):
    """Создать очередь по уже обнаруженным источникам, без сети."""
    settings: Settings = ctx.obj
    with exclusive_lock(settings.data_dir):
        state = open_state(ctx)
        try:
            if dry_run:
                state.db.execute("BEGIN")
                # Do not mutate even SQLite on dry-run; estimate based on the saved catalog.
                items = state.db.execute("SELECT count(*) FROM item_candidates").fetchone()[0]
                show(
                    {
                        "network": False,
                        "items_discovered": items,
                        "history_jobs_upper_bound": items
                        * len(settings.selected_markets(state.get("active_markets", [])))
                        * 4,
                        "selected_markets": settings.markets or "all",
                        "requests_per_second": settings.requests_per_second,
                        "tools_per_second": settings.tools_per_second,
                        "existing_jobs": state.counts(markets=settings.markets),
                        "catalog_classes": len(state.get("catalog_classes", {})),
                    }
                )
                state.db.rollback()
            else:
                bootstrap(state)
                show(create_plan(settings, state))
                state.set("full_plan_created", True)
                state.set(
                    "plan_scope",
                    {
                        "full_history_check": settings.full_history_check,
                        "enrich_items": settings.enrich_items,
                        "markets": settings.markets or "all",
                        "datasets": settings.datasets or "all",
                    },
                )
        finally:
            state.close()


async def execute_download(
    settings: Settings, state: State, max_jobs, max_seconds, retry_failed, refresh
):
    bootstrap(state)
    state.recover(retry_failed)
    state.refresh_discovery(
        DISCOVERY, (datetime.now(UTC) - timedelta(hours=settings.discovery_ttl_hours)).isoformat()
    )
    if refresh:
        state.refresh()
    create_plan(settings, state)
    state.set("full_plan_created", True)
    state.set(
        "plan_scope",
        {
            "full_history_check": settings.full_history_check,
            "enrich_items": settings.enrich_items,
            "markets": settings.markets or "all",
            "datasets": settings.datasets or "all",
        },
    )
    return await Downloader(settings, state).run(max_jobs=max_jobs, max_seconds=max_seconds)


@app.command()
def download(
    ctx: typer.Context,
    max_jobs: Annotated[
        int | None, typer.Option(min=1, help="Ограничить число попыток, включая разведку")
    ] = None,
    max_seconds: Annotated[float | None, typer.Option(min=1)] = None,
    dry_run: bool = False,
    retry_failed: bool = False,
    refresh: bool = False,
    build: Annotated[
        bool, typer.Option("--build/--no-build", help="Построить DuckDB/Parquet после остановки")
    ] = True,
):
    """Выгрузить историю, сохранить прогресс. Ctrl+C безопасен; полный запуск ручной."""
    if dry_run:
        plan_command(ctx, True)
        return
    settings: Settings = ctx.obj
    with exclusive_lock(settings.data_dir):
        state = open_state(ctx)
        try:
            try:
                show(
                    asyncio.run(
                        execute_download(
                            settings, state, max_jobs, max_seconds, retry_failed, refresh
                        )
                    )
                )
            except KeyboardInterrupt:
                console.print("Прервано. Очередь сохранена; используйте resume.")
                state.recover()
            registry(state)
            if build:
                show(build_database(state, settings=settings))
                show(check_archive(state, settings=settings))
            create_report(state, settings=settings)
        finally:
            state.close()


@app.command()
def resume(
    ctx: typer.Context,
    max_jobs: Annotated[int | None, typer.Option(min=1)] = None,
    max_seconds: Annotated[float | None, typer.Option(min=1)] = None,
    retry_failed: bool = False,
    build: Annotated[bool, typer.Option("--build/--no-build")] = True,
):
    """Продолжить сохранённую очередь без повторения успешных заданий."""
    download(ctx, max_jobs, max_seconds, False, retry_failed, False, build)


@app.command()
def status(ctx: typer.Context):
    """Состояние очереди, ошибки, объём и кандидаты предметов."""
    state = open_state(ctx)
    try:
        show(
            {
                "jobs": state.counts(markets=ctx.obj.markets),
                "selected_markets": ctx.obj.markets or "all",
                "items": state.db.execute("SELECT count(*) FROM item_candidates").fetchone()[0],
                "markets": state.get("markets", []),
                "active_markets": state.get("active_markets", []),
                "data_dir": str(state.root.resolve()),
            }
        )
    finally:
        state.close()


@app.command("build-db")
def build_db(ctx: typer.Context, restore_state: bool = False):
    """Пересоздать DuckDB и Parquet из raw; --restore-state восстанавливает SQLite."""
    with exclusive_lock(ctx.obj.data_dir):
        state = open_state(ctx)
        try:
            show(build_database(state, restore_state=restore_state, settings=ctx.obj))
        finally:
            state.close()


@app.command("local")
def local(
    ctx: typer.Context,
    database: Annotated[Path | None, typer.Option(help="Импортировать готовую DuckDB без изменения исходного файла")] = None,
    port: Annotated[int, typer.Option(min=1024, max=65535)] = 8765,
    open_browser: bool = True,
):
    """Открыть локальное приложение без Docker, HTTPS и секретного ключа."""
    from .local import launch_local

    settings = ctx.obj.model_copy(update={"data_dir": ctx.obj.data_dir.resolve()})
    try:
        launch_local(settings, source=database, port=port, open_browser=open_browser)
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command("update-fetch", hidden=True)
def update_fetch(ctx: typer.Context, update_id: Annotated[str, typer.Option()]):
    from .update import batch_root, fetch_update

    with exclusive_lock(batch_root(ctx.obj.data_dir, update_id)):
        show(asyncio.run(fetch_update(ctx.obj, update_id)))


@app.command("update-merge", hidden=True)
def update_merge(ctx: typer.Context, update_id: Annotated[str, typer.Option()]):
    from .update import batch_root, merge_update

    with exclusive_lock(batch_root(ctx.obj.data_dir, update_id)):
        show(merge_update(ctx.obj, update_id))


@app.command()
def verify(ctx: typer.Context):
    """Проверить SHA-256, схемы, количества и ссылочную целостность без сети."""
    with exclusive_lock(ctx.obj.data_dir):
        state = open_state(ctx)
        try:
            result = check_archive(state, settings=ctx.obj)
            show(result)
            if result["errors"]:
                raise typer.Exit(1)
        finally:
            state.close()


@app.command()
def report(ctx: typer.Context):
    """Сформировать HTML и JSON отчёты по фактически скачанным данным."""
    state = open_state(ctx)
    try:
        result = create_report(state, settings=ctx.obj)
        console.print(str((state.root / "reports" / "download_report.html").resolve()))
        show({"status": result["download_status"], "counts": result["counts"]})
    finally:
        state.close()


@app.command("export")
def export(ctx: typer.Context):
    """Экспортировать таблицы DuckDB в новую атомарную генерацию Parquet."""
    with exclusive_lock(ctx.obj.data_dir):
        state = open_state(ctx)
        try:
            console.print(str(export_parquet(state, settings=ctx.obj).resolve()))
        finally:
            state.close()


@app.command("smoke-test")
def smoke_test(ctx: typer.Context, max_seconds: Annotated[float, typer.Option(min=1)] = 25):
    """ЯВНО СЕТЕВОЙ тест: 6 официальных запросов, отдельный архив smoke/."""
    settings = ctx.obj.model_copy(update={"data_dir": ctx.obj.data_dir / "smoke"})
    console.print("СЕТЕВОЙ SMOKE TEST: до 6 запросов к AHledger, без полной выгрузки.")
    with exclusive_lock(settings.data_dir):
        state = State(settings.data_dir)
        try:
            for spec in (
                JobSpec(dataset="markets", url=API + "/v1/markets"),
                JobSpec(
                    dataset="catalog", url=SITE + "/api/catalog", params={"class": "trade-goods"}
                ),
                JobSpec(
                    dataset="pricetable",
                    url=API + "/v1/pricetable/forever.pvp.horde.us",
                    market="forever.pvp.horde.us",
                ),
                JobSpec(
                    dataset="history",
                    url=API + "/v1/prices/forever.pvp.horde.us/2589",
                    market="forever.pvp.horde.us",
                    item_id=2589,
                    params={"range": "24h"},
                ),
                JobSpec(
                    dataset="history",
                    url=API + "/v1/prices/forever.rp.horde.us/2589",
                    market="forever.rp.horde.us",
                    item_id=2589,
                    params={"range": "90d"},
                ),
                JobSpec(dataset="economy", url=API + "/v1/economy"),
            ):
                state.enqueue(spec)
            state.db.commit()
            # Expansion is disabled for smoke, so discovered items cannot become an accidental bulk run.
            state.refresh()
            downloader = Downloader(settings, state)
            downloader.expand = False
            result = asyncio.run(downloader.run(max_jobs=6, max_seconds=max_seconds))
            show(result)
            if result.get("FAILED") or result.get("PENDING") or result.get("RETRY"):
                raise typer.Exit(1)
        finally:
            state.close()


def registry(state: State):
    rows = [
        dict(row)
        for row in state.db.execute(
            "SELECT dataset,market,status,count(*) jobs FROM jobs GROUP BY dataset,market,status"
        )
    ]
    atomic_json(
        state.root / "reports" / "source_registry.json",
        {
            "generated_at": utc_now(),
            "sources": rows,
            "documentation": [
                SITE + "/" + name for name in ("data", "developers", "terms", "changelog")
            ],
            "reference_client": "https://github.com/DeepCapt/ahledger-sync",
            "catalog_classes": state.get("catalog_classes", {}),
            "professions": state.get("professions", []),
            "documented_interfaces": {
                "markets": "/v1/markets",
                "catalog": "https://ahledger.com/api/catalog?class={class}",
                "items": "/v1/items/{itemId}?game=forever",
                "pricetable": "/v1/pricetable/{market}",
                "history": "/v1/prices/{market}/{itemId}?range={24h,7d,30d,90d}",
                "crafting": "/v1/crafting/{market}?profession={profession}&all=1",
                "disenchant": "/v1/disenchant/{market}?all=true",
                "structure": "/v1/structure/{market}",
                "signals": "/v1/signals/{market}?discount={10,20,30,40}&days={3,7,14}",
                "deals": "/v1/deals/{market}?discount=15&units=1&sells=0&limit=200&sort={saving,discount,sales}",
                "economy": "/v1/economy and /v1/economy/{market}",
                "builds": "https://ahledger.com/feed/builds.xml",
                "llms": "https://ahledger.com/llms-full.txt",
            },
            "documented_not_downloaded": {
                "/v1/tooltip/{market}?items={up_to_100_ids}": "Redundant with archived price tables/history/sales; NOT_CHECKED as a separate dataset",
                "/health": "Operational service status, not historical game data; NOT_CHECKED",
            },
            "raw_auction_scans": "unavailable",
            "contact": SITE + "/contact",
        },
    )
