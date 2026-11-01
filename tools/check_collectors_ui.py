"""Real HTTP delivery between two isolated servers, addon events and browser QA."""
import argparse
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn

from cennik.addon.lua_data import lua_literal
from cennik.collector.server import create_hub
from cennik.workspace.web import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('node', 'playwright', 'browser'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / 'tests'))
    from test_accounting import accounting_lua, lua_value

    with tempfile.TemporaryDirectory(prefix='cennik-collectors-ui-') as temporary:
        directory = Path(temporary)
        hub = create_hub(directory / 'hub', 'a'*40)
        invitation = hub.state.hub.invite('Тестовый сборщик')
        app = create_app(directory / 'profile')
        w = app.state.workspace
        game = directory / 'game'
        shutil.copytree(root / 'CennikAddon', game / 'Interface/AddOns/CennikAddon')
        w.save_settings(w.settings().model_copy(update={'game_root': str(game), 'account': 'fixture',
            'auto_import': False, 'auto_refresh': False, 'auto_recalculate': False}))

        @app.get('/fixture/keys')
        def keys():
            return {'token': invitation['token'], 'admin': 'a'*40}

        @app.post('/fixture/game')
        def capture():
            lua = accounting_lua(w)
            lua.execute("CennikImport.auctioneer_registry="+lua_literal(w.collectors.registry())+";Cennik.AuctionContext:Initialize()")
            assert lua.eval("Cennik.AuctionContext:Check('buy')")[0]
            lua.execute("""
              GetBuildInfo=function() return '1.60.1','fixture' end
              Game.replicates=0
              C_AuctionHouse.ReplicateItems=function() Game.replicates=Game.replicates+1 end
              C_AuctionHouse.GetNumReplicateItems=function() return 1 end
              C_AuctionHouse.GetReplicateItemInfo=function() return 'Linen',nil,4,nil,nil,nil,nil,nil,nil,40,0,nil,nil,nil,nil,nil,2589,true end
              C_AuctionHouse.GetReplicateItemLink=function() return 'item:2589:0:0:0' end
              C_AuctionHouse.GetReplicateItemTimeLeft=function() return 4 end
              C_AuctionHouse.GetItemKeyInfo=function() return {isCommodity=true} end
              Auctionator.State={FullScanFrameRef={CanInitiate=function() return true end,InitiateScan=function() C_AuctionHouse.ReplicateItems() end}}
              Cennik.db.collector.enabled=true;Cennik.db.collector.market='forever.pvp.alliance.us'
              Cennik.MarketCollector:Initialize();Game.event('AUCTION_HOUSE_SHOW');Cennik.MarketCollector:Tick(1)
              assert(Game.replicates==0);Game.advance(3);Cennik.MarketCollector:Tick(1);Game.event('REPLICATE_ITEM_LIST_UPDATE')
              Cennik.CollectorStatus:Set('SCAN_TIMEOUT');Cennik.CollectorUI:Show(true)
            """)
            saved = w.game_paths()[1]
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_text('CennikAddonDB='+lua_literal(lua_value(lua.globals().CennikAddonDB)), encoding='utf-8')
            args.output.joinpath('addon-support.txt').write_text(lua.eval('Cennik.CollectorStatus:Report()'), encoding='utf-8')
            return {'scans': 1}

        @app.post('/fixture/proposal')
        def suggest_auctioneer():
            lua = accounting_lua(w)
            lua.execute("Cennik.AuctionContext.registry={};assert(Cennik.AuctionContext:Propose('neutral'))")
            assert not lua.eval("Cennik.AuctionContext:Check('collect')")[0]
            saved = w.game_paths()[1]
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_text('CennikAddonDB='+lua_literal(lua_value(lua.globals().CennikAddonDB)), encoding='utf-8')
            return {'proposed': True, 'collection_blocked': True}

        servers = [uvicorn.Server(uvicorn.Config(a, host='127.0.0.1', port=p, proxy_headers=False, log_level='warning'))
                   for a, p in ((hub, 8768), (app, 8767))]
        threads = [threading.Thread(target=s.run) for s in servers]
        for thread in threads:
            thread.start()
        try:
            for _ in range(100):
                if all(s.started for s in servers):
                    break
                time.sleep(.1)
            subprocess.run([args.node, str(Path(__file__).with_suffix('.cjs')), args.playwright, args.browser,
                            str(args.output)], check=True, timeout=180)
        finally:
            for server in servers:
                server.should_exit = True
            for thread in threads:
                thread.join(timeout=30)
            if not w.stop.is_set():
                w.close()


if __name__ == '__main__':
    main()
