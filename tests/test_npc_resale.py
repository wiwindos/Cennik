from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from test_addon_lua import runtime
from test_workspace import workspace as workspace

from cennik.addon.models import ExportRequest, Package
from cennik.addon.service import AddonExchange
from cennik.core.interfaces import EvaluationContext
from cennik.core.models import AnalyticsConfig, Item, PriceObservation, Run
from cennik.engines.economics import auction_net
from cennik.engines.prices import PriceEngine
from cennik.strategies.npc_resale import NpcToAuction
from cennik.workspace.models import MARKET, PlanRequest
from cennik.workspace.npc import NpcReferenceProvider, NpcReferences

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)


class Provider:
    market, as_of = MARKET, NOW

    def __init__(self):
        self.item = Item(item_id=2589, name="Fixture NPC stock", vendor_buy_copper=20, source="fixture NPC")
        self.price = PriceObservation(item_id=2589, market=MARKET, observed_at=NOW-timedelta(minutes=5),
            granularity="scan", source="fixture AH", min_copper=101, median_copper=500,
            quantity=100, sales_estimated_per_day=Decimal(200), sales_observed_at=NOW-timedelta(minutes=5))
        self.history = {}

    def current_prices(self):
        return {2589: self.price} if self.price else {}

    def quality(self):
        return {}

    def items(self):
        return {2589: self.item}

    def sales_history(self, days):
        return self.history


def context(provider=None, **config):
    provider = provider or Provider()
    settings = AnalyticsConfig(**config)
    return EvaluationContext(provider, settings, "npc-fixture", NOW, PriceEngine(provider, settings), provider.items())


def test_npc_to_auction_uses_minimum_sale_price_and_cut():
    result = NpcToAuction().evaluate(context())[0]
    assert result.required_capital_copper == result.buy_price_copper == 20
    assert result.reference_price_copper == 101
    assert result.potential_profit_copper == 76
    assert result.details["auction_cut_copper"] == 5
    assert result.details["materials"][0]["source"] == "npc"
    assert result.details["npc_offer"]["stock"] is None
    assert result.confidence == "limited"
    assert "result_tradeability_unconfirmed" in result.risk_flags


@pytest.mark.parametrize("binding", ["bop", "BIND_ON_PICKUP", "1", "soulbound", "quest", "4"])
def test_known_untradeable_items_are_rejected(binding):
    provider = Provider()
    provider.item.binding = binding
    ctx = context(provider)
    assert NpcToAuction().evaluate(ctx) == []
    assert ctx.diagnostics["npc_to_auction"]["untradeable_result"] == 1


@pytest.mark.parametrize("binding", ["boe", "2", "0", None])
def test_tradeable_or_unconfirmed_binding_can_produce_indicative_signal(binding):
    provider = Provider()
    provider.item.binding = binding
    assert NpcToAuction().evaluate(context(provider))


@pytest.mark.parametrize("value", [None, 0, -1])
def test_missing_or_nonpositive_vendor_buy_price_not_invented(value):
    provider = Provider()
    provider.item.vendor_buy_copper = value
    assert NpcToAuction().evaluate(context(provider)) == []


@pytest.mark.parametrize("problem", ["missing", "stale", "zero", "empty"])
def test_missing_or_unusable_sale_prices_are_rejected(problem):
    provider = Provider()
    if problem == "missing":
        provider.price = None
    elif problem == "stale":
        provider.price.observed_at -= timedelta(days=4)
    elif problem == "zero":
        provider.price.min_copper = 0
    else:
        provider.price.quantity = 0
    assert NpcToAuction().evaluate(context(provider)) == []


def test_unprofitable_after_fee_and_min_profit_threshold():
    provider = Provider()
    provider.item.vendor_buy_copper = 97
    assert NpcToAuction().evaluate(context(provider)) == []
    assert NpcToAuction().evaluate(context(min_profit_copper=77)) == []


def test_liquidity_and_batch_limit_use_auction_demand():
    ctx = context(craft_liquidity_enabled=True, craft_liquidity_source="latest_source")
    result = NpcToAuction().evaluate(ctx)[0]
    assert result.details["max_batch_by_liquidity"] == 20
    assert result.details["liquidity"]["passed"]
    ctx.prices.current[2589].sales_estimated_per_day = None
    assert NpcToAuction().evaluate(ctx) == []


