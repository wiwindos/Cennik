from __future__ import annotations

import html
from datetime import UTC, date, datetime, timedelta

import duckdb

from .storage import TERMINAL, State
from .util import atomic_json, atomic_write, dumps, utc_now


def query_rows(connection, sql, params=None):
    result = connection.execute(sql, params or [])
    columns = [entry[0] for entry in result.description]
    return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]


def create_report(state: State) -> dict:
    counts = state.counts()
    total = sum(counts.values())
    finished = sum(counts.get(k, 0) for k in TERMINAL)
    raw_files = list((state.root / "raw" / "blobs").glob("*/*.zst"))
    raw_size = sum(path.stat().st_size for path in raw_files)
    dataset_jobs = [
        dict(row)
        for row in state.db.execute(
            "SELECT dataset,market,status,count(*) AS jobs,sum(records) AS records FROM jobs GROUP BY dataset,market,status"
        )
    ]
    requests = query_rows_sqlite(
        state, "SELECT status,count(*) AS count FROM responses GROUP BY status"
    )
    report = {
        "generated_at": utc_now(),
        "attribution": {"text": "Prices from AHledger", "url": "https://ahledger.com"},
        "download_status": "COMPLETE"
        if total
        and finished == total
        and not counts.get("PARTIAL")
        and state.get("full_plan_created", False)
        and state.get("markets")
        and state.get("catalog_classes")
        and state.get("professions")
        else "PARTIAL",
        "queue_status": "COMPLETE" if total and finished == total else "PARTIAL",
        "jobs": counts,
        "total_jobs": total,
        "terminal_jobs": finished,
        "completion_fraction": finished / total if total else 0,
        "scope": state.get("plan_scope", "bounded_research_only"),
        "queue_completeness_is_not_dataset_continuity": True,
        "unique_raw_responses": len(raw_files),
        "raw_compressed_bytes": raw_size,
        "storage_bytes": {
            name: sum(p.stat().st_size for p in (state.root / name).rglob("*") if p.is_file())
            for name in ("raw", "analytics", "parquet")
        },
        "http_responses": requests,
        "datasets": dataset_jobs,
        "raw_auction_scans": "unavailable",
        "contact_url": "https://ahledger.com/contact",
        "source_limits": {
            "crafting": "500 per profession; no documented pagination, a 500-row result is PARTIAL",
            "disenchant": "100 ranked rows; all=true does not remove the cap",
            "deals": "200 ranked rows per sort; overlap deduplicated, no exhaustive listing history",
            "structure": "60 positions; partial market structure",
            "catalog": "Eight compact columns; vendor/binding/stack fields not supplied. Item endpoint supplies vendorSell but may still omit other fields",
            "item_sources": "No documented bulk public endpoint for NPC/vendors/drops/quests/zones",
            "build_changes": "RSS build summaries, not complete structured item/recipe version diffs",
            "24h": "Per full scan, irregular sampling; auction prices remain aggregates of listings",
            "7d": "Hourly buckets; 30d/90d daily. Windows slide; bulk price history older than 90d is not documented",
            "sales": "Estimated removals from scans, never confirmed transactions",
        },
        "analysis_readiness": {
            "price_analysis": "Available for saved item/market/time points only",
            "liquidity": "Estimated sales only, subject to scan coverage",
            "cheap_lots": "Current ranked item deals; not historical individual auction lots",
            "professions": "Saved recipes and separate current valuations; cap can truncate professions",
            "historical_backtesting": "Aggregate studies only; no fill/order/transaction simulation",
            "sniping": "Price aggregates cannot reconstruct individual auction announcements",
        },
        "earliest_history_by_market": [],
        "counts": {},
        "coverage": [],
        "gaps": [],
        "unobtained": query_rows_sqlite(
            state,
            "SELECT dataset,market,item_id,status,error FROM jobs WHERE status IN ('FAILED','NOT_AVAILABLE') LIMIT 1000",
        ),
    }
    database = state.root / "analytics" / "ahledger.duckdb"
    if database.exists():
        connection = duckdb.connect(str(database), read_only=True)
        try:
            connection.execute("SET TimeZone='UTC'")
            for table in (
                "markets",
                "items",
                "recipes",
                "price_history",
                "price_history_ranges",
                "price_snapshots",
                "market_economy",
                "market_deals",
            ):
                row = connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()
                assert row is not None
                report["counts"][table] = row[0]
            report["earliest_history_by_market"] = query_rows(
                connection,
                """SELECT m.market,m.market_status,m.label,
                min(p.observed_at) AS earliest,max(p.observed_at) AS latest,count(p.observed_at) AS observations
                FROM markets m LEFT JOIN price_history p ON p.market=m.market
                GROUP BY m.market,m.market_status,m.label ORDER BY CASE WHEN m.market_status='scanned' THEN 0 ELSE 1 END,m.market""",
            )
            report["coverage"] = query_rows(
                connection, "SELECT * FROM coverage ORDER BY market,date,dataset,granularity"
            )
            report["granularities"] = query_rows(
                connection,
                """SELECT market,granularity,resolution_seconds,
                count(*) AS points,min(observed_at) AS earliest,max(observed_at) AS latest
                FROM price_history GROUP BY market,granularity,resolution_seconds ORDER BY market,granularity""",
            )
            report["gaps"] = query_rows(
                connection,
                """SELECT dataset,market,item_id,"range",gaps_json FROM dataset_metadata
                WHERE gaps_json!='[]' ORDER BY market,item_id LIMIT 2000""",
            )
            report["gap_report_limit"] = 2000
            report["warnings"] = query_rows(
                connection,
                "SELECT DISTINCT dataset,warnings_json FROM dataset_metadata WHERE warnings_json!='[]' LIMIT 200",
            )
        finally:
            connection.close()
    else:
        report["analytical_database"] = "NOT_CHECKED"
    output = state.root / "reports"
    atomic_json(output / "download_report.json", report)
    atomic_write(output / "download_report.html", render_report(report).encode("utf-8"))
    return report


