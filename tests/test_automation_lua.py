from PIL import Image, ImageDraw
from test_accounting import accounting_lua, lua_value
from test_production import production_lua, production_setup
from test_workspace import workspace as workspace

from cennik.addon.lua_data import lua_literal
from cennik.worker.automation import decode_signal
from cennik.workspace.models import MARKET


def automatic(workspace, tmp_path):
    clients, _, _, _ = production_setup(workspace, tmp_path)
    lua = production_lua(workspace, clients[0])
    config = {
        "enabled": True,
        "client_id": clients[0].id,
        "token": "123456abcdef",
        "flush_seconds": 900,
    }
    lua.execute(
        "CennikImport.automation="
        + lua_literal(config)
        + ";Cennik.db.current_market="
        + lua_literal(MARKET)
    )
    lua.execute("""
      local originalGUID=UnitGUID
      UnitGUID=function(unit) return originalGUID(unit=='target' and 'npc' or unit) end
      Cennik.db.collector.enabled=true
      local def={id='one',market=Cennik.db.current_market,item_id=2589,quantity=5,mode='limit',
        unit_limit_copper=10,discount_bps=0,priority='normal',use_bags=false}
      assert(Cennik.Sniper:Add(def,true));Cennik.db.sniper.rules.one.enabled=true
      def=Cennik:Clone(def);def.id='paused';def.item_id=4238
      assert(Cennik.Sniper:Add(def,true))
      Cennik.Automation:Initialize();Game.advance(4)
    """)
    return lua


def signal(lua):
    image = Image.new("RGB", (320, 100), "black")
    draw = ImageDraw.Draw(image)
    for i in range(1, 8):
        color = lua.eval(f"Cennik.Automation.cells[{i}].color")
        draw.rectangle(
            (12 + (i - 1) * 10, 12, 21 + (i - 1) * 10, 21),
            fill=tuple(round(color[j] * 255) for j in (1, 2, 3)),
        )
    return decode_signal(image, "123456abcdef")[0]


def test_full_scan_reload_closed_npc_and_selective_sniper_restore(workspace, tmp_path):
    lua = automatic(workspace, tmp_path)
    lua.execute("""
      Cennik.db.collector.last_scan={full=true,scan_id='new-full'}
      Cennik.MarketCollector.packing={};Cennik.Automation:Tick(1)
      assert(Cennik.Automation.code==1 and not Cennik.Automation.quiescing)
      Cennik.MarketCollector.packing=nil;Cennik.Automation:Tick(1)
      assert(Cennik.Automation.code==3 and Cennik.Automation.quiescing,Cennik.Automation:Summary())
      assert(not Cennik.Sniper:CanSend())
      assert(not Cennik.MarketCollector:Available())
    """)
    first = signal(lua)
    assert first.code == 3
    lua.execute("""
      Game.event('PLAYER_LOGOUT')
      assert(Cennik.Automation.db.resume_pending)
      AuctionHouseFrame:Hide();Cennik.Automation:Initialize()
      Cennik.db.sniper.rules.one.enabled=false
      Game.advance(4);Cennik.Automation:Tick(1)
      assert(Cennik.Automation.code==4)
    """)
    second = signal(lua)
    assert second.code == 4 and second.boot != first.boot
    lua.execute("""
      local original=UnitGUID
      UnitGUID=function(unit) if unit=='target' then return 'Creature-0-1-1-1-999-0001' end;return original(unit) end
      Cennik.Automation:Tick(1);assert(Cennik.Automation.code==4)
      UnitGUID=original
      AuctionHouseFrame:Show();Game.event('AUCTION_HOUSE_SHOW');Cennik.Automation:Tick(1)
      assert(Cennik.Automation.code==2 and not Cennik.Automation.db.resume_pending)
      assert(Cennik.db.sniper.rules.one.enabled and not Cennik.db.sniper.rules.paused.enabled)
      assert(Cennik.UI.section=='Sniper' and Cennik.db.collector.enabled)
      assert(not Cennik.Sniper.manualHold)
    """)


