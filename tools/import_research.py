"""Import already fetched real research responses without any HTTP requests."""

from pathlib import Path

from ahledger.analytics import build_database
from ahledger.cli import registry
from ahledger.http import cache_expiry
from ahledger.models import JobSpec
from ahledger.parsers import parse
from ahledger.report import create_report
from ahledger.storage import RawArchive, State, job_key
from ahledger.util import dumps, loads
from ahledger.verify import verify


def main():
    root = Path(__file__).resolve().parents[1]
    research = root / "research"
    state = State(root / "data")
    archive = RawArchive(state)
    imported = 0
    try:
        for file in sorted(research.glob("*.meta.json")):
            name = file.name.removesuffix(".meta.json")
            original = loads(file.read_bytes())
            if name == "catalog-options":
                continue  # Invalid class returned no class list; retain evidence in research.
            mapping = {
                "markets": "markets",
                "catalog-trade-goods": "catalog",
                "catalog-page": "catalog_index",
                "crafting-options": "professions",
                "pricetable": "pricetable",
                "no-scans": "history",
                "crafting": "crafting",
                "disenchant": "disenchant",
                "economy": "economy",
                "economy-market": "economy_market",
                "structure": "structure",
                "signals": "signals",
                "deals": "deals",
                "item": "item",
                "builds": "builds",
            }
            if name.startswith("prices-"):
                dataset = "history"
            elif name.startswith("crafting-") and name != "crafting-options":
                dataset = "crafting"
            else:
                dataset = mapping.get(name)
            if not dataset:
                continue
            from urllib.parse import parse_qsl, urlsplit, urlunsplit

            url = urlsplit(original["url"])
            params = dict(parse_qsl(url.query))
            market = None
            if dataset in {
                "history",
                "crafting",
                "pricetable",
                "disenchant",
                "structure",
                "signals",
                "deals",
                "economy_market",
            }:
                market = url.path.split("/")[3]
            context = (
                {"class_name": "Trade Goods", "expected_count": 629} if dataset == "catalog" else {}
            )
            spec = JobSpec(
                dataset=dataset,
                url=urlunsplit((url.scheme, url.netloc, url.path, "", "")),
                params=params,
                market=market,
                item_id=2589 if dataset in {"history", "item"} else None,
                context=context,
            )
            state.enqueue(spec)
            row = state.db.execute(
                "SELECT status FROM jobs WHERE id=?", (job_key(spec),)
            ).fetchone()
            if row[0] != "PENDING":
                continue
            body = (research / (name + ".body")).read_bytes()
            parsed = parse(spec, body)
            meta = archive.save(
                spec,
                original["status"],
                body,
                original["headers"],
                retrieved_at=original["retrieved_at"],
            )
            meta.update(
                validation=parsed.status,
                records=parsed.count,
                granularity=parsed.granularity,
                observed_at=parsed.minimum,
                latest_observed_at=parsed.maximum,
                warnings=parsed.warnings,
                gaps=parsed.gaps,
                schema=parsed.schema,
            )
            archive.write_metadata(meta)
            state.finish(job_key(spec), parsed.status, meta["id"], parsed.count)
            state.candidates(parsed.items, catalogue=dataset == "catalog", detail=dataset == "item")
            if dataset == "markets":
                state.set("markets", [r for r in parsed.data["markets"] if r["game"] == "forever"])
            elif dataset == "economy":
                state.set("active_markets", sorted({r["market"] for r in parsed.data["daily"]}))
                state.set(
                    "market_status",
                    {r["market"]: r["marketStatus"] for r in parsed.data["markets"]},
                )
            elif dataset == "catalog_index":
                state.set("catalog_classes", parsed.data)
            elif dataset == "professions":
                state.set("professions", parsed.data["professions"])
            if original["status"] == 200:
                headers = {k.lower(): v for k, v in original["headers"].items()}
                state.cache_response(job_key(spec), meta["id"], headers, cache_expiry(headers))
            imported += 1
        state.db.commit()
        print("Imported real responses:", imported)
        print(dumps(build_database(state)))
        print(dumps(verify(state)))
        registry(state)
        report = create_report(state)
        print(
            dumps(
                {
                    "counts": report["counts"],
                    "earliest": report["earliest_history_by_market"],
                    "raw_compressed_bytes": report["raw_compressed_bytes"],
                }
            )
        )
    finally:
        state.close()


if __name__ == "__main__":
    main()
