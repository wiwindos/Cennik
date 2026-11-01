from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lupa.lua51 import LuaRuntime
from test_workspace import workspace as workspace

from cennik.addon.lua_data import lua_literal, read_saved_variables
from cennik.workspace.accounting import JournalRevision, LedgerEvent
from cennik.workspace.models import MARKET
from cennik.workspace.web import create_app

ROOT = Path(__file__).resolve().parents[1]


def line(item, quantity=1, **extra):
    return {"item_id": item, "quantity": quantity, **extra}


def event(workspace, identity, kind, **extra):
    return {"event_id": identity, "workspace_id": workspace.identity, "market": MARKET,
        "realm": "FixtureRealm", "character": "Main", "observed_at": int(datetime.now(UTC).timestamp())-100+int(identity.split(":")[-1]),
        "kind": kind, **extra}


def accept(workspace, *events):
    return workspace.accounting.accept({"events": list(events)})


def test_partial_sales_keep_stock_cost_and_distinguish_cash_flow(workspace):
    accept(workspace, event(workspace, "e:1", "purchase", outputs=[line(2589, 100)], money_copper=300000,
        task_id="task", strategy_id="simple_craft"),
        event(workspace, "e:2", "craft", inputs=[line(2589, 100)], outputs=[line(4238, 10)]),
        event(workspace, "e:3", "sale", inputs=[line(4238, 6)], money_copper=228000))
    data = workspace.accounting.overview()
    assert data["summary"]["known_profit_copper"] == 48000
    assert data["summary"]["cash_flow_copper"] == -72000
    assert data["summary"]["inventory_cost_copper"] == 120000
    assert data["inventory"][0]["remaining"] == 4
    assert data["parties"][0]["task_id"] == "task"


def test_fifo_uses_actual_purchases_and_preserves_every_copper(workspace):
    accept(workspace, event(workspace, "e:1", "purchase", outputs=[line(1, 3)], money_copper=10),
        event(workspace, "e:2", "purchase", outputs=[line(1, 2)], money_copper=20),
        event(workspace, "e:3", "sale", inputs=[line(1, 4)], money_copper=50),
        event(workspace, "e:4", "sale", inputs=[line(1, 1)], money_copper=50))
    rows = workspace.accounting.overview()["events"]
    assert rows[0]["cost_copper"] == 10 and rows[1]["cost_copper"] == 20
    assert workspace.accounting.overview()["summary"]["known_profit_copper"] == 70


def test_explicit_task_affinity_precedes_general_fifo(workspace):
    accept(workspace, event(workspace, "e:1", "purchase", outputs=[line(1)], money_copper=10, task_id="a"),
        event(workspace, "e:2", "purchase", outputs=[line(1)], money_copper=20, task_id="b"),
        event(workspace, "e:3", "sale", inputs=[line(1)], money_copper=50, task_id="b"))
    assert workspace.accounting.overview()["events"][0]["cost_copper"] == 20


def test_unknown_opening_and_missing_purchase_never_mean_free_materials(workspace):
    accept(workspace, event(workspace, "e:1", "opening", outputs=[line(1, 2)]),
        event(workspace, "e:2", "craft", inputs=[line(1, 3)], outputs=[line(2, 2)]),
        event(workspace, "e:3", "sale", inputs=[line(2)], money_copper=100))
    data = workspace.accounting.overview()
    assert data["summary"]["unknown_sales"] == 1
    assert data["events"][0]["profit_copper"] is None
    assert data["inventory"][0]["cost"] is None


def test_disenchant_actual_outputs_allocate_total_cost_without_rounding_loss(workspace):
    accept(workspace, event(workspace, "e:1", "purchase", outputs=[line(1)], money_copper=101),
        event(workspace, "e:2", "disenchant", inputs=[line(1)], outputs=[line(2, 2, reference_unit_copper=10), line(3, 1, reference_unit_copper=80)]))
    lots = workspace.accounting.overview()["inventory"]
    assert [x["cost"] for x in lots] == [20, 81]
    assert sum(x["cost"] for x in lots) == 101


