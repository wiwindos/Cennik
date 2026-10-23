from datetime import UTC, datetime, timedelta

import pytest
from test_addon import exchange as exchange
from test_addon import exported
from test_addon_lua import runtime
from test_cennik import analytics_root as analytics_root
from test_workspace import workspace as workspace

from cennik.addon.lua_data import lua_literal
from cennik.workspace.models import MARKET
from cennik.workspace.sniper import SnipeMaterials, SnipeRule, decode_rules, encode_rules


def game(exchange, *, quantity=25, gear=False, bags=False, mode="any"):
    lua = runtime(exported(exchange))
    lua.execute("""
        Cennik.db.workspace_id='test-scope'; Cennik.db.sniper.workspace_id='test-scope'
        Cennik.db.current_market='forever.pvp.alliance.us'; Cennik.db.binding.market=Cennik.db.current_market
        C_Item.GetItemCount=function() return Game.stock or 0 end
        Game.stock=5
        function GetBuildInfo() return 'fixture','fixture' end
    """)
    rule = SnipeRule(id="fixture-rule", item_id=2589, quantity=quantity, use_bags=bags,
        mode=mode, unit_limit_copper=20 if mode == "limit" else 0,
        item_level=0 if gear else None, item_suffix=0 if gear else None, pet_id=0 if gear else None)
    lua.execute("definition="+lua_literal(rule.model_dump(mode="json")))
    lua.execute("assert(Cennik.Sniper:Add(definition)); r=Cennik.db.sniper.rules[definition.id]; r.enabled=true")
    lua.execute("Game.commodity[2589]={{quantity=100,unitPrice=10,numOwnerItems=0}}")
    if gear:
        lua.execute("""Game.gear[2589]=true
            Game.items[2589]={{quantity=40,buyoutAmount=400,containsOwnerItem=false,containsAccountItem=false,
                auctionID=123,itemKey=C_AuctionHouse.MakeItemKey(2589)}}""")
    return lua


def found(lua, gear=False):
    lua.execute("Cennik.Sniper:Tick(0.1)")
    lua.execute("Game.event('ITEM_SEARCH_RESULTS_UPDATED',C_AuctionHouse.MakeItemKey(2589))" if gear else
                "Game.event('COMMODITY_SEARCH_RESULTS_UPDATED',2589)")


def test_target_buy_does_not_need_profitable_opportunity_or_published_run(exchange):
    lua = game(exchange)
    lua.execute("Cennik.db.current_run=nil")
    found(lua)
    assert lua.eval("Cennik.Sniper.alerts[r.definition.id].cost") == 250
    assert lua.eval("Game.click(function() return Cennik.Sniper:Buy(r.definition.id) end)") is True
    assert lua.eval("r.bought") == 0
    lua.execute("Game.event('COMMODITY_PRICE_UPDATED',10,250)")
    assert lua.eval("Cennik.db.pending.stage") == "quoted"
    assert lua.eval("#Game.calls") == 2  # search + initial quote, no automatic confirm
    lua.execute("Game.click(function() Cennik.Sniper:ConfirmPurchase() end)")
    lua.execute("Game.event('COMMODITY_PURCHASE_SUCCEEDED',2589)")
    assert lua.eval("r.bought") == 25
    assert lua.eval("Cennik.db.sniper.spent") == 250
    assert not lua.eval("r.enabled")
    lua.execute("""
      local history=#Cennik.db.sniper.history
      local ids={}; for _,event in ipairs(Cennik.db.sniper.events) do ids[#ids+1]=event.id end
      CennikImport.acknowledged_sniper_event_ids=ids; Cennik.Sniper:Initialize()
      assert(#Cennik.db.sniper.events==0 and #Cennik.db.sniper.history==history)
    """)


def test_uncached_group_of_forty_items_buys_and_accounts_only_one(exchange):
    lua = game(exchange, gear=True)
    found(lua, gear=True)
    assert lua.eval("Cennik.Sniper.alerts[r.definition.id].quantity") == 1
    assert lua.eval("Cennik.Sniper.alerts[r.definition.id].available") == 40
    assert lua.eval("Cennik.Sniper.alerts[r.definition.id].cost") == 400
    lua.execute("Game.click(function() Cennik.Sniper:Buy(r.definition.id) end); Game.event('AUCTION_HOUSE_PURCHASE_COMPLETED',123)")
    assert lua.eval("r.bought") == 1
    assert lua.eval("Cennik.Sniper:Remaining(r)") == 24
    assert lua.eval("Cennik.db.sniper.spent") == 400


def test_once_only_bag_subtraction_and_import_preserve_purchases(exchange):
    lua = game(exchange, bags=True)
    assert lua.eval("r.stock") == 5
    assert lua.eval("r.goal") == 20
    lua.execute("r.bought=7; Game.stock=999; Cennik.Sniper:Add(definition)")
    assert lua.eval("r.goal") == 20
    assert lua.eval("Cennik.Sniper:Remaining(r)") == 13
    lua.execute("assert(Cennik.Sniper:Apply(assert(Cennik.Sniper:Preview(Cennik.Sniper:Export()))))")
    assert lua.eval("r.bought") == 7
    assert not lua.eval("r.enabled")


@pytest.mark.parametrize("ownership", ["numOwnerItems=100", "numOwnerItems=nil"])
def test_own_or_unknown_ownership_never_alerts(exchange, ownership):
    lua = game(exchange)
    lua.execute("Game.commodity[2589]={{quantity=100,unitPrice=10,"+ownership+"}}")
    found(lua)
    assert lua.eval("Cennik.Sniper.alerts[r.definition.id]") is None


def test_expired_reference_never_substitutes_monthly_median(exchange):
    lua = game(exchange, mode="median3d")
    found(lua)
    assert lua.eval("#Game.calls") == 0
    lua.execute("r.reference={copper=100,valid_until=Game.now+100}")
    found(lua)
    assert lua.eval("Cennik.Sniper.alerts[r.definition.id].limit") == 50
    lua.execute("r.reference.valid_until=Game.now-1")
    result = lua.eval("Cennik.Sniper:Buy(r.definition.id)")
    assert result[0] is False


def test_purchase_unknown_blocks_duplicate_and_can_be_reconciled(exchange):
    lua = game(exchange, gear=True)
    found(lua, gear=True)
    lua.execute("Game.click(function() Cennik.Sniper:Buy(r.definition.id) end); Cennik:Initialize()")
    assert lua.eval("Cennik.db.pending.stage") == "unknown"
    assert lua.eval("r.bought") == 0
    result = lua.eval("Cennik.Sniper:Buy(r.definition.id)")
    assert result[0] is False
    lua.execute("assert(not Cennik.Execution:Reconcile(true,40,400)); assert(Cennik.Execution:Reconcile(true,1,400))")
    assert lua.eval("r.bought") == 1
    assert lua.eval("Cennik.db.pending") is None


def test_old_budget_does_not_block_findings_but_changed_auction_is_blocked(exchange):
    lua = game(exchange, gear=True)
    lua.execute("Cennik.db.sniper.budget_copper=250")
    found(lua, gear=True)
    assert lua.eval("Cennik.Sniper.alerts[r.definition.id].cost") == 400
    lua.execute("Cennik.db.sniper.budget_copper=1000; r.checked_at=nil; Game.advance(2)")
    found(lua, gear=True)
    lua.execute("Game.items[2589][1].buyoutAmount=500")
    result = lua.eval("Game.click(function() return Cennik.Sniper:Buy(r.definition.id) end)")
    assert result is False


def test_all_rpc_traffic_consumes_background_allowance(exchange):
    lua = game(exchange)
    lua.execute("for i=1,48 do C_AuctionHouse.SendSearchQuery(C_AuctionHouse.MakeItemKey(2589),{},true) end; Game.advance(4); Cennik.Sniper:Tick(0.1)")
    assert lua.eval("#Game.calls") == 48
    lua.execute("assert(Cennik.Sniper:ResumeSearch()); Cennik.Sniper:Tick(0.1)")
    assert lua.eval("#Game.calls") == 48  # Resuming does not reset the shared rate limit.
    lua.execute("Game.advance(61); Cennik.Sniper:Tick(0.1)")
    assert lua.eval("#Game.calls") == 49


def test_manual_search_holds_background_until_explicit_resume_and_keeps_findings(exchange):
    lua = game(exchange)
    found(lua)
    lua.execute("""
      local s=Cennik.Sniper; local offer=s.alerts[r.definition.id]
      C_AuctionHouse.SendSearchQuery(C_AuctionHouse.MakeItemKey(5763),{},true)
      local calls=#Game.calls
      for i=1,4 do Game.advance(30); s:Tick(0.1) end
      assert(#Game.calls==calls and s.manualHold and r.enabled)
      assert(s.alerts[r.definition.id]==offer and not s.current)
      assert(s.overlay.resume:IsShown())
      assert(s.overlay.footer:GetText():find('Поиск на паузе',1,true))
      -- Merely viewing saved findings must not resume searching.
      s:ShowFindings(); s:Tick(0.1); assert(#Game.calls==calls)
      s.overlay.resume.scripts.OnClick()
      assert(not s.manualHold and Cennik.UI.frame:IsShown() and Cennik.UI.section=='Sniper')
      s:Tick(0.1); assert(#Game.calls==calls+1 and not s.manualHold)
    """)


