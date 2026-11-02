from collections import defaultdict

import pytest
from test_craft_graph import offer, recipe

from cennik.engines.craft_graph import CraftGraph, MaterialOffer, PriceLevel


def physical(plan):
    stock = defaultdict(int)
    for p in plan.purchases:
        stock[p.offer.item_id] += p.quantity
    for s in plan.steps:
        for r in s.recipe.inputs:
            needed = r.quantity * s.batches
            assert stock[r.item_id] >= needed
            stock[r.item_id] -= needed
        stock[s.recipe.result_item_id] += s.output_quantity
    return dict(stock)


def test_user_example_and_joint_choice_when_each_branch_would_prefer_buy():
    x = recipe(1, 2, [(1, 3)], qty=5)
    a, b = recipe(2, 3, [(2, 2)]), recipe(3, 4, [(2, 2)])
    root = recipe(4, 5, [(3, 1), (4, 1)])
    for inputs in (root.inputs, list(reversed(root.inputs))):
        graph = CraftGraph([root, b, a, x], {1: offer(1, 10), 2: offer(2, 8)})
        plan = graph.plan(root.model_copy(update={"inputs": inputs}))
        assert plan.cost_copper == 30
        assert len(plan.steps) == 4 and plan.steps[0].recipe.spell_id == 1
        assert plan.steps[0].batches == 1 and plan.steps[0].required_quantity == 4
        assert physical(plan)[2] == 1
        # Every plan owns its balance; the next call cannot reuse its leftovers.
        assert graph.plan(root).cost_copper == 30


def test_whole_batch_replanning_and_guaranteed_not_maximum_output():
    x = recipe(1, 2, [(1, 3)], qty=5).model_copy(update={"qty_max": 50})
    root = recipe(2, 3, [(2, 2)])
    graph = CraftGraph([x, root], {1: offer(1, 10)})
    one, two, three = (graph.plan(root, batches=n) for n in (1, 2, 3))
    assert [p.cost_copper for p in (one, two, three)] == [30, 30, 60]
    assert two.steps[0].batches == 1 and two.steps[-1].batches == 2
    assert physical(two) == {1: 0, 2: 1, 3: 2}
    assert three.steps[0].batches == 2


def test_one_craft_plus_purchased_tail_and_alternative_producers():
    x = recipe(1, 2, [(1, 1)], qty=5)
    root = recipe(2, 3, [(2, 6)])
    plan = CraftGraph([x, root], {1: offer(1, 10), 2: offer(2, 5)}).plan(root)
    assert plan.cost_copper == 15
    assert {(p.offer.item_id, p.quantity) for p in plan.purchases} == {(1, 1), (2, 1)}
    assert physical(plan)[3] == 1
    alternate = recipe(3, 2, [(1, 1)], qty=6)
    plan = CraftGraph([x, alternate, root], {1: offer(1, 10), 2: offer(2, 5)}).plan(root)
    assert plan.cost_copper == 10 and plan.steps[0].recipe.spell_id == 3


def test_finite_depth_and_more_expensive_npc_fallback():
    fractional = MaterialOffer(1, 4, "auction", (PriceLevel(3, 10, 3), PriceLevel(2, 9, 2)))
    assert [fractional.cost(q) for q in (0, 1, 3, 4, 5, 6)] == [0, 4, 10, 15, 19, None]
    root = recipe(1, 2, [(1, 4)])
    cheap = MaterialOffer(1, 3, "auction", (PriceLevel(3, 3),))
    npc = offer(1, 8, "npc")
    plan = CraftGraph([root], {1: cheap}, alternatives=(npc,)).plan(root)
    assert plan.cost_copper == 32 and plan.purchases[0].offer.source == "npc"
    expensive = MaterialOffer(1, 3, "auction", (PriceLevel(2, 3), PriceLevel(2, 6)))
    plan = CraftGraph([root], {1: expensive}, alternatives=(npc,)).plan(root)
    assert plan.cost_copper == 18 and plan.purchases[0].offer.source == "auction"
    assert CraftGraph([root], {1: cheap}).plan(root) is None


def test_shared_depth_is_consumed_once_and_cycles_remain_blocked():
    raw = recipe(1, 2, [(1, 3)], qty=5)
    a, b = recipe(2, 3, [(2, 2)]), recipe(3, 4, [(2, 2)])
    root = recipe(4, 5, [(3, 1), (4, 1)])
    graph = CraftGraph(
        [root, a, b, raw], {1: MaterialOffer(1, 10, "auction", (PriceLevel(3, 10),))}
    )
    plan = graph.plan(root)
    assert plan.cost_copper == 30 and physical(plan)[2] == 1
    # A selected dependency must always precede every consumer.
    assert [s.recipe.result_item_id for s in plan.steps].index(2) < [
        s.recipe.result_item_id for s in plan.steps
    ].index(3)


def test_duplicate_reagents_and_limited_search_return_only_complete_routes():
    root = recipe(1, 2, [(1, 1), (1, 2)])
    plan = CraftGraph([root], {1: offer(1, 10)}).plan(root)
    assert plan.cost_copper == 30 and len(plan.steps[0].recipe.inputs) == 1
    assert physical(plan)[2] == 1
    a = recipe(2, 1, [(9, 1)])
    graph = CraftGraph([root, a], {1: offer(1, 10), 9: offer(9, 1)}, max_nodes=2)
    plan = graph.plan(root)
    assert plan.cost_copper == 30 and graph.search_limited
    assert physical(plan)[2] == 1


@pytest.mark.parametrize("batches", [0, -1, 1.5, True])
def test_invalid_batch_counts_do_not_create_stock(batches):
    root = recipe(1, 2, [(1, 1)])
    assert CraftGraph([root], {1: offer(1, 10)}).plan(root, batches=batches) is None