def test_missing_disenchant_allocation_prices_preserves_unknown_cost(workspace):
    accept(workspace, event(workspace, "e:1", "purchase", outputs=[line(1)], money_copper=100),
        event(workspace, "e:2", "disenchant", inputs=[line(1)], outputs=[line(2), line(3)]))
    data = workspace.accounting.overview()
    assert all(x["cost"] is None for x in data["inventory"])
    assert data["events"][0]["problems"]


def test_transfer_preserves_each_items_cost_and_never_creates_profit(workspace):
    accept(workspace, event(workspace, "e:1", "purchase", outputs=[line(1)], money_copper=10),
        event(workspace, "e:2", "purchase", outputs=[line(2)], money_copper=30),
        event(workspace, "e:3", "transfer", inputs=[line(1), line(2)], outputs=[line(1), line(2)], target_character="Alt"),
        event(workspace, "e:4", "sale", character="Alt", inputs=[line(2)], money_copper=50))
    data = workspace.accounting.overview()
    assert data["summary"]["known_profit_copper"] == 20
    assert data["inventory"][0]["cost"] == 10
    assert data["inventory"][0]["character"] == "Alt"


def test_partial_sale_refunded_deposit_and_expiry_loss_are_not_double_fees(workspace):
    accept(workspace, event(workspace, "e:1", "purchase", outputs=[line(1, 10)], money_copper=100),
        event(workspace, "e:2", "listing", outputs=[line(1, 10)], listing_id="auction", deposit_copper=20),
        event(workspace, "e:3", "sale", inputs=[line(1, 6)], money_copper=114, returned_deposit_copper=12, listing_id="auction"),
        event(workspace, "e:4", "return", outputs=[line(1, 4)], listing_id="auction"))
    data = workspace.accounting.overview()
    assert data["summary"]["known_profit_copper"] == 46  # 114 - 60 - lost deposit 8
    assert data["summary"]["cash_flow_copper"] == 6  # -100 -20 +114 +12
    assert data["summary"]["inventory_cost_copper"] == 40
    assert data["summary"]["deposit_loss_copper"] == 8


def test_retry_is_idempotent_and_conflicting_batch_does_not_partially_apply(workspace):
    a = event(workspace, "e:1", "purchase", outputs=[line(1)], money_copper=10)
    assert accept(workspace, a)["accepted"] == 1
    assert accept(workspace, a)["accepted"] == 0
    b = event(workspace, "e:2", "purchase", outputs=[line(2)], money_copper=20)
    with pytest.raises(ValueError, match="другим содержимым"):
        accept(workspace, b, {**a, "money_copper": 11})
    assert workspace.accounting.overview()["total"] == 1


def test_audited_revision_replays_downstream_profit_and_rejects_stale_editor(workspace):
    a = event(workspace, "e:1", "opening", outputs=[line(1)])
    accept(workspace, a, event(workspace, "e:2", "sale", inputs=[line(1)], money_copper=20))
    assert workspace.accounting.overview()["summary"]["unknown_sales"] == 1
    replacement = LedgerEvent.model_validate({**a, "money_copper": 12})
    revision = JournalRevision(reason="Уточнил начальную стоимость", replacement=replacement)
    workspace.accounting.revise(a["event_id"], revision)
    assert workspace.accounting.revise(a["event_id"], revision)["reused"]
    assert workspace.accounting.overview()["summary"]["known_profit_copper"] == 8
    assert workspace.accounting.load(a["event_id"])["original"]["money_copper"] is None
    with pytest.raises(ValueError, match="уже исправлена"):
        workspace.accounting.revise(a["event_id"], JournalRevision(reason="Другое исправление", replacement=replacement))


def test_review_event_has_no_inventory_or_cash_effect_and_foreign_profile_is_not_acked(workspace):
    a = event(workspace, "e:1", "sale", confirmation="review", inputs=[line(1)], money_copper=100)
    accept(workspace, a, {**a, "event_id": "e:2", "workspace_id": "foreign"})
    data = workspace.accounting.overview()
    assert data["summary"]["known_profit_copper"] == data["summary"]["cash_flow_copper"] == 0
    assert data["total"] == 1
    assert workspace.accounting.wire()["acknowledged_event_ids"] == ["e:1"]


