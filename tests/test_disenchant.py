from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_cennik import analytics_root as analytics_root
from test_workspace import workspace as workspace

from cennik.addon.models import ExportRequest
from cennik.addon.service import AddonExchange
from cennik.core.interfaces import EvaluationContext
from cennik.core.models import AnalyticsConfig, Item, PriceObservation, Recipe, RecipeInput, Run
from cennik.engines.disenchant import BUNDLED, DisenchantCatalog, DisenchantRules, expected_gross
from cennik.engines.prices import PriceEngine
from cennik.services.analytics import AnalyticsService
from cennik.strategies.disenchant import BuyDisenchant, CraftDisenchant
from cennik.workspace.models import MARKET, PlanRequest
from cennik.workspace.web import create_app

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


@pytest.fixture
def rules():
    return DisenchantRules.model_validate_json(BUNDLED.read_bytes())


def price(item, value, **kwargs):
    return PriceObservation(item_id=item, market=MARKET, observed_at=NOW - timedelta(minutes=5),
                            granularity="scan", min_copper=value, median_copper=value,
                            quantity=100, source="fixture", **kwargs)


class Provider:
    market, as_of = MARKET, NOW

    def __init__(self):
        self.prices = {4239: price(4239, 50), 10940: price(10940, 100), 10938: price(10938, 200),
                       10: price(10, 30)}
        self.catalog = {4239: Item(item_id=4239, binding="bop"),
                        11: Item(item_id=11, vendor_buy_copper=5)}
        self.crafts = [Recipe(spell_id=100, version_hash="v1", result_item_id=4239, qty_min=2, qty_max=3,
                             inputs=[RecipeInput(item_id=10, quantity=2), RecipeInput(item_id=11, quantity=1)])]

    def current_prices(self):
        return self.prices

    def quality(self):
        return {}

    def items(self):
        return self.catalog

    def recipes(self):
        return self.crafts

    def sales_history(self, days):
        return {}


def context(rules, provider=None, **settings):
    provider = provider or Provider()
    config = AnalyticsConfig(**settings)
    return EvaluationContext(provider, config, "fixture", NOW, PriceEngine(provider, config),
                             provider.items(), disenchant=rules)


def test_bundled_reference_excludes_guesses_and_uses_corrected_dust(rules):
    assert len(rules.items) == 13731
    assert len(rules.exclusions) == 13
    assert not any(rules.yields(i) for i in rules.exclusions)
    bracers = rules.yields(4259)
    dust = next(y for y in bracers if y.material_item_id == 11137)
    assert (dust.min_count, dust.max_count, dust.chance) == (1, 2, Decimal(".75"))


def test_buy_expected_value_fractional_materials_and_cut(rules):
    result = BuyDisenchant().evaluate(context(rules))[0]
    assert result.item_id == 4239 and result.quantity == 1
    assert result.reference_price_copper == 180
    assert result.potential_profit_copper == 121
    assert result.details["net_revenue_copper"] == 171
    assert result.details["disenchant"]["rules"]["sha256"] == rules.sha256
    assert "disenchant_expected_value" in result.risk_flags
    assert result.confidence == "limited"


@pytest.mark.parametrize("problem", ["missing", "stale", "zero", "no_supply"])
def test_any_missing_or_unusable_output_rejects_both_strategies(rules, problem):
    provider = Provider()
    if problem == "missing":
        del provider.prices[10938]
    elif problem == "stale":
        provider.prices[10938].observed_at -= timedelta(days=5)
    elif problem == "zero":
        provider.prices[10938].min_copper = 0
    else:
        provider.prices[10938].quantity = 0
    ctx = context(rules, provider)
    assert BuyDisenchant().evaluate(ctx) == []
    assert CraftDisenchant().evaluate(ctx) == []
    assert ctx.diagnostics["buy_disenchant"]


def test_craft_allows_bop_and_does_not_need_output_auction_price(rules):
    provider = Provider()
    del provider.prices[4239]
    result = CraftDisenchant().evaluate(context(rules, provider))[0]
    assert result.required_capital_copper == 65
    assert result.quantity == 2  # minimum craft output, not the optimistic maximum 3
    assert result.reference_price_copper == 360
    assert result.potential_profit_copper == 277
    assert len(result.details["steps"]) == 1
    assert any(m["source"] == "npc" for m in result.details["materials"])


