"""Exclusive purchasing plans across two addon runtimes and the private bridge."""
import json
from datetime import UTC, datetime

import pytest
from test_accounting import lua_value
from test_production import production_lua, production_setup, save_client
from test_workspace import workspace as workspace

from cennik.addon.lua_data import lua_literal
from cennik.addon.models import ExportRequest
from cennik.workspace.production import OrderCreate


def import_routes(workspace, clients, lua_clients):
    workspace.synchronize(include_scans=False)
    text = clients[0].paths()[0].joinpath("ImportedData.lua").read_text("utf-8")
    for lua in lua_clients:
        lua.execute(text)
        lua.execute("Cennik:Import();Cennik.Production:Initialize()")


def test_addon_handoff_keeps_limits_exclusive_queue_and_real_craft(workspace, tmp_path):
    clients, _, original, _ = production_setup(workspace, tmp_path, create=False)
    package = workspace.exchange.export(ExportRequest(
        request_id="plan-fixture-0001", opportunity_ids=original.opportunity_ids))
    task = package.tasks[0]
    auctioneer, crafter = [production_lua(workspace, c) for c in clients]
    import_routes(workspace, clients, (auctioneer, crafter))
    auctioneer.execute("""
      local t=Cennik.db.tasks["""+lua_literal(task.task_id)+"""]
      t.lines[1].unit_limit_copper=12;t.max_total_cost_copper=72
      t.conditions_confirmed=true;t.validation={state='ready_to_buy'}
      Cennik.UI.selected={[t.task_id]=true}
      assert(Cennik.Production:SendPlan("""+lua_literal(clients[1].id)+""",Cennik.UI.selected))
      assert(Cennik.Production:PurchaseBlock(t.task_id))
      local ok=Cennik:CheckTask(t);assert(not ok)
    """)
    save_client(auctioneer, clients[0])
    import_routes(workspace, clients, (auctioneer, crafter))
    order = workspace.production.status()[0]
    assert order["procurement"] == "self_purchase" and order["transfers"] == []
    assert auctioneer.eval("#CennikImport.packages") == 0
    assert auctioneer.eval("#Cennik.Production.db.plan_requests") == 0
    assert crafter.eval("#CennikImport.packages") == 1
    target = crafter.eval("Cennik.db.tasks["+lua_literal(task.task_id)+"]")
    assert target["lines"][1]["unit_limit_copper"] == 12
    assert target["max_total_cost_copper"] == 72
    assert target["conditions_confirmed"] is None and target["validation"] is None
    assert lua_value(target["bought"]) == {}
    # The source keeps its historical task, but can no longer buy it.
    assert auctioneer.eval("Cennik.Production:PurchaseBlock("+lua_literal(task.task_id)+")")
    crafter.execute("""
      Cennik.Accounting:Purchase('own',2589,6,60,{task_id="""+lua_literal(task.task_id)+""",strategy_id='simple_craft'})
      Game.bags={{item_id=2589,quantity=6}};Cennik.Accounting.bags=Cennik.Accounting:Snapshot()
      assert(Cennik.Production:Available(Cennik.Production:Selected().steps[1])==2)
      Game.click(function() Cennik.Production:Craft() end)
      assert(Game.craftCount==2)
      Game.event('UNIT_SPELLCAST_SUCCEEDED','player','one',100)
      Game.bags[1].quantity=3;Game.bags[2]={item_id=4238,quantity=1};Game.event('BAG_UPDATE_DELAYED');Game.advance(1)
      Game.event('UNIT_SPELLCAST_SUCCEEDED','player','two',100)
      Game.bags[1]=nil;Game.bags[2].quantity=2;Game.event('BAG_UPDATE_DELAYED');Game.advance(1)
    """)
    save_client(crafter, clients[1])
    import_routes(workspace, clients, (auctioneer, crafter))
    assert workspace.production.status()[0]["steps"][0]["done"] == 2
    assert workspace.accounting.overview()["summary"]["purchase_copper"] == 60
    assert len(workspace.production.orders()) == 1
    workspace.production.close_order(order["order_id"])
    import_routes(workspace, clients, (auctioneer, crafter))
    assert crafter.eval("#CennikImport.packages") == 0
    assert crafter.eval("Cennik.Production:PurchaseBlock("+lua_literal(task.task_id)+")")


