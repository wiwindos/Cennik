"""Rebuild a whole selected batch from the frozen recipe catalogue and live depth."""

from cennik.core.models import Opportunity, Recipe
from cennik.engines.craft_graph import MODEL, CraftGraph, MaterialOffer, SearchLimit, wire_steps


def auction_items(opportunity: Opportunity) -> set[int]:
    catalog = opportunity.details.get("graph_catalog") or {}
    return set(catalog.get("auction_items", [])) | {
        r["item_id"] for r in catalog.get("offers", []) if r["source"] == "auction"
    }


def recalculate(opportunity: Opportunity, batches: int, auctions: dict[int, MaterialOffer]) -> dict:
    catalog = opportunity.details.get("graph_catalog") or {}
    if catalog.get("model") != MODEL:
        raise ValueError(
            "Пересчитайте возможности: для цепочки нужен новый расчёт общих компонентов"
        )
    recipes = [Recipe.model_validate(row) for row in catalog["recipes"]]
    root = next(
        (
            r
            for r in recipes
            if (r.spell_id, r.version_hash)
            == (catalog["root_spell_id"], catalog["root_version_hash"])
        ),
        None,
    )
    if root is None or root.result_item_id != opportunity.item_id or not root.qty_min:
        raise ValueError("Не найден исходный рецепт цепочки")
    final_quantity = opportunity.quantity * batches
    if final_quantity % root.qty_min:
        raise ValueError("Количество результата не соответствует целым партиям рецепта")
    offers = list(auctions.values()) + [
        MaterialOffer(r["item_id"], r["unit_price_copper"], "npc")
        for r in catalog["offers"]
        if r["source"] == "npc"
    ]
    graph = CraftGraph(recipes, {}, alternatives=tuple(offers), **catalog["settings"])
    try:
        plan = graph.plan(root, final_quantity // root.qty_min)
    except SearchLimit as error:
        raise ValueError(
            "Цепочка слишком сложная: не найден полный путь в пределах лимита поиска"
        ) from error
    if plan is None:
        raise ValueError(
            "Нет полного пути для партии: проверьте объём компонентов, глубину цепочки и лимит крафтов"
        )
    names = catalog.get("names", {})
    materials = [
        {
            "item_id": p.offer.item_id,
            "item_name": names.get(str(p.offer.item_id), str(p.offer.item_id)),
            "quantity": p.quantity,
            "source": p.offer.source,
            "unit_price_copper": p.offer.unit_price_copper,
            "cost_copper": p.offer.cost(p.quantity),
        }
        for p in plan.purchases
    ]
    steps = wire_steps(plan)
    return {
        "model": MODEL,
        "steps": steps,
        "materials": materials,
        "cost_copper": plan.cost_copper,
        "surplus": [
            {
                "item_id": s.recipe.result_item_id,
                "quantity": s.output_quantity - s.required_quantity,
            }
            for s in plan.steps
            if s.output_quantity > s.required_quantity
        ],
        "search": {
            **catalog["settings"],
            "nodes": graph.nodes,
            "cycles_blocked": graph.cycles_blocked,
            "limited": graph.search_limited,
        },
    }