def test_pending_purchase_local_stop_and_disabled_mode_are_respected(workspace, tmp_path):
    lua = automatic(workspace, tmp_path)
    lua.execute("""
      Cennik.db.pending={stage='unknown'}
      Cennik.Automation:Tick(1);assert(Cennik.Automation.code==6)
      assert(not Cennik.Automation.quiescing)
      Cennik.db.pending=nil
      Cennik.db.collector.last_scan={full=true,scan_id='new-full'}
      Cennik.Automation:Tick(1);assert(Cennik.Automation.quiescing)
      Game.advance(121);Cennik.Automation:Tick(1)
      assert(not Cennik.Automation.quiescing and Cennik.Automation.code==2)
      Game.advance(25);Cennik.Automation:Tick(1)
      assert(Cennik.Automation.quiescing and Cennik.Automation.code==3)
      Cennik.Automation:Stop();Cennik.Automation:Tick(1)
      assert(Cennik.Automation.code==0 and not Cennik.Automation.quiescing)
      CennikImport.automation={enabled=false};Cennik.Automation:Initialize()
      assert(not Cennik.Automation.config and not Cennik.Automation.marker:IsShown())
    """)


def test_no_selected_target_is_needed_to_save_or_reopen(workspace, tmp_path):
    lua = automatic(workspace, tmp_path)
    lua.execute("""
      local original=UnitGUID
      UnitGUID=function(unit) if unit=='target' then return nil end;return original(unit) end
      Cennik.db.collector.last_scan={full=true,scan_id='no-target'}
      Cennik.Automation:Tick(1)
      assert(Cennik.Automation.code==3 and Cennik.Automation.quiescing)
      Game.event('PLAYER_LOGOUT')
      AuctionHouseFrame:Hide();Cennik.Automation:Initialize();Game.advance(4)
      Cennik.Automation:Tick(1)
      assert(Cennik.Automation.code==4 and Cennik.Automation.db.resume_pending)
      AuctionHouseFrame:Show();Game.event('AUCTION_HOUSE_SHOW');Cennik.Automation:Tick(1)
      assert(Cennik.Automation.code==2 and not Cennik.Automation.db.resume_pending)
    """)


def test_ready_waits_for_slow_worker_and_retries_same_unsaved_snapshot(workspace, tmp_path):
    lua = automatic(workspace, tmp_path)
    lua.execute("""
      Cennik.db.collector.last_scan={full=true,scan_id='retry-same'}
      Cennik.Automation:Tick(1)
      Game.advance(45);Cennik.Automation:Tick(1)
      assert(Cennik.Automation.code==3 and Cennik.Automation.quiescing)
      Game.advance(76);Cennik.Automation:Tick(1)
      assert(not Cennik.Automation.quiescing)
      Game.advance(21);Cennik.Automation:Tick(1)
      assert(Cennik.Automation.code==3 and Cennik.Automation.quiescing)
      assert(Cennik.db.collector.last_scan.scan_id=='retry-same')
    """)


def test_first_open_uses_interact_binding_but_unknown_auction_stays_blocked(workspace, tmp_path):
    lua = automatic(workspace, tmp_path)
    lua.execute("""
      AuctionHouseFrame:Hide()
      Cennik.Automation.db.npc_id=nil;Cennik.Automation.db.resume_pending=false
      Cennik.Automation:Tick(1);assert(Cennik.Automation.code==4)
      AuctionHouseFrame:Show();Cennik.AuctionContext.Check=function() return false,'Unknown NPC' end
      Cennik.Automation:Tick(1);assert(Cennik.Automation.code==6)
      assert(not Cennik.Automation.quiescing)
    """)


def test_different_npc_cannot_restore_saved_sniper_after_reload(workspace, tmp_path):
    lua = automatic(workspace, tmp_path)
    lua.execute("""
      Cennik.Automation.db.resume_pending=true;Cennik.Automation.db.npc_id=8719
      Cennik.db.sniper.rules.one.enabled=false
      Cennik.AuctionContext.Check=function() return true end
      Cennik.AuctionContext.Snapshot=function() return {npc_id=999} end
      assert(not Cennik.Automation:Restore())
      assert(Cennik.Automation.db.resume_pending and not Cennik.db.sniper.rules.one.enabled)
    """)


