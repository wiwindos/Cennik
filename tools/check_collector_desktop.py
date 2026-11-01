"""Exercise the built EXE, native tray and real HTTP delivery on temporary profiles."""
import argparse
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import uvicorn

from cennik.addon.lua_data import Parser, lua_literal
from cennik.collector.agent import CollectorAgent
from cennik.collector.server import create_hub


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "tests"))
    from auction_fixture import approve_hub
    from test_collectors import sample
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    hub = create_hub(output / "hub", "a"*40)
    approve_hub(hub.state.hub)
    invitation = hub.state.hub.invite("EXE fixture")
    game = output / "game"
    (game / "Interface/AddOns/Auctionator").mkdir(parents=True)
    agent = CollectorAgent(output / "profile")
    agent.configure(str(game), f"http://127.0.0.1:{port}", invitation["token"])
    saved = game / "WTF/Account/fixture/SavedVariables/CennikAddon.lua"
    saved.parent.mkdir(parents=True)
    source = sample()
    saved.write_text("CennikAddonDB="+lua_literal(source), "utf-8")
    server = uvicorn.Server(uvicorn.Config(hub, host="127.0.0.1", port=port, log_level="warning", proxy_headers=False))
    thread = threading.Thread(target=server.run)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        assert server.started
        result_path = output / "exe-result.json"
        process = subprocess.run([str(args.exe.resolve()), "--root", str(agent.root), "--smoke-test", str(result_path)], timeout=90, check=True)
        assert process.returncode == 0
        result = json.loads(result_path.read_text("utf-8"))
        assert result["frozen"] and result["tray_visible"] and result["hide_to_tray"] and result["restored"]
        assert not result["sql_loaded"]
        counts = hub.state.hub.overview()["members"][0]["counts"]
        assert counts["full_scans"] == 1 and counts["rows"] == 2, counts
        assert counts["npc"] == counts["recipe"] == counts["disenchant"] == 1, counts
        assert agent.client.overview()["pending"] == 0
        parser = Parser((game / "Interface/AddOns/CennikAddon/ImportedData.lua").read_text("utf-8"))
        assert parser.identifier() == "CennikImport"
        parser.require("=")
        payload = parser.value()
        assert payload["collector_only"] and payload["auctioneer_registry"]["entries"]
        assert payload["acknowledged_scan_ids"] == [source["market_scans"][0]["scan_id"]]
        assert not list(agent.root.rglob("*.sqlite")) and not list(agent.root.rglob("*.duckdb"))
        print(json.dumps({"exe": "passed", "native_tray": True, "sql_loaded": False, "counts": counts}, ensure_ascii=False))
    finally:
        server.should_exit = True
        thread.join(timeout=15)


if __name__ == "__main__":
    main()
