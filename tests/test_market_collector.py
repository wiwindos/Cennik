
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
    lua.execute("Cennik.MarketCollector:Tick(1); Game.event('AUCTION_HOUSE_CLOSED'); Game.event('REPLICATE_ITEM_LIST_UPDATE')")
    assert lua.eval("#Cennik.db.market_scans") == 0


def test_empty_update_waits_for_real_response_then_ignores_duplicates(exchange):
    lua = collector(exchange)
    lua.execute("Cennik.MarketCollector:Tick(1); C_AuctionHouse.GetNumReplicateItems=function() return 0 end; Game.event('REPLICATE_ITEM_LIST_UPDATE')")
    assert lua.eval("#Cennik.db.market_scans") == 0
    assert lua.eval("Cennik.MarketCollector.requested_at~=nil")
    lua.execute("C_AuctionHouse.GetNumReplicateItems=function() return 1 end; Game.event('REPLICATE_ITEM_LIST_UPDATE'); Game.event('REPLICATE_ITEM_LIST_UPDATE')")
    assert lua.eval("#Cennik.db.market_scans") == 1
    assert lua.eval("Cennik.db.market_scans[1].full")


def test_empty_timeout_is_partial_and_never_claims_full_market(exchange):
    lua = collector(exchange)
    lua.execute("Cennik.MarketCollector:Tick(1); C_AuctionHouse.GetNumReplicateItems=function() return 0 end; Game.event('REPLICATE_ITEM_LIST_UPDATE'); Game.now=Game.now+121; Cennik.MarketCollector:Tick(1)")
    assert lua.eval("#Cennik.db.market_scans") == 1
    assert not lua.eval("Cennik.db.market_scans[1].full")
    lua.execute("Game.event('REPLICATE_ITEM_LIST_UPDATE')")
    assert lua.eval("#Cennik.db.market_scans") == 1
    lua.execute("Cennik.MarketCollector:Tick(1)")
    assert lua.eval("Game.replicates") == 1


def test_full_queue_pauses_without_discarding_scans(exchange):
    lua = collector(exchange)
    lua.execute("Cennik.db.market_scans={{scan_id='a'},{scan_id='b'},{scan_id='c'}}; Cennik.MarketCollector:Tick(1)")
    assert lua.eval("Game.replicates") == 0
    assert lua.eval("#Cennik.db.market_scans") == 3
    lua.execute("CennikImport.acknowledged_scan_ids={'b'}; Cennik.MarketCollector:Initialize()")
    assert lua.eval("#Cennik.db.market_scans") == 2
    assert lua.eval("Cennik.db.market_scans[1].scan_id") == "a"
    assert lua.eval("Cennik.db.market_scans[2].scan_id") == "c"


def test_wrong_character_market_never_starts_scan(exchange):
    lua = collector(exchange)
    lua.execute("Cennik.db.binding=nil; Cennik.MarketCollector:Tick(1)")
    assert lua.eval("Game.replicates") == 0


def test_game_auto_scan_toggle_survives_reload_until_web_configuration_changes(exchange):
    lua = collector(exchange)
    lua.execute("""
      CennikImport.market_collector={workspace_id='fresh',market=t.market,auto_scan=true}
      Cennik.MarketCollector:Initialize()
      Cennik.db.collector.enabled=false
      Cennik.MarketCollector:Initialize()
    """)
    assert not lua.eval("Cennik.db.collector.enabled")
    lua.execute("CennikImport.market_collector.auto_scan=false; Cennik.MarketCollector:Initialize()")
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