def test_pausing_sniper_during_ready_does_not_restore_old_targets(workspace, tmp_path):
    lua = automatic(workspace, tmp_path)
    lua.execute("""
      Cennik.db.collector.last_scan={full=true,scan_id='pause-test'}
      Cennik.Automation:Tick(1);assert(Cennik.Automation.quiescing)
      Cennik.Sniper:Pause();Game.event('PLAYER_LOGOUT')
      assert(next(Cennik.Automation.db.resume_rules)==nil)
      Cennik.Automation:Stop()
      Cennik.CollectorUI:ToggleMenu()
      assert(Cennik.CollectorUI.menuButtons[10]:GetText()=='Автоцикл: статус')
    """)


def install_full_scan(lua):
    lua.execute("""
      GetBuildInfo=function() return '1.60.1','fixture' end
      Game.replicates=0
      C_AuctionHouse.ReplicateItems=function() Game.replicates=Game.replicates+1 end
      C_AuctionHouse.GetNumReplicateItems=function() return 1 end
      C_AuctionHouse.GetReplicateItemInfo=function()
        return 'Fixture',nil,4,nil,nil,nil,nil,nil,nil,40,0,nil,nil,nil,nil,nil,2589,true
      end
      C_AuctionHouse.GetReplicateItemLink=function() return 'item:2589:0:0:0' end
      C_AuctionHouse.GetReplicateItemTimeLeft=function() return 4 end
      C_AuctionHouse.GetItemKeyInfo=function() return {isCommodity=true} end
      local lib=LibStub('LibAHTab-1-0',true)
      lib:CreateTab('FixtureFullScan',CreateFrame('Frame'))
      Auctionator.State={FullScanFrameRef={CanInitiate=function() return true end,
        InitiateScan=function() lib:SetSelected('FixtureFullScan');C_AuctionHouse.ReplicateItems() end}}
      Cennik.UI:Attach();Cennik.UI:Show()
      Cennik.Adapter:Initialize();Cennik.MarketCollector:Initialize()
      Cennik.Sniper.hooked=false;Cennik.Sniper:Initialize()
      Cennik.Sniper.manualHold=nil;Cennik.Sniper.foreignTab=false
    """)


def test_owned_scan_automatically_yields_sniper_and_does_not_become_manual_conflict(workspace, tmp_path):
    lua=automatic(workspace,tmp_path)
    install_full_scan(lua)
    lua.execute("""
      Cennik.db.collector.enabled=false
      CennikImport.automation.token='abcdef123456'
      Cennik.Automation:Initialize();Game.advance(4)
      assert(Cennik.db.collector.enabled,'Auto mode must enable collection')
      Cennik.Sniper:Tick(1)
      assert(not Cennik.Sniper.current,'New sniper searches must yield to a due full scan')
      Cennik.MarketCollector:Tick(1)
      assert(Game.replicates==1)
      assert(not Cennik.Sniper.manualHold and not Cennik.Sniper.foreignTab)
      Game.event('REPLICATE_ITEM_LIST_UPDATE')
      assert(Cennik.db.collector.last_scan.full)
      assert(#Cennik.db.market_scans==1)
      Cennik.Sniper:Tick(1)
      assert(not Cennik.Sniper.current,'Do not start another search before saving')
      Cennik.Automation:Tick(1)
      assert(Cennik.Automation.quiescing and Cennik.Automation.code==3)
    """)


