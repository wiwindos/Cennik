"""Bounded joint batch search with one material balance per production plan.

One producer per item; optional purchased tail units. Only complete acyclic
routes are returned. Unused guaranteed output has no credited sale value.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property

from cennik.core.models import Recipe, RecipeInput

MODEL = "shared_batches_v1"


@dataclass(frozen=True)
class PriceLevel:
    quantity: int
    numerator: int
    denominator: int = 1


@dataclass(frozen=True)
class MaterialOffer:
    item_id: int
    unit_price_copper: int
    source: str
    levels: tuple[PriceLevel, ...] | None = None

    @cached_property
    def _depth(self):
        amounts, costs = [], []
        quantity = total = 0
        for level in self.levels or ():
            quantity += level.quantity
            total += (level.quantity * level.numerator + level.denominator - 1) // level.denominator
            amounts.append(quantity)
            costs.append(total)
        return amounts, costs

    def cost(self, quantity: int) -> int | None:
        if quantity <= 0:
            return 0 if quantity == 0 else None
        if self.levels is None:
            return self.unit_price_copper * quantity
        amounts, costs = self._depth
        index = bisect_left(amounts, quantity)
        if index == len(amounts):
            return None
        before = amounts[index - 1] if index else 0
        base = costs[index - 1] if index else 0
        level = self.levels[index]
        return (
            base
            + ((quantity - before) * level.numerator + level.denominator - 1) // level.denominator
        )


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


@dataclass(frozen=True)
class _Choice:
    recipe: Recipe | None
    buy_tail: bool = False


class SearchLimit(ValueError):
    pass


def complete(recipe: Recipe) -> bool:
    return bool(
        recipe.result_item_id is not None
        and recipe.qty_min
        and recipe.qty_min > 0
        and recipe.qty_max
        and recipe.qty_max >= recipe.qty_min
        and recipe.inputs
        and all(r.quantity > 0 and r.item_id != recipe.result_item_id for r in recipe.inputs)
    )


def normalized(recipe: Recipe) -> Recipe:
    quantities: dict[int, int] = defaultdict(int)
    for row in recipe.inputs:
        quantities[row.item_id] += row.quantity
    return recipe.model_copy(
        update={
            "inputs": [RecipeInput(item_id=i, quantity=q) for i, q in sorted(quantities.items())]
        }
    )


def wire_steps(plan: CraftPlan) -> list[dict]:
    return [
        {
            "recipe": s.recipe.model_dump(mode="json"),
            "batches": s.batches,
            "required_quantity": s.required_quantity,
            "output_quantity": s.output_quantity,
            "surplus_quantity": s.output_quantity - s.required_quantity,
            "inputs": [
                {"item_id": r.item_id, "quantity": r.quantity * s.batches} for r in s.recipe.inputs
            ],
        }
        for s in plan.steps
    ]


class CraftGraph:
    def __init__(
        self,
        recipes: list[Recipe],
        offers: dict[int, MaterialOffer],
        *,
        max_depth: int = 5,
        max_nodes: int = 2000,
        max_crafts: int = 10000,
        alternatives: tuple[MaterialOffer, ...] = (),
    ):
        self.offers = offers
        self.max_depth, self.max_nodes, self.max_crafts = max_depth, max_nodes, max_crafts
        self.producers: dict[int, list[Recipe]] = defaultdict(list)
        for recipe in sorted(recipes, key=lambda r: (r.spell_id, r.version_hash)):
            if complete(recipe):
                assert recipe.result_item_id is not None
                self.producers[recipe.result_item_id].append(normalized(recipe))
        self.buy_offers: dict[int, list[MaterialOffer]] = defaultdict(list)
        for offer in [*offers.values(), *alternatives]:
            if offer.unit_price_copper > 0 and offer not in self.buy_offers[offer.item_id]:
                self.buy_offers[offer.item_id].append(offer)
        self.nodes = self.cycles_blocked = 0
        self.search_limited = False
        self.best: CraftPlan | None = None

    def _buy(self, item, quantity):
        options = [
            (cost, o)
            for o in self.buy_offers.get(item, [])
            if (cost := o.cost(quantity)) is not None
        ]
        return min(options, key=lambda row: (row[0], row[1].source)) if options else None

    def plan(self, recipe: Recipe, batches: int = 1) -> CraftPlan | None:
        self.nodes = self.cycles_blocked = 0
        self.search_limited, self.best = False, None
        if not complete(recipe) or type(batches) is not int or batches <= 0:
            return None
        root = normalized(recipe)
        assert root.result_item_id is not None and root.qty_min is not None
        self._quantity, self._root = root.qty_min * batches, root.result_item_id
        try:
            self._search({self._root: _Choice(root)})
        except (SearchLimit, RecursionError) as error:
            self.search_limited = True
            if self.best is None:
                raise SearchLimit("craft_graph_search_limit") from error
        return self.best

    def _order(self, policy):
        state: dict[int, int] = {}
        order: list[int] = []

        def visit(item):
            if state.get(item) == 1:
                self.cycles_blocked += 1
                return False
            if state.get(item) == 2:
                return True
            state[item] = 1
            choice = policy.get(item)
            if choice and choice.recipe:
                for row in choice.recipe.inputs:
                    if not visit(row.item_id):
                        return False
            state[item] = 2
            order.append(item)
            return True

        return list(reversed(order)) if visit(self._root) else None

    def _evaluate(self, policy):
        order = self._order(policy)
        if order is None:
            return None
        needed: dict[int, int] = defaultdict(int, {self._root: self._quantity})
        depth: dict[int, int] = defaultdict(int)
        cost, crafts, frontier, purchases, steps = 0, 0, [], [], []
        for item in order:
            amount = needed[item]
            if not amount:
                continue
            choice = policy.get(item)
            if choice is None:
                frontier.append(item)
                continue
            if choice.recipe is None:
                purchase = self._buy(item, amount)
                if purchase is None:
                    return None
                cost += purchase[0]
                purchases.append(Purchase(purchase[1], amount))
                continue
            recipe = choice.recipe
            assert recipe.qty_min is not None
            if depth[item] >= self.max_depth:
                return None
            count = (
                amount // recipe.qty_min
                if choice.buy_tail
                else (amount + recipe.qty_min - 1) // recipe.qty_min
            )
            tail = amount - count * recipe.qty_min if choice.buy_tail else 0
            crafts += count
            if crafts > self.max_crafts:
                return None
            # Ignore tail cost in partial lower bounds: demand 4 -> 5 can reduce
            # a purchased remainder from 4 to 0. Crafted/input needs stay monotone.
            if tail:
                purchases.append(Purchase(MaterialOffer(item, 1, "tail"), tail))
            if count:
                steps.append(CraftStep(recipe, count, amount - tail))
                for row in recipe.inputs:
                    needed[row.item_id] += row.quantity * count
                    depth[row.item_id] = max(depth[row.item_id], depth[item] + 1)
        if frontier:
            return cost, frontier, None
        final_purchases = []
        for purchase in purchases:
            if purchase.offer.source == "tail":
                quote = self._buy(purchase.offer.item_id, purchase.quantity)
                if quote is None:
                    return None
                cost += quote[0]
                purchase = Purchase(quote[1], purchase.quantity)
            final_purchases.append(purchase)
        return cost, [], CraftPlan(cost, tuple(final_purchases), tuple(reversed(steps)))

    def _search(self, policy):
        self.nodes += 1
        if self.nodes > self.max_nodes:
            raise SearchLimit("craft_graph_search_limit")
        evaluation = self._evaluate(policy)
        if evaluation is None:
            return
        cost, frontier, result = evaluation
        if result is not None:

            def key(p: CraftPlan):
                return p.cost_copper, sum(s.batches for s in p.steps), len(p.steps)

            if self.best is None or key(result) < key(self.best):
                self.best = result
            return
        if self.best is not None and cost > self.best.cost_copper:
            return
        item = frontier[0]
        choices = [_Choice(None)] if self.buy_offers.get(item) else []
        for recipe in self.producers.get(item, []):
            choices.append(_Choice(recipe))
            if recipe.qty_min and recipe.qty_min > 1 and self.buy_offers.get(item):
                choices.append(_Choice(recipe, True))
        for choice in choices:
            self._search({**policy, item: choice})

    def snapshot(self, root: Recipe) -> dict:
        found = {(root.spell_id, root.version_hash): normalized(root)}
        visited: dict[int, int] = {}
        items = set()

        def gather(recipe, depth):
            for row in recipe.inputs:
                items.add(row.item_id)
                if (
                    depth + 1 >= self.max_depth
                    or visited.get(row.item_id, self.max_depth + 1) <= depth + 1
                ):
                    continue
                visited[row.item_id] = depth + 1
                for producer in self.producers.get(row.item_id, []):
                    key = producer.spell_id, producer.version_hash
                    found[key] = producer
                    if len(found) > self.max_nodes:
                        raise SearchLimit("craft_graph_catalog_limit")
                    gather(producer, depth + 1)

        gather(root, 0)
        return {
            "model": MODEL,
            "root_spell_id": root.spell_id,
            "root_version_hash": root.version_hash,
            "auction_items": sorted(items),
            "recipes": [r.model_dump(mode="json") for _, r in sorted(found.items())],
            "offers": [
                {"item_id": o.item_id, "unit_price_copper": o.unit_price_copper, "source": o.source}
                for item in sorted(items)
                for o in self.buy_offers.get(item, [])
            ],
            "settings": {
                "max_depth": self.max_depth,
                "max_nodes": self.max_nodes,
                "max_crafts": self.max_crafts,
            },
        }
