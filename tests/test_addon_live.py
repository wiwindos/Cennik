"""Live repricing and actual purchase safety, using invented game auction/NPC data."""
from test_addon import exchange as exchange
from test_addon import exported
from test_addon_lua import browse_api, click, runtime, scan
from test_cennik import analytics_root as analytics_root


def test_reprice_all_materials_above_caps_retains_fee_margin_and_negative_profit(exchange):
    lua=runtime(exported(exchange,"simple_craft"))
    lua.execute('t.lines[1].quantity=7; Game.commodity[10]={{quantity=20,unitPrice=31,numOwnerItems=0}}')
    scan(lua)
    assert lua.eval("t.state") == "limits_exceeded"
    assert lua.eval("t.validation.complete")
    assert lua.eval("t.validation.cost_copper") == 222
    assert lua.eval("t.validation.gross_revenue_copper") == 300
    assert lua.eval("t.validation.fee_copper") == 15
    assert lua.eval("t.validation.profit_copper") == 63
    assert lua.eval("t.validation.margin_percent") == 21
    assert lua.eval("t.validation.break_even_unit_copper") == 117
    assert not lua.eval("Cennik.BuyQueue:Add(t)")[0]
    lua.execute('Game.commodity[10][1].unitPrice=100')
    scan(lua)
    assert lua.eval("t.state") == "not_profitable"
    assert lua.eval("t.validation.profit_copper") == -420
    assert "−420c" in lua.eval("Cennik.UI:Details(t)")


def test_missing_quantity_never_invents_full_profit_and_expiry_removes_current_row_numbers(exchange):
    lua=runtime(exported(exchange,"simple_craft"))
    lua.execute('Game.commodity[10][1].quantity=1')
    scan(lua)
    assert lua.eval("t.state") == "insufficient_supply"
    assert not lua.eval("t.validation.complete")
    assert lua.eval("t.validation.profit_copper") is None
    lua.execute('Game.commodity[10][1].quantity=2')
    scan(lua)
    assert lua.eval("Cennik.UI:Live(t)") is not None
    lua.execute('Game.advance(91)')
    assert lua.eval("Cennik.UI:Live(t)") is None
    assert lua.eval("t.state") == "pending_scan"
    assert "Полная текущая экономика неизвестна" in lua.eval("Cennik.UI:Details(t)")


def test_item_price_groups_plan_exact_units_with_all_available_depth(exchange):
    lua=runtime(exported(exchange,"simple_craft"))
    lua.execute('''
      t.lines[1].quantity=7
      Game.items[10]={
        {quantity=10,buyoutAmount=10,auctionID=1,containsOwnerItem=false,itemKey=C_AuctionHouse.MakeItemKey(10)},
        {quantity=7,buyoutAmount=12,auctionID=2,containsOwnerItem=false,itemKey=C_AuctionHouse.MakeItemKey(10)}}
      Cennik.Scanner:Start(t); Game.event("COMMODITY_SEARCH_RESULTS_UPDATED",20); Game.event("ITEM_SEARCH_RESULTS_UPDATED",C_AuctionHouse.MakeItemKey(10))
    ''')
    assert lua.eval("t.validation.plans[1].cost") == 70
    assert lua.eval("t.validation.plans[1].lots[1].auction_id") == 1
    assert lua.eval("t.validation.plans[1].lots[1].quantity") == 1
    lua.execute('''
      t.lines[1].quantity=2; t.max_total_cost_copper=200
      Game.items[10]={{quantity=3,buyoutAmount=30,auctionID=3,containsOwnerItem=false,itemKey=C_AuctionHouse.MakeItemKey(10)}}
      Cennik.Scanner:Start(t); Game.event("COMMODITY_SEARCH_RESULTS_UPDATED",20); Game.event("ITEM_SEARCH_RESULTS_UPDATED",C_AuctionHouse.MakeItemKey(10))
    ''')
    assert lua.eval("t.validation.cost_copper") == 65
    assert lua.eval("t.validation.plans[1].extra_quantity") == 0
    assert lua.eval("t.state") == "ready_to_buy"
    lua.execute('t.allow_overbuy=true; t.validation=Cennik.Validator:Validate(t)')
    assert lua.eval("t.validation.state") == "ready_to_buy"


