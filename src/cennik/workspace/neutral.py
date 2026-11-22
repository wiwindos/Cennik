"""Read-only resale research from separately archived, confirmed neutral scans."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal

import pyarrow as pa
import zstandard as zstd

from ahledger.util import dumps
from cennik.engines.economics import auction_net
from cennik.markets import neutral_market

from .scan_rows import unpack_scan


def variant_key(item_id, link):
    """Ignore link presentation/player context, retain enchant/gems/suffix/bonuses.

    Unrecognized links stay distinct and never borrow a base-item AHL quote.
    """
    match = re.search(r"item:([\d:-]+)", link or "")
    if not match:
        return str(item_id) if not link else str(item_id)+":"+str(link)
    parts = match[1].split(":")
    if parts[0] != str(item_id):
        return str(item_id)+":"+match[1]
    modifiers = [(s or "0") for s in parts[1:8]]
    modifiers += ["0"] * (7-len(modifiers))
    # Fields 8..11 describe player level/spec/upgrade/difficulty. Bonus count,
    # bonuses and subsequent modifiers (12 onward) can change the item itself.
    bonuses = [(s or "0") for s in parts[12:]]
    while bonuses and bonuses[-1] == "0":
        bonuses.pop()
    if not any(s != "0" for s in modifiers+bonuses):
        return str(item_id)
    return str(item_id)+":"+":".join(modifiers+bonuses)


def weighted_median(rows):
    ordered = sorted(rows, key=lambda r: Decimal(r["buyout"])/r["quantity"])
    total = sum(r["quantity"] for r in ordered)
    targets = ((total+1)//2, (total+2)//2)
    running, middle = 0, []
    for row in ordered:
        running += row["quantity"]
        while len(middle) < 2 and running >= targets[len(middle)]:
            middle.append(Decimal(row["buyout"])/row["quantity"])
        if len(middle) == 2:
            return int(sum(middle)/2)
    return None


class NeutralAuctions:
    def __init__(self, database):
        self.market = database
        self.backfilled = False
        self.cache = None
        self.last_good = None
        self.refresh_lock = threading.Lock()
        self.refresh_thread = None
        self.closed = False
        with database.lock:
            database.db.execute("""CREATE TABLE IF NOT EXISTS neutral_scan_registry(
                scan_id VARCHAR PRIMARY KEY,observed_at TIMESTAMPTZ,received_at TIMESTAMPTZ,
                row_count BIGINT,valid_rows BIGINT,complete BOOLEAN,path VARCHAR,warnings VARCHAR);
                CREATE TABLE IF NOT EXISTS neutral_offers AS SELECT * FROM working_offers WHERE false;
            """)

    def ingest(self, scan, identity, path):
        """Called only after intake classified and durably archived a neutral scan."""
        m = self.market
        if scan.get("market") != neutral_market(m.market_key):
            raise ValueError("Межфракционный снимок другого рынка")
        observed = datetime.fromtimestamp(scan["observed_at"], UTC)
        with m.lock:
            if m.rows("SELECT 1 FROM neutral_scan_registry WHERE scan_id=?", [identity]):
                return
            offers, warnings, complete = unpack_scan(scan, identity, observed)
            head = m.rows("SELECT max(observed_at) t FROM neutral_scan_registry WHERE complete")[0]["t"]
            replace = complete and (head is None or observed >= head)
            m.db.execute("BEGIN")
            try:
                m.db.execute("INSERT INTO neutral_scan_registry VALUES(?,?,?,?,?,?,?,?)", [identity, observed,
                    datetime.now(UTC), scan["data"].count(";")+1 if scan["data"] else 0, len(offers),
                    complete, str(path.relative_to(m.root)), dumps(warnings)])
                if replace:
                    m.db.execute("DELETE FROM neutral_offers")
                    if offers:
                        m.db.register("neutral_incoming", pa.Table.from_pylist(offers))
                        try:
                            m.db.execute("INSERT INTO neutral_offers BY NAME SELECT * FROM neutral_incoming")
                        finally:
                            m.db.unregister("neutral_incoming")
                m.db.execute("COMMIT")
            except Exception:
                m.db.execute("ROLLBACK")
                raise
            self.last_good = self.cache or self.last_good
            self.cache = None
            m.names.scan(scan)

    def backfill(self):
        """Index the existing trusted category once; never promote unknown archives."""
        if self.backfilled:
            return
        m = self.market
        with m.lock:
            if self.backfilled:
                return
            known = {r["scan_id"] for r in m.rows("SELECT scan_id FROM neutral_scan_registry")}
            root = m.root/"archive/neutral_auctions"/neutral_market(m.market_key)
            for path in sorted(root.rglob("*.json.zst")):
                identity = path.name.removesuffix(".json.zst")
                if identity in known:
                    continue
                try:
                    scan = json.loads(zstd.ZstdDecompressor().decompress(path.read_bytes(), max_output_size=64_000_000))
                    if hashlib.sha256(dumps(scan).encode()).hexdigest() != identity:
                        raise ValueError("Неверная контрольная сумма архива")
                    self.ingest(scan, identity, path)
                except (ValueError, TypeError, KeyError, OSError, zstd.ZstdError):
                    logging.getLogger("cennik.neutral").exception("Neutral archive could not be indexed: %s", path.name)
            self.backfilled = True

    def comparison(self, *, cut, own_only, prepared=False):
        m=self.market
        # Filtering a prepared comparison must remain possible during analytics.
        # Its quote timestamps stay intact; the response explicitly marks the wait.
        cached=self.cache or self.last_good
        compatible=cached is not None and cached[0][-2:]==(str(cut),own_only)
        if not m.lock.acquire(blocking=not (prepared and compatible)):
            return {**cached[1], 'refreshing':True}
        try:
            if prepared and compatible:
                if self.cache is cached and cached[0][1]==m.generation():
                    return cached[1]
                self.queue_refresh(cut=cut,own_only=own_only)
                return {**cached[1],'refreshing':True}
            return self._comparison(cut=cut,own_only=own_only)
        finally:
            m.lock.release()

    def queue_refresh(self, *, cut, own_only):
        with self.refresh_lock:
            if self.closed or self.refresh_thread and self.refresh_thread.is_alive():return
            def refresh():
                try:self._comparison(cut=cut,own_only=own_only)
                except Exception:logging.getLogger('cennik.neutral').exception('Neutral comparison refresh failed')
            self.refresh_thread=threading.Thread(target=refresh,name='cennik-neutral-view',daemon=True)
            self.refresh_thread.start()

    def close(self):
        with self.refresh_lock:
            self.closed=True
            thread=self.refresh_thread
        if thread:thread.join()

    def _comparison(self, *, cut, own_only):
        from .data import WorkspaceProvider

        m = self.market
        self.backfill()
        with m.lock:
            head = m.rows("SELECT * FROM neutral_scan_registry WHERE complete ORDER BY observed_at DESC,received_at DESC LIMIT 1")
            key = (head[0]["scan_id"] if head else None, m.generation(), str(cut), own_only)
            if self.cache and self.cache[0] == key:
                return self.cache[1]
            groups = defaultdict(list)
            for lot in m.rows("SELECT * FROM neutral_offers WHERE buyout>0 AND owned IS NOT TRUE"):
                groups[variant_key(lot["item_id"], lot["variant"])].append(lot)
            if not groups:
                result = {"rows": [], "snapshot": head[0] if head else None,**self.scan_summary()}
                self.cache = (key, result)
                return result
            now = datetime.now(UTC)
            item_ids=sorted({lots[0]['item_id'] for lots in groups.values()})
            base_ids=sorted({lots[0]['item_id'] for variant,lots in groups.items() if variant==str(lots[0]['item_id'])})
            sales={} if own_only else {r['item_id']:r for r in m.rows("""SELECT item_id,
                sales_estimated_per_day,observed_at,source FROM price_snapshots
                WHERE market=? AND observed_at<=? AND sales_estimated_per_day IS NOT NULL
                AND item_id IN (SELECT unnest(?))
                QUALIFY row_number() OVER(PARTITION BY item_id ORDER BY observed_at DESC,source,raw_id)=1""",
                [m.market_key,now,item_ids])}
            with WorkspaceProvider(m, m.market_key, now, own_data_only=own_only) as provider:
                prices = provider.current_prices(base_ids)
                references = {} if own_only else {r["item_id"]: r for r in m.rows("""SELECT item_id,
                    median30d_copper copper,observed_at FROM price_snapshots
                    WHERE market=? AND source='AHL1' AND observed_at<=? AND median30d_copper>0
                    AND item_id IN (SELECT unnest(?))
                    QUALIFY row_number() OVER(PARTITION BY item_id ORDER BY observed_at DESC)=1""", [m.market_key, now,base_ids])}
            ordinary = defaultdict(list)
            special_ids = sorted({lots[0]["item_id"] for variant,lots in groups.items() if variant != str(lots[0]["item_id"])})
            if special_ids:
                # Do not copy the entire ordinary order book (hundreds of thousands
                # of lots) into every web worker just to compare a few variants.
                for lot in m.rows("SELECT * FROM working_offers WHERE buyout>0 AND item_id IN (SELECT unnest(?))", [special_ids]):
                    ordinary[variant_key(lot["item_id"], lot["variant"])].append(lot)
            result = []
            sales_by_item=sales
            for variant, lots in groups.items():
                item = lots[0]["item_id"]
                special = variant != str(item)
                price = prices.get(item)
                matches = ordinary.get(variant, [])
                minimum = median = observed = source = None
                sales = sales_at = sales_source = None
                estimate=sales_by_item.get(item)
                if estimate:
                    sales,sales_at,sales_source=estimate['sales_estimated_per_day'],estimate['observed_at'],estimate['source']
                if special:
                    # Base-item AHL statistics cannot price a suffix/bonus variant.
                    # A stale own book may be shown, with its real timestamp.
                    if matches:
                        minimum = int(min(Decimal(r["buyout"])/r["quantity"] for r in matches))
                        median = weighted_median(matches)
                        observed = matches[0]["observed_at"]
                        source = "Cennik replicate · точный вариант"
                elif price:
                    minimum, median, observed, source = price.min_copper, price.median_copper, price.observed_at, price.source
                reference = references.get(item) if not special else None
                historic = int(reference["copper"]) if reference else None
                candidates = [v for v in (minimum, median, historic) if v is not None and v > 0]
                sale = min(candidates) if observed and candidates else None
                flags = ["sale_unconfirmed"]
                if sales is None:
                    flags.append("unknown_sales")
                if not historic:
                    flags.append("no_history")
                if sale is None:
                    flags.append("no_variant_price" if special else "no_sale_price")
                if special:
                    flags.append("exact_variant")
                lots.sort(key=lambda r: (Decimal(r["buyout"])/r["quantity"], r["buyout"], r["sequence"]))
                result.append({"id": hashlib.sha256(variant.encode()).hexdigest(), "item_id": item,
                    "name": lots[0]["name"], "variant_name": lots[0]["name"], "special_variant": special, "variant": lots[0]["variant"],
                    "quantity_available": sum(l["quantity"] for l in lots), "lot_count": len(lots),
                    "buy_min_copper": int(Decimal(lots[0]["buyout"])/lots[0]["quantity"]),
                    "ordinary_min_copper": minimum, "ordinary_median_copper": median,
                    "historical_median_copper": historic, "historical_observed_at": reference["observed_at"] if reference else None,
                    "sale_copper": sale, "sale_observed_at": observed, "sale_source": source,
                    "sales_per_day": str(sales) if sales is not None else None, "sales_observed_at": sales_at,
                    "sales_source": sales_source,
                    "sales_scope": "all_variants" if special else "item",
                    "sales_unavailable_reason": "own_data_only" if own_only else "no_source_estimate" if sales is None else None,
                    "flags": flags, "lots": lots})
            result = {"rows": result, "snapshot": head[0] if head else None,**self.scan_summary()}
            self.cache = (key, result)
            return result

    def scan_summary(self):
        m=self.market
        stats=m.rows("SELECT count(*) scans,count(*) FILTER(WHERE complete) complete_scans FROM neutral_scan_registry")[0]
        latest=m.rows("SELECT observed_at,complete,warnings FROM neutral_scan_registry ORDER BY observed_at DESC,received_at DESC LIMIT 1")
        return {**stats,'last_attempt':latest[0] if latest else None}

    @staticmethod
    def plan(row, cut, budget):
        sale = row["sale_copper"]
        picked, cost, quantity, gross, net = [], 0, 0, 0, 0
        for lot in row["lots"]:
            if sale is None:
                break
            proceeds = auction_net(sale*lot["quantity"], cut)
            profit = proceeds-lot["buyout"]
            if profit <= 0 or budget is not None and cost+lot["buyout"] > budget:
                continue
            picked.append({"sequence": lot["sequence"], "quantity": lot["quantity"], "buyout_copper": lot["buyout"],
                "unit_copper": float(Decimal(lot["buyout"])/lot["quantity"]), "gross_copper": sale*lot["quantity"],
                "net_copper": proceeds, "profit_copper": profit})
            cost += lot["buyout"]
            quantity += lot["quantity"]
            gross += sale*lot["quantity"]
            net += proceeds
        profit = net-cost
        best_margin = None if sale is None else auction_net(sale*row["lots"][0]["quantity"], cut)-row["lots"][0]["buyout"]
        return {**{k: v for k, v in row.items() if k != "lots"}, "quantity": quantity,
            "cost_copper": cost, "gross_copper": gross, "net_copper": net,
            "cut_copper": gross-net, "profit_copper": profit,
            "profit_per_unit_copper": float(Decimal(profit)/quantity) if quantity else
                (float(Decimal(best_margin)/row["lots"][0]["quantity"]) if best_margin is not None else None),
            "roi_percent": float(Decimal(profit)*100/cost) if cost else None,
            "selected_lot_count": len(picked), "selected_lots": picked}

    def report(self, *, cut, own_only=False, mode="deals", q="", min_profit=0, min_roi=0,
               budget=None, max_age=0, sort="profit", order="desc", offset=0, limit=50, selected=None,
               min_sales=None, max_sales=None, price_basis="buy", min_price=None, max_price=None, max_roi=None,prepared=False):
        for lower,upper,label in ((min_sales,max_sales,"Продажи в сутки"),
                                  (min_price,max_price,"Цена"),(min_roi,max_roi,"Доходность")):
            if lower is not None and upper is not None and lower > upper:
                raise ValueError(label+": значение «от» больше значения «до»")
        m = self.market
        now = datetime.now(UTC)
        data = self.comparison(cut=cut, own_only=own_only,prepared=prepared)
        snapshot = data["snapshot"]
        age = lambda at: max(0, int((now-at).total_seconds())) if at else None
        rows = []
        translated = set(m.names.search(q, 50000)) if q else set()
        for row in data["rows"]:
            if selected and row["id"] != selected:
                continue
            if q and q.casefold() not in (row["name"] or "").casefold() and str(row["item_id"]) != q and row["item_id"] not in translated:
                continue
            sales = Decimal(row["sales_per_day"]) if row["sales_per_day"] is not None else None
            if (min_sales is not None or max_sales is not None) and (sales is None
                    or min_sales is not None and sales < Decimal(str(min_sales))
                    or max_sales is not None and sales > Decimal(str(max_sales))):
                continue
            price = row["buy_min_copper"] if price_basis == "buy" else row["sale_copper"]
            if (min_price is not None or max_price is not None) and (price is None
                    or min_price is not None and price < min_price
                    or max_price is not None and price > max_price):
                continue
            plan = self.plan(row, cut, budget)
            if max_roi is not None and (plan["roi_percent"] is None or plan["roi_percent"] > max_roi):
                continue
            plan["sale_age_seconds"] = age(row["sale_observed_at"])
            plan["neutral_age_seconds"] = age(snapshot["observed_at"]) if snapshot else None
            if max_age and (plan["sale_age_seconds"] is None or plan["sale_age_seconds"] > max_age
                            or plan["neutral_age_seconds"] is None or plan["neutral_age_seconds"] > max_age):
                continue
            if mode == "deals" and (plan["profit_copper"] <= 0 or plan["profit_copper"] < min_profit
                                     or plan["roi_percent"] is None or plan["roi_percent"] < min_roi):
                continue
            if mode == "all" and (min_profit > 0 and plan["profit_copper"] < min_profit
                                  or min_roi > 0 and (plan["roi_percent"] is None or plan["roi_percent"] < min_roi)):
                continue
            if not selected:
                plan.pop("selected_lots")
            rows.append(plan)
        sort_key = {"profit": "profit_copper", "roi": "roi_percent", "unit": "profit_per_unit_copper",
                    "sales": "sales_per_day", "buy_price": "buy_min_copper", "sale_price": "sale_copper"}[sort]
        direction = 1 if order == "asc" else -1
        rows.sort(key=lambda r: (r[sort_key] is None, direction*Decimal(str(r[sort_key] or 0)), r["item_id"], r["id"]))
        summary = {**{key:data[key] for key in ('scans','complete_scans','last_attempt')}, "snapshot": snapshot,
            "item_count": len(data["rows"]), "row_count": snapshot["row_count"] if snapshot else 0,
            "ordinary_observed_at": max((r["sale_observed_at"] for r in data["rows"] if r["sale_observed_at"]), default=None)}
        page = rows[offset:offset+limit]
        if selected and page:
            picked = page[0]["selected_lots"]
            page[0]["selected_lots"] = picked[offset:offset+limit]
            page[0]["lots_truncated"] = len(picked)>limit
        return {"format_version":2,"comparison_refreshing":data.get('refreshing',False),
            "market": neutral_market(m.market_key), "sale_market": m.market_key,
            "sale_market_label": m.market_info.get("label") or m.market_key,
            "cut_percent": str(cut), "budget_copper": budget, "summary": summary, "as_of": now,
            "items": page, "total": len(rows), "offset": offset, "limit": limit}

# EOF