def lua_value(value):
    if hasattr(value, "items"):
        entries = {lua_value(k): lua_value(v) for k, v in value.items()}
        if entries and set(entries) == set(range(1, len(entries)+1)):
            return [entries[i] for i in range(1, len(entries)+1)]
        return entries
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def accounting_lua(workspace):
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute((ROOT / "tests/lua/wow_mock.lua").read_text("utf-8"))
    lua.execute("""
      Game.bags={}; Game.mail={}
      C_Container={GetContainerNumSlots=function(b) return b==0 and 20 or 0 end,
        GetContainerItemInfo=function(b,s) local r=Game.bags[s];return r and {stackCount=r.quantity} end,
        GetContainerItemLink=function(b,s) local r=Game.bags[s];return r and ('item:'..r.item_id) end,
        UseContainerItem=function() end}
      GetNumMerchantItems=function() return 1 end
      GetMerchantItemInfo=function() return 'Vendor',nil,20,2,-1,true,true,false end
      GetMerchantItemLink=function() return 'item:2589' end
      BuyMerchantItem=function() end; SellCursorItem=function() end
      DoTradeSkill=function() end
      GetTradeSkillNumReagents=function() return 1 end
      GetTradeSkillReagentItemLink=function() return 'item:2589' end
      GetTradeSkillReagentInfo=function() return 'Linen',nil,3 end
      GetTradeSkillItemLink=function() return 'item:4238' end
      GetTradeSkillRecipeLink=function() return 'enchant:100' end
      GetInboxNumItems=function() return #Game.mail end
      GetInboxHeaderInfo=function(i) local m=Game.mail[i];if m then return nil,nil,m.sender or 'Auction House',m.subject or 'Auction',m.money or 0,0,30,#(m.items or {}) end end
      GetInboxInvoiceInfo=function(i) local m=Game.mail[i];if m then return m.kind,m.name,'Other',0,m.buyout or 0,m.deposit or 0,m.fee or 0 end end
      GetInboxItemLink=function(i,s) local m=Game.mail[i];local r=m and m.items and m.items[s];return r and ('item:'..r.item_id) end
      GetInboxItem=function(i,s) local m=Game.mail[i];local r=m and m.items and m.items[s];if r then return 'Item',r.item_id,nil,r.quantity end end
      TakeInboxItem=function() end; TakeInboxMoney=function() end; AutoLootMailItem=function() end
      Game.draft={}
      GetSendMailItemLink=function(i) local r=Game.draft[i];return r and ('item:'..r.item_id) end
      GetSendMailItem=function(i) local r=Game.draft[i];if r then return 'Item',r.item_id,nil,r.quantity end end
      GetSendMailMoney=function() return 0 end;GetSendMailPrice=function() return 30 end
      SendMail=function() end
      C_Item.GetItemLink=function(location) return 'item:'..location.itemID end
      C_AuctionHouse.PostCommodity=function() end
      C_AuctionHouse.CalculateCommodityDeposit=function() return 10 end
      GetNumLootItems=function() return 1 end; GetLootSlotLink=function() return 'item:10940' end
      AUCTION_EXPIRED_MAIL_SUBJECT='Expired: %s';AUCTION_REMOVED_MAIL_SUBJECT='Cancelled: %s'
      ns={};function module(source) assert(loadstring(source))('CennikAddon',ns) end
    """)
    for file in (ROOT / "CennikAddon/CennikAddon.toc").read_text().splitlines():
        if file and not file.startswith("#"):
            lua.globals().module((ROOT / "CennikAddon" / file.replace("\\", "/")).read_text("utf-8"))
    incoming = {"format_version": 1, "device_id": "ledger-test", "market_collector": {"workspace_id": workspace.identity},
        "accounting": {"format_version": 1, "workspace_id": workspace.identity, "market": MARKET}}
    lua.execute("CennikImport=" + lua_literal(incoming))
    lua.execute("Game.now="+str(int(datetime.now(UTC).timestamp()))+"; Cennik:Initialize();Cennik:BindMarket('"+MARKET+"')")
    return lua


