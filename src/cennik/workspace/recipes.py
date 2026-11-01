"""Complete observed recipes; partial profession windows never erase known data."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from ahledger.util import atomic_json, dumps
from cennik.core.models import Recipe


class LocalRecipes:
    def __init__(self, root, market):
        self.path = root / "references/observed_recipes.json"
        self.market = market

    def document(self):
        return json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}

    def generation(self):
        return hashlib.sha256(self.path.read_bytes()).hexdigest() if self.path.exists() else "empty"

    def ingest(self, report):
        if (
            not isinstance(report, dict)
            or not isinstance(report.get("recipes", {}), dict)
            or len(report.get("recipes", {})) > 5000
        ):
            raise ValueError("Некорректный список игровых рецептов")
        known = self.document()
        changed = skipped = 0
        for value in report.get("recipes", {}).values():
            try:
                if not isinstance(value, dict):
                    raise ValueError("Неверный рецепт")
                stamp = value.get("observed_at", report.get("observed_at"))
                if (
                    type(stamp) is not int
                    or not 0 < stamp <= int(datetime.now(UTC).timestamp()) + 300
                ):
                    raise ValueError("Неверная дата рецепта")
                fields = {
                    "spell_id": value["spell_id"],
                    "result_item_id": value["item_id"],
                    "qty_min": value["qty_min"],
                    "qty_max": value["qty_max"],
                    "inputs": [
                        {"item_id": x["item_id"], "quantity": x["quantity"]}
                        for x in value["inputs"]
                    ],
                }
                if (
                    any(
                        type(fields[k]) is not int or fields[k] <= 0
                        for k in ("spell_id", "result_item_id", "qty_min", "qty_max")
                    )
                    or fields["qty_max"] < fields["qty_min"]
                ):
                    raise ValueError("Неверный выход рецепта")
                if (
                    not fields["inputs"]
                    or len(fields["inputs"]) > 100
                    or any(
                        type(x[k]) is not int or not 0 < x[k] <= 1000000
                        for x in fields["inputs"]
                        for k in ("item_id", "quantity")
                    )
                ):
                    raise ValueError("Неверные реагенты")
                digest = hashlib.sha256(dumps(fields).encode()).hexdigest()
                recipe = Recipe(
                    **fields,
                    name=value.get("name"),
                    profession=value.get("profession"),
                    version_hash=digest,
                    source_market=self.market,
                    raw_id="observed-recipe-" + digest,
                    source_retrieved_at=datetime.fromtimestamp(stamp, UTC),
                )
                key = str(recipe.spell_id)
                previous = known.get(key)
                if previous and previous["observed_at"] > stamp:
                    continue
                if not previous or previous["recipe"]["version_hash"] != digest:
                    known[key] = {"observed_at": stamp, "recipe": recipe.model_dump(mode="json")}
                    changed += 1
            except (ValueError, TypeError, KeyError):
                skipped += 1
        if changed:
            if len(known) > 10000:
                raise ValueError("Слишком много игровых рецептов")
            atomic_json(self.path, known)
        return {"updated": changed, "incomplete": skipped, "total": len(known)}

    def recipes(self, as_of):
        return [
            Recipe.model_validate(row["recipe"])
            for row in self.document().values()
            if row["observed_at"] <= as_of.timestamp()
        ]
