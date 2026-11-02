"""Invented markets and recipes; no real saved variables or databases."""
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from test_addon_lua import click, runtime, scan
from test_production import production_setup
from test_workspace import workspace as workspace

from cennik.addon.lua_data import lua_literal
from cennik.core.models import Item, Opportunity, PriceObservation, Recipe, RecipeInput
from cennik.workspace.leveling import LevelingPublish, LevelingQuery, ProfessionSnapshot


def snapshot(character="Crafter", *, difficulty="orange", skill=100, disenchant=True):
    stamp = int(datetime.now(UTC).timestamp())
    return {"format_version": 1, "character": character, "realm": "FixtureRealm", "observed_at": stamp,
            "disenchant": disenchant, "professions": {"tailoring": {"name": "Портняжное дело", "skill": skill,
            "max_skill": 300, "observed_at": stamp, "coverage": "visible_recipes", "recipes": {"100": {
            "spell_id": 100, "name": "Fixture bracers", "item_id": 4239, "qty_min": 1, "qty_max": 1,
            "inputs": [{"item_id": 2589, "quantity": 3}], "difficulty": difficulty, "skill_ups": 1,
            "observed_skill": skill, "observed_at": stamp}}}}}


class Provider:
    def __init__(self, market):
        self.market, self.as_of = market, datetime.now(UTC)
        self.prices = {i: PriceObservation(item_id=i, market=market, observed_at=self.as_of, granularity="scan",
                        min_copper=p, median_copper=p, quantity=1000, source="fixture")
                       for i, p in [(2589, 10), (4239, 50), (10940, 100), (10938, 200), (12345, 100)]}
        self.catalog = {4239: Item(item_id=4239, name="Fixture bracers", vendor_sell_copper=10)}
        self.crafts = [Recipe(spell_id=100, version_hash="fixture", result_item_id=4239, name="Fixture bracers",
                       profession="Tailoring", skill_required=50, qty_min=1, qty_max=1,
                       inputs=[RecipeInput(item_id=2589, quantity=3)])]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

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


@pytest.fixture
def leveling(workspace, tmp_path):
    clients, _, _, _ = production_setup(workspace, tmp_path, create=False)
    provider = Provider(workspace.market_key)
    workspace.provider = lambda *args, **kwargs: provider
    workspace.leveling.ingest(snapshot(), clients[1])
    return workspace, clients, provider


def query(clients, **kwargs):
    return LevelingQuery(profile_id=clients[1].id, profession="tailoring", **kwargs)


def test_compares_exits_cost_per_point_and_gates_disenchant(leveling):
    w, clients, provider = leveling
    result = w.leveling.analyze(query(clients))
    rows = {r["exit"]: r for r in result["variants"]}
    assert set(rows) == {"auction", "npc"}
    assert rows["auction"]["cost_copper"] == 30 and rows["auction"]["net_copper"] == 48
    assert rows["auction"]["profit_copper"] == 18 and rows["auction"]["cost_per_skill_copper"] == "-18"
    assert rows["npc"]["profit_copper"] == -20 and rows["npc"]["cost_per_skill_copper"] == "20"
    result = w.leveling.analyze(query(clients, allow_disenchant=True))
    assert {r["exit"] for r in result["variants"]} == {"auction", "npc", "disenchant"}
    w.leveling.ingest(snapshot(disenchant=False), clients[1])
    result = w.leveling.analyze(query(clients, allow_disenchant=True))
    assert result["disenchant_warning"] and not result["disenchant_enabled"]
    assert all(r["exit"] != "disenchant" for r in result["variants"])


def test_unknown_probabilities_grey_cap_and_manual_skill(leveling):
    w, clients, _ = leveling
    w.leveling.ingest(snapshot(difficulty="yellow"), clients[1])
    result = w.leveling.analyze(query(clients, crafts=2))
    assert all(r["expected_skill_ups"] is None and r["cost_per_skill_copper"] is None for r in result["variants"])
    result = w.leveling.analyze(query(clients, crafts=2, yellow_chance="0.5"))
    assert result["variants"][0]["expected_skill_ups"] == "1.0"
    assert any("задана вами" in warning for warning in result["variants"][0]["warnings"])
    result = w.leveling.analyze(query(clients, skill=101))
    assert all(not r["can_publish"] and r["difficulty"] == "unknown" for r in result["variants"])
    w.leveling.ingest(snapshot(difficulty="grey"), clients[1])
    assert not w.leveling.analyze(query(clients))["variants"]
    w.leveling.ingest(snapshot(skill=300), clients[1])
    assert not w.leveling.analyze(query(clients))["variants"]


