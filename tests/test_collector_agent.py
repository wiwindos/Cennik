import json
import sys
import time
from types import SimpleNamespace

import httpx
import pytest
from auction_fixture import approve_hub
from fastapi.testclient import TestClient
from test_accounting import accounting_lua, lua_value
from test_collectors import ADMIN, sample, transport
from test_workspace import workspace as workspace

from cennik.addon.lua_data import Parser, lua_literal
from cennik.collector.agent import CollectorAgent, game_directory
from cennik.collector.server import create_hub
from cennik.collector.startup import set_startup, startup_command


def game(tmp_path):
    root = tmp_path / "game with spaces"
    (root / "Interface/AddOns/Auctionator").mkdir(parents=True)
    return root


def imported(root):
    parser = Parser((root / "Interface/AddOns/CennikAddon/ImportedData.lua").read_text("utf-8"))
    assert parser.identifier() == "CennikImport"
    parser.require("=")
    return parser.value()


def saved(root, value, account="first"):
    path = root / "WTF/Account" / account / "SavedVariables/CennikAddon.lua"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("CennikAddonDB="+lua_literal(value), "utf-8")
    return path


def test_setup_lua_saved_upload_ack_and_registry_without_sql(tmp_path):
    app = create_hub(tmp_path / "hub", ADMIN)
    approve_hub(app.state.hub)
    invitation = app.state.hub.invite("Collector")
    root = game(tmp_path)
    with TestClient(app) as http:
        agent = CollectorAgent(tmp_path / "agent", transport(http))
        agent.configure(str(root), "http://localhost:8768", invitation["token"])
        initial = imported(root)
        assert initial["collector_only"] and not initial["packages"]
        assert not initial.get("accounting") and not initial.get("production")
        source = sample()
        path = saved(root, source)
        agent.tick(force=True)
        payload = imported(root)
        assert payload["acknowledged_scan_ids"] == [source["market_scans"][0]["scan_id"]]
        assert payload["auctioneer_registry"]["entries"]
        assert payload["collector_delivery"]["at"]
        assert path.read_text("utf-8").startswith("CennikAddonDB=")
        assert agent.overview()["pending"] == 0
        assert app.state.hub.overview()["members"][0]["counts"]["full_scans"] == 1
        assert all(secret not in agent.report() for secret in (invitation["token"], "PRIVATE_CHARACTER", "PRIVATE_WORKSPACE", str(root)))
        assert all(secret not in (root / "Interface/AddOns/CennikAddon/ImportedData.lua").read_text("utf-8") for secret in (invitation["token"], ADMIN))
        assert not list((tmp_path / "agent").rglob("*.sqlite")) and not list((tmp_path / "agent").rglob("*.duckdb"))
        agent.tick(force=True)
        assert app.state.hub.overview()["members"][0]["counts"]["full_scans"] == 1


def test_offline_restart_queue_and_pause_do_not_lose_data(tmp_path):
    root = game(tmp_path)
    def offline(request):
        raise httpx.ConnectError("offline", request=request)
    agent = CollectorAgent(tmp_path / "agent", httpx.MockTransport(offline))
    agent.configure(str(root), "http://localhost:8768", "private")
    source = sample()
    saved(root, source)
    agent.tick(force=True)
    assert agent.overview()["error"] and agent.overview()["pending"] == 5
    assert imported(root)["acknowledged_scan_ids"]
    agent.pause(True)
    assert agent.overview()["status"] == "Приостановлено"
    assert imported(root)["market_collector"]["auto_scan"] is False
    again = CollectorAgent(tmp_path / "agent", httpx.MockTransport(offline))
    assert again.overview()["pending"] == 5 and again.settings["paused"]
    again.pause(False)
    again.tick(force=True)
    assert again.overview()["pending"] == 5


def test_invalid_saves_have_no_ack_and_other_accounts_continue(tmp_path):
    root = game(tmp_path)
    agent = CollectorAgent(tmp_path / "agent", httpx.MockTransport(lambda r: httpx.Response(503)))
    agent.configure(str(root), "http://localhost:8768", "private")
    path = saved(root, sample(), "a")
    path.write_text("CennikAddonDB=os.execute('bad')", "utf-8")
    saved(root, sample(), "b")
    agent.tick(force=True)
    assert agent.overview()["error"]
    assert len(agent.files) == 1
    assert imported(root)["acknowledged_scan_ids"]
    assert path.read_text("utf-8").endswith("('bad')")


def test_rejects_owner_installation_and_bad_connection_before_install(tmp_path):
    root = game(tmp_path)
    agent = CollectorAgent(tmp_path / "agent")
    with pytest.raises(ValueError, match="HTTPS"):
        agent.configure(str(root), "http://remote.example", "token")
    assert not (root / "Interface/AddOns/CennikAddon").exists()
    addon = root / "Interface/AddOns/CennikAddon"
    addon.mkdir()
    path = addon / "ImportedData.lua"
    original = "CennikImport={format_version=2,routes={}}"
    path.write_text(original, "utf-8")
    with pytest.raises(ValueError, match="другому Cennik"):
        agent.configure(str(root), "https://collector.example", "token")
    assert path.read_text("utf-8") == original


