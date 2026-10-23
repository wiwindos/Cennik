"""Read-only public AHledger API sample. Python 3.10+, standard library only."""

from __future__ import annotations

import argparse
import gzip
import io
import json
import logging
import os
import re
import socket
import time
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4


BASE_URL = "https://api.ahledger.com"
MARKET = "forever.pvp.alliance.us"
DOCUMENTATION = "https://ahledger.com/developers"
ROOT = Path(__file__).resolve().parent
MAX_BYTES = 64 * 1024 * 1024
INTERVAL = 2.2  # Serial requests: fewer than 30/min, including retries.
TIMEOUT = 45
ATTEMPTS = 4
MAX_RETRY_WAIT = 120
POPULAR_ITEMS = (2589, 2592, 4306, 4338, 14047)
LOG = logging.getLogger("ahledger_export")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_text(value: object, level: int = 0) -> str:
    """Pretty JSON preserving API decimal numbers without float conversion."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite JSON number")
        return str(value)
    if isinstance(value, (dict, list)):
        if not value:
            return "{}" if isinstance(value, dict) else "[]"
        indent = "  " * (level + 1)
        if isinstance(value, dict):
            parts = [
                json.dumps(key, ensure_ascii=False) + ": " + json_text(child, level + 1)
                for key, child in value.items()
            ]
            opening, closing = "{", "}"
        else:
            parts = [json_text(child, level + 1) for child in value]
            opening, closing = "[", "]"
        return opening + "\n" + indent + (",\n" + indent).join(parts) + "\n" + "  " * level + closing
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def save_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json_text(value) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def load_json(body: bytes) -> object:
    return json.loads(body.decode("utf-8-sig"), parse_float=Decimal, parse_constant=reject_constant)


def parse_pricetable(body: bytes) -> dict:
    lines = body.decode("utf-8-sig").splitlines()
    if not lines or len(lines[0].split("|")) != 4:
        raise ValueError("Expected AHL1 header with four fields")
    version, market, timestamp, declared = lines[0].split("|")
    if version != "AHL1" or market.replace("/", ".") != MARKET:
        raise ValueError("Unexpected AHL1 version or market")
    declared_count = int(declared)
    if declared_count < 0 or declared_count != len(lines) - 1:
        raise ValueError("AHL1 row count does not match header")
    columns = (
        "itemId", "median", "minBuyout", "quantity", "median7d",
        "median30d", "low30d", "high30d", "salesPerDayX10",
    )
    rows, seen = [], set()
    for number, line in enumerate(lines[1:], start=2):
        values = line.split(":")
        if len(values) != len(columns):
            raise ValueError(f"AHL1 line {number}: expected nine fields")
        row = {key: int(value) if value else None for key, value in zip(columns, values)}
        item_id = row["itemId"]
        if item_id is None or item_id <= 0 or item_id in seen:
            raise ValueError(f"AHL1 line {number}: invalid/duplicate item ID")
        if any(value is not None and value < 0 for value in row.values()):
            raise ValueError(f"AHL1 line {number}: negative value")
        seen.add(item_id)
        sales = row["salesPerDayX10"]
        row["salesEstimatedPerDay"] = Decimal(sales) / 10 if sales is not None else None
        rows.append(row)
    return {
        "format": "AHL1", "market": market, "timestamp": int(timestamp),
        "observedAt": datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat(),
        "declaredCount": declared_count, "moneyUnit": "copper",
        "salesNote": "Source estimate, not confirmed transactions; empty fields remain null.",
        "rows": rows,
    }


def retry_after(headers: dict) -> float | None:
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            LOG.warning("Cannot parse Retry-After: %r", value)
            return None


def read_body(response, headers: dict) -> bytes:
    raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Response exceeds 64 MiB")
    encoding = headers.get("content-encoding", "identity").lower()
    if encoding == "gzip":
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Decompressed response exceeds 64 MiB")
    elif encoding not in ("", "identity"):
        raise ValueError(f"Unsupported Content-Encoding: {encoding}")
    return raw


class Exporter:
    def __init__(self, directory: Path):
        self.directory = directory
        self.last_request = 0.0
        self.not_before = 0.0
        self.manifest = {
            "market": MARKET, "apiBase": BASE_URL, "documentation": DOCUMENTATION,
            "startedAt": utc_now(), "finishedAt": None, "status": "running",
            "scope": "Public documented routes; one sample item; no catalog crawl or uploads.",
            "consistency": "Independent responses, not an atomic snapshot; retain source timestamps.",
            "sampleItem": None, "requests": [],
        }
        self.checkpoint()

    def checkpoint(self) -> None:
        save_json(self.directory / "manifest.json", self.manifest)

    def fetch(self, name: str, route: str, params: dict | None = None, *, ahl1: bool = False):
        url = BASE_URL + route + ("?" + urlencode(params) if params else "")
        record = {
            "name": name, "url": url, "startedAt": utc_now(),
            "status": "pending", "file": None, "attempts": [], "warnings": [],
        }
        self.manifest["requests"].append(record)
        self.checkpoint()
        try:
            return self._fetch(record, ahl1)
        except Exception as error:
            record["status"] = "error"
            record["error"] = f"{type(error).__name__}: {error}"
            LOG.exception("%s: failed; continuing with other routes", name)
            return None
        finally:
            record["finishedAt"] = utc_now()
            if record["status"] == "error":
                save_json(self.directory / "errors" / (name + ".json"), record)
            self.checkpoint()

    def _fetch(self, record: dict, ahl1: bool):
        for attempt in range(1, ATTEMPTS + 1):
            delay = max(self.last_request + INTERVAL, self.not_before) - time.monotonic()
            if delay > MAX_RETRY_WAIT:
                raise RuntimeError("Server cooldown exceeds 120 seconds; this route was not requested")
            if delay > 0:
                time.sleep(delay)
            self.last_request = time.monotonic()
            request = Request(record["url"], headers={
                "User-Agent": "Cennik-API-Sample/1.0", "Accept": "text/plain" if ahl1 else "application/json",
            })
            event = {"number": attempt, "startedAt": utc_now()}
            record["attempts"].append(event)
            self.checkpoint()
            LOG.info("GET %s (attempt %s/%s)", record["url"], attempt, ATTEMPTS)
            started = time.monotonic()
            body, headers, status, network_error = b"", {}, None, None
            try:
                try:
                    response = urlopen(request, timeout=TIMEOUT)
                except HTTPError as error:
                    response = error
                with response:
                    status = response.status
                    headers = {key.lower(): value for key, value in response.headers.items()}
                    # Set a shared cooldown even if body reading/decoding subsequently fails.
                    server_wait = retry_after(headers)
                    if server_wait is not None:
                        self.not_before = max(self.not_before, time.monotonic() + server_wait)
                    body = read_body(response, headers)
            except (URLError, TimeoutError, socket.timeout, ConnectionError, HTTPException) as error:
                network_error = f"{type(error).__name__}: {error}"
            finally:
                event.update({
                    "httpStatus": status, "elapsedSeconds": round(time.monotonic() - started, 3),
                    "bytes": len(body), "headers": {key: headers[key] for key in (
                        "content-type", "content-encoding", "cache-control", "etag", "date", "retry-after",
                    ) if key in headers},
                })
                if network_error:
                    event["error"] = network_error
                self.checkpoint()
            LOG.info("%s: HTTP %s, %s bytes, %.3fs%s", record["name"], status, len(body),
                     event["elapsedSeconds"], "; " + network_error if network_error else "")
            if network_error or status in (408, 429, 500, 502, 503, 504):
                wait = retry_after(headers)
                wait = max(2 ** attempt, wait if wait is not None else 0)
                self.not_before = max(self.not_before, time.monotonic() + wait)
                if attempt < ATTEMPTS and wait <= MAX_RETRY_WAIT:
                    LOG.warning("Retry in at least %.1fs: %s", wait, record["name"])
                    continue
            if network_error:
                raise RuntimeError(network_error)
            if status != 200:
                try:
                    event["responseBody"] = load_json(body)
                except (ValueError, UnicodeError):
                    event["responsePreview"] = body[:1500].decode("utf-8", errors="replace")
                raise RuntimeError(f"HTTP {status}; see errors/{record['name']}.json")
            try:
                data = parse_pricetable(body) if ahl1 else load_json(body)
            except (ValueError, UnicodeError, OverflowError) as error:
                event["responsePreview"] = body[:1500].decode("utf-8", errors="replace")
                raise ValueError(f"Cannot parse successful HTTP response: {error}") from error
            if not isinstance(data, (dict, list)):
                raise ValueError("Expected JSON object or array")
            if isinstance(data, dict):
                response_market = data.get("market")
                if isinstance(response_market, str) and response_market.replace("/", ".") != MARKET:
                    raise ValueError(f"Wrong response market: {response_market}")
                if "error" in data and data["error"]:
                    raise ValueError(f"API returned an error in HTTP 200: {str(data['error'])[:300]}")
                for key in ("marketStatus", "pricesAsOf", "generatedAt", "filters", "notes"):
                    if key in data:
                        record[key] = data[key]
            relative = record["name"] + ".json"
            save_json(self.directory / relative, data)
            record.update({"status": "ok", "file": relative})
            if not data or isinstance(data, dict) and data.get("series") == []:
                record["warnings"].append("Empty data/history; no missing values were invented.")
                LOG.warning("%s: empty data/history", record["name"])
            LOG.info("Saved %s", relative)
            return data

    def finish(self, status: str) -> None:
        requests = self.manifest["requests"]
        self.manifest.update({
            "status": status, "finishedAt": utc_now(),
            "summary": {
                "routes": len({entry["url"].split("?")[0] for entry in requests}),
                "responsesSaved": sum(entry["status"] == "ok" for entry in requests),
                "errors": sum(entry["status"] == "error" for entry in requests),
                "incomplete": sum(entry["status"] == "pending" for entry in requests),
                "httpAttempts": sum(len(entry["attempts"]) for entry in requests),
            },
        })
        self.checkpoint()
        LOG.info("Finished: %s; %s; output: %s", status, self.manifest["summary"], self.directory)


def choose_item(table: dict | None, requested: int | None) -> tuple[int, str]:
    if requested:
        return requested, "Explicit --item-id; presence is not guaranteed."
    rows = table.get("rows", []) if table else []
    available = {
        row["itemId"]: row for row in rows
        if (row.get("quantity") or 0) > 0 and (row.get("minBuyout") or 0) > 0
    }
    for item_id in POPULAR_ITEMS:
        if item_id in available:
            return item_id, "Common cloth present in this market's current price table."
    if available:
        item_id = max(available, key=lambda candidate: available[candidate]["quantity"])
        return item_id, "No preferred cloth found; selected the priced item with the highest quantity."
    LOG.warning("No nonempty price table available; falling back to Linen Cloth 2589")
    return 2589, "Fallback Linen Cloth; current availability could not be verified."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--item-id", type=int, help="Sample item; default: choose one from the current price table")
    parser.add_argument("--profession", action="append", help="Crafting profession; repeat to sample more (default: Tailoring)")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "exports", help="Parent directory for new run folders")
    args = parser.parse_args()
    if args.item_id is not None and args.item_id <= 0:
        parser.error("--item-id must be positive")
    professions = list(dict.fromkeys(args.profession or ["Tailoring"]))
    if any(not profession.strip() for profession in professions):
        parser.error("--profession must not be empty")
    run_name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    directory = args.output_dir.expanduser().resolve() / run_name
    directory.mkdir(parents=True, exist_ok=False)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[
        logging.StreamHandler(), logging.FileHandler(directory / "export.log", encoding="utf-8"),
    ])
    exporter = Exporter(directory)
    try:
        exporter.fetch("system/health", "/health")
        exporter.fetch("system/markets", "/v1/markets")
        table = exporter.fetch("market/pricetable", f"/v1/pricetable/{MARKET}", ahl1=True)
        item_id, reason = choose_item(table, args.item_id)
        exporter.manifest["sampleItem"] = {"id": item_id, "selectionReason": reason}
        exporter.checkpoint()
        LOG.info("Sample item #%s: %s", item_id, reason)
        prefix = f"item_{item_id}"
        exporter.fetch(f"{prefix}/details", f"/v1/items/{item_id}", {"game": "forever"})
        exporter.fetch(f"{prefix}/tooltip", f"/v1/tooltip/{MARKET}", {"items": str(item_id)})
        for period in ("24h", "7d", "30d", "90d"):
            exporter.fetch(f"{prefix}/prices_{period}", f"/v1/prices/{MARKET}/{item_id}", {"range": period})
        for sort in ("saving", "discount", "sales"):
            exporter.fetch(f"market/deals_{sort}", f"/v1/deals/{MARKET}", {
                "discount": 15, "units": 1, "sells": 0, "sort": sort, "limit": 200,
            })
        for number, profession in enumerate(professions, start=1):
            slug = re.sub(r"[^a-z0-9]+", "_", profession.lower()).strip("_") or "profession"
            exporter.fetch(f"crafting/{number:02d}_{slug}", f"/v1/crafting/{MARKET}", {
                "profession": profession, "all": 1,
            })
        exporter.fetch("market/structure", f"/v1/structure/{MARKET}")
        exporter.fetch("market/signals", f"/v1/signals/{MARKET}", {"discount": 20, "days": 7})
        exporter.fetch("market/disenchant", f"/v1/disenchant/{MARKET}", {"all": "true"})
        exporter.fetch("economy/global", "/v1/economy")
        exporter.fetch("economy/market", f"/v1/economy/{MARKET}")
        failed = any(entry["status"] != "ok" for entry in exporter.manifest["requests"])
        exporter.finish("partial" if failed else "completed")
        return 1 if failed else 0
    except KeyboardInterrupt:
        LOG.warning("Interrupted by user; already saved responses remain available")
        exporter.finish("interrupted")
        return 130
    except Exception:
        LOG.exception("Fatal exporter error")
        exporter.finish("failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
