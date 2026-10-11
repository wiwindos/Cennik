from __future__ import annotations

import gzip
import sqlite3
import time
import uuid
import zlib
from pathlib import Path
from typing import Any

import zstandard as zstd

from .config import PARSER_VERSION
from .models import JobSpec
from .util import atomic_json, atomic_write, digest, dumps, loads, utc_now

TERMINAL = ("COMPLETE", "PARTIAL", "NO_DATA", "NOT_AVAILABLE")
SAFE_HEADERS = {
    "etag",
    "last-modified",
    "cache-control",
    "age",
    "date",
    "expires",
    "content-type",
    "content-length",
    "content-encoding",
    "retry-after",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
}


def job_key(spec: JobSpec) -> str:
    return digest(dumps({"dataset": spec.dataset, "url": spec.url, "params": spec.params}).encode())


def job_scope(datasets: list[str] | None = None, markets: list[str] | None = None):
    clauses = []
    parameters: list[Any] = []
    if datasets:
        clauses.append(f"dataset IN ({','.join('?' for _ in datasets)})")
        parameters.extend(datasets)
    if markets:
        clauses.append(f"(market IS NULL OR market IN ({','.join('?' for _ in markets)}))")
        parameters.extend(markets)
    return " AND ".join(clauses) or "1", parameters


