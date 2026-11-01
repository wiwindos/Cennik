"""Two independent addon runtimes, shared import file, real hand-off evidence."""
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_accounting import accounting_lua, event, line, lua_value
from test_bulk import opportunity, setup
from test_workspace import workspace as workspace

from cennik.addon.lua_data import lua_literal
from cennik.workspace.models import MARKET
from cennik.workspace.production import ClientRoster, OrderCreate, ProductionClient


def production_setup(workspace, tmp_path, *, create=True):
    value = opportunity().model_copy(update={"quantity": 2, "required_capital_copper": 60})
    value.details["recipe"] = {"spell_id": 100, "result_item_id": 4238, "qty_min": 1,
                               "inputs": [{"item_id": 2589, "quantity": 3}], "profession": "Tailoring"}
    value.details["materials"][0]["quantity"] = 6
    setup(workspace, tmp_path, value)
    game = tmp_path / "paired-game"
    clients = [ProductionClient(id="a"*32, role="auctioneer", game_root=str(game), account="first",
                               character="FixturePlayer", realm="FixtureRealm"),
               ProductionClient(id="b"*32, role="crafter", game_root=str(game), account="second",
                               character="Crafter", realm="FixtureRealm")]
    shutil.copytree(Path(__file__).resolve().parents[1] / "CennikAddon", clients[0].paths()[0])
    workspace.production.save_clients(ClientRoster(clients=clients))
    request = OrderCreate(crafter_id=clients[1].id, opportunity_ids=[value.id])
    if not create:
        return clients, None, request, None
    result = workspace.production.create(request)
    order = workspace.production.status()[0]
    return clients, order, request, result


def production_lua(workspace, client):
    lua = accounting_lua(workspace)
    lua.execute("Game.character="+lua_literal(client.character)+";Game.partner="+lua_literal("Crafter" if client.role == "auctioneer" else "FixturePlayer"))
    lua.execute("""
      UnitName=function(unit) return unit=='NPC' and Game.partner or Game.character end
      Game.messages={};C_ChatInfo={RegisterAddonMessagePrefix=function() end,
        SendAddonMessage=function(prefix,text,channel,target) Game.messages[#Game.messages+1]={prefix=prefix,text=text,target=target} end}
      SendMailFrame=CreateFrame('Frame');TradeFrame=CreateFrame('Frame')
      SendMailNameEditBox=CreateFrame('EditBox');SendMailSubjectEditBox=CreateFrame('EditBox')
      GetNumTradeSkills=function() return 1 end
      local craft=DoTradeSkill
      DoTradeSkill=function(index,count) assert(Game.hardware,'Craft needs player click');Game.craftCount=count;craft(index,count) end
      Game.trade={}
      GetTradePlayerItemLink=function(i) local r=Game.trade[i];return r and ('item:'..r.item_id) end
      GetTradePlayerItemInfo=function(i) local r=Game.trade[i];return 'Item',nil,r and r.quantity end
      GetPlayerTradeMoney=function() return 0 end;GetTargetTradeMoney=function() return 0 end
      ERR_TRADE_COMPLETE='Trade complete'
    """)
    payload = {"format_version": 1, "device_id": client.id, "client_id": client.id,
               "market_collector": {"workspace_id": workspace.identity},
               "accounting": workspace.accounting.wire(), "production": workspace.production.wire(client)}
    lua.execute("CennikImport="+lua_literal({"format_version": 2, "routes": {client.realm+":"+client.character: payload}}))
    lua.execute("Cennik:Import();Cennik:BindMarket('"+MARKET+"');Cennik.Accounting:Initialize();Cennik.Production:Initialize()")
    assert lua.eval("Cennik.Production.db~=nil")
    return lua


def relay(sender, recipient):
    messages = lua_value(sender.eval("Game.messages")) or []
    for message in messages:
        recipient.globals().Cennik.Production.OnMessage(recipient.globals().Cennik.Production,
            message["prefix"], message["text"], sender.eval("Game.character")+"-FixtureRealm")
    sender.execute("Game.messages={}")