def test_high_supply_and_stale_sales_are_rejected():
    provider = Provider()
    provider.price.quantity = 1000
    assert NpcToAuction().evaluate(context(provider, craft_liquidity_enabled=True, craft_liquidity_source="latest_source")) == []
    provider.price.quantity = 100
    provider.price.sales_observed_at -= timedelta(days=4)
    assert NpcToAuction().evaluate(context(provider, craft_liquidity_enabled=True, craft_liquidity_source="latest_source")) == []


def package():
    ctx = context()
    result = NpcToAuction().evaluate(ctx)[0]
    run = Run(run_id=ctx.run_id, market=MARKET, started_at=NOW, as_of=NOW, generation="npc-fixture",
              config=ctx.config, strategy_versions={"npc_to_auction": "1.0.0"})
    task = AddonExchange.task(result, run, ExportRequest(request_id="npc-resale-test", opportunity_ids=[result.id]), NOW)
    return Package(package_id="npc-package", market=MARKET, run_id=run.run_id, generation=run.generation,
                   max_budget_copper=10000, created_at=NOW, expires_at=task.expires_at, tasks=[task])


def live():
    value = package()
    lua = runtime(value)
    lua.execute("""t.scans[t.item_id]={complete=true,observed_at=Game.now,commodity=true,key={itemID=t.item_id},
        rows={{ownership_known=true,owned=false,quantity=100,unit_price=101}}}""")
    return lua, value


def test_export_uses_npc_lines_and_new_format():
    task = package().tasks[0]
    assert task.format_version == 3
    assert len(task.lines) == 1 and task.lines[0].source == "npc"
    assert task.max_total_cost_copper == 20


def test_live_npc_task_is_not_automatically_completed_or_added_to_ah_queue():
    lua, value = live()
    result = lua.eval("Cennik.Validator:Validate(t)")
    assert result["state"] == "ready_at_npc", result["reason"]
    assert result["profit_copper"] == value.tasks[0].predicted_profit_copper
    lua.execute("t.state='ready_at_npc'")
    assert lua.eval("Cennik.BuyQueue:Add(t)")[0] is False
    assert lua.eval("#Game.calls") == 0
    lua.execute("t.format_version=2")
    assert lua.eval("Cennik:CheckTask(t)")[0] is False


@pytest.mark.parametrize("problem", ["missing", "stale", "scope", "conditions", "stock", "sale_stale", "sale_missing"])
def test_live_checks_reject_unknown_npc_conditions(problem):
    lua, _ = live()
    snippets = {
        "missing": "t.npc_prices={}",
        "stale": "t.npc_prices[t.item_id].observed_at=Game.now-901",
        "scope": "t.npc_prices[t.item_id].scope='another-character'",
        "conditions": "t.conditions_confirmed=false",
        "stock": "t.npc_prices[t.item_id].available=0",
        "sale_stale": "t.scans[t.item_id].observed_at=Game.now-91",
        "sale_missing": "t.scans[t.item_id].rows={}",
    }
    lua.execute(snippets[problem])
    assert lua.eval("Cennik.Validator:Validate(t).state") not in {"ready_at_npc", "completed", "ready_to_buy"}


def test_live_pack_cost_and_stock_account_for_full_npc_purchase_pack():
    lua, _ = live()
    lua.execute("t.opportunity.quantity=2; t.lines[1].quantity=2; Cennik.NPC:Manual(t,t.item_id,60,3); t.npc_prices[t.item_id].available=2")
    assert lua.eval("Cennik.Validator:Validate(t).state") == "insufficient_supply"
    lua.execute("t.npc_prices[t.item_id].available=3; t.max_total_cost_copper=60")
    result = lua.eval("Cennik.Validator:Validate(t)")
    assert result["state"] == "ready_at_npc"
    assert result["cost_copper"] == 60
    assert result["gross_revenue_copper"] == 202  # unrequested extra unit not credited


def test_live_blocks_bop_from_client_even_if_catalog_binding_unknown():
    lua, _ = live()
    lua.execute("""local original=C_Item.GetItemInfo; C_Item.GetItemInfo=function(item)
        local values={original(item)}; values[14]=1; return unpack(values,1,14) end""")
    assert lua.eval("Cennik.Validator:Validate(t).state") == "requires_manual_check"