def test_lua_task_purchase_and_mail_receipt_only_count_once(workspace):
    lua = accounting_lua(workspace)
    lua.execute("""Cennik.Accounting:Purchase('test','item:2589',2,20,{source='auction'})
      Game.mail={{kind='buyer',buyout=20,items={{item_id=2589,quantity=2}}}}
      Game.event('MAIL_SHOW'); TakeInboxItem(1,1); Game.mail={};Game.event('MAIL_INBOX_UPDATE')""")
    accept(workspace, *lua_value(lua.eval("Cennik.db.accounting.events")))
    summary = workspace.accounting.overview()["summary"]
    assert summary["purchase_copper"] == 20 and summary["inventory_cost_copper"] == 20
    assert summary["events"] == 2


def test_lua_vendor_purchase_and_sale_confirm_money_and_inventory_together(workspace):
    lua = accounting_lua(workspace)
    lua.execute("""Game.event('MERCHANT_SHOW'); BuyMerchantItem(1,2)
      Game.bags[1]={item_id=2589,quantity=2};Game.money=Game.money-20
      Game.event('BAG_UPDATE_DELAYED');Game.advance(1)
      Game.vendor={[2589]=15};C_Container.UseContainerItem(0,1)
      Game.bags={};Game.money=Game.money+30;Game.advance(1)""")
    accept(workspace, *lua_value(lua.eval("Cennik.db.accounting.events")))
    data = workspace.accounting.overview()
    assert data["summary"]["known_profit_copper"] == 10
    assert data["summary"]["purchase_copper"] == 20


def test_lua_craft_and_disenchant_use_observed_deltas_not_expected_recipe_yield(workspace):
    lua = accounting_lua(workspace)
    lua.execute("""Cennik.Accounting:Purchase('test',2589,6,60,{source='vendor'})
      Game.bags[1]={item_id=2589,quantity=6};Cennik.Accounting.bags=Cennik.Accounting:Snapshot()
      DoTradeSkill(1,2);Game.event('UNIT_SPELLCAST_SUCCEEDED','player','cast',100)
      Game.bags[1].quantity=3;Game.bags[2]={item_id=4238,quantity=1};Game.event('BAG_UPDATE_DELAYED');Game.advance(1)
      Game.event('UNIT_SPELLCAST_SUCCEEDED','player','cast2',100)
      Game.bags[1]=nil;Game.bags[2].quantity=2;Game.event('BAG_UPDATE_DELAYED');Game.advance(1)
      Game.event('UNIT_SPELLCAST_SENT','player','target','cast3',13262)
      Game.event('UNIT_SPELLCAST_SUCCEEDED','player','cast3',13262);Game.event('LOOT_OPENED')
      Game.bags[2].quantity=1;Game.bags[3]={item_id=10940,quantity=3};Game.event('LOOT_CLOSED');Game.advance(1)""")
    accept(workspace, *lua_value(lua.eval("Cennik.db.accounting.events")))
    rows = workspace.accounting.overview()["events"]
    assert [r["kind"] for r in rows] == ["disenchant", "craft", "craft", "purchase"]
    assert rows[0]["outputs"][0]["quantity"] == 3
    assert rows[0]["cost_copper"] == 30


def test_lua_post_partial_sale_mail_preserves_unsold_stock_and_net_proceeds(workspace):
    lua = accounting_lua(workspace)
    lua.execute("""Cennik.Accounting:Purchase('test',2589,10,100,{source='vendor'})
      Game.bags[1]={item_id=2589,quantity=10};Cennik.Accounting.bags=Cennik.Accounting:Snapshot()
      C_AuctionHouse.PostCommodity({itemID=2589},1,10,20)
      Game.bags={};Game.money=Game.money-10;Game.event('AUCTION_HOUSE_AUCTION_CREATED',42);Game.advance(1)
      local listing=Cennik.Accounting.db.listings['42']
      Game.mail={{kind='seller',name=listing.name,buyout=120,deposit=6,fee=6,money=120}}
      Game.event('MAIL_SHOW');TakeInboxMoney(1);Game.mail={};Game.event('MAIL_INBOX_UPDATE')""")
    accept(workspace, *lua_value(lua.eval("Cennik.db.accounting.events")))
    data = workspace.accounting.overview()
    assert data["summary"]["known_profit_copper"] == 54
    assert data["inventory"][0]["remaining"] == 4
    assert data["events"][0]["inputs"][0]["quantity"] == 6