def test_craft_uses_cheaper_intermediate_path_and_rejects_cycles(rules):
    provider = Provider()
    provider.prices[10].min_copper = 200
    provider.prices[12] = price(12, 10)
    provider.crafts.append(Recipe(spell_id=101, version_hash="i1", result_item_id=10, qty_min=2, qty_max=2,
                                  inputs=[RecipeInput(item_id=12, quantity=1)]))
    result = CraftDisenchant().evaluate(context(rules, provider))[0]
    assert result.required_capital_copper == 15
    assert len(result.details["steps"]) == 2
    provider.crafts[-1].inputs = [RecipeInput(item_id=4239, quantity=1)]
    del provider.prices[4239]
    del provider.prices[10]
    assert CraftDisenchant().evaluate(context(rules, provider)) == []


def test_output_liquidity_is_checked_per_material(rules):
    ctx = context(rules, craft_liquidity_enabled=True, craft_liquidity_source="latest_source")
    assert BuyDisenchant().evaluate(ctx) == []
    assert ctx.diagnostics["buy_disenchant"]["disenchant_material_liquidity_unknown_sales"] == 1


def test_rounding_scales_expected_outputs_before_rounding(rules):
    provider = Provider()
    provider.prices[10940].min_copper = 101
    provider.prices[10938].min_copper = 201
    value = BuyDisenchant().evaluate(context(rules, provider))[0]
    outputs = value.details["disenchant"]["yields"]
    assert expected_gross(outputs) == 181
    assert expected_gross(outputs, 10) == 1815  # not 181 * 10


def test_reference_validation_rejects_incomplete_distribution_and_unknown_pool(rules):
    raw = rules.model_dump(mode="json")
    first = next(iter(raw["pools"]))
    raw["pools"][first][0]["chance"] = "0.01"
    with pytest.raises(ValidationError):
        DisenchantRules.model_validate(raw)
    raw = rules.model_dump(mode="json")
    raw["items"]["4239"]["pool"] = "missing"
    with pytest.raises(ValidationError):
        DisenchantRules.model_validate(raw)


def test_version_update_is_atomic_reversible_and_rejects_reused_versions(tmp_path, rules):
    catalog = DisenchantCatalog(tmp_path)
    updated = rules.model_copy(update={"version": "release-v1"})
    assert catalog.install(updated)["requires_recalculation"]
    assert catalog.snapshot().version == "release-v1"
    assert (tmp_path / "versions" / (rules.version + ".json")).is_file()
    with pytest.raises(ValueError, match="новый номер"):
        catalog.install(updated.model_copy(update={"source": "different"}))
    assert catalog.snapshot().sha256 == updated.sha256
    catalog.install(rules)
    assert catalog.snapshot().sha256 == rules.sha256


def test_rules_change_invalidates_generation_and_cache_without_database_changes(analytics_root, rules):
    service = AnalyticsService(analytics_root)
    before = service.generation()
    database = service.path.read_bytes()
    service.disenchant.install(rules.model_copy(update={"version": "release-test"}))
    assert service.generation() != before
    assert service.path.read_bytes() == database


