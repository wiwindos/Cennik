from datetime import UTC, datetime

import pytest
from test_addon_lua import runtime
from test_npc_resale import package
from test_workspace import workspace as workspace

from cennik.addon.lua_data import lua_literal
from cennik.addon.models import ExportRequest, Package
from cennik.addon.service import ExchangeConflict
from cennik.core.models import Opportunity, Run
from cennik.workspace.bulk import BulkChoice, BulkCreate, BulkSelection
from cennik.workspace.models import MARKET


def opportunity(identity="one", strategy="simple_craft", item=4238):
    now = datetime.now(UTC)
    details = {"materials": [{"item_id": 2589, "quantity": 3, "source": "auction", "unit_price_copper": 10}],
               "result_unit_price_copper": 100}
    if strategy == "vendor_arbitrage":
        details["vendor_sell_copper"] = 100
    if strategy in {"buy_disenchant", "craft_disenchant"}:
        details["disenchant"] = {"quantity": 1, "rules": {"version": "fixture", "sha256": "a"*64},
            "yields": [{"material_item_id": 10940, "expected_count": "2", "min_count": 2, "max_count": 2, "chance": "1"}]}
    if strategy == "npc_to_auction":
        details["materials"] = [{"item_id": item, "quantity": 1, "source": "npc", "unit_price_copper": 30}]
    if strategy == "craft_graph":
        details["steps"] = [{"batches": 1, "required_quantity": 1, "output_quantity": 1, "surplus_quantity": 0,
                             "inputs": [{"item_id": 2589, "quantity": 3}]}]
    return Opportunity(id=identity, run_id="bulk-fixture", strategy_id=strategy, strategy_version="fixture",
        market=MARKET, item_id=item, item_name="Bulk fixture " + identity, action="fixture", observed_at=now,
        generated_at=now, buy_price_copper=10, reference_price_copper=100, quantity=1, required_capital_copper=10,
        potential_profit_copper=85, roi_percent=850, confidence="limited", risk_flags=[], reason="Fixture",
        source="fixture", data_age_seconds=0, costs_included=["purchase"], costs_excluded=[], details=details)


def setup(workspace, tmp_path, *values):
    ops = {o.id: o for o in values}
    addon = tmp_path / "game/Interface/AddOns/CennikAddon"
    addon.mkdir(parents=True)
    (addon / "Bulk.lua").write_text("-- fixture")
    (addon / "CennikAddon.toc").write_text("ImportedData.lua\nBulk.lua")
    saved = tmp_path / "game/WTF/Account/fixture/SavedVariables/CennikAddon.lua"
    workspace.game_paths = lambda: (addon, saved)
    run = Run(run_id="bulk-fixture", market=MARKET, started_at=datetime.now(UTC), as_of=datetime.now(UTC),
        generation=workspace.analytics.generation(), config=workspace.store.settings().config,
        strategy_versions={}, status="completed")
    workspace.store.detail = lambda market, identity: ops.get(identity)
    workspace.store.latest = lambda *args, **kwargs: run
    status = {"published_run": run.model_dump(mode="json"), "active_market": MARKET,
              "generation": run.generation, "settings_match": True, "result_generation_matches": True,
              "strategy_versions_match": True, "observation_stale": False}
    workspace.exchange.ready = lambda: status
    workspace.analytics.status = lambda: status
    return run, status, addon, saved


def book(item, prices=((10, 10),), *, when=None, commodity=True, **updates):
    return {"item_id": item, "observed_at": when or int(datetime.now(UTC).timestamp()), "status": "complete",
        "variants_count": 1, "rows": [{"item_key": {"itemID": item}, "commodity": commodity,
        "quantity": q if commodity else 1, "unit_price": p, "ownership_known": True, "owned": False,
        **({} if commodity else {"auction_id": i+1})} for i, (p, q) in enumerate(prices)], **updates}


def submit(workspace, identity, *books, **updates):
    b = workspace.bulk.load(identity)
    now = int(datetime.now(UTC).timestamp())
    raw = {"format_version": 1, "report_id": "fixture:bulk:1", "batch_id": identity,
           "workspace_id": workspace.identity, "market": MARKET, "started_at": b["created_at"],
           "completed_at": now, "status": "complete", "items": list(books), **updates}
    return workspace.bulk.accept({"reports": [raw]}), raw