def save_client(lua, client):
    saved = client.paths()[1]
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text("CennikAddonDB="+lua_literal(lua_value(lua.globals().CennikAddonDB)), encoding="utf-8")


def begin_mail(sender, recipient, item, count):
    items = [{"item_id": item, "quantity": count}]
    sender.execute("Game.bags="+lua_literal(items)+";Game.draft={};Cennik.Accounting.bags=Cennik.Accounting:Snapshot()")
    assert sender.eval("Game.click(function() return Cennik.Production:Prepare('mail') end)")
    sender.execute("Game.draft="+lua_literal(items)+";Game.event('MAIL_SEND_INFO_UPDATE');SendMail(Game.partner,'unused','')")
    relay(sender, recipient)
    subject = sender.eval("SendMailSubjectEditBox:GetText()")
    sender.execute("Game.bags={};Game.money=Game.money-30;Game.event('MAIL_SEND_SUCCESS');Game.advance(1)")
    recipient.execute("Game.mail="+lua_literal([{"sender": sender.eval("Game.character"), "subject": subject, "items": items}])+";Game.event('MAIL_SHOW')")


def collect_mail(recipient, item, count):
    recipient.execute("TakeInboxItem(1,1);Game.bags="+lua_literal([{"item_id": item, "quantity": count}])+";Game.mail={};Game.event('MAIL_INBOX_UPDATE');Game.advance(1)")


def test_routing_order_idempotence_and_no_repurchase(workspace, tmp_path):
    clients, order, request, result = production_setup(workspace, tmp_path)
    assert workspace.production.create(request)["reused"]
    assert len(workspace.production.orders()) == 1
    assert order["steps"][0]["crafts"] == 2
    assert order["transfers"][0]["lines"][0]["quantity"] == 6
    assert result["tasks"] == 1
    assert workspace.production.candidates() == []
    workspace.synchronize(include_scans=False)
    text = clients[0].paths()[0].joinpath("ImportedData.lua").read_text("utf-8")
    for client in clients:
        lua = production_lua(workspace, client)
        lua.execute(text)
        lua.execute("Cennik:Import()")
        assert lua.eval("CennikImport.client_id") == client.id
        assert lua.eval("Cennik.db.device_id") == client.id
        assert bool(lua.eval("#CennikImport.packages")) == (client.role == "auctioneer")
    assert (workspace.root / "bridge/clients" / clients[0].id / "state.json").exists()
    assert (workspace.root / "bridge/clients" / clients[1].id / "state.json").exists()
    with pytest.raises(ValueError, match="уже назначено"):
        workspace.production.create(OrderCreate(crafter_id=clients[1].id, task_ids=order["task_ids"]))
    with pytest.raises(ValueError, match="активных заказов"):
        workspace.production.save_clients(ClientRoster())


