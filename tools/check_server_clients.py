"""Run frozen Worker and Collector against the actual full-server HTTP stack."""

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
from cennik.server.app import create_server
from cennik.server.bridge import RegisterDevice
from cennik.worker.agent import WorkerAgent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--collector", type=Path)
    parser.add_argument("--hub", action="store_true", help="Run through a separate real WIW Hub HTTP gateway")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--scan-rows", type=int, default=0, help="Also deliver one large synthetic snapshot"
    )
    parser.add_argument(
        "--cycle-config",
        action="store_true",
        help="Verify addressed opt-in configuration; never inject game input",
    )
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    secret = "native-hub-fixture-" + "x" * 40
    app = create_server(out / "server", "native-fixture-" + "x" * 40, public_origin=origin,
        hub_secret=secret if args.hub else "")
    catalog = app.state.catalog
    profile = catalog.profiles()[0].model_copy(update={"realm_names": ["FixtureRealm"]})
    catalog.save(profile, source=True)
    device = app.state.bridge.register(
        profile.id,
        RegisterDevice(
            label="Native PC",
            clients=[
                {
                    "role": "auctioneer",
                    "character": "FixturePlayer",
                    "realm": "FixtureRealm",
                    "account": "first",
                },
                {
                    "role": "crafter",
                    "character": "Crafter",
                    "realm": "FixtureRealm",
                    "account": "second",
                },
            ],
        ),
    )
    game = out / "game"
    (game / "Interface/AddOns/Auctionator").mkdir(parents=True)
    backend_port = port
    if args.hub:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            backend_port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=backend_port, log_level="warning", proxy_headers=False))
    hub_server = hub_thread = None
    if args.hub:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "funpayparsers"))
        from marketwatch.app import create_app as hub_app
        from marketwatch.config import Config
        hub = hub_app(Config(db_path=out / "hub.sqlite3", environment="development", collect=False,
            cennik_url=f"http://127.0.0.1:{backend_port}", cennik_secret=secret))
        hub_server = uvicorn.Server(uvicorn.Config(hub, host="127.0.0.1", port=port, log_level="warning", proxy_headers=False))
        hub_thread = threading.Thread(target=hub_server.run)
        hub_thread.start()
    thread = threading.Thread(target=server.run)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        if hub_server:
            for _ in range(100):
                if hub_server.started:
                    break
                time.sleep(.05)
            assert hub_server.started
        agent = WorkerAgent(out / "worker")
        agent.configure(str(game), origin, device["token"])
        if args.cycle_config:
            from types import SimpleNamespace

            # HWND=0 is always invalid, including in the frozen process.
            target = {
                "hwnd": 0,
                "pid": 0,
                "started": 0,
                "path": str(game / "Wow.exe"),
                "title": "No game input",
            }
            agent.cycle.backend = SimpleNamespace(windows=lambda root: [target])
            agent.cycle.configure(
                target=target, route=agent.manifest["clients"][0], game_root=str(game), enabled=True
            )
        w = catalog.workspace(profile.id)
        client_id = device["clients"][0]
        event = {
            "event_id": client_id + ":ledger:1",
            "workspace_id": w.identity,
            "market": profile.market,
            "realm": "FixtureRealm",
            "character": "FixturePlayer",
            "observed_at": int(time.time()),
            "kind": "purchase",
            "outputs": [{"item_id": 2589, "quantity": 3}],
            "money_copper": 30,
        }
        scans = []
        if args.scan_rows:
            from cennik.collector.auctioneers import AuctioneerProposal, Review
            from cennik.collector.protocol import Observation

            assert 0 < args.scan_rows <= 1_000_000
            npc = AuctioneerProposal(
                npc_id=12345,
                npc_name="Native fixture",
                realm="FixtureRealm",
                map_id=84,
                map_name="Fixture",
                x=50,
                y=60,
                proposed_type="ordinary",
                observed_at=int(time.time()),
                build="fixture",
                server_id=profile.id,
                market=profile.market,
            )
            proposal = Observation(kind="auctioneer_proposal", data=npc.model_dump(mode="json"))
            app.state.hub.auctioneers.suggest(proposal.identity(), proposal.data, "native")
            app.state.hub.auctioneers.review(
                proposal.identity(), Review(action="approve", auction_type="ordinary")
            )
            scans = [
                {
                    "format_version": 1,
                    "scan_id": "native:large:1",
                    "market": profile.market,
                    "realm": "FixtureRealm",
                    "observed_at": int(time.time()),
                    "full": True,
                    "count": args.scan_rows,
                    "reported_count": args.scan_rows,
                    "data": ";".join(["2589:4:40:0:4:1:0"] * args.scan_rows),
                    "variants": [{"name": "Fixture", "link": "item:2589:0:0:0", "divisible": True}],
                    "auction_context": {
                        "scope_id": npc.scope_id(),
                        "revision": 1,
                        "auction_type": "ordinary",
                        "server_id": profile.id,
                        "npc_id": 12345,
                        "realm": "FixtureRealm",
                        "map_id": 84,
                        "x": 50,
                        "y": 60,
                        "session_id": "native:auction:1",
                    },
                }
            ]
        saved = game / "WTF/Account/first/SavedVariables/CennikAddon.lua"
        saved.parent.mkdir(parents=True)
        saved.write_text(
            "CennikAddonDB="
            + lua_literal(
                {
                    "device_id": client_id,
                    "binding": {
                        "market": profile.market,
                        "realm": "FixtureRealm",
                        "character": "FixturePlayer",
                    },
                    "accounting": {"events": [event]},
                    "production": {"profession_snapshot": {
                        "format_version": 1, "character": "FixturePlayer", "realm": "FixtureRealm",
                        "observed_at": int(time.time()), "disenchant": False,
                        "professions": {"tailoring": {"name": "Tailoring", "skill": 100,
                            "max_skill": 300, "observed_at": int(time.time()), "coverage": "levels_only", "recipes": {}}}}},
                    "market_scans": scans,
                }
            ),
            "utf-8",
        )
        result_path = out / "worker-result.json"
        subprocess.run(
            [
                str(args.worker.resolve()),
                "--root",
                str(agent.root),
                "--smoke-test",
                str(result_path),
                "--smoke-delay",
                "40" if args.scan_rows else "16",
            ],
            check=True,
            timeout=90,
        )
        result = json.loads(result_path.read_text("utf-8"))
        assert result["frozen"] and result["tray_visible"] and not result["sql_loaded"], result
        assert result["hide_to_tray"] and result["restored"]
        assert w.accounting.overview()["summary"]["purchase_copper"] == 30, agent.report()
        assert w.leveling.profiles()[client_id]["professions"]["tailoring"]["skill"] == 100
        assert (game / "Interface/AddOns/CennikAddon/Professions.lua").is_file()
        parser = Parser((game / "Interface/AddOns/CennikAddon/ImportedData.lua").read_text("utf-8"))
        assert parser.identifier() == "CennikImport"
        parser.require("=")
        payload = parser.value()
        assert payload["format_version"] == 2 and len(payload["routes"]) == 2
        if args.cycle_config:
            active = payload["routes"]["FixtureRealm:FixturePlayer"]["automation"]
            assert active["enabled"] and active["client_id"] == client_id
            assert payload["routes"]["FixtureRealm:Crafter"]["automation"] == {"enabled": False}
        assert (
            event["event_id"]
            in payload["routes"]["FixtureRealm:FixturePlayer"]["accounting"][
                "acknowledged_event_ids"
            ]
        )
        if scans:
            assert w.market.rows("SELECT count(*) n FROM working_offers")[0]["n"] == args.scan_rows
            assert payload["routes"]["FixtureRealm:FixturePlayer"]["acknowledged_scan_ids"] == [
                "native:large:1"
            ]
        assert not list(agent.root.rglob("*.sqlite")) and not list(agent.root.rglob("*.duckdb"))
        if args.collector:
            invitation = app.state.hub.invite("Native Collector", profile_id=profile.id)
            collector_game = out / "collector-game"
            (collector_game / "Interface/AddOns/Auctionator").mkdir(parents=True)
            collector = CollectorAgent(out / "collector")
            collector.configure(str(collector_game), origin + "/collect", invitation["token"])
            if scans:
                sample = {**scans[0], "scan_id": "collector:full:1", "count": 2, "reported_count": 2,
                    "observed_at": int(time.time()), "data": ";".join(["2589:4:40:0:4:1"] * 2)}
                collector_save = collector_game / "WTF/Account/collector/SavedVariables/CennikAddon.lua"
                collector_save.parent.mkdir(parents=True)
                collector_save.write_text("CennikAddonDB=" + lua_literal({"market_scans": [sample],
                    "server_binding": profile.public(), "binding": {"realm": "FixtureRealm"},
                    "collector_status": {"collector_id": "native-collector", "current": {"code": "READY", "at": int(time.time())},
                        "scans": 1, "rows": 2}}), "utf-8")
            cresult = out / "collector-result.json"
            subprocess.run(
                [
                    str(args.collector.resolve()),
                    "--root",
                    str(collector.root),
                    "--smoke-test",
                    str(cresult),
                ],
                check=True,
                timeout=90,
            )
            value = json.loads(cresult.read_text("utf-8"))
            assert value["frozen"] and value["tray_visible"] and not value["sql_loaded"]
            assert collector.client.registry()["market_catalog"][0]["id"] == profile.id
            if scans:
                member = next(m for m in app.state.hub.members() if m["id"] == invitation["id"])
                assert member["counts"]["full_scans"] == 1 and member["counts"]["rows"] == 2
        print(
            json.dumps(
                {
                    "worker_exe": "passed",
                    "collector_exe": "passed" if args.collector else "not_run",
                    "private_purchase_copper": 30,
                    "addressed_routes": 2,
                    "full_scan_rows": args.scan_rows,
                    "local_sql": False,
                    "separate_hub_gateway": args.hub,
                }
            )
        )
    finally:
        if hub_server:
            hub_server.should_exit = True
            hub_thread.join(timeout=30)
        server.should_exit = True
        thread.join(timeout=30)


if __name__ == "__main__":
    main()
