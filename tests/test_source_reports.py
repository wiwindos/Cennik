import copy
import json
import time

import httpx
import pytest
from test_workspace import workspace as workspace

from ahledger.models import JobSpec
from ahledger.parsers import Parsed
from cennik.engines.npc_catalog import NpcCatalogItem
from cennik.workspace.source_reports import describe


def prices(rows, *, status="COMPLETE", stamp="2026-11-02T10:00:00+00:00"):
    return Parsed({"rows": [{"id": i, "minBuyout": value, "median": value + 5,
                             "quantity": 10, "median30d": value + 10} for i, value in rows]},
                  count=len(rows), status=status, maximum=stamp)


def spec(dataset="pricetable", **kwargs):
    return JobSpec(dataset=dataset, url="https://api.ahledger.com/v1/fixture", **kwargs)


def test_price_diffs_ignore_dates_and_retain_last_change_after_cache_and_failure(workspace):
    reports, request = workspace.source_reports, spec()
    first = reports.applied(request, prices([(1, 10), (2, 20)]))
    assert first["first_comparison"] and first["added"] == 2
    changed = reports.applied(request, prices([(1, 11), (3, 30)]))
    assert [changed[k] for k in ("added", "changed", "removed", "total")] == [1, 1, 1, 2]
    assert changed["samples"] == {"added": ["#3"], "changed": ["#1"], "removed": ["#2"]}
    same = reports.applied(request, prices([(1, 11), (3, 30)], stamp="2026-11-02T11:00:00+00:00"))
    assert same["state"] == "unchanged" and same["changed"] == 0
    reports.checked(request, cached=True)
    row = reports.overview()["scopes"][0]
    assert row["last"]["state"] == "cache" and row["last_change"] == changed
    reports.checked(request)
    row = reports.overview()["scopes"][0]
    assert row["last"]["origin"] == "not_modified"
    reports.failed(request, message="AHledger HTTP 404")
    row = reports.overview()["scopes"][0]
    assert row["last"]["state"] == "error" and row["last_success"]["origin"] == "not_modified"
    assert row["last_change"] == changed


def test_partial_responses_never_claim_removals(workspace):
    reports, request = workspace.source_reports, spec()
    reports.applied(request, prices([(1, 10), (2, 20)]))
    partial = reports.applied(request, prices([(1, 15), (3, 30)], status="PARTIAL"))
    assert partial["state"] == "partial" and partial["removed"] is None
    assert partial["added"] == partial["changed"] == 1
    after_partial = reports.applied(request, prices([(1, 15), (3, 30)]))
    assert after_partial["removed"] is None
    complete = reports.applied(request, prices([(1, 15)]))
    assert complete["removed"] == 1


def test_recipe_comparison_counts_mechanics_not_changing_market_valuations(workspace):
    request = spec("crafting", params={"profession": "Tailoring"})
    body = {"recipes": [{"recipe": {"spellId": 100, "name": "Fixture", "profession": "Tailoring"},
                          "result": {"id": 4238, "qtyMin": 1, "qtyMax": 1},
                          "reagents": [{"id": 2589, "qty": 3, "price": 10}], "cost": 30}]}
    workspace.source_reports.applied(request, Parsed(body, count=1))
    updated = copy.deepcopy(body)
    updated["recipes"][0]["cost"] = 300
    updated["recipes"][0]["reagents"][0]["price"] = 100
    result = workspace.source_reports.applied(request, Parsed(updated, count=1))
    assert result["changed"] == 0
    updated["recipes"][0]["reagents"][0]["qty"] = 4
    result = workspace.source_reports.applied(request, Parsed(updated, count=1))
    assert result["changed"] == 1 and result["samples"]["changed"] == ["Fixture"]
    assert describe(request)[0] != describe(spec("crafting", params={"profession": "Alchemy"}))[0]