def test_lua_reload_retains_unacked_events_and_only_removes_explicit_acks(workspace):
    lua = accounting_lua(workspace)
    lua.execute("Cennik.Accounting:Purchase('test',2589,2,20,{source='vendor'});Cennik.Accounting:Initialize()")
    assert lua.eval("#Cennik.db.accounting.events") == 1
    lua.execute("CennikImport.accounting.acknowledged_event_ids={Cennik.db.accounting.events[1].event_id};Cennik.Accounting:Initialize()")
    assert lua.eval("#Cennik.db.accounting.events") == 0


def test_same_second_sequence_does_not_sort_tenth_event_before_second(workspace):
    now = int(datetime.now(UTC).timestamp())-1
    rows = [event(workspace, "device:ledger:2", "purchase", outputs=[line(1)], money_copper=10, observed_at=now),
        event(workspace, "device:ledger:10", "sale", inputs=[line(1)], money_copper=30, observed_at=now)]
    accept(workspace, *reversed(rows))
    assert workspace.accounting.overview()["summary"]["known_profit_copper"] == 20


def test_lua_confirmed_own_mail_transfers_cost_without_a_second_purchase(workspace):
    lua = accounting_lua(workspace)
    lua.execute("""Cennik.Accounting:Purchase('test',2589,2,20,{source='vendor'})
      Game.bags[1]={item_id=2589,quantity=2};Cennik.Accounting.bags=Cennik.Accounting:Snapshot()
      Cennik.db.accounting_characters['FixtureRealm:Alt']=true
      Game.draft={{item_id=2589,quantity=2}};Game.event('MAIL_SEND_INFO_UPDATE');SendMail('Alt','subject','body')
      Game.bags={};Game.money=Game.money-30;Game.event('MAIL_SEND_SUCCESS');Game.advance(1)""")
    accept(workspace, *lua_value(lua.eval("Cennik.db.accounting.events")))
    data = workspace.accounting.overview()
    assert data["inventory"][0]["character"] == "Alt"
    assert data["inventory"][0]["cost"] == 20
    assert data["summary"]["purchase_copper"] == 20
    assert data["summary"]["known_profit_copper"] == -30


def test_lua_cancelled_or_failed_mail_does_not_create_sale_and_reload_does_not_report_finished_craft(workspace):
    lua = accounting_lua(workspace)
    lua.execute("""Game.mail={{kind='seller',name='Unknown',buyout=100,fee=5,money=95}}
      Game.event('MAIL_SHOW');TakeInboxMoney(1);Game.event('MAIL_FAILED');Game.mail={};Game.event('MAIL_INBOX_UPDATE')""")
    assert lua.eval("#Cennik.db.accounting.events") == 1
    assert lua.eval("Cennik.db.accounting.events[1].kind") == "issue"


def test_lua_interrupted_transform_requires_review_without_synthetic_output(workspace):
    lua = accounting_lua(workspace)
    lua.execute("""Game.bags[1]={item_id=2589,quantity=3};DoTradeSkill(1,1)
      Game.event('UNIT_SPELLCAST_SUCCEEDED','player','cast',100)
      Cennik.Accounting:Initialize()""")
    assert lua.eval("Cennik.db.accounting.events[1].kind") == "issue"


def test_lua_more_than_five_hundred_events_are_retained_and_batched(workspace):
    lua = accounting_lua(workspace)
    lua.execute("for i=1,510 do Cennik.Accounting:Emit('observation',{reason='fixture'}) end")
    raw = lua_value(lua.eval("Cennik.db.accounting"))
    assert len(raw["events"]) == 510 and len(raw["outbox"]) == 500
    assert workspace.accounting.accept(raw)["accepted"] == 500
    lua.execute("CennikImport.accounting="+lua_literal(workspace.accounting.wire())+";Cennik.Accounting:Initialize()")
    assert lua.eval("#Cennik.db.accounting.events") == 10
    assert workspace.accounting.accept(lua_value(lua.eval("Cennik.db.accounting")))["accepted"] == 10


