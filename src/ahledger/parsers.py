from __future__ import annotations

import re
import statistics
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from .models import History, Item, JobSpec, Market
from .util import copper, loads, timestamp


@dataclass
class Parsed:
    data: Any
    status: str = "COMPLETE"
    count: int = 0
    warnings: list[str] = field(default_factory=list)
    minimum: str | None = None
    maximum: str | None = None
    granularity: str | None = None
    interval_seconds: int | None = None
    gaps: list[dict] = field(default_factory=list)
    items: dict[int, dict] = field(default_factory=dict)
    schema: dict = field(default_factory=dict)


class CatalogIndex(HTMLParser):
    def __init__(self):
        super().__init__()
        self.classes: dict[str, dict] = {}
        self.current: str | None = None
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        href = dict(attrs).get("href", "")
        match = re.fullmatch(r"/wow-forever/items/([a-z-]+)", href)
        if tag == "a" and match:
            self.current = match[1]
            self.text = []

    def handle_data(self, data):
        if self.current:
            self.text.append(data.strip())

    def handle_endtag(self, tag):
        if tag == "a" and self.current:
            values = [s for s in self.text if s]
            count = next(
                (int(s.replace(",", "")) for s in values if re.fullmatch(r"[\d,]+", s)), None
            )
            label = next((s for s in values if not re.fullmatch(r"[\d,]+", s)), self.current)
            self.classes[self.current] = {"name": label, "count": count}
            self.current = None


def index_classes(body: bytes) -> dict[str, dict]:
    parser = CatalogIndex()
    parser.feed(body.decode("utf-8"))
    if not parser.classes:
        raise ValueError("Catalog index has no class links; format changed")
    return parser.classes


def parse_ahl1(body: bytes, market: str | None = None) -> dict:
    lines = body.decode("utf-8-sig").splitlines()
    if not lines or len(lines[0].split("|")) != 4:
        raise ValueError("AHL1 header must have four fields")
    version, table_market, unix, count = lines[0].split("|")
    if version != "AHL1":
        raise ValueError(f"Unsupported price table format: {version}")
    table_market = table_market.replace("/", ".")
    if market and table_market != market:
        raise ValueError("Price table market does not match request")
    observed = timestamp(int(unix))
    if int(count) != len(lines) - 1 or int(count) < 0:
        raise ValueError("AHL1 declared row count does not match body")
    names = (
        "id",
        "median",
        "minBuyout",
        "quantity",
        "median7d",
        "median30d",
        "low30d",
        "high30d",
        "salesPerDayX10",
    )
    rows, seen = [], set()
    for line in lines[1:]:
        fields = line.split(":")
        if len(fields) != 9:
            raise ValueError(f"AHL1 row has {len(fields)} fields, expected 9")
        row: dict[str, Any] = {
            key: int(value) if value else None for key, value in zip(names, fields, strict=True)
        }
        if row["id"] is None or row["id"] <= 0 or row["id"] in seen:
            raise ValueError("AHL1 invalid or duplicate item id")
        if any(value is not None and value < 0 for value in row.values()):
            raise ValueError("AHL1 negative quantity/price")
        seen.add(row["id"])
        row["salesPerDay"] = (
            Decimal(row["salesPerDayX10"]) / 10 if row["salesPerDayX10"] is not None else None
        )
        rows.append(row)
    return {
        "market": table_market,
        "observedAt": observed,
        "declaredCount": int(count),
        "rows": rows,
    }


def extract_items(value: Any) -> dict[int, dict]:
    found: dict[int, dict] = {}

    def visit(node):
        if isinstance(node, dict):
            if isinstance(node.get("supplyShocks"), list):
                for item_id in node["supplyShocks"]:
                    if isinstance(item_id, int) and item_id > 0:
                        found.setdefault(item_id, {"id": item_id})
            if isinstance(node.get("id"), int) and node["id"] > 0 and "name" in node:
                found[node["id"]] = node
            if isinstance(node.get("itemId"), int) and node["itemId"] > 0:
                found.setdefault(node["itemId"], {"id": node["itemId"]})
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return found


def schema_shape(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: schema_shape(v) for k, v in sorted(data.items())}
    if isinstance(data, list):
        return {"array": schema_shape(data[0]) if data else "empty"}
    return {"int": "integer", "Decimal": "number", "NoneType": "null"}.get(
        type(data).__name__, type(data).__name__
    )