def query_rows_sqlite(state, sql):
    return [dict(row) for row in state.db.execute(sql)]


def render_report(report: dict) -> str:
    def esc(value):
        return html.escape(str(value))

    start = date(2026, 9, 17)
    # Report is an archive-time snapshot, independent of a machine's local timezone.
    end = datetime.fromisoformat(report["generated_at"]).astimezone(UTC).date()
    dates = [start + timedelta(days=n) for n in range(max(0, (end - start).days) + 1)]
    coverage = {
        (str(row["market"]), str(row["date"])[:10], row["dataset"], row["granularity"]): row
        for row in report["coverage"]
    }
    markets = report["earliest_history_by_market"]
    scope = report["scope"]
    scope_label = (
        "Ограниченное исследование API"
        if scope == "bounded_research_only"
        else "Ручная выгрузка по сохранённому плану"
    )
    headers = "".join(f"<th>{d:%m-%d}</th>" for d in dates)
    rows = []
    for market in markets:
        for dataset, granularities in (
            ("price_history", ("scan", "hour", "day")),
            ("market_economy", ("day",)),
        ):
            for granularity in granularities:
                cells = []
                for day in dates:
                    row = coverage.get((market["market"], str(day), dataset, granularity))
                    status = "PARTIAL" if row else "NOT_CHECKED"
                    title = dumps(row) if row else status
                    cells.append(
                        f'<td class="{status}" title="{esc(title)}">{row["observations"] if row else "·"}</td>'
                    )
                rows.append(
                    f"<tr><th>{esc(market['market'])}<br><small>{dataset}/{granularity}</small></th>{''.join(cells)}</tr>"
                )
    earliest = "".join(
        f"<tr><td>{esc(r['market'])}</td><td>{esc(r['earliest'])}</td><td>{esc(r['latest'])}</td><td>{r['observations']}</td></tr>"
        for r in markets
    )
    limits = "".join(
        f"<li><b>{esc(k)}</b>: {esc(v)}</li>" for k, v in report["source_limits"].items()
    )
    statuses = "".join(
        f'<span class="legend {s}">{s}</span> '
        for s in ("COMPLETE", "PARTIAL", "NO_DATA", "NOT_AVAILABLE", "FAILED", "NOT_CHECKED")
    )
    return f"""<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AHledger — отчёт исторического архива</title><style>
body{{font:15px system-ui;background:#0f172a;color:#e2e8f0;margin:32px}}a{{color:#7dd3fc}}h1{{font-size:28px}}.cards{{display:flex;gap:18px;flex-wrap:wrap}}.card{{padding:18px;background:#1e293b;border-radius:12px;min-width:140px}}strong{{display:block;font-size:24px}}table{{border-collapse:collapse;margin:20px 0}}th,td{{border:1px solid #334155;padding:6px;text-align:center;white-space:nowrap}}th{{text-align:left}}.matrix{{overflow:auto}}.COMPLETE{{background:#166534}}.PARTIAL{{background:#854d0e}}.NO_DATA{{background:#334155}}.NOT_AVAILABLE{{background:#4c1d95}}.FAILED{{background:#991b1b}}.NOT_CHECKED{{background:#1e293b}}.legend{{display:inline-block;padding:5px 10px}}small{{color:#94a3b8}}pre{{white-space:pre-wrap}}details{{margin:16px 0}}
</style><h1>Исторический архив AHledger</h1><p><a href="https://ahledger.com">Prices from AHledger</a> · {esc(report["generated_at"])} · состояние: <b>{report["download_status"]}</b></p>
<div class="cards"><div class="card">Задания<strong>{report["terminal_jobs"]:,} / {report["total_jobs"]:,}</strong></div><div class="card">Предметы<strong>{report["counts"].get("items", 0):,}</strong></div><div class="card">Рецепты (версии)<strong>{report["counts"].get("recipes", 0):,}</strong></div><div class="card">Точки истории<strong>{report["counts"].get("price_history", 0):,}</strong></div><div class="card">Raw архив<strong>{report["raw_compressed_bytes"] / 1024**2:.2f} MiB</strong></div></div>
<p>Режим: {scope_label}. Завершение очереди не означает непрерывность истории. Данные ограниченного тестового запуска не являются полной выгрузкой.</p>
<h2>Покрытие по рынкам, датам, наборам и детализации</h2>{statuses}
<p>Число в ячейке — сохранённые наблюдения. PARTIAL означает наличие данных без подтверждения полного покрытия всех предметов и временных интервалов. NOT_CHECKED не означает отсутствия данных на сервере. Нерегулярные сканы не имеют гарантированного расписания.</p>
<div class="matrix"><table><tr><th>Рынок / набор / детализация</th>{headers}</tr>{"".join(rows)}</table></div>
<h2>Фактические границы истории цен</h2><table><tr><th>Рынок</th><th>Первая точка</th><th>Последняя точка</th><th>Точек</th></tr>{earliest}</table>
<h2>Ограничения источника</h2><ul>{limits}</ul><p>raw_auction_scans: unavailable. Для запроса архива: <a href="https://ahledger.com/contact">контактная форма AHledger</a>.</p>
<p>Агрегаты цен не заменяют отдельные объявления аукциона. Оценки продаж не подтверждают реальные транзакции. Историческое моделирование снайпинга по этому архиву невозможно.</p>
<details><summary>Ошибки и недоступные данные</summary><pre>{esc(dumps(report["unobtained"]))}</pre></details>
<details><summary>Пропуски между наблюдениями</summary><pre>{esc(dumps(report["gaps"]))}</pre></details>
<details><summary>Состояние наборов</summary><pre>{esc(dumps(report["datasets"]))}</pre></details>
<p>Полная машинная версия: <a href="download_report.json">download_report.json</a>.</p></html>"""