def test_fyi_compares_previous_installed_version_and_reports_cache(workspace):
    previous = workspace.npc_catalog.snapshot().model_copy(update={"items": {2589: NpcCatalogItem(name="Cloth", buy_copper=10, sell_copper=2)}})
    updated = previous.model_copy(update={"version": "fixture-new", "items": {
        2589: NpcCatalogItem(name="Cloth", buy_copper=11, sell_copper=2),
        2592: NpcCatalogItem(name="Other cloth", buy_copper=20, sell_copper=4)}})
    result = workspace.source_reports.fyi(previous, updated)
    assert [result[k] for k in ("added", "changed", "removed")] == [1, 1, 0]
    assert not result["first_comparison"] and result["metadata_changed"]
    cached = workspace.source_reports.fyi(updated, updated, origin="cache")
    assert cached["state"] == "cache" and not cached["metadata_changed"]
    assert workspace.source_reports.overview()["scopes"][0]["last_change"] == result
    manual = updated.model_copy(update={"version": "fixture-manual", "items": {
        2589: NpcCatalogItem(name="Cloth", buy_copper=99, sell_copper=2)}})
    unchanged = workspace.source_reports.fyi(manual, manual)
    assert unchanged["added"] == unchanged["changed"] == unchanged["removed"] == 0


def test_request_reports_network_cache_304_error_and_seeds_older_cache(workspace):
    stamp = int(time.time()) - 30
    body = f"AHL1|forever/pvp/alliance/us|{stamp}|1\n2589:15:10:100:15:15:15:15:1000\n".encode()
    replies = [httpx.Response(200, content=body, headers={"etag": '"fixture"', "cache-control": "max-age=300"}),
               httpx.Response(304), httpx.Response(404)]
    seen = []

    def reply(request):
        seen.append(request)
        return replies.pop(0)

    workspace.sources.transport = httpx.MockTransport(reply)
    request = JobSpec(dataset="pricetable", url="https://api.ahledger.com/v1/pricetable/forever.pvp.alliance.us", market=workspace.market_key)
    workspace.request(request)
    assert len(seen) == 1
    # Simulate deployment onto an existing cache, before the new journal existed.
    baseline = workspace.source_reports.root / "baselines" / (describe(request)[0] + ".json")
    baseline.unlink()
    workspace.request(request)
    assert len(seen) == 1 and workspace.source_reports.overview()["scopes"][0]["last"]["state"] == "cache"
    with workspace.store.connection() as db:
        db.execute("UPDATE source_cache SET expires=0")
    workspace.last_request = 0
    workspace.request(request)
    assert seen[-1].headers["if-none-match"] == '"fixture"'
    assert workspace.source_reports.overview()["scopes"][0]["last"]["origin"] == "not_modified"
    with workspace.store.connection() as db:
        db.execute("UPDATE source_cache SET expires=0")
    workspace.last_request = 0
    with pytest.raises(ValueError, match="404"):
        workspace.request(request)
    result = workspace.source_reports.overview()["scopes"][0]
    assert result["last"]["state"] == "error" and result["last_success"]["total"] == 1


def test_unknown_datasets_are_not_claimed_unchanged_and_journal_is_bounded(workspace):
    reports = workspace.source_reports
    for i in range(110):
        reports.applied(spec("deals", item_id=i + 1), Parsed({}, count=7))
    state = json.loads(reports.path.read_text("utf-8"))
    assert len(state["recent"]) == 100
    assert state["recent"][0]["state"] == "loaded" and state["recent"][0]["changed"] is None
    assert len(reports.overview()["recent"]) == 20


def test_unreadable_optional_report_does_not_break_workspace_overview(workspace):
    workspace.source_reports.path.parent.mkdir(parents=True, exist_ok=True)
    workspace.source_reports.path.write_text("invalid-json", "utf-8")
    result = workspace.overview()
    assert result["source_changes"]["error"]
    assert result["market"] == workspace.market_key