@pytest.mark.parametrize("tab", ["native", "auctionator"])
def test_other_auction_tabs_hold_search_even_without_a_manual_query(exchange, tab):
    lua = game(exchange)
    lua.execute("Cennik.UI:Attach(); Cennik.UI:Show(); Cennik.UI:SelectSection('Sniper')")
    if tab == "native":
        lua.execute("AuctionHouseFrame:SetDisplayMode({'buy'})")
    else:
        lua.execute("local lib=LibStub(); lib:CreateTab('AuctionatorTabs_Shopping',CreateFrame('Frame')); lib:SetSelected('AuctionatorTabs_Shopping')")
    lua.execute("""
      local s=Cennik.Sniper
      Game.advance(10); s:Tick(0.1)
      assert(s.manualHold and #Game.calls==0 and r.enabled)
      -- Switching back is not permission to overwrite manual results again.
      Cennik.UI:Show(); s:Tick(0.1); assert(s.manualHold and #Game.calls==0)
      assert(s:ResumeSearch()); s:Tick(0.1)
      assert(#Game.calls==1 and not s.manualHold and not s.foreignTab)
    """)


def test_manual_search_cancels_inflight_sniper_and_ignores_late_result(exchange):
    lua = game(exchange)
    lua.execute("""
      local s=Cennik.Sniper
      s:Tick(0.1); assert(s.current and not s.manualHold)
      C_AuctionHouse.SendSearchQuery(C_AuctionHouse.MakeItemKey(5763),{},true)
      Game.event('COMMODITY_SEARCH_RESULTS_UPDATED',2589)
      assert(not s.current and not s.alerts[r.definition.id] and s.manualHold)
      Game.advance(65); s:Tick(0.1); assert(#Game.calls==2)
    """)


def test_auctionator_search_completion_does_not_release_manual_hold(exchange):
    lua = game(exchange)
    lua.execute("""
      local a,s=Cennik.Adapter,Cennik.Sniper
      a:ReceiveEvent(a.startEvent); a:ReceiveEvent(a.endEvent)
      Game.advance(65); s:Tick(0.1)
      assert(not a.shopping and s.manualHold and #Game.calls==0)
      Cennik.db.pending={sniper=true,stage='unknown',item_id=2589,quantity=1,unit_price=10,cost=10}
      assert(not s:ResumeSearch() and s.manualHold)
      Cennik.db.pending=nil
      assert(s:ResumeSearch()); s:Tick(0.1)
      assert(#Game.calls==1 and not s.manualHold)
    """)


def test_priorities_do_not_starve_low_priority(exchange):
    lua = game(exchange)
    lua.execute("""
      for _,entry in ipairs({{'high',2590},{'low',2591}}) do
        local d=Cennik:Clone(definition); d.id=entry[1]; d.item_id=entry[2]; d.priority=entry[1]
        assert(Cennik.Sniper:Add(d)); Cennik.db.sniper.rules[d.id].enabled=true
      end
      counts={}; for i=1,30 do local choice=Cennik.Sniper:Choose(); counts[choice.definition.id]=(counts[choice.definition.id] or 0)+1; choice.checked_at=GetTime(); Game.advance(1) end
    """)
    assert lua.eval("counts.high") > lua.eval("counts.low") > 0


def test_batch_discovery_uses_one_query_for_multiple_nonmatching_keys(exchange):
    lua = game(exchange, mode="limit")
    lua.execute("""
      C_AuctionHouse.SearchForItemKeys=function(keys) Game.batchKeys=keys; table.insert(Game.calls,{kind='keys'}) end
      C_AuctionHouse.GetBrowseResults=function() return {} end
      C_AuctionHouse.HasFullBrowseResults=function() return true end
      r.keys={C_AuctionHouse.MakeItemKey(2589)}
      local d=Cennik:Clone(definition); d.id='other'; d.item_id=2592
      assert(Cennik.Sniper:Add(d)); local other=Cennik.db.sniper.rules.other; other.enabled=true; other.keys={C_AuctionHouse.MakeItemKey(2592)}
      Cennik.Sniper:Tick(0.1); Game.event('AUCTION_HOUSE_BROWSE_RESULTS_UPDATED')
    """)
    assert lua.eval("#Game.batchKeys") == 2
    assert lua.eval("#Game.calls") == 1
    assert lua.eval("Cennik.Sniper.current") is None
    assert lua.eval("Cennik.Sniper.alerts[r.definition.id]") is None


def test_python_lua_portable_round_trip_and_no_executable_import(exchange):
    lua = game(exchange)
    rules = [SnipeRule(id="foreign-rule", item_id=5763, quantity=40, mode="limit", unit_limit_copper=12345)]
    text = encode_rules(rules, 500000)
    lua.globals().portable = text
    lua.execute("assert(Cennik.Sniper:Apply(assert(Cennik.Sniper:Preview(portable))))")
    budget, imported = decode_rules(lua.eval("Cennik.Sniper:Export()"))
    assert budget == 500000
    assert rules[0] in imported
    assert lua.eval("Cennik.Sniper:Preview('return os.execute(1)')")[0] is None
    with pytest.raises(ValueError):
        decode_rules(text.replace(MARKET, "forever.pvp.horde.us"))


def test_reference_three_completed_days_equal_weights_and_no_future_points(workspace):
    rule = SnipeRule(item_id=2589, mode="median3d")
    workspace.sniper.save(rule)
    end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = []
    for day, value, repeats in [(3, 80000, 20), (2, 100000, 1), (1, 120000, 1), (0, 1, 10)]:
        for index in range(repeats):
            rows.append({"item_id": 2589, "market": MARKET, "observed_at": (end-timedelta(days=day)+timedelta(minutes=index)).isoformat(),
                "median_copper": value, "granularity": "hour", "range": "7d", "raw_id": "fixture", "retrieved_at": end.isoformat()})
    workspace.market.insert_rows("price_history_ranges", rows)
    result = workspace.sniper.reference(rule.id)
    assert result["copper"] == 100000
    assert result["days"] == 3


def test_game_rule_reports_idempotent_and_web_changes_win_conflict(workspace):
    rule = SnipeRule(item_id=2589)
    event = {"id": "fixture:snipe:1", "kind": "rule_update", "rule": rule.model_dump(mode="json")}
    data = {"workspace_id": workspace.identity, "events": [event]}
    assert workspace.sniper.accept_game(data) == [event["id"]]
    revision = workspace.sniper.listing()["revision"]
    workspace.sniper.accept_game(data)
    assert workspace.sniper.listing()["revision"] == revision
    workspace.sniper.save(rule.model_copy(update={"quantity": 100}))
    data["events"] = [{**event, "id": "fixture:snipe:2", "base_revision": revision}]
    workspace.sniper.accept_game(data)
    assert workspace.sniper.listing()["rules"][0]["quantity"] == 100
    assert workspace.sniper.listing()["reports"][0]["conflict"]


def test_game_import_rejects_entire_merge_when_existing_identity_changes(exchange):
    lua = game(exchange)
    lua.execute("""
      local added=Cennik:Clone(definition); added.id='new'; added.item_id=2592
      local invalid=Cennik:Clone(definition); invalid.item_id=5763
      local before=#Cennik.db.sniper.events
      local ok=Cennik.Sniper:Apply({budget=999,rules={added,invalid}})
      assert(not ok and Cennik.db.sniper.rules.new==nil)
      assert(#Cennik.db.sniper.events==before and r.definition.item_id==2589)
    """)


def test_web_import_is_atomic_and_identity_cannot_reuse_purchase_counts(workspace):
    old = SnipeRule(id="old", item_id=2589)
    workspace.sniper.save(old)
    text = encode_rules([SnipeRule(id="new", item_id=2592), old.model_copy(update={"item_id": 5763})], 999)
    with pytest.raises(ValueError, match="создайте новую"):
        workspace.sniper.apply(text)
    assert workspace.sniper.wire()["rules"] == [old.model_dump(mode="json")]


def test_paused_goal_ignores_late_results(exchange):
    lua = game(exchange)
    lua.execute("Cennik.Sniper:Tick(0.1); r.enabled=false")
    lua.execute("Game.event('COMMODITY_SEARCH_RESULTS_UPDATED',2589)")
    assert lua.eval("Cennik.Sniper.alerts[r.definition.id]") is None


def test_commodity_requote_above_limit_never_buys_and_partial_fill_counts_once(exchange):
    lua = game(exchange, mode="limit")
    lua.execute("Game.commodity[2589][1].quantity=7")
    found(lua)
    lua.execute("Game.click(function() Cennik.Sniper:Buy(r.definition.id) end)")
    lua.execute("Game.event('COMMODITY_PRICE_UPDATED',25,175)")
    assert lua.eval("r.bought") == 0
    assert lua.eval("Cennik.db.pending") is None
    lua.execute("Game.advance(2)")
    found(lua)
    lua.execute("Game.click(function() Cennik.Sniper:Buy(r.definition.id) end); Game.event('COMMODITY_PRICE_UPDATED',10,70)")
    lua.execute("Game.click(function() Cennik.Execution:Confirm() end); Game.event('COMMODITY_PURCHASE_SUCCEEDED',2589); Game.event('COMMODITY_PURCHASE_SUCCEEDED',2589)")
    assert lua.eval("r.bought") == 7
    assert lua.eval("Cennik.Sniper:Remaining(r)") == 18
    assert lua.eval("Cennik.db.sniper.spent") == 70


