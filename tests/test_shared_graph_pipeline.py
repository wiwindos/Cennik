import pytest
from test_bulk import book, opportunity, setup, submit
from test_craft_graph import offer, recipe
from test_production import production_lua, production_setup, save_client
from test_workspace import scan
from test_workspace import workspace as workspace

from cennik.addon.lua_data import lua_literal
from cennik.engines.craft_graph import MODEL, CraftGraph, wire_steps
from cennik.workspace.bulk import BulkChoice, BulkCreate, BulkSelection
from cennik.workspace.models import MARKET, PlanRequest
from cennik.workspace.production import OrderCreate


def graph_op(identity="joint"):
    value = opportunity(identity, strategy="craft_graph")
    mid = recipe(100, 2996, [(2589, 3)], qty=5)
    root = recipe(101, 4238, [(2996, 2)])
    graph = CraftGraph([mid, root], {2589: offer(2589, 10)})
    plan = graph.plan(root)
    value.details.update(
        recipe=root.model_dump(mode="json"),
        graph_model=MODEL,
        liquidity={"conservative_sales_per_day": "1000000"},
        graph_catalog=graph.snapshot(root),
        steps=wire_steps(plan),
        materials=[{"item_id": 2589, "quantity": 3, "source": "auction", "unit_price_copper": 10}],
    )
    return value.model_copy(
        update={
            "strategy_version": "0.4.0",
            "required_capital_copper": 30,
            "potential_profit_copper": 65,
        }
    )


def checked(workspace, tmp_path, *values):
    setup(workspace, tmp_path, *values)
    identity = workspace.bulk.create(BulkCreate(opportunity_ids=[o.id for o in values]))["batch_id"]
    submit(workspace, identity, book(2589, ((10, 3),)), book(2996, ()), book(4238, ((100, 10),)))
    return identity


def test_workspace_rebuilds_two_final_crafts_from_one_intermediate_batch(workspace, tmp_path):
    data = scan(count=1)
    data["data"] = "2589:3:30:0:3:1"
    workspace.market.import_scan(data)
    value = graph_op()
    setup(workspace, tmp_path, value)
    plan = workspace.plan(PlanRequest(opportunity_id=value.id, batches=2))
    assert plan["complete"] and plan["cost_copper"] == 30, plan
    assert plan["lines"][0]["needed"] == 3
    assert [s["batches"] for s in plan["graph"]["steps"]] == [1, 2]
    assert plan["graph"]["surplus"] == [{"item_id": 2996, "quantity": 1}]
    workspace.publish_plan(plan["id"])
    task = workspace.exchange.packages(MARKET)[0].tasks[0]
    assert task.lines[0].quantity == 3 and task.opportunity.quantity == 2
    assert [s["batches"] for s in task.opportunity.details["steps"]] == [1, 2]


def test_bulk_capacity_and_joint_publication_do_not_share_stock_between_orders(workspace, tmp_path):
    one, two = graph_op("one"), graph_op("two")
    identity = checked(workspace, tmp_path, one, two)
    result = workspace.bulk.detail(identity, [BulkChoice(opportunity_id=one.id, batches=2)])[
        "items"
    ][0]
    assert result["complete"] and result["cost_copper"] == 30
    assert result["available_batches"] == result["budget_batches"] == 2
    assert result["materials"][0]["quantity"] == 3
    with pytest.raises(ValueError, match="неполная"):
        workspace.bulk.publish(
            identity,
            BulkSelection(choices=[BulkChoice(opportunity_id=o.id, batches=2) for o in (one, two)]),
        )
    with workspace.store.connection() as db:
        assert db.execute("SELECT count(*) FROM addon_tasks").fetchone()[0] == 0


