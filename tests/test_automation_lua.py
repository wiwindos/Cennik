from PIL import Image, ImageDraw
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
      Cennik.Automation:Tick(1);assert(Cennik.Automation.code==6)
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
      Game.advance(16);Cennik.Automation:Tick(1)
      assert(not Cennik.Automation.quiescing and Cennik.Automation.code==2)
      Game.advance(25);Cennik.Automation:Tick(1)
      assert(not Cennik.Automation.quiescing)
      Cennik.Automation:Stop();Cennik.Automation:Tick(1)
      assert(Cennik.Automation.code==0 and not Cennik.Automation.quiescing)
      CennikImport.automation={enabled=false};Cennik.Automation:Initialize()
      assert(not Cennik.Automation.config and not Cennik.Automation.marker:IsShown())
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
      assert(Cennik.CollectorUI.menuButtons[9]:GetText()=='Автоцикл: статус')
    """)
