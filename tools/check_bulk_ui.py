"""End-to-end bulk research QA on an isolated profile and simulated game client."""
import argparse
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from lupa.lua51 import LuaRuntime

from ahledger.models import JobSpec
from ahledger.parsers import parse
from cennik.addon.lua_data import lua_literal
from cennik.workspace.models import MARKET, ItemFacts
from cennik.workspace.web import create_app

ROOT = Path(__file__).resolve().parents[1]


def python_value(value):
    if hasattr(value, "items"):
        entries = {python_value(k): python_value(v) for k, v in value.items()}
        if entries and set(entries) == set(range(1, len(entries)+1)):
            return [entries[i] for i in range(1, len(entries)+1)]
        return entries
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def simulate_game(addon, saved):
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute((ROOT / "tests/lua/wow_mock.lua").read_text("utf-8"))
    lua.execute('ns={}; function module(source) assert(loadstring(source))("CennikAddon",ns) end')
    for line in (ROOT / "CennikAddon/CennikAddon.toc").read_text("utf-8").splitlines():
        if line and not line.startswith("#"):
            lua.globals().module((ROOT / "CennikAddon" / line.replace("\\", "/")).read_text("utf-8"))
    lua.execute((addon / "ImportedData.lua").read_text("utf-8"))
    stamp = int(datetime.now(UTC).timestamp())
    lua.execute(f"Game.now={stamp};GetServerTime=function() return math.floor(Game.now) end; Cennik:Initialize()")
    lua.execute("Cennik:BindMarket('" + MARKET + "')")
    lua.execute("Game.commodity[2589]={{quantity=10,unitPrice=100,numOwnerItems=0}}")
    assert lua.eval("Cennik.Bulk:Start()")
    lua.execute("Game.event('COMMODITY_SEARCH_RESULTS_UPDATED',2589)")
    assert lua.eval("Cennik.db.bulk_checks.reports[1].status") == "complete"
    assert lua.eval("next(Cennik.db.tasks)") is None
    assert lua.eval("Cennik.db.pending") is None
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text("CennikAddonDB=" + lua_literal(python_value(lua.globals().CennikAddonDB)), encoding="utf-8")
    return {"status": "ok", "purchases": 0, "reports": 1}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("node", "playwright", "browser"):
        parser.add_argument("--"+name, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cennik-bulk-ui-") as temporary:
        directory = Path(temporary)
        app = create_app(directory / "profile")
        workspace = app.state.workspace
        game = directory / "game"
        addon = game / "Interface/AddOns/CennikAddon"
        shutil.copytree(ROOT / "CennikAddon", addon)
        saved = game / "WTF/Account/fixture/SavedVariables/CennikAddon.lua"
        workspace.save_settings(workspace.settings().model_copy(update={"game_root": str(game), "account": "fixture",
            "auto_import": False, "auto_recalculate": False, "auto_refresh": False}))
        workspace.update_facts(2589, ItemFacts(vendor_buy_copper=20))
        spec = JobSpec(dataset="pricetable", url="https://fixture.invalid/prices", market=MARKET)
        stamp = int(datetime.now(UTC).timestamp())-5
        body = f"AHL1|forever/pvp/alliance/us|{stamp}|1\n2589:101:101:100:101:101:101:101:2000\n".encode()
        workspace.market.import_api(spec, parse(spec, body), body, {})
        settings = workspace.store.settings()
        settings.config.enabled_strategies = ["npc_to_auction"]
        settings.config.craft_liquidity_enabled = False
        workspace.analytics.update_settings(settings)
        workspace.calculate()

        @app.post("/fixture/game-reload")
        def reload_game():
            return simulate_game(addon, saved)

        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8767, proxy_headers=False, log_level="warning"))
        thread = threading.Thread(target=server.run)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(.1)
            subprocess.run([args.node, str(Path(__file__).with_suffix(".cjs")), args.playwright, args.browser,
                            str(args.output)], check=True, timeout=150)
        finally:
            server.should_exit = True
            thread.join(timeout=30)
            workspace.close()


if __name__ == "__main__":
    main()