def test_two_accounts_mail_craft_trade_return_profit_and_reload(workspace, tmp_path):
    clients, order, _, _ = production_setup(workspace, tmp_path)
    auctioneer, crafter = [production_lua(workspace, c) for c in clients]
    auctioneer.execute("Cennik.Accounting:Purchase('fixture',2589,6,60,{task_id="+lua_literal(order["task_ids"][0])+",strategy_id='simple_craft'})")
    begin_mail(auctioneer, crafter, 2589, 6)
    collect_mail(crafter, 2589, 6)
    assert crafter.eval("Cennik.Production:Available(Cennik.Production:Selected().steps[1])") == 2
    crafter.execute("Game.click(function() Cennik.Production:Craft() end)")
    assert crafter.eval("Game.craftCount") == 2
    crafter.execute("""
      Game.event('UNIT_SPELLCAST_SUCCEEDED','player','one',100)
      Game.bags[1].quantity=3;Game.bags[2]={item_id=4238,quantity=1};Game.event('BAG_UPDATE_DELAYED');Game.advance(1)
      Game.event('UNIT_SPELLCAST_SUCCEEDED','player','two',100)
      Game.bags[1]=nil;Game.bags[2].quantity=2;Game.event('BAG_UPDATE_DELAYED');Game.advance(1)
    """)
    assert crafter.eval("Cennik.Production.db.done[Cennik.Production:Selected().steps[1].step_id]") == 2
    for lua in (auctioneer, crafter):
        lua.execute("Game.event('TRADE_SHOW')")
    assert crafter.eval("Game.click(function() return Cennik.Production:Prepare('trade') end)")
    crafter.execute("Game.trade={{item_id=4238,quantity=2}};Game.event('TRADE_ACCEPT_UPDATE',1,1)")
    auctioneer.execute("Game.event('TRADE_ACCEPT_UPDATE',1,1)")
    # Trade closing is not completion. A delayed manifest must still be matched.
    crafter.execute("Game.event('TRADE_CLOSED')")
    assert crafter.eval("#Cennik.Production.db.events") == 3
    crafter.execute("Game.bags={};Game.event('UI_INFO_MESSAGE',0,ERR_TRADE_COMPLETE)")
    auctioneer.execute("Game.bags={{item_id=4238,quantity=2}};Game.event('UI_INFO_MESSAGE',0,ERR_TRADE_COMPLETE)")
    relay(crafter, auctioneer)
    for lua in (auctioneer, crafter):
        lua.execute("Game.advance(1)")
    auctioneer.execute("Game.advance(1);Cennik.Accounting:Emit('sale',{inputs={Cennik.Accounting:Line(4238,1)},money_copper=100})")
    for lua, client in zip((auctioneer, crafter), clients, strict=True):
        save_client(lua, client)
    first = workspace.synchronize(include_scans=False)
    assert all('error' not in r["production_reports"] and 'error' not in r["accounting_reports"] for r in first["clients"]), first
    status = workspace.production.status()[0]
    assert status["steps"][0]["done"] == 2
    assert [p["state"] for t in status["transfers"] for p in t["parts"]] == ["confirmed", "confirmed"]
    totals = workspace.accounting.overview()["summary"]
    assert totals["purchase_copper"] == 60
    assert totals["inventory_cost_copper"] == 30
    assert totals["known_profit_copper"] == 40  # 100 net sale - 30 cost - 30 postage.
    again = workspace.synchronize(include_scans=False)
    assert all(r["accounting_reports"]["accepted"] == r["production_reports"]["accepted"] == 0 for r in again["clients"])
    assert workspace.accounting.overview()["summary"] == totals
    for lua, client in zip((auctioneer, crafter), clients, strict=True):
        lua.execute(client.paths()[0].joinpath("ImportedData.lua").read_text("utf-8"))
        lua.execute("Cennik:Import();Cennik.Production:Initialize()")
        assert lua.eval("#Cennik.Production.db.events") == 0
    assert workspace.production.close_order(order["order_id"])["closed"]


def report(workspace, client, order, n, kind, **extra):
    return {"event_id": client.id+":production:"+str(n), "workspace_id": workspace.identity,
        "client_id": client.id, "order_id": order["order_id"], "observed_at": int(datetime.now(UTC).timestamp()),
        "kind": kind, **extra}


def test_reports_are_atomic_partial_and_reject_duplicate_or_excess(workspace, tmp_path):
    clients, order, _, _ = production_setup(workspace, tmp_path)
    transfer = order["transfers"][0]
    args = {"transfer_id": transfer["id"], "part_id": clients[0].id+":1", "mode": "mail"}
    sent = report(workspace, clients[0], order, 1, "sent", lines=[line(2589, 6)], **args)
    assert workspace.production.accept(clients[0], {"events": [sent]})["accepted"] == 1
    one = report(workspace, clients[1], order, 1, "received", lines=[line(2589, 2)], **args)
    two = report(workspace, clients[1], order, 2, "received", lines=[line(2589, 4)], **args)
    workspace.production.accept(clients[1], {"events": [one]})
    assert workspace.production.status()[0]["transfers"][0]["parts"][0]["state"] == "awaiting_peer"
    with pytest.raises(ValueError):
        workspace.production.accept(clients[1], {"events": [two, {**two, "event_id": clients[1].id+":production:3"}]})
    assert len(workspace.production.events()) == 2
    workspace.production.accept(clients[1], {"events": [one, two]})
    assert workspace.production.status()[0]["transfers"][0]["parts"][0]["state"] == "confirmed"
    with pytest.raises(ValueError, match="больше материалов|Повтор отправки"):
        workspace.production.accept(clients[0], {"events": [{**sent, "event_id": clients[0].id+":production:2"}]})
    assert workspace.production.wire(clients[1])["sequence"] == 2


