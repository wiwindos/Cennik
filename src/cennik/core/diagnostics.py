"""Small run summaries for polling; full source metadata stays in the run payload."""
from collections import Counter
from collections.abc import Iterable

from .models import Run


def empty_metadata_summary() -> dict:
    return {"record_count": 0, "records_with_warnings": 0, "records_with_gaps": 0,
            "gap_count": 0, "status_counts": {}, "dataset_counts": {}}


def metadata_summary(rows: Iterable[dict]) -> dict:
    result = empty_metadata_summary()
    statuses: Counter[str] = Counter()
    datasets: Counter[str] = Counter()
    for row in rows:
        result["record_count"] += 1
        result["records_with_warnings"] += bool(row.get("warnings"))
        result["records_with_gaps"] += bool(row.get("gaps"))
        result["gap_count"] += len(row.get("gaps") or [])
        statuses[row.get("status") or "unknown"] += 1
        datasets[row.get("dataset") or "unknown"] += 1
    result["status_counts"] = dict(statuses)
    result["dataset_counts"] = dict(datasets)
    return result


def summarize_run(run: Run, *, metadata: dict | None = None, signals: dict | None = None) -> Run:
    diagnostics = run.diagnostics
    quality = diagnostics.get("quality", {})
    counts = metadata if metadata is not None else quality.get("metadata_summary")
    if counts is None:
        counts = metadata_summary(quality.get("metadata", []))
    summary: dict = {
        "summary_only": True,
        "performance": diagnostics.get("performance", {}),
        "observed_items": diagnostics.get("observed_items", 0),
        "rejections": diagnostics.get("rejections", {}),
        "strategy_signals": signals if signals is not None else diagnostics.get("strategy_signals", {}),
        "quality": {name: quality[name] for name in (
            "raw_order_book_available", "sales_are_source_estimates", "coverage_is_partial", "recipe_selection"
        ) if name in quality},
    }
    summary["quality"]["metadata_summary"] = counts
    return run.model_copy(update={"diagnostics": summary})
