from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from test_addon import exchange as exchange
from test_cennik import analytics_root as analytics_root
from test_market_collector import collector
from test_workspace import scan
from test_workspace import workspace as workspace

from cennik.collector.auctioneers import AuctioneerRegistry, Review
from cennik.collector.client import CollectorClient
from cennik.collector.protocol import Observation
from cennik.core.models import Item, Recipe, RecipeInput
from cennik.workspace.names import GameNames
from cennik.workspace.web import create_app


def baseline():
    return SimpleNamespace(version="english-fixture", items={2589: Item(item_id=2589, name="Linen Cloth", source="item")},
        recipes=[Recipe(spell_id=100, result_item_id=2589, name="Make Cloth", version_hash="test",
                        inputs=[RecipeInput(item_id=1, quantity=1)], qty_min=1, qty_max=1)])


def test_seed_is_idempotent_and_does_not_overwrite_client_language(tmp_path):
    names = GameNames(tmp_path)
    names.put([("item", 2589, "Льняная ткань")], "ruRU", stamp=10)
    names.seed(baseline())
    names.seed(baseline())
    assert names.document()["names"]["item"]["2589"] == {"enUS": "Linen Cloth", "ruRU": "Льняная ткань"}
    names.api({"items": [{"item_id": 2589, "name": "Release cloth"}]}, 20)
    names.seed(baseline())
    assert GameNames(tmp_path).document()["names"]["item"]["2589"]["enUS"] == "Release cloth"
    assert names.put([("item", 2589, "Mixed legacy")], None) == 0
    assert names.put([("item", 2589, "German")], "deDE") == 0


def test_latest_same_name_protects_against_late_rename(tmp_path):
    names = GameNames(tmp_path)
    for stamp, name in ((10, "Cloth"), (30, "Cloth"), (20, "Old name")):
        names.put([("item", 2589, name)], "enGB", stamp=stamp)
    assert names.document()["names"]["item"]["2589"]["enUS"] == "Cloth"
    assert names.document(names.document()["revision"])["unchanged"]


def test_legacy_mixed_names_are_not_assumed_english(tmp_path):
    names = GameNames(tmp_path)
    data = baseline()
    data.items[2147483000] = Item(item_id=2147483000, name="Старый скан", source="local_scan")
    names.seed(data)
    assert "2147483000" not in names.document()["names"]["item"]


def test_random_suffix_is_not_saved_as_base_item_name(tmp_path):
    names = GameNames(tmp_path)
    value = {**scan(), "locale": "ruRU", "variants": [{"name": "Ткань силы", "link": "item:2589:0:0:0:0:0:5:0"}]}
    names.scan(value)
    assert "2589" not in names.document()["names"]["item"]
    value["variants"][0]["base_name"] = "Льняная ткань"
    names.scan(value)
    assert names.document()["names"]["item"]["2589"]["ruRU"] == "Льняная ткань"


def test_addon_captures_locale_and_reagent_names(exchange):
    lua = collector(exchange)
    lua.execute("""
        GetLocale=function() return 'ruRU' end
        Cennik.MarketCollector:Tick(1);Game.event('REPLICATE_ITEM_LIST_UPDATE')
        r=Cennik:RecipeNames({spell_id=100,item_id=2589,inputs={{item_id=2320,quantity=1}}})
    """)
    assert lua.eval("Cennik.db.market_scans[1].locale") == "ruRU"
    assert lua.eval("r.locale") == "ruRU"
    assert lua.eval("r.item_names['2589']~=nil")
    assert lua.eval("r.item_names['2320']~=nil")


@pytest.mark.asyncio
async def test_scan_search_both_languages_and_prices_unchanged(workspace):
    workspace.market.names.seed(baseline())
    value = {**scan(), "locale": "ruRU"}
    stored = workspace.market.import_scan(value)
    generation = workspace.market.generation()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(workspace.root, workspace=workspace)), base_url="http://127.0.0.1") as client:
        for query in ("ЛЬНЯНАЯ", "Linen", "2589"):
            response = await client.get("/api/v2/items", params={"q": query})
            assert response.status_code == 200
            assert response.json()["items"][0]["item_id"] == 2589
        names = (await client.get("/api/v2/game-names")).json()
        assert names["names"]["item"]["2589"]["ruRU"] == "Льняная ткань"
    assert workspace.market.generation() == generation
    assert workspace.market.rows("SELECT buyout FROM working_offers")[0]["buyout"] == 40
    assert stored["complete"]


def test_public_scan_preserves_locale_and_does_not_leak_private_fields():
    value = {**scan(), "locale": "ruRU", "secret": "private", "character": "private"}
    public = CollectorClient.scan_data(value)
    observation = Observation(kind="scan", data=public)
    assert observation.data["locale"] == "ruRU"
    assert "secret" not in observation.data and "character" not in observation.data
    with pytest.raises(ValueError):
        Observation(kind="scan", data={**public, "locale": "made-up"})


def test_registry_keeps_legacy_clients_compatible(tmp_path):
    from auction_fixture import proposal
    registry = AuctioneerRegistry(tmp_path)
    data = proposal().model_copy(update={"locale": "ruRU"}).model_dump(mode="json")
    record = Observation(kind="auctioneer_proposal", data=data)
    registry.suggest(record.identity(), data, "test")
    registry.review(record.identity(), Review(action="approve", auction_type="ordinary"))
    assert registry.proposals()[0]["data"]["locale"] == "ruRU"
    assert "locale" not in registry.wire()["entries"][0]["data"]


def test_recipe_and_npc_localization_without_recipe_model_changes(tmp_path):
    names = GameNames(tmp_path)
    stamp = int(datetime.now(UTC).timestamp())
    recipe = {"spell_id": 100, "item_id": 2589, "name": "Создать ткань", "locale": "ruRU", "observed_at": stamp,
              "item_names": {"2589": "Льняная ткань", "2320": "Грубая нить"}}
    Observation(kind="recipe", data=recipe)
    names.saved({"collector_recipes": {"recipes": {"100": recipe}}, "collector_npc": {"1:2589": {
        "item_id": 2589, "npc_id": 1, "npc_name": "Торговец", "locale": "ruRU", "observed_at": stamp}}})
    document = names.document()["names"]
    assert document["recipe"]["100"]["ruRU"] == "Создать ткань"
    assert document["item"]["2320"]["ruRU"] == "Грубая нить"
    assert document["npc"]["1"]["ruRU"] == "Торговец"
