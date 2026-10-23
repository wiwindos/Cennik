"""Manual Windows synchronization: HTTP packages -> Lua, SavedVariables -> reports."""

import argparse
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from ahledger.util import exclusive_lock
from cennik.addon.lua_data import lua_literal, read_saved_variables
from cennik.addon.models import Package, ReportBatch

if TYPE_CHECKING:
    from cennik.addon.service import AddonExchange


def atomic_write(path: Path, text: str):
    staging = path.with_name(path.name + ".cennik-tmp")
    staging.write_text(text, encoding="utf-8")
    os.replace(staging, path)


class Bridge:
    def __init__(
        self,
        server: str,
        addon_dir: Path,
        saved_variables: Path,
        market: str,
        state_path: Path,
        api_key: str = "",
        transport=None,
        exchange: "AddonExchange | None" = None,
    ):
        url = urlparse(server)
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("Invalid server URL")
        loopback = url.hostname in ("127.0.0.1", "localhost", "::1")
        if url.scheme not in ("http", "https") or (
            not loopback and (url.scheme != "https" or not api_key)
        ):
            raise ValueError("Remote Bridge requires HTTPS and CENNIK_API_KEY")
        if not (addon_dir / "CennikAddon.toc").is_file():
            raise ValueError("Choose the installed CennikAddon directory with CennikAddon.toc")
        self.addon_dir, self.saved_variables, self.market, self.state_path = (
            addon_dir,
            saved_variables,
            market,
            state_path,
        )
        self.state = (
            json.loads(state_path.read_text(encoding="utf-8"))
            if state_path.exists()
            else {"device_id": uuid4().hex, "acknowledged": []}
        )
        self.exchange = exchange
        self.extra_import: dict = {}
        self.report_handler: Callable[[ReportBatch], list[str]] | None = None
        self.client = None if exchange is not None else httpx.Client(
            base_url=server.rstrip("/"),
            timeout=20,
            follow_redirects=False,
            trust_env=False,
            headers={"X-API-Key": api_key} if api_key else {},
            transport=transport,
        )
        if not api_key and self.client is not None:
            health = self.client.get("/healthz")
            health.raise_for_status()
            if not health.json().get("local_mode"):
                raise ValueError("Server requires CENNIK_API_KEY")

    def synchronize(self):
        with exclusive_lock(self.state_path.parent):
            if self.state_path.exists():
                self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            return self._synchronize()

    def _synchronize(self):
        acknowledged = set(self.state["acknowledged"])
        # Read a stable saved snapshot. Never write a file owned by the game client.
        if self.saved_variables.is_file():
            before = self.saved_variables.stat()
            text = self.saved_variables.read_text(encoding="utf-8-sig")
            after = self.saved_variables.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("SavedVariables is being written; retry after /reload")
            data = read_saved_variables(text, max_bytes=64_000_000 if self.extra_import else 16_000_000)
            events = data.get("events") or []
            if not isinstance(events, list):
                raise ValueError("Invalid SavedVariables event list")
            pending = [e for e in events if e.get("event_id") not in acknowledged]
            for offset in range(0, len(pending), 500):
                batch = ReportBatch(events=pending[offset : offset + 500])
                if self.report_handler is not None:
                    accepted = self.report_handler(batch)
                elif self.exchange is not None:
                    accepted = self.exchange.reports(batch)
                else:
                    assert self.client is not None
                    response = self.client.post(
                        "/api/addon/reports", json=batch.model_dump(mode="json")
                    )
                    response.raise_for_status()
                    accepted = response.json()["accepted_event_ids"]
                if set(accepted) != {e.event_id for e in batch.events}:
                    raise ValueError("Server did not acknowledge the complete report batch")
                acknowledged.update(accepted)
                self.state["acknowledged"] = sorted(acknowledged)
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(self.state_path, json.dumps(self.state, ensure_ascii=False, indent=2))
        if self.exchange is not None:
            content: dict = {"format_version": 1, "packages": [
                p.model_dump(mode="json") for p in self.exchange.packages(self.market)
            ]}
        else:
            assert self.client is not None
            response = self.client.get("/api/addon/packages", params={"market": self.market})
            response.raise_for_status()
            content = response.json()
        if content.get("format_version") != 1:
            raise ValueError("Unsupported exchange format")
        packages = [Package.model_validate(p).model_dump(mode="json") for p in content["packages"]]
        if any(p["market"] != self.market for p in packages):
            raise ValueError("Unexpected package market")
        imported = {
            "format_version": 1,
            "device_id": self.state["device_id"],
            "packages": packages,
            "acknowledged_event_ids": sorted(acknowledged),
        }
        imported.update({k: v for k, v in self.extra_import.items()
                         if k in {"market_collector", "acknowledged_scan_ids", "sniper_book", "sniper_catalog", "acknowledged_sniper_event_ids"}})
        text = (
            "-- Written by Cennik Bridge; loaded on /reload.\nCennikImport = "
            + lua_literal(imported)
            + "\n"
        )
        if len(text.encode("utf-8")) > 8_000_000:
            raise ValueError("Lua import exceeds 8 MB; reduce the exported selection")
        atomic_write(
            self.addon_dir / "ImportedData.lua",
            text,
        )
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.state_path, json.dumps(self.state, ensure_ascii=False, indent=2))
        return {"packages": len(packages), "acknowledged_events": len(acknowledged)}

    def close(self):
        if self.client is not None:
            self.client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://127.0.0.1:8765")
    parser.add_argument("--addon-dir", required=True, type=Path)
    parser.add_argument("--saved-variables", required=True, type=Path)
    parser.add_argument("--market", required=True)
    parser.add_argument("--state", type=Path, default=Path("data/bridge/state.json"))
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Explicitly run synchronization every 10 seconds; never triggers analysis",
    )
    args = parser.parse_args()
    bridge = Bridge(
        args.server,
        args.addon_dir,
        args.saved_variables,
        args.market,
        args.state,
        os.getenv("CENNIK_API_KEY", ""),
    )
    try:
        while True:
            try:
                print(json.dumps(bridge.synchronize(), ensure_ascii=False), flush=True)
            except (ValueError, RuntimeError, OSError, httpx.HTTPError) as exc:
                print("Sync error: " + str(exc), flush=True)
                if not args.watch:
                    raise SystemExit(1) from exc
            if not args.watch:
                break
            time.sleep(10)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()


if __name__ == "__main__":
    main()
