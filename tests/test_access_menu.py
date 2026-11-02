"""Menu entry points work outside the AH without bypassing role/collector limits."""
from test_accounting import accounting_lua
from test_production import production_lua, production_setup
from test_workspace import workspace as workspace


def test_minimap_opens_production_and_role_outside_auction(workspace, tmp_path):
    clients, _, _, _ = production_setup(workspace, tmp_path)
    lua = production_lua(workspace, clients[1])
    lua.execute("""
      AuctionHouseFrame:Hide();CennikImport.worker_discovery=true
      local u=Cennik.CollectorUI
      u.access:GetScript('OnClick')()
      assert(u.menu:IsShown())
      assert(u.menuButtons[1]:IsEnabled())
      u.menuButtons[1]:GetScript('OnClick')()
      assert(Cennik.Production.window:IsShown() and not u.menu:IsShown())
      u:ToggleMenu();u.menuButtons[2]:GetScript('OnClick')()
      assert(Cennik.CharacterRole.frame:IsShown())
      u:ToggleMenu();assert(not u.menuButtons[5]:IsEnabled())
      local ok,reason=u:MenuAction({id='auction',auction=true})
      assert(not ok and reason=='Откройте аукцион у NPC' and u.menu:IsShown())
      AuctionHouseFrame:Show();u:RefreshMenu()
      assert(u.menuButtons[5]:IsEnabled())
      u.menuButtons[5]:GetScript('OnClick')()
      assert(Cennik.UI.frame:IsShown() and Cennik.UI.section=='Opportunities')
    """)


def test_menu_keeps_collector_only_and_role_connection_limits(workspace):
    lua = accounting_lua(workspace, collector_only=True)
    lua.execute("""
      GetBuildInfo=function() return '1.60.1','fixture' end
      AuctionHouseFrame:Hide()
      local u=Cennik.CollectorUI;u:ToggleMenu()
      assert(not u.menuButtons[1]:IsShown() and not u.menuButtons[2]:IsShown())
      assert(u.menuButtons[3]:IsShown() and u.menuButtons[4]:IsShown())
      local ok=u:MenuAction({id='production'});assert(not ok)
      u.menuButtons[4]:GetScript('OnClick')()
      assert(u.window:IsShown() and u.report)
      assert(u.edit:GetText():find('Cennik Collector support v1',1,true))
      Cennik.collectorOnly=false;CennikImport.worker_discovery=nil
      u:ToggleMenu();assert(u.menuButtons[2]:IsShown() and not u.menuButtons[2]:IsEnabled())
      u:ToggleMenu();assert(not u.menu:IsShown())
    """)