def test_repeated_representative_id_never_doubles_available_depth(exchange):
    lua=runtime(exported(exchange,"simple_craft"))
    lua.execute('''
      local key=C_AuctionHouse.MakeItemKey(10)
      Game.items[10]={{quantity=12,buyoutAmount=25000,auctionID=1,containsOwnerItem=false,itemKey=key},
        {quantity=12,buyoutAmount=25000,auctionID=1,containsOwnerItem=false,itemKey=key}}
      local rows=Cennik.Adapter:Results(10,key,false)
      plan,state,reason=Cennik.Validator:PlanScan(t,t.lines[1],20,true,{complete=true,observed_at=Game.now,key=key,commodity=false,rows=rows})
    ''')
    assert lua.eval("plan") is None
    assert lua.eval("state") == "insufficient_supply"
    assert "доступно 12" in lua.eval("reason")


def test_upgrade_discards_old_estimates_and_blocks_legacy_plans_without_losing_spend(exchange):
    lua=runtime(exported(exchange,"simple_craft"))
    lua.execute('''
      t.spent=30; t.bought[10]=1; t.validation={complete=true}; t.scans={old=true}
      t.opportunity.details.workspace_plan_id='old-plan'
      Cennik.db.quantity_model=nil
      Cennik:Initialize()
      assert(t.spent==30 and t.bought[10]==1 and t.validation==nil and next(t.scans)==nil)
      local ok,reason=Cennik:CheckTask(t)
      assert(not ok and reason:find('прежнюю модель количества',1,true))
      t.opportunity.details.quantity_model='forever_units_v1'
      assert(Cennik:CheckTask(t))
    ''')


def test_accept_prices_is_a_review_form_then_explicit_save_without_purchase(exchange):
    lua=runtime(exported(exchange,"simple_craft"))
    lua.execute('Game.commodity[10][1].unitPrice=40')
    scan(lua)
    lua.execute('Cennik.UI:Attach(); Cennik.UI.focus=t.task_id; Cennik.UI:Show(); oldLimit=t.lines[1].unit_limit_copper; oldBudget=Cennik.db.packages[t.package_id].budget')
    lua.execute('for _,entry in ipairs(Cennik.UI.used) do if entry.widget.text=="Принять цены" then entry.widget:GetScript("OnClick")(); break end end')
    assert lua.eval("Cennik.UI.section") == "Limits"
    assert lua.eval("t.lines[1].unit_limit_copper") == lua.eval("oldLimit")
    lua.execute('for _,entry in ipairs(Cennik.UI.used) do if entry.widget.text=="Сохранить пределы и подтверждённые цены NPC" then entry.widget:GetScript("OnClick")(); break end end')
    assert lua.eval("t.lines[1].unit_limit_copper") == 40
    assert lua.eval("t.max_total_cost_copper") == 85
    assert lua.eval("Cennik.db.packages[t.package_id].budget") == lua.eval("oldBudget")
    assert lua.eval("t.opportunity.details.materials[1].unit_price_copper") == 30
    scan(lua)
    assert lua.eval("t.state") == "requires_manual_check"  # New cost does not silently increase package budget.
    assert "бюджета" in lua.eval("t.reason")
    lua.execute('Cennik.db.packages[t.package_id].budget=85')
    scan(lua)
    assert lua.eval("t.state") == "ready_to_buy"
    assert lua.eval("t.spent") == 0


def vendor_variants(lua):
    browse_api(lua)
    lua.execute('''
      Game.gear[t.item_id]=true
      k1={itemID=t.item_id,itemSuffix=1}; k2={itemID=t.item_id,itemSuffix=2}
      link1="|Hitem:" .. t.item_id .. ":0:0:0:0:0:1|h[First]|h"
      link2="|Hitem:" .. t.item_id .. ":0:0:0:0:0:2|h[Second]|h"
      Game.vendor[link1]=100; Game.vendor[link2]=150
      Game.variantRows={
        [1]={{quantity=1,buyoutAmount=80,auctionID=71,containsOwnerItem=false,itemKey=k1,itemLink=link1}},
        [2]={{quantity=1,buyoutAmount=90,auctionID=72,containsOwnerItem=false,itemKey=k2,itemLink=link2}}}
      C_AuctionHouse.GetNumItemSearchResults=function(key) return #(Game.variantRows[key.itemSuffix] or {}) end
      C_AuctionHouse.GetItemSearchResultInfo=function(key,index) return Game.variantRows[key.itemSuffix][index] end
      C_AuctionHouse.GetAuctionInfoByID=function(id)
        for _,rows in pairs(Game.variantRows) do for _,row in ipairs(rows) do if row.auctionID==id then return row end end end
      end
      Game.browse={{itemKey=k1},{itemKey=k2}}
    ''')


