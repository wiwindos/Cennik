"""Run Cennik on an isolated copy of a saved sample; never download data."""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cennik.core.models import AnalyticsConfig, AnalyticsSettings
from cennik.data.duckdb import DuckDBProvider, fingerprint
from cennik.services.analytics import AnalyticsService


def check_sample(source: Path) -> dict:
    before = fingerprint(source)
    with DuckDBProvider(source, "", datetime.now(UTC)) as provider:
        markets = [m for m in provider.markets() if m["observed_at"]]
    if not markets:
        raise ValueError("Sample has no market observations")
    # One active market per check. Select the freshest covered one from the actual DB.
    chosen = max(markets, key=lambda m: m["observed_at"])
    observed = chosen["observed_at"]
    with tempfile.TemporaryDirectory(prefix="cennik-sample-") as temporary:
        root = Path(temporary)
        target = root / "analytics/ahledger.duckdb"
        target.parent.mkdir()
        shutil.copy2(source, target)
        service = AnalyticsService(root)
        service.update_settings(AnalyticsSettings(active_market=chosen["market"], config=AnalyticsConfig()))
        # Evaluate at the archive's observed time, rather than pretending old prices
        # are current. This test time is explicit and is not exposed in the API.
        result = service.start(background=False, as_of=observed + timedelta(seconds=1))["run"]
        # Sample catalog was retrieved later than the snapshot, so assess the normal
        # current-time path too, with an explicit wider freshness threshold.
        historical_result = result
        service.update_settings(AnalyticsSettings(active_market=chosen["market"],
            config=AnalyticsConfig(max_price_age_seconds=31536000)))
        result = service.start(background=False)["run"]
        listing = service.store.listing(chosen["market"], limit=500)
        strategies = {}
        with service.store.connection() as db:
            for row in db.execute("SELECT strategy,count(*) FROM opportunities WHERE run_id=? GROUP BY strategy", [result["run_id"]]):
                strategies[row[0]] = row[1]
        assert result["status"] == "completed", result
        assert service.start(background=False)["reused"]
        assert fingerprint(target) == before
        assert fingerprint(source) == before
        return {"source": str(source.resolve()), "source_unchanged": True, "generation": before,
                "market": chosen["market"], "observed_at": observed.isoformat(),
                "run_id": result["run_id"], "status": result["status"],
                "explicit_freshness_override_seconds": 31536000,
                "historical_time_check_status": historical_result["status"],
                "signal_count": result["signal_count"], "strategies": strategies,
                "observed_items": result["diagnostics"]["observed_items"],
                "rejections": result["diagnostics"]["rejections"],
                "preview": [{k:o[k] for k in ("strategy_id","item_id","potential_profit_copper","roi_percent")}
                            for o in listing["items"][:5]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = check_sample(args.database)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", "utf-8")
    print(text)


if __name__ == "__main__":
    main()