class State:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "state.sqlite", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, spec TEXT NOT NULL, dataset TEXT NOT NULL, market TEXT,
            item_id INTEGER, priority INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
            attempts INTEGER NOT NULL DEFAULT 0, not_before REAL NOT NULL DEFAULT 0,
            raw_id TEXT, records INTEGER NOT NULL DEFAULT 0, error TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS queue ON jobs(status, priority, not_before);
        CREATE INDEX IF NOT EXISTS queue_claim ON jobs(priority,created_at,id)
            WHERE status IN ('PENDING','RETRY');
        CREATE INDEX IF NOT EXISTS queue_market_claim ON jobs(market,priority,created_at,id)
            WHERE status IN ('PENDING','RETRY');
        CREATE INDEX IF NOT EXISTS job_items ON jobs(item_id);
        CREATE TABLE IF NOT EXISTS responses (
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL, sha TEXT NOT NULL,
            status INTEGER NOT NULL, metadata TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS response_jobs ON responses(job_id);
        CREATE TABLE IF NOT EXISTS cache (job_id TEXT PRIMARY KEY, raw_id TEXT NOT NULL,
            etag TEXT, last_modified TEXT, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS item_candidates (
            id INTEGER PRIMARY KEY, referenced INTEGER NOT NULL DEFAULT 0,
            catalogue INTEGER NOT NULL DEFAULT 0, detail INTEGER NOT NULL DEFAULT 0,
            payload TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return loads(row[0]) if row else default

    def set(self, key: str, value: Any):
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, dumps(value)))
        self.db.commit()

    def enqueue(self, spec: JobSpec) -> bool:
        now = utc_now()
        result = self.db.execute(
            """INSERT OR IGNORE INTO jobs
            (id,spec,dataset,market,item_id,priority,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (
                job_key(spec),
                spec.model_dump_json(),
                spec.dataset,
                spec.market,
                spec.item_id,
                spec.priority,
                now,
                now,
            ),
        )
        return result.rowcount == 1

    def claim(
        self, datasets: list[str] | None = None, markets: list[str] | None = None
    ) -> sqlite3.Row | None:
        scope, parameters = job_scope(datasets)
        parameters.insert(0, time.time())
        predicate = "status IN ('PENDING','RETRY') AND not_before <= ? AND " + scope
        with self.db:
            if markets:
                # Fetch one indexed head per market and the shared queue, rather than
                # repeatedly walking thousands of earlier jobs for excluded markets.
                heads = []
                for market in [None, *dict.fromkeys(markets)]:
                    market_clause = "market IS NULL" if market is None else "market=?"
                    scoped_parameters = parameters if market is None else [*parameters, market]
                    head = self.db.execute(
                        f"SELECT * FROM jobs INDEXED BY queue_market_claim WHERE {predicate} AND {market_clause} ORDER BY priority,created_at,id LIMIT 1",
                        scoped_parameters,
                    ).fetchone()
                    if head is not None:
                        heads.append(head)
                row = min(
                    heads,
                    key=lambda head: (head["priority"], head["created_at"], head["id"]),
                    default=None,
                )
            else:
                row = self.db.execute(
                    f"SELECT * FROM jobs INDEXED BY queue_claim WHERE {predicate} ORDER BY priority,created_at,id LIMIT 1",
                    parameters,
                ).fetchone()
            if row:
                self.db.execute(
                    "UPDATE jobs SET status='RUNNING',attempts=attempts+1,updated_at=? WHERE id=?",
                    (utc_now(), row["id"]),
                )
        return row

    def finish(
        self,
        key: str,
        status: str,
        raw_id: str | None = None,
        records: int = 0,
        error: str | None = None,
        not_before: float = 0,
    ):
        with self.db:
            self.db.execute(
                """UPDATE jobs SET status=?,raw_id=COALESCE(?,raw_id),records=?,
                error=?,not_before=?,updated_at=? WHERE id=?""",
                (status, raw_id, records, error, not_before, utc_now(), key),
            )

    def recover(self, retry_failed: bool = False):
        with self.db:
            self.db.execute("UPDATE jobs SET status='PENDING' WHERE status='RUNNING'")
            if retry_failed:
                self.db.execute(
                    "UPDATE jobs SET status='PENDING',attempts=0,not_before=0 WHERE status='FAILED'"
                )

    def refresh(self):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET status='PENDING',attempts=0 WHERE status IN ('COMPLETE','PARTIAL','NO_DATA','NOT_AVAILABLE')"
            )

    def refresh_discovery(self, datasets: list[str], cutoff: str | None):
        predicate = f"dataset IN ({','.join('?' for _ in datasets)}) AND status IN ('COMPLETE','PARTIAL','NO_DATA','NOT_AVAILABLE')"
        parameters: list[Any] = list(datasets)
        if cutoff:
            predicate += " AND updated_at < ?"
            parameters.append(cutoff)
        with self.db:
            self.db.execute(
                f"UPDATE jobs SET status='PENDING',attempts=0 WHERE {predicate}", parameters
            )

    def counts(
        self, *, datasets: list[str] | None = None, markets: list[str] | None = None
    ) -> dict[str, int]:
        predicate, parameters = job_scope(datasets, markets)
        return dict(
            self.db.execute(
                f"SELECT status,count(*) FROM jobs WHERE {predicate} GROUP BY status", parameters
            ).fetchall()
        )

    def candidates(self, values: dict[int, dict[str, Any]], *, catalogue=False, detail=False):
        for item_id, payload in values.items():
            self.db.execute(
                """INSERT INTO item_candidates VALUES (?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET referenced=max(referenced,excluded.referenced),
                catalogue=max(catalogue,excluded.catalogue),detail=max(detail,excluded.detail),
                payload=CASE WHEN excluded.catalogue OR excluded.detail THEN excluded.payload ELSE payload END""",
                (item_id, int(not catalogue), int(catalogue), int(detail), dumps(payload)),
            )

    def cache_entry(self, key: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM cache WHERE job_id=?", (key,)).fetchone()
        return dict(row) if row else None

    def cache_response(self, key: str, raw_id: str, headers: dict[str, str], expires: float):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?)",
                (key, raw_id, headers.get("etag"), headers.get("last-modified"), expires),
            )


class RawArchive:
    def __init__(self, state: State):
        self.state = state
        self.root = state.root / "raw"

    def save(
        self,
        spec: JobSpec,
        status: int,
        body: bytes,
        headers: dict[str, str],
        *,
        retrieved_at: str | None = None,
        reused_sha: str | None = None,
    ) -> dict:
        sha = digest(body)
        blob = self.root / "blobs" / sha[:2] / (sha + ".zst")
        old_size = blob.stat().st_size if blob.exists() else 0
        intact = False
        if blob.exists():
            try:
                intact = digest(zstd.ZstdDecompressor().decompress(blob.read_bytes())) == sha
            except (OSError, zstd.ZstdError):
                pass
        if not intact:
            atomic_write(blob, zstd.ZstdCompressor(level=8).compress(body))
        raw_id = uuid.uuid4().hex
        meta = {
            "id": raw_id,
            "job_id": job_key(spec),
            "spec": spec.model_dump(mode="json"),
            "url": spec.url,
            "params": spec.params,
            "endpoint": spec.dataset,
            "market": spec.market,
            "http_status": status,
            "retrieved_at": retrieved_at or utc_now(),
            "sha256": sha,
            "size": len(body),
            "compressed_size": blob.stat().st_size,
            "headers": {k.lower(): v for k, v in headers.items() if k.lower() in SAFE_HEADERS},
            "parser_version": PARSER_VERSION,
            "validation": "NOT_CHECKED",
            "reused_sha256": reused_sha,
        }
        self.write_metadata(meta)
        self.state.set(
            "compressed_bytes",
            self.state.get("compressed_bytes", 0) + blob.stat().st_size - old_size,
        )
        return meta

    def write_metadata(self, meta: dict):
        atomic_json(self.root / "metadata" / (meta["id"] + ".json"), meta)
        self.state.db.execute(
            "INSERT OR REPLACE INTO responses VALUES (?,?,?,?,?)",
            (meta["id"], meta["job_id"], meta["sha256"], meta["http_status"], dumps(meta)),
        )
        self.state.db.commit()

    def metadata(self, raw_id: str) -> dict:
        return loads((self.root / "metadata" / (raw_id + ".json")).read_bytes())

    def read_sha(self, sha: str) -> bytes:
        path = self.root / "blobs" / sha[:2] / (sha + ".zst")
        try:
            body = zstd.ZstdDecompressor().decompress(path.read_bytes())
        except zstd.ZstdError as exc:
            raise ValueError(f"Corrupt Zstandard blob: {sha}") from exc
        if digest(body) != sha:
            raise ValueError(f"Raw SHA-256 mismatch: {sha}")
        return body

    def read(self, meta: dict) -> bytes:
        if meta.get("reused_raw_id"):
            return self.read(self.metadata(meta["reused_raw_id"]))
        body = self.read_sha(meta.get("reused_sha256") or meta["sha256"])
        encoding = meta.get(
            "body_content_encoding", meta["headers"].get("content-encoding", "identity")
        )
        try:
            if encoding == "gzip":
                return gzip.decompress(body)
            if encoding == "deflate":
                return zlib.decompress(body)
            if encoding not in {"identity", "", None}:
                raise ValueError(f"Unsupported HTTP content encoding: {encoding}")
        except (OSError, zlib.error) as exc:
            raise ValueError("Corrupt HTTP compressed body") from exc
        return body

    def manifests(self, *, ordered: bool = True):
        # Metadata is authoritative; this also works when SQLite has been lost.
        paths = (self.root / "metadata").glob("*.json")
        for path in sorted(paths) if ordered else paths:
            yield loads(path.read_bytes())

    def restore_state(self):
        from .parsers import parse

        metas = sorted(self.manifests(), key=lambda m: (m["retrieved_at"], m["id"]))
        active = set(self.state.get("active_markets", []))
        for meta in metas:
            spec = JobSpec.model_validate(meta["spec"])
            self.state.enqueue(spec)
            self.write_metadata(meta)
            if meta["validation"] in TERMINAL:
                self.state.finish(
                    meta["job_id"], meta["validation"], meta["id"], meta.get("records", 0)
                )
                if meta["validation"] != "NOT_AVAILABLE":
                    parsed = parse(spec, self.read(meta))
                    self.state.candidates(
                        parsed.items,
                        catalogue=spec.dataset == "catalog",
                        detail=spec.dataset == "item",
                    )
                    if spec.dataset == "markets":
                        self.state.set(
                            "markets", [r for r in parsed.data["markets"] if r["game"] == "forever"]
                        )
                    elif spec.dataset == "economy":
                        active.update(r["market"] for r in parsed.data["daily"])
                        self.state.set(
                            "market_status",
                            {r["market"]: r["marketStatus"] for r in parsed.data["markets"]},
                        )
                    elif spec.dataset == "history" and parsed.count and spec.market:
                        active.add(spec.market)
                    elif spec.dataset == "catalog_index":
                        self.state.set("catalog_classes", parsed.data)
                    elif spec.dataset == "professions":
                        self.state.set("professions", parsed.data["professions"])
        self.state.set("active_markets", sorted(active))
        self.state.db.commit()
