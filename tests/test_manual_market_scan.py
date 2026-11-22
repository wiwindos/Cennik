from test_market_collector import collector, exchange, analytics_root


def modern_scanner(lua):
    lua.execute('''
      scanner=Auctionator.State.FullScanFrameRef
      scanner.state={TimeOfLastReplicateScan=Game.now-10}
      scanner.IsAutoscanReady=function() return false end
      scanner.CanInitiate=function(self)
        return (self.state.TimeOfLastReplicateScan~=nil and
          Game.now-self.state.TimeOfLastReplicateScan>900 and not self.inProgress)
          or self.state.TimeOfLastReplicateScan==nil
      end
      scanner.InitiateScan=function(self)
        if self:CanInitiate() then
          self.state.TimeOfLastReplicateScan=Game.now
          self.inProgress=true
          C_AuctionHouse.ReplicateItems()
        end
      end
    ''')


def test_manual_and_auto_scans_both_respect_fifteen_minutes(exchange):
    lua=collector(exchange)
    lua.execute('''
      Cennik.db.collector.next_at=Game.now+700
      Cennik.MarketCollector:Tick(1)
      assert(Game.replicates==0)
      assert(not Cennik.MarketCollector:StartManual())
      assert(Cennik.CollectorStatus.db.current.code=='COOLDOWN')
      Game.advance(701)
      assert(Cennik.MarketCollector:StartManual())
      assert(Game.replicates==1)
      assert(Cennik.db.collector.next_at==Game.now+900)
      assert(not Cennik.MarketCollector:StartManual())
      assert(Game.replicates==1)
      Game.event('REPLICATE_ITEM_LIST_UPDATE')
      assert(#Cennik.db.market_scans==1 and Cennik.db.market_scans[1].full)
      Cennik.MarketCollector:Tick(1);assert(Game.replicates==1)
      assert(not Cennik.MarketCollector:StartManual())
      Game.advance(901)
      assert(Cennik.MarketCollector:StartManual())
      assert(Game.replicates==2)
    ''')


def test_manual_scan_preserves_auctionator_pipeline_and_updates_real_request_time(exchange):
    lua=collector(exchange);modern_scanner(lua)
    lua.execute('''
      previous=scanner.state.TimeOfLastReplicateScan
      Cennik.db.collector.next_at=Game.now+700
      assert(not Cennik.MarketCollector:Available(true))
      assert(scanner.state.TimeOfLastReplicateScan==previous)
      Cennik.MarketCollector:Tick(1);assert(Game.replicates==0)
      Game.advance(901)
      assert(Cennik.MarketCollector:Available(true))
      assert(Cennik.MarketCollector:StartManual())
      assert(Game.replicates==1 and scanner.inProgress)
      assert(scanner.state.TimeOfLastReplicateScan==Game.now)
      assert(not Cennik.MarketCollector:StartManual())
      Game.event('REPLICATE_ITEM_LIST_UPDATE')
      assert(Cennik.db.market_scans[1].full)
    ''')


def test_server_throttle_and_legacy_permission_are_never_bypassed(exchange):
    lua=collector(exchange);modern_scanner(lua)
    lua.execute('''
      previous=scanner.state.TimeOfLastReplicateScan
      C_AuctionHouse.IsThrottledMessageSystemReady=function() return false end
      assert(not Cennik.MarketCollector:StartManual())
      assert(Game.replicates==0 and scanner.state.TimeOfLastReplicateScan==previous)
      assert(Cennik.CollectorStatus.db.current.code=='SCAN_REJECTED')
      C_AuctionHouse.IsThrottledMessageSystemReady=function() return true end
      scanner.IsAutoscanReady=nil
      scanner.CanInitiate=function() return false end
      assert(not Cennik.MarketCollector:StartManual())
      assert(Game.replicates==0 and scanner.state.TimeOfLastReplicateScan==previous)
      assert(Cennik.CollectorStatus.db.current.code=='SCAN_REJECTED')
    ''')


def test_full_queue_manual_scan_does_not_rotate_existing_snapshots(exchange):
    lua=collector(exchange)
    lua.execute('''
      Cennik.db.market_scans={{scan_id='a'},{scan_id='b'},{scan_id='c'}}
      assert(not Cennik.MarketCollector:StartManual())
      assert(Game.replicates==0 and #Cennik.db.market_scans==3)
      assert(Cennik.db.market_scans[1].scan_id=='a')
      assert(Cennik.CollectorStatus.db.current.code=='QUEUE_FULL')
    ''')


