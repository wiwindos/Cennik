"""Authorized live deployment check in one disposable, separately scoped market."""

import json
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx

from cennik.addon.lua_data import Parser, lua_literal
from cennik.collector.agent import CollectorAgent
from cennik.collector.auctioneers import AuctioneerProposal
from cennik.worker.agent import WorkerAgent

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/hub-deploy/live-clients"
ORIGIN = "https://205.172.56.135:8444"


def execute():
    OUT.mkdir(exist_ok=False)
    session = json.loads((ROOT / "outputs/hub-deploy/verification-session.json").read_text())
    with httpx.Client(
        base_url=ORIGIN,
        timeout=60,
        trust_env=False,
        headers={"Origin": ORIGIN, "X-CSRF-Token": session["csrf"]},
    ) as owner:
        owner.cookies.set("market_session", session["cookie"], domain="205.172.56.135", path="/")

        def api(method, path, body=None):
            for _ in range(30):
                result = (
                    owner.request(method, path, json=body)
                    if body is not None
                    else owner.request(method, path)
                )
                if result.status_code != 409:
                    break
                time.sleep(1)
            result.raise_for_status()
            return result.json()

        identity = uuid4().hex
        (OUT / "scope.json").write_text(json.dumps({"profile_id": identity}), "utf-8")
        profile = api(
            "POST",
            "/cennik/server/api/markets",
            {
                "profile": {
                    "id": identity,
                    "label": "Проверка развёртывания · временный рынок",
                    "client_family": "test",
                    "realm_names": ["DeploymentFixture"],
                    "ahl_market": None,
                },
                "enable_updates": False,
            },
        )
        (OUT / "scope.json").write_text(json.dumps({"profile_id": identity}), "utf-8")
        prefix = "/cennik/m/" + identity + "/api/v2"
        settings = api("GET", prefix + "/settings")["workspace"]
        settings.update(
            auto_recalculate=True, own_data_only=True, auto_refresh=False, auto_fyi=False
        )
        api("PUT", prefix + "/settings", settings)
        device = api(
            "POST",
            "/cennik/server/api/markets/" + identity + "/workers",
            {
                "label": "Deployment EXE check",
                "clients": [
                    {
                        "role": "auctioneer",
                        "character": "DeployAuctioneer",
                        "realm": "DeploymentFixture",
                        "account": "first",
                    },
                    {
                        "role": "crafter",
                        "character": "DeployCrafter",
                        "realm": "DeploymentFixture",
                        "account": "second",
                    },
                ],
            },
        )
        invitation = api(
            "POST",
            "/cennik/server/api/collectors",
            {"label": "Deployment Collector check", "profile_id": identity},
        )
        (OUT / "scope.json").write_text(
            json.dumps(
                {
                    "profile_id": identity,
                    "device_id": device["id"],
                    "collector_id": invitation["id"],
                }
            ),
            "utf-8",
        )
        collector_game = OUT / "collector-game"
        (collector_game / "Interface/AddOns/Auctionator").mkdir(parents=True)
        collector = CollectorAgent(OUT / "collector")
        collector.configure(str(collector_game), ORIGIN + "/collect", invitation["token"])
        npc = AuctioneerProposal(
            server_id=identity,
            market=profile["market"],
            npc_id=12345,
            npc_name="Deployment synthetic NPC",
            realm="DeploymentFixture",
            map_id=84,
            x=50,
            y=60,
            proposed_type="ordinary",
            observed_at=int(time.time()),
            build="1.60.1",
        )
        collector.client.queue("auctioneer_proposal", npc.model_dump(mode="json"))
        assert collector.client.flush()["sent"] == 1
        proposals = api("GET", prefix + "/collectors/auctioneers")["items"]
        proposal = next(row for row in proposals if row["data"]["server_id"] == identity)
        api(
            "POST",
            prefix + "/collectors/auctioneers/" + proposal["record_id"] + "/review",
            {"action": "approve", "auction_type": "ordinary"},
        )
        for _ in range(30):
            rows = api("GET", prefix + "/collectors/auctioneers")["entries"]
            if any(r["scope_id"] == npc.scope_id() for r in rows):
                break
            time.sleep(1)
        else:
            raise AssertionError("Auctioneer approval not committed")
        game = OUT / "game"
        (game / "Interface/AddOns/Auctionator").mkdir(parents=True)
        agent = WorkerAgent(OUT / "worker")
        agent.configure(str(game), ORIGIN, device["token"])
        client_id = device["clients"][0]
        stamp = int(time.time())
        scan = {
            "format_version": 1,
            "scan_id": "deployment:full:100000",
            "market": profile["market"],
            "realm": "DeploymentFixture",
            "observed_at": stamp,
            "full": True,
            "count": 100000,
            "reported_count": 100000,
            "data": ";".join(["2589:4:40:0:4:1:0"] * 100000),
            "variants": [
                {
                    "name": "Synthetic cloth",
                    "link": "item:2589:0:0:0",
                    "divisible": True,
                    "vendor_sell": 13,
                }
            ],
            "auction_context": {
                "scope_id": npc.scope_id(),
                "server_id": identity,
                "revision": 1,
                "auction_type": "ordinary",
                "npc_id": 12345,
                "realm": "DeploymentFixture",
                "map_id": 84,
                "x": 50,
                "y": 60,
                "session_id": "deployment:auction",
            },
        }
        event = {
            "event_id": client_id + ":deployment:purchase",
            "workspace_id": "cennik-next-" + identity,
            "market": profile["market"],
            "realm": "DeploymentFixture",
            "character": "DeployAuctioneer",
            "observed_at": stamp,
            "kind": "purchase",
            "outputs": [{"item_id": 2589, "quantity": 3}],
            "money_copper": 30,
        }
        event["workspace_id"] = api("GET", prefix + "/overview")["workspace_id"]
        saved = {
            "device_id": client_id,
            "binding": {
                "market": profile["market"],
                "realm": "DeploymentFixture",
                "character": "DeployAuctioneer",
            },
            "accounting": {"events": [event]},
            "market_scans": [scan],
            "production": {
                "profession_snapshot": {
                    "format_version": 1,
                    "character": "DeployAuctioneer",
                    "realm": "DeploymentFixture",
                    "observed_at": stamp,
                    "disenchant": False,
                    "professions": {
                        "tailoring": {
                            "name": "Tailoring",
                            "skill": 100,
                            "max_skill": 300,
                            "observed_at": stamp,
                            "coverage": "levels_only",
                            "recipes": {},
                        }
                    },
                }
            },
        }
        path = game / "WTF/Account/first/SavedVariables/CennikAddon.lua"
        path.parent.mkdir(parents=True)
        path.write_text("CennikAddonDB=" + lua_literal(saved), "utf-8")
        subprocess.run(
            [
                str(ROOT / "dist/worker-0.5.0/CennikWorker.exe"),
                "--root",
                str(agent.root),
                "--background",
                "--smoke-test",
                str(OUT / "worker-result.json"),
                "--smoke-delay",
                "90",
            ],
            check=True,
            timeout=140,
        )
        result = json.loads((OUT / "worker-result.json").read_text())
        assert result["frozen"] and result["tray_visible"] and not result["sql_loaded"], result
        parser = Parser((game / "Interface/AddOns/CennikAddon/ImportedData.lua").read_text("utf-8"))
        assert parser.identifier() == "CennikImport"
        parser.require("=")
        payload = parser.value()
        assert len(payload["routes"]) == 2
        route = payload["routes"]["DeploymentFixture:DeployAuctioneer"]
        assert "deployment:full:100000" in route["acknowledged_scan_ids"], (
            "Full scan not acknowledged"
        )
        assert event["event_id"] in route["accounting"]["acknowledged_event_ids"], (
            "Accounting event not acknowledged"
        )
        overview = api("GET", prefix + "/overview")
        assert overview["archive"]["scans"] == 1 and overview["archive"]["rows"] == 100000, (
            overview["archive"]
        )
        sample = {
            **scan,
            "scan_id": "deployment:collector:2",
            "count": 2,
            "reported_count": 2,
            "observed_at": int(time.time()),
            "data": ";".join(["2589:4:40:0:4:1"] * 2),
        }
        sv = collector_game / "WTF/Account/collector/SavedVariables/CennikAddon.lua"
        sv.parent.mkdir(parents=True)
        sv.write_text("CennikAddonDB=" + lua_literal({"market_scans": [sample]}), "utf-8")
        subprocess.run(
            [
                str(ROOT / "dist/collector-0.5.0/CennikCollector.exe"),
                "--root",
                str(collector.root),
                "--background",
                "--smoke-test",
                str(OUT / "collector-result.json"),
            ],
            check=True,
            timeout=60,
        )
        value = json.loads((OUT / "collector-result.json").read_text())
        assert value["frozen"] and value["tray_visible"] and not value["sql_loaded"], value
        for _ in range(30):
            overview = api("GET", prefix + "/overview")
            if overview["archive"]["scans"] >= 2:
                break
            time.sleep(1)
        assert overview["archive"]["scans"] == 2, overview["archive"]
        report = {
            "public_https": True,
            "worker_frozen": True,
            "collector_frozen": True,
            "worker_full_scan_rows": 100000,
            "collector_full_scan_rows": 2,
            "addressed_routes": 2,
            "accounting_acknowledged": True,
            "opportunities": overview["analytics"]["opportunity_count"],
            "temporary_profile_id": identity,
        }
        (OUT / "result.json").write_text(json.dumps(report, indent=2), "utf-8")
        print(json.dumps(report), flush=True)


def main():
    try:
        execute()
    finally:
        scope = OUT / "scope.json"
        if scope.exists():
            from deploy_hub_ssh import connect

            identity = json.loads(scope.read_text())["profile_id"]
            source = ROOT / "tools/cleanup_live_test_market.py"
            with connect() as ssh:
                with ssh.open_sftp() as sftp:
                    sftp.put(str(source), "/home/webbox/cennik-deploy/cleanup-test.py")
                _, out, err = ssh.exec_command(
                    "python3 /home/webbox/cennik-deploy/cleanup-test.py --profile " + identity,
                    timeout=240,
                )
                output = out.read().decode()
                errors = err.read().decode()
                status = out.channel.recv_exit_status()
                (OUT / "cleanup.log").write_text(output + errors, "utf-8")
                if status:
                    raise RuntimeError("Test cleanup failed; see private deployment cleanup.log")
                print(
                    "Disposable test market and credentials cleaned; Cennik restarted", flush=True
                )


if __name__ == "__main__":
    main()