def test_game_sniper_panel_switches_tabs_and_prefilled_material_creates_new_goal(exchange):
    lua = game(exchange)
    lua.execute("""
      Cennik.UI:Attach(); Cennik.UI:Show(); Cennik.UI:SelectSection('Sniper'); Game.advance(0)
      assert(#Cennik.UI.sniperRows==1)
      Cennik.UI:SelectSection('History'); Cennik.UI:SelectSection('Sniper'); Game.advance(0)
      assert(Cennik.UI.sniperRows[1].font:IsVisible())
      Cennik.Sniper:ShowEditor(2592,nil,{item_id=2592,quantity=17,mode='any',priority='normal',discount_bps=5000,unit_limit_copper=0,use_bags=true})
      local p=Cennik.Sniper.editor
      for _,frame in ipairs(Game.frames) do
        if frame.parent==p and frame.text=='Сохранить цель' then frame.scripts.OnClick(frame); break end
      end
      local count=0
      for _,rule in ipairs(Cennik.Sniper:Rules()) do
        count=count+1
        if rule.definition.item_id==2592 then assert(rule.definition.quantity==17 and rule.stock==5 and rule.goal==12 and not rule.enabled) end
      end
      assert(count==2)
    """)


def test_own_three_day_reference_uses_archive_and_preserves_old_scans(workspace):
    from test_workspace import scan

    end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    for index in (1, 2, 3):
        data = scan(offset=index)
        data["observed_at"] = int((end-timedelta(days=index)+timedelta(hours=12)).timestamp())
        workspace.market.import_scan(data)
    rule = SnipeRule(item_id=2589, mode="median3d")
    workspace.sniper.save(rule)
    result = workspace.sniper.reference(rule.id)
    assert result["source"] == "Cennik replicate"
    assert result["copper"] == 10
    assert len(workspace.market.rows("SELECT * FROM scan_registry")) == 3


def test_zero_money_never_blocks_finding_and_blocked_purchase_keeps_alert(exchange):
    lua = game(exchange)
    lua.execute("Game.money=0; Cennik.db.sniper.budget_copper=1")
    found(lua)
    assert lua.eval("Cennik.Sniper.alerts[r.definition.id].cost") == 250
    assert "недостаточно золота" in lua.eval("r.reason")
    assert lua.eval("Cennik.Sniper:Buy(r.definition.id)")[0] is False
    assert lua.eval("Cennik.Sniper.alerts[r.definition.id]") is not None
    assert lua.eval("Cennik.db.pending") is None
    lua.execute("Game.money=1000; Game.click(function() assert(Cennik.Sniper:Buy(r.definition.id)) end)")
    assert lua.eval("Cennik.db.pending.stage") == "await_quote"


def test_command_selects_sniper_after_tab_onshow_and_accepts_whitespace(exchange):
    lua = game(exchange)
    lua.execute("""
      Cennik.UI:Attach(); Cennik.UI.section='History'
      Cennik.UI.frame:SetScript('OnShow',function() Cennik.UI.section='Opportunities' end)
      SlashCmdList.CENNIK('  SniPer  '); Game.advance(0)
      assert(Cennik.UI.section=='Sniper' and Cennik.UI.frame:IsVisible())
    """)


def test_delete_hidden_keeps_audit_and_import_cannot_resurrect_deleted_task(exchange):
    lua = game(exchange)
    lua.execute("""
      assert(not Cennik:DeleteHiddenTask(t)); assert(Cennik:HideTask(t))
      local count=#Cennik.db.history; assert(Cennik:DeleteHiddenTask(t))
      assert(Cennik.db.tasks[t.task_id]==nil and #Cennik.db.history>count)
      Cennik:Import(); assert(Cennik.db.tasks[t.task_id]==nil)
      assert(Cennik.db.history[#Cennik.db.history].item_id==t.item_id)
    """)


def test_manual_other_character_purchase_preserves_unknown_cost(exchange):
    lua = game(exchange)
    lua.execute("assert(Cennik.Sniper:MarkBought(r.definition.id,7))")
    assert lua.eval("Cennik.Sniper:Remaining(r)") == 18
    assert lua.eval("r.spent") == 0
    assert lua.eval("Cennik.db.sniper.events[#Cennik.db.sniper.events].cost_unknown")
    assert not lua.eval("Cennik.Sniper:MarkBought(r.definition.id,19)")[0]
    lua.execute("Cennik.Sniper:ShowHistory()")


def test_explicit_method_controls_and_recipe_bulk_keep_direct_reagents(exchange):
    lua = game(exchange)
    lua.execute("""
      CennikImport.sniper_catalog={workspace_id='test-scope',items={{item_id=2592,name='Material'},{item_id=2321,name='Thread',vendor_buy_copper=50}},
        recipes={{spell_id=100,version_hash='latest',item_id=5763,qty_min=1,qty_max=1,profession='Tailoring',materials={{item_id=2592,quantity=3},{item_id=2321,quantity=1}}}}}
      Cennik.Sniper:ShowEditor(5763); local editor=Cennik.Sniper.editor
      assert(not editor.priceInput:IsShown())
      editor.modeButtons.limit.scripts.OnClick(); assert(editor.priceGold:IsShown() and not editor.priceInput:IsShown())
      editor.priceSilver:SetText('12'); editor.priceCopper:SetText('50'); editor.modeButtons.median3d.scripts.OnClick()
      assert(editor.priceInput:GetText()=='50'); editor.modeButtons.any.scripts.OnClick()
      assert(not editor.priceInput:IsShown())
      Cennik.Sniper:ShowCard(5763)
      Cennik.Sniper:ShowMaterialPreview(Cennik.Sniper:Recipes(5763)[1],4)
      for _,frame in ipairs(Game.frames) do if frame.parent==Cennik.Sniper.materialPreview and frame.text=='Добавить материалы' then frame.scripts.OnClick(); break end end
      local found=0
      for _,rule in ipairs(Cennik.Sniper:Rules()) do
        assert(rule.definition.item_id~=2321)
        if rule.definition.item_id==2592 then found=found+1; assert(rule.definition.quantity==12 and not rule.enabled) end
      end
      assert(found==1)
    """)


def test_name_lookup_and_auctionator_selection_open_ready_item_without_changing_auctionator(exchange):
    lua = game(exchange)
    lua.execute("""
      CennikImport.sniper_catalog={workspace_id='test-scope',items={{item_id=5763,name='Red Woolen Bag'}},recipes={}}
      local selected
      local original=C_Item.GetItemInfo
      C_Item.GetItemInfo=function(value) assert(type(value)~='number','Name search must not request every item'); return original(value) end
      assert(Cennik.Sniper:Lookup('Woolen',function(rows) selected=rows[1].item_id end)); assert(selected==5763)
      C_Item.GetItemInfo=original
      Auctionator.Buying={Events={ShowItemBuy='item buy',ShowCommodityBuy='commodity buy'}}
      Cennik.Sniper:InstallAuctionatorButtons()
      Cennik.Sniper.auctionatorListener:ReceiveEvent('item buy',{itemKey=C_AuctionHouse.MakeItemKey(5763)})
      assert(Cennik.Sniper.auctionatorShortcut:IsVisible())
      for _,frame in ipairs(Game.frames) do if frame.parent==Cennik.Sniper.auctionatorShortcut and frame.text=='В снайпер' then frame.scripts.OnClick(); break end end
      assert(Cennik.Sniper.editor.itemEntry:GetText()=='5763')
    """)


def seed_recipe(workspace):
    import json

    from test_cennik import insert

    db = workspace.market.db
    for item, name, npc in [(5763, "Fixture bag", None), (2592, "Fixture cloth", None), (2321, "Fixture thread", 50)]:
        insert(db, "items", item_id=item, name=name, vendor_buy_copper=npc)
    for version, quantity, hours in [("old", 30, 2), ("latest", 3, 1)]:
        at = datetime.now(UTC)-timedelta(hours=hours)
        raw = "recipe-"+version
        insert(db, "recipes", spell_id=100, version_hash=version, result_item_id=5763, qty_min=1, qty_max=1,
            profession="Tailoring", raw_id=raw, retrieved_at=at,
            payload_json=json.dumps({"reagents": [{"id": 2592, "qty": quantity}, {"id": 2321, "qty": 1}]}))
        insert(db, "recipe_inputs", spell_id=100, version_hash=version, item_id=2592, quantity=quantity)
        insert(db, "recipe_inputs", spell_id=100, version_hash=version, item_id=2321, quantity=1)
        insert(db, "dataset_metadata", raw_id=raw, dataset="crafting", market=MARKET, status="COMPLETE", retrieved_at=at)