def test_extended_currency_merchant_offer_cannot_be_used_as_gold_price():
    lua, _ = live()
    lua.execute("""GetNumMerchantItems=function() return 1 end
        GetMerchantItemLink=function() return 'item:2589:0:0:0' end
        GetMerchantItemInfo=function() return 'Fixture',nil,5,1,-1,true,true,true end
        Game.now=Game.now+1; Cennik.NPC:Capture()""")
    assert lua.eval("Cennik.Validator:Validate(t).state") == "requires_manual_check"


def test_npc_party_needs_no_auction_procurement_book(workspace):
    result = NpcToAuction().evaluate(context())[0]
    workspace.store.detail = lambda *args, **kwargs: result
    workspace.analytics.status = lambda: {"result_generation_matches": True}
    settings = workspace.store.settings()
    settings.config.craft_liquidity_enabled = False
    workspace.store.save_settings(settings)
    plan = workspace.plan(PlanRequest(opportunity_id=result.id, batches=7))
    assert plan["complete"] and plan["within_budget"]
    assert plan["cost_copper"] == 140
    assert plan["gross_revenue_copper"] == 707
    assert plan["profit_copper"] == auction_net(707, Decimal(5)) - 140
    assert not plan["npc_stock_confirmed"] and plan["warnings"]
    assert plan["lines"][0]["source"] == "npc"


def test_npc_party_respects_liquidity_limit(workspace):
    result = NpcToAuction().evaluate(context(craft_liquidity_enabled=True, craft_liquidity_source="latest_source"))[0]
    workspace.store.detail = lambda *args, **kwargs: result
    workspace.analytics.status = lambda: {"result_generation_matches": True}
    plan = workspace.plan(PlanRequest(opportunity_id=result.id, batches=21))
    assert not plan["complete"]
    assert any("долю" in problem for problem in plan["problems"])


def captured_quote(**updates):
    return {"batch_price_copper": 101, "batch_quantity": 3, "observed_at": NOW.timestamp()-60,
            "scope": "Fixture/Realm/Alliance", "available": 6, **updates}


def test_captured_prices_are_idempotent_and_change_generation_without_db_writes(workspace):
    before = workspace.market.generation()
    generation = workspace.generation()
    result = workspace.npc_references.ingest({2589: captured_quote()})
    assert result["updated"] == 1
    assert workspace.npc_references.ingest({2589: captured_quote()})["updated"] == 0
    assert workspace.generation() != generation
    assert workspace.market.generation() == before
    assert workspace.npc_references.snapshot()[2589].unit_price_copper == 34
    assert not workspace.market.rows("SELECT * FROM items")


@pytest.mark.parametrize("changes", [{"scope": "Fixture/Realm/Horde"}, {"blocked": True},
                                     {"batch_quantity": 0}, {"batch_price_copper": -1},
                                     {"observed_at": datetime.now(UTC).timestamp()+86400}])
def test_invalid_captured_quotes_do_not_become_prices(tmp_path, changes):
    quotes = NpcReferences(tmp_path)
    assert quotes.ingest({2589: captured_quote(**changes)})["rejected"] == 1
    assert not quotes.snapshot()


def test_observed_pack_price_used_exactly_with_full_pack_liquidity_and_export(tmp_path):
    references = NpcReferences(tmp_path)
    references.ingest({2589: captured_quote()})
    provider = NpcReferenceProvider(Provider(), references.snapshot(), NOW)
    result = NpcToAuction().evaluate(context(provider))[0]
    assert result.quantity == 3
    assert result.required_capital_copper == 101  # not ceil(101/3) * 3 = 102
    assert result.reference_price_copper == 303
    assert result.details["materials"][0]["npc_batch_quantity"] == 3
    assert result.details["npc_offer"]["captured_reference"]["scope"] == "Fixture/Realm/Alliance"
    run = Run(run_id=result.run_id, market=MARKET, started_at=NOW, as_of=NOW, generation="npc-test",
              config=AnalyticsConfig(), strategy_versions={})
    task = AddonExchange.task(result, run, ExportRequest(request_id="npc-pack-test", opportunity_ids=[result.id]), NOW)
    assert task.max_total_cost_copper == 101
    assert task.lines[0].quantity == 3