def test_stable_file_wait_and_missing_game_are_visible(tmp_path):
    root = game(tmp_path)
    agent = CollectorAgent(tmp_path / "agent", httpx.MockTransport(lambda r: httpx.Response(503)))
    agent.configure(str(root), "http://localhost:8768", "token")
    path = saved(root, sample())
    agent.tick()
    assert not agent.files
    stamp, _ = agent.quiet[str(path)]
    agent.quiet[str(path)] = (stamp, time.monotonic()-2)
    agent.tick()
    assert agent.files
    (root / "Interface/AddOns/CennikAddon/CennikAddon.toc").unlink()
    agent.tick()
    assert "Аддон не найден" in agent.overview()["error"]


def test_game_variants_and_startup_quoting(tmp_path, monkeypatch):
    root = game(tmp_path)
    assert game_directory(str(root)) == root
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "space folder/Collector.exe"))
    command = startup_command(tmp_path / "profile folder")
    assert command.startswith('"') and '" --root "' in command and command.endswith(" --background")


def test_collector_addon_has_no_trading_or_money_journal_but_collects_craft(workspace):
    lua = accounting_lua(workspace, collector_only=True)
    lua.execute("""
      Cennik.CollectorStatus.db.delivery={enabled=true};Cennik.db.collector.enabled=true
      Game.event('MERCHANT_SHOW');BuyMerchantItem(1,2)
      Game.bags[1]={item_id=2589,quantity=2};Game.money=Game.money-20
      Game.event('BAG_UPDATE_DELAYED');Game.advance(3)
      Cennik.Accounting:Emit('craft',{inputs={{item_id=2589,quantity=2}},outputs={{item_id=4238,quantity=1}}})
    """)
    assert lua.eval("Cennik.collectorOnly")
    assert lua.eval("Cennik.Accounting.db.events") is None
    assert lua.eval("#Cennik.db.collector_transforms") == 1
    assert not lua.eval("Cennik.Adapter:Call('PlaceBid',1,10)")
    assert lua.eval("Cennik:CheckTask({})")[0] is False
    lua.execute("SlashCmdList.CENNIK('sniper')")
    assert lua.eval("Cennik.CollectorUI.window:IsShown()")
    assert lua.eval("Cennik.db.binding.character") == "FixturePlayer"
    diagnostic = lua_value(lua.eval("Cennik.db.collector_transforms[1]"))
    assert not any(name in diagnostic for name in ("character", "money_copper", "task_id"))


def test_report_has_only_public_diagnostics(tmp_path):
    agent = CollectorAgent(tmp_path / "agent")
    agent.errors["files"] = "PRIVATE_FILE_PATH_AND_KEY"
    data = json.loads(agent.report())
    assert "PRIVATE_FILE_PATH_AND_KEY" not in agent.report()
    assert data["file_error"] == "FILE_IMPORT_FAILED"


def test_new_save_uploads_immediately_after_idle_heartbeat(tmp_path):
    app = create_hub(tmp_path / "hub", ADMIN)
    approve_hub(app.state.hub)
    invite = app.state.hub.invite("Collector")
    with TestClient(app) as http:
        root = game(tmp_path)
        agent = CollectorAgent(tmp_path / "agent", transport(http))
        agent.configure(str(root), "http://localhost:8768", invite["token"])
        path = saved(root, sample())
        agent.tick()  # heartbeat before a stable game save; next heartbeat is in 30s
        assert not agent.client.due()
        stamp, _ = agent.quiet[str(path)]
        agent.quiet[str(path)] = (stamp, time.monotonic()-2)
        agent.tick()
        assert agent.overview()["pending"] == 0
        assert app.state.hub.overview()["members"][0]["counts"]["full_scans"] == 1


def test_collector_addon_observes_real_craft_and_disenchant_without_financial_events(workspace):
    lua = accounting_lua(workspace, collector_only=True)
    lua.execute("""
      Cennik.CollectorStatus.db.delivery={enabled=true};Cennik.db.collector.enabled=true
      Game.bags[1]={item_id=2589,quantity=3}
      DoTradeSkill(1,1);Game.event('UNIT_SPELLCAST_SUCCEEDED','player','cast',100)
      Game.bags[1]=nil;Game.bags[2]={item_id=4238,quantity=1}
      Game.event('BAG_UPDATE_DELAYED');Game.advance(1)
      Game.event('UNIT_SPELLCAST_SENT','player','target','cast2',13262)
      Game.event('UNIT_SPELLCAST_SUCCEEDED','player','cast2',13262);Game.event('LOOT_OPENED')
      Game.bags[2]=nil;Game.bags[3]={item_id=10940,quantity=3}
      Game.event('LOOT_CLOSED');Game.advance(1)
    """)
    rows = lua_value(lua.eval("Cennik.db.collector_transforms"))
    assert [r["kind"] for r in rows] == ["craft", "disenchant"]
    assert rows[1]["outputs"][0]["quantity"] == 3
    assert lua.eval("Cennik.db.accounting") is None
    assert lua.eval("Cennik.db.sniper") is None


def test_startup_is_current_user_only_and_can_be_removed(tmp_path, monkeypatch):
    calls = []
    class Handle:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    registry = SimpleNamespace(HKEY_CURRENT_USER="current_user", REG_SZ=1,
        CreateKey=lambda hive, key: (calls.append((hive, key)) or Handle()),
        SetValueEx=lambda *args: calls.append(("set", args[-1])),
        DeleteValue=lambda *args: calls.append(("delete", args[-1])))
    monkeypatch.setitem(sys.modules, "winreg", registry)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Collector.exe"))
    set_startup(True, tmp_path / "profile")
    set_startup(False, tmp_path / "profile")
    assert calls[0][0] == "current_user" and calls[1][1].endswith(" --background")
    assert calls[-1] == ("delete", "CennikCollector")