def test_catalog_only_current_recipe_and_bulk_does_not_expand_or_add_npc(workspace):
    seed_recipe(workspace)
    recipes = workspace.sniper.recipes_for(5763)
    assert [r["version_hash"] for r in recipes] == ["latest"]
    request = SnipeMaterials(item_id=5763, spell_id=100, version_hash="latest", batches=4)
    preview = workspace.sniper.material_preview(request)
    lines = {r["item_id"]: r for r in preview["lines"]}
    assert lines[2592]["quantity"] == 12
    assert lines[2321]["npc"]
    workspace.sniper.add_materials(request)
    rules = workspace.sniper.wire()["rules"]
    assert len(rules) == 1 and rules[0]["item_id"] == 2592 and rules[0]["quantity"] == 12
    with pytest.raises(ValueError, match="Действующий рецепт"):
        workspace.sniper.add_materials(request.model_copy(update={"version_hash": "old"}))


def test_shift_link_only_fills_focused_sniper_editor_and_keeps_original_handler(exchange):
    lua = game(exchange)
    lua.execute("""
      HandleModifiedItemClick=function(link) Game.linkCalls=(Game.linkCalls or 0)+1 end
      IsShiftKeyDown=function() return true end
      Cennik.Sniper:ShowEditor(5763)
      HandleModifiedItemClick('|Hitem:2592|h[Cloth]|h')
      assert(Cennik.Sniper.editor.itemEntry:GetText()=='|Hitem:2592|h[Cloth]|h')
      Cennik.Sniper.editor.itemEntry:ClearFocus()
      HandleModifiedItemClick('|Hitem:2321|h[Thread]|h')
      assert(Cennik.Sniper.editor.itemEntry:GetText()=='|Hitem:2592|h[Cloth]|h' and Game.linkCalls==2)
    """)


def test_recipe_request_outbox_is_idempotent_and_explicit(workspace):
    event = {"id": "fixture:recipe:1", "kind": "recipe_request", "item_id": 5763}
    data = {"workspace_id": workspace.identity, "events": [event]}
    workspace.sniper.accept_game(data)
    workspace.sniper.accept_game(data)
    assert workspace.sniper.pending_recipe_requests() == [event["id"]]
    workspace.sniper.complete_recipe_requests([event["id"]])
    workspace.sniper.accept_game(data)
    assert not workspace.sniper.pending_recipe_requests()


def test_group_of_twelve_single_bags_uses_actual_auction_quantity_not_search_count(exchange):
    lua = game(exchange, quantity=1, gear=True)
    lua.execute("""
      C_Item.GetItemInfoInstant=function(id) return id,'Container','Bag','',1,1 end
      Game.items[2589][1].quantity=12
      C_AuctionHouse.GetAuctionInfoByID=function(id) return {auctionID=id,itemKey=C_AuctionHouse.MakeItemKey(2589),quantity=1,buyoutAmount=400,containsOwnerItem=false,containsAccountItem=false} end
    """)
    found(lua, gear=True)
    assert lua.eval("Cennik.Sniper.alerts[r.definition.id].quantity") == 1
    assert lua.eval("Cennik.Sniper.alerts[r.definition.id].cost") == 400
    assert lua.eval("r.last_result.search_quantity") == 12
    assert lua.eval("Cennik.Sniper.overlay.buy:GetText()") == "Купить 1 шт. за 400c"


def test_nonstackable_search_group_represents_one_item_at_full_buyout_price(exchange):
    lua = game(exchange, quantity=1, gear=True)
    lua.execute("C_Item.GetItemInfoInstant=function(id) return id,'Container','Bag','',1,1 end; Game.items[2589][1].quantity=12")
    found(lua, gear=True)
    assert lua.eval("Cennik.Sniper.alerts[r.definition.id].quantity") == 1
    assert lua.eval("Cennik.Sniper.alerts[r.definition.id].unit_price") == 400
    assert lua.eval("Cennik.Sniper.alerts[r.definition.id].cost") == 400
    assert lua.eval("r.last_result.grouped_single_rows") == 1


def test_findings_keep_selection_content_and_action_during_refresh(exchange):
    lua = game(exchange, quantity=1)
    found(lua)
    lua.execute("""
      local def=Cennik.Sniper:NewDefinition(2592,1); assert(Cennik.Sniper:Add(def))
      other=Cennik.db.sniper.rules[def.id]; other.enabled=true
      Cennik.Sniper:AcceptRows(other,{{item_id=2592,item_key=C_AuctionHouse.MakeItemKey(2592),commodity=true,quantity=5,unit_price=25,total_price=125,ownership_known=true,owned=false}})
      assert(Cennik.Sniper.selected==r.definition.id)
      Cennik.Sniper:BeginDetail(r,C_AuctionHouse.MakeItemKey(2589)); Cennik.Sniper:RenderOverlay()
      assert(Cennik.Sniper.overlay.buy:IsEnabled() and Cennik.Sniper.overlay.buy:GetText()=='Купить 1 шт. за 10c')
      assert(Cennik.Sniper.overlay.content:GetText():find('Уже есть на аукционе',1,true))
      local row=Cennik.Sniper.overlay.rows[2]; row.scripts.OnClick(row)
      assert(Cennik.Sniper.selected==other.definition.id)
      Cennik.Sniper:AcceptRows(other,{})
      assert(Cennik.Sniper.selected==other.definition.id)
      assert(Cennik.Sniper.overlay.content:GetText():find('Выбранный лот недоступен',1,true))
    """)


def test_popup_background_close_buttons_and_manual_reopen_do_not_stop_search(exchange):
    lua = game(exchange)
    found(lua)
    lua.execute("""
      Cennik.Sniper:ShowEditor(5763); Cennik.Sniper:ShowCard(5763); Cennik.Sniper:ShowTransfer(); Cennik.Sniper:ShowHistory()
      for p in pairs(Cennik.Sniper.popupFrames) do
        assert(p.cennikBackground.color[4]==1 and p.cennikClose)
        p.cennikClose.scripts.OnClick(); assert(not p:IsShown())
      end
      assert(r.enabled)
      Cennik.Sniper:RenderOverlay(); assert(not Cennik.Sniper.overlay:IsShown())
      Cennik.Sniper:ShowFindings(); assert(Cennik.Sniper.overlay:IsShown() and r.enabled)
    """)


@pytest.mark.parametrize("gear", [False, True])
def test_all_offers_opens_native_buy_for_exact_key_without_purchasing_or_resuming(exchange, gear):
    lua = game(exchange, gear=gear)
    lua.execute("Cennik.UI:Attach(); Cennik.UI:Show(); Cennik.UI:SelectSection('Sniper')")
    if gear:
        lua.execute("""
          r.keys[1].itemLevel=9; r.keys[1].itemSuffix=17
          Game.items[2589][1].itemKey=Cennik:Clone(r.keys[1])
          Cennik.Sniper:Tick(0.1); Game.event('ITEM_SEARCH_RESULTS_UPDATED',r.keys[1])
        """)
    else:
        found(lua)
    lua.execute("""
      local s=Cennik.Sniper; local offer=s.alerts[r.definition.id]
      assert(offer and s.overlay.offers:IsEnabled())
      AuctionHouseFrameDisplayMode={Buy={'nativeBrowse'}}
      local setMode=AuctionHouseFrame.SetDisplayMode
      AuctionHouseFrame.SetDisplayMode=function(self,mode)
        if mode==AuctionHouseFrameDisplayMode.Buy then Game.buyTabSelected=true end
        setMode(self,mode)
      end
      AuctionHouseFrame.SelectBrowseResult=function(self,row)
        assert(s.manualHold and not s.current and Game.buyTabSelected)
        Game.nativeSelection=Cennik:Clone(row); Cennik.UI.frame:Hide()
        self:SetDisplayMode({'nativeBuy'})
        C_AuctionHouse.SendSearchQuery(row.itemKey,{},true)
      end
      Game.click(function() s.overlay.offers.scripts.OnClick() end)
      assert(Game.nativeSelection and Cennik.Adapter:KeyEqual(Game.nativeSelection.itemKey,offer.key))
      assert(s.overlay:IsShown() and not s.overlayHidden and s.manualHold and r.enabled)
      assert(s.activeWindow==AuctionHouseFrame)
      assert(AuctionHouseFrame:GetFrameLevel()>s.overlay:GetFrameLevel())
      assert(not Cennik.db.pending and r.bought==0)
      for _,call in ipairs(Game.calls) do assert(call.kind=='search') end
      local calls=#Game.calls
      Game.advance(65); s:Tick(0.1)
      assert(#Game.calls==calls and s.overlay:IsShown())
      SlashCmdList.CENNIK('sniper'); s:ShowFindings()
      assert(s.manualHold and s.overlay.resume:IsShown())
      assert(s.alerts[r.definition.id]==offer)
    """)
    assert lua.eval("Game.nativeSelection.itemKey.itemSuffix") == (17 if gear else 0)