def scan_vendor_variants(lua, prepare=False):
    lua.execute('Cennik.BuyQueue:Prepare()' if prepare else 'Cennik.Scanner:Start(t)')
    lua.execute('Game.event("AUCTION_HOUSE_BROWSE_RESULTS_UPDATED"); Game.event("ITEM_SEARCH_RESULTS_UPDATED",k1); Game.event("ITEM_SEARCH_RESULTS_UPDATED",k2)')


def test_vendor_checks_every_variant_and_uses_real_npc_price_on_exact_lot(exchange):
    lua=runtime(exported(exchange,"vendor_arbitrage"))
    vendor_variants(lua)
    scan_vendor_variants(lua)
    assert lua.eval("#t.scans[t.item_id].variants") == 2
    assert lua.eval("t.validation.cost_copper") == 90
    assert lua.eval("t.validation.profit_copper") == 60
    assert lua.eval("t.validation.plans[1].lots[1].auction_id") == 72
    assert lua.eval("t.opportunity.details.vendor_sell_copper") == 100
    # Explicit caps first; neither scan submits a protected purchase.
    assert lua.eval("t.state") == "limits_exceeded"
    lua.execute('t.lines[1].unit_limit_copper=100; t.max_total_cost_copper=100; Cennik.db.packages[t.package_id].budget=100')
    scan_vendor_variants(lua)
    assert lua.eval("t.state") == "ready_to_buy"
    lua.execute('Cennik.BuyQueue:Add(t)')
    scan_vendor_variants(lua,prepare=True)
    assert click(lua,"Start")
    assert lua.eval("Game.calls[#Game.calls].kind") == "bid"
    assert lua.eval("Cennik.db.pending.auction_id") == 72
    lua.execute('Game.event("AUCTION_HOUSE_PURCHASE_COMPLETED",72)')
    assert lua.eval("t.vendor_bought_revenue") == 150
    assert lua.eval("t.spent") == 90


def test_unknown_vendor_price_no_server_fallback_and_changed_price_blocks_bid(exchange):
    lua=runtime(exported(exchange,"vendor_arbitrage"))
    vendor_variants(lua)
    lua.execute('Game.vendor[link1]=nil; Game.vendor[t.item_id]=nil; t.opportunity.details.vendor_sell_copper=nil')
    scan_vendor_variants(lua)
    lua.execute('Game.advance(31)')
    assert lua.eval("t.state") == "scan_failed"
    assert lua.eval("t.validation") is None
    assert lua.eval("t.spent") == 0
    lua.execute('Cennik.NPC:ManualVendor(t,k1,100)')
    scan_vendor_variants(lua)
    assert lua.eval("t.validation.complete")
    assert lua.eval("t.validation.profit_copper") == 60
    lua=runtime(exported(exchange,"vendor_arbitrage"))
    vendor_variants(lua)
    lua.execute('t.lines[1].unit_limit_copper=100; t.max_total_cost_copper=100; Cennik.db.packages[t.package_id].budget=100')
    scan_vendor_variants(lua)
    lua.execute('Cennik.BuyQueue:Add(t)')
    scan_vendor_variants(lua,prepare=True)
    lua.execute('Game.vendor[link2]=1')
    assert not click(lua,"Start")
    assert lua.eval("Cennik.db.pending") is None


def test_npc_capture_actual_bundle_cost_unknown_extended_cost_and_character_scope(exchange):
    lua=runtime(exported(exchange,"simple_craft"))
    lua.execute('''
      t.npc_prices=nil
      GetNumMerchantItems=function() return 1 end
      GetMerchantItemLink=function() return "|Hitem:11|h[NPC reagent]|h" end
      GetMerchantItemInfo=function() return "NPC reagent",1,12,2,-1,true,true,false end
      Game.event("MERCHANT_SHOW")
    ''')
    scan(lua)
    assert lua.eval("t.validation.npc_cost_copper") == 12  # Need 1, merchant sells 2 for 12.
    assert "Открытый торговец" in lua.eval("Cennik.UI:Details(t)")
    lua.execute('for _,material in ipairs(t.opportunity.details.materials) do if material.source=="npc" then material.unit_price_copper=nil end end')
    lua.execute('Game.advance(901)')
    scan(lua)
    assert lua.eval("t.validation.profit_copper") is None
    assert "NPC" in lua.eval("t.reason")
    lua.execute('Game.event("MERCHANT_UPDATE"); function UnitName() return "OtherPlayer" end')
    assert lua.eval("Cennik.NPC:Quote(t,11)") is None
    lua.execute('function UnitName() return "FixturePlayer" end; GetMerchantItemInfo=function() return "NPC reagent",1,12,2,-1,true,true,true end; Game.event("MERCHANT_UPDATE")')
    assert lua.eval("Cennik.NPC:Quote(t,11)") is None
    lua.execute('Cennik.NPC:Manual(t,11,0)')
    scan(lua)
    assert lua.eval("t.validation.npc_cost_copper") == 0


