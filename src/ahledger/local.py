"""Loopback-only desktop launch and non-destructive import of a saved DuckDB."""

from __future__ import annotations

import json
import os
import shutil
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from pathlib import Path

import uvicorn

from .analytics import analytics_connection
from .config import Settings
from .util import atomic_json, exclusive_lock, utc_now
from .web import create_app

REQUIRED_TABLES = {
    "markets",
    "items",
    "price_snapshots",
    "price_history",
    "recipes",
    "recipe_inputs",
    "dataset_metadata",
}


def import_database(source: Path, settings: Settings) -> dict:
    source = source.resolve(strict=True)
    if not source.is_file():
        raise ValueError("Укажите файл DuckDB.")
    if Path(str(source) + ".wal").exists():
        raise ValueError(
            "Рядом с базой есть WAL: сначала закройте исходное приложение и получите готовый снимок."
        )
    with analytics_connection(source, settings, read_only=True) as db:
        tables = {row[0] for row in db.execute("SHOW TABLES").fetchall()}
        missing = REQUIRED_TABLES - tables
        if missing:
            raise ValueError("В базе отсутствуют таблицы AHledger: " + ", ".join(sorted(missing)))
        # Check the columns used immediately by the UI before changing the active DB.
        db.execute("SELECT market,label,market_status FROM markets LIMIT 0")
        db.execute("SELECT item_id,name,payload_json FROM items LIMIT 0")
        db.execute("SELECT market,item_id,observed_at,granularity FROM price_history LIMIT 0")
    destination = settings.data_dir / "analytics/ahledger.duckdb"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if Path(str(destination) + ".wal").exists():
        raise ValueError("У текущей базы есть WAL. Закройте работающий загрузчик перед импортом.")
    if destination.exists() and os.path.samefile(source, destination):
        return {"source": str(source), "database": str(destination.resolve()), "method": "existing"}
    temporary = destination.parent / (".import-" + uuid.uuid4().hex + ".duckdb")
    method = "hardlink"
    try:
        try:
            os.link(source, temporary)
        except OSError:
            method = "copy"
            shutil.copyfile(source, temporary)
        # Keep the previous local DB available without mixing it into the imported
        # snapshot. All normal application reads still use the standard destination.
        previous = destination.parent / "ahledger.previous.duckdb"
        if destination.exists() and not previous.exists():
            os.link(destination, previous)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    result = {
        "source": str(source),
        "database": str(destination.resolve()),
        "bytes": source.stat().st_size,
        "method": method,
        "imported_at": utc_now(),
    }
    atomic_json(destination.parent / "imported.json", result)
    return result


def prepare_local(settings: Settings, source: Path | None = None) -> dict:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    destination = settings.data_dir / "analytics/ahledger.duckdb"
    with exclusive_lock(settings.data_dir):
        if source is not None:
            return import_database(source, settings)
        if destination.is_file():
            return {"database": str(destination.resolve()), "method": "existing"}
        return {"database": None, "method": "empty"}


def launch_local(settings: Settings, *, source: Path | None, port: int, open_browser: bool) -> None:
    url = f"http://127.0.0.1:{port}"
    with socket.socket() as probe:
        running = probe.connect_ex(("127.0.0.1", port)) == 0
    if running:
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=2) as response:
                value = json.load(response)
        except (OSError, ValueError) as exc:
            raise ValueError(f"Порт {port} уже занят. Выберите другой --port.") from exc
        if not value.get("local_mode") or value.get("workspace") != str(
            settings.data_dir.resolve()
        ):
            raise ValueError(f"Порт {port} занят другим приложением. Выберите другой --port.")
        if source is not None:
            raise ValueError(
                "Локальное приложение уже работает. Остановите его перед импортом другой базы."
            )
        if open_browser:
            webbrowser.open(url)
        print(f"Локальное приложение уже запущено: {url}", flush=True)
        return
    imported = prepare_local(settings, source)
    print(
        f"Cennik: {url}\nРабочая папка: {settings.data_dir.resolve()}\nБаза: {imported['database']}",
        flush=True,
    )
    app = create_app(settings, local=True)

    def show_browser():
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url + "/healthz", timeout=1):
                    webbrowser.open(url)
                    return
            except urllib.error.URLError:
                time.sleep(0.2)

    if open_browser:
        threading.Thread(target=show_browser, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=port, proxy_headers=False, log_level="info")