def test_packet_deduplicates_inputs_is_separate_from_tasks_and_is_idempotently_imported(workspace, tmp_path):
    a, b = opportunity(), opportunity("two")
    setup(workspace, tmp_path, a, b)
    generation = workspace.generation()
    made = workspace.bulk.create(BulkCreate(opportunity_ids=[a.id, b.id]))
    assert made["unique_items"] == 2
    assert workspace.bulk.wire()["packet"]["item_ids"] == [2589, 4238]
    with workspace.store.connection() as db:
        assert db.execute("SELECT count(*) FROM addon_tasks").fetchone()[0] == 0
    accepted, raw = submit(workspace, made["batch_id"], book(2589), book(4238, ((100, 5),)))
    assert accepted["accepted"] == 1
    assert workspace.bulk.accept({"reports": [raw]})["accepted"] == 0
    assert workspace.generation() == generation
    assert raw["report_id"] in workspace.bulk.wire()["acknowledged_report_ids"]


def test_price_levels_capacity_and_fee_are_recalculated(workspace, tmp_path):
    a = opportunity()
    setup(workspace, tmp_path, a)
    identity = workspace.bulk.create(BulkCreate(opportunity_ids=[a.id]))["batch_id"]
    submit(workspace, identity, book(2589, ((10, 4), (20, 2))), book(4238, ((100, 10),)))
    result = workspace.bulk.detail(identity, [BulkChoice(opportunity_id=a.id, batches=2)])["items"][0]
    assert result["cost_copper"] == 80  # 4*10 + 2*20, not min*6
    assert result["profit_copper"] == 110  # 200 less 5% less 80
    assert result["available_batches"] == result["budget_batches"] == 2
    assert len(result["materials"][0]["offers"]) == 2


@pytest.mark.parametrize("strategy", ["vendor_arbitrage", "historical_discount", "simple_craft", "craft_graph",
                                       "buy_disenchant", "craft_disenchant", "npc_to_auction"])
def test_all_strategies_refine_auction_side_and_preserve_npc_or_yield_limits(workspace, tmp_path, strategy):
    a = opportunity(strategy=strategy)
    setup(workspace, tmp_path, a)
    identity = workspace.bulk.create(BulkCreate(opportunity_ids=[a.id]))["batch_id"]
    ids = workspace.bulk.load(identity)["item_ids"]
    reports = [book(i, ((100 if i in (10940, 4238) and strategy not in
                          {"vendor_arbitrage", "historical_discount", "buy_disenchant"} else 10, 20),)) for i in ids]
    if strategy == "buy_disenchant":
        reports = [book(i, ((100 if i == 10940 else 10, 20),)) for i in ids]
    submit(workspace, identity, *reports)
    result = workspace.bulk.detail(identity)["items"][0]
    assert result["complete"] and result["profit_copper"] > 0
    if strategy == "npc_to_auction":
        assert result["available_batches"] is None
        assert any("NPC" in x for x in result["warnings"])
    if "disenchant" in strategy:
        assert result["disenchant_outputs"][0]["unit_price_copper"] == 100


def test_owned_unknown_owners_and_noncommodity_search_group_not_counted_as_exact_lots(workspace, tmp_path):
    a = opportunity(strategy="historical_discount")
    setup(workspace, tmp_path, a)
    identity = workspace.bulk.create(BulkCreate(opportunity_ids=[a.id]))["batch_id"]
    b = book(4238, ((10, 10), (20, 10), (30, 10)), commodity=False)
    b["rows"][0]["owned"] = True
    b["rows"][1]["ownership_known"] = False
    b["rows"][2]["available_quantity"] = 500
    submit(workspace, identity, b)
    r = workspace.bulk.detail(identity)["items"][0]
    assert r["cost_copper"] == 30 and r["available_batches"] == 1
    assert any("AuctionID" in x for x in r["warnings"])