def test_capture_is_per_character_and_saved_recipe_works_without_ahl(leveling):
    w, clients, provider = leveling
    with pytest.raises(ValueError, match="другому персонажу"):
        w.leveling.ingest(snapshot("Other"), clients[1])
    provider.crafts = []
    result = w.leveling.analyze(query(clients))
    assert len(result["variants"]) == 2 and all(r["can_publish"] for r in result["variants"])
    broken = snapshot()
    broken["professions"]["tailoring"]["recipes"]["100"]["inputs"][0]["quantity"] = -3
    with pytest.raises(ValidationError):
        ProfessionSnapshot.model_validate(broken)


def test_recipe_purchase_is_once_per_batch_and_compared_with_training(leveling):
    w, clients, _ = leveling
    data = snapshot()
    data["professions"]["tailoring"]["recipes"] = {}
    w.leveling.ingest(data, clients[1])
    no_access = w.leveling.analyze(query(clients))
    assert all(r["cost_copper"] is None and not r["can_publish"] for r in no_access["variants"])
    args = {100: {"item_id": 12345, "trainer_cost_copper": 250, "skill_up_confirmed": True}}
    result = w.leveling.analyze(query(clients, crafts=2, access=args))
    assert all(r["cost_copper"] == 160 and r["can_publish"] for r in result["variants"])
    assert sum(m["quantity"] for m in result["variants"][0]["materials"] if m["item_id"] == 12345) == 1
    args[100]["trainer_cost_copper"] = 50
    result = w.leveling.analyze(query(clients, crafts=2, access=args))
    assert all(r["cost_copper"] == 110 and r["fixed_cost_copper"] == 50 for r in result["variants"])
    assert not any(m["item_id"] == 12345 for m in result["variants"][0]["materials"])


def test_insufficient_supply_missing_price_and_cross_character_de_block(leveling):
    w, clients, provider = leveling
    provider.prices[2589].quantity = 2
    assert all(not r["can_publish"] for r in w.leveling.analyze(query(clients))["variants"])
    del provider.prices[2589]
    assert all(r["cost_copper"] is None for r in w.leveling.analyze(query(clients))["variants"])
    provider.prices = Provider(w.market_key).prices
    w.leveling.ingest(snapshot("FixturePlayer"), clients[0])
    result = w.leveling.analyze(query(clients, allow_disenchant=True, disenchanter_id=clients[0].id))
    de = next(r for r in result["variants"] if r["exit"] == "disenchant")
    assert not de["can_publish"] and any("передача" in b for b in de["blockers"])


def test_publish_loss_floor_idempotency_and_crafter_ownership(leveling):
    w, clients, _ = leveling
    result = w.leveling.analyze(query(clients, allow_disenchant=True))
    request = LevelingPublish(analysis_id=result["analysis_id"], variant_id="100:npc", max_loss_copper=19)
    with pytest.raises(ValueError, match="убыток"):
        w.leveling.publish(request)
    request.max_loss_copper = 20
    published = w.leveling.publish(request)
    assert w.leveling.publish(request)["order_id"] == published["order_id"]
    packages = [p.model_dump(mode="json") for p in w.exchange.packages(w.market_key)]
    assert not w.production.route_packages(clients[0], packages)
    delivered = w.production.route_packages(clients[1], packages)
    assert len(delivered) == 1
    task = delivered[0]["tasks"][0]
    assert task["strategy_id"] == "profession_leveling" and task["format_version"] == 4
    assert task["min_profit_copper"] == -20 and task["predicted_profit_copper"] == -20
    assert task["lines"][0]["quantity"] == 3
    assert w.production.status()[0]["steps"][0]["crafts"] == 1
    with w.store.connection() as db:
        assert db.execute("SELECT count(*) FROM addon_tasks").fetchone()[0] == 1


def test_publish_rejects_changed_character_or_market_and_old_profit_contract_stays(leveling):
    w, clients, provider = leveling
    result = w.leveling.analyze(query(clients))
    provider.prices[2589].min_copper = 11
    request = LevelingPublish(analysis_id=result["analysis_id"], variant_id="100:npc", max_loss_copper=100)
    with pytest.raises(ValueError, match="изменились"):
        w.leveling.publish(request)
    provider.prices[2589].min_copper = 10
    w.leveling.ingest(snapshot(skill=101), clients[1])
    with pytest.raises(ValueError, match="изменились"):
        w.leveling.publish(request)
    from test_bulk import opportunity
    with pytest.raises(ValidationError, match="положительную"):
        Opportunity.model_validate({**opportunity().model_dump(), "potential_profit_copper": -20})