def test_restore_in_new_lua_session_reopens_overlay_and_resumes_queries(workspace,tmp_path):
    first=automatic(workspace,tmp_path)
    install_full_scan(first)
    first.execute("""
      Cennik.UI:Show();Cennik.UI:SelectSection('Sniper')
      Cennik.Sniper.selected='one';Cennik.Sniper.overlayRequested=true;Cennik.Sniper:RenderOverlay()
      Cennik.db.collector.last_scan={full=true,scan_id='saved-full'}
      Cennik.Automation:Tick(1);Game.event('PLAYER_LOGOUT')
    """)
    saved=lua_value(first.globals().CennikAddonDB)
    incoming=lua_value(first.globals().CennikImport)
    incoming['auctioneer_registry']=lua_value(first.eval('Cennik.AuctionContext.registry'))
    incoming['acknowledged_scan_ids']=['saved-full']
    second=accounting_lua(workspace)
    second.execute('CennikAddonDB='+lua_literal(saved)+';CennikImport='+lua_literal(incoming))
    second.execute("""
      local original=UnitGUID
      UnitGUID=function(unit) return original(unit=='target' and 'npc' or unit) end
      AuctionHouseFrame:Hide();Cennik:Initialize();Game.advance(4)
      Cennik.Automation:Tick(1);assert(Cennik.Automation.code==4)
      AuctionHouseFrame:Show();Game.event('AUCTION_HOUSE_SHOW')
      Cennik.Sniper.manualHold='Initial native tab';Cennik.Sniper.manualHoldSource='tab';Cennik.Sniper.foreignTab=true
      Cennik.Automation:Tick(1)
      assert(not Cennik.Automation.db.resume_pending,Cennik.Automation:Summary())
      assert(Cennik.UI.section=='Sniper' and Cennik.Sniper.overlay:IsShown())
      assert(Cennik.Sniper.selected=='one')
      assert(Cennik.db.sniper.rules.one.enabled and not Cennik.db.sniper.rules.paused.enabled)
      assert(not Cennik.Sniper.manualHold and not Cennik.Sniper.foreignTab)
      Cennik.db.collector.next_at=Cennik:Now()+900
      Game.advance(2);Cennik.Sniper:Tick(1)
      assert(Cennik.Sniper.current,'Sniper must actually query after restoration')
    """)


def test_manual_operations_and_full_queue_pause_automatic_work(workspace,tmp_path):
    lua=automatic(workspace,tmp_path)
    install_full_scan(lua)
    lua.execute("""
      Cennik.Sniper:HoldForManualSearch('Manual Auctionator search')
      Cennik.Automation:Tick(1);Cennik.MarketCollector:Tick(1)
      assert(Game.replicates==0 and Cennik.Automation.code==1)
      assert(not Cennik.Automation.quiescing)
      assert(Cennik.Sniper:ResumeSearch())
      Cennik.Bulk.active=true;Cennik.MarketCollector:Tick(1)
      assert(Game.replicates==0 and Cennik.Bulk.active)
      Cennik.Bulk.active=false
      GetCurrentKeyBoardFocus=function() return true end
      Cennik.MarketCollector:Tick(1);assert(Game.replicates==0)
      GetCurrentKeyBoardFocus=function() return nil end
      InCombatLockdown=function() return true end
      Cennik.MarketCollector:Tick(1);assert(Game.replicates==0)
      InCombatLockdown=function() return false end
      Cennik.db.market_scans={{scan_id='a'},{scan_id='b'},{scan_id='c'}}
      Cennik.MarketCollector:Tick(1)
      assert(Game.replicates==0 and #Cennik.db.market_scans==3)
      assert(not Cennik.Automation:ScanCapacity())
      assert((Cennik.db.collector.rotated or 0)==0)
      Cennik.db.market_scans={{scan_id='a',packed_bytes=45000000},{scan_id='b',packed_bytes=45000000}}
      assert(not Cennik.Automation:ScanCapacity(),'Byte budget must preserve pending scans too')
    """)


def test_updated_sniper_rule_and_user_hidden_overlay_are_not_reactivated(workspace,tmp_path):
    lua=automatic(workspace,tmp_path)
    lua.execute("""
      Cennik.Sniper.overlayHidden=true;Cennik.Automation:Capture()
      Cennik.Automation.db.npc_id=12345;Cennik.Automation.db.resume_pending=true
      Cennik.db.sniper.rules.one.enabled=false
      Cennik.db.sniper.rules.one.definition.unit_limit_copper=11
      assert(Cennik.Automation:Restore())
      assert(not Cennik.db.sniper.rules.one.enabled)
      assert(Cennik.Sniper.overlayHidden)
    """)


