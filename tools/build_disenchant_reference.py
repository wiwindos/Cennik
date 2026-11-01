"""Compile the separate research data export into a portable rules document.

Only numeric data and item metadata are transferred; no addon code is copied.
Run export_rules.py in research first to reproduce/validate its inputs.
"""
import json
from pathlib import Path

from cennik.engines.disenchant import BUNDLED, DisenchantRules

root = Path(__file__).resolve().parents[1]
data = json.loads((root / "research/disenchant-2026-10-24/modeled_yields.json").read_text("utf-8"))
pools, keys, items, exclusions = {}, {}, {}, {}
for row in sorted(data["items"], key=lambda r: r["item_id"]):
    key = json.dumps(row["yields"], sort_keys=True)
    if key not in keys:
        name = "pool_" + str(len(keys) + 1)
        keys[key], pools[name] = name, row["yields"]
    items[row["item_id"]] = {"name": row["name"], "pool": keys[key], "item_level": row["item_level"],
                             "quality": row["quality"], "item_class": row["item_class"]}
    if row["warning"]:
        exclusions[row["item_id"]] = "Приблизительный выход эпического предмета ниже item level 40"
document = DisenchantRules(version="ahledger-0.9.6-20261024", source="AHledger 0.9.6 model + item catalog 2026-10-24",
    source_url=data["source_url"], checked_at="2026-10-24T11:40:00Z", pools=pools, items=items, exclusions=exclusions)
BUNDLED.parent.mkdir(parents=True, exist_ok=True)
BUNDLED.write_text(document.model_dump_json() + "\n", encoding="utf-8")
print(document.metadata())
