"""Export public beta reference evidence, never profiles, queues or credentials."""
import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from ahledger.analytics import SCHEMAS
from ahledger.util import atomic_json
from cennik.data.duckdb import DuckDBProvider
from cennik.engines.disenchant import DisenchantRules
from cennik.engines.npc_catalog import NpcPriceDocument
from cennik.workspace.bootstrap import BootstrapDocument

ROOT = Path(__file__).resolve().parents[1]


def export(output, databases, market="forever.pvp.alliance.us"):
    items, recipes, prices, facts, references = {}, {}, {}, {}, {}
    conflicts = set()
    provenance = []
    for database in databases:
        with DuckDBProvider(database, market, datetime.now(UTC)) as provider:
            source_items = provider.items()
            for identity, item in source_items.items():
                previous = items.get(identity)
                if previous is None or item.observed_at and (not previous.observed_at or item.observed_at > previous.observed_at):
                    items[identity] = item
            for row in provider.rows("SELECT " + ",".join(SCHEMAS["items"]) + " FROM items WHERE origin_dataset<>'manual'"):
                facts[row["item_id"]] = row
            for recipe in provider.recipes():
                if not recipe.result_item_id or not recipe.qty_min or not recipe.qty_max or not recipe.inputs:
                    continue
                previous = recipes.get(recipe.spell_id)
                stamp = recipe.source_generated_at or recipe.source_retrieved_at
                old_stamp = previous.source_generated_at or previous.source_retrieved_at if previous else None
                if previous and stamp == old_stamp and previous.version_hash != recipe.version_hash:
                    conflicts.add(recipe.spell_id)
                elif previous is None or stamp and (old_stamp is None or stamp > old_stamp):
                    recipes[recipe.spell_id] = recipe
                    conflicts.discard(recipe.spell_id)
            for identity, price in provider.current_prices().items():
                if not price.min_copper or price.quantity == 0:
                    continue
                previous = prices.get(identity)
                if previous is None or price.observed_at > previous.observed_at:
                    prices[identity] = price
            for row in provider.rows("SELECT item_id,median30d_copper copper FROM price_snapshots WHERE market=? AND median30d_copper>0 QUALIFY row_number() OVER(PARTITION BY item_id ORDER BY observed_at DESC,retrieved_at DESC)=1", [market]):
                references[row["item_id"]] = row["copper"]
            provenance.append({"source": database.name, "items": len(source_items)})
    for identity in conflicts:
        recipes.pop(identity, None)
    now = datetime.now(UTC)
    document = BootstrapDocument(version="forever-beta-" + now.strftime("%Y%m%dT%H%M%SZ"), created_at=now,
        source_market=market, items=items, recipes=list(recipes.values()), prices=prices, references=references,
        item_facts=facts, provenance={"databases": provenance, "ambiguous_recipes_excluded": sorted(conflicts),
            "note": "Локальная бета US: явный стартовый ориентир для EU, без тестовых очередей и привязок"})
    output.mkdir(parents=True, exist_ok=True)
    (output / "baseline.json").write_text(document.model_dump_json(), "utf-8")
    for name, model in [("npc_catalog", NpcPriceDocument), ("disenchant", DisenchantRules)]:
        data = (ROOT / "src/cennik/reference" / (name + ".json")).read_bytes()
        model.model_validate_json(data)
        (output / (name + ".json")).write_bytes(data)
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in output.glob("*.json") if p.name != "manifest.json"}
    manifest = {"format_version": 1, "version": document.version, "phase": "beta", "source_market": market,
        "created_at": now.isoformat(), "items": len(items), "recipes": len(recipes),
        "components": len({i.item_id for r in recipes.values() for i in r.inputs}), "prices": len(prices), "files": files}
    atomic_json(output / "manifest.json", manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = export(args.output, [ROOT / "data/analytics/ahledger.duckdb", ROOT / "data/cennik-next/market.duckdb"])
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
