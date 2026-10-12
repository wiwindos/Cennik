"""Bounded headless Edge UI check with a fake job runner; no AHledger requests."""

import argparse
import base64
import json
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import uvicorn
import websocket

from ahledger.config import Settings
from ahledger.web import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true", help="Check loopback-only automatic login")
    args = parser.parse_args()
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
        samples = sorted((project / "data/samples").glob("*/analytics/ahledger.duckdb"))
        source_database = samples[0] if samples else project / "data/analytics/ahledger.duckdb"
        shutil.copy2(source_database, analytics / "ahledger.duckdb")
        with duckdb.connect(str(analytics / "ahledger.duckdb"), read_only=True) as db:
            history_market, history_item, history_grain = db.execute(
                "SELECT market,item_id,granularity FROM price_history GROUP BY ALL ORDER BY count(*) DESC,item_id LIMIT 1"
            ).fetchone()
        app = create_app(Settings(data_dir=archive), key, local=args.local)
        # Widen freshness only in this isolated UI check; real old archives stay old.
        from cennik.core.models import AnalyticsConfig, AnalyticsSettings

        covered = [m for m in app.state.analytics.markets() if m["observed_at"]]
        chosen = max(covered, key=lambda m: m["observed_at"])["market"]
        # Invented two-step chain exists only in this temporary UI database, so
        # chain detail rendering is tested even when the real sample has no craft.
        with duckdb.connect(str(analytics / "ahledger.duckdb")) as db:
            now = datetime.now(UTC)

            def fixture_row(table, **fields):
                fields.update(retrieved_at=now, raw_id="ui-craft-fixture")
                columns = ",".join('"' + name + '"' for name in fields)
                db.execute(f'INSERT INTO "{table}" ({columns}) VALUES ({",".join("?" for _ in fields)})', list(fields.values()))

            for item_id, name, buy in [(90000001, "Fixture ore", 10), (90000002, "Fixture bar", None), (90000003, "Fixture craft", None)]:
                fixture_row("items", item_id=item_id, name=name, vendor_buy_copper=buy, binding="none", origin_dataset="fixture")
            for spell, output, material, quantity in [(90000101, 90000002, 90000001, 3), (90000102, 90000003, 90000002, 2)]:
                fixture_row("recipes", spell_id=spell, version_hash="ui-graph", name="Fixture graph step", profession="FixtureGraph",
                            result_item_id=output, qty_min=1, qty_max=1, payload_json=json.dumps({"reagents": [{"id": material, "qty": quantity}]}))
                fixture_row("recipe_inputs", spell_id=spell, version_hash="ui-graph", item_id=material, quantity=quantity)
                if db.execute("SELECT 1 FROM information_schema.tables WHERE table_name='recipe_sources'").fetchone():
                    fixture_row("recipe_sources", spell_id=spell, version_hash="ui-graph", market=chosen)
            fixture_row("dataset_metadata", dataset="crafting", market=chosen, status="COMPLETE")
            fixture_row("price_snapshots", market=chosen, item_id=90000003, min_copper=1000000000, quantity=10, observed_at=now, source="isolated UI fixture")
        app.state.analytics.update_settings(AnalyticsSettings(
            active_market=chosen, config=AnalyticsConfig(max_price_age_seconds=31536000)))
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
                condition_deadline = time.monotonic() + 20
                while time.monotonic() < condition_deadline:
                    if evaluate(expression):
                        return
                    time.sleep(0.1)
                raise RuntimeError("UI condition timed out: " + expression)

            call("Runtime.enable")
            wait_for("!!document.getElementById('key')")
            if not args.local:
                evaluate(
                    "sessionStorage.setItem('ahl-key', " + json.dumps(key) + "); location.reload()"
                )
            wait_for(
                "document.getElementById('dashboard') && !document.getElementById('dashboard').hidden"
            )
            if args.local:
                assert evaluate("document.getElementById('login').hidden && document.getElementById('logout').hidden")
                assert evaluate("!sessionStorage.getItem('ahl-key')")
                assert evaluate("document.getElementById('connection').textContent === 'Локально'")
            evaluate(
                "document.getElementById('search').value='2589'; document.getElementById('search-form').requestSubmit()"
            )
            wait_for("document.querySelectorAll('#search-results button').length > 0")
            evaluate("document.getElementById('history-market').value=" + json.dumps(history_market) +
                     ";document.getElementById('item-id').value=" + str(history_item) +
                     ";document.getElementById('granularity').value=" + json.dumps(history_grain) +
                     ";document.getElementById('history-form').requestSubmit()")
            wait_for("document.querySelectorAll('#history tr').length > 0")
            wait_for("document.getElementById('analytics-market').options.length > 1 && !document.getElementById('analytics-recalculate').disabled")
            assert app.state.analytics.thread is None  # Reading the UI never calculates.
            assert evaluate("document.getElementById('analytics-craft-graph').checked")
            assert evaluate("document.querySelector('#analytics-strategy option[value=craft_graph]') !== null")
            evaluate("document.getElementById('analytics-craft-depth').value='3';document.getElementById('analytics-settings').requestSubmit()")
            wait_for("document.getElementById('analytics-status').textContent.includes('Наблюдение:') && !document.getElementById('analytics-save').disabled")
            deadline_settings = time.monotonic() + 10
            while app.state.analytics.store.settings().config.strategy_settings.get("craft_graph", {}).get("max_depth") != 3:
                if time.monotonic() >= deadline_settings:
                    raise RuntimeError("CraftGraph settings were not saved")
                time.sleep(0.05)
            assert app.state.analytics.thread is None
            evaluate("document.getElementById('analytics-recalculate').click()")
            wait_for("document.querySelectorAll('#analytics-results tr').length > 0")
            wait_for("document.getElementById('analytics-diagnostics').textContent.includes('Сигналов:')")
            assert evaluate("document.getElementById('analytics-diagnostics').textContent.includes('Причина отсева')")
            assert evaluate("document.getElementById('analytics-diagnostics').textContent.length < 12000")
            assert not evaluate("document.getElementById('analytics-diagnostics').textContent.includes('payload_json')")
            evaluate("document.querySelector('#analytics-results button').click()")
            wait_for("!document.getElementById('analytics-detail').hidden")
            assert evaluate("document.getElementById('analytics-inputs').textContent.includes('indicative')")
            evaluate("document.getElementById('analytics-strategy').value='craft_graph';document.getElementById('analytics-filters').requestSubmit()")
            wait_for("Array.from(document.querySelectorAll('#analytics-results tr')).some(r=>r.textContent.includes('Fixture craft'))")
            evaluate("Array.from(document.querySelectorAll('#analytics-results tr')).find(r=>r.textContent.includes('Fixture craft')).querySelector('button').click()")
            wait_for("document.querySelectorAll('#analytics-inputs ol li').length === 2")
            assert evaluate("document.getElementById('analytics-inputs').textContent.includes('Порядок изготовления')")
            assert evaluate("document.getElementById('analytics-inputs').textContent.includes('Излишки не добавлены к прибыли')")
            evaluate("document.getElementById('analytics-strategy').value='';document.getElementById('analytics-filters').requestSubmit()")
            wait_for("document.querySelectorAll('#analytics-results tr').length > 0")
            evaluate("document.getElementById('analytics-sort').value='roi';document.getElementById('analytics-filters').requestSubmit()")
            wait_for("document.querySelectorAll('#analytics-results tr').length > 0")
            roi_values = evaluate("Array.from(document.querySelectorAll('#analytics-results tr'),r=>Number(r.cells[4].textContent.replace('%','')))")
            assert roi_values == sorted(roi_values, reverse=True)
            evaluate("document.querySelector('#analytics-results button').click()")
            wait_for("!document.getElementById('analytics-detail').hidden")
            screenshot = project / "data/reports/cennik-analytics-preview.png"
            evaluate("document.getElementById('analytics-title').scrollIntoView()")
            screenshot.write_bytes(base64.b64decode(call("Page.captureScreenshot", {"format":"png"})["data"]))
            other = next(m["market"] for m in app.state.analytics.markets() if m["market"] != chosen)
            evaluate("document.getElementById('analytics-market').value=" + json.dumps(other) + ";document.getElementById('analytics-market').dispatchEvent(new Event('change'))")
            wait_for("document.querySelectorAll('#analytics-results tr').length === 0")
            wait_for("document.getElementById('analytics-result-note').textContent.includes('ещё нет успешного')")
            assert app.state.analytics.store.settings().active_market == other
            evaluate("document.getElementById('analytics-market').value=" + json.dumps(chosen) + ";document.getElementById('analytics-market').dispatchEvent(new Event('change'))")
            wait_for("document.querySelectorAll('#analytics-results tr').length > 0")
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
            evaluate("document.getElementById('update-market').click()")
            wait_for("document.getElementById('update-market').disabled && !document.getElementById('stop').disabled")
            assert received[-1]["action"] == "update"
            assert received[-1]["markets"] == ["forever.pvp.alliance.us"]
            evaluate("document.getElementById('stop').click()")
            wait_for("!document.getElementById('update-market').disabled")
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
                "UI OK: authorization, search, saved history, Cennik manual calculation, details, sorting, market switch/history, fake start/stop, mobile layout; no AHledger requests"
            )
            print(output)
        finally:
            if ws:
                try:
                    ws.send(json.dumps({"id": serial + 1, "method": "Browser.close"}))
                except (OSError, websocket.WebSocketException):
                    pass
                ws.close()
            app.state.controller.stop()
            server.should_exit = True
            server_thread.join(timeout=3)
            if browser:
                try:
                    browser.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    subprocess.run(["taskkill", "/PID", str(browser.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                    browser.wait(timeout=5)


if __name__ == "__main__":
    main()
