"""Run frozen Worker and Collector against the actual full-server HTTP stack."""
import argparse
import json
import socket
import subprocess
import threading
import time
from pathlib import Path

import uvicorn

from cennik.addon.lua_data import Parser, lua_literal
from cennik.collector.agent import CollectorAgent
from cennik.server.app import create_server
from cennik.server.bridge import RegisterDevice
from cennik.worker.agent import WorkerAgent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--collector", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    app = create_server(out / "server", "native-fixture-"+"x"*40, public_origin=origin)
    catalog = app.state.catalog
    profile = catalog.profiles()[0].model_copy(update={"realm_names": ["FixtureRealm"]})
    catalog.save(profile, source=True)
    device = app.state.bridge.register(profile.id, RegisterDevice(label="Native PC", clients=[
        {"role": "auctioneer", "character": "FixturePlayer", "realm": "FixtureRealm", "account": "first"},
        {"role": "crafter", "character": "Crafter", "realm": "FixtureRealm", "account": "second"}]))
    game = out / "game"
    (game / "Interface/AddOns/Auctionator").mkdir(parents=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", proxy_headers=False))
    thread = threading.Thread(target=server.run)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        assert server.started
        agent = WorkerAgent(out / "worker")
        agent.configure(str(game), origin, device["token"])
        w = catalog.workspace(profile.id)
        client_id = device["clients"][0]
        event = {"event_id": client_id+":ledger:1", "workspace_id": w.identity, "market": profile.market,
            "realm": "FixtureRealm", "character": "FixturePlayer", "observed_at": int(time.time()), "kind": "purchase",
            "outputs": [{"item_id": 2589, "quantity": 3}], "money_copper": 30}
        saved = game / "WTF/Account/first/SavedVariables/CennikAddon.lua"
        saved.parent.mkdir(parents=True)
        saved.write_text("CennikAddonDB="+lua_literal({"device_id": client_id, "binding": {"market": profile.market,
            "realm": "FixtureRealm", "character": "FixturePlayer"}, "accounting": {"events": [event]}}), "utf-8")
        result_path = out / "worker-result.json"
        subprocess.run([str(args.worker.resolve()), "--root", str(agent.root), "--smoke-test", str(result_path)], check=True, timeout=90)
        result = json.loads(result_path.read_text("utf-8"))
        assert result["frozen"] and result["tray_visible"] and not result["sql_loaded"], result
        assert result["hide_to_tray"] and result["restored"]
        assert w.accounting.overview()["summary"]["purchase_copper"] == 30, agent.report()
        parser = Parser((game / "Interface/AddOns/CennikAddon/ImportedData.lua").read_text("utf-8"))
        assert parser.identifier() == "CennikImport"
        parser.require("=")
        payload = parser.value()
        assert payload["format_version"] == 2 and len(payload["routes"]) == 2
        assert event["event_id"] in payload["routes"]["FixtureRealm:FixturePlayer"]["accounting"]["acknowledged_event_ids"]
        assert not list(agent.root.rglob("*.sqlite")) and not list(agent.root.rglob("*.duckdb"))
        if args.collector:
            invitation = app.state.hub.invite("Native Collector", profile_id=profile.id)
            collector_game = out / "collector-game"
            (collector_game / "Interface/AddOns/Auctionator").mkdir(parents=True)
            collector = CollectorAgent(out / "collector")
            collector.configure(str(collector_game), origin+"/collect", invitation["token"])
            cresult = out / "collector-result.json"
            subprocess.run([str(args.collector.resolve()), "--root", str(collector.root), "--smoke-test", str(cresult)], check=True, timeout=90)
            value = json.loads(cresult.read_text("utf-8"))
            assert value["frozen"] and value["tray_visible"] and not value["sql_loaded"]
            assert collector.client.registry()["market_catalog"][0]["id"] == profile.id
        print(json.dumps({"worker_exe": "passed", "collector_exe": "passed" if args.collector else "not_run",
            "private_purchase_copper": 30, "addressed_routes": 2, "local_sql": False}))
    finally:
        server.should_exit = True
        thread.join(timeout=30)


if __name__ == "__main__":
    main()