def test_lua_npc_skill_task_checks_actual_vendor_price_and_loss_floor(leveling):
    w, clients, _ = leveling
    result = w.leveling.analyze(query(clients))
    w.leveling.publish(LevelingPublish(analysis_id=result["analysis_id"], variant_id="100:npc", max_loss_copper=20))
    package = w.exchange.packages(w.market_key)[0]
    lua = runtime(package)
    lua.execute("Cennik.db.client_id=" + lua_literal(clients[1].id))
    lua.execute("""
      UnitName=function(unit) return unit=='npc' and 'FixtureAuctioneer' or 'Crafter' end
      Cennik.db.binding.character='Crafter'
      GetTradeSkillLine=function() return 'Tailoring',100,300 end
      GetNumTradeSkills=function() return 1 end
      GetTradeSkillInfo=function() return 'Fixture bracers','optimal' end
      GetTradeSkillRecipeLink=function() return 'enchant:100' end
      GetTradeSkillItemLink=function() return 'item:4239' end
      GetTradeSkillNumMade=function() return 1,1 end
      GetTradeSkillNumReagents=function() return 1 end
      GetTradeSkillReagentInfo=function() return 'Cloth',nil,3 end
      GetTradeSkillReagentItemLink=function() return 'item:2589' end
      Game.vendor={[4239]=10}
    """)
    scan(lua)
    assert lua.eval("#Cennik.Scanner.ids") == 1
    assert lua.eval("t.validation.profit_copper") == -20
    assert lua.eval("t.validation.fee_copper") == 0
    assert lua.eval("t.validation.state") == "ready_to_buy"
    lua.execute("Game.vendor[4239]=9;v=Cennik.Validator:Validate(t)")
    assert lua.eval("v.state") == "not_profitable"
    lua.execute("GetTradeSkillLine=function() return 'Tailoring',101,300 end;v=Cennik.Validator:Validate(t)")
    assert lua.eval("v.state") == "requires_manual_check"
    lua.execute("GetTradeSkillLine=function() return 'Tailoring',100,300 end;Game.vendor[4239]=10;Cennik.BuyQueue:Add(t)")
    scan(lua, prepare=True)
    assert click(lua, "Start")
    lua.execute("Game.event('COMMODITY_PRICE_UPDATED',10,30)")
    assert click(lua, "Confirm")
    lua.execute("Game.event('COMMODITY_PURCHASE_SUCCEEDED')")
    assert lua.eval("t.bought[2589]") == 3 and lua.eval("t.spent") == 30
    assert lua.eval("t.state") == "completed"


def test_real_provider_uses_saved_recipe_and_local_scan(workspace):
    from test_workspace import scan as market_scan

    from cennik.workspace.models import ItemFacts
    data = market_scan()
    data["data"] = "2589:30:300:0:30:1;4239:10:500:0:10:1"
    data["variants"].append({"link": "item:4239", "name": "Fixture bracers", "divisible": True, "vendor_sell": 10})
    assert workspace.market.import_scan(data)["complete"]
    workspace.update_facts(4239, ItemFacts(vendor_sell_copper=10))
    profile = workspace.leveling.ingest(snapshot("FixturePlayer"))
    result = workspace.leveling.analyze(LevelingQuery(profile_id=profile["profile_id"], profession="tailoring"))
    assert len(result["variants"]) == 2
    assert next(r for r in result["variants"] if r["exit"] == "npc")["profit_copper"] == -20


def test_partial_lua_capture_preserves_hidden_recipes_and_reports_current_rank(leveling):
    from test_accounting import accounting_lua, lua_value
    w, _, _ = leveling
    lua = accounting_lua(w)
    lua.execute("""
      GetTradeSkillLine=function() return 'Портняжное дело',100,300 end
      GetNumTradeSkills=function() return 1 end
      GetTradeSkillInfo=function() return 'Fixture cloth','medium' end
      GetTradeSkillNumMade=function() return 1,1 end
      Cennik.Professions:Capture(true)
    """)
    first = lua_value(lua.eval("Cennik.db.production.profession_snapshot"))
    assert first["professions"]["tailoring"]["skill"] == 100
    assert first["professions"]["tailoring"]["recipes"]["100"]["difficulty"] == "yellow"
    lua.execute("GetNumTradeSkills=function() return 0 end;GetTradeSkillLine=function() return 'Портняжное дело',101,300 end;Cennik.Professions:Capture(true)")
    second = ProfessionSnapshot.model_validate(lua_value(lua.eval("Cennik.db.production.profession_snapshot")))
    assert second.professions["tailoring"].skill == 101
    assert second.professions["tailoring"].recipes["100"].observed_skill == 100


def test_development_preview_uses_frozen_clock_and_all_saved_prices(workspace):
    from test_workspace import scan as market_scan
    data = market_scan()
    data["observed_at"] = int((datetime.now(UTC) - timedelta(days=2)).timestamp())
    data["data"] = "2589:30:300:0:30:1;4239:10:500:0:10:1"
    assert workspace.market.import_scan(data)["complete"]
    clock = datetime.fromtimestamp(data["observed_at"] + 60, UTC)
    workspace.development_clock = lambda: clock
    workspace.evaluation_now = lambda: clock
    profile = workspace.leveling.ingest(snapshot("FixturePlayer"))
    result = workspace.leveling.analyze(LevelingQuery(profile_id=profile["profile_id"], profession="tailoring"))
    assert result["variants"]
    assert all(r["cost_copper"] == 30 for r in result["variants"])
