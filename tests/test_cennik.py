"""Small invented fixtures are isolated from real archives and never use the network."""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import duckdb
import pytest
from fastapi.testclient import TestClient

from ahledger.analytics import COMMON, SCHEMAS, build_database
from ahledger.config import Settings
from ahledger.web import create_app
from cennik.core.interfaces import EvaluationContext
from cennik.core.models import AnalyticsConfig, AnalyticsSettings, Model, Run
from cennik.data.duckdb import DuckDBProvider, fingerprint
from cennik.engines.economics import auction_net, roi
from cennik.engines.prices import PriceEngine
from cennik.services.analytics import AnalyticsService
from cennik.strategies import default_registry
from cennik.strategies.craft import SimpleCraftArbitrage
from cennik.strategies.discount import HistoricalDiscount
from cennik.strategies.vendor import VendorArbitrage

NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
KEY = "test-cennik-" + "a" * 40
HEADERS = {"X-API-Key": KEY}


def insert(db, table, **fields):
    fields.setdefault("retrieved_at", NOW - timedelta(hours=1))
    columns = ",".join('"' + c + '"' for c in fields)
    db.execute(f'INSERT INTO "{table}" ({columns}) VALUES ({",".join("?" for _ in fields)})', list(fields.values()))


@pytest.fixture
def analytics_root(tmp_path):
    root = tmp_path / "analytics-fixture"
    path = root / "analytics/ahledger.duckdb"
    path.parent.mkdir(parents=True)
    with duckdb.connect(str(path)) as db:
        for table, columns in SCHEMAS.items():
            fields = ",".join(f'"{name}" {sql}' for name, sql in {**columns, **COMMON}.items())
            db.execute(f'CREATE TABLE "{table}" ({fields})')
        db.execute("CREATE TABLE price_history AS SELECT * FROM price_history_ranges")
        for market in ("market-a", "market-b", "empty-market"):
            insert(db, "markets", market=market, label=market, market_status="scanned")
        for item_id, sell, buy in [(1,100,None),(2,10,None),(3,100,None),(10,None,None),(11,None,5),(20,None,None)]:
            insert(db, "items", item_id=item_id, name=f"Item {item_id}", vendor_sell_copper=sell,
                   vendor_buy_copper=buy, origin_dataset="item")
        for item_id, price in [(1,50),(2,80),(3,50),(10,30),(20,150)]:
            insert(db, "price_snapshots", market="market-a", item_id=item_id,
                   observed_at=NOW - timedelta(days=10) if item_id == 3 else NOW - timedelta(hours=1),
                   min_copper=price, median_copper=price+5, quantity=10, source="fixture scan",
                   sales_estimated_per_day=Decimal("0.5"), raw_id="scan-a", sha256="fixture")
        insert(db, "price_snapshots", market="market-b", item_id=1, observed_at=NOW - timedelta(hours=1),
               min_copper=150, median_copper=155, quantity=4, source="fixture scan")
        for day in range(1,8):
            insert(db, "price_history", market="market-a", item_id=2,
                   observed_at=NOW.replace(hour=0) - timedelta(days=day), granularity="day",
                   min_copper=95, median_copper=100, source="fixture history")
            insert(db, "price_history", market="market-b", item_id=2,
                   observed_at=NOW.replace(hour=0) - timedelta(days=day), granularity="day",
                   min_copper=1, median_copper=2, source="fixture history")
        for version, reagent, quantity in [("v1",10,2),("v2",10,20)]:
            insert(db, "recipes", spell_id=100, version_hash=version, name="Fixture craft", profession="Tailoring",
                   skill_required=100, acquisition="trainer", result_item_id=20, qty_min=2, qty_max=3, raw_id="recipe-a",
                   payload_json=json.dumps({"reagents":[{"id":reagent,"qty":quantity},{"id":11,"qty":1}]}))
            insert(db, "recipe_inputs", spell_id=100, version_hash=version, item_id=reagent, quantity=quantity)
            insert(db, "recipe_inputs", spell_id=100, version_hash=version, item_id=11, quantity=1)
        insert(db, "dataset_metadata", raw_id="recipe-a", dataset="crafting", market="market-a", status="COMPLETE")
    return root


def context(provider, config=None):
    config = config or AnalyticsConfig()
    prices = PriceEngine(provider, config)
    return EvaluationContext(provider, config, "fixture-run", NOW, prices, provider.items())