def test_transfer_preserves_distinct_task_cost_lots(workspace):
    rows = [event(workspace, "e:1", "purchase", outputs=[line(2589, 2)], money_copper=20, task_id="one"),
            event(workspace, "e:2", "purchase", outputs=[line(2589, 3)], money_copper=60, task_id="two"),
            event(workspace, "e:3", "transfer", inputs=[line(2589, 5)], outputs=[line(2589, 5)], target_character="Crafter"),
            event(workspace, "e:4", "craft", character="Crafter", inputs=[line(2589, 3)], outputs=[line(4238)], task_id="two")]
    workspace.accounting.accept({"events": rows})
    overview = workspace.accounting.overview()
    assert overview["events"][0]["cost_copper"] == 60
    assert next(r for r in overview["inventory"] if r["item_id"] == 2589)["task_id"] == "one"


def test_cancelled_trade_and_wrong_peer_messages_never_count(workspace, tmp_path):
    clients, _, _, _ = production_setup(workspace, tmp_path)
    sender, receiver = [production_lua(workspace, c) for c in clients]
    sender.execute("Game.bags={{item_id=2589,quantity=6}};Game.event('TRADE_SHOW')")
    assert sender.eval("Game.click(function() return Cennik.Production:Prepare('trade') end)")
    sender.execute("Game.trade={{item_id=2589,quantity=6}};Game.event('TRADE_ACCEPT_UPDATE',1,1);Game.event('TRADE_CLOSED');Game.advance(3)")
    assert sender.eval("#Cennik.Production.db.events") == 0
    message = lua_value(sender.eval("Game.messages[1]"))
    receiver.globals().Cennik.Production.OnMessage(receiver.globals().Cennik.Production, message["prefix"], message["text"], "Stranger")
    assert receiver.eval("next(Cennik.Production.db.parcels)") is None


def test_lua_mail_partial_attachment_collection_counts_only_removed_quantity(workspace, tmp_path):
    clients, order, _, _ = production_setup(workspace, tmp_path)
    sender, receiver = [production_lua(workspace, c) for c in clients]
    begin_mail(sender, receiver, 2589, 6)
    receiver.execute("""
      TakeInboxItem(1,1);Game.mail[1].items[1].quantity=4;Game.bags={{item_id=2589,quantity=2}}
      Game.event('MAIL_INBOX_UPDATE');Game.advance(1)
    """)
    assert receiver.eval("Cennik.Production.db.events[1].lines[1].quantity") == 2
    receiver.execute("TakeInboxItem(1,1);Game.mail={};Game.bags[1].quantity=6;Game.event('MAIL_INBOX_UPDATE');Game.advance(1)")
    assert receiver.eval("Cennik.Production.db.events[2].lines[1].quantity") == 4
    assert receiver.eval("Cennik.Production.db.order_stock["+lua_literal(order["order_id"])+"][\"2589\"]") == 6