@pytest.mark.parametrize("problem", ["missing", "failed", "stale"])
def test_partial_or_stale_results_are_not_zero_supply_or_publishable(workspace, tmp_path, problem):
    a = opportunity()
    setup(workspace, tmp_path, a)
    identity = workspace.bulk.create(BulkCreate(opportunity_ids=[a.id]))["batch_id"]
    b = workspace.bulk.load(identity)
    when = b["created_at"]-2000 if problem == "stale" else b["created_at"]
    rows = [book(4238, ((100, 10),), when=when)]
    if problem != "missing":
        rows.append(book(2589, when=when, status="failed" if problem == "failed" else "complete"))
    if problem == "stale":
        b["created_at"] -= 2000
        from ahledger.util import atomic_json
        atomic_json(workspace.bulk.path(identity), b)
    submit(workspace, identity, *rows, status="partial" if problem != "stale" else "complete")
    r = workspace.bulk.detail(identity)["items"][0]
    assert not r["complete"] and r["profit_copper"] is None and r["available_batches"] is None
    with pytest.raises(ValueError):
        workspace.bulk.publish(identity, BulkSelection(choices=[BulkChoice(opportunity_id=a.id)]))


def test_joint_publication_cannot_spend_the_same_supply_twice_and_is_atomic(workspace, tmp_path):
    a, b = opportunity(), opportunity("two")
    setup(workspace, tmp_path, a, b)
    identity = workspace.bulk.create(BulkCreate(opportunity_ids=[a.id, b.id]))["batch_id"]
    submit(workspace, identity, book(2589, ((10, 5),)), book(4238, ((100, 10),)))
    assert all(x["complete"] for x in workspace.bulk.detail(identity)["items"])
    with pytest.raises(ValueError, match="совместная закупка"):
        workspace.bulk.publish(identity, BulkSelection(choices=[BulkChoice(opportunity_id=a.id), BulkChoice(opportunity_id=b.id)]))
    with workspace.store.connection() as db:
        assert db.execute("SELECT count(*) FROM addon_tasks").fetchone()[0] == 0


def test_final_selection_uses_existing_package_pipeline_with_updated_amounts_and_no_duplicate(workspace, tmp_path):
    a = opportunity()
    setup(workspace, tmp_path, a)
    identity = workspace.bulk.create(BulkCreate(opportunity_ids=[a.id]))["batch_id"]
    submit(workspace, identity, book(2589, ((10, 4), (20, 2))), book(4238, ((100, 10),)))
    selection = BulkSelection(choices=[BulkChoice(opportunity_id=a.id, batches=2)])
    result = workspace.bulk.publish(identity, selection)
    assert result["cost_copper"] == 80
    assert workspace.bulk.publish(identity, selection)["reused"]
    with workspace.store.connection() as db:
        package = Package.model_validate_json(db.execute("SELECT payload FROM addon_packages").fetchone()[0])
        assert db.execute("SELECT count(*) FROM addon_tasks").fetchone()[0] == 1
    task = package.tasks[0]
    assert task.opportunity.quantity == 2
    assert task.lines[0].quantity == 6 and task.lines[0].unit_limit_copper == 20
    assert task.max_total_cost_copper == 80 and task.predicted_profit_copper == 110
    assert task.opportunity.details["original_opportunity_id"] == a.id
    with pytest.raises(ValueError, match="уже передана"):
        workspace.bulk.publish(identity, BulkSelection(choices=[BulkChoice(opportunity_id=a.id)]))
    with pytest.raises(ExchangeConflict, match="уже экспортирована"):
        workspace.exchange.export(ExportRequest(request_id="regular-after-bulk", opportunity_ids=[a.id]))


def test_zero_profit_cannot_be_published(workspace, tmp_path):
    a = opportunity(strategy="vendor_arbitrage")
    setup(workspace, tmp_path, a)
    identity = workspace.bulk.create(BulkCreate(opportunity_ids=[a.id]))["batch_id"]
    submit(workspace, identity, book(a.item_id, ((100, 10),)))
    assert workspace.bulk.detail(identity)["items"][0]["profit_copper"] == 0
    with pytest.raises(ValueError, match="убыточная"):
        workspace.bulk.publish(identity, BulkSelection(choices=[BulkChoice(opportunity_id=a.id)]))
    with workspace.store.connection() as db:
        assert db.execute("SELECT count(*) FROM addon_tasks").fetchone()[0] == 0


