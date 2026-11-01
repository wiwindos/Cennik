"""Isolated browser + two addon runtimes + SavedVariables production QA."""
import argparse
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn

from cennik.addon.lua_data import lua_literal
from cennik.addon.models import ExportRequest
from cennik.workspace.web import create_app


def simulate(workspace, clients):
    from test_production import begin_mail, collect_mail, production_lua, relay, save_client

    order = workspace.production.status()[0]
    auctioneer, crafter = [production_lua(workspace, c) for c in clients]
    auctioneer.execute("Cennik.Accounting:Purchase('fixture',2589,6,60,{task_id="+lua_literal(order["task_ids"][0])+",strategy_id='simple_craft'})")
    begin_mail(auctioneer, crafter, 2589, 6)
    collect_mail(crafter, 2589, 6)
    crafter.execute("""
      Game.click(function() Cennik.Production:Craft() end)
      Game.event('UNIT_SPELLCAST_SUCCEEDED','player','one',100)
      Game.bags[1].quantity=3;Game.bags[2]={item_id=4238,quantity=1};Game.event('BAG_UPDATE_DELAYED');Game.advance(1)
      Game.event('UNIT_SPELLCAST_SUCCEEDED','player','two',100)
      Game.bags[1]=nil;Game.bags[2].quantity=2;Game.event('BAG_UPDATE_DELAYED');Game.advance(1)
    """)
    for lua in (auctioneer, crafter):
        lua.execute("Game.event('TRADE_SHOW')")
    crafter.execute("Game.click(function() Cennik.Production:Prepare('trade') end);Game.trade={{item_id=4238,quantity=2}};Game.event('TRADE_ACCEPT_UPDATE',1,1)")
    auctioneer.execute("Game.event('TRADE_ACCEPT_UPDATE',1,1)")
    relay(crafter, auctioneer)
    crafter.execute("Game.bags={};Game.event('UI_INFO_MESSAGE',0,ERR_TRADE_COMPLETE);Game.advance(1)")
    auctioneer.execute("Game.bags={{item_id=4238,quantity=2}};Game.event('UI_INFO_MESSAGE',0,ERR_TRADE_COMPLETE);Game.advance(2)")
    auctioneer.execute("Cennik.Accounting:Emit('sale',{inputs={Cennik.Accounting:Line(4238,1)},money_copper=100})")
    for lua, client in zip((auctioneer, crafter), clients, strict=True):
        save_client(lua, client)
    return {"saved": 2}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("node", "playwright", "browser"):
        parser.add_argument("--"+name, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from test_production import production_setup

    with tempfile.TemporaryDirectory(prefix="cennik-production-ui-") as temporary:
        directory = Path(temporary)
        app = create_app(directory / "profile")
        workspace = app.state.workspace
        workspace.save_settings(workspace.settings().model_copy(update={"auto_import": False,
            "auto_recalculate": False, "auto_refresh": False}))
        clients, _, request, _ = production_setup(workspace, directory, create=False)
        workspace.exchange.export(ExportRequest(request_id="production-ui-fixture", opportunity_ids=request.opportunity_ids))

        @app.post("/fixture/production")
        def capture():
            return simulate(workspace, clients)

        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8767, proxy_headers=False, log_level="warning"))
        thread = threading.Thread(target=server.run)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(.1)
            subprocess.run([args.node, str(Path(__file__).with_suffix(".cjs")), args.playwright, args.browser,
                str(args.output)], check=True, timeout=180)
        finally:
            server.should_exit = True
            thread.join(timeout=30)
            workspace.close()


if __name__ == "__main__":
    main()