def test_manual_scan_restore_timestamp_and_schedule_when_auctionator_does_not_send(exchange):
    lua=collector(exchange);modern_scanner(lua)
    lua.execute('''
      scanner.state.TimeOfLastReplicateScan=Game.now-901
      previous=scanner.state.TimeOfLastReplicateScan
      Cennik.db.collector.next_at=Game.now-1
      scanner.InitiateScan=function() end
      assert(not Cennik.MarketCollector:StartManual())
      assert(Game.replicates==0 and not Cennik.MarketCollector.requested_at)
      assert(scanner.state.TimeOfLastReplicateScan==previous)
      assert(Cennik.db.collector.next_at==Game.now-1)
      assert(Cennik.CollectorStatus.db.current.code=='SCAN_REJECTED')
      scanner.InitiateScan=function() error('fixture failure') end
      assert(not Cennik.MarketCollector:StartManual())
      assert(scanner.state.TimeOfLastReplicateScan==previous)
      assert(Cennik.db.collector.next_at==Game.now-1)
      assert(not Cennik.Adapter.sending and not Cennik.MarketCollector.ownRequest)
      assert(Cennik.CollectorStatus.db.current.code=='SCAN_FAILED')
    ''')


def test_manual_scan_rejection_after_send_has_clear_status_without_retry_loop(exchange):
    lua=collector(exchange)
    lua.execute('''
      assert(Cennik.MarketCollector:StartManual())
      Game.event('AUCTION_HOUSE_THROTTLED_MESSAGE_DROPPED')
      assert(not Cennik.MarketCollector.requested_at)
      assert(Cennik.CollectorStatus.db.current.code=='SCAN_REJECTED')
      Cennik.MarketCollector:Tick(1)
      assert(Game.replicates==1 and #Cennik.db.market_scans==0)
    ''')


def test_manual_scan_respects_other_operations_and_auctionator_in_progress(exchange):
    lua=collector(exchange);modern_scanner(lua)
    lua.execute('''
      scanner.inProgress=true
      assert(not Cennik.MarketCollector:StartManual());assert(Game.replicates==0)
      scanner.inProgress=false;Cennik.Adapter.shopping=true
      assert(not Cennik.MarketCollector:StartManual());assert(Game.replicates==0)
      Cennik.Adapter.shopping=false;AuctionHouseFrame:Hide()
      assert(not Cennik.MarketCollector:StartManual());assert(Game.replicates==0)
    ''')


def test_market_button_uses_manual_scan_even_with_autoscan_disabled(exchange):
    lua=collector(exchange);modern_scanner(lua)
    lua.execute('''
      Cennik.db.collector.next_at=Game.now+700
      Cennik.db.collector.enabled=false
      Cennik.UI:Attach();Cennik.UI:SelectSection('Market')
      local clicked=false;local callback
      for _,frame in ipairs(Game.frames) do
        if rawget(frame,'text')=='Сканировать сейчас' then
          local click=frame:GetScript('OnClick')
          if click then callback=click;click(frame);clicked=true;break end
        end
      end
      assert(clicked and Game.replicates==0)
      assert(Cennik.CollectorStatus.db.current.code=='COOLDOWN')
      Game.advance(901);callback()
      assert(Game.replicates==1)
      Game.event('REPLICATE_ITEM_LIST_UPDATE')
      assert(#Cennik.db.market_scans==1 and Cennik.db.market_scans[1].full)
      assert(not Cennik.db.collector.enabled)
    ''')


def test_manual_scan_requires_room_for_snapshot_bytes(exchange):
    lua=collector(exchange)
    lua.execute('''
      Cennik.db.market_scans={{scan_id='keep',packed_bytes=100000000}}
      assert(not Cennik.MarketCollector:StartManual())
      assert(Game.replicates==0 and #Cennik.db.market_scans==1)
      assert(Cennik.db.market_scans[1].scan_id=='keep')
      assert(Cennik.CollectorStatus.db.current.code=='QUEUE_FULL')
    ''')

# EOF
