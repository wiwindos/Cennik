from datetime import UTC, datetime

import httpx
import pytest
from test_bulk import book, opportunity, setup, submit
from test_workspace import scan
from test_workspace import workspace as workspace

from cennik.core.models import Item
from cennik.engines.prices import PriceEngine
from cennik.workspace.bulk import BulkChoice, BulkCreate, BulkSelection
from cennik.workspace.models import PlanRequest
from cennik.workspace.web import create_app


def unknown_opportunity(strategy="simple_craft"):
    value = opportunity(strategy=strategy)
    evidence = {"enabled": True, "status": "unknown", "passed": None,
                "conservative_sales_per_day": "200", "thresholds": {"max_batch_sales_percent": "10"}}
    value.risk_flags.append("liquidity_unknown")
    value.details["liquidity"] = evidence
    if "disenchant" in strategy:
        value.details["disenchant"]["yields"][0]["liquidity"] = evidence
    return value


@pytest.mark.parametrize("strategy", ["simple_craft", "craft_graph", "buy_disenchant", "craft_disenchant", "npc_to_auction"])
def test_unknown_demand_bulk_checks_publish_through_normal_addon_pipeline(workspace, tmp_path, strategy):
    value = unknown_opportunity(strategy)
    setup(workspace, tmp_path, value)
    workspace.effective_config = lambda: workspace.store.settings().config.model_copy(update={"craft_liquidity_enabled": True})
    identity = workspace.bulk.create(BulkCreate(opportunity_ids=[value.id]))["batch_id"]
    books = [book(i, ((100 if i in (10940, 4238) and strategy != "buy_disenchant" else 10, 100),))
             for i in workspace.bulk.load(identity)["item_ids"]]
    if strategy == "buy_disenchant":
        books = [book(i, ((100 if i == 10940 else 10, 100),)) for i in workspace.bulk.load(identity)["item_ids"]]
    submit(workspace, identity, *books)
    result = workspace.bulk.detail(identity, [BulkChoice(opportunity_id=value.id, batches=3)])["items"][0]
    assert result["complete"] and result["profit_copper"] > 0
    assert result["budget_batches"] >= 3
    assert any("Спрос" in warning for warning in result["warnings"])
    assert workspace.bulk.liquidity_limit(value) is None
    selection = BulkSelection(choices=[BulkChoice(opportunity_id=value.id, batches=3)])
    workspace.bulk.publish(identity, selection)
    with workspace.store.connection() as db:
        assert db.execute("SELECT count(*) FROM addon_tasks").fetchone()[0] == 1


def test_strict_mode_still_blocks_unknown_bulk_demand(workspace, tmp_path):
    value = unknown_opportunity()
    setup(workspace, tmp_path, value)
    workspace.effective_config = lambda: workspace.store.settings().config.model_copy(
        update={"craft_liquidity_enabled": True, "craft_allow_unknown_sales": False})
    identity = workspace.bulk.create(BulkCreate(opportunity_ids=[value.id]))["batch_id"]
    submit(workspace, identity, book(2589), book(4238, ((100, 10),)))
    assert not workspace.bulk.detail(identity)["items"][0]["complete"]
    with pytest.raises(ValueError):
        workspace.bulk.publish(identity, BulkSelection(choices=[BulkChoice(opportunity_id=value.id)]))


def test_unknown_demand_plan_retains_real_supply_and_budget_checks(workspace, tmp_path):
    workspace.market.import_scan(scan())
    value = unknown_opportunity()
    setup(workspace, tmp_path, value)
    workspace.effective_config = lambda: workspace.store.settings().config.model_copy(update={"craft_liquidity_enabled": True})
    result = workspace.plan(PlanRequest(opportunity_id=value.id, batches=2))
    assert result["complete"] and result["within_budget"] and result["profit_copper"] > 0
    assert any("Спрос" in warning for warning in result["warnings"])
    too_large = workspace.plan(PlanRequest(opportunity_id=value.id, batches=3))
    assert not too_large["complete"] and too_large["profit_copper"] is None
    workspace.effective_config = lambda: workspace.store.settings().config.model_copy(
        update={"craft_liquidity_enabled": True, "craft_allow_unknown_sales": False})
    assert not workspace.plan(PlanRequest(opportunity_id=value.id))["complete"]


def test_price_gap_report_deduplicates_and_does_not_invent_prices():
    from test_npc_resale import context

    engine: PriceEngine = context().prices
    for strategy, purpose in (("simple_craft", "material"), ("craft_graph", "sale"), ("craft_graph", "sale")):
        assert engine.usable(None, item_id=123, strategy=strategy, purpose=purpose)
    report = engine.gap_report({123: Item(item_id=123, name="Missing material")})
    assert report["total"] == 1 and len(report["items"][0]["uses"]) == 2
    assert 123 not in engine.current
    assert report["items"][0]["name"] == "Missing material"


@pytest.mark.asyncio
async def test_zero_settings_and_price_gap_endpoint(workspace, tmp_path):
    run, _, _, _ = setup(workspace, tmp_path, opportunity())
    run.diagnostics["price_gaps"] = {"items": [
        {"item_id": 2589, "name": "Linen Cloth", "reasons": ["stale_price"], "uses": []},
        {"item_id": 123, "name": "Missing material", "reasons": ["missing_positive_buy_price"], "uses": []},
    ], "total": 2, "truncated": False}
    workspace.market.names.put([("item", 2589, "Льняная ткань")], "ruRU", stamp=datetime.now(UTC).timestamp())
    app = create_app(workspace.root, workspace=workspace)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://127.0.0.1") as client:
        settings = (await client.get("/api/v2/analytics/settings")).json()
        settings["config"].update(craft_min_sales_per_day=0, craft_sales_window_days=0, craft_min_sales_days=0,
                                  craft_allow_unknown_sales=True)
        response = await client.put("/api/v2/analytics/settings", json=settings)
        assert response.status_code == 200, response.text
        for query in ("2589", "ткань", "Cloth"):
            result = (await client.get("/api/v2/price-gaps", params={"q": query})).json()
            assert result["total"] == 1 and result["items"][0]["item_id"] == 2589
        result = (await client.get("/api/v2/price-gaps", params={"limit": 1, "offset": 1})).json()
        assert result["total"] == 2 and result["items"][0]["item_id"] == 123


def test_price_gaps_stay_in_full_run_but_out_of_polling_summary(workspace):
    from cennik.core.models import Run

    now = datetime.now(UTC)
    run = Run(run_id="price-gap-fixture", market=workspace.market_key, started_at=now, as_of=now,
              generation="fixture", config=workspace.store.settings().config, strategy_versions={})
    run.diagnostics = {"price_gap_count": 1, "price_gaps": {"items": [{"item_id": 123}], "total": 1}}
    workspace.store.begin(run, "gap-fixture")
    run.status = "completed"
    workspace.store.finish(run, [])
    assert workspace.store.latest(workspace.market_key).diagnostics["price_gaps"]["items"]
    for backfill in (False, True):
        if backfill:
            with workspace.store.connection() as db:
                db.execute("UPDATE runs SET summary=NULL WHERE run_id=?", [run.run_id])
        summary = workspace.store.latest(workspace.market_key, summary=True)
        assert summary.diagnostics["price_gap_count"] == 1
        assert "price_gaps" not in summary.diagnostics