def test_first_native_auction_tab_is_claimed_by_enabled_auto_mode(workspace,tmp_path):
    lua=automatic(workspace,tmp_path)
    install_full_scan(lua)
    lua.execute("""
      LibStub('LibAHTab-1-0',true):SetSelected('FixtureFullScan')
      assert(Cennik.Sniper.manualHoldSource=='tab' and Cennik.Sniper.foreignTab)
      Cennik.Automation:Tick(1)
      assert(not Cennik.Automation.initial_open)
      assert(not Cennik.Sniper.manualHold and not Cennik.Sniper.foreignTab)
      assert(Cennik.UI.section=='Sniper' and Cennik.db.sniper.rules.one.enabled)
      assert(not Cennik.db.sniper.rules.paused.enabled)
      Game.advance(3);Cennik.MarketCollector:Tick(1)
      assert(Game.replicates==1,'Opening NPC must start the automatic scan without another Resume click')
      Cennik.Automation:Tick(1)
      assert(Cennik.Automation.label:GetText():find('Получаем полный скан',1,true))
    """)


def test_initial_open_never_releases_real_manual_query_or_later_tab_switch(workspace,tmp_path):
    lua=automatic(workspace,tmp_path)
    install_full_scan(lua)
    lua.execute("""
      Cennik.Sniper:HoldForManualSearch('Real manual query')
      LibStub('LibAHTab-1-0',true):SetSelected('FixtureFullScan')
      assert(Cennik.Sniper.manualHoldSource=='manual')
      Cennik.Automation:Tick(1)
      assert(Cennik.Automation.initial_open and Cennik.Sniper.manualHold)
      assert(Cennik.Automation.label:GetText():find('Ручной поиск AH',1,true))
      Cennik.Sniper:ResumeSearch();Cennik.Automation:Tick(1)
      assert(not Cennik.Automation.initial_open)
      LibStub('LibAHTab-1-0',true):SetSelected('FixtureFullScan')
      Cennik.Automation:Tick(1)
      assert(Cennik.Sniper.manualHoldSource=='tab' and Cennik.Automation.code==1)
    """)


def test_collect_only_restore_clears_default_tab_hold_without_starting_paused_rules(workspace,tmp_path):
    lua=automatic(workspace,tmp_path)
    lua.execute("""
      Cennik.Sniper:Pause();Cennik.Automation:Capture()
      Cennik.Automation.db.npc_id=12345;Cennik.Automation.db.resume_pending=true
      Cennik.Sniper:HoldForManualSearch('Default opening tab','tab');Cennik.Sniper.foreignTab=true
      Cennik.Automation:Tick(1)
      assert(not Cennik.Automation.db.resume_pending)
      assert(not Cennik.Sniper.manualHold and not Cennik.Sniper.foreignTab)
      assert(not Cennik.db.sniper.rules.one.enabled)
      assert(Cennik.db.collector.enabled)
    """)


def test_shared_full_scan_resumes_but_does_not_override_real_manual_results(workspace,tmp_path):
    lua=automatic(workspace,tmp_path)
    install_full_scan(lua)
    lua.execute("""
      Cennik.Automation:Tick(1)
      assert(not Cennik.Automation.initial_open)
      C_AuctionHouse.ReplicateItems()
      assert(Cennik.Sniper.manualHoldSource=='scan')
      Game.event('REPLICATE_ITEM_LIST_UPDATE')
      Game.advance(3);Cennik.Automation:Tick(1)
      assert(not Cennik.Sniper.manualHold)
      assert(Cennik.Automation.quiescing and Cennik.Automation.code==3)
      Cennik.Automation.quiescing=false
      Cennik.Sniper:HoldForManualSearch('User results')
      C_AuctionHouse.ReplicateItems()
      assert(Cennik.Sniper.manualHoldSource=='manual')
    """)
