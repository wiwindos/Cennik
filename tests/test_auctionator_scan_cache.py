from test_market_collector import collector, exchange, analytics_root
from test_accounting import lua_value
from test_workspace import workspace
from cennik.addon.lua_data import lua_literal


def cached_collector(exchange):
    lua=collector(exchange)
    lua.execute('''
      local events=Auctionator.FullScan.Events
      events.ScanProgress='fullprogress'
      Auctionator.EventBus={listeners={},Register=function(self,listener,names)
        for _,name in ipairs(names) do
          self.listeners[name]=self.listeners[name] or {}
          table.insert(self.listeners[name],listener)
        end
      end,Fire=function(self,source,event,...)
        for _,listener in ipairs(self.listeners[event] or {}) do listener:ReceiveEvent(event,...) end
      end}
      local bus=Auctionator.EventBus
      bus:Register(Cennik.Adapter,{events.ScanStart,events.ScanComplete,events.ScanFailed})
      scanner=Auctionator.State.FullScanFrameRef
      scanner.CacheScanData=function() end;scanner.ProcessBatch=function() end;scanner.EndProcessing=function() end
      scanner.InitiateScan=function(self)
        if not self:CanInitiate() then return end
        bus:Fire(self,events.ScanStart)
        self.inProgress=true;C_AuctionHouse.ReplicateItems()
        bus:Fire(self,events.ScanProgress,.1)
      end
      Cennik.MarketCollector.listening=nil;Cennik.MarketCollector:Initialize()
      function Game.row(item,quantity,buyout,owner,link)
        return {replicateInfo={[1]='Fixture',[3]=quantity or 4,[10]=buyout or 40,[11]=0,
          [14]=owner,[17]=item or 2589,[18]=true},itemLink=link or 'item:2589:0:0:0',timeLeft=4}
      end
      Game.apiReads=0;Game.reported=1
      C_AuctionHouse.GetNumReplicateItems=function() return Game.reported end
      C_AuctionHouse.GetReplicateItemInfo=function() Game.apiReads=Game.apiReads+1;error('duplicate info read') end
      C_AuctionHouse.GetReplicateItemLink=function() Game.apiReads=Game.apiReads+1;error('duplicate link read') end
      C_AuctionHouse.GetReplicateItemTimeLeft=function() Game.apiReads=Game.apiReads+1;error('duplicate time read') end
      function Game.received(rows,reported)
        Game.reported=reported or #rows;scanner.scanData=rows
        bus:Fire(scanner,events.ScanProgress,.2)
        Game.event('REPLICATE_ITEM_LIST_UPDATE')
      end
      function Game.completed(rows)
        -- Auctionator resets its own scanData before its asynchronous database
        -- callback emits ScanComplete with the retained, filtered row objects.
        scanner.inProgress=false;scanner.scanData={}
        bus:Fire(scanner,events.ScanProgress,.99)
        bus:Fire(scanner,events.ScanComplete,rows)
      end
    ''')
    return lua


def test_one_request_uses_cached_rows_without_reading_replicate_api_again(exchange):
    lua=cached_collector(exchange)
    lua.execute('''
      assert(Cennik.MarketCollector:StartManual());assert(Game.replicates==1)
      rows={Game.row()};Game.received(rows)
      assert(not Cennik.MarketCollector.packing and #Cennik.db.market_scans==0)
      assert(Cennik.CollectorStatus.db.current.code=='AUCTIONATOR_PROCESSING')
      Game.completed(rows)
      assert(Game.apiReads==0 and Game.replicates==1)
      assert(#Cennik.db.market_scans==1 and Cennik.db.market_scans[1].full)
      assert(Cennik.db.market_scans[1].data=='2589:4:40:0:4:1:0')
      Game.completed(rows);Game.event('REPLICATE_ITEM_LIST_UPDATE')
      assert(#Cennik.db.market_scans==1 and Game.apiReads==0)
      assert(not Cennik.MarketCollector:StartManual())
    ''')