def test_exclusion_keeps_original_and_can_be_restored_by_revision(workspace):
    a = event(workspace, "e:1", "purchase", outputs=[line(1)], money_copper=10)
    accept(workspace, a)
    workspace.accounting.revise("e:1", JournalRevision(reason="Ошибочная запись"))
    assert workspace.accounting.overview()["summary"]["purchase_copper"] == 0
    assert workspace.accounting.overview()["events"][0]["details"]["excluded"]
    workspace.accounting.revise("e:1", JournalRevision(reason="Вернул верную запись", expected_revision=1, replacement=LedgerEvent.model_validate(a)))
    assert workspace.accounting.overview()["summary"]["purchase_copper"] == 10


def test_lua_mail_metadata_update_is_not_a_collection(workspace):
    lua = accounting_lua(workspace)
    lua.execute("""Game.mail={{kind='buyer',buyout=10,items={{item_id=2589,quantity=1}}}}
      Game.event('MAIL_SHOW');TakeInboxItem(1,1)
      Game.mail[1].buyout=20;Game.event('MAIL_INBOX_UPDATE')""")
    assert lua.eval("#Cennik.db.accounting.events") == 0
    lua.execute("Game.mail={};Game.event('MAIL_INBOX_UPDATE')")
    assert lua.eval("Cennik.db.accounting.events[1].money_copper") == 20


def test_lua_external_commodity_purchase_and_regular_task_hook_capture_once(workspace):
    lua = accounting_lua(workspace)
    lua.execute("""Game.click(function() C_AuctionHouse.StartCommoditiesPurchase(2589,2) end)
      Game.event('COMMODITY_PRICE_UPDATED',10,20)
      Game.click(function() C_AuctionHouse.ConfirmCommoditiesPurchase(2589,2) end)
      Game.event('COMMODITY_PURCHASE_SUCCEEDED')
      Cennik:Emit({task_id='task',opportunity_id='op',run_id='run',market='"""+MARKET+"""',strategy_id='buy_disenchant'},
        'purchase','bought',{},2589,1,10)""")
    accept(workspace, *lua_value(lua.eval("Cennik.db.accounting.events")))
    assert workspace.accounting.overview()["summary"]["purchase_copper"] == 30
    assert workspace.accounting.overview()["total"] == 2


def test_lua_own_mail_receipt_does_not_duplicate_transferred_inventory(workspace):
    lua = accounting_lua(workspace)
    lua.execute("""Cennik.Accounting:Purchase('test',2589,2,20,{source='vendor'})
      Game.bags[1]={item_id=2589,quantity=2};Cennik.Accounting.bags=Cennik.Accounting:Snapshot()
      Cennik.db.accounting_characters['FixtureRealm:Alt']=true
      Game.draft={{item_id=2589,quantity=2}};Game.event('MAIL_SEND_INFO_UPDATE');SendMail('Alt','subject','body')
      Game.bags={};Game.money=Game.money-30;Game.event('MAIL_SEND_SUCCESS');Game.advance(1)
      UnitName=function() return 'Alt' end;Cennik:BindMarket('"""+MARKET+"""')
      Game.mail={{sender='FixturePlayer',items={{item_id=2589,quantity=2}}}}
      Game.event('MAIL_SHOW');TakeInboxItem(1,1);Game.mail={};Game.event('MAIL_INBOX_UPDATE')""")
    accept(workspace, *lua_value(lua.eval("Cennik.db.accounting.events")))
    inventory = workspace.accounting.overview()["inventory"]
    assert len(inventory) == 1 and inventory[0]["remaining"] == 2
    assert inventory[0]["cost"] == 20 and inventory[0]["character"] == "Alt"


