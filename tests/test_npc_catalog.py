
import json
import runpy
from pathlib import Path

import httpx
import pytest
from test_npc_resale import NOW, Provider, captured_quote, context
from test_workspace import workspace as workspace

from cennik.engines.npc_catalog import BUNDLED, NpcPriceCatalog, NpcPriceDocument
from cennik.strategies.npc_resale import NpcToAuction
from cennik.workspace.models import MARKET, PlanRequest
from cennik.workspace.npc import NpcReferenceProvider, NpcReferences
from cennik.workspace.web import create_app


def document(**updates):
    return NpcPriceDocument.model_validate({"version": "fixture-1", "phase": "beta", "build": "1.60.1.70009",
        "source": "Fixture client data; Classic Era fallback", "source_url": "https://auctionhouse.fyi/api/v1/items",
        "retrieved_at": NOW, "raw_sha256": "a" * 64,
        "items": {2589: {"name": "Fixture API cloth", "buy_copper": 30, "sell_copper": 13}}, **updates})


def catalog_provider(doc=None, underlying=None):
    provider = underlying or Provider()
    provider.item.vendor_buy_copper = None
    return NpcReferenceProvider(provider, {}, NOW, doc or document())


def test_api_fallback_is_conditional_with_provenance_and_does_not_leak_into_craft_prices():
    provider = catalog_provider()
    assert provider.items()[2589].vendor_buy_copper is None
    result = NpcToAuction().evaluate(context(provider))[0]
    assert result.required_capital_copper == 30
    assert result.potential_profit_copper == 66
    assert result.details["npc_offer"]["conditional_unit_assumption"]
    assert not result.details["npc_offer"]["purchase_unit_confirmed"]
    assert result.details["npc_offer"]["catalog_reference"]["catalog"]["raw_sha256"] == "a" * 64
    assert {"npc_catalog_vendor_unconfirmed", "npc_catalog_purchase_unit_unconfirmed",
            "npc_catalog_release_refresh_required"} <= set(result.risk_flags)
    assert "Условный кандидат" in result.reason and result.confidence == "limited"
    assert provider.items()[2589].vendor_buy_copper is None


@pytest.mark.parametrize("source", ["manual", "existing DB"])
def test_existing_prices_take_priority_over_catalog(source):
    provider = Provider()
    provider.item.source = source
    wrapped = NpcReferenceProvider(provider, {}, NOW, document())
    result = NpcToAuction().evaluate(context(wrapped))[0]
    assert result.required_capital_copper == 20
    assert result.details["npc_offer"]["catalog_reference"] is None


def test_captured_exact_pack_takes_priority_over_api_and_removes_conditional_flag(tmp_path):
    refs = NpcReferences(tmp_path)
    refs.ingest({2589: captured_quote()})
    provider = NpcReferenceProvider(Provider(), refs.snapshot(), NOW, document())
    result = NpcToAuction().evaluate(context(provider))[0]
    assert result.required_capital_copper == 101 and result.quantity == 3
    assert not result.details["npc_offer"]["conditional_unit_assumption"]
    assert result.details["npc_offer"]["purchase_unit_confirmed"]
    assert not any(x.startswith("npc_catalog_") for x in result.risk_flags)


def test_catalogue_can_supply_a_name_when_price_has_no_item_metadata():
    provider = catalog_provider()
    provider.provider.items = lambda: {}
    result = NpcToAuction().evaluate(context(provider))[0]
    assert result.item_name == "Fixture API cloth"


def test_sell_is_never_invented_as_buy_and_explicit_zero_is_not_overridden():
    doc = document(items={2589: {"name": "Only sell", "sell_copper": 2}})
    assert not NpcToAuction().evaluate(context(catalog_provider(doc)))
    provider = Provider()
    provider.item.vendor_buy_copper = 0
    assert not NpcToAuction().evaluate(context(NpcReferenceProvider(provider, {}, NOW, document())))


def test_known_purchase_quantity_uses_exact_pack_cost():
    doc = document(phase="release", version="fixture-live", items={2589: {
        "name": "Fixture", "buy_copper": 101, "buy_quantity": 3}})
    result = NpcToAuction().evaluate(context(catalog_provider(doc)))[0]
    assert result.required_capital_copper == 101 and result.quantity == 3
    assert result.reference_price_copper == 303
    assert not result.details["npc_offer"]["conditional_unit_assumption"]
    assert "npc_catalog_release_refresh_required" not in result.risk_flags
    assert "npc_catalog_vendor_unconfirmed" in result.risk_flags


def test_refresh_is_versioned_atomic_and_does_not_write_database(workspace):
    before = workspace.market.generation()
    generation = workspace.generation()
    old = workspace.npc_catalog.snapshot()
    new = document(version="fixture-live", phase="release")
    preview = workspace.npc_catalog.preview(new)
    assert preview["requires_recalculation"] and not preview["next"]["requires_release_refresh"]
    assert workspace.generation() == generation  # preview is read only
    workspace.npc_catalog.install(new)
    assert workspace.generation() != generation
    assert workspace.market.generation() == before
    assert not workspace.market.rows("SELECT * FROM items")
    assert (workspace.npc_catalog.directory / "versions" / (old.version + ".json")).exists()
    assert workspace.npc_catalog.snapshot().version == "fixture-live"
    assert not workspace.npc_catalog.preview(new)["requires_recalculation"]
    # Returning to the beta snapshot must restore the release-refresh flag.
    workspace.npc_catalog.install(old)
    assert workspace.npc_catalog.snapshot().metadata()["requires_release_refresh"]


