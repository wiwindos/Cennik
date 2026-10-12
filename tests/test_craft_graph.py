"""Invented recipes/markets only. No downloads or modifications of real DuckDB."""
from datetime import timedelta
from decimal import Decimal

import duckdb
import pytest
from fastapi.testclient import TestClient
from test_cennik import HEADERS, KEY, NOW, context, insert, mutate
from test_cennik import analytics_root as analytics_root

from ahledger.config import Settings
from ahledger.web import create_app
from cennik.core.models import AnalyticsConfig, AnalyticsSettings, Recipe, RecipeInput
from cennik.data.duckdb import DuckDBProvider, fingerprint
from cennik.engines.craft_graph import CraftGraph, MaterialOffer, SearchLimit
from cennik.services.analytics import AnalyticsService
from cennik.strategies.craft import SimpleCraftArbitrage
from cennik.strategies.craft_graph import CraftGraphArbitrage


def recipe(spell, output, inputs, qty=1):
    return Recipe(spell_id=spell, version_hash=f"v{spell}", result_item_id=output,
                  qty_min=qty, qty_max=qty, name=f"Recipe {spell}", profession="Mining",
                  inputs=[RecipeInput(item_id=item, quantity=q) for item, q in inputs])


def offer(item, cost, source="auction"):
    return MaterialOffer(item, cost, source)


def test_integer_batches_surplus_and_actual_purchase_cost():
    bar = recipe(1, 2, [(1, 3)], qty=2)
    result = recipe(2, 3, [(2, 3)])
    graph = CraftGraph([bar, result], {1: offer(1, 10), 2: offer(2, 100)})
    plan = graph.plan(result)
    assert plan.cost_copper == 60  # two whole batches, not 45 copper of fractional work
    assert [s.batches for s in plan.steps] == [2, 1]
    assert plan.steps[0].output_quantity == 4 and plan.steps[0].required_quantity == 3
    assert [(p.offer.item_id, p.quantity) for p in plan.purchases] == [(1, 6)]


def test_chooses_purchase_over_costlier_intermediate_and_ignores_sale_price():
    bar, result = recipe(1, 2, [(1, 3)], qty=2), recipe(2, 3, [(2, 3)])
    graph = CraftGraph([bar, result], {1: offer(1, 10), 2: offer(2, 19)})
    plan = graph.plan(result)
    assert plan.cost_copper == 57 and len(plan.steps) == 1


def test_alternative_producers_missing_market_price_and_npc_leaf():
    bad = recipe(1, 2, [(8, 1)])
    expensive = recipe(2, 2, [(1, 10)])
    cheap = recipe(3, 2, [(1, 2)])
    final = recipe(4, 3, [(2, 3)])
    plan = CraftGraph([bad, expensive, cheap, final], {1: offer(1, 5, "npc")}).plan(final)
    assert plan.cost_copper == 30 and [s.recipe.spell_id for s in plan.steps] == [3, 4]
    assert plan.purchases[0].offer.source == "npc"


def test_shared_inputs_are_charged_in_full_without_shared_surplus_optimization():
    bar = recipe(1, 2, [(1, 3)], qty=2)
    thread = recipe(2, 3, [(2, 1)])
    final = recipe(3, 4, [(2, 1), (3, 1)])
    plan = CraftGraph([bar, thread, final], {1: offer(1, 10)}).plan(final)
    assert plan.cost_copper == 60 and sum(p.quantity for p in plan.purchases) == 6
    assert len(plan.steps) == 4


def test_cycles_cannot_manufacture_free_profit():
    a, b, final = recipe(1, 1, [(2, 1)], qty=2), recipe(2, 2, [(1, 1)], qty=2), recipe(3, 3, [(1, 1)])
    graph = CraftGraph([a, b, final], {})
    assert graph.plan(final) is None and graph.cycles_blocked > 0
    # A real purchased leaf can break the cycle; it is still charged in full.
    plan = CraftGraph([a, b, final], {2: offer(2, 7)}).plan(final)
    assert plan.cost_copper == 7 and [s.recipe.spell_id for s in plan.steps] == [1, 3]
    assert CraftGraph([recipe(4, 1, [(1, 1)], qty=2)], {1: offer(1, 1)}).plan(recipe(4, 1, [(1, 1)], qty=2)) is None


def test_depth_batch_and_search_limits_fail_closed():
    a, b, final = recipe(1, 2, [(1, 1)]), recipe(2, 3, [(2, 1)]), recipe(3, 4, [(3, 2)])
    offers = {1: offer(1, 7)}
    assert CraftGraph([a, b, final], offers, max_depth=2).plan(final) is None
    assert CraftGraph([a, b, final], offers, max_depth=3).plan(final).cost_copper == 14
    assert CraftGraph([a, b, final], offers, max_crafts=2).plan(final) is None
    with pytest.raises(SearchLimit):
        CraftGraph([a, b, final], offers, max_nodes=1).plan(final)


