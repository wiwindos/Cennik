"""Browser / Lua / SavedVariables accounting QA, on an isolated profile only."""
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
from cennik.workspace.web import create_app

ROOT = Path(__file__).resolve().parents[1]


def simulate(workspace, saved):
    sys.path.insert(0, str(ROOT / "tests"))
    from test_accounting import accounting_lua, lua_value

    lua = accounting_lua(workspace)
    lua.execute("""
      Game.event('MERCHANT_SHOW');BuyMerchantItem(1,6)
      Game.bags[1]={item_id=2589,quantity=6};Game.money=Game.money-60
      Game.event('BAG_UPDATE_DELAYED');Game.advance(1);Game.event('MERCHANT_CLOSED')
      DoTradeSkill(1,2);Game.event('UNIT_SPELLCAST_SUCCEEDED','player','first',100)
      Game.bags[1].quantity=3;Game.bags[2]={item_id=4238,quantity=1};Game.event('BAG_UPDATE_DELAYED');Game.advance(1)
      Game.event('UNIT_SPELLCAST_SUCCEEDED','player','second',100)
      Game.bags[1]=nil;Game.bags[2].quantity=2;Game.event('BAG_UPDATE_DELAYED');Game.advance(1)
      C_AuctionHouse.PostCommodity({itemID=4238},1,2,100)
      Game.bags={};Game.money=Game.money-10;Game.event('AUCTION_HOUSE_AUCTION_CREATED',42);Game.advance(1)
      Game.mail={{kind='seller',name=Cennik.Accounting.db.listings['42'].name,buyout=100,deposit=5,fee=5,money=100}}
      Game.event('MAIL_SHOW');TakeInboxMoney(1);Game.mail={};Game.event('MAIL_INBOX_UPDATE')
    """)
    assert lua.eval("#Cennik.db.accounting.events") == 5
    assert lua.eval("next(Cennik.db.tasks)") is None
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text("CennikAddonDB="+lua_literal(lua_value(lua.globals().CennikAddonDB)), encoding="utf-8")
    return {"events": 5}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("node", "playwright", "browser"):
        parser.add_argument("--"+name, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cennik-accounting-") as temporary:
        directory = Path(temporary)
        app = create_app(directory / "profile")
        workspace = app.state.workspace
        game = directory / "game"
        addon = game / "Interface/AddOns/CennikAddon"
        shutil.copytree(ROOT / "CennikAddon", addon)
        saved = game / "WTF/Account/fixture/SavedVariables/CennikAddon.lua"
        workspace.save_settings(workspace.settings().model_copy(update={"game_root": str(game), "account": "fixture",
            "auto_import": False, "auto_recalculate": False, "auto_refresh": False}))

        @app.post("/fixture/capture")
        def capture():
            return simulate(workspace, saved)

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