def test_joint_budget_is_enforced_before_any_working_task_is_created(workspace, tmp_path):
    a, b = opportunity(), opportunity("two")
    setup(workspace, tmp_path, a, b)
    workspace.save_settings(workspace.settings().model_copy(update={"budget_copper": 50}))
    identity = workspace.bulk.create(BulkCreate(opportunity_ids=[a.id, b.id]))["batch_id"]
    submit(workspace, identity, book(2589, ((10, 10),)), book(4238, ((100, 10),)))
    with pytest.raises(ValueError, match="Общая закупка"):
        workspace.bulk.publish(identity, BulkSelection(choices=[BulkChoice(opportunity_id=a.id), BulkChoice(opportunity_id=b.id)]))
    with workspace.store.connection() as db:
        assert db.execute("SELECT count(*) FROM addon_tasks").fetchone()[0] == 0


def test_two_hundred_item_report_fits_literal_transport_without_truncation(workspace, tmp_path):
    from cennik.addon.lua_data import read_saved_variables

    values = [opportunity(str(i), "historical_discount", 10000+i) for i in range(200)]
    setup(workspace, tmp_path, *values)
    identity = workspace.bulk.create(BulkCreate(opportunity_ids=[o.id for o in values]))["batch_id"]
    books = [book(o.item_id, tuple((price, 1) for price in range(1, 41))) for o in values]
    _, report = submit(workspace, identity, *books)
    encoded = "CennikAddonDB=" + lua_literal({"bulk_checks": {"reports": [report]}})
    decoded = read_saved_variables(encoded, max_bytes=64_000_000)
    assert len(decoded["bulk_checks"]["reports"][0]["items"]) == 200
    assert workspace.bulk.accept(decoded["bulk_checks"])["accepted"] == 0
    detail = workspace.bulk.detail(identity)
    assert len(detail["items"]) == 200 and all(x["complete"] for x in detail["items"])


def test_bad_identity_time_or_reused_report_id_is_rejected(workspace, tmp_path):
    a = opportunity()
    setup(workspace, tmp_path, a)
    identity = workspace.bulk.create(BulkCreate(opportunity_ids=[a.id]))["batch_id"]
    _, raw = submit(workspace, identity, book(2589), book(4238))
    raw["items"][0]["rows"][0]["unit_price"] += 1
    with pytest.raises(ValueError, match="другим содержимым"):
        workspace.bulk.accept({"reports": [raw]})
    with pytest.raises(ValueError):
        workspace.bulk.load("../escape")
    raw["report_id"] = "other"
    raw["completed_at"] += 10000
    with pytest.raises(ValueError):
        workspace.bulk.accept({"reports": [raw]})


def test_packet_limits_do_not_silently_truncate(workspace, tmp_path):
    values = [opportunity(str(i), item=10000+i) for i in range(200)]
    setup(workspace, tmp_path, *values)
    with pytest.raises(ValueError, match="201 уникальных"):
        workspace.bulk.create(BulkCreate(opportunity_ids=[o.id for o in values]))
    with pytest.raises(ValueError):
        BulkCreate(opportunity_ids=["same", "same"])


def test_reloaded_report_can_pass_literal_parser_and_bridge_without_creating_tasks(workspace, tmp_path):
    a = opportunity()
    _, _, addon, saved = setup(workspace, tmp_path, a)
    identity = workspace.bulk.create(BulkCreate(opportunity_ids=[a.id]))["batch_id"]
    _, report = submit(workspace, identity, book(2589), book(4238))
    saved.parent.mkdir(parents=True)
    saved.write_text("CennikAddonDB=" + lua_literal({"device_id": "bulk-device", "bulk_checks": {"reports": [report]}}), encoding="utf-8")
    result = workspace.synchronize(include_scans=False)
    assert "error" not in result["bulk_reports"]
    text = (addon / "ImportedData.lua").read_text("utf-8")
    assert identity in text and report["report_id"] in text
    assert not workspace.exchange.packages(MARKET)
    # Broken research must not break the regular exchange.
    report["report_id"] = "bad"
    report["items"][0]["item_id"] = 999
    saved.write_text("CennikAddonDB=" + lua_literal({"device_id": "bulk-device", "bulk_checks": {"reports": [report]}}), encoding="utf-8")
    assert "error" in workspace.synchronize(include_scans=False)["bulk_reports"]