def test_all_offers_blocks_during_purchase_and_unknown_equipment_variant(exchange):
    lua = game(exchange, gear=True)
    found(lua, gear=True)
    lua.execute("""
      local s=Cennik.Sniper
      Cennik.db.pending=Cennik:Clone(s.alerts[r.definition.id]); Cennik.db.pending.sniper=true; Cennik.db.pending.stage='unknown'
      s:RenderOverlay(); assert(not s.overlay.offers:IsEnabled())
      assert(not s:ShowOffers() and not s.manualHold)
      Cennik.db.pending=nil
      s.alerts[r.definition.id]=nil; r.last_finding=nil; r.keys=nil
      r.definition.item_level=nil; r.definition.item_suffix=nil; r.definition.pet_id=nil
      assert(not s:ShowOffers() and #Game.calls==1)
      assert(s.message:find('выберите нужный вариант',1,true))
    """)


def test_finding_details_scroll_preserves_position_until_selection_changes(exchange):
    lua = game(exchange)
    found(lua)
    lua.execute("""
      local s=Cennik.Sniper; local p=s.overlay
      -- Simulate the measured height of wrapped localized text in the client.
      p.content.GetStringHeight=function() return 450 end
      Game.money=0; s:RenderOverlay()
      assert(p.contentScroll:GetScrollChild()==p.contentBody and p.contentBody:GetHeight()>p.contentScroll:GetHeight())
      assert(p.contentScroll.clipsChildren and p.content.parent==p.contentBody)
      p.contentScroll:SetVerticalScroll(100)
      local updates=p.contentScroll.rectUpdates
      for i=1,10 do s:RenderOverlay() end
      assert(p.contentScroll:GetVerticalScroll()==100 and p.contentScroll.rectUpdates==updates)
      Game.money=1000000; s:RenderOverlay()
      assert(p.contentScroll:GetVerticalScroll()==100)
      local def=Cennik:Clone(definition); def.id='second'; def.item_id=5763
      assert(s:Add(def)); s:ShowFindings(def.id)
      assert(p.contentScroll:GetVerticalScroll()==0)
      p.content.GetStringHeight=function() return 28 end
      s.selected=r.definition.id; s:RenderOverlay()
      assert(p.contentScroll:GetVerticalScrollRange()==0)
    """)


def test_findings_global_pause_and_resume_include_paused_but_not_completed_targets(exchange):
    lua = game(exchange)
    found(lua)
    lua.execute("""
      local s=Cennik.Sniper; local p=s.overlay
      for _,id in ipairs({'paused','completed'}) do
        local d=Cennik:Clone(definition); d.id=id; assert(s:Add(d))
      end
      Cennik.db.sniper.rules.completed.bought=definition.quantity
      p.pauseAll.scripts.OnClick()
      assert(not r.enabled and not s.current and p:IsShown())
      s:HoldForManualSearch('Ручной поиск')
      p.resumeAll.scripts.OnClick()
      assert(r.enabled and Cennik.db.sniper.rules.paused.enabled)
      assert(not Cennik.db.sniper.rules.completed.enabled and not s.manualHold)
      assert(p.resumeAll:GetText()=='Продолжить все' and p.pauseAll:GetText()=='Пауза все')
      assert(p.offers:GetText()=='Посмотреть лоты' and Cennik.UI.section=='Sniper')
      assert(not Cennik.db.pending and #Game.calls==1)
    """)


def test_finding_border_flashes_once_then_stays_lit_without_poll_restarts(exchange):
    lua = game(exchange, gear=True)
    found(lua, gear=True)
    lua.execute("""
      local s=Cennik.Sniper; local p=s.overlay; local key=C_AuctionHouse.MakeItemKey(2589)
      local icon=p.foundIcons[r.definition.id]
      assert(icon:IsShown() and icon.image.texture==12345)
      local started=p.flashStarted; local signal=s.findingSignal
      Game.advance(0.25); s:RenderOverlay(); assert(p.borderLit and p.flashStarted==started)
      Game.advance(0.5); p.scripts.OnUpdate(); assert(not p.borderLit)
      Game.advance(0.25)
      p.scripts.OnUpdate(); assert(p.borderLit)
      Game.advance(10); s:AcceptRows(r,Cennik.Adapter:Results(2589,key,false),key)
      assert(p.borderLit and p.flashStarted==started and s.findingSignal==signal)
      Game.items[2589][2]={quantity=1,buyoutAmount=500,containsOwnerItem=false,containsAccountItem=false,auctionID=124,itemKey=key}
      s:AcceptRows(r,Cennik.Adapter:Results(2589,key,false),key)
      assert(s.findingSignal==signal+1 and p.flashStarted==GetTime())
      -- Unread icons stay visible when another target is selected.
      local d=Cennik:Clone(definition); d.id='other'; d.item_id=5763; assert(s:Add(d))
      s:ShowFindings('other'); assert(icon:IsShown() and s.findingHighlight.item_id==2589)
      icon.scripts.OnClick(icon); assert(s.selected==r.definition.id)
      assert(not icon:IsShown() and not p.borderActive and s.unreadCount==0)
    """)


def test_add_dialog_stays_above_large_findings_dashboard_when_new_lot_arrives(exchange):
    lua = game(exchange, gear=True)
    found(lua, gear=True)
    lua.execute("""
      local s=Cennik.Sniper; local p=s.overlay
      p.add.scripts.OnClick()
      assert(s.editor:IsShown() and s.editor:GetFrameLevel()>p:GetFrameLevel())
      local key=C_AuctionHouse.MakeItemKey(2589)
      Game.items[2589][2]={quantity=1,buyoutAmount=500,containsOwnerItem=false,containsAccountItem=false,auctionID=124,itemKey=key}
      s:AcceptRows(r,Cennik.Adapter:Results(2589,key,false),key)
      assert(s.editor:IsShown() and s.editor:GetFrameLevel()>p:GetFrameLevel())
      s.editor.cennikClose.scripts.OnClick(); assert(p:IsShown() and r.enabled)
      for i=1,60 do local d=Cennik:Clone(definition); d.id='many-' .. i; assert(s:Add(d)) end
      s:RenderOverlay()
      assert(p.scroll:GetHeight()>=400 and p.scroll:GetVerticalScrollRange()>0)
      assert(p.content.font[2]==14 and p.rows[1].title.font[2]==13)
    """)


def test_unread_targets_coalesce_acknowledge_individually_and_rearm_without_changing_purchase(exchange):
    lua = game(exchange)
    found(lua)
    lua.execute("""
      local s=Cennik.Sniper; local p=s.overlay; local key=C_AuctionHouse.MakeItemKey(2589)
      local d=Cennik:Clone(definition); d.id='other'; d.item_id=5763
      assert(s:Add(d)); local other=Cennik.db.sniper.rules.other; other.enabled=true
      Game.commodity[5763]={{quantity=50,unitPrice=15,numOwnerItems=0}}
      local otherKey=C_AuctionHouse.MakeItemKey(5763)
      s:AcceptRows(other,Cennik.Adapter:Results(5763,otherKey,true),otherKey)
      local first,second=p.foundIcons[r.definition.id],p.foundIcons.other
      assert(s.unreadCount==2 and first:IsShown() and second:IsShown())
      assert(first~=second and first.layoutIndex==1 and second.layoutIndex==2)
      assert(p.finding:GetText():find('2',1,true))
      Game.commodity[2589][1].quantity=110
      s:AcceptRows(r,Cennik.Adapter:Results(2589,key,true),key)
      assert(p.foundIcons[r.definition.id]==first and p.foundIcons.other==second)
      assert(first.layoutIndex==1 and second.layoutIndex==2 and s.unreadCount==2)
      local original=s.alerts[r.definition.id]; local otherOffer=s.alerts.other
      p.rows[1].scripts.OnClick(p.rows[1])
      assert(s.selected==r.definition.id and not s.unreadFindings[r.definition.id])
      assert(s.unreadCount==1 and p.borderActive and not first:IsShown() and second:IsShown())
      assert(s.alerts[r.definition.id]==original and s.alerts.other==otherOffer)
      second.scripts.OnClick(second)
      assert(s.selected=='other' and s.unreadCount==0 and not second:IsShown())
      assert(not p.borderActive and s.alerts.other==otherOffer)
      for _,edge in ipairs(p.redBorder) do assert(edge:GetAlpha()==0) end
      local signal=s.findingSignal
      s:AcceptRows(r,Cennik.Adapter:Results(2589,key,true),key)
      assert(s.unreadCount==0 and s.findingSignal==signal)
      Game.commodity[2589][1].quantity=90
      s:AcceptRows(r,Cennik.Adapter:Results(2589,key,true),key)
      Game.commodity[2589][1].quantity=80
      s:AcceptRows(r,Cennik.Adapter:Results(2589,key,true),key)
      assert(s.unreadCount==1 and s.findingSignal==signal+2 and first:IsShown())
      assert(s.unreadFindings[r.definition.id].finding.available==80)
      s:AcceptRows(r,Cennik.Adapter:Results(2589,key,true),key)
      assert(s.unreadCount==1 and s.findingSignal==signal+2)
      p.rows[1].scripts.OnClick(p.rows[1])
      Game.commodity[2589]={}
      s:AcceptRows(r,Cennik.Adapter:Results(2589,key,true),key)
      assert(s.unreadCount==1 and not s.alerts[r.definition.id] and not p.buy:IsEnabled())
      assert(s.findingNotice:find('недоступно',1,true))
      signal=s.findingSignal
      s:MissingFindings(r,key); s:RenderOverlay(); assert(s.findingSignal==signal)
    """)