def test_latest_version_replaces_profitable_old_one_and_historical_as_of(analytics_root):
    mutate(analytics_root, "UPDATE dataset_metadata SET retrieved_at='2026-10-11T11:30:00Z' WHERE raw_id='recipe-a-v2'")
    path = analytics_root / "analytics/ahledger.duckdb"
    with DuckDBProvider(path, "market-a", NOW) as provider:
        assert [r.version_hash for r in provider.recipes()] == ["v2"]
        assert SimpleCraftArbitrage().evaluate(context(provider)) == []
        assert provider.recipe_selection["superseded_versions"] == 1
    with DuckDBProvider(path, "market-a", NOW - timedelta(minutes=45)) as provider:
        assert [r.version_hash for r in provider.recipes()] == ["v1"]
    with DuckDBProvider(path, "market-b", NOW) as provider:
        assert provider.recipes() == []


def test_equal_time_conflict_and_incomplete_latest_never_fallback(analytics_root):
    mutate(analytics_root, "UPDATE dataset_metadata SET retrieved_at='2026-10-11T11:00:00Z'")
    path = analytics_root / "analytics/ahledger.duckdb"
    with DuckDBProvider(path, "market-a", NOW) as provider:
        assert provider.recipes() == []
        assert provider.recipe_selection["ambiguous"] == 1
    mutate(analytics_root, "UPDATE dataset_metadata SET retrieved_at='2026-10-11T11:30:00Z' WHERE raw_id='recipe-a-v2'")
    mutate(analytics_root, "DELETE FROM recipe_inputs WHERE version_hash='v2' AND item_id=10")
    with DuckDBProvider(path, "market-a", NOW) as provider:
        assert len(provider.recipes()) == 1 and provider.recipes()[0].inputs == []
        assert SimpleCraftArbitrage().evaluate(context(provider)) == []


def test_latest_profession_snapshot_does_not_resurrect_missing_recipe(analytics_root):
    path = analytics_root / "analytics/ahledger.duckdb"
    with duckdb.connect(str(path)) as db:
        db.execute("UPDATE dataset_metadata SET params_json='{" + '"profession":"Tailoring","all":"1"' + "}'")
        insert(db, "dataset_metadata", raw_id="empty-latest", market="market-a", dataset="crafting",
               status="PARTIAL", retrieved_at=NOW, params_json='{"profession":"Tailoring","all":"1"}')
    with DuckDBProvider(path, "market-a", NOW) as provider:
        assert provider.recipes() == [] and provider.recipe_selection["outside_latest_snapshot"] == 2


def test_source_generation_time_beats_later_cached_download_and_excludes_future(analytics_root):
    import json
    path = analytics_root / "analytics/ahledger.duckdb"
    with duckdb.connect(str(path)) as db:
        db.execute("UPDATE dataset_metadata SET payload_json=? WHERE raw_id='recipe-a-v1'",
                   [json.dumps({"response_metadata": {"generatedAt": (NOW - timedelta(hours=3)).isoformat()}})])
        db.execute("UPDATE dataset_metadata SET payload_json=? WHERE raw_id='recipe-a-v2'",
                   [json.dumps({"response_metadata": {"generatedAt": (NOW - timedelta(hours=2)).isoformat()}})])
    with DuckDBProvider(path, "market-a", NOW) as provider:
        assert provider.recipes()[0].version_hash == "v2"
    with duckdb.connect(str(path)) as db:
        db.execute("UPDATE dataset_metadata SET payload_json=? WHERE raw_id='recipe-a-v2'",
                   [json.dumps({"generatedAt": (NOW + timedelta(hours=1)).isoformat()})])
    with DuckDBProvider(path, "market-a", NOW) as provider:
        assert provider.recipes()[0].version_hash == "v1"


def test_market_provenance_survives_shared_mechanics_retrieved_later(analytics_root):
    path = analytics_root / "analytics/ahledger.duckdb"
    with duckdb.connect(str(path)) as db:
        insert(db, "recipe_sources", spell_id=100, version_hash="v1", market="market-a",
               raw_id="recipe-a-v1", retrieved_at=NOW - timedelta(hours=1))
        # The same mechanical hash has a later global row from another market.
        db.execute("UPDATE recipes SET retrieved_at=?,raw_id='future-market-b' WHERE version_hash='v1'", [NOW + timedelta(days=1)])
        db.execute("UPDATE recipe_inputs SET retrieved_at=? WHERE version_hash='v1'", [NOW + timedelta(days=1)])
    with DuckDBProvider(path, "market-a", NOW) as provider:
        assert provider.recipes()[0].version_hash == "v1"
        assert len(provider.recipes()[0].inputs) == 2
        assert provider.recipes()[0].raw_id == "recipe-a-v1"
    with DuckDBProvider(path, "market-b", NOW) as provider:
        assert provider.recipes() == []


