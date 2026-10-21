"""Offline comparison on disposable copies of a downloaded DB and cached batch."""

import argparse
import json
import shutil
import tempfile
import time
from pathlib import Path

from ahledger.analytics import SCHEMAS, analytics_connection, build_database
from ahledger.config import Settings
from ahledger.storage import State
from ahledger.update import batch_root, merge_update
from ahledger.util import atomic_json
from ahledger.verify import verify
from cennik.data.duckdb import fingerprint


def signatures(path, settings):
    with analytics_connection(path, settings, read_only=True) as db:
        result = {}
        for table in [*SCHEMAS, "price_history", "coverage"]:
            columns = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")').fetchall()]
            projection = ",".join('"' + c + '"' for c in columns)
            result[table] = db.execute(f'SELECT count(*),sum(hash({projection})) FROM "{table}"').fetchone()
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    before = fingerprint(args.source)
    with tempfile.TemporaryDirectory(prefix="update-benchmark-", dir=project / "data") as temporary:
        root = Path(temporary).resolve()
        assert root.is_relative_to(project.resolve())
        legacy, incremental = root / "legacy", root / "incremental"
        identity = args.batch.name
        shutil.copytree(args.batch, legacy, ignore=shutil.ignore_patterns("analytics", "parquet", "result.json"))
        shutil.copytree(legacy, batch_root(incremental, identity))
        destination = incremental / "analytics/ahledger.duckdb"
        destination.parent.mkdir(parents=True)
        shutil.copyfile(args.source, destination)
        stage = State(legacy)
        try:
            market = stage.get("target_market")
            settings = Settings(data_dir=incremental, markets=[market])
            start = time.monotonic()
            checked = verify(stage, settings=settings, check_analytics=False)
            assert checked["integrity"] == "COMPLETE"
            build_database(stage, settings=settings, base_database=args.source, allowed_market=market)
            checked = verify(stage, settings=settings)
            assert checked["integrity"] == "COMPLETE"
            full_seconds = time.monotonic() - start
            print(f"Full merge + checks + Parquet: {full_seconds:.2f}s", flush=True)
        finally:
            stage.close()
        start = time.monotonic()
        result = merge_update(settings, identity)
        incremental_seconds = time.monotonic() - start
        print(f"Incremental merge + checks: {incremental_seconds:.2f}s", flush=True)
        full_signature = signatures(legacy / "analytics/ahledger.duckdb", settings)
        incremental_signature = signatures(destination, settings)
        assert full_signature == incremental_signature, [t for t in full_signature if full_signature[t] != incremental_signature[t]]
        assert fingerprint(args.source) == before
        report = {"full_seconds": round(full_seconds, 3),
                  "incremental_seconds": round(incremental_seconds, 3),
                  "speedup": round(full_seconds / incremental_seconds, 2),
                  "rows_and_row_checksums_match": True, "source_unchanged": True,
                  "network_requests": 0, "touched_tables": result["touched_tables"]}
        atomic_json(args.output, report)
        print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
