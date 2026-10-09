"""Bounded headless Edge UI check with a fake job runner; no AHledger requests."""

import base64
import json
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn
import websocket

from ahledger.config import Settings
from ahledger.web import create_app


def main():
    project = Path(__file__).resolve().parents[1]
    key = "web-ui-check-only-" + "a" * 40
    edge_path = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    with tempfile.TemporaryDirectory(prefix="ahledger-ui-") as temporary:
        directory = Path(temporary)
        archive = directory / "data"
        archive.mkdir()
        with sqlite3.connect(
            (project / "data/state.sqlite").as_uri() + "?mode=ro", uri=True
        ) as source:
            destination = sqlite3.connect(archive / "state.sqlite")
            source.backup(destination)
            destination.close()
        analytics = archive / "analytics"
        analytics.mkdir()
        shutil.copy2(project / "data/analytics/ahledger.duckdb", analytics / "ahledger.duckdb")
        app = create_app(Settings(data_dir=archive), key)
        received = []

        def fake_job(request):
            received.append(request.model_dump())
            app.state.controller.cancelled.wait(timeout=25)
            app.state.controller.save(state="stopped")

        app.state.controller.execute = fake_job
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=18080, log_level="error")
        )
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()
        browser = None
        ws = None
        try:
            deadline = time.monotonic() + 20
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.05)
            browser = subprocess.Popen(
                [
                    str(edge_path),
                    "--headless=new",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--remote-debugging-port=19229",
                    "--remote-debugging-address=127.0.0.1",
                    "--window-size=1440,1100",
                    "--user-data-dir=" + str(directory / "profile"),
                    "http://127.0.0.1:18080/",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            targets = []
            while not targets and time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(
                        "http://127.0.0.1:19229/json/list", timeout=1
                    ) as response:
                        targets = [t for t in json.load(response) if t.get("type") == "page"]
                except OSError:
                    time.sleep(0.1)
            if not targets:
                raise RuntimeError("Edge did not start within the bounded check")
            ws = websocket.create_connection(
                targets[0]["webSocketDebuggerUrl"], timeout=5, suppress_origin=True
            )
            serial = 0
            exceptions = []

            def call(method, params=None):
                nonlocal serial
                serial += 1
                ws.send(json.dumps({"id": serial, "method": method, "params": params or {}}))
                while True:
                    response = json.loads(ws.recv())
                    if response.get("method") == "Runtime.exceptionThrown":
                        exceptions.append(response["params"])
                    if response.get("id") == serial:
                        if "error" in response:
                            raise RuntimeError(response["error"])
                        return response.get("result", {})

            def evaluate(expression):
                result = call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
                if "exceptionDetails" in result:
                    raise RuntimeError(result["exceptionDetails"])
                return result.get("result", {}).get("value")

            def wait_for(expression):
                while time.monotonic() < deadline:
                    if evaluate(expression):
                        return
                    time.sleep(0.1)
                raise RuntimeError("UI condition timed out: " + expression)

            call("Runtime.enable")
            wait_for("!!document.getElementById('key')")
            evaluate(
                "sessionStorage.setItem('ahl-key', " + json.dumps(key) + "); location.reload()"
            )
            wait_for(
                "document.getElementById('dashboard') && !document.getElementById('dashboard').hidden"
            )
            evaluate(
                "document.getElementById('search').value='2589'; document.getElementById('search-form').requestSubmit()"
            )
            wait_for("document.querySelectorAll('#search-results button').length > 0")
            evaluate("document.getElementById('history-form').requestSubmit()")
            wait_for("document.querySelectorAll('#history tr').length > 0")
            evaluate(
                "document.getElementById('rate').value='2'; document.getElementById('download-form').requestSubmit()"
            )
            wait_for("document.getElementById('start').disabled")
            assert received and received[0]["markets"] == ["forever.pvp.horde.us"]
            assert received[0]["requests_per_second"] == 2
            output = project / "data/reports/web-panel-preview.png"
            output.write_bytes(
                base64.b64decode(call("Page.captureScreenshot", {"format": "png"})["data"])
            )
            evaluate("document.getElementById('stop').click()")
            wait_for("!document.getElementById('start').disabled")
            call(
                "Emulation.setDeviceMetricsOverride",
                {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True},
            )
            assert evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            mobile = project / "data/reports/web-panel-mobile.png"
            mobile.write_bytes(
                base64.b64decode(call("Page.captureScreenshot", {"format": "png"})["data"])
            )
            assert not exceptions, exceptions
            print(
                "UI OK: authorization, search, actual saved history, fake start/stop, mobile layout; no AHledger requests"
            )
            print(output)
        finally:
            if ws:
                ws.close()
            app.state.controller.stop()
            server.should_exit = True
            server_thread.join(timeout=3)
            if browser:
                browser.terminate()
                try:
                    browser.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    browser.kill()


if __name__ == "__main__":
    main()
