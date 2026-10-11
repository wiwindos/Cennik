"""Offline memory check: build a copied raw archive or deduplicate a wide synthetic table."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

from ahledger.analytics import analytics_connection, build_database, deduplicate
from ahledger.config import Settings
from ahledger.storage import State


def peak_rss_bytes() -> int:
    if sys.platform == "win32":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                *[
                    (name, ctypes.c_size_t)
                    for name in (
                        "PeakWorkingSetSize",
                        "WorkingSetSize",
                        "QuotaPeakPagedPoolUsage",
                        "QuotaPagedPoolUsage",
                        "QuotaPeakNonPagedPoolUsage",
                        "QuotaNonPagedPoolUsage",
                        "PagefileUsage",
                        "PeakPagefileUsage",
                    )
                ],
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return counters.PeakWorkingSetSize
    import resource

    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


def file_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check_archive(source: Path, root: Path, settings: Settings) -> dict:
    source = source.resolve()
    database = source / "analytics/ahledger.duckdb"
    before = file_hash(database)
    shutil.copytree(source / "raw", root / "raw")
    # Back up SQLite through its read-only API so an existing WAL is included.
    with closing(
        sqlite3.connect((source / "state.sqlite").as_uri() + "?mode=ro", uri=True)
    ) as original:
        with closing(sqlite3.connect(root / "state.sqlite")) as target:
            original.backup(target)
    state = State(root)
    try:
        result = build_database(state, settings=settings)
    finally:
        state.close()
    if before is not None:
        with analytics_connection(database, settings, read_only=True) as db:
            original_counts = {
                table: db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                for table in result["counts"]
            }
        assert original_counts == result["counts"], "Rebuild counts differ from the source database"
    assert file_hash(database) == before, "Source database changed"
    return {
        "mode": "copied_archive",
        "source": str(source),
        "source_db_unchanged": True,
        "build": result,
    }


def check_wide_rows(root: Path, settings: Settings, rows: int) -> dict:
    database = root / "stress.duckdb"
    profile = root / "query-profile.json"
    with analytics_connection(database, settings) as db:
        # 1 KiB per payload, four revisions per key; no Python list of rows.
        db.execute(
            """CREATE TABLE wide_rows AS SELECT (i//4)::BIGINT AS item_id,
            i::BIGINT AS revision,repeat(md5(i::VARCHAR),32) AS payload_json FROM range(?) r(i)""",
            [rows],
        )
        db.execute("CHECKPOINT")
        db.execute("SET enable_profiling='json'")
        escaped = profile.as_posix().replace("'", "''")
        db.execute(f"SET profiling_output='{escaped}'")
        deduplicate(db, "wide_rows", "latest_rows", "item_id", "revision DESC")
        # The profile exposes DuckDB's recorded peak buffer/spill use.
        query_profile = json.loads(profile.read_text("utf-8"))
        db.execute("SET enable_profiling='no_output'")
        count, incorrect = db.execute("""SELECT count(*),count(*) FILTER (
            WHERE revision%4!=3 OR payload_json!=repeat(md5(revision::VARCHAR),32))
            FROM latest_rows""").fetchone()
        assert count == rows // 4 and incorrect == 0
    return {
        "mode": "wide_row_deduplication",
        "input_rows": rows,
        "output_rows": count,
        "logical_payload_bytes": rows * 1024,
        "duckdb_peak_buffer_bytes": query_profile["system_peak_buffer_memory"],
        "duckdb_peak_spill_bytes": query_profile["system_peak_temp_dir_size"],
        "database_bytes": database.stat().st_size,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--source", type=Path, help="Copy this archive; never rebuild the source")
    modes.add_argument("--stress-rows", type=int, help="Synthetic row count, divisible by 4")
    parser.add_argument("--memory-mb", type=int, default=512)
    parser.add_argument("--work-dir", type=Path, default=Path("data/memory-checks"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.stress_rows is not None and (args.stress_rows < 4 or args.stress_rows % 4):
        parser.error("--stress-rows must be positive and divisible by 4")
    settings = Settings(analytics_memory_mb=args.memory_mb)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="analytics-memory-", dir=args.work_dir) as directory:
        root = Path(directory)
        result = (
            check_archive(args.source, root, settings)
            if args.source
            else check_wide_rows(
                root,
                settings,
                args.stress_rows,
            )
        )
    result.update(
        memory_limit_mb=args.memory_mb,
        threads=1,
        peak_process_rss_bytes=peak_rss_bytes(),
        elapsed_seconds=round(time.monotonic() - started, 2),
        platform=sys.platform,
    )
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", "utf-8")
    print(text)


if __name__ == "__main__":
    main()
