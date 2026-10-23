from pathlib import Path

import pytest
from lupa.lua51 import LuaRuntime
from test_addon import exchange as exchange
from test_addon import exported
from test_cennik import analytics_root as analytics_root

from cennik.addon.lua_data import lua_literal

ROOT = Path(__file__).resolve().parents[1]


def runtime(package):
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute((ROOT / "tests/lua/wow_mock.lua").read_text(encoding="utf-8"))
    lua.execute('ns={}; function module(source) assert(loadstring(source))("CennikAddon",ns) end')
    for line in (ROOT / "CennikAddon/CennikAddon.toc").read_text().splitlines():
        if line and not line.startswith("#"):
            lua.globals().module(
                (ROOT / "CennikAddon" / line.replace("\\", "/")).read_text(encoding="utf-8")
            )
    lua.execute(
        "CennikImport="
        + lua_literal(
            {
                "format_version": 1,
                "device_id": "test-device",
                "packages": [package.model_dump(mode="json")],
            }
        )
    )
    lua.execute(f"Game.now={int(package.created_at.timestamp()) + 1}; Cennik:Initialize()")
    lua.execute(
        't=Cennik.db.tasks[next(Cennik.db.tasks)]; Cennik.db.binding={market=t.market,realm=GetRealmName(),faction="alliance",character=UnitName("player")}; t.conditions_confirmed=true'
    )
    lua.execute('Game.vendor={[t.item_id]=t.opportunity.details.vendor_sell_copper}; for _,line in ipairs(t.lines) do if line.source=="npc" then Cennik.NPC:Manual(t,line.item_id,line.unit_limit_copper) end end')
    task = package.tasks[0]
    prices = {
        task.item_id: task.opportunity.details.get(
            "result_unit_price_copper", task.opportunity.buy_price_copper
        )
    }
    for line in task.lines:
        if line.source == "auction":
            prices[line.item_id] = line.unit_limit_copper
    lua.execute(
        "Game.commodity="
        + lua_literal(
            {
                item: [{"quantity": 100, "unitPrice": price, "numOwnerItems": 0}]
                for item, price in prices.items()
            }
        )
    )
    return lua


def scan(lua, prepare=False):
    lua.execute("Cennik.BuyQueue:Prepare()" if prepare else "Cennik.Scanner:Start(t)")
    ids = lua.eval("Cennik.Scanner.ids")
    if ids is None:
        return
    for index in range(1, len(ids) + 1):
        lua.globals().Game.event("COMMODITY_SEARCH_RESULTS_UPDATED", ids[index])


def click(lua, method):
    return lua.eval(
        "function() return Game.click(function() return Cennik.Execution:" + method + "() end) end"
    )()


@pytest.mark.parametrize(
    "strategy", ["vendor_arbitrage", "historical_discount", "simple_craft", "craft_graph"]
)
def test_lua_validates_four_exported_strategies_and_keeps_frozen_plan(exchange, strategy):
    lua = runtime(exported(exchange, strategy))
    scan(lua)
    assert lua.eval("t.state") == ("not_profitable" if strategy == "historical_discount" else "ready_to_buy")
    assert lua.eval("t.validation.profit_copper") < 0 if strategy == "historical_discount" else lua.eval("t.validation.profit_copper") > 0
    assert lua.eval("t.opportunity.execution_status") == "indicative"
    if strategy == "craft_graph":
        assert lua.eval("#t.opportunity.details.steps") == 2
        assert lua.eval("t.opportunity.details.steps[1].recipe.version_hash") == "bar-v1"
        assert lua.eval("t.validation.npc_cost_copper") == 5