def test_failed_optional_auction_query_does_not_block_complete_craft_route(workspace, tmp_path):
    value = graph_op()
    setup(workspace, tmp_path, value)
    identity = workspace.bulk.create(BulkCreate(opportunity_ids=[value.id]))["batch_id"]
    submit(
        workspace,
        identity,
        book(2589, ((10, 3),)),
        book(2996, (), status="failed", reason="No AH key"),
        book(4238, ((100, 10),)),
        status="partial",
    )
    result = workspace.bulk.detail(identity, [BulkChoice(opportunity_id=value.id, batches=2)])[
        "items"
    ][0]
    assert result["complete"] and result["cost_copper"] == 30
    assert any("2996" in text for text in result["warnings"])


def test_bulk_to_crafter_keeps_one_shared_batch_and_actual_leftover_cost(workspace, tmp_path):
    clients, _, _, _ = production_setup(workspace, tmp_path, create=False)
    value = graph_op()
    identity = checked(workspace, tmp_path / "graph", value)
    result = workspace.bulk.publish(
        identity, BulkSelection(choices=[BulkChoice(opportunity_id=value.id, batches=2)])
    )
    task = workspace.exchange.packages(MARKET)[0].tasks[0]
    assert result["package_id"] and task.opportunity.quantity == 2 and task.lines[0].quantity == 3
    assert [s["batches"] for s in task.opportunity.details["steps"]] == [1, 2]
    workspace.production.create(
        OrderCreate(crafter_id=clients[1].id, task_ids=[task.task_id], procurement="self_purchase")
    )
    crafter = production_lua(workspace, clients[1])
    crafter.execute(
        "Cennik.Accounting:Purchase('shared',2589,3,30,{task_id="
        + lua_literal(task.task_id)
        + ",strategy_id='craft_graph'})"
    )
    crafter.execute("""
      GetNumTradeSkills=function() return 2 end
      GetTradeSkillRecipeLink=function(i) return i==1 and 'enchant:100' or 'enchant:101' end
      GetTradeSkillItemLink=function(i) return i==1 and 'item:2996' or 'item:4238' end
      GetTradeSkillReagentItemLink=function(i) return i==1 and 'item:2589' or 'item:2996' end
      GetTradeSkillReagentInfo=function(i) return 'Component',nil,i==1 and 3 or 2 end
      Game.bags={{item_id=2589,quantity=3}};Cennik.Accounting.bags=Cennik.Accounting:Snapshot()
      Game.click(function() Cennik.Production:Craft() end);assert(Game.craftCount==1)
      Game.event('UNIT_SPELLCAST_SUCCEEDED','player','mid',100)
      Game.bags={{item_id=2996,quantity=5}};Game.event('BAG_UPDATE_DELAYED');Game.advance(1)
      Cennik.Production.stepIndex=2
      Game.click(function() Cennik.Production:Craft() end);assert(Game.craftCount==2)
      Game.event('UNIT_SPELLCAST_SUCCEEDED','player','final1',101)
      Game.bags={{item_id=2996,quantity=3},{item_id=4238,quantity=1}};Game.event('BAG_UPDATE_DELAYED');Game.advance(1)
      Game.event('UNIT_SPELLCAST_SUCCEEDED','player','final2',101)
      Game.bags={{item_id=2996,quantity=1},{item_id=4238,quantity=2}};Game.event('BAG_UPDATE_DELAYED');Game.advance(1)
      Cennik.Accounting:Emit('sale',{inputs={Cennik.Accounting:Line(4238,1)},money_copper=95})
    """)
    save_client(crafter, clients[1])
    sync = workspace.synchronize(include_scans=False)
    assert all(
        "error" not in r["production_reports"] and "error" not in r["accounting_reports"]
        for r in sync["clients"]
    ), sync
    assert [s["done"] for s in workspace.production.status()[0]["steps"]] == [1, 2]
    ledger = workspace.accounting.overview()
    assert ledger["summary"]["purchase_copper"] == 30
    assert ledger["summary"]["known_profit_copper"] == 83
    assert ledger["summary"]["inventory_cost_copper"] == 18
    assert any(i["item_id"] == 2996 and i["remaining"] == 1 for i in ledger["inventory"])
