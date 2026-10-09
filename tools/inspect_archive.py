"""Read real saved data through DuckDB; no network."""

from pathlib import Path

import duckdb

root = Path(__file__).resolve().parents[1]
connection = duckdb.connect(str(root / "data/analytics/ahledger.duckdb"), read_only=True)
try:
    connection.execute("SET TimeZone='UTC'")
    rows = connection.execute("""
        SELECT market,granularity,min(observed_at),max(observed_at),count(*)
        FROM price_history GROUP BY market,granularity ORDER BY market,granularity
    """).fetchall()
    for row in rows:
        print(row)
    print(
        "First actual observation:",
        connection.execute("""
        SELECT market,item_id,observed_at,median_copper,quantity FROM price_history
        ORDER BY observed_at,market LIMIT 1
    """).fetchone(),
    )
finally:
    connection.close()

print(
    "Total data directory bytes:",
    sum(p.stat().st_size for p in (root / "data").rglob("*") if p.is_file()),
)