def mutate(root, sql):
    with duckdb.connect(str(root / "analytics/ahledger.duckdb")) as db:
        db.execute(sql)


def test_vendor_profit_loss_missing_and_stale(analytics_root):
    with DuckDBProvider(analytics_root / "analytics/ahledger.duckdb", "market-a", NOW) as provider:
        ctx = context(provider)
        signals = VendorArbitrage().evaluate(ctx)
        assert len(signals) == 1
        signal = signals[0]
        assert signal.item_id == 1 and signal.potential_profit_copper == 50
        assert signal.roi_percent == 100 and signal.required_capital_copper == 50
        assert signal.details["auction_cut_copper"] == 0
        assert signal.execution_status == "indicative"
        assert signal.data_age_seconds == 3600
        assert ctx.diagnostics["vendor_arbitrage"]["stale_price"] == 1
    with DuckDBProvider(analytics_root / "analytics/ahledger.duckdb", "market-b", NOW) as provider:
        assert VendorArbitrage().evaluate(context(provider)) == []


def test_discount_true_and_threshold(analytics_root):
    with DuckDBProvider(analytics_root / "analytics/ahledger.duckdb", "market-a", NOW) as provider:
        ctx = context(provider)
        signals = HistoricalDiscount().evaluate(ctx)
        assert len(signals) == 1
        signal = signals[0]
        assert signal.item_id == 2 and signal.reference_price_copper == 100
        assert signal.potential_profit_copper == 15 and signal.roi_percent == Decimal("18.75")
        assert signal.details["history"]["day_count"] == 7
        assert signal.details["auction_cut_copper"] == 5
        assert HistoricalDiscount().evaluate(context(provider, AnalyticsConfig(discount_percent=25))) == []


@pytest.mark.parametrize("sql,reason", [
    ("UPDATE price_history SET median_copper=CASE WHEN day(observed_at)%2=0 THEN 20 ELSE 180 END WHERE market='market-a'", "unstable_history"),
    ("DELETE FROM price_history WHERE observed_at<'2026-10-09'", "insufficient_history"),
    ("UPDATE price_snapshots SET min_copper=NULL WHERE item_id=2", "missing_positive_buy_price"),
    ("UPDATE price_snapshots SET quantity=0 WHERE item_id=2", "no_observed_supply"),
    ("DELETE FROM price_history WHERE observed_at>'2026-10-05'", "stale_history"),
    ("UPDATE price_history SET median_copper=65 WHERE observed_at>='2026-10-08' AND market='market-a'", "recent_price_decline"),
])
def test_false_discount_rejected(analytics_root, sql, reason):
    mutate(analytics_root, sql)
    with DuckDBProvider(analytics_root / "analytics/ahledger.duckdb", "market-a", NOW) as provider:
        ctx = context(provider)
        assert HistoricalDiscount().evaluate(ctx) == []
        assert reason in ctx.diagnostics["historical_discount"]


def test_history_resolution_and_future_no_double_count(analytics_root):
    path = analytics_root / "analytics/ahledger.duckdb"
    with duckdb.connect(str(path)) as db:
        for grain, stamp, value in [("hour",NOW-timedelta(days=1,hours=2),100),
                                    ("scan",NOW-timedelta(days=1,hours=2),100),
                                    ("day",NOW.replace(hour=0),99999),
                                    ("scan",NOW+timedelta(days=1),99999)]:
            insert(db, "price_history", market="market-a",item_id=2, observed_at=stamp,
                   granularity=grain,median_copper=value,min_copper=value,source="fixture")
    with DuckDBProvider(path,"market-a",NOW) as provider:
        ref = context(provider).prices.reference(2)
        assert ref.copper == 100 and ref.day_count == 7
        assert ref.sample_count == 7 and ref.granularities == ["day","scan"]
        assert provider.current_prices()[2].min_copper == 80


def test_craft_versions_materials_npc_cut_and_quantity(analytics_root):
    with DuckDBProvider(analytics_root / "analytics/ahledger.duckdb", "market-a", NOW) as provider:
        ctx = context(provider)
        signals = SimpleCraftArbitrage().evaluate(ctx)
        assert len(signals) == 1
        signal = signals[0]
        assert signal.details["version_hash"] == "v1"
        assert signal.required_capital_copper == 65 and signal.quantity == 2
        assert signal.reference_price_copper == 300
        assert signal.potential_profit_copper == 220 and signal.roi_percent == Decimal("338.46")
        assert signal.details["materials"][1]["source"] == "npc"
        assert signal.details["auction_cut_copper"] == 15
        assert signal.details["recipe"]["skill_required"] == 100
        assert ctx.diagnostics["simple_craft"]["not_profitable_after_cut"] == 1
    with DuckDBProvider(analytics_root / "analytics/ahledger.duckdb", "market-b", NOW) as provider:
        assert provider.recipes() == []


