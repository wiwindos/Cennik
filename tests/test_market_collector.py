from test_addon import exchange as exchange
from test_addon import exported
from test_addon_lua import runtime
from test_cennik import analytics_root as analytics_root


def collector(exchange):
    lua = runtime(exported(exchange))
    lua.execute("""
      Game.replicates=0
      function GetBuildInfo() return '1.60.1','fixture' end
      C_AuctionHouse.ReplicateItems=function() Game.replicates=Game.replicates+1 end
      C_AuctionHouse.GetNumReplicateItems=function() return 1 end
      C_AuctionHouse.GetReplicateItemInfo=function()
        return 'Fixture',nil,4,nil,nil,nil,nil,nil,nil,40,0,nil,nil,nil,nil,nil,2589,true
      end
      C_AuctionHouse.GetReplicateItemLink=function() return 'item:2589:0:0:0' end
      C_AuctionHouse.GetReplicateItemTimeLeft=function() return 4 end
      C_AuctionHouse.GetItemKeyInfo=function() return {isCommodity=true} end
      Auctionator.State={FullScanFrameRef={
        CanInitiate=function() return true end,
        InitiateScan=function() C_AuctionHouse.ReplicateItems() end}}
      Cennik.db.collector={enabled=true,market=t.market,next_at=0}
      Cennik.MarketCollector:Initialize()
      Game.advance(3)
    """)
    return lua


def test_one_shared_request_cooldown_and_capture(exchange):
    lua = collector(exchange)
    lua.execute("Cennik.MarketCollector:Tick(1)")
    assert lua.eval("Game.replicates") == 1
    lua.execute("Game.event('REPLICATE_ITEM_LIST_UPDATE')")
    assert lua.eval("#Cennik.db.market_scans") == 1
    assert lua.eval("Cennik.db.market_scans[1].full")
    assert lua.eval("Cennik.db.market_scans[1].data") == "2589:4:40:0:4:1:0"
    lua.execute("Cennik.MarketCollector:Tick(1)")
    assert lua.eval("Game.replicates") == 1


def test_unsolicited_and_closed_auction_updates_do_not_create_scans(exchange):
    lua = collector(exchange)
    lua.execute("Game.event('REPLICATE_ITEM_LIST_UPDATE')")
    assert lua.eval("#Cennik.db.market_scans") == 0
    lua.execute(
        "Cennik.MarketCollector:Tick(1); Game.event('AUCTION_HOUSE_CLOSED'); Game.event('REPLICATE_ITEM_LIST_UPDATE')"
    )
    assert lua.eval("#Cennik.db.market_scans") == 0


def test_empty_update_waits_for_real_response_then_ignores_duplicates(exchange):
    lua = collector(exchange)
    lua.execute(
        "Cennik.MarketCollector:Tick(1); C_AuctionHouse.GetNumReplicateItems=function() return 0 end; Game.event('REPLICATE_ITEM_LIST_UPDATE')"
    )
    assert lua.eval("#Cennik.db.market_scans") == 0
    assert lua.eval("Cennik.MarketCollector.requested_at~=nil")
    lua.execute(
        "C_AuctionHouse.GetNumReplicateItems=function() return 1 end; Game.event('REPLICATE_ITEM_LIST_UPDATE'); Game.event('REPLICATE_ITEM_LIST_UPDATE')"
    )
    assert lua.eval("#Cennik.db.market_scans") == 1
    assert lua.eval("Cennik.db.market_scans[1].full")


def test_empty_timeout_is_partial_and_never_claims_full_market(exchange):
    lua = collector(exchange)
    lua.execute(
        "Cennik.MarketCollector:Tick(1); C_AuctionHouse.GetNumReplicateItems=function() return 0 end; Game.event('REPLICATE_ITEM_LIST_UPDATE'); Game.now=Game.now+Cennik.MarketCollector.responseTimeout+1; Cennik.MarketCollector:Tick(1)"
    )
    assert lua.eval("#Cennik.db.market_scans") == 1
    assert not lua.eval("Cennik.db.market_scans[1].full")
    lua.execute("Game.event('REPLICATE_ITEM_LIST_UPDATE')")
    assert lua.eval("#Cennik.db.market_scans") == 1
    lua.execute("Cennik.MarketCollector:Tick(1)")
    assert lua.eval("Game.replicates") == 1


def test_slow_server_response_is_accepted_without_duplicate_request(exchange):
    lua = collector(exchange)
    lua.execute("""
      Cennik.MarketCollector:Tick(1)
      Game.advance(180);Cennik.MarketCollector:Tick(1)
      assert(Cennik.MarketCollector.requested_at)
      assert(Cennik.CollectorStatus.db.current.code=='REQUESTED')
      assert(Cennik.CollectorStatus.db.current.detail:find('180 / 600',1,true))
      Game.advance(419);Cennik.MarketCollector:Tick(1)
      assert(Cennik.MarketCollector.requested_at)
      assert(Game.replicates==1)
      Game.event('REPLICATE_ITEM_LIST_UPDATE')
      assert(#Cennik.db.market_scans==1 and Cennik.db.market_scans[1].full)
      assert(Cennik.db.market_scans[1].received_at-Cennik.db.market_scans[1].requested_at==599)
      Game.event('REPLICATE_ITEM_LIST_UPDATE')
      assert(#Cennik.db.market_scans==1)
    """)