def test_finding_icons_scroll_when_many_targets_have_unread_changes(exchange):
    lua = game(exchange)
    found(lua)
    lua.execute("""
      local s=Cennik.Sniper; local p=s.overlay
      local original=s.alerts[r.definition.id]
      for index=1,24 do
        local d=Cennik:Clone(definition); d.id='notice-' .. index; d.item_id=6000+index
        assert(s:Add(d)); local rule=Cennik.db.sniper.rules[d.id]
        local lot=Cennik:Clone(original); lot.rule_id=d.id; lot.item_id=d.item_id
        s:NotifyFinding(rule,lot,'Новое предложение',true)
      end
      s:RenderOverlay()
      assert(s.unreadCount==25 and p.iconsScroll:GetScrollChild()==p.iconsBody)
      assert(p.iconsScroll.clipsChildren and p.iconsScroll:GetVerticalScrollRange()>0)
      local first=p.foundIcons[r.definition.id]; local last=p.foundIcons['notice-24']
      assert(first:IsVisible() and last:IsShown() and last.layoutIndex==25)
      p.iconsScroll:SetVerticalScroll(56); local updates=p.iconsScroll.rectUpdates
      for index=1,10 do s:RenderOverlay() end
      assert(p.iconsScroll:GetVerticalScroll()==56 and p.iconsScroll.rectUpdates==updates)
      last.scripts.OnClick(last)
      assert(not last:IsShown() and first:IsShown() and s.unreadCount==24)
      assert(s.selected=='notice-24' and s.alerts[r.definition.id]==original)
    """)


def test_reappearing_ordinary_auction_notifies_again_after_its_disappearance_was_read(exchange):
    lua = game(exchange, gear=True)
    found(lua, gear=True)
    lua.execute("""
      local s=Cennik.Sniper; local p=s.overlay; local key=C_AuctionHouse.MakeItemKey(2589)
      local icon=p.foundIcons[r.definition.id]; icon.scripts.OnClick(icon)
      local row=Game.items[2589][1]; Game.items[2589]={}
      s:AcceptRows(r,Cennik.Adapter:Results(2589,key,false),key)
      assert(s.unreadCount==1 and not s.alerts[r.definition.id])
      icon.scripts.OnClick(icon); assert(s.unreadCount==0)
      Game.items[2589]={row}
      s:AcceptRows(r,Cennik.Adapter:Results(2589,key,false),key)
      assert(s.unreadCount==1 and s.findingHighlight.auction_id==123)
      local signal=s.findingSignal
      s:AcceptRows(r,Cennik.Adapter:Results(2589,key,false),key)
      assert(s.findingSignal==signal)
    """)


def test_native_auction_and_task_dialog_follow_clicked_child_without_scan_focus_steal(exchange):
    lua = game(exchange, gear=True)
    found(lua, gear=True)
    lua.execute("""
      local s=Cennik.Sniper; local p=s.overlay
      s:ShowFindings(); assert(p:GetFrameLevel()>AuctionHouseFrame:GetFrameLevel())
      p.add.scripts.OnClick(); local editor=s.editor
      assert(editor:GetFrameLevel()>p:GetFrameLevel())
      local nativePanel=CreateFrame('Frame',nil,AuctionHouseFrame)
      local nativeButton=CreateFrame('Button',nil,nativePanel)
      Game.mouseFoci={nativeButton}; Game.event('GLOBAL_MOUSE_DOWN','LeftButton')
      assert(s.activeWindow==AuctionHouseFrame and AuctionHouseFrame:GetFrameLevel()>editor:GetFrameLevel())
      local before=AuctionHouseFrame:GetFrameLevel()
      local key=C_AuctionHouse.MakeItemKey(2589)
      Game.items[2589][2]={quantity=1,buyoutAmount=500,containsOwnerItem=false,containsAccountItem=false,auctionID=124,itemKey=key}
      s:AcceptRows(r,Cennik.Adapter:Results(2589,key,false),key)
      assert(s.activeWindow==AuctionHouseFrame and AuctionHouseFrame:GetFrameLevel()==before)
      assert(p:GetFrameLevel()<before and editor:IsShown())
      Cennik.UI:ShowTaskDetails(t); local details=Cennik.UI.taskDetails
      assert(details:GetFrameLevel()>AuctionHouseFrame:GetFrameLevel())
      Game.mouseFoci={p.rows[1]}; Game.event('GLOBAL_MOUSE_DOWN','LeftButton')
      assert(p:GetFrameLevel()>details:GetFrameLevel())
      Game.mouseFoci={details.check}; Game.event('GLOBAL_MOUSE_DOWN','LeftButton')
      assert(details:GetFrameLevel()>p:GetFrameLevel())
      GetMouseFoci=nil; GetMouseFocus=function() return nativeButton end
      Game.event('GLOBAL_MOUSE_DOWN','LeftButton')
      assert(AuctionHouseFrame:GetFrameLevel()>details:GetFrameLevel())
    """)


def test_findings_raise_again_when_native_auction_changed_levels_outside_focus_manager(exchange):
    lua = game(exchange)
    found(lua)
    lua.execute("""
      local s=Cennik.Sniper; local p=s.overlay
      s:ShowFindings(); assert(s.activeWindow==p and p.toplevel)
      AuctionHouseFrame:SetFrameLevel(p:GetFrameLevel()+1000)
      local before=Game.raiseCount
      -- The cached activeWindow is still p, but the game raised the AH.
      s:ShowFindings()
      assert(p:GetFrameLevel()>AuctionHouseFrame:GetFrameLevel())
      assert(Game.raised==p and Game.raiseCount>before)
      -- Child controls must also work without the optional global event.
      AuctionHouseFrame:SetFrameLevel(p:GetFrameLevel()+1000)
      p.rows[1].scripts.OnClick(p.rows[1])
      assert(Game.raised==p and p:GetFrameLevel()>AuctionHouseFrame:GetFrameLevel())
      AuctionHouseFrame:SetFrameLevel(p:GetFrameLevel()+1000)
      p.scripts.OnMouseDown(p,'LeftButton')
      assert(Game.raised==p and p:GetFrameLevel()>AuctionHouseFrame:GetFrameLevel())
      p.add.scripts.OnClick()
      assert(Game.raised==s.editor and s.editor:GetFrameLevel()>p:GetFrameLevel())
    """)


def test_mouse_focus_order_does_not_send_findings_behind_auction_parent(exchange):
    lua = game(exchange)
    found(lua)
    lua.execute("""
      local s=Cennik.Sniper; local p=s.overlay
      s:ShowFindings()
      Game.mouseFoci={AuctionHouseFrame,p.rows[1]}
      Game.event('GLOBAL_MOUSE_DOWN','LeftButton')
      assert(s.activeWindow==p and Game.raised==p)
      assert(p:GetFrameLevel()>AuctionHouseFrame:GetFrameLevel())
    """)


def test_edit_dialog_stays_in_front_after_opener_is_raised_at_end_of_click(exchange):
    lua = game(exchange)
    lua.execute("UIParent=CreateFrame('Frame')")
    found(lua)
    lua.execute("""
      local s=Cennik.Sniper; s:ShowFindings(); local p=s.overlay
      Game.mouseFoci={p.edit}; Game.event('GLOBAL_MOUSE_DOWN','LeftButton')
      p.edit.scripts.OnMouseDown(p.edit,'LeftButton')
      p.edit.scripts.OnClick(p.edit)
      local editor=s.editor
      assert(editor:IsVisible() and Game.focused==editor.itemEntry)
      -- Simulate the engine's toplevel raise of the clicked owner after
      -- its callback has opened another window. Levels alone are insufficient.
      p:SetFrameLevel(editor:GetFrameLevel()+1000); p:Raise()
      assert(s.activeWindow==editor and editor:GetFrameStrata()=='DIALOG')
      assert(p:GetFrameStrata()=='MEDIUM')
      s:RenderOverlay()
      assert(editor:GetFrameStrata()=='DIALOG' and p:GetFrameStrata()=='MEDIUM')
      -- A subsequent real click must still be able to activate the auction.
      local nativeButton=CreateFrame('Button',nil,AuctionHouseFrame)
      Game.mouseFoci={nativeButton}; Game.event('GLOBAL_MOUSE_DOWN','LeftButton')
      assert(s.activeWindow==AuctionHouseFrame and editor:GetFrameStrata()=='MEDIUM')
      assert(AuctionHouseFrame:GetFrameLevel()>editor:GetFrameLevel())
      -- Reopening the hidden findings while editing is passive.
      s:ShowEditor(5763); editor=s.editor; p:Hide(); s:RenderOverlay()
      assert(p:IsShown() and s.activeWindow==editor and p:GetFrameStrata()=='MEDIUM')
      assert(editor:GetFrameStrata()=='DIALOG')
    """)