@pytest.mark.parametrize("sql", [
    "DELETE FROM price_snapshots WHERE item_id=10",
    "UPDATE items SET vendor_buy_copper=NULL WHERE item_id=11",
    "UPDATE recipe_inputs SET version_hash='wrong' WHERE item_id=10",
    "UPDATE recipes SET qty_min=NULL",
    "UPDATE items SET binding='bop' WHERE item_id=20",
    "UPDATE price_snapshots SET observed_at='2026-09-01' WHERE item_id=10",
])
def test_craft_missing_or_invalid_input_never_zero_cost(analytics_root, sql):
    mutate(analytics_root, sql)
    with DuckDBProvider(analytics_root / "analytics/ahledger.duckdb", "market-a", NOW) as provider:
        # A mismatched version can leave an incomplete subset. Provider must validate
        # that normalized reagents match the archived recipe mechanics, not just join.
        assert SimpleCraftArbitrage().evaluate(context(provider)) == []


def test_integer_commission_and_decimal_roi():
    assert auction_net(433, Decimal("15")) == 369
    assert auction_net(79, Decimal("5")) == 76
    assert roi(1, 3) == Decimal("33.33")


def configured(root, market="market-a", **config):
    service = AnalyticsService(root)
    service.update_settings(AnalyticsSettings(active_market=market, config=AnalyticsConfig(**config)))
    return service


def test_runs_idempotent_read_only_atomic_and_filters(analytics_root):
    service = configured(analytics_root)
    before = fingerprint(service.path)
    run = service.start(background=False,as_of=NOW)["run"]
    assert run["status"] == "completed" and run["signal_count"] == 3
    again = service.start(background=False,as_of=NOW)
    assert again["reused"] and again["run"]["run_id"] == run["run_id"]
    assert fingerprint(service.path) == before
    listing = service.store.listing("market-a",strategy="simple_craft", min_profit=100, min_roi="300",sort="roi")
    assert listing["total"] == 1 and listing["items"][0]["rating"] != "0"
    assert service.store.listing("market-a",confidence="strong")["total"] == 0
    assert service.store.listing("market-a",limit=1,offset=1)["total"] == 3


def test_switch_market_persists_history_and_does_not_mix(analytics_root):
    service = configured(analytics_root)
    a = service.start(background=False,as_of=NOW)["run"]
    a_id = service.store.listing("market-a")["items"][0]["id"]
    service.update_settings(AnalyticsSettings(active_market="market-b"))
    assert service.store.listing("market-b")["items"] == []
    assert service.store.detail("market-b", a_id) is None
    b = service.start(background=False,as_of=NOW)["run"]
    assert b["signal_count"] == 0 and b["market"] == "market-b"
    restarted = AnalyticsService(analytics_root)
    assert restarted.store.settings().active_market == "market-b"
    restarted.update_settings(AnalyticsSettings(active_market="market-a"))
    assert restarted.store.listing("market-a")["run_id"] == a["run_id"]
    assert restarted.start(background=False,as_of=NOW)["reused"]


def test_error_preserves_previous_result_and_no_partial_publication(analytics_root):
    service = configured(analytics_root)
    old = service.start(background=False,as_of=NOW)["run"]

    class Broken:
        id, version = "z_broken", "1"

        def evaluate(self, context):
            assert service.store.listing("market-a")["run_id"] == old["run_id"]
            assert "vendor_arbitrage" in context.diagnostics
            raise ValueError("fixture failure")

    service.registry.register(Broken())
    service.update_settings(AnalyticsSettings(active_market="market-a",config=AnalyticsConfig(enabled_strategies=["vendor_arbitrage","z_broken"])))
    failed = service.start(background=False,as_of=NOW)["run"]
    assert failed["status"] == "failed" and failed["error"] == "fixture failure"
    assert service.store.listing("market-a")["run_id"] == old["run_id"]
    assert service.store.listing("market-a")["total"] == 3