def test_trade_materials_and_mail_unused_materials_back(workspace, tmp_path):
    clients, _, _, _ = production_setup(workspace, tmp_path)
    sender, receiver = [production_lua(workspace, c) for c in clients]
    sender.execute("Game.bags={{item_id=2589,quantity=6}}")
    for lua in (sender, receiver):
        lua.execute("Game.event('TRADE_SHOW')")
    assert sender.eval("Game.click(function() return Cennik.Production:Prepare('trade') end)")
    sender.execute("Game.trade={{item_id=2589,quantity=6}};Game.event('TRADE_ACCEPT_UPDATE',1,1)")
    receiver.execute("Game.event('TRADE_ACCEPT_UPDATE',1,1)")
    relay(sender, receiver)
    sender.execute("Game.bags={};Game.event('UI_INFO_MESSAGE',0,ERR_TRADE_COMPLETE);Game.advance(1)")
    receiver.execute("Game.bags={{item_id=2589,quantity=6}};Game.event('UI_INFO_MESSAGE',0,ERR_TRADE_COMPLETE);Game.advance(1)")
    assert receiver.eval("#Cennik.Production.db.events") == 1
    begin_mail(receiver, sender, 2589, 6)
    collect_mail(sender, 2589, 6)
    # Having another order's linen does not restore this returned allocation.
    receiver.execute("Game.bags={{item_id=2589,quantity=6}}")
    assert receiver.eval("Cennik.Production:Available(Cennik.Production:Selected().steps[1])") == 0
    for lua, client in zip((sender, receiver), clients, strict=True):
        save_client(lua, client)
    result = workspace.synchronize(include_scans=False)
    assert all('error' not in r["production_reports"] for r in result["clients"]), result
    assert [p["state"] for t in workspace.production.status()[0]["transfers"] for p in t["parts"]] == ["confirmed", "confirmed"]


def test_prepare_mail_splits_only_required_stack_and_does_not_send(workspace, tmp_path):
    clients, _, _, _ = production_setup(workspace, tmp_path)
    sender = production_lua(workspace, clients[0])
    sender.execute("""
      Game.bags={{item_id=2589,quantity=12}}
      C_Container.SplitContainerItem=function(bag,slot,q)
        assert(Game.hardware,'Split needs click');Game.bags[slot].quantity=Game.bags[slot].quantity-q
        Game.cursor={item_id=2589,quantity=q}
      end
      C_Container.PickupContainerItem=function() error('Expected partial stack') end
      ClickSendMailItemButton=function(slot)
        assert(Game.hardware,'Attach needs click');assert(slot<=12);Game.draft[slot]=Game.cursor;Game.cursor=nil
      end
      Game.sentCalls=0
      local send=SendMail;SendMail=function(...) Game.sentCalls=Game.sentCalls+1;send(...) end
      Game.click(function() Cennik.Production:Prepare('mail') end)
    """)
    assert sender.eval("Game.bags[1].quantity") == 6
    assert sender.eval("Game.draft[1].quantity") == 6
    assert sender.eval("Game.sentCalls") == 0
    assert sender.eval("SendMailNameEditBox:GetText()") == "Crafter-FixtureRealm"


def test_duplicate_attachment_stacks_are_aggregated_for_partial_receipt(workspace, tmp_path):
    clients, _, _, _ = production_setup(workspace, tmp_path)
    sender, receiver = [production_lua(workspace, c) for c in clients]
    begin_mail(sender, receiver, 2589, 6)
    receiver.execute("""
      Game.mail[1].items={{item_id=2589,quantity=3},{item_id=2589,quantity=3}};Game.event('MAIL_INBOX_UPDATE')
      TakeInboxItem(1,1);Game.mail[1].items={{item_id=2589,quantity=3}};Game.bags={{item_id=2589,quantity=3}}
      Game.event('MAIL_INBOX_UPDATE')
    """)
    assert receiver.eval("Cennik.Production.db.events[1].lines[1].quantity") == 3


def test_completed_step_does_not_capture_later_unrelated_craft(workspace, tmp_path):
    clients, order, _, _ = production_setup(workspace, tmp_path)
    crafter = production_lua(workspace, clients[1])
    crafter.execute("""
      local o=Cennik.Production:Selected();local step=o.steps[1]
      Cennik.Production.db.active_step={order_id=o.order_id,step_id=step.step_id}
      Cennik.Production.db.done[step.step_id]=step.crafts
      Cennik.Accounting:Emit('craft',{inputs={Cennik.Accounting:Line(2589,3)},outputs={Cennik.Accounting:Line(4238,1)},
        details={recipe_id=100,successful_casts=1}})
    """)
    assert crafter.eval("Cennik.db.accounting.events[1].details.production_step_id") is None
    assert crafter.eval("Cennik.Production.db.done["+lua_literal(order["steps"][0]["step_id"])+"]") == 2