def test_database_npc_price_is_labelled_and_missing_price_never_fabricates_profit(exchange):
    lua=runtime(exported(exchange,"simple_craft"))
    lua.execute('t.npc_prices=nil')
    scan(lua)
    assert lua.eval("t.validation.npc_cost_copper") == 5
    assert "База: цена NPC" in lua.eval("Cennik.UI:Details(t)")
    lua.execute('for _,material in ipairs(t.opportunity.details.materials) do if material.source=="npc" then material.unit_price_copper=nil end end')
    scan(lua)
    assert lua.eval("t.validation.profit_copper") is None
    assert lua.eval("t.validation.roi_percent") is None
    assert "Не подтверждена текущая цена NPC" in lua.eval("t.reason")
    lua.execute('Cennik.NPC:Manual(t,11,7)')
    scan(lua)
    assert lua.eval("t.validation.npc_cost_copper") == 7


def test_quote_increase_cannot_consume_gold_reserved_for_npc(exchange):
    lua=runtime(exported(exchange,"simple_craft",unit_limits={10:40}))
    lua.execute('Game.money=65; Game.commodity[10][1].unitPrice=30')
    scan(lua)
    lua.execute('Cennik.BuyQueue:Add(t)')
    scan(lua,prepare=True)
    assert click(lua,"Start")
    lua.execute('Game.event("COMMODITY_PRICE_UPDATED",31,62)')
    assert lua.eval("Cennik.db.pending") is None
    assert lua.eval("t.spent") == 0


def test_vendor_database_fallback_has_explicit_source_and_partial_owned_commodity_is_usable(exchange):
    lua=runtime(exported(exchange,"vendor_arbitrage"))
    lua.execute('Game.vendor[t.item_id]=nil; Game.commodity[t.item_id][1].numOwnerItems=10')
    scan(lua)
    assert lua.eval("t.state") == "ready_to_buy"
    assert lua.eval("t.validation.profit_copper") == 50
    assert "База: цена продажи NPC" in lua.eval("t.validation.revenue_basis")
    assert lua.eval("t.scans[t.item_id].rows[1].quantity") == 90


def test_limits_draft_survives_scan_expiry_and_npc_bundle_manual_entry(exchange):
    lua=runtime(exported(exchange,"simple_craft"))
    scan(lua)
    lua.execute('''
      Cennik.UI:Attach(); Cennik.UI.focus=t.task_id; Cennik.UI:Show()
      Cennik.UI.limitTask=t.task_id; Cennik.UI.section="Limits"; Cennik.UI:Refresh()
      for _,entry in ipairs(Cennik.UI.used) do
        if entry.widget.kind=="EditBox" and entry.widget.text=="30" then
          entry.widget:SetText("40"); entry.widget:GetScript("OnTextChanged")(entry.widget,true)
        end
      end
      Game.advance(91)
    ''')
    assert lua.eval('Cennik.UI.limitDraft.fields["price-10"]') == "40"
    assert lua.eval('function() for _,e in ipairs(Cennik.UI.used) do if e.widget.kind=="EditBox" and e.widget.text=="40" then return true end end return false end')()
    lua.execute('Cennik.NPC:Manual(t,11,12,2)')
    scan(lua)
    assert lua.eval("t.validation.npc_cost_copper") == 12


def test_changed_npc_quote_hides_old_live_economics_until_rechecked(exchange):
    lua=runtime(exported(exchange,"simple_craft"))
    scan(lua)
    assert lua.eval("Cennik.UI:Live(t)") is not None
    lua.execute('Cennik.NPC:Manual(t,11,8)')
    assert lua.eval("Cennik.UI:Live(t)") is None
    scan(lua)
    assert lua.eval("t.validation.npc_cost_copper") == 8


def test_scan_expiry_keeps_unknown_purchase_state_and_repeat_block(exchange):
    lua=runtime(exported(exchange,"vendor_arbitrage"))
    scan(lua)
    lua.execute('Cennik.BuyQueue:Add(t)')
    scan(lua,prepare=True)
    assert click(lua,"Start")
    lua.execute('Game.event("COMMODITY_PRICE_UPDATED",50,50)')
    assert click(lua,"Confirm")
    lua.execute('Game.advance(92)')
    assert lua.eval("Cennik.db.pending.stage") == "unknown"
    assert lua.eval("t.state") == "requires_manual_check"
    assert not click(lua,"Start")