def test_source_generation_change_requires_manual_recalculation(analytics_root):
    service = configured(analytics_root)
    first = service.start(background=False,as_of=NOW)["run"]
    mutate(analytics_root,"UPDATE price_snapshots SET min_copper=40 WHERE market='market-a' AND item_id=1")
    status = service.status()
    assert not status["result_generation_matches"]
    assert service.store.listing("market-a")["run_id"] == first["run_id"]
    assert service.thread is None
    second = service.start(background=False,as_of=NOW)
    assert not second["reused"]
    assert second["run"]["generation"] != first["generation"]
    assert second["run"]["status"] == "completed"
    assert service.store.listing("market-a",strategy="vendor_arbitrage")["items"][0]["potential_profit_copper"] == 60


def test_observation_time_is_not_download_time(analytics_root):
    mutate(analytics_root,"UPDATE price_snapshots SET retrieved_at='2026-10-11T12:00:00Z'")
    with DuckDBProvider(analytics_root / "analytics/ahledger.duckdb","market-a",NOW) as provider:
        signal = VendorArbitrage().evaluate(context(provider))[0]
        assert signal.observed_at == NOW - timedelta(hours=1)
        assert signal.data_age_seconds == 3600


def test_new_strategy_module_and_duplicate_normalization(analytics_root):
    class CustomSettings(Model):
        minimum_profit_copper: int = 1

    class CustomVendor(VendorArbitrage):
        id, version = "custom_vendor", "1"
        settings_model = CustomSettings

        def evaluate(self, context):
            params = self.settings_model.model_validate(context.config.strategy_settings[self.id])
            return [o for o in super().evaluate(context) if o.potential_profit_copper >= params.minimum_profit_copper] * 2

    service = AnalyticsService(analytics_root)
    service.registry.register(CustomVendor())
    service.update_settings(AnalyticsSettings(active_market="market-a",
        config=AnalyticsConfig(enabled_strategies=["custom_vendor"])))
    result = service.start(background=False,as_of=NOW)["run"]
    assert result["status"] == "completed" and result["signal_count"] == 1
    assert service.store.listing("market-a")["items"][0]["strategy_id"] == "custom_vendor"
    service.update_settings(AnalyticsSettings(active_market="market-a",
        config=AnalyticsConfig(enabled_strategies=["custom_vendor"],
                               strategy_settings={"custom_vendor":{"minimum_profit_copper":60}})))
    assert service.start(background=False,as_of=NOW)["run"]["signal_count"] == 0
    with pytest.raises(ValueError):
        service.update_settings(AnalyticsSettings(active_market="market-a",
            config=AnalyticsConfig(strategy_settings={"custom_vendor":{"unknown":1}})))


def test_concurrent_runs_and_settings_blocked_across_instances(analytics_root):
    service = configured(analytics_root)
    entered, release = threading.Event(), threading.Event()

    class Slow:
        id, version = "slow", "1"

        def evaluate(self, context):
            entered.set()
            assert release.wait(5)
            return []

    service.registry.register(Slow())
    service.update_settings(AnalyticsSettings(active_market="market-a",config=AnalyticsConfig(enabled_strategies=["slow"])))
    service.start(as_of=NOW)
    try:
        assert entered.wait(3)
        with pytest.raises(RuntimeError):
            service.start(as_of=NOW)
        with pytest.raises(RuntimeError):
            service.update_settings(AnalyticsSettings(active_market="market-b"))
        second = AnalyticsService(analytics_root)
        assert second.store.latest("market-a").status == "running"
        with pytest.raises(RuntimeError):
            second.start(as_of=NOW)
        assert second.store.listing("market-a")["items"] == []
    finally:
        release.set()
        service.shutdown()


def test_restart_interrupts_without_automatic_resume(analytics_root):
    service = configured(analytics_root)
    run = Run(run_id="interrupted",market="market-a",started_at=NOW,as_of=NOW,
              generation=fingerprint(service.path),config=AnalyticsConfig(),strategy_versions={})
    service.store.begin(run,"interrupted")
    restarted = AnalyticsService(analytics_root)
    assert restarted.thread is None
    assert restarted.store.latest("market-a").status == "interrupted"