def test_commodity_separate_click_confirmation_actual_event_and_repeat_protection(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    scan(lua)
    lua.execute("Cennik.BuyQueue:Add(t)")
    scan(lua, prepare=True)
    assert click(lua, "Start")
    assert lua.eval("#Game.calls") >= 3
    assert lua.eval("t.spent") == 0
    lua.execute('Game.event("COMMODITY_PRICE_UPDATED",50,50)')
    assert lua.eval("Cennik.db.pending.stage") == "quoted"
    assert lua.eval("Game.calls[#Game.calls].kind") == "start"  # no automatic confirm
    assert click(lua, "Confirm")
    assert lua.eval("t.spent") == 0
    lua.execute(
        'Game.event("COMMODITY_PURCHASE_SUCCEEDED"); Game.event("COMMODITY_PURCHASE_SUCCEEDED")'
    )
    assert lua.eval("t.state") == "completed" and lua.eval("t.spent") == 50
    assert lua.eval("t.bought[t.item_id]") == 1
    lua.execute("Cennik:Import()")
    assert lua.eval("t.spent") == 50
    assert not click(lua, "Start")


def test_quote_price_increase_cancels_and_cannot_exceed_original_limit(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    scan(lua)
    lua.execute("Cennik.BuyQueue:Add(t)")
    scan(lua, prepare=True)
    click(lua, "Start")
    lua.execute('Game.event("COMMODITY_PRICE_UPDATED",51,51)')
    assert lua.eval("Cennik.db.pending") is None
    assert lua.eval("t.spent") == 0
    assert not click(lua, "Confirm")
    assert lua.eval("Cennik.db.history[#Cennik.db.history-1].state") == "price_changed"


def test_purchase_timeout_late_success_exactly_once_and_reload_block(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    scan(lua)
    lua.execute("Cennik.BuyQueue:Add(t)")
    scan(lua, prepare=True)
    click(lua, "Start")
    lua.execute('Game.event("COMMODITY_PRICE_UPDATED",50,50)')
    click(lua, "Confirm")
    lua.execute("Game.advance(16)")
    assert lua.eval("Cennik.db.pending.stage") == "unknown"
    assert not click(lua, "Start")
    lua.execute("Cennik:Initialize()")
    assert lua.eval("Cennik.db.pending.stage") == "unknown"
    lua.execute(
        'Game.event("COMMODITY_PURCHASE_SUCCEEDED"); Game.event("COMMODITY_PURCHASE_SUCCEEDED")'
    )
    assert lua.eval("t.spent") == 50


def test_hardware_action_required_and_foreign_event_cannot_confirm_purchase(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    scan(lua)
    lua.execute("Cennik.BuyQueue:Add(t)")
    scan(lua, prepare=True)
    assert not lua.eval("Cennik.Execution:Start()")
    assert lua.eval("Cennik.db.pending.stage") == "unknown"
    lua.execute("Cennik.Execution:Reconcile(false)")
    scan(lua, prepare=True)
    click(lua, "Start")
    lua.execute('Game.event("COMMODITY_PRICE_UPDATED",50,50)')
    click(lua, "Confirm")
    lua.execute(
        'Game.click(function() C_AuctionHouse.PlaceBid(999,10) end); Game.event("COMMODITY_PURCHASE_SUCCEEDED")'
    )
    assert lua.eval("t.spent") == 0 and lua.eval("Cennik.db.pending.external_conflict")


@pytest.mark.parametrize(
    "change,expected",
    [
        ("Game.commodity[t.item_id][1].quantity=0", "insufficient_supply"),
        ("Game.commodity[t.item_id][1].unitPrice=51", "limits_exceeded"),
        ("Game.commodity[t.item_id][1].numOwnerItems=nil", "requires_manual_check"),
        ("Game.money=1", "requires_manual_check"),
        ('Cennik.db.binding.market="other"', "requires_manual_check"),
        ('t.run_id="old-run"', "requires_manual_check"),
        ("t.expires_at_epoch=Game.now", "requires_manual_check"),
    ],
)
def test_missing_supply_limits_money_market_run_and_expiry(exchange, change, expected):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    lua.execute(change)
    scan(lua)
    assert lua.eval("t.state") == expected
    assert lua.eval("t.spent") == 0


def test_insufficient_low_price_depth_and_partial_craft_are_not_completed(exchange):
    lua = runtime(exported(exchange, "simple_craft"))
    lua.execute(
        "Game.commodity[10]={{quantity=1,unitPrice=30,numOwnerItems=0},{quantity=5,unitPrice=31,numOwnerItems=0}}"
    )
    scan(lua)
    assert lua.eval("t.state") == "limits_exceeded"
    assert lua.eval("t.validation.cost_copper") == 66
    lua.execute("Game.commodity[10][2].unitPrice=30; t.conditions_confirmed=false")
    scan(lua)
    assert lua.eval("t.state") == "requires_manual_check"
    lua.execute("t.conditions_confirmed=true")
    scan(lua)
    assert lua.eval("t.state") == "ready_to_buy"
    lua.execute("t.bought[10]=1; t.spent=30; Cennik.db.packages[t.package_id].spent=30")
    scan(lua)
    assert lua.eval("t.state") == "ready_to_buy"
    assert lua.eval("t.validation.plans[1].quantity") == 1


def test_zero_gold_keeps_scan_economics_but_cannot_queue_or_buy(exchange):
    lua = runtime(exported(exchange, "simple_craft"))
    lua.execute("Game.money=0")
    scan(lua)
    assert lua.eval("t.state") == "requires_manual_check"
    assert lua.eval("t.validation.cost_copper") > 0
    assert lua.eval("t.validation.profit_copper") > 0
    assert lua.eval("Cennik.BuyQueue:Add(t)")[0] is False
    assert click(lua, "Start") is False
    assert lua.eval("t.spent") == 0


def test_item_group_plans_depth_but_revalidates_and_buys_one_id_at_a_time(exchange):
    lua = runtime(exported(exchange, "simple_craft", unit_limits={10: 45}))
    lua.execute(
        "Game.items[10]={{quantity=3,buyoutAmount=30,auctionID=77,containsOwnerItem=false,itemKey=C_AuctionHouse.MakeItemKey(10)}}"
    )
    lua.execute(
        'Cennik.Scanner:Start(t); Game.event("COMMODITY_SEARCH_RESULTS_UPDATED",20); Game.event("ITEM_SEARCH_RESULTS_UPDATED",C_AuctionHouse.MakeItemKey(10))'
    )
    assert lua.eval("t.state") == "ready_to_buy"
    assert lua.eval("t.validation.cost_copper") == 65
    lua.execute(
        'Cennik.BuyQueue:Add(t); Cennik.BuyQueue:Prepare(); Game.event("COMMODITY_SEARCH_RESULTS_UPDATED",20); Game.event("ITEM_SEARCH_RESULTS_UPDATED",C_AuctionHouse.MakeItemKey(10))'
    )
    assert click(lua, "Start")
    assert lua.eval("Game.calls[#Game.calls].kind") == "bid"
    lua.execute('Game.event("AUCTION_HOUSE_NEW_BID_RECEIVED",77)')
    assert lua.eval("t.spent") == 0
    lua.execute('Game.event("AUCTION_HOUSE_PURCHASE_COMPLETED",77)')
    assert lua.eval("t.spent") == 30 and lua.eval("t.bought[10]") == 1
    lua.execute('Game.items[10][1].auctionID=78; Game.items[10][1].quantity=2; Cennik.BuyQueue:Prepare(); Game.event("COMMODITY_SEARCH_RESULTS_UPDATED",20); Game.event("ITEM_SEARCH_RESULTS_UPDATED",C_AuctionHouse.MakeItemKey(10))')
    assert click(lua, "Start")
    lua.execute('Game.event("AUCTION_HOUSE_PURCHASE_COMPLETED",78)')
    assert lua.eval("t.spent") == 60 and lua.eval("t.bought[10]") == 2


def test_variant_unknown_partial_results_throttle_and_scan_timeout(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    lua.execute("Game.gear[t.item_id]=true")
    scan(lua)
    assert lua.eval("t.state") == "requires_manual_check"
    lua.execute("t.selected_keys[t.item_id]=C_AuctionHouse.MakeItemKey(t.item_id); Game.full=false")
    scan(lua)
    assert lua.eval("t.state") == "scanning"
    lua.execute("Game.advance(31)")
    assert lua.eval("t.state") == "scan_failed"
    lua.execute("Game.full=true; Game.throttled=true; Cennik.Scanner:Start(t)")
    assert lua.eval("t.state") == "scan_failed"


def test_ui_tab_three_sections_pool_and_immutable_report_snapshots(exchange):
    lua = runtime(exported(exchange, "craft_graph"))
    scan(lua)
    lua.execute("Cennik.UI:Attach(); Cennik.UI:Refresh()")
    count = lua.eval("#Game.frames")
    lua.execute("for i=1,50 do Cennik.UI:Refresh() end")
    assert lua.eval("#Game.frames") == count
    lua.execute(
        'Cennik.UI.section="Buy Queue"; Cennik.UI:Refresh(); Cennik.UI.section="History"; Cennik.UI:Refresh()'
    )
    lua.execute(
        'snapshot=Cennik:Emit(t,"scan","price_checked",{scans=t.scans}); t.scans[t.item_id].complete=false'
    )
    assert lua.eval("snapshot.details.scans[t.item_id].complete")


def test_integer_fee_and_discount_boundary(exchange):
    lua = runtime(exported(exchange, "historical_discount"))
    assert lua.eval("Cennik.Validator:Part(433,15,100)") == 64
    lua.execute(
        "t.opportunity.details.history.copper=199; t.lines[1].unit_limit_copper=170; t.max_total_cost_copper=170; Game.commodity[t.item_id][1].unitPrice=170"
    )
    scan(lua)
    assert lua.eval("t.state") == "not_profitable"  # 170/199 does not reach 15% discount


def test_item_lot_price_change_prevents_submitting_bid(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    lua.execute(
        'Game.items[t.item_id]={{quantity=1,buyoutAmount=50,auctionID=77,containsOwnerItem=false,itemKey=C_AuctionHouse.MakeItemKey(t.item_id)}}; Cennik.Scanner:Start(t); Game.event("ITEM_SEARCH_RESULTS_UPDATED",C_AuctionHouse.MakeItemKey(t.item_id)); Cennik.BuyQueue:Add(t); Cennik.BuyQueue:Prepare(); Game.event("ITEM_SEARCH_RESULTS_UPDATED",C_AuctionHouse.MakeItemKey(t.item_id)); Game.items[t.item_id][1].buyoutAmount=51'
    )
    assert not click(lua, "Start")
    assert lua.eval("t.spent") == 0
    assert lua.eval("Cennik.db.pending") is None


def test_renew_import_preserves_partial_spending_and_event_identity(exchange):
    package = exported(exchange, "simple_craft")
    lua = runtime(package)
    lua.execute(
        't.bought[10]=1; t.spent=30; Cennik.db.packages[t.package_id].spent=30; t.allow_overbuy=true; Cennik:State(t,"partially_bought")'
    )
    renewed = exchange.renew(package.package_id)
    lua.execute(
        "CennikImport="
        + lua_literal(
            {
                "format_version": 1,
                "device_id": "test-device",
                "packages": [renewed.model_dump(mode="json")],
            }
        )
    )
    lua.execute("Cennik:Import()")
    assert lua.eval("t.bought[10]") == 1 and lua.eval("t.spent") == 30
    assert lua.eval("Cennik.db.packages[t.package_id].spent") == 30
    assert lua.eval("t.allow_overbuy")


def test_other_commodity_result_and_ah_close_do_not_resume_queue(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    scan(lua)
    lua.execute("Cennik.BuyQueue:Add(t)")
    scan(lua, prepare=True)
    click(lua, "Start")
    lua.execute('Game.event("COMMODITY_PRICE_UPDATED",50,50)')
    click(lua, "Confirm")
    lua.execute('Game.event("COMMODITY_PURCHASE_SUCCEEDED",999)')
    assert lua.eval("t.spent") == 0
    lua.execute('Game.event("AUCTION_HOUSE_CLOSED")')
    assert lua.eval("Cennik.db.pending.stage") == "unknown"
    assert lua.eval("Cennik.db.queue_stopped")


def test_external_reservation_cancels_our_unconfirmed_quote(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    scan(lua)
    lua.execute("Cennik.BuyQueue:Add(t)")
    scan(lua, prepare=True)
    click(lua, "Start")
    lua.execute("Game.click(function() C_AuctionHouse.StartCommoditiesPurchase(999,1) end)")
    assert lua.eval("Cennik.db.pending") is None
    assert lua.eval("t.spent") == 0
    assert not click(lua, "Confirm")


def test_first_bridge_import_adopts_device_after_empty_addon_load(exchange):
    package = exported(exchange, "vendor_arbitrage")
    lua = runtime(package)
    lua.execute(
        "CennikAddonDB=nil; CennikImport={format_version=1,packages={}}; Cennik:Initialize()"
    )
    assert lua.eval("Cennik.db.device_id").startswith("offline-")
    lua.execute(
        "CennikImport="
        + lua_literal(
            {
                "format_version": 1,
                "device_id": "new-bridge",
                "packages": [package.model_dump(mode="json")],
            }
        )
    )
    lua.execute("Cennik:Import()")
    assert lua.eval("Cennik.db.device_id") == "new-bridge"
    assert lua.eval("Cennik.db.tasks[next(Cennik.db.tasks)].task_id") == package.tasks[0].task_id


def test_client_localized_names_links_icons_and_search_keep_original_ids_and_task(exchange):
    package = exported(exchange, "simple_craft")
    lua = runtime(package)
    original = package.tasks[0].item_name
    lua.execute('Game.localized[t.item_id]="Льняной рулон"; for _,line in ipairs(t.lines) do Game.localized[line.item_id]="Материал " .. line.item_id end')
    lua.execute("Cennik.UI:Attach(); Cennik.UI.focus=t.task_id; Cennik.UI:Refresh()")
    text = lua.eval("Cennik.UI:Details(t)")
    assert "Льняной рулон" in text and "Материал" in text
    assert original not in text
    assert "|Hitem:" in text
    assert lua.eval("Cennik.Items:Label(t.item_id)").startswith("|T12345:16:")
    assert lua.eval("Cennik.Adapter:Shopping(t.item_id)")[0]
    assert lua.eval("Game.shopping[1]") == "Льняной рулон"
    assert lua.eval("t.item_name") == original
    assert lua.eval("t.lines[1].item_id") == package.tasks[0].lines[0].item_id
    # Client names can change with a locale change; no saved translation cache wins.
    lua.execute('Game.localized[t.item_id]="Linen Bolt"; Cennik.UI:Refresh()')
    assert "Linen Bolt" in lua.eval("Cennik.UI:Details(t)")
    assert "Льняной рулон" not in lua.eval("Cennik.UI:Details(t)")
    assert lua.eval("t.item_id") == package.tasks[0].item_id


@pytest.mark.parametrize("event", ["ITEM_DATA_LOAD_RESULT", "GET_ITEM_INFO_RECEIVED"])
def test_uncached_name_loads_once_and_automatically_starts_localized_shopping(exchange, event):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    lua.execute("Game.missingInfo[t.item_id]=true; Cennik.UI:Attach(); Cennik.UI.focus=t.task_id; Cennik.UI:Refresh()")
    assert "загрузка" in lua.eval("Cennik.UI:Details(t)")
    assert lua.eval("Cennik.Adapter:Shopping(t.item_id)")[0]
    lua.execute("Game.advance(0.5)")
    assert lua.eval("Game.shopping") is None
    assert lua.eval("#Game.itemRequests") == 1
    lua.execute('Game.localized[t.item_id]="Льняная ткань"; Game.missingInfo[t.item_id]=false')
    lua.globals().Game.event(event, lua.eval("t.item_id"), True)
    assert lua.eval("Game.shopping[1]") == "Льняная ткань"
    assert lua.eval("Cennik.Items.pending[t.item_id]") is None
    lua.execute("Game.advance(1)")
    assert "Льняная ткань" in lua.eval("Cennik.UI:Details(t)")
    assert lua.eval("#Game.itemRequests") == 1
    assert lua.eval("t.spent") == 0


def test_uncached_item_type_waits_then_scans_by_id_without_search_text(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    lua.execute("Game.missingInfo[t.item_id]=true; Cennik.Scanner:Start(t)")
    assert lua.eval("t.state") == "scanning"
    assert lua.eval("#Game.calls") == 0
    lua.execute('Game.localized[t.item_id]="Льняная ткань"; Game.missingInfo[t.item_id]=false; Game.event("ITEM_DATA_LOAD_RESULT",t.item_id,true)')
    assert lua.eval("Game.calls[#Game.calls].key.itemID") == lua.eval("t.item_id")
    lua.execute('Game.event("COMMODITY_SEARCH_RESULTS_UPDATED",t.item_id)')
    assert lua.eval("t.state") == "ready_to_buy"


def test_waiting_for_next_item_cannot_accept_duplicate_previous_search_result(exchange):
    lua = runtime(exported(exchange, "simple_craft"))
    lua.execute("Game.missingInfo[10]=true; Cennik.Scanner:Start(t)")
    lua.execute('Game.event("COMMODITY_SEARCH_RESULTS_UPDATED",t.item_id); Game.event("COMMODITY_SEARCH_RESULTS_UPDATED",t.item_id)')
    assert lua.eval("Cennik.Scanner.index") == 2
    assert lua.eval("Cennik.Scanner.current") is None
    lua.execute('Game.missingInfo[10]=false; Game.event("ITEM_DATA_LOAD_RESULT",10,true); Game.event("COMMODITY_SEARCH_RESULTS_UPDATED",10)')
    assert lua.eval("t.state") == "ready_to_buy"


def test_item_load_timeout_and_explicit_retry_do_not_search_english_fallback(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    lua.execute("Game.missingInfo[t.item_id]=true; Cennik.UI:Attach(); Cennik.Adapter:Shopping(t.item_id); Game.advance(16)")
    assert lua.eval("Game.shopping") is None
    assert "Не удалось загрузить" in lua.eval("Cennik.UI.notice")
    assert "не загружен" in lua.eval("Cennik.Items:Name(t.item_id)")
    lua.execute("Game.advance(20)")
    assert lua.eval("#Game.itemRequests") == 1
    lua.execute('Cennik.Adapter:Shopping(t.item_id); Game.localized[t.item_id]="Льняная ткань"; Game.missingInfo[t.item_id]=false; Game.event("ITEM_DATA_LOAD_RESULT",t.item_id,true)')
    assert lua.eval("Game.shopping[1]") == "Льняная ткань"
    assert lua.eval("#Game.itemRequests") == 2


def test_pending_item_load_cannot_open_search_after_ah_close_or_override_new_request(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    lua.execute("Game.missingInfo[t.item_id]=true; Cennik.Adapter:Shopping(t.item_id); AuctionHouseFrame:Hide()")
    lua.execute('Game.missingInfo[t.item_id]=false; Game.event("ITEM_DATA_LOAD_RESULT",t.item_id,true)')
    assert lua.eval("Game.shopping") is None
    assert "закрыт" in lua.eval("Cennik.UI.notice")
    lua.execute("AuctionHouseFrame:Show(); Game.missingInfo[t.item_id]=true; Cennik.Adapter:Shopping(t.item_id); Cennik.Adapter:Shopping(999)")
    lua.execute('Game.missingInfo[t.item_id]=false; Game.event("ITEM_DATA_LOAD_RESULT",t.item_id,true)')
    assert lua.eval("Game.shopping[1]") == "Item 999"


def test_local_item_labels_in_history_and_limits_and_recycled_search_button_is_enabled(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    lua.execute('Game.localized[t.item_id]="Льняная ткань"; Cennik.UI:Attach(); Cennik.UI.focus=t.task_id; Cennik:State(t,"pending_scan"); Cennik.UI.section="History"; Cennik.UI:Refresh()')
    assert lua.eval('function() for _,e in ipairs(Cennik.UI.used) do if type(e.widget.text)=="string" and e.widget.text:find("Льняная ткань") then return true end end return false end')()
    lua.execute('Cennik.UI.limitTask=t.task_id; Cennik.UI.section="Limits"; Cennik.UI:Refresh()')
    assert lua.eval('function() for _,e in ipairs(Cennik.UI.used) do if type(e.widget.text)=="string" and e.widget.text:find("Льняная ткань") then return true end end return false end')()
    lua.execute('Cennik.UI.section="Buy Queue"; Cennik.UI:Refresh(); for _,e in ipairs(Cennik.UI.used) do if e.widget.kind=="Button" then e.widget:Disable() end end; Cennik.UI.section="Opportunities"; Cennik.UI:Refresh()')
    assert lua.eval('function() for _,e in ipairs(Cennik.UI.used) do if e.widget.text=="Поиск" then return e.widget:IsEnabled() end end return false end')()


def test_old_item_load_cannot_restart_search_after_closing_and_reopening_ah(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    lua.execute('Game.missingInfo[t.item_id]=true; Cennik.Adapter:Shopping(t.item_id); AuctionHouseFrame:Hide(); Game.event("AUCTION_HOUSE_CLOSED"); AuctionHouseFrame:Show()')
    lua.execute('Game.missingInfo[t.item_id]=false; Game.event("ITEM_DATA_LOAD_RESULT",t.item_id,true); Game.advance(1)')
    assert lua.eval("Game.shopping") is None
    assert lua.eval("Cennik.db.pending") is None


def test_shopping_uses_unquoted_client_name_and_rejects_unrelated_item_variant(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    lua.execute('Game.localized[t.item_id]="Разбойничий нагрудник"; Cennik.Adapter:Shopping(t.item_id)')
    assert lua.eval("Game.shoppingMode") == "plain"
    assert lua.eval("Game.shopping[1]") == "Разбойничий нагрудник"
    lua.execute('Cennik.UI:Attach(); Cennik.UI.focus=t.task_id; Cennik.UI:Show(); Cennik.lastSelectedKey=C_AuctionHouse.MakeItemKey(999)')
    lua.execute('for _,e in ipairs(Cennik.UI.used) do if e.widget.text=="Выбрать вариант" then e.widget:GetScript("OnClick")(); break end end')
    assert lua.eval("t.selected_keys[999]") is None
    assert "другой предмет" in lua.eval("Cennik.UI.noticeNote.text")
    lua.execute('Cennik.lastSelectedKey=C_AuctionHouse.MakeItemKey(t.item_id); Cennik.lastSelectedKey.itemSuffix=7; Cennik.UI:Refresh()')
    lua.execute('for _,e in ipairs(Cennik.UI.used) do if e.widget.text=="Выбрать вариант" then e.widget:GetScript("OnClick")(); break end end; Cennik.lastSelectedKey.itemSuffix=99')
    assert lua.eval("t.selected_keys[t.item_id].itemSuffix") == 7
    assert "Вариант подтверждён" in lua.eval("Cennik.UI.noticeNote.text")


def test_market_button_shows_success_and_persistent_character_scope(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    lua.execute('t.market="forever.pvp.alliance.us"; Cennik.db.current_market=t.market; Cennik.db.binding=nil; Cennik.UI:Attach(); Cennik.UI:Show()')
    assert "Подтвердите рынок" in lua.eval("Cennik.UI.bindingNote.text")
    lua.execute('Cennik.UI.bindButton:GetScript("OnClick")()')
    assert lua.eval("Cennik.UI.bindButton.text") == "Рынок подтверждён"
    assert "Рынок подтверждён" in lua.eval("Cennik.UI.noticeNote.text")
    assert "FixturePlayer" in lua.eval("Cennik.UI.bindingNote.text")
    assert "FixtureRealm" in lua.eval("Cennik.UI.bindingNote.text")
    assert "Альянс" in lua.eval("Cennik.UI.bindingNote.text")
    lua.execute('Cennik.UI.notice="Другой статус"; Cennik.UI:SelectSection("History"); Cennik.UI:SelectSection("Opportunities"); Game.advance(0)')
    assert "Рынок подтверждён" in lua.eval("Cennik.UI.bindingNote.text")
    lua.execute('function UnitName() return "AnotherPlayer" end; Cennik.UI:Refresh()')
    assert lua.eval("Cennik.UI.bindButton.text") == "Подтвердить рынок"
    assert "AnotherPlayer" in lua.eval("Cennik.UI.bindingNote.text")


def test_market_button_reports_wrong_faction_and_pending_purchase_without_binding(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    lua.execute('Cennik.db.current_market="forever.pvp.horde.us"; Cennik.db.binding=nil; Cennik.UI:Attach(); Cennik.UI:Show(); Cennik.UI.bindButton:GetScript("OnClick")()')
    assert lua.eval("Cennik.db.binding") is None
    assert "фракции" in lua.eval("Cennik.UI.noticeNote.text")
    assert lua.eval("Cennik.UI.bindButton.text") == "Подтвердить рынок"
    lua.execute('Cennik.db.current_market="forever.pvp.alliance.us"; Cennik.db.pending={}; Cennik.UI.bindButton:GetScript("OnClick")()')
    assert lua.eval("Cennik.db.binding") is None
    assert "результат покупки" in lua.eval("Cennik.UI.noticeNote.text")


def test_first_tab_show_and_reclick_relayout_visible_rows_without_history_workaround(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    lua.execute('Cennik.UI:Attach(); Cennik.UI:Show(); Game.advance(0)')
    assert lua.eval("Cennik.UI.opportunityCount") == 1
    assert lua.eval("#Cennik.UI.opportunityRows") == 1
    assert lua.eval("Cennik.UI.opportunityRows[1]:IsVisible()")
    assert lua.eval("Cennik.UI.opportunityScroll:GetScrollChild()==Cennik.UI.opportunityList")
    assert lua.eval("Cennik.UI.opportunityScroll.rectUpdates") > 0
    lua.execute('Cennik.UI.opportunityScroll:SetVerticalScroll(600); Cennik.UI.tabs.Opportunities:GetScript("OnClick")(); Game.advance(0)')
    assert lua.eval("Cennik.UI.opportunityScroll:GetVerticalScroll()") == 0
    assert lua.eval("Cennik.UI.opportunityRows[1]:IsVisible()")


def test_opportunity_wrapped_title_and_summary_grow_row_and_remain_in_tooltip(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    lua.execute("""
      Cennik.UI:Attach(); Cennik.UI:Show(); Game.advance(0)
      local first=Cennik.UI.opportunityRows[1]
      -- Use the height a client font reports after wrapping a long name.
      first.cennikTitle.GetStringHeight=function() return 126 end
      first.cennikSummary.GetStringHeight=function() return 42 end
      Game.localized[t.item_id]=string.rep('Очень длинное название предмета ',12)
      t.reason='Полное пояснение состояния'
      Cennik.UI:Refresh()
      local row=Cennik.UI.opportunityRows[1]
      assert(row.cennikTitle:GetText():find(Game.localized[t.item_id],1,true))
      assert(row.cennikSummary:GetText():find('Торговля с NPC',1,true))
      assert(row:GetHeight()>=182 and Cennik.UI.opportunityScroll:GetVerticalScrollRange()>0)
      assert(row.cennikTitle.parent==row and row.cennikSummary.parent==row)
      GameTooltip={SetOwner=function() end,SetHyperlink=function(_,link) Game.tooltipLink=link end,
        AddLine=function(_,line) Game.tooltipLines=Game.tooltipLines or {}; table.insert(Game.tooltipLines,line) end,
        Show=function() end,Hide=function() end}
      row:GetScript('OnEnter')(row)
      assert(Game.tooltipLink:find(Game.localized[t.item_id],1,true))
      assert(Game.tooltipLines[1]==row.cennikSummary:GetText() and Game.tooltipLines[2]==t.reason)
    """)


def test_list_does_not_reuse_history_scroll_and_repeated_tab_clicks_keep_pool_bounded(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    lua.execute('Cennik.UI:Attach(); Cennik.UI:Show(); oldList=Cennik.UI.opportunityScroll; Cennik.UI.tabs.History:GetScript("OnClick")()')
    lua.execute('for _,e in ipairs(Cennik.UI.used) do if e.key:find("text%-History") and e.widget.kind=="ScrollFrame" then historyScroll=e.widget; historyScroll:SetVerticalScroll(450) end end')
    assert lua.eval("historyScroll~=oldList")
    lua.execute('Cennik.UI.tabs.Opportunities:GetScript("OnClick")(); Game.advance(0)')
    assert lua.eval("Cennik.UI.opportunityScroll==oldList")
    assert lua.eval("Cennik.UI.opportunityScroll:GetVerticalScroll()") == 0
    assert lua.eval("Cennik.UI.opportunityRows[1]:IsVisible()")
    count = lua.eval("#Game.frames")
    lua.execute('for i=1,50 do Cennik.UI.tabs.History:GetScript("OnClick")(); Cennik.UI.tabs.Opportunities:GetScript("OnClick")(); Game.advance(0) end')
    assert lua.eval("#Game.frames") == count
    assert lua.eval("Cennik.UI.opportunityRows[1]:IsVisible()")


def test_empty_market_list_is_explicit_and_nested_redraw_is_deferred(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    lua.execute('Cennik.UI:Attach(); Cennik.UI.focus=t.task_id; Cennik.UI:Show(); Cennik.db.current_run="another-run"; Cennik.UI:Refresh()')
    assert lua.eval("Cennik.UI.opportunityCount") == 0
    assert lua.eval("Cennik.UI:Task()") is None
    assert lua.eval('function() for _,e in ipairs(Cennik.UI.used) do if type(e.widget.text)=="string" and e.widget.text:find("Нет заданий") then return e.widget:IsVisible() end end return false end')()
    lua.execute('Cennik.db.current_run=t.run_id; Cennik.UI.rendering=true; Cennik.UI:Refresh(); Cennik.UI.rendering=false; Game.advance(0)')
    assert lua.eval("Cennik.UI.opportunityCount") == 1
    assert lua.eval("Cennik.UI.opportunityRows[1]:IsVisible()")


def test_scroll_children_keep_anchors_and_history_after_deferred_and_repeated_redraw(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    lua.execute('Cennik.UI:Attach(); Cennik.UI:Show(); Game.advance(0)')
    assert lua.eval("Cennik.UI.opportunityList:GetNumPoints()") > 0
    assert lua.eval("Cennik.UI.opportunityRows[1]:IsVisible()")
    lua.execute('Cennik:State(t,"pending_scan","Проверка записи"); beforeHistory=#Cennik.db.history; Cennik.UI.tabs.History:GetScript("OnClick")()')
    lua.execute('for _,e in ipairs(Cennik.UI.used) do if e.widget.kind=="Frame" and e.key:find("text%-History$") then historyBox=e.widget end end')
    assert lua.eval("historyBox:GetNumPoints()") > 0
    assert lua.eval("historyBox:IsVisible()")
    text = lua.eval("historyBox.text")
    lua.execute('Game.advance(0); for i=1,30 do Cennik.UI:Refresh() end')
    assert lua.eval("historyBox:GetNumPoints()") > 0
    assert lua.eval("historyBox:IsVisible()")
    assert lua.eval("historyBox.text") == text
    assert lua.eval("#Cennik.db.history") == lua.eval("beforeHistory")
    lua.execute('Cennik.UI.tabs.Opportunities:GetScript("OnClick")(); Game.advance(0)')
    assert lua.eval("Cennik.UI.opportunityRows[1]:IsVisible()")


def test_detail_and_history_scroll_children_have_nonzero_content_height(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    lua.execute('Cennik.UI:Attach(); Cennik.UI.focus=t.task_id; Cennik.UI:Show(); Game.advance(0)')
    lua.execute('for _,e in ipairs(Cennik.UI.used) do if e.widget.kind=="Frame" and e.key:find("text%-Opportunities$") then detailsBox=e.widget end end')
    assert lua.eval("detailsBox:GetNumPoints()") > 0
    assert lua.eval("detailsBox:GetHeight()") >= 140
    assert lua.eval("detailsBox:IsVisible()")
    lua.execute('for i=1,40 do Cennik:Emit(t,"state","pending_scan",{reason="Событие " .. i}) end; Cennik.UI.tabs.History:GetScript("OnClick")(); Game.advance(0)')
    lua.execute('for _,e in ipairs(Cennik.UI.used) do if e.widget.kind=="Frame" and e.key:find("text%-History$") then historyBox=e.widget end end')
    assert lua.eval("historyBox:GetHeight()") > 350
    assert lua.eval("historyBox:IsVisible()")


def test_list_scroll_offset_is_clamped_when_number_of_tasks_shrinks(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    lua.execute('for i=1,20 do local copy=Cennik:Clone(t); copy.task_id="extra-" .. i; Cennik.db.tasks[copy.task_id]=copy end; Cennik.UI:Attach(); Cennik.UI:Show(); Game.advance(0)')
    lua.execute('Cennik.UI.opportunityScroll:SetVerticalScroll(300); for id in pairs(Cennik.db.tasks) do if id~=t.task_id then Cennik.db.tasks[id]=nil end end; Cennik.UI:Refresh()')
    assert lua.eval("Cennik.UI.opportunityCount") == 1
    assert lua.eval("Cennik.UI.opportunityScroll:GetVerticalScroll()") == 0
    assert lua.eval("Cennik.UI.opportunityRows[1]:IsVisible()")


def test_rescan_clears_old_economics_and_reports_missing_lots_below_limit(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    scan(lua)
    assert lua.eval("t.validation.profit_copper") > 0
    lua.execute("Game.commodity[t.item_id][1].unitPrice=t.lines[1].unit_limit_copper+1; Cennik.Scanner:Start(t)")
    assert lua.eval("t.validation") is None
    assert lua.eval("next(t.scans)") is None
    lua.execute('Game.event("COMMODITY_SEARCH_RESULTS_UPDATED",t.item_id)')
    assert lua.eval("t.state") == "limits_exceeded"
    assert "выше предела" in lua.eval("t.reason")
    assert lua.eval("t.validation.profit_copper") == 49
    lua.execute("t.expires_at_epoch=Game.now; Cennik.Scanner:Start(t)")
    assert "истёк" in lua.eval("t.reason")
    assert lua.eval("t.validation") is None


def test_non_equip_trade_goods_scan_all_craft_materials_without_variant_prompt(exchange):
    lua = runtime(exported(exchange, "simple_craft"))
    lua.execute('C_Item.GetItemInfoInstant=function(id) return id,"Trade Goods","Leather","NON_EQUIP",1,7 end')
    scan(lua)
    assert lua.eval("t.state") == "ready_to_buy"
    assert lua.eval("t.validation.cost_copper") > 0
    text = lua.eval("Cennik.UI:Details(t)")
    assert "закупка 2 шт. за 60c" in text
    assert "оценка источника" in text


def test_hide_keeps_history_spending_and_survives_reimport_and_restore(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    scan(lua)
    lua.execute("Cennik.BuyQueue:Add(t); t.spent=10; t.bought[t.item_id]=1; Cennik.UI:Attach(); Cennik.UI:Show(); Cennik:HideTask(t); Cennik:Import(); Cennik.UI:Refresh()")
    assert lua.eval("t.hidden") and lua.eval("t.spent") == 10
    assert lua.eval("t.bought[t.item_id]") == 1
    assert lua.eval("#Cennik.db.history") > 0
    assert lua.eval("#Cennik.db.queue") == 0
    assert lua.eval("Cennik.UI.opportunityCount") == 0
    assert not lua.eval("Cennik.BuyQueue:Add(t)")[0]
    lua.execute('Cennik.UI:SelectSection("Hidden"); for _,entry in ipairs(Cennik.UI.used) do if entry.widget.text=="Вернуть" then entry.widget:GetScript("OnClick")(); break end end')
    assert not lua.eval("t.hidden")
    assert lua.eval("t.state") == "pending_scan"
    assert lua.eval("t.spent") == 10
    assert lua.eval("Cennik.UI.opportunityCount") == 1
    lua.execute("Cennik.db.pending={task_id=t.task_id}")
    assert not lua.eval("Cennik:HideTask(t)")[0]
    assert not lua.eval("t.hidden")


def browse_api(lua):
    lua.execute('''
        Game.browse={}; Game.browseFull=true
        C_AuctionHouse.SendBrowseQuery=function(query) Game.browseQuery=query end
        C_AuctionHouse.HasFullBrowseResults=function() return Game.browseFull end
        C_AuctionHouse.GetBrowseResults=function() return Game.browse end
        C_AuctionHouse.RequestMoreBrowseResults=function() Game.moreBrowse=true end
    ''')


def test_unique_equipment_variant_discovered_by_local_name_filtered_by_id(exchange):
    lua = runtime(exported(exchange, "simple_craft"))
    browse_api(lua)
    lua.execute('Game.gear[t.item_id]=true; Game.localized[t.item_id]="Сапоги"; Cennik.Scanner:Start(t)')
    assert lua.eval("Game.browseQuery.searchString") == "Сапоги"
    lua.execute('key={itemID=t.item_id,itemLevel=12,itemSuffix=0}; Game.browse={{itemKey={itemID=999}},{itemKey=key},{itemKey=key}}; Game.event("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")')
    assert lua.eval("t.selected_keys[t.item_id].itemLevel") == 12
    lua.execute('Game.items[t.item_id]={{quantity=1,buyoutAmount=150,auctionID=77,containsOwnerItem=false,itemKey=key}}; Game.event("ITEM_SEARCH_RESULTS_UPDATED",key); Game.event("COMMODITY_SEARCH_RESULTS_UPDATED",10)')
    assert lua.eval("t.state") == "ready_to_buy"
    assert lua.eval("t.scans[10].complete")
    assert lua.eval("t.spent") == 0


def test_ambiguous_equipment_blocks_and_shopping_targets_required_material(exchange):
    lua = runtime(exported(exchange, "simple_craft"))
    browse_api(lua)
    lua.execute('Game.gear[10]=true; Cennik.Scanner:Start(t); Game.event("COMMODITY_SEARCH_RESULTS_UPDATED",t.item_id); Game.browse={{itemKey={itemID=10,itemSuffix=1}},{itemKey={itemID=10,itemSuffix=2}}}; Game.event("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")')
    assert lua.eval("t.state") == "requires_manual_check"
    assert lua.eval("t.needs_variant_item_id") == 10
    assert lua.eval("t.selected_keys[10]") is None
    lua.execute('Cennik.UI:Attach(); Cennik.UI.focus=t.task_id; Cennik.UI:Show(); for _,entry in ipairs(Cennik.UI.used) do if entry.widget.text=="Поиск" then entry.widget:GetScript("OnClick")(); break end end')
    assert lua.eval("Game.shopping[1]") == "Item 10"


def test_empty_equipment_browse_rejects_as_missing_supply_and_partial_times_out(exchange):
    lua = runtime(exported(exchange, "vendor_arbitrage"))
    browse_api(lua)
    lua.execute('Game.gear[t.item_id]=true; Cennik.Scanner:Start(t); Game.event("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED")')
    assert lua.eval("t.state") == "insufficient_supply"
    assert "доступно 0 по текущим ценам" in lua.eval("t.reason")
    lua.execute('Cennik.Scanner:Start(t); Game.browseFull=false; Game.event("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED"); Game.advance(31)')
    assert lua.eval("Game.moreBrowse")
    assert lua.eval("t.state") == "scan_failed"


def test_details_clip_above_money_bar_and_show_sales_source_date(exchange):
    lua = runtime(exported(exchange, "simple_craft"))
    lua.execute('AuctionHouseFrame:SetHeight(580); t.opportunity.details.price.sales_estimated_per_day="2.8"; t.opportunity.details.price.sales_observed_at="2026-10-14T06:16:08Z"; t.opportunity.details.price.sales_source="AHL1"; Cennik.UI:Attach(); Cennik.UI.focus=t.task_id; Cennik.UI:Show()')
    assert lua.eval("Cennik.UI.body.clipsChildren")
    lua.execute('for _,entry in ipairs(Cennik.UI.used) do if entry.widget.kind=="ScrollFrame" and entry.key:find("text%-Opportunities$") then detailScroll=entry.widget end end')
    assert lua.eval("detailScroll.clipsChildren")
    # The entire viewport ends before the reserved coin/footer area.
    assert lua.eval("62+100-detailScroll.points[1][3]+detailScroll:GetHeight()") <= 580-48
    text = lua.eval("Cennik.UI:Details(t)")
    assert "2.8" in text and "2026-10-14T06:16:08Z" in text and "AHL1" in text
    assert "не гарантирует" in text