def test_rejected_request_receipt_survives_retry_and_releases_local_hold(workspace, tmp_path):
    clients, order, _, _ = production_setup(workspace, tmp_path)
    source = production_lua(workspace, clients[0])
    task_id = order["task_ids"][0]
    request = OrderCreate(request_id="already-assigned-plan", crafter_id=clients[1].id,
                          task_ids=[task_id], procurement="self_purchase")
    source.execute("Cennik.Production.db.plan_requests="+lua_literal([request.model_dump(mode="json")]))
    raw = {"plan_requests": [request.model_dump(mode="json")]}
    result = workspace.production.accept_plan_requests(clients[0], raw)
    assert result[0]["state"] == "rejected"
    assert workspace.production.accept_plan_requests(clients[0], raw) == result
    source.execute("CennikImport.production="+lua_literal(workspace.production.wire(clients[0]))+";Cennik.Production:Initialize()")
    assert source.eval("#Cennik.Production.db.plan_requests") == 0
    assert source.eval("Cennik.Production:PurchaseBlock("+lua_literal(task_id)+")") is None
    assert "не передан" in source.eval("Cennik.Production.message")
    with pytest.raises(ValueError, match="аукционер"):
        workspace.production.accept_plan_requests(clients[1], raw)


def test_own_purchase_rejects_existing_purchase_and_cross_task_limits(workspace, tmp_path):
    clients, _, original, _ = production_setup(workspace, tmp_path, create=False)
    package = workspace.exchange.export(ExportRequest(request_id="plan-fixture-0002", opportunity_ids=original.opportunity_ids))
    task = package.tasks[0]
    request = OrderCreate(crafter_id=clients[1].id, task_ids=[task.task_id], procurement="self_purchase")
    with pytest.raises(ValueError, match="другому заданию"):
        OrderCreate(**{**request.model_dump(), "task_budgets": {"other": 5}})
    source = production_lua(workspace, clients[0])
    workspace.synchronize(include_scans=False)
    source.execute(clients[0].paths()[0].joinpath("ImportedData.lua").read_text("utf-8"))
    source.execute("Cennik:Import();Cennik.Production:Initialize();Cennik.db.tasks["+lua_literal(task.task_id)+"].spent=1")
    ok, reason = source.eval("Cennik.Production:SendPlan("+lua_literal(clients[1].id)+",{["+lua_literal(task.task_id)+"]=true})")
    assert not ok and "покупка" in reason
    with workspace.store.connection() as db:
        db.execute("INSERT INTO addon_events VALUES(?,?,?,?,?,?)", ["prior", task.task_id, task.market,
            datetime.now(UTC).isoformat(), "purchase", json.dumps({"quantity": 1})])
    with pytest.raises(ValueError, match="уже была покупка"):
        workspace.production.create(request)
    assert workspace.production.orders() == []


def test_resolved_nonpurchase_can_be_handed_off_and_other_crafter_cannot_buy(workspace, tmp_path):
    from cennik.addon.models import ReportBatch, ReportEvent

    clients, _, original, _ = production_setup(workspace, tmp_path, create=False)
    package = workspace.exchange.export(ExportRequest(request_id="plan-fixture-0003", opportunity_ids=original.opportunity_ids))
    task = package.tasks[0]
    event = dict(task_id=task.task_id, opportunity_id=task.opportunity_id, run_id=task.run_id,
                 market=task.market, observed_at=datetime.now(UTC))
    workspace.exchange.reports(ReportBatch(events=[ReportEvent(event_id="unknown:1", kind="state", state="requires_manual_check", **event)]))
    request = OrderCreate(crafter_id=clients[1].id, task_ids=[task.task_id], procurement="self_purchase")
    with pytest.raises(ValueError, match="неизвестен"):
        workspace.production.create(request)
    workspace.exchange.reports(ReportBatch(events=[ReportEvent(event_id="resolved:2", kind="purchase", state="not_bought", **event)]))
    # Routing ordinary packages must preserve their pre-existing budget.
    normal = workspace.production.route_packages(clients[0], [package.model_dump(mode="json")])
    assert normal[0]["max_budget_copper"] == package.max_budget_copper
    workspace.production.create(request)
    other = clients[1].model_copy(update={"id": "c"*32})
    assert workspace.production.route_packages(other, [package.model_dump(mode="json")]) == []
    assert workspace.production.route_packages(clients[0], [package.model_dump(mode="json")]) == []
