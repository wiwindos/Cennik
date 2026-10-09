from __future__ import annotations

from typing import Any

from .config import API, RANGES, SITE, Settings
from .models import JobSpec
from .parsers import Parsed
from .storage import State


def bootstrap(state: State):
    for dataset, url, params in (
        ("markets", API + "/v1/markets", {}),
        ("economy", API + "/v1/economy", {}),
        ("catalog_index", SITE + "/wow-forever/items", {}),
        # Documented validation endpoint responds with the accepted profession list.
        (
            "professions",
            API + "/v1/crafting/forever.pvp.horde.us",
            {"profession": "invalid", "all": "1"},
        ),
    ):
        state.enqueue(JobSpec(dataset=dataset, url=url, params=params, priority=-30))
    for name in ("data", "developers", "terms", "changelog"):
        state.enqueue(
            JobSpec(
                dataset="documentation",
                url=SITE + "/" + name,
                priority=20,
                context={"document": name},
            )
        )
    state.enqueue(JobSpec(dataset="llms", url=SITE + "/llms-full.txt", priority=20))
    state.enqueue(JobSpec(dataset="builds", url=SITE + "/feed/builds.xml", priority=10))
    state.db.commit()


def history_jobs(settings: Settings, state: State, item_ids: list[int], referenced: bool):
    active = settings.selected_markets(state.get("active_markets", []))
    for market in active:
        for item_id in item_ids:
            for range_name in RANGES:
                state.enqueue(
                    JobSpec(
                        dataset="history",
                        url=f"{API}/v1/prices/{market}/{item_id}",
                        params={"range": range_name},
                        market=market,
                        item_id=item_id,
                        priority=(0 if referenced else 20)
                        + {"24h": 0, "7d": 1, "90d": 2, "30d": 3}[range_name],
                        context={
                            "history_scope": "referenced" if referenced else "catalogue_check"
                        },
                    )
                )


def plan(settings: Settings, state: State) -> dict[str, Any]:
    before = state.db.total_changes
    markets = state.get("markets", [])
    if markets:
        unknown = set(settings.markets) - {market["id"] for market in markets}
        if unknown:
            raise ValueError("Unknown market IDs: " + ", ".join(sorted(unknown)))
    markets = [
        market for market in markets if not settings.markets or market["id"] in settings.markets
    ]
    for market in markets:
        market_id = market["id"]
        for dataset in ("pricetable", "economy_market"):
            endpoint = "economy" if dataset == "economy_market" else dataset
            state.enqueue(
                JobSpec(
                    dataset=dataset,
                    url=f"{API}/v1/{endpoint}/{market_id}",
                    market=market_id,
                    priority=-20 if dataset == "pricetable" else 10,
                )
            )
        if market_id not in state.get("active_markets", []):
            # One real availability check, never synthetic price rows.
            state.enqueue(
                JobSpec(
                    dataset="history",
                    url=f"{API}/v1/prices/{market_id}/2589",
                    params={"range": "90d"},
                    market=market_id,
                    item_id=2589,
                    priority=10,
                    context={"availability_check": True},
                )
            )
        for profession in state.get("professions", []):
            state.enqueue(
                JobSpec(
                    dataset="crafting",
                    url=f"{API}/v1/crafting/{market_id}",
                    params={"profession": profession, "all": "1"},
                    market=market_id,
                    priority=10,
                )
            )
        if market_id in state.get("active_markets", []):
            for dataset, params in (
                ("disenchant", {"all": "true"}),
                ("structure", {}),
                (
                    "deals",
                    {
                        "discount": "15",
                        "units": "1",
                        "sells": "0",
                        "sort": "saving",
                        "limit": "200",
                    },
                ),
                (
                    "deals",
                    {
                        "discount": "15",
                        "units": "1",
                        "sells": "0",
                        "sort": "discount",
                        "limit": "200",
                    },
                ),
                (
                    "deals",
                    {"discount": "15", "units": "1", "sells": "0", "sort": "sales", "limit": "200"},
                ),
            ):
                state.enqueue(
                    JobSpec(
                        dataset=dataset,
                        url=f"{API}/v1/{dataset}/{market_id}",
                        params=params,
                        market=market_id,
                        priority=20,
                    )
                )
            variants = (
                [(d, n) for d in (10, 20, 30, 40) for n in (3, 7, 14)]
                if settings.signal_variants
                else [(20, 7)]
            )
            for discount, days in variants:
                state.enqueue(
                    JobSpec(
                        dataset="signals",
                        url=f"{API}/v1/signals/{market_id}",
                        params={"discount": str(discount), "days": str(days)},
                        market=market_id,
                        priority=20,
                    )
                )
    for slug, info in state.get("catalog_classes", {}).items():
        state.enqueue(
            JobSpec(
                dataset="catalog",
                url=SITE + "/api/catalog",
                params={"class": slug},
                priority=10,
                context={"class_name": info["name"], "expected_count": info.get("count")},
            )
        )
    candidates = state.db.execute(
        "SELECT * FROM item_candidates ORDER BY referenced DESC,id"
    ).fetchall()
    references = [r["id"] for r in candidates if r["referenced"]]
    history_jobs(settings, state, references, True)
    if settings.full_history_check:
        history_jobs(settings, state, [r["id"] for r in candidates if not r["referenced"]], False)
    if settings.enrich_items != "none":
        for row in candidates:
            if not row["detail"] and (row["referenced"] or settings.enrich_items == "all"):
                state.enqueue(
                    JobSpec(
                        dataset="item",
                        url=f"{API}/v1/items/{row['id']}",
                        params={"game": "forever"},
                        item_id=row["id"],
                        priority=24,
                    )
                )
    state.db.commit()
    return {
        "added": state.db.total_changes - before,
        "jobs": state.counts(markets=settings.markets),
        "items": len(candidates),
        "markets": len(markets),
        "active_markets": settings.selected_markets(state.get("active_markets", [])),
        "selected_markets": settings.markets or "all",
        "estimate_seconds_at_current_limit": sum(
            v
            for k, v in state.counts(markets=settings.markets).items()
            if k in {"PENDING", "RETRY"}
        )
        / settings.requests_per_second,
    }