def validate_values(data: Any):
    observed_money = {
        "min",
        "minBuyout",
        "median",
        "median7d",
        "median30d",
        "median90d",
        "p25",
        "p75",
        "low30d",
        "high30d",
        "price",
        "resultPrice",
        "value",
        "valueLow",
        "vendorBuy",
        "vendorSell",
    }
    estimated_money = {
        "netValue",
        "cost",
        "margin",
        "profit",
        "profitPerDay",
        "disenchantValue",
        "saving",
        "base",
    }
    counts = {
        "quantity",
        "auctions",
        "qty",
        "qtyMin",
        "qtyMax",
        "units",
        "scans",
        "minCount",
        "maxCount",
    }
    if isinstance(data, dict):
        for key, value in data.items():
            if value is not None and key in observed_money:
                copper(value)
            if value is not None and key in estimated_money:
                copper(value, estimated=True)
            if (
                value is not None
                and key in counts
                and (
                    not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < 2**63
                )
            ):
                raise ValueError(f"Invalid non-negative integer count: {key}")
            if (
                key == "chance"
                and value is not None
                and not Decimal(0) <= Decimal(str(value)) <= Decimal(1)
            ):
                raise ValueError("Invalid probability")
            validate_values(value)
    elif isinstance(data, list):
        for value in data:
            validate_values(value)