def test_lua_cancelled_auction_return_loses_deposit_without_second_stock_receipt(workspace):
    lua = accounting_lua(workspace)
    lua.execute("""Cennik.Accounting:Purchase('test',2589,4,40,{source='vendor'})
      Game.bags[1]={item_id=2589,quantity=4};Cennik.Accounting.bags=Cennik.Accounting:Snapshot()
      C_AuctionHouse.PostCommodity({itemID=2589},1,4,20)
      Game.bags={};Game.money=Game.money-10;Game.event('AUCTION_HOUSE_AUCTION_CREATED',42);Game.advance(1)
      Game.event('AUCTION_CANCELED',42)
      Game.mail={{subject=string.format(AUCTION_REMOVED_MAIL_SUBJECT,Cennik.Accounting.db.listings['42'].name),items={{item_id=2589,quantity=4}}}}
      Game.event('MAIL_SHOW');TakeInboxItem(1,1);Game.mail={};Game.event('MAIL_INBOX_UPDATE')""")
    accept(workspace, *lua_value(lua.eval("Cennik.db.accounting.events")))
    data = workspace.accounting.overview()
    assert data["summary"]["deposit_loss_copper"] == 10
    assert data["summary"]["inventory_cost_copper"] == 40
    assert data["inventory"][0]["remaining"] == 4


def test_ambiguous_variant_sale_marks_stock_and_later_cost_as_unknown_until_revision(workspace):
    accept(workspace, event(workspace, "e:1", "purchase", outputs=[line(1, variant="a")], money_copper=10),
        event(workspace, "e:2", "purchase", outputs=[line(1, variant="b")], money_copper=20),
        event(workspace, "e:3", "sale", inputs=[line(1)], money_copper=30),
        event(workspace, "e:4", "sale", inputs=[line(1, variant="b")], money_copper=30))
    data = workspace.accounting.overview()
    assert data["summary"]["unknown_sales"] == 2
    assert data["inventory"][0]["quantity_uncertain"]
    assert data["inventory"][0]["cost"] is None


def test_literal_sync_routes_and_manual_revision_on_isolated_profile(tmp_path):
    app = create_app(tmp_path / "profile")
    workspace = app.state.workspace
    addon = tmp_path / "game/Interface/AddOns/CennikAddon"
    addon.mkdir(parents=True)
    (addon / "CennikAddon.toc").write_text("ImportedData.lua", encoding="utf-8")
    saved = tmp_path / "game/WTF/Account/test/SavedVariables/CennikAddon.lua"
    saved.parent.mkdir(parents=True)
    workspace.game_paths = lambda: (addon, saved)
    lua = accounting_lua(workspace)
    lua.execute("Cennik.Accounting:Purchase('test',2589,2,20,{source='vendor'})")
    saved.write_text("CennikAddonDB="+lua_literal(lua_value(lua.globals().CennikAddonDB)), encoding="utf-8")
    try:
        assert workspace.synchronize(include_scans=False)["accounting_reports"]["accepted"] == 1
        assert workspace.synchronize(include_scans=False)["accounting_reports"]["accepted"] == 0
        parsed = read_saved_variables(saved.read_text("utf-8"))
        assert parsed["accounting"]["events"]
        assert "accounting" in (addon / "ImportedData.lua").read_text("utf-8")
        broken = lua_value(lua.globals().CennikAddonDB)
        broken["accounting"]["outbox"][0]["outputs"][0]["quantity"] = -1
        saved.write_text("CennikAddonDB="+lua_literal(broken), encoding="utf-8")
        assert "error" in workspace.synchronize(include_scans=False)["accounting_reports"]
        assert workspace.accounting.overview()["capture_status"]["error"]
        saved.write_text("CennikAddonDB="+lua_literal(lua_value(lua.globals().CennikAddonDB)), encoding="utf-8")
        assert workspace.synchronize(include_scans=False)["accounting_reports"]["accepted"] == 0
        with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1234)) as client:
            data = client.get("/api/v2/accounting").json()
            assert data["summary"]["purchase_copper"] == 20
            identity = data["events"][0]["event_id"]
            raw = client.get("/api/v2/accounting/events/"+identity).json()["original"]
            raw["money_copper"] = 30
            response = client.post("/api/v2/accounting/events/"+identity+"/revise", json={"reason": "Стоимость уточнена", "replacement": raw})
            assert response.status_code == 200
            assert client.get("/api/v2/accounting").json()["summary"]["inventory_cost_copper"] == 30
    finally:
        if not workspace.stop.is_set():
            workspace.close()