def test_auctionator_button_is_captured_without_cennik_sending_another_query(exchange):
    lua=cached_collector(exchange)
    lua.execute('''
      scanner:InitiateScan()
      assert(Cennik.CollectorStatus.db.current.code=='SHARED_SCAN')
      rows={Game.row()};Game.received(rows);Game.completed(rows)
      assert(Game.replicates==1 and #Cennik.db.market_scans==1)
      assert(Cennik.db.market_scans[1].full)
    ''')


def test_filtered_auctionator_rows_are_partial_with_original_count(exchange):
    lua=cached_collector(exchange)
    lua.execute('''
      assert(Cennik.MarketCollector:StartManual())
      original={Game.row(),nil,Game.row()};Game.received(original,3)
      Game.completed({original[1],original[3]})
      s=Cennik.db.market_scans[1]
      assert(not s.full and s.count==2 and s.reported_count==3 and s.skipped==1)
      assert(s.error and Cennik.CollectorStatus.db.current.code=='SCAN_PARTIAL')
      assert(Game.apiReads==0)
    ''')


def test_empty_filtered_result_cannot_publish_an_empty_market(exchange):
    lua=cached_collector(exchange)
    lua.execute('''
      assert(Cennik.MarketCollector:StartManual())
      Game.received({},3);Game.completed({})
      s=Cennik.db.market_scans[1]
      assert(s.count==0 and s.reported_count==3 and not s.full)
    ''')


def test_late_callback_from_old_scan_does_not_finish_new_scan(exchange):
    lua=cached_collector(exchange)
    lua.execute('''
      assert(Cennik.MarketCollector:StartManual())
      old={Game.row()};Game.received(old)
      Auctionator.EventBus:Fire(scanner,Auctionator.FullScan.Events.ScanFailed)
      scanner.inProgress=false;Game.advance(901)
      assert(Cennik.MarketCollector:StartManual())
      current={Game.row()};Game.received(current)
      Auctionator.EventBus:Fire(scanner,Auctionator.FullScan.Events.ScanComplete,old)
      assert(#Cennik.db.market_scans==0 and Cennik.MarketCollector.requested_at)
      Game.completed(current)
      assert(#Cennik.db.market_scans==1 and Cennik.db.market_scans[1].full)
    ''')


def test_full_result_must_match_each_original_row_not_only_its_length(exchange):
    lua=cached_collector(exchange)
    lua.execute('''
      assert(Cennik.MarketCollector:StartManual())
      original={Game.row(),Game.row()};Game.received(original)
      Game.completed({original[1],Game.row()})
      s=Cennik.db.market_scans[1]
      assert(not s.full and s.reported_count==2 and s.count==1 and s.error)
    ''')


def test_processing_has_separate_timeout_after_server_response(exchange):
    lua=cached_collector(exchange)
    lua.execute('''
      assert(Cennik.MarketCollector:StartManual())
      Game.advance(590);rows={Game.row()};Game.received(rows)
      Game.advance(20);Cennik.MarketCollector:Tick(1)
      assert(Cennik.MarketCollector.requested_at)
      assert(Cennik.CollectorStatus.db.current.code=='AUCTIONATOR_PROCESSING')
      Game.advance(581);Cennik.MarketCollector:Tick(1)
      assert(not Cennik.MarketCollector.requested_at)
      assert(Cennik.CollectorStatus.db.current.code=='SCAN_TIMEOUT')
      assert(Cennik.CollectorStatus.db.current.detail:find('Auctionator',1,true))
      Game.completed(rows);assert(#Cennik.db.market_scans==0)
    ''')


def test_close_or_changed_npc_prevents_cached_result_acceptance(exchange):
    lua=cached_collector(exchange)
    lua.execute('''
      assert(Cennik.MarketCollector:StartManual());rows={Game.row()};Game.received(rows)
      Game.event('AUCTION_HOUSE_CLOSED');Game.completed(rows)
      assert(#Cennik.db.market_scans==0)
      assert(Cennik.CollectorStatus.db.current.code=='SCAN_INTERRUPTED')
    ''')
    lua=cached_collector(exchange)
    lua.execute('''
      assert(Cennik.MarketCollector:StartManual());rows={Game.row()};Game.received(rows)
      UnitGUID=function() return 'Creature-0-1-1-1-999-0001' end
      Game.completed(rows)
      assert(#Cennik.db.market_scans==0)
      assert(Cennik.CollectorStatus.db.current.code=='AUCTION_CONTEXT_UNKNOWN')
    ''')