def bulk_lua():
    lua = runtime(package())
    stamp = int(datetime.now(UTC).timestamp())
    packet = {"format_version": 1, "batch_id": "a"*32, "workspace_id": "fixture-workspace", "market": MARKET,
              "run_id": "bulk-run", "created_at": stamp, "expires_at": stamp+86400,
              "item_ids": [2589, 4238], "max_rows": 8000, "max_item_rows": 1000, "max_variants": 20}
    lua.execute("Game.now="+str(stamp)+"; GetServerTime=function() return math.floor(Game.now) end")
    lua.execute("Cennik.db.workspace_id='fixture-workspace'; CennikImport.bulk_checks=" + lua_literal({"packet": packet}))
    lua.execute("Cennik.Bulk:Initialize(); Game.commodity[2589]={{quantity=10,unitPrice=10,numOwnerItems=0}}; Game.commodity[4238]={{quantity=5,unitPrice=100,numOwnerItems=0}}")
    return lua


def test_lua_bulk_reads_deduplicated_queries_without_changing_normal_tasks_or_buying():
    lua = bulk_lua()
    before = lua.eval("t.state")
    assert lua.eval("Cennik.Bulk:Start()")
    lua.execute("Game.event('COMMODITY_SEARCH_RESULTS_UPDATED',2589); Game.advance(1.5); Game.event('COMMODITY_SEARCH_RESULTS_UPDATED',4238)")
    assert lua.eval("Cennik.db.bulk_checks.reports[1].status") == "complete"
    assert lua.eval("#Cennik.db.bulk_checks.reports[1].items") == 2
    assert lua.eval("t.state") == before
    assert lua.eval("Cennik.db.pending") is None
    assert lua.eval("#Cennik.db.queue") == 0
    assert lua.eval("function() for _,r in ipairs(Game.calls) do if r.kind~='search' then return false end end return true end")()
    assert lua.eval("Cennik.Bulk:Start()")[0] is False


def test_lua_partial_timeout_and_manual_interference_never_mean_empty_complete_market():
    lua = bulk_lua()
    lua.execute("Cennik.Bulk:Start(); Game.event('COMMODITY_SEARCH_RESULTS_UPDATED',2589); Game.advance(33)")
    assert lua.eval("Cennik.db.bulk_checks.reports[1].status") == "partial"
    assert lua.eval("Cennik.db.bulk_checks.reports[1].items[2].status") == "failed"
    lua = bulk_lua()
    lua.execute("Cennik.Bulk:Start(); C_AuctionHouse.SendSearchQuery(C_AuctionHouse.MakeItemKey(99))")
    assert lua.eval("Cennik.db.bulk_checks.reports[1].status") == "cancelled"
    assert not lua.eval("Cennik.Bulk.active")


def test_lua_bulk_blocks_workflow_queries_until_done_and_preserves_pending_purchase():
    lua = bulk_lua()
    lua.execute("Cennik.db.pending={stage='unknown'}")
    assert lua.eval("Cennik.Bulk:Start()")[0] is False
    lua.execute("Cennik.db.pending=nil; Cennik.Bulk:Start()")
    assert lua.eval("Cennik.Scanner:Start(t)") is False
    assert not lua.eval("Cennik.Adapter:Ready()")
    lua.execute("Game.event('AUCTION_HOUSE_CLOSED')")
    assert lua.eval("Cennik.db.bulk_checks.reports[1].status") == "cancelled"


