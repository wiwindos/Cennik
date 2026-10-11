from __future__ import annotations

from .analytics import analytics_connection
from .config import Settings
from .models import JobSpec
from .parsers import parse
from .storage import TERMINAL, RawArchive, State
from .util import atomic_json, utc_now


def verify(state: State, *, settings: Settings | None = None) -> dict:
    settings = settings or Settings.read(root=state.root)
    archive = RawArchive(state)
    errors = []
    rejected_responses = []
    checked = 0
    for meta in archive.manifests(ordered=False):
        try:
            original_size = len(archive.read_sha(meta["sha256"]))
            if original_size != meta["size"]:
                raise ValueError("Original response size differs from manifest")
            checked += 1
            spec = JobSpec.model_validate(meta["spec"])
            if meta["validation"] == "FAILED":
                rejected_responses.append(
                    {
                        "raw_id": meta["id"],
                        "http_status": meta["http_status"],
                        "error": meta.get("error"),
                    }
                )
                continue
            if (
                meta["http_status"] in {200, 304}
                or spec.dataset == "professions"
                and meta["http_status"] == 400
            ):
                record_count = parse(spec, archive.read(meta)).count
                if "records" in meta and record_count != meta["records"]:
                    raise ValueError("Manifest record count differs from reparsed raw")
            if (
                meta["validation"] in TERMINAL
                and meta["validation"] not in {"NOT_AVAILABLE"}
                and meta["http_status"] == 0
            ):
                raise ValueError("Network failure marked terminal")
        except (ValueError, KeyError, OSError, TypeError) as exc:
            errors.append({"raw_id": meta["id"], "error": str(exc)})
    for row in state.db.execute(
        "SELECT id,status,raw_id FROM jobs WHERE status IN ('COMPLETE','PARTIAL','NO_DATA','NOT_AVAILABLE')"
    ):
        if (
            not row["raw_id"]
            or not (archive.root / "metadata" / (row["raw_id"] + ".json")).exists()
        ):
            errors.append({"job_id": row["id"], "error": "Terminal job has no manifest"})
    integrity = state.db.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        errors.append({"error": "SQLite integrity: " + integrity})
    db = state.root / "analytics" / "ahledger.duckdb"
    analytics = "NOT_CHECKED"
    if db.exists():
        connection = analytics_connection(db, settings, read_only=True)
        try:
            checks = {
                "duplicate price observations": "SELECT count(*) FROM (SELECT market,item_id,observed_at,granularity,count(*) n FROM price_history GROUP BY ALL HAVING n>1)",
                "price item foreign keys": "SELECT count(*) FROM price_history p LEFT JOIN items i USING(item_id) WHERE i.item_id IS NULL",
                "price market foreign keys": "SELECT count(*) FROM price_history p LEFT JOIN markets m USING(market) WHERE m.market IS NULL",
                "recipe input item foreign keys": "SELECT count(*) FROM recipe_inputs r LEFT JOIN items i USING(item_id) WHERE i.item_id IS NULL",
                "recipe result item foreign keys": "SELECT count(*) FROM recipes r LEFT JOIN items i ON r.result_item_id=i.item_id WHERE r.result_item_id IS NOT NULL AND i.item_id IS NULL",
                "invalid money/quantity": "SELECT count(*) FROM price_history WHERE min_copper<0 OR median_copper<0 OR quantity<0 OR auctions<0 OR currency!='copper' OR observed_at IS NULL",
            }
            for name, sql in checks.items():
                row = connection.execute(sql).fetchone()
                assert row is not None
                count = row[0]
                if count:
                    errors.append({"check": name, "violations": count})
            analytics = "FAILED" if errors else "COMPLETE"
        finally:
            connection.close()
    pending = sum(
        value
        for status, value in state.counts().items()
        if status in {"PENDING", "RUNNING", "RETRY", "FAILED"}
    )
    result = {
        "verified_at": utc_now(),
        "integrity": "FAILED" if errors else "COMPLETE",
        "raw_manifests_checked": checked,
        "analytics": analytics,
        "coverage": "PARTIAL"
        if pending or not state.get("full_plan_created", False)
        else "CHECK_REPORT",
        "pending_or_failed_jobs": pending,
        "errors": errors,
        "rejected_responses": rejected_responses,
    }
    atomic_json(state.root / "reports" / "verification.json", result)
    return result
