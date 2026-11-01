"""Verify no-key pairing inside the frozen Worker with a real local HTTP server."""

import argparse
import json
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from cennik.addon.lua_data import Parser
from cennik.server.app import create_server
from cennik.worker.agent import WorkerAgent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    key = "pairing-fixture-" + "x" * 40
    app = create_server(out / "server", key, public_origin=origin)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", proxy_headers=False)
    )
    thread = threading.Thread(target=server.run)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        game = out / "game"
        (game / "Interface/AddOns/Auctionator").mkdir(parents=True)
        agent = WorkerAgent(out / "worker")
        agent.configure(str(game), origin)
        code = agent.pairing["code"]
        profile = app.state.catalog.profiles()[0]
        with httpx.Client(base_url=origin, trust_env=False) as client:
            approved = client.post(
                "/server/api/pairing/approve",
                headers={"X-Owner-Key": key},
                json={"code": code, "profile_id": profile.id},
            )
            approved.raise_for_status()
        result_path = out / "worker-result.json"
        subprocess.run(
            [
                str(args.worker.resolve()),
                "--root",
                str(agent.root),
                "--smoke-test",
                str(result_path),
            ],
            check=True,
            timeout=90,
        )
        result = json.loads(result_path.read_text("utf-8"))
        assert result["frozen"] and result["tray_visible"] and not result["sql_loaded"], result
        assert result["status"] == "Работает" and result["version"] == "0.2.0", result
        assert not (agent.root / "pairing.json").exists()
        manifest = json.loads((agent.root / "manifest.json").read_text("utf-8"))
        assert manifest["clients"] == []
        parsed = Parser((game / "Interface/AddOns/CennikAddon/ImportedData.lua").read_text("utf-8"))
        assert parsed.identifier() == "CennikImport"
        parsed.require("=")
        payload = parsed.value()
        assert payload["discovery"]["worker_discovery"]
        assert not list((app.state.catalog.root / "pairing").glob("*.json"))
        print(
            json.dumps(
                {
                    "frozen_pairing": "passed",
                    "owner_approval": True,
                    "discovery_import": True,
                    "local_sql": False,
                }
            )
        )
    finally:
        server.should_exit = True
        thread.join(timeout=30)


if __name__ == "__main__":
    main()