def add_intermediate(root):
    import json
    with duckdb.connect(str(root / "analytics/ahledger.duckdb")) as db:
        insert(db, "items", item_id=9, name="Ore", vendor_buy_copper=4, binding="none")
        insert(db, "recipes", spell_id=90, version_hash="bar-v1", name="Smelt", profession="Mining",
               result_item_id=10, qty_min=3, qty_max=4, raw_id="smelt-a",
               payload_json=json.dumps({"reagents": [{"id": 9, "qty": 2}]}))
        insert(db, "recipe_inputs", spell_id=90, version_hash="bar-v1", item_id=9, quantity=2)
        insert(db, "dataset_metadata", raw_id="smelt-a", dataset="crafting", market="market-a", status="COMPLETE")


def test_graph_strategy_full_cost_cut_stale_leaf_supply_and_market(analytics_root):
    add_intermediate(analytics_root)
    # NPC ore makes 3 bars for 8 copper. Final craft needs 2 bars and 5 copper NPC reagent.
    path = analytics_root / "analytics/ahledger.duckdb"
    with DuckDBProvider(path, "market-a", NOW) as provider:
        signal, = CraftGraphArbitrage().evaluate(context(provider))
        assert signal.required_capital_copper == 13 and signal.quantity == 2
        assert signal.potential_profit_copper == 272 and signal.roi_percent == Decimal("2092.31")
        assert signal.details["auction_cut_copper"] == 15
        assert [s["recipe"]["spell_id"] for s in signal.details["steps"]] == [90, 100]
        assert signal.details["steps"][0]["surplus_quantity"] == 1
        assert signal.details["surplus_value_copper"] == 0
        assert all(m["source"] == "npc" for m in signal.details["materials"])
        assert signal.execution_status == "indicative"
    with DuckDBProvider(path, "market-b", NOW) as provider:
        assert CraftGraphArbitrage().evaluate(context(provider)) == []
    mutate(analytics_root, "UPDATE items SET vendor_buy_copper=NULL WHERE item_id=9")
    with DuckDBProvider(path, "market-a", NOW) as provider:
        assert CraftGraphArbitrage().evaluate(context(provider)) == []  # only direct single-step remains
    with duckdb.connect(str(path)) as db:
        insert(db, "price_snapshots", market="market-a", item_id=9, min_copper=4, quantity=1,
               observed_at=NOW - timedelta(hours=2), source="ore-scan")
    with DuckDBProvider(path, "market-a", NOW) as provider:
        signal, = CraftGraphArbitrage().evaluate(context(provider))
        assert "insufficient_observed_material_supply" in signal.risk_flags
        assert signal.data_age_seconds == 7200
    mutate(analytics_root, "UPDATE price_snapshots SET observed_at='2026-09-01' WHERE item_id=9")
    with DuckDBProvider(path, "market-a", NOW) as provider:
        assert CraftGraphArbitrage().evaluate(context(provider)) == []


def test_graph_settings_limits_reject_results_and_validate(analytics_root):
    add_intermediate(analytics_root)
    path = analytics_root / "analytics/ahledger.duckdb"
    with DuckDBProvider(path, "market-a", NOW) as provider:
        ctx = context(provider, AnalyticsConfig(strategy_settings={"craft_graph": {"max_nodes": 1}}))
        assert CraftGraphArbitrage().evaluate(ctx) == []
        assert ctx.diagnostics["craft_graph"]["craft_graph_search_limit"] >= 1
    service = AnalyticsService(analytics_root)
    with pytest.raises(ValueError):
        service.update_settings(AnalyticsSettings(active_market="market-a", config=AnalyticsConfig(
            strategy_settings={"craft_graph": {"max_depth": 11}})))


def test_graph_duckdb_store_api_idempotency_and_source_unchanged(analytics_root):
    add_intermediate(analytics_root)
    before = fingerprint(analytics_root / "analytics/ahledger.duckdb")
    with TestClient(create_app(Settings(data_dir=analytics_root), KEY)) as client:
        saved = client.put("/api/analytics/settings", headers=HEADERS, json={
            "active_market": "market-a", "config": {"enabled_strategies": ["craft_graph"],
            "max_price_age_seconds": 31536000, "strategy_settings": {"craft_graph": {"max_depth": 3}}}})
        assert saved.status_code == 200
        assert saved.json()["config"]["strategy_settings"]["craft_graph"]["max_nodes"] == 2000
        assert client.get("/api/analytics/opportunities?strategy=craft_graph").status_code == 401
        assert client.post("/api/analytics/recalculate", headers=HEADERS).status_code == 202
        client.app.state.analytics.shutdown()
        listing = client.get("/api/analytics/opportunities?strategy=craft_graph", headers=HEADERS).json()
        assert listing["total"] == 1
        detail = client.get("/api/analytics/opportunities/" + listing["items"][0]["id"], headers=HEADERS).json()
        assert len(detail["details"]["steps"]) == 2
        assert client.get("/api/analytics/status", headers=HEADERS).json()["strategy_versions_match"]
        assert client.post("/api/analytics/recalculate", headers=HEADERS).json()["reused"]
        client.put("/api/analytics/settings", headers=HEADERS, json={"active_market": "market-b"})
        assert client.get("/api/analytics/opportunities?strategy=craft_graph", headers=HEADERS).json()["total"] == 0
    assert fingerprint(analytics_root / "analytics/ahledger.duckdb") == before