def test_nested_catalog_and_auxiliary_dialogs_open_above_their_owner(exchange):
    lua = game(exchange)
    lua.execute("UIParent=CreateFrame('Frame')")
    found(lua)
    lua.execute("""
      local s=Cennik.Sniper
      local function click(window,caption)
        for i=#Game.frames,1,-1 do
          local b=Game.frames[i]
          if b.kind=='Button' and b:GetText()==caption and s:WindowForControl(b)==window then
            b.scripts.OnClick(b); return
          end
        end
        error('Button not found: ' .. caption)
      end
      local function above(previous)
        local current=s.activeWindow
        assert(current~=previous and current:IsVisible())
        assert(current:GetFrameLevel()>previous:GetFrameLevel())
        assert(current:GetFrameStrata()=='DIALOG' and previous:GetFrameStrata()=='MEDIUM')
        return current
      end
      s:ShowFindings(); local current=s.overlay
      click(current,'Изменить'); current=above(current)
      click(current,'Карточка / рецепт'); current=above(current)
      click(current,'Снайпить предмет'); current=above(current)
      s:ShowPicker({{item_id=2589},{item_id=5763}},function() end); current=above(current)
      local recipe={item_id=5763,materials={{item_id=2592,quantity=3}}}
      s:ShowMaterialPreview(recipe,2); current=above(current)
      s:ShowTransfer(); current=above(current)
      s:ShowHistory(); current=above(current)
      s:ShowManualBought(r.definition.id); current=above(current)
      Cennik.db.pending={sniper=true,stage='unknown',quantity=1,cost=10}
      s:ShowReconcile(); current=above(current)
      Cennik.db.pending=nil
      Cennik.UI:ShowTaskDetails(t); local details=above(current)
      s:ShowFindings(); above(details)
      Cennik.UI:ShowTaskDetails(t); assert(s.activeWindow==details and details:IsVisible())
      -- The Auctionator shortcut is populated automatically, including while
      -- a dialog is being edited. Neither first show nor reuse may take focus.
      s:ShowEditor(5763); local editor=s.editor
      s:ShowAuctionatorShortcut(5763); local shortcut=s.auctionatorShortcut
      assert(s.activeWindow==editor and shortcut:GetFrameStrata()=='MEDIUM')
      shortcut:Hide(); s:ShowAuctionatorShortcut(2589)
      assert(s.activeWindow==editor and shortcut:GetFrameStrata()=='MEDIUM')
      s.auctionatorSelection={itemID=2589}
      click(shortcut,'В снайпер'); current=above(shortcut)
      assert(current==s.editor and current.itemEntry:GetText()=='2589')
    """)


def test_shortcut_hides_on_cennik_and_native_auction_tab_changes(exchange):
    lua = game(exchange)
    lua.execute("""
      Cennik.UI:Attach()
      Cennik.Sniper:ShowAuctionatorShortcut(5763); assert(Cennik.Sniper.auctionatorShortcut:IsShown())
      Cennik.UI:SelectSection('History'); assert(not Cennik.Sniper.auctionatorShortcut:IsShown())
      Cennik.Sniper:ShowAuctionatorShortcut(5763)
      AuctionHouseFrame:SetDisplayMode('sell'); assert(not Cennik.Sniper.auctionatorShortcut:IsShown())
    """)


def test_repeated_checks_do_not_repaint_rows_details_or_purchase_feedback(exchange):
    lua = game(exchange, quantity=1)
    found(lua)
    lua.execute("""
      local s=Cennik.Sniper; local p=s.overlay
      s:Notice('Проверяем сохранение результата')
      for _,frame in ipairs({p.rows[1],p.content,p.buy,p.notice}) do
        frame.writes=0
        local original=frame.SetText
        frame.SetText=function(self,value) self.writes=(self.writes or 0)+1; original(self,value) end
      end
      for i=1,5 do
        Game.advance(2)
        s:BeginDetail(r,C_AuctionHouse.MakeItemKey(2589)); s:RenderOverlay()
        s:AcceptRows(r,Cennik.Adapter:Results(2589,C_AuctionHouse.MakeItemKey(2589),true))
      end
      for _,frame in ipairs({p.rows[1],p.content,p.buy,p.notice}) do assert((frame.writes or 0)==0) end
      assert(p.notice:GetText():find('Проверяем сохранение результата',1,true))
      assert(p.scroll.rectUpdates==1)
    """)


def test_new_auction_not_best_price_alerts_once_and_never_changes_selected_lot(exchange):
    lua = game(exchange, quantity=1, gear=True)
    lua.execute("PlaySound=function() Game.sounds=(Game.sounds or 0)+1 end")
    found(lua, gear=True)
    lua.execute("""
      local s=Cennik.Sniper; local key=C_AuctionHouse.MakeItemKey(2589)
      assert((Game.sounds or 0)==0 and s.overlay.content:GetText():find('Уже есть',1,true))
      s.overlay.cennikClose.scripts.OnClick()
      Game.items[2589][2]={quantity=1,buyoutAmount=500,containsOwnerItem=false,containsAccountItem=false,auctionID=124,itemKey=key}
      s:AcceptRows(r,Cennik.Adapter:Results(2589,key,false),key)
      assert(Game.sounds==1 and s.overlay:IsShown())
      assert(s.alerts[r.definition.id].auction_id==123)
      assert(s.overlay.finding:GetText():find('500c',1,true))
      s:AcceptRows(r,Cennik.Adapter:Results(2589,key,false),key); assert(Game.sounds==1)
      s.overlay.latest.scripts.OnClick()
      assert(s.alerts[r.definition.id].auction_id==124 and s.overlay.buy:GetText()=='Купить 1 шт. за 500c')
      -- Losing the selected lot must not turn the same button into a different purchase.
      Game.items[2589][2]=nil; s:AcceptRows(r,Cennik.Adapter:Results(2589,key,false),key)
      assert(s.alerts[r.definition.id]==nil and not s.overlay.buy:IsEnabled())
      assert(s.overlay.content:GetText():find('Выбранный лот недоступен',1,true))
      s.overlay.next.scripts.OnClick(); assert(s.alerts[r.definition.id].auction_id==123)
    """)


def test_price_change_needs_explicit_selection_and_empty_baseline_detects_appearance(exchange):
    lua = game(exchange, quantity=1, gear=True)
    lua.execute("""
      PlaySound=function() Game.sounds=(Game.sounds or 0)+1 end
      local s=Cennik.Sniper; local key=C_AuctionHouse.MakeItemKey(2589)
      s:MissingFindings(r)
      s:AcceptRows(r,Cennik.Adapter:Results(2589,key,false),key)
      assert(Game.sounds==1 and s.alerts[r.definition.id].event=='new')
      Game.items[2589][1].buyoutAmount=500
      s:AcceptRows(r,Cennik.Adapter:Results(2589,key,false),key)
      assert(s.alerts[r.definition.id]==nil and not s.overlay.buy:IsEnabled())
      assert(s.overlay.content:GetText():find('400c',1,true))
      s.overlay.latest.scripts.OnClick()
      assert(s.alerts[r.definition.id].cost==500)
      s:AcceptRows(r,Cennik.Adapter:Results(2589,key,false),key); assert(Game.sounds==2)
    """)


def test_commodity_supply_change_is_not_claimed_as_new_auction(exchange):
    lua = game(exchange, quantity=1)
    lua.execute("PlaySound=function() Game.sounds=(Game.sounds or 0)+1 end")
    found(lua)
    lua.execute("""
      local s=Cennik.Sniper; local key=C_AuctionHouse.MakeItemKey(2589)
      Game.commodity[2589][1].quantity=80
      s:AcceptRows(r,Cennik.Adapter:Results(2589,key,true),key); assert(Game.sounds==1)
      Game.commodity[2589][1].quantity=90
      s:AcceptRows(r,Cennik.Adapter:Results(2589,key,true),key)
      assert(Game.sounds==2 and s.overlay.finding:GetText():find('Изменилось предложение товара',1,true))
      s:AcceptRows(r,Cennik.Adapter:Results(2589,key,true),key); assert(Game.sounds==2)
    """)


def test_buy_button_reports_busy_then_success_with_actual_total_and_no_poll_overwrite(exchange):
    lua = game(exchange, quantity=1, gear=True)
    found(lua, gear=True)
    lua.execute("""
      local s=Cennik.Sniper; local p=s.overlay
      Game.throttled=true
      Game.click(function() p.buy.scripts.OnClick() end)
      assert(Cennik.db.pending==nil and p.notice:GetText():find('Покупка не отправлена',1,true))
      Game.throttled=false
      Game.click(function() p.buy.scripts.OnClick() end)
      assert(Cennik.db.pending.stage=='buying' and r.bought==0)
      assert(p.notice:GetText():find('ждём подтверждения',1,true))
      Game.event('AUCTION_HOUSE_PURCHASE_COMPLETED',999); assert(r.bought==0)
      Game.event('AUCTION_HOUSE_PURCHASE_COMPLETED',123)
      assert(Cennik.db.pending==nil and r.bought==1)
      assert(p.notice:GetText():find('Куплено:',1,true) and p.notice:GetText():find('400c',1,true),p.notice:GetText())
      s:RenderOverlay(); assert(p.notice:GetText():find('Куплено:',1,true))
    """)