def parse(spec: JobSpec, body: bytes) -> Parsed:
    dataset = spec.dataset
    data: Any
    if dataset in {"documentation", "llms"}:
        text = body.decode("utf-8")
        if not text.strip():
            raise ValueError("Empty text document")
        return Parsed(text, count=1)
    if dataset == "catalog_index":
        data = index_classes(body)
        return Parsed(data, count=len(data))
    if dataset == "builds":
        tree = ET.fromstring(body)
        if tree.tag != "rss":
            raise ValueError("Expected RSS document")
        rows: list[dict[str, Any]] = [
            {child.tag: child.text for child in item} for item in tree.findall("./channel/item")
        ]
        for row in rows:
            if row.get("pubDate"):
                row["observedAt"] = parsedate_to_datetime(row["pubDate"]).isoformat()
        return Parsed(
            rows,
            status="PARTIAL",
            count=len(rows),
            warnings=["RSS содержит описания билдов, не полный структурированный diff предметов"],
        )
    if dataset == "pricetable":
        data = parse_ahl1(body, spec.market)
        return Parsed(
            data,
            count=len(data["rows"]),
            status="COMPLETE" if data["rows"] else "NO_DATA",
            minimum=data["observedAt"],
            maximum=data["observedAt"],
            granularity="snapshot",
            items={r["id"]: {"id": r["id"]} for r in data["rows"]},
        )
    data = loads(body)
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    validate_values(data)
    result = Parsed(data, schema=schema_shape(data), items=extract_items(data))
    if data.get("currency") and str(data["currency"]).lower() not in {"copper", "cp"}:
        raise ValueError("Unsupported currency")
    if spec.market and "market" in data and data["market"].replace("/", ".") != spec.market:
        raise ValueError("Response market does not match request")
    if dataset == "markets":
        rows = data["markets"]
        ids = [Market.model_validate(row).id for row in rows]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate market ids")
        result.count = len(rows)
    elif dataset == "professions":
        if not isinstance(data.get("professions"), list) or not data["professions"]:
            raise ValueError("Profession validation response has no professions")
        result.count = len(data["professions"])
    elif dataset == "catalog":
        rows = data["rows"]
        seen: set[int] = set()
        for row in rows:
            if not isinstance(row, list) or len(row) != 8:
                raise ValueError("Compact catalog row must contain eight fields")
            if not isinstance(row[0], int) or row[0] <= 0 or row[0] in seen:
                raise ValueError("Catalog contains invalid or duplicate ids")
            if not isinstance(row[1], str) or not isinstance(row[4], int) or row[4] < 0:
                raise ValueError("Invalid compact catalog values")
            seen.add(row[0])
            result.items[row[0]] = {
                "id": row[0],
                "name": row[1],
                "quality": row[2],
                "itemClass": spec.context.get("class_name", spec.params.get("class")),
                "itemSubclass": row[3],
                "level": row[4],
                "requiredLevel": row[5],
                "icon": row[6],
                "inventoryType": row[7],
            }
        result.count = len(rows)
        expected = spec.context.get("expected_count")
        if expected is not None and result.count != expected:
            result.status = "PARTIAL"
            result.warnings.append(
                f"Catalog index advertises {expected}, returned {result.count}; possible build drift or truncation"
            )
    elif dataset == "item":
        item = Item.model_validate(data)
        if item.id != spec.item_id:
            raise ValueError("Item response id does not match request")
        result.count = 1
        for field_name in ("vendorSell", "vendorBuy"):
            copper(data.get(field_name))
    elif dataset == "history":
        model = History.model_validate(data)
        if model.range != spec.params["range"] or model.item.id != spec.item_id:
            raise ValueError("Price response range/item does not match request")
        result.granularity = {"24h": "scan", "7d": "hour", "30d": "day", "90d": "day"}[model.range]
        result.interval_seconds = {"7d": 3600, "30d": 86400, "90d": 86400}.get(model.range)
        times = sorted(point.t for point in model.series)
        if len(set(times)) != len(times):
            raise ValueError("Duplicate timestamps within one history range")
        result.count = len(times)
        result.minimum, result.maximum = (times[0], times[-1]) if times else (None, None)
        if not times:
            result.status = "NO_DATA"
        if model.latest is not None:
            timestamp(model.latest["observedAt"])
            for key in ("median", "minBuyout", "median7d", "median30d", "median90d"):
                copper(model.latest.get(key))
        intervals = [
            (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
            for a, b in zip(times[:-1], times[1:], strict=True)
        ]
        result.data["_observed_intervals"] = {
            "minimum": min(intervals) if intervals else None,
            "median": statistics.median(intervals) if intervals else None,
            "maximum": max(intervals) if intervals else None,
        }
        for a, b, interval in zip(times[:-1], times[1:], intervals, strict=True):
            if result.interval_seconds and interval > result.interval_seconds:
                result.gaps.append(
                    {
                        "from": a,
                        "to": b,
                        "missing_buckets": int(interval // result.interval_seconds) - 1,
                    }
                )
        if result.gaps:
            result.status = "PARTIAL"
            result.warnings.append(
                "Missing time buckets; first/last observation does not imply continuity"
            )
    elif dataset in {"crafting", "deals", "signals", "structure", "disenchant"}:
        key = {
            "crafting": "recipes",
            "deals": "deals",
            "signals": "signals",
            "structure": "items",
            "disenchant": "opportunities",
        }[dataset]
        rows = data[key]
        if not isinstance(rows, list):
            raise ValueError(f"Expected list: {key}")
        if "marketStatus" not in data or "generatedAt" not in data:
            raise ValueError("Market tool response is missing provenance fields")
        timestamp(data["generatedAt"])
        timestamp(data.get("pricesAsOf"))
        result.count = len(rows)
        result.minimum = result.maximum = timestamp(data.get("pricesAsOf"))
        result.granularity = "estimate"
        limit = data.get("filters", {}).get("limit")
        if limit and len(rows) >= int(limit):
            result.status = "PARTIAL"
            result.warnings.append(f"Reached server limit {limit}; no documented pagination")
        if dataset in {"structure", "deals", "disenchant", "signals"} and rows:
            result.status = "PARTIAL"
            result.warnings.append(
                "Filtered/ranked current dataset, not an exhaustive historical archive"
            )
        if dataset == "crafting":
            ids = [row["recipe"]["spellId"] for row in rows]
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate spellId in crafting response")
            for row in rows:
                if "result" not in row or "reagents" not in row:
                    raise ValueError("Recipe missing result/reagents")
                for reagent in row["reagents"]:
                    if not isinstance(reagent["qty"], int) or reagent["qty"] < 0:
                        raise ValueError("Invalid reagent quantity")
        if not rows:
            result.status = "NO_DATA"
    elif dataset in {"economy", "economy_market"}:
        daily = data["daily"]
        if not isinstance(daily, list):
            raise ValueError("Economy daily history must be a list")
        dates = [timestamp(row["day"]) or "" for row in daily]
        for row in daily:
            copper(row.get("value"))
            copper(row.get("valueLow"))
        result.count = len(daily)
        result.minimum, result.maximum = (min(dates), max(dates)) if dates else (None, None)
        result.granularity, result.interval_seconds = "day", 86400
        per_market: dict[str, list[str]] = {}
        for row in daily:
            per_market.setdefault(row.get("market", spec.market or "unknown"), []).append(
                timestamp(row["day"]) or ""
            )
        for market_id, times in per_market.items():
            if len(set(times)) != len(times):
                raise ValueError("Duplicate economy day/market")
            times.sort()
            for a, b in zip(times[:-1], times[1:], strict=True):
                distance = (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
                if distance > 86400:
                    result.gaps.append(
                        {
                            "market": market_id,
                            "from": a,
                            "to": b,
                            "missing_buckets": int(distance // 86400) - 1,
                        }
                    )
        if result.gaps:
            result.status = "PARTIAL"
            result.warnings.append("Missing daily economy buckets")
        if not daily:
            result.status = "NO_DATA"
    else:
        raise ValueError(f"No parser for dataset {dataset}")
    for field_name in ("count", "totalCount"):
        if isinstance(data.get(field_name), int) and data[field_name] != result.count:
            result.status = "PARTIAL"
            result.warnings.append(f"Declared {field_name} differs from parsed count")
    if (
        data.get("next")
        or data.get("nextCursor")
        or data.get("hasMore")
        or data.get("pagination", {}).get("hasMore")
    ):
        result.status = "PARTIAL"
        result.warnings.append(
            "Unconsumed pagination indicated; unsupported API change, not complete"
        )
    return result