def absorb(settings: Settings, state: State, spec: JobSpec, parsed: Parsed):
    """Expand only new references during large runs; full plan is done at stage boundaries."""
    if spec.dataset == "markets":
        state.set("markets", [row for row in parsed.data["markets"] if row["game"] == "forever"])
    elif spec.dataset == "economy":
        active = {row["market"] for row in parsed.data["markets"] if row.get("current") is not None}
        active.update(row["market"] for row in parsed.data["daily"])
        state.set("active_markets", sorted(active))
        state.set(
            "market_status", {row["market"]: row["marketStatus"] for row in parsed.data["markets"]}
        )
    elif spec.dataset == "catalog_index":
        state.set("catalog_classes", parsed.data)
    elif spec.dataset == "professions":
        state.set("professions", parsed.data["professions"])
    if spec.dataset in {"markets", "economy", "catalog_index", "professions"}:
        plan(settings, state)
    new = []
    for item_id in parsed.items:
        existing = state.db.execute(
            "SELECT referenced,catalogue FROM item_candidates WHERE id=?", (item_id,)
        ).fetchone()
        reference = spec.dataset != "catalog"
        if existing is None or (reference and not existing["referenced"]):
            new.append(item_id)
    state.candidates(
        parsed.items, catalogue=spec.dataset == "catalog", detail=spec.dataset == "item"
    )
    if parsed.items and spec.dataset != "catalog" or settings.full_history_check:
        history_jobs(settings, state, new, spec.dataset != "catalog")
    if settings.enrich_items != "none" and spec.dataset != "item":
        for item_id in new:
            if spec.dataset != "catalog" or settings.enrich_items == "all":
                state.enqueue(
                    JobSpec(
                        dataset="item",
                        url=f"{API}/v1/items/{item_id}",
                        params={"game": "forever"},
                        item_id=item_id,
                        priority=24,
                    )
                )
    state.db.commit()