def test_large_cached_scan_preserves_duplicates_and_variant_and_ownership(exchange):
    lua=cached_collector(exchange)
    lua.execute('''
      Cennik.AuctionContext.registry.entries[1].auction_type='neutral'
      assert(Cennik.MarketCollector:StartManual())
      rows={};local row=Game.row(2589,4,40,'FixturePlayer','item:2589:0:0:0:0:0:42')
      for i=1,200000 do rows[i]=row end
      Game.received(rows);Game.completed(rows)
      assert(Cennik.MarketCollector.packing)
      Game.advance(0)
      s=Cennik.db.market_scans[1]
      assert(s.full and s.count==200000 and s.reported_count==200000)
      assert(s.market==t.market:gsub('%.alliance%.','.neutral.'):gsub('%.horde%.','.neutral.'))
      assert(s.variants[1].link=='item:2589:0:0:0:0:0:42')
      assert(s.data:sub(-1)=='1')
      assert(rows[1]==row and #rows==200000 and Game.apiReads==0)
      assert(not Cennik.MarketCollector.cacheRows and not Cennik.MarketCollector.cacheOwner)
    ''')


def test_cached_result_keeps_the_existing_server_import_format(exchange,workspace):
    lua=cached_collector(exchange)
    lua.execute('Cennik.db.current_market='+lua_literal(workspace.market_key)+';Cennik.db.collector.market=Cennik.db.current_market;Cennik.db.binding.market=Cennik.db.current_market')
    lua.execute('''
      assert(Cennik.MarketCollector:StartManual());rows={Game.row()}
      Game.received(rows);Game.completed(rows)
    ''')
    saved=lua_value(lua.eval('Cennik.db.market_scans[1]'))
    assert workspace.market.import_scan(saved)['complete']
    before=workspace.market.rows('select * from working_offers')
    lua=cached_collector(exchange)
    lua.execute('Cennik.db.current_market='+lua_literal(workspace.market_key)+';Cennik.db.collector.market=Cennik.db.current_market;Cennik.db.binding.market=Cennik.db.current_market')
    lua.execute('''
      assert(Cennik.MarketCollector:StartManual());rows={Game.row()}
      Game.received(rows,2);Game.completed(rows)
    ''')
    partial=lua_value(lua.eval('Cennik.db.market_scans[1]'))
    assert not workspace.market.import_scan(partial)['complete']
    assert workspace.market.rows('select * from working_offers')==before


def test_throttle_of_another_query_after_response_does_not_cancel_cached_scan(exchange):
    lua=cached_collector(exchange)
    lua.execute('''
      assert(Cennik.MarketCollector:StartManual());rows={Game.row()};Game.received(rows)
      Game.event('AUCTION_HOUSE_THROTTLED_MESSAGE_DROPPED')
      assert(Cennik.MarketCollector.requested_at and Cennik.MarketCollector.response_received_at)
      Game.completed(rows)
      assert(Cennik.db.market_scans[1].full)
    ''')


def test_another_query_during_packing_cannot_replace_the_cached_snapshot(exchange):
    lua=cached_collector(exchange)
    lua.execute('''
      assert(Cennik.MarketCollector:StartManual())
      rows={};for i=1,2501 do rows[i]=Game.row() end
      Game.received(rows);Game.completed(rows)
      assert(Cennik.MarketCollector.packing)
      C_AuctionHouse.ReplicateItems()
      Game.advance(0)
      assert(Cennik.db.market_scans[1].full and Cennik.db.market_scans[1].count==2501)
      assert(Game.apiReads==0)
    ''')


def test_missing_cached_link_does_not_fall_back_to_reading_the_api(exchange):
    lua=cached_collector(exchange)
    lua.execute('''
      assert(Cennik.MarketCollector:StartManual());rows={Game.row()};rows[1].itemLink=nil
      Game.received(rows);Game.completed(rows)
      assert(Cennik.db.market_scans[1].full and Game.apiReads==0)
    ''')

# EOF
