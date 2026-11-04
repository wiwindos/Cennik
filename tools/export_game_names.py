"""Export only verified English AHL names; never changes the source database or beta prices."""
import hashlib
import json
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = ROOT / "data/analytics/ahledger.duckdb"
    names = {"item": {}, "recipe": {}}
    with duckdb.connect(str(source), read_only=True, config={"memory_limit": "512MB", "threads": "1"}) as db:
        for identity, name in db.execute("SELECT item_id,name FROM items WHERE origin_dataset IN ('item','catalog','crafting','pricetable','disenchant') AND name IS NOT NULL").fetchall():
            names["item"][str(identity)] = name
        for identity, name in db.execute("SELECT spell_id,name FROM recipes WHERE name IS NOT NULL QUALIFY row_number() OVER(PARTITION BY spell_id ORDER BY retrieved_at DESC)=1").fetchall():
            names["recipe"][str(identity)] = name
    if any(any("\u0400" <= c <= "\u04ff" for c in name) for values in names.values() for name in values.values()):
        raise ValueError("Mixed-language source; review English evidence before export")
    version = hashlib.sha256(json.dumps(names, sort_keys=True).encode()).hexdigest()
    document = {"version": version, "locale": "enUS", "source": "Saved AHL beta API responses", "names": names}
    target = ROOT / "src/cennik/reference/game_names.json"
    target.write_text(json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n", "utf-8")
    print(json.dumps({"file": str(target), "items": len(names["item"]), "recipes": len(names["recipe"]), "bytes": target.stat().st_size}))


if __name__ == "__main__":
    main()