def test_api_authorized_manual_pipeline_validation_and_isolation(analytics_root):
    with TestClient(create_app(Settings(data_dir=analytics_root),KEY)) as client:
        for route in ("markets","settings","status","opportunities","opportunities/missing"):
            assert client.get("/api/analytics/"+route).status_code == 401
        assert client.put("/api/analytics/settings",json={}).status_code == 401
        assert client.post("/api/analytics/recalculate").status_code == 401
        assert client.get("/api/analytics/status",headers=HEADERS).json()["latest_run"] is None
        assert client.post("/api/analytics/recalculate",headers=HEADERS).status_code == 422
        assert len(client.get("/api/analytics/markets",headers=HEADERS).json()["markets"]) == 3
        for value in ({"active_market":"no-market"},{"active_market":"market-a","config":{"discount_percent":101}},
                      {"active_market":"market-a","config":{"enabled_strategies":["sql"]}}):
            assert client.put("/api/analytics/settings",json=value,headers=HEADERS).status_code == 422
        assert client.put("/api/analytics/settings",json={"active_market":"market-a","config":{"max_price_age_seconds":31536000}},headers=HEADERS).status_code == 200
        response = client.post("/api/analytics/recalculate",headers=HEADERS)
        assert response.status_code == 202
        client.app.state.analytics.shutdown()
        result = client.get("/api/analytics/opportunities?strategy=vendor_arbitrage&sort=profit",headers=HEADERS).json()
        assert result["total"] == 2
        identity = result["items"][0]["id"]
        detail = client.get("/api/analytics/opportunities/"+identity,headers=HEADERS)
        assert detail.status_code == 200 and detail.json()["execution_status"] == "indicative"
        assert client.get("/api/analytics/opportunities?sort=sql",headers=HEADERS).status_code == 422
        assert client.get("/api/analytics/opportunities?limit=501",headers=HEADERS).status_code == 422
        assert client.post("/api/analytics/recalculate",headers=HEADERS).json()["reused"]
        client.put("/api/analytics/settings",json={"active_market":"market-b"},headers=HEADERS)
        assert client.get("/api/analytics/opportunities",headers=HEADERS).json()["items"] == []
        assert client.get("/api/analytics/opportunities/"+identity,headers=HEADERS).status_code == 404
        assert client.get("/static/analytics.js").status_code == 200


def test_no_data_not_zero_profitable_deals(analytics_root):
    service = configured(analytics_root,"empty-market")
    assert "нет наблюдаемых" in service.status()["error"]
    run = service.start(background=False,as_of=NOW)["run"]
    assert run["status"] == "failed" and "отсутствие данных" in run["error"]
    assert service.store.latest("empty-market",published=True) is None


def test_all_unknown_prices_are_missing_data(analytics_root):
    mutate(analytics_root,"UPDATE price_snapshots SET min_copper=NULL WHERE market='market-a'")
    service = configured(analytics_root)
    run = service.start(background=False,as_of=NOW)["run"]
    assert run["status"] == "failed" and "отсутствие данных" in run["error"]


def test_real_archive_builder_to_cennik_api(state,settings,spec,history):
    from conftest import store_response
    from test_analytics import markets_response

    markets_response(state)
    store_response(state,spec,history)
    build_database(state)
    before = hashlib.sha256((state.root / "analytics/ahledger.duckdb").read_bytes()).hexdigest()
    with TestClient(create_app(settings,KEY)) as client:
        value = {"active_market":spec.market,"config":{"max_price_age_seconds":31536000}}
        assert client.put("/api/analytics/settings",json=value,headers=HEADERS).status_code == 200
        assert client.post("/api/analytics/recalculate",headers=HEADERS).status_code == 202
        client.app.state.analytics.shutdown()
        result = client.get("/api/analytics/status",headers=HEADERS).json()
        assert result["published_run"]["status"] == "completed"
        assert result["published_run"]["diagnostics"]["observed_items"] == 1
        assert result["generation"] == before
    assert fingerprint(state.root / "analytics/ahledger.duckdb") == before


def test_registry_extension_and_duplicate_registration():
    registry = default_registry()
    with pytest.raises(ValueError):
        registry.register(VendorArbitrage())
    with pytest.raises(ValueError):
        registry.selected(["missing"])


def test_settings_validation():
    with pytest.raises(ValueError):
        AnalyticsConfig(history_days=2,min_history_days=4)
    with pytest.raises(ValueError):
        AnalyticsConfig(auction_cut_percent=100)
    with pytest.raises(ValueError):
        AnalyticsConfig(enabled_strategies=["vendor_arbitrage","vendor_arbitrage"])