def test_lua_commodity_pagination_uses_item_id_and_never_accepts_partial_pages():
    lua = bulk_lua()
    lua.execute("""C_AuctionHouse.RequestMoreCommoditySearchResults=function(ref) Game.moreRef=ref end
        Game.full=false; Cennik.Bulk:Start(); Game.event('COMMODITY_SEARCH_RESULTS_UPDATED',2589)
        Game.advance(1.5)""")
    assert lua.eval("Game.moreRef") == 2589
    assert lua.eval("#Cennik.Bulk.report.items") == 0
    lua.execute("Game.full=true; Game.event('COMMODITY_SEARCH_RESULTS_UPDATED',2589); Game.advance(1.5); Game.event('COMMODITY_SEARCH_RESULTS_UPDATED',4238)")
    assert lua.eval("Cennik.db.bulk_checks.reports[1].status") == "complete"


def test_lua_queries_each_equipment_variant_and_keeps_exact_auction_ids():
    lua = bulk_lua()
    lua.execute("""Cennik.db.bulk_checks.packet.item_ids={2589}; Game.gear[2589]=true
        C_AuctionHouse.SendBrowseQuery=function() end
        C_AuctionHouse.HasFullBrowseResults=function() return true end
        C_AuctionHouse.GetBrowseResults=function() return {
          {itemKey={itemID=2589,itemLevel=10,itemSuffix=0,battlePetSpeciesID=0}},
          {itemKey={itemID=2589,itemLevel=10,itemSuffix=1,battlePetSpeciesID=0}}} end
        C_AuctionHouse.GetNumItemSearchResults=function() return 1 end
        C_AuctionHouse.GetItemSearchResultInfo=function(key) return {
          itemKey=key,auctionID=42+key.itemSuffix,quantity=20,buyoutAmount=50,
          containsOwnerItem=false,containsAccountItem=false} end
        Cennik.Bulk:Start(); Game.event('AUCTION_HOUSE_BROWSE_RESULTS_UPDATED'); Game.advance(1.5)
        Game.event('ITEM_SEARCH_RESULTS_UPDATED',{itemID=2589,itemLevel=10,itemSuffix=0,battlePetSpeciesID=0})
        Game.advance(1.5)
        Game.event('ITEM_SEARCH_RESULTS_UPDATED',{itemID=2589,itemLevel=10,itemSuffix=1,battlePetSpeciesID=0})""")
    assert lua.eval("Cennik.db.bulk_checks.reports[1].status") == "complete"
    assert lua.eval("Cennik.db.bulk_checks.reports[1].items[1].variants_count") == 2
    assert lua.eval("#Cennik.db.bulk_checks.reports[1].items[1].rows") == 2
    assert lua.eval("Cennik.db.bulk_checks.reports[1].items[1].rows[1].quantity") == 1


def test_lua_reload_marks_interrupted_packet_and_ack_removes_only_research_report():
    lua = bulk_lua()
    lua.execute("""Cennik.Bulk:Start(); Game.event('COMMODITY_SEARCH_RESULTS_UPDATED',2589)
        Cennik.Bulk.active=false; Cennik.Bulk:Initialize()""")
    assert lua.eval("Cennik.db.bulk_checks.reports[1].status") == "cancelled"
    lua.execute("""CennikImport.bulk_checks.acknowledged_report_ids={Cennik.db.bulk_checks.reports[1].report_id}
        Cennik.Bulk:Initialize()""")
    assert lua.eval("#Cennik.db.bulk_checks.reports") == 0
    assert lua.eval("t.state") == "pending_scan"


def test_disenchant_volume_keeps_original_output_liquidity_limit(workspace, tmp_path):
    a = opportunity(strategy="buy_disenchant")
    a.details["disenchant"]["yields"][0]["liquidity"] = {"enabled": True,
        "conservative_sales_per_day": "100", "thresholds": {"max_batch_sales_percent": "10"}}
    setup(workspace, tmp_path, a)
    identity = workspace.bulk.create(BulkCreate(opportunity_ids=[a.id]))["batch_id"]
    submit(workspace, identity, book(a.item_id, ((10, 100),)), book(10940, ((100, 100),)))
    result = workspace.bulk.detail(identity)["items"][0]
    assert result["available_batches"] == 100 and result["budget_batches"] == 5
    result = workspace.bulk.detail(identity, [BulkChoice(opportunity_id=a.id, batches=6)])["items"][0]
    assert not result["complete"] and any("ликвидности выхода" in x for x in result["problems"])