def test_empty_events_cannot_extend_wait_forever_or_publish_empty_market(exchange):
    lua = collector(exchange)
    lua.execute("""
      Cennik.MarketCollector:Tick(1)
      C_AuctionHouse.GetNumReplicateItems=function() return 0 end
      for i=1,6 do
        Game.advance(100);Game.event('REPLICATE_ITEM_LIST_UPDATE');Cennik.MarketCollector:Tick(1)
        assert(Cennik.MarketCollector.requested_at)
      end
      Game.advance(1);Cennik.MarketCollector:Tick(1)
      assert(not Cennik.MarketCollector.requested_at)
      assert(Game.replicates==1 and #Cennik.db.market_scans==1)
      assert(not Cennik.db.market_scans[1].full)
      assert(Cennik.CollectorStatus.db.current.code=='SCAN_TIMEOUT')
      assert(Cennik.CollectorStatus.db.last_error.detail:find('600',1,true))
      Game.event('REPLICATE_ITEM_LIST_UPDATE')
      assert(#Cennik.db.market_scans==1)
    """)


def test_shared_scan_uses_extended_wait_and_keeps_shared_status(exchange):
    lua = collector(exchange)
    lua.execute("""
      C_AuctionHouse.ReplicateItems()
      Game.advance(180);Cennik.MarketCollector:Tick(1)
      assert(Cennik.CollectorStatus.db.current.code=='SHARED_SCAN')
      assert(Game.replicates==1)
      Game.event('REPLICATE_ITEM_LIST_UPDATE')
      assert(Cennik.db.market_scans[1].full)
    """)


def test_row_processing_is_not_aborted_by_response_timeout(exchange):
    lua = collector(exchange)
    lua.execute("""
      C_AuctionHouse.GetNumReplicateItems=function() return 2000 end
      Cennik.MarketCollector:Tick(1);Game.event('REPLICATE_ITEM_LIST_UPDATE')
      assert(Cennik.MarketCollector.packing)
      Game.now=Game.now+Cennik.MarketCollector.responseTimeout+1
      Cennik.MarketCollector:Tick(1)
      assert(Cennik.MarketCollector.packing)
      Game.advance(0)
      assert(Cennik.db.market_scans[1].full)
      assert(Cennik.db.market_scans[1].count==2000)
    """)


def test_full_queue_rotates_oldest_without_reload(exchange):
    lua = collector(exchange)
    lua.execute(
        "Cennik.db.market_scans={{scan_id='a'},{scan_id='b'},{scan_id='c'}}; Cennik.MarketCollector:Tick(1)"
    )
    assert lua.eval("Game.replicates") == 1
    lua.execute("Game.event('REPLICATE_ITEM_LIST_UPDATE')")
    assert lua.eval("#Cennik.db.market_scans") == 3
    assert lua.eval("Cennik.db.market_scans[1].scan_id") == "b"
    assert lua.eval("Cennik.db.collector.rotated") == 1
    lua.execute("CennikImport.acknowledged_scan_ids={'b'}; Cennik.MarketCollector:Initialize()")
    assert lua.eval("#Cennik.db.market_scans") == 2
    assert lua.eval("Cennik.db.market_scans[1].scan_id") == "c"
    assert lua.eval("Cennik.db.market_scans[2].full")


def test_wrong_character_market_never_starts_scan(exchange):
    lua = collector(exchange)
    lua.execute("Cennik.db.binding=nil; Cennik.MarketCollector:Tick(1)")
    assert lua.eval("Game.replicates") == 0


def test_timeout_with_full_queue_does_not_grow_queue_or_replace_complete_scans(exchange):
    lua = collector(exchange)
    lua.execute(
        "Cennik.db.market_scans={{scan_id='a',full=true},{scan_id='b',full=true},{scan_id='c',full=true}};Cennik.MarketCollector:Tick(1);Cennik.MarketCollector.emptyResponse=true;Game.advance(Cennik.MarketCollector.responseTimeout+1);Cennik.MarketCollector:Tick(1)"
    )
    assert lua.eval("#Cennik.db.market_scans") == 3
    assert lua.eval("Cennik.db.market_scans[1].scan_id") == "a"
    assert lua.eval("Cennik.db.collector.skipped_partial") == 1


def test_game_auto_scan_toggle_survives_reload_until_web_configuration_changes(exchange):
    lua = collector(exchange)
    lua.execute("""
      CennikImport.market_collector={workspace_id='fresh',market=t.market,auto_scan=true}
      Cennik.MarketCollector:Initialize()
      Cennik.db.collector.enabled=false
      Cennik.MarketCollector:Initialize()
    """)
    assert not lua.eval("Cennik.db.collector.enabled")
    lua.execute(
        "CennikImport.market_collector.auto_scan=false; Cennik.MarketCollector:Initialize()"
    )
    lua.execute("CennikImport.market_collector.auto_scan=true; Cennik.MarketCollector:Initialize()")
    assert lua.eval("Cennik.db.collector.enabled")


def test_new_workspace_keeps_history_but_hides_old_execution_queue(exchange):
    lua = collector(exchange)
    lua.execute("""
      Cennik.db.queue={t.task_id}
      CennikImport.packages={}
      CennikImport.market_collector={workspace_id='fresh',market=t.market,auto_scan=true}
      Cennik:Import()
    """)
    assert lua.eval("Cennik.db.current_run") is None
    assert lua.eval("#Cennik.db.queue") == 0
    assert lua.eval("Cennik.db.tasks[t.task_id]~=nil")


def test_profile_switch_never_discards_uncertain_purchase(exchange):
    lua = collector(exchange)
    lua.execute("""
      Cennik.db.pending={task_id=t.task_id,stage='unknown'}
      CennikImport.market_collector={workspace_id='fresh',market=t.market,auto_scan=true}
      Cennik:Import()
    """)
    assert lua.eval("Cennik.db.pending.stage") == "unknown"
    assert lua.eval("Cennik.db.workspace_id") is None
    assert lua.eval("Cennik.db.current_run==t.run_id")
