"""Bounded recipe graph with integer batches and no speculative surplus value.

Recipes are hyperedges (several inputs, one result). Buying is a terminal edge.
Each dependency chooses its cheapest complete bounded plan; surplus is not reused
across branches. This is a conservative plan, not global batch optimization.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from cennik.core.models import Recipe


@dataclass(frozen=True)
class MaterialOffer:
    item_id: int
    unit_price_copper: int
    source: str


@dataclass(frozen=True)
class Purchase:
    offer: MaterialOffer
    quantity: int


@dataclass(frozen=True)
class CraftStep:
    recipe: Recipe
    batches: int
    required_quantity: int

    @property
    def output_quantity(self) -> int:
        assert self.recipe.qty_min is not None
        return self.batches * self.recipe.qty_min


@dataclass(frozen=True)
class CraftPlan:
    cost_copper: int
    purchases: tuple[Purchase, ...]
    steps: tuple[CraftStep, ...]


class SearchLimit(ValueError):
    pass


def complete(recipe: Recipe) -> bool:
    return bool(recipe.result_item_id is not None and recipe.qty_min and recipe.qty_min > 0
                and recipe.qty_max and recipe.qty_max >= recipe.qty_min and recipe.inputs
                and all(r.quantity > 0 and r.item_id != recipe.result_item_id for r in recipe.inputs))


class CraftGraph:
    def __init__(self, recipes: list[Recipe], offers: dict[int, MaterialOffer], *,
                 max_depth: int = 5, max_nodes: int = 2000, max_crafts: int = 10000):
        self.offers = offers
        self.max_depth, self.max_nodes, self.max_crafts = max_depth, max_nodes, max_crafts
        self.producers: dict[int, list[Recipe]] = defaultdict(list)
        for recipe in sorted(recipes, key=lambda r: (r.spell_id, r.version_hash)):
            if complete(recipe):
                assert recipe.result_item_id is not None
                self.producers[recipe.result_item_id].append(recipe)
        self.nodes = 0
        self.cycles_blocked = 0
        self._cache: dict[tuple, CraftPlan | None] = {}

    def plan(self, recipe: Recipe) -> CraftPlan | None:
        self.nodes = self.cycles_blocked = 0
        self._cache.clear()
        if not complete(recipe):
            return None
        assert recipe.qty_min is not None
        return self._craft(recipe, recipe.qty_min, 0, frozenset())

    def _visit(self) -> None:
        self.nodes += 1
        if self.nodes > self.max_nodes:
            raise SearchLimit("craft_graph_search_limit")

    def _acquire(self, item: int, quantity: int, depth: int, ancestors: frozenset[int]) -> CraftPlan | None:
        if item in ancestors:
            self.cycles_blocked += 1
            return None
        key = (item, quantity, depth, ancestors)
        if key in self._cache:
            return self._cache[key]
        self._visit()
        candidates: list[CraftPlan] = []
        offer = self.offers.get(item)
        if offer and offer.unit_price_copper > 0:
            candidates.append(CraftPlan(offer.unit_price_copper * quantity, (Purchase(offer, quantity),), ()))
        if depth < self.max_depth:
            for recipe in self.producers.get(item, []):
                plan = self._craft(recipe, quantity, depth, ancestors)
                if plan is not None:
                    candidates.append(plan)
        result = min(candidates, key=lambda p: (p.cost_copper, len(p.steps))) if candidates else None
        self._cache[key] = result
        return result

    def _craft(self, recipe: Recipe, quantity: int, depth: int, ancestors: frozenset[int]) -> CraftPlan | None:
        self._visit()
        assert recipe.qty_min is not None and recipe.result_item_id is not None
        if recipe.result_item_id in ancestors:
            self.cycles_blocked += 1
            return None
        batches = (quantity + recipe.qty_min - 1) // recipe.qty_min
        if batches > self.max_crafts:
            return None
        inputs: dict[int, int] = defaultdict(int)
        for reagent in recipe.inputs:
            inputs[reagent.item_id] += reagent.quantity * batches
        cost = 0
        purchases: list[Purchase] = []
        steps: list[CraftStep] = []
        for item, needed in sorted(inputs.items()):
            plan = self._acquire(item, needed, depth + 1, ancestors | {recipe.result_item_id})
            if plan is None:
                return None
            cost += plan.cost_copper
            purchases.extend(plan.purchases)
            steps.extend(plan.steps)
        if sum(s.batches for s in steps) + batches > self.max_crafts:
            return None
        steps.append(CraftStep(recipe, batches, quantity))
        return CraftPlan(cost, tuple(purchases), tuple(steps))