def test_provider_respects_manual_prices_and_as_of_cutoff(tmp_path):
    references = NpcReferences(tmp_path)
    references.ingest({2589: captured_quote()})
    underlying = Provider()
    underlying.item.source = "manual"
    assert NpcReferenceProvider(underlying, references.snapshot(), NOW).items()[2589].vendor_buy_copper == 20
    underlying.item.source = "fixture"
    assert NpcReferenceProvider(underlying, references.snapshot(), NOW-timedelta(hours=2)).items()[2589].vendor_buy_copper == 20


def test_captured_npc_pack_party_cost_is_exact(workspace):
    workspace.npc_references.ingest({2589: captured_quote()})
    ctx = context(NpcReferenceProvider(Provider(), workspace.npc_references.snapshot(), NOW))
    result = NpcToAuction().evaluate(ctx)[0]
    workspace.store.detail = lambda *args, **kwargs: result
    workspace.analytics.status = lambda: {"result_generation_matches": True}
    settings = workspace.store.settings()
    settings.config.craft_liquidity_enabled = False
    workspace.store.save_settings(settings)
    plan = workspace.plan(PlanRequest(opportunity_id=result.id, batches=2))
    assert plan["complete"] and plan["cost_copper"] == 202
    assert plan["lines"][0]["needed"] == 6
    assert plan["gross_revenue_copper"] == 606


def test_synchronization_imports_npc_saved_variables_to_file_only(workspace, tmp_path):
    from cennik.addon.lua_data import lua_literal

    game = tmp_path / "Game"
    addon = game / "Interface/AddOns/CennikAddon"
    addon.mkdir(parents=True)
    (addon / "CennikAddon.toc").write_text("ImportedData.lua")
    saved = game / "WTF/Account/fixture/SavedVariables/CennikAddon.lua"
    saved.parent.mkdir(parents=True)
    raw = {"device_id": "fixture-device", "events": [], "market_scans": [], "npc_quotes": {2589: captured_quote()}}
    saved.write_text("CennikAddonDB=" + lua_literal(raw), encoding="utf-8")
    workspace.save_settings(workspace.settings().model_copy(update={"game_root": str(game), "account": "fixture"}))
    before = workspace.market.generation()
    result = workspace.synchronize()
    assert result["npc_prices"]["updated"] == 1
    assert workspace.market.generation() == before
    assert not workspace.market.rows("SELECT * FROM items")
    assert workspace.overview()["npc_reference_prices"] == 1
    with workspace.provider(workspace.market.path, MARKET, datetime.now(UTC)) as provider:
        assert provider.items()[2589].vendor_buy_copper == 34


def test_npc_pack_plan_can_be_published_without_auction_purchase_lines(workspace, tmp_path):
    from test_workspace import scan

    from cennik.addon.lua_data import lua_literal

    game = tmp_path / "Game"
    addon = game / "Interface/AddOns/CennikAddon"
    addon.mkdir(parents=True)
    (addon / "CennikAddon.toc").write_text("ImportedData.lua")
    saved = game / "WTF/Account/fixture/SavedVariables/CennikAddon.lua"
    saved.parent.mkdir(parents=True)
    saved.write_text("CennikAddonDB=" + lua_literal({"device_id": "fixture-device", "events": [], "market_scans": []}), encoding="utf-8")
    workspace.save_settings(workspace.settings().model_copy(update={"game_root": str(game), "account": "fixture"}))
    data = scan(count=1)
    data["data"] = "2589:4:404:0:4:1"
    workspace.market.import_scan(data)
    workspace.npc_references.ingest({2589: captured_quote()})
    settings = workspace.store.settings()
    settings.config.enabled_strategies = ["npc_to_auction"]
    settings.config.craft_liquidity_enabled = False
    workspace.analytics.update_settings(settings)
    workspace.calculate()
    value = workspace.store.listing(MARKET)["items"][0]
    plan = workspace.plan(PlanRequest(opportunity_id=value["id"], batches=2))
    published = workspace.publish_plan(plan["id"])
    assert not published["reused"]
    task = workspace.exchange.packages(MARKET)[0].tasks[0]
    assert task.format_version == 3
    assert task.opportunity.quantity == 6
    assert task.max_total_cost_copper == 202
    assert task.lines[0].quantity == 6 and task.lines[0].source == "npc"
