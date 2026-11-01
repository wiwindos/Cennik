"""An explicit offline clock and combined saved evidence for development only."""
from datetime import datetime, timedelta, timezone

from cennik.data.duckdb import DuckDBProvider

from .data import WorkspaceProvider

LOCAL_TIME = timezone(timedelta(hours=5))


def coverage_report(database, real_now: datetime) -> dict:
    start = real_now - timedelta(days=4)
    days: dict[str, dict] = {}

    def day(stamp):
        key = stamp.astimezone(LOCAL_TIME).date().isoformat()
        return days.setdefault(key, {"day": key, "complete_scans": 0, "scan_rows": 0,
            "priced_items": 0, "api_price_items": 0, "reference_records": 0, "selected_at": None,
            "api_observed_at": None})

    cursor = start.astimezone(LOCAL_TIME).replace(hour=0, minute=0, second=0, microsecond=0)
    while cursor <= real_now:
        day(cursor)
        cursor += timedelta(days=1)

    scans = database.rows("""SELECT observed_at,valid_rows,quote_path FROM scan_registry
        WHERE complete AND market=? AND observed_at>=? AND observed_at<=? ORDER BY observed_at""",
        [database.market_key, start, real_now])
    for scan in scans:
        row = day(scan["observed_at"])
        row["complete_scans"] += 1
        row["scan_rows"] += scan["valid_rows"]
        count = 0
        if scan["quote_path"]:
            count = database.rows("SELECT count(DISTINCT item_id) n FROM read_parquet(?) WHERE min_copper>0",
                [str(database.root / scan["quote_path"])])[0]["n"]
        if count >= row["priced_items"]:
            row["priced_items"], row["selected_at"] = count, scan["observed_at"]
    imports = database.rows("""SELECT dataset,record_count,coalesce(maximum_at,retrieved_at) observed_at
        FROM dataset_metadata WHERE (market=? OR market IS NULL)
        AND status IN ('COMPLETE','PARTIAL') AND retrieved_at>=? AND retrieved_at<=?""",
        [database.market_key, start, real_now])
    for entry in imports:
        stamp = entry["observed_at"]
        if not start <= stamp <= real_now:
            continue
        row = day(stamp)
        if entry["dataset"] == "pricetable":
            count = entry["record_count"] or 0
            if (count, stamp) > (row["api_price_items"], row["api_observed_at"] or start):
                row["api_price_items"], row["api_observed_at"] = count, stamp
        elif entry["dataset"] == "crafting":
            row["reference_records"] += entry["record_count"] or 0
    for row in days.values():
        if row["api_price_items"] > row["priced_items"]:
            row["selected_at"] = row["api_observed_at"]
    candidates = [row for row in days.values() if row["selected_at"] and max(row["priced_items"], row["api_price_items"]) > 0]
    best = max(candidates, key=lambda row: (max(row["priced_items"], row["api_price_items"]),
        row["reference_records"], row["selected_at"]), default=None)
    return {"window_start": start, "window_end": real_now, "timezone": "Asia/Yekaterinburg",
        "days": sorted(days.values(), key=lambda row: row["day"]),
        "selected_at": best["selected_at"] if best else None,
        "selected_day": best["day"] if best else None,
        "selection": "Maximum item coverage per snapshot; references and recency break ties"}


class DevelopmentProvider:
    """Keep real evidence timestamps; only the calculation clock is frozen.

    Items absent from later scans may be reused here for UI/strategy development.
    This deliberately mixed dataset is never used by the live provider.
    """
    def __init__(self, database, market: str, as_of: datetime, data_as_of: datetime):
        self.market, self.as_of = market, as_of
        self.data = WorkspaceProvider(database, market, data_as_of)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.data.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.data, name)

    def current_prices(self):
        prices = DuckDBProvider.current_prices(self.data)
        sales = self.data.rows("""SELECT *, 'scan' granularity FROM source_sales_daily
            WHERE market=? AND observed_at<=? AND min_copper>0
            QUALIFY row_number() OVER(PARTITION BY item_id ORDER BY observed_at DESC,raw_id)=1""",
            [self.market, self.data.as_of])
        for row in sales:
            price = self.data.observation(row)
            previous = prices.get(price.item_id)
            if previous is None or price.observed_at > previous.observed_at:
                prices[price.item_id] = price
            if price.sales_estimated_per_day is not None:
                prices[price.item_id] = prices[price.item_id].model_copy(update={
                    "sales_estimated_per_day": price.sales_estimated_per_day,
                    "sales_observed_at": price.observed_at, "sales_source": price.source})
        paths = [str(self.data.database.root / row["quote_path"]) for row in self.data.rows(
            "SELECT quote_path FROM scan_registry WHERE complete AND market=? AND observed_at<=? AND quote_path IS NOT NULL",
            [self.market, self.data.as_of])]
        if paths:
            rows = self.data.rows("""SELECT *, 'scan' granularity FROM read_parquet(?)
                WHERE market=? AND CAST(observed_at AS TIMESTAMPTZ)<=? AND min_copper>0
                QUALIFY row_number() OVER(PARTITION BY item_id ORDER BY CAST(observed_at AS TIMESTAMPTZ) DESC,raw_id)=1""",
                [paths, self.market, self.data.as_of])
            for row in rows:
                price = self.data.observation(row)
                previous = prices.get(price.item_id)
                if previous is None or price.observed_at > previous.observed_at:
                    if previous:
                        price = price.model_copy(update={key: getattr(previous, key) for key in
                            ("sales_estimated_per_day", "sales_observed_at", "sales_source")})
                    prices[price.item_id] = price
        self.data.used_raw_ids.update(price.raw_id for price in prices.values() if price.raw_id)
        return prices

    def source_reference(self, item_id):
        if not hasattr(self, "_references"):
            self._references = {row["item_id"]: row for row in self.data.rows("""
                SELECT item_id,median30d_copper copper,observed_at,raw_id FROM (
                    SELECT * FROM source_sales_daily UNION ALL SELECT * FROM price_snapshots
                ) WHERE market=? AND source='AHL1' AND observed_at<=? AND median30d_copper>0
                QUALIFY row_number() OVER(PARTITION BY item_id ORDER BY observed_at DESC,raw_id)=1""",
                [self.market, self.data.as_of])}
        value = self._references.get(item_id)
        if value and value.get("raw_id"):
            self.data.used_raw_ids.add(value["raw_id"])
        return value