def test_commodity_buttons_show_quote_total_and_expiry_without_silent_purchase(exchange):
    lua = game(exchange, quantity=1)
    found(lua)
    lua.execute("""
      local s=Cennik.Sniper; local p=s.overlay
      Game.click(function() p.buy.scripts.OnClick() end)
      assert(Cennik.db.pending.stage=='await_quote' and r.bought==0)
      Game.event('COMMODITY_PRICE_UPDATED',20,20)
      assert(p.buy:GetText()=='Подтвердить за 20c' and r.bought==0)
      Game.advance(4); Game.click(function() p.buy.scripts.OnClick() end)
      assert(Cennik.db.pending==nil and r.bought==0)
      assert(p.notice:GetText():find('Котировка старше 3 секунд',1,true))
      for _,call in ipairs(Game.calls) do assert(call.kind~='confirm') end
    """)


def test_variant_baselines_and_batch_missing_do_not_replace_selected_auction(exchange):
    lua = game(exchange, quantity=1, gear=True)
    lua.execute("""
      PlaySound=function() Game.sounds=(Game.sounds or 0)+1 end
      local s=Cennik.Sniper
      local a=C_AuctionHouse.MakeItemKey(2589); local b=Cennik:Clone(a); b.itemLevel=1
      s:PrimeFindings(r,{a,b})
      local function row(key,id) return {{item_id=2589,item_key=key,commodity=false,auction_id=id,quantity=1,unit_price=400,total_price=400,ownership_known=true,owned=false}} end
      s:AcceptRows(r,row(a,123),a); s:AcceptRows(r,row(b,124),b)
      assert((Game.sounds or 0)==0 and s.alerts[r.definition.id].auction_id==123)
      C_AuctionHouse.GetBrowseResults=function() return {{itemKey=b,minPrice=400}} end
      C_AuctionHouse.HasFullBrowseResults=function() return true end
      s.current={kind='batch',sent=true,keys={a,b},mapping={[s:Key(a)]={r},[s:Key(b)]={r}}}
      s:OnSearchEvent('AUCTION_HOUSE_BROWSE_RESULTS_UPDATED')
      assert(s.alerts[r.definition.id]==nil and r.offer_count==1)
      assert(#s.details==1 and s:Key(s.details[1].key)==s:Key(b))
      s:NextFinding(r); assert(s.alerts[r.definition.id].auction_id==124)
      local c=Cennik:Clone(a); c.itemLevel=2
      s:AcceptRows(r,row(c,125),c)
      assert(Game.sounds==1 and s.alerts[r.definition.id].auction_id==124)
    """)


def test_grouped_single_bags_keep_cheapest_25000_not_119700_or_300000(exchange):
    lua = game(exchange, quantity=1, gear=True)
    lua.execute("""
      C_Item.GetItemInfoInstant=function(id) return id,'Container','Bag','',1,1 end
      local key=C_AuctionHouse.MakeItemKey(2589)
      Game.items[2589]={
        {quantity=12,buyoutAmount=25000,containsOwnerItem=false,containsAccountItem=false,auctionID=101,itemKey=key},
        {quantity=1,buyoutAmount=119700,containsOwnerItem=false,containsAccountItem=false,auctionID=102,itemKey=key},
        {quantity=1,buyoutAmount=300000,containsOwnerItem=false,containsAccountItem=false,auctionID=103,itemKey=key}}
    """)
    found(lua, gear=True)
    assert lua.eval("Cennik.Sniper.alerts[r.definition.id].cost") == 25000
    assert lua.eval("Cennik.Sniper.alerts[r.definition.id].quantity") == 1
    lua.execute("Game.click(function() assert(Cennik.Sniper:Buy(r.definition.id)) end)")
    assert lua.eval("Game.calls[#Game.calls].kind") == "bid"
    assert lua.eval("Game.calls[#Game.calls].args[1]") == 101
    assert lua.eval("Game.calls[#Game.calls].args[2]") == 25000
    lua.execute("Game.event('AUCTION_HOUSE_PURCHASE_COMPLETED',101)")
    assert lua.eval("r.bought") == 1


def test_cheaper_bag_arriving_later_is_visible_and_selected_only_explicitly(exchange):
    lua = game(exchange, quantity=1, gear=True)
    lua.execute("C_Item.GetItemInfoInstant=function(id) return id,'Container','Bag','',1,1 end; Game.items[2589][1].quantity=1; Game.items[2589][1].buyoutAmount=119700")
    found(lua, gear=True)
    lua.execute("""
      local s=Cennik.Sniper; local key=C_AuctionHouse.MakeItemKey(2589)
      Game.items[2589][2]={quantity=12,buyoutAmount=25000,containsOwnerItem=false,containsAccountItem=false,auctionID=124,itemKey=key}
      s:AcceptRows(r,Cennik.Adapter:Results(2589,key,false),key)
      assert(s.alerts[r.definition.id].cost==119700)
      assert(s.overlay.content:GetText():find('Есть дешевле: 25000c',1,true))
      s.overlay.best.scripts.OnClick()
      assert(s.alerts[r.definition.id].cost==25000 and s.alerts[r.definition.id].quantity==1)
      assert(s.overlay.buy:GetText()=='Купить 1 шт. за 25000c')
    """)


def test_catalog_stack_size_never_changes_one_item_purchase_and_own_cheap_bag_is_excluded(exchange):
    lua = game(exchange, quantity=1, gear=True)
    lua.execute("C_Item.GetItemInfo=function() return 'Fixture',nil,nil,nil,nil,nil,nil,20 end")
    found(lua, gear=True)
    assert lua.eval("Cennik.Sniper.alerts[r.definition.id].quantity") == 1
    assert lua.eval("Cennik.Sniper.alerts[r.definition.id].unit_price") == 400
    lua.execute("""
      C_Item.GetItemInfoInstant=function(id) return id,'Container','Bag','',1,1 end
      C_Item.GetItemInfo=function() return 'Bag',nil,nil,nil,nil,nil,nil,1 end
      local key=C_AuctionHouse.MakeItemKey(2589)
      Game.items[2589]={
        {quantity=12,buyoutAmount=25000,containsOwnerItem=true,containsAccountItem=false,auctionID=101,itemKey=key},
        {quantity=1,buyoutAmount=119700,containsOwnerItem=false,containsAccountItem=false,auctionID=102,itemKey=key}}
      Cennik.Sniper:AcceptRows(r,Cennik.Adapter:Results(2589,key,false),key)
      Cennik.Sniper:SelectCheapestFinding(r)
      assert(Cennik.Sniper.alerts[r.definition.id].cost==119700)
      assert(r.last_result.cheapest_raw_price==25000 and r.last_result.cheapest_eligible_unit_price==119700)
      assert(r.last_result.owned_rows==1)
    """)


def test_sniper_coin_fields_round_trip_and_reject_invalid_silver(exchange):
    lua = game(exchange)
    lua.execute("""
      Cennik.UI:Attach(); Cennik.UI:Show()
      local s=Cennik.Sniper; s:ShowEditor(r.definition.item_id,r)
      local p=s.editor; p.modeButtons.limit.scripts.OnClick()
      p.priceGold:SetText('5'); p.priceSilver:SetText('48'); p.priceCopper:SetText('98')
      p.modeButtons.median3d.scripts.OnClick(); p.modeButtons.limit.scripts.OnClick()
      assert(p.priceGold:GetText()=='5' and p.priceSilver:GetText()=='48' and p.priceCopper:GetText()=='98')
      local function save(editor)
        for _,f in ipairs(Game.frames) do if f.parent==editor and f.text=='Сохранить цель' then f.scripts.OnClick(); return end end
        error('Save button missing')
      end
      save(p); assert(r.definition.mode=='limit' and r.definition.unit_limit_copper==54898)
      s:ShowEditor(r.definition.item_id,r); p=s.editor
      assert(p.priceGold:GetText()=='5' and p.priceSilver:GetText()=='48' and p.priceCopper:GetText()=='98')
      p.priceSilver:SetText('100'); save(p)
      assert(p:IsShown() and r.definition.unit_limit_copper==54898)
      p.priceSilver:SetText('48'); p.priceGold:SetText('5.48'); save(p)
      assert(p:IsShown() and r.definition.unit_limit_copper==54898)
      p.priceGold:SetText('0'); p.priceSilver:SetText('0'); p.priceCopper:SetText('1'); save(p)
      assert(r.definition.unit_limit_copper==1)
    """)


def test_current_search_price_and_depth_are_not_replaced_by_an_older_auction_cache(exchange):
    lua = game(exchange, quantity=1, gear=True)
    lua.execute("""
      Game.items[2589][1].quantity=12; Game.items[2589][1].buyoutAmount=25000
      C_AuctionHouse.GetAuctionInfoByID=function(id) return {auctionID=id,itemKey=C_AuctionHouse.MakeItemKey(2589),quantity=1,buyoutAmount=119700,containsOwnerItem=false,containsAccountItem=false} end
    """)
    found(lua, gear=True)
    assert lua.eval("Cennik.Sniper.alerts[r.definition.id].cost") == 25000
    assert lua.eval("Cennik.Sniper.alerts[r.definition.id].available") == 12
    assert lua.eval("Game.click(function() return Cennik.Sniper:Buy(r.definition.id) end)") is False
    assert lua.eval("Cennik.db.pending") is None
    assert lua.eval("Game.calls[#Game.calls].kind") == "search"