@pytest.mark.parametrize("strategy", [BuyDisenchant, CraftDisenchant])
@pytest.mark.parametrize("dust_price", [100, 101])
def test_export_and_live_addon_valuation_uses_material_prices(rules, strategy, dust_price):
    provider = Provider()
    provider.prices[10940].min_copper = dust_price
    ctx = context(rules, provider)
    opportunity = strategy().evaluate(ctx)[0]
    run = Run(run_id="fixture", market=MARKET, started_at=NOW, as_of=NOW, generation="fixture",
              config=ctx.config, strategy_versions={})
    task = AddonExchange.task(opportunity, run, ExportRequest(request_id="disenchant-test",
                             opportunity_ids=[opportunity.id]), NOW)
    assert len(task.lines) == (1 if strategy == BuyDisenchant else 2)
    assert task.format_version == 2  # earlier addons accept only v1 tasks and safely ignore this strategy
    from test_addon_lua import runtime

    from cennik.addon.models import Package

    package = Package(package_id="de-test", market=MARKET, run_id=run.run_id, generation=run.generation,
        max_budget_copper=10000, created_at=NOW, expires_at=task.expires_at, tasks=[task])
    lua = runtime(package)
    # Direct full live scans isolate the validator from AH request mocks.
    lua.execute("""t.conditions_confirmed=true; t.scans={}; function setscan(id,unit)
        t.scans[id]={complete=true,observed_at=Game.now,commodity=true,key={itemID=id},rows={
          {item_id=id,quantity=100,available_quantity=100,unit_price=unit,total_price=unit*100,item_key={itemID=id},commodity=true,ownership_known=true,owned=false}}}
    end""")
    for item, unit in [(4239, 50), (10, 30), (10940, dust_price), (10938, 200)]:
        lua.globals().setscan(item, unit)
    if strategy == CraftDisenchant:
        lua.execute("t.scans[t.item_id]=nil")  # no finished gear market quote
    live = lua.eval("Cennik.Validator:Validate(t)")
    assert live["state"] == "ready_to_buy", live["reason"]
    assert live["gross_revenue_copper"] == opportunity.reference_price_copper
    assert live["profit_copper"] == opportunity.potential_profit_copper
    lua.execute("t.scans[10938]=nil")
    assert lua.eval("Cennik.Validator:Validate(t).state") == "pending_scan"
    lua.execute("t.format_version=1")
    assert lua.eval("Cennik:CheckTask(t)")[0] is False


def test_manual_update_api_works_in_development_mode(tmp_path, rules):
    app = create_app(tmp_path / "web")
    workspace = app.state.workspace
    try:
        with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1245)) as client:
            from test_workspace import scan
            workspace.market.import_scan(scan())
            workspace.save_settings(workspace.settings().model_copy(update={"development_mode": True}))
            assert workspace.development_clock() is not None
            document = rules.model_dump(mode="json")
            document["version"] = "release-web-test"
            assert client.post("/api/v2/disenchant/rules/preview", json=document).status_code == 200
            assert client.put("/api/v2/disenchant/rules", json=document).status_code == 200
            assert client.get("/api/v2/disenchant/rules").json()["version"] == "release-web-test"
            assert client.get("/api/v2/disenchant/rules/document").json()["items"]
    finally:
        workspace.close()


def test_ahledger_comparison_does_not_import_prices_or_replace_rules(workspace, monkeypatch):
    import cennik.workspace.service as module
    raw = {"opportunities": [{"item": {"id": 4239, "quality": "UNCOMMON", "level": 13, "itemClass": "Armor"},
                              "yields": [{"id": 10940, "minCount": 1, "maxCount": 2, "chance": .8},
                                         {"id": 10938, "minCount": 1, "maxCount": 2, "chance": .2}]}]}
    real_client = httpx.Client
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=raw, headers={"Cache-Control": "public,max-age=300"})
    monkeypatch.setattr(module.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    generation = workspace.analytics.generation()
    result = workspace.check_disenchant_rules()
    assert result["matches"] == 1 and not result["full_catalog_verified"]
    assert workspace.check_disenchant_rules()["matches"] == 1 and len(calls) == 1
    assert workspace.analytics.generation() == generation
    assert not workspace.market.rows("SELECT * FROM price_snapshots")


def test_batch_plan_uses_reagents_and_expected_output_materials(workspace, rules):
    from test_workspace import scan
    data = scan()
    data["data"] = "4239:4:200:0:4:1"
    data["count"] = 1
    data["variants"] = [{"link": "item:4239:0:0:0", "name": "Gear", "divisible": True, "vendor_sell": 1}]
    workspace.market.import_scan(data)
    value = BuyDisenchant().evaluate(context(rules))[0]
    workspace.store.detail = lambda *args, **kwargs: value
    workspace.analytics.status = lambda: {"result_generation_matches": True}
    settings = workspace.store.settings()
    settings.config.craft_liquidity_enabled = False
    workspace.store.save_settings(settings)
    plan = workspace.plan(PlanRequest(opportunity_id=value.id, batches=2))
    assert plan["complete"]
    assert plan["cost_copper"] == 100
    assert plan["gross_revenue_copper"] == 360
    assert plan["profit_copper"] == 242


def test_backup_contains_exact_rules_even_when_using_bundled_defaults(workspace, rules):
    destination = workspace.backup()["directory"]
    from pathlib import Path
    backed_up = DisenchantRules.model_validate_json((Path(destination) / "references/disenchant/active.json").read_bytes())
    assert backed_up.sha256 == rules.sha256