def test_version_cannot_be_reused_for_different_content(tmp_path):
    seed = tmp_path / "bundled.json"
    seed.write_text(document().model_dump_json())
    catalog = NpcPriceCatalog(tmp_path / "refs", seed)
    with pytest.raises(ValueError, match="новый номер"):
        catalog.install(document(source="Different data"))
    with pytest.raises(ValueError):
        document(version="..\\escape")
    assert catalog.snapshot().sha256 == document().sha256


def test_backup_preserves_bundled_catalog_without_creating_profile_override(workspace):
    assert not (workspace.npc_catalog.directory / "active.json").exists()
    backup = Path(workspace.backup()["directory"])
    saved = NpcPriceDocument.model_validate_json((backup / "references/npc_catalog/active.json").read_bytes())
    assert saved.sha256 == workspace.npc_catalog.snapshot().sha256
    assert not (workspace.npc_catalog.directory / "active.json").exists()


def test_catalogue_plan_retains_the_conditional_and_beta_warnings(workspace):
    result = NpcToAuction().evaluate(context(catalog_provider()))[0]
    workspace.store.detail = lambda *args, **kwargs: result
    workspace.analytics.status = lambda: {"result_generation_matches": True}
    settings = workspace.store.settings()
    settings.config.craft_liquidity_enabled = False
    workspace.store.save_settings(settings)
    plan = workspace.plan(PlanRequest(opportunity_id=result.id, batches=2))
    assert plan["cost_copper"] == 60
    assert any("buy принят за цену 1" in x for x in plan["warnings"])
    assert any("релизом" in x for x in plan["warnings"])


def test_catalogue_change_invalidates_calculated_results_and_old_plans(workspace):
    from test_workspace import scan

    data = scan(count=1)
    data["data"] = "2589:4:404:0:4:1"
    workspace.market.import_scan(data)
    settings = workspace.store.settings()
    settings.config.enabled_strategies = ["npc_to_auction"]
    settings.config.craft_liquidity_enabled = False
    workspace.analytics.update_settings(settings)
    workspace.calculate()
    result = workspace.store.listing(MARKET)["items"][0]
    assert result["required_capital_copper"] == 55
    assert workspace.analytics.status()["result_generation_matches"]
    workspace.npc_catalog.install(document(version="next-version", phase="release"))
    assert not workspace.analytics.status()["result_generation_matches"]
    with pytest.raises(RuntimeError, match="Пересчитайте"):
        workspace.plan(PlanRequest(opportunity_id=result["id"]))


def test_bundled_catalog_has_reproducible_counts_and_beta_provenance():
    doc = NpcPriceDocument.model_validate_json(BUNDLED.read_bytes())
    assert len(doc.items) == 10612
    assert doc.metadata()["buy_prices"] == 9566
    assert doc.metadata()["sell_prices"] == 9390
    assert doc.metadata()["unknown_buy_quantity"] == 9566
    assert doc.metadata()["requires_release_refresh"]
    assert doc.items[2589].buy_copper == 55
    assert doc.items[2589].buy_quantity is None
    assert "Classic Era" in doc.source


def test_converter_preserves_raw_units_and_missing_values():
    convert = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/build_npc_catalog.py"))["convert"]
    data = {"v": 1, "build": "1.60.1.70532", "source": "Fixture",
            "items": {"159": {"n": "Water", "buy": 25, "sell": 1, "st": 20},
                      "2320": {"n": "Thread", "sell": 0}}}
    value = convert(json.dumps(data).encode(), version="fixture-new", phase="release", retrieved_at=NOW.isoformat())
    assert value.items[159].buy_copper == 25
    assert value.items[159].buy_quantity is None  # max stack is not the purchase quantity
    assert value.items[2320].buy_copper is None and value.items[2320].sell_copper == 0
    data["items"]["159"]["buy"] = True
    with pytest.raises(ValueError):
        convert(json.dumps(data).encode(), version="invalid", phase="beta", retrieved_at=NOW.isoformat())


@pytest.mark.asyncio
async def test_api_preview_install_and_generation_without_db_changes(tmp_path):
    app = create_app(tmp_path / "api-profile")
    workspace = app.state.workspace
    before = workspace.market.generation()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
                                     base_url="http://127.0.0.1") as client:
            initial = (await client.get("/api/v2/overview")).json()
            value = document(version="fixture-live", phase="release").model_dump(mode="json")
            preview = await client.post("/api/v2/npc/catalog/preview", json=value)
            assert preview.status_code == 200
            assert not preview.json()["next"]["requires_release_refresh"]
            assert (await client.put("/api/v2/npc/catalog", json=value)).status_code == 200
            overview = (await client.get("/api/v2/overview")).json()
            assert overview["generation"] != initial["generation"]
            assert overview["npc_catalog"]["version"] == "fixture-live"
            assert (await client.get("/api/v2/npc/catalog/document")).json()["phase"] == "release"
            value["items"]["2589"]["buy_copper"] = 999
            assert (await client.put("/api/v2/npc/catalog", json=value)).status_code == 422
            assert workspace.market.generation() == before
    finally:
        workspace.close()
