"""Manual jobs for the web panel. No network activity at server startup."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import Settings
from .storage import TERMINAL, job_scope
from .update import batch_root
from .util import atomic_json, utc_now

Dataset = Literal[
    "history",
    "pricetable",
    "item",
    "crafting",
    "disenchant",
    "structure",
    "signals",
    "deals",
    "economy_market",
    "builds",
    "documentation",
    "llms",
]
MarketID = Annotated[str, Field(pattern=r"^forever\.[a-z0-9-]+\.(alliance|horde)\.(us|eu)$")]
DEFAULT_MARKETS = [
    {
        "id": f"forever.{ruleset}.{faction}.us",
        "label": f"{ruleset.upper()} · {faction.title()} · US",
    }
    for ruleset in ("normal", "pvp")
    for faction in ("alliance", "horde")
]


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["download", "discover", "build", "verify", "export", "update"] = "download"
    markets: list[MarketID] = Field(default_factory=lambda: ["forever.pvp.horde.us"], max_length=12)
    datasets: list[Dataset] = Field(default_factory=list, max_length=12)
    requests_per_second: float = Field(default=1, ge=0.01, le=5)
    tools_per_second: float = Field(default=0.5, ge=0.01, le=1)
    retry_failed: bool = False

    @model_validator(mode="after")
    def one_update_market(self):
        if self.action == "update" and len(self.markets) != 1:
            raise ValueError("Для обновления выберите ровно один рынок.")
        return self


def read_state(root: Path, settings: Settings | None = None) -> dict:
    value: dict = {
        "jobs": {},
        "items": 0,
        "markets": DEFAULT_MARKETS,
        "active_markets": [],
        "records": 0,
        "raw_bytes": 0,
        "current": [],
        "rate_multiplier": 1,
    }
    path = root / "state.sqlite"
    if not path.exists():
        return value
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            kv = {
                row[0]: json.loads(row[1]) for row in connection.execute("SELECT key,value FROM kv")
            }
            scope, params = job_scope(
                settings.download_datasets() if settings else None,
                settings.markets if settings else None,
            )
            value.update(
                jobs=dict(
                    connection.execute(
                        f"SELECT status,count(*) FROM jobs WHERE {scope} GROUP BY status", params
                    )
                ),
                items=connection.execute("SELECT count(*) FROM item_candidates").fetchone()[0],
                markets=kv.get("markets") or DEFAULT_MARKETS,
                active_markets=kv.get("active_markets", []),
                records=kv.get("records_saved", 0),
                raw_bytes=kv.get("compressed_bytes", 0),
                rate_multiplier=kv.get("rate_multiplier", 1),
                current=[
                    dict(row)
                    for row in connection.execute(
                        f"SELECT dataset,market,item_id FROM jobs WHERE status='RUNNING' AND {scope} LIMIT 8",
                        params,
                    )
                ],
            )
    finally:
        connection.close()
    return value


class Controller:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.data_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.process: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.cancelled = threading.Event()
        self.run_settings = settings
        self.monotonic_start: float | None = None
        self.baseline = 0
        self.info_path = self.root / "web-run.json"
        self.info: dict = {"state": "idle", "action": None}
        if self.info_path.exists():
            self.info = json.loads(self.info_path.read_text("utf-8"))
            if self.info.get("state") in {"running", "building", "stopping"}:
                self.info.update(state="interrupted", finished_at=utc_now())
                atomic_json(self.info_path, self.info)
            if self.info.get("request"):
                previous = JobRequest.model_validate(self.info["request"])
                self.run_settings = self.job_settings(previous)

    def job_settings(self, request: JobRequest) -> Settings:
        return Settings.model_validate(
            self.settings.model_dump()
            | {
                "data_dir": self.root,
                "markets": request.markets,
                "datasets": request.datasets,
                "requests_per_second": request.requests_per_second,
                "tools_per_second": request.tools_per_second,
            }
        )

    def progress_root(self) -> Path:
        if self.info.get("action") == "update" and self.info.get("update_id"):
            return batch_root(self.root, self.info["update_id"])
        return self.root

    def updating(self) -> bool:
        with self.lock:
            return self.info.get("action") == "update" and self.info.get("state") in {"running", "building", "stopping"}

    def save(self, **values):
        with self.lock:
            self.info.update(values)
            atomic_json(self.info_path, self.info)

    def snapshot(self) -> dict:
        with self.lock:
            info = dict(self.info)
            settings = self.run_settings
            started, baseline = self.monotonic_start, self.baseline
        state = read_state(self.progress_root(), settings)
        done = sum(state["jobs"].get(status, 0) for status in TERMINAL)
        remaining = sum(state["jobs"].get(status, 0) for status in ("PENDING", "RUNNING", "RETRY"))
        rate = max(0, done - baseline) / max(1, time.monotonic() - started) if started else 0
        state.update(
            run=info,
            done=done,
            remaining=remaining,
            total=sum(state["jobs"].values()),
            jobs_per_second=round(rate, 3),
            eta_seconds=round(remaining / rate)
            if rate and info.get("state") == "running"
            else None,
            analytics_ready=(self.root / "analytics/ahledger.duckdb").exists(),
            attribution="Prices from https://ahledger.com",
        )
        build = self.root / "analytics/build_result.json"
        if build.exists():
            state["analytics_updated_at"] = json.loads(build.read_text("utf-8")).get("generated_at")
        return state

    def start(self, request: JobRequest) -> dict:
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise RuntimeError("Уже выполняется задание; сначала остановите его")
            saved = read_state(self.root)
            known = {market["id"] for market in saved["markets"]}
            unknown = set(request.markets) - known
            if unknown and saved["active_markets"]:
                raise ValueError("Неизвестные рынки: " + ", ".join(sorted(unknown)))
            self.run_settings = self.job_settings(request)
            previous = dict(self.info)
            update_id = None
            if request.action == "update":
                resume = previous.get("state") in {"stopped", "failed", "interrupted", "partial"} and previous.get("request", {}).get("markets") == request.markets
                update_id = previous.get("update_id") if resume else None
                update_id = update_id or uuid.uuid4().hex
            self.cancelled.clear()
            self.monotonic_start = time.monotonic()
            scoped = read_state(batch_root(self.root, update_id) if update_id else self.root, self.run_settings)
            self.baseline = sum(scoped["jobs"].get(status, 0) for status in TERMINAL)
            self.info = {
                "state": "running",
                "action": request.action,
                "started_at": utc_now(),
                "request": request.model_dump(),
                "error": None,
                "update_id": update_id,
            }
            self.save()
            self.thread = threading.Thread(target=self.execute, args=(request,), daemon=True)
            self.thread.start()
            return dict(self.info)

    def commands(self, request: JobRequest) -> list[list[str]]:
        config = self.root / "web-config.toml"
        values = self.run_settings.model_dump(mode="json")
        config.write_text(
            "\n".join(f"{key} = {json.dumps(value)}" for key, value in values.items()) + "\n",
            "utf-8",
        )
        base = [
            sys.executable,
            "-m",
            "ahledger",
            "--config",
            str(config),
            "--data-dir",
            str(self.root),
        ]
        if request.action == "discover":
            return [[*base, "discover", "--refresh", "--max-seconds", "45"]]
        if request.action == "build":
            return [[*base, "build-db"], [*base, "verify"], [*base, "report"]]
        if request.action == "verify":
            return [[*base, "verify"]]
        if request.action == "export":
            return [[*base, "export"]]
        if request.action == "update":
            return [[*base, "update-fetch", "--update-id", self.info["update_id"]],
                    [*base, "update-merge", "--update-id", self.info["update_id"]]]
        return [
            [
                *base,
                "download",
                "--no-build",
                *(["--retry-failed"] if request.retry_failed else []),
            ],
            [*base, "build-db"],
            [*base, "verify"],
            [*base, "report"],
        ]

    def execute(self, request: JobRequest):
        log_path = self.root / "web.log"
        try:
            if log_path.exists() and log_path.stat().st_size > 5 * 1024**2:
                log_path.replace(self.root / "web.log.1")
            commands = self.commands(request)
            with log_path.open("ab", buffering=0) as log:
                for command in commands:
                    with self.lock:
                        if self.cancelled.is_set():
                            break
                        self.save(
                            state="building" if "build-db" in command or "update-merge" in command else "running",
                            command=command[-1],
                        )
                        environment = os.environ.copy()
                        environment.pop("AHL_WEB_API_KEY", None)
                        environment.update(
                            PYTHONUNBUFFERED="1",
                            PYTHONIOENCODING="utf-8",
                            NO_COLOR="1",
                            TERM="dumb",
                        )
                        self.process = subprocess.Popen(
                            command,
                            stdin=subprocess.DEVNULL,
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            env=environment,
                            start_new_session=os.name != "nt",
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                        )
                    exit_code = self.process.wait()
                    if self.cancelled.is_set():
                        break
                    if exit_code != 0:
                        raise RuntimeError(
                            f"Команда завершилась с кодом {exit_code}; подробности в журнале"
                        )
            snapshot = read_state(self.progress_root(), self.run_settings)
            unfinished = any(
                snapshot["jobs"].get(status, 0)
                for status in ("PENDING", "RETRY", "RUNNING", "FAILED")
            )
            outcome = (
                "stopped"
                if self.cancelled.is_set()
                else ("partial" if request.action in {"download", "update"} and unfinished else "completed")
            )
            self.save(state=outcome, finished_at=utc_now())
        except Exception as exc:
            self.save(
                state="stopped" if self.cancelled.is_set() else "failed",
                error=str(exc)[:1000],
                finished_at=utc_now(),
            )
        finally:
            with self.lock:
                self.process = None

    def stop(self) -> dict:
        with self.lock:
            if not self.thread or not self.thread.is_alive():
                return dict(self.info)
            self.cancelled.set()
            self.save(state="stopping")
            if self.process and self.process.poll() is None:
                try:
                    if os.name == "nt":
                        # The venv launcher may own a child python.exe. Stop the
                        # entire owned job tree, so downloads cannot outlive Stop.
                        subprocess.run(
                            ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW, check=False,
                        )
                    else:
                        os.killpg(self.process.pid, signal.SIGINT)  # type: ignore[attr-defined]
                except ProcessLookupError:
                    pass
            return dict(self.info)

    def shutdown(self):
        self.stop()
        if self.thread:
            self.thread.join(timeout=15)
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.kill()

    def logs(self) -> str:
        path = self.root / "web.log"
        if not path.exists():
            return "Журнал пока пуст."
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 32000))
            return stream.read().decode("utf-8", errors="replace")
