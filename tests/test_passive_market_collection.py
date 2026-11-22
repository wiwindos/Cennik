from test_market_collector import exchange, analytics_root
from test_auctionator_scan_cache import cached_collector


def test_autoscan_choice_is_per_character_and_survives_import_refresh(exchange):
    lua=cached_collector(exchange)
    lua.execute('''
      CennikImport.worker_discovery=true
      local guid='Player-one'
      UnitGUID=function() return guid end
      Cennik.MarketCollector:Initialize()
      assert(not Cennik.db.collector.enabled)
      Cennik.MarketCollector:SetAutoScan(true)
      assert(Cennik.db.collector.enabled)
      Cennik.MarketCollector:Initialize()
      assert(Cennik.db.collector.enabled)
      guid='Player-two';Cennik.MarketCollector:Initialize()
      assert(not Cennik.db.collector.enabled)
      guid='Player-one';Cennik.MarketCollector:Initialize()
      assert(Cennik.db.collector.enabled)
      Cennik.MarketCollector:SetAutoScan(false)
      Cennik.MarketCollector:Initialize()
      assert(not Cennik.db.collector.enabled)
    ''')


def test_disabled_autoscan_passively_captures_auctionator_without_own_request(exchange):
    lua=cached_collector(exchange)
    lua.execute('''
      Cennik.MarketCollector:SetAutoScan(false)
      assert(Cennik.CollectorStatus:IsEnabled())
      Cennik.MarketCollector:Tick(1)
      assert(Game.replicates==0)
      scanner:InitiateScan()
      rows={Game.row()};Game.received(rows);Game.completed(rows)
      assert(#Cennik.db.market_scans==1 and Cennik.db.market_scans[1].full)
      assert(Game.apiReads==0 and Game.replicates==1)
      Game.advance(901);Cennik.MarketCollector:Tick(1)
      assert(Game.replicates==1)
    ''')
