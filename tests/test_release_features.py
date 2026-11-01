import json
import time
from datetime import UTC, datetime

import httpx
import pytest
from test_workspace import scan
from test_workspace import workspace as workspace

from ahledger.models import JobSpec
from cennik.sources import FYI, Sources, TokenUpdate


def test_upstream_key_is_private_scoped_and_not_assumed_to_raise_limits(tmp_path):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200, json={"markets": [{"id": "forever.pvp.alliance.eu", "label": "EU"}]}
        )

    sources = Sources(tmp_path, httpx.MockTransport(respond))
    assert sources.save_token(TokenUpdate(token="ahlk_fixture")) == {"ahl_token_set": True}
    assert sources.check_token()["ok"]
    assert seen[0].headers["authorization"] == "Bearer ahlk_fixture"
    assert "authorization" not in sources.headers(FYI)
    assert "ahlk_fixture" not in json.dumps(sources.public())
    sources.save_token(TokenUpdate(clear=True))
    assert not sources.token()


def test_fyi_same_beta_does_not_become_release_and_bad_update_keeps_old(tmp_path, workspace):
    previous = workspace.npc_catalog.snapshot()
    previous = previous.model_copy(update={"items": {2589: previous.items[2589]}})
    raw = {
        "v": 1,
        "build": previous.build,
        "source": "fixture beta",
        "items": {"2589": {"n": "Cloth", "buy": 55, "sell": 13}},
    }
    sources = Sources(tmp_path, httpx.MockTransport(lambda request: httpx.Response(200, json=raw)))
    document = sources.fyi_document(previous)
    assert document.phase == "beta" and document.items[2589].buy_quantity is None
    workspace.npc_catalog.install(document)
    raw["items"]["2589"]["buy"] = -1
    with pytest.raises(ValueError):
        sources.fyi_document(document, force=True)
    assert workspace.npc_catalog.snapshot().sha256 == document.sha256


def test_own_mode_uses_own_prices_and_complete_game_recipes(workspace):
    workspace.market.import_scan(scan())
    settings = workspace.settings().model_copy(update={"own_data_only": True})
    workspace.save_settings(settings)
    with pytest.raises(RuntimeError, match="без AHledger"):
        workspace.require_live_updates()
    stamp = int(time.time()) - 1
    report = {
        "observed_at": stamp,
        "recipes": {
            "100": {
                "spell_id": 100,
                "item_id": 4238,
                "qty_min": 1,
                "qty_max": 1,
                "inputs": [{"item_id": 2589, "quantity": 3}],
            }
        },
    }
    assert workspace.local_recipes.ingest(report)["updated"] == 1
    assert (
        workspace.local_recipes.ingest({"recipes": {"101": {"spell_id": 101}}})["incomplete"] == 1
    )
    with workspace.provider(None, workspace.market_key, datetime.now(UTC)) as provider:
        assert provider.current_prices()[2589].min_copper == 10
        assert not provider.source_reference(2589)
        assert provider.recipes()[0].result_item_id == 4238
    result = workspace.calculate()
    assert result["run"]["status"] == "completed"
    assert result["run"]["config"]["own_data_only"]


def test_source_schedule_does_not_fetch_ahl_in_own_mode(workspace):
    workspace.save_settings(
        workspace.settings().model_copy(
            update={"own_data_only": True, "auto_refresh": True, "auto_fyi": False}
        )
    )
    workspace.source_updates.ready_at = 0
    assert workspace.source_updates.due() is None
    assert not workspace.sources.headers("https://example.invalid/").get("Authorization")


def test_actual_price_request_uses_token_but_own_mode_ignores_newer_ahl_price(workspace):
    workspace.market.import_scan(scan())
    seen = []

    def respond(request):
        seen.append(request)
        text = f"AHL1|{workspace.market_key.replace('.', '/')}|{int(time.time())}|1\n2589:999:999:8:999:999:999:999:10\n"
        return httpx.Response(200, text=text, headers={"cache-control": "max-age=300"})

    workspace.sources.save_token(TokenUpdate(token="ahlk_private_fixture"))
    workspace.sources.transport = httpx.MockTransport(respond)
    spec = JobSpec(
        dataset="pricetable",
        url="https://api.ahledger.com/v1/pricetable/" + workspace.market_key,
        market=workspace.market_key,
    )
    assert workspace.request(spec)["changed"]
    assert seen[0].headers["authorization"] == "Bearer ahlk_private_fixture"
    assert "ahlk_private_fixture" not in json.dumps(workspace.overview(), default=str)
    workspace.save_settings(workspace.settings().model_copy(update={"own_data_only": True}))
    with workspace.provider(None, workspace.market_key, datetime.now(UTC)) as provider:
        assert provider.current_prices()[2589].min_copper == 10


def test_retry_after_and_truncated_catalog_cannot_replace_good_reference(tmp_path, workspace):
    calls = []

    def limited(request):
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": "3600"})

    sources = Sources(tmp_path, httpx.MockTransport(limited))
    for _ in range(2):
        with pytest.raises(RuntimeError):
            sources.markets(fresh=True)
    assert len(calls) == 1
    previous = workspace.npc_catalog.snapshot()
    raw = {
        "v": 1,
        "build": previous.build,
        "source": "fixture",
        "items": {"2589": {"n": "Cloth", "buy": 55, "sell": 13}},
    }
    sources.transport = httpx.MockTransport(lambda request: httpx.Response(200, json=raw))
    with pytest.raises(ValueError, match="сокращённый"):
        sources.fyi_document(previous, force=True)
    assert workspace.npc_catalog.snapshot().sha256 == previous.sha256


def test_fyi_timestamp_changes_do_not_invalidate_identical_prices(tmp_path, workspace):
    previous = workspace.npc_catalog.snapshot()
    previous = previous.model_copy(update={"items": {2589: previous.items[2589]}})
    item = previous.items[2589]
    raw = {
        "v": 1,
        "build": previous.build,
        "source": previous.source,
        "now": int(time.time()),
        "items": {"2589": {"n": item.name, "buy": item.buy_copper, "sell": item.sell_copper}},
    }
    sources = Sources(tmp_path, httpx.MockTransport(lambda request: httpx.Response(200, json=raw)))
    assert sources.fyi_document(previous).sha256 == previous.sha256
