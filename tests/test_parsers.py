import json
from decimal import Decimal

import pytest

from ahledger.models import JobSpec
from ahledger.parsers import index_classes, parse, parse_ahl1
from ahledger.util import copper, timestamp


def test_valid_history_preserves_unknown_fields(spec, history):
    history["newField"] = {"nested": [1, 2]}
    history["series"][0]["newPointMetric"] = 123
    result = parse(spec, json.dumps(history).encode())
    assert result.count == 1 and result.granularity == "hour"
    assert result.data["newField"]["nested"] == [1, 2]
    assert result.data["series"][0]["newPointMetric"] == 123


def test_empty_history_not_synthetic(spec, history):
    history.update(latest=None, series=[])
    result = parse(spec, json.dumps(history).encode())
    assert result.status == "NO_DATA" and result.minimum is None


@pytest.mark.parametrize("key", ["market", "item", "series", "latest", "range"])
def test_missing_required_history_field(spec, history, key):
    del history[key]
    with pytest.raises((ValueError, KeyError)):
        parse(spec, json.dumps(history).encode())


@pytest.mark.parametrize(
    "key,value",
    [
        ("median", -1),
        ("min", 1.5),
        ("quantity", -1),
        ("auctions", True),
        ("t", "2026-10-09T00:00:00"),
    ],
)
def test_invalid_point_values(spec, history, key, value):
    history["series"][0][key] = value
    with pytest.raises(ValueError):
        parse(spec, json.dumps(history).encode())


def test_gap_and_observed_frequency(spec, history):
    history["series"].append({**history["series"][0], "t": "2026-10-09T10:00:00Z"})
    result = parse(spec, json.dumps(history).encode())
    assert result.status == "PARTIAL"
    assert result.gaps[0]["missing_buckets"] == 2
    assert result.data["_observed_intervals"]["median"] == 10800


def test_duplicate_observations(spec, history):
    history["series"].append(history["series"][0])
    with pytest.raises(ValueError, match="Duplicate"):
        parse(spec, json.dumps(history).encode())


@pytest.mark.parametrize("body", [b"{broken", b"[]", b'{"x":NaN}'])
def test_broken_json(spec, body):
    with pytest.raises(ValueError):
        parse(spec, body)


def test_range_and_market_identity(spec, history):
    history["range"] = "30d"
    with pytest.raises(ValueError, match="range/item"):
        parse(spec, json.dumps(history).encode())
    history["range"] = "7d"
    history["market"] = "forever.normal.horde.us"
    with pytest.raises(ValueError, match="market"):
        parse(spec, json.dumps(history).encode())


def test_ahl1_scaled_sales_and_unknowns():
    result = parse_ahl1(b"AHL1|forever/pvp/horde/us|1791533772|1\n2589:20:18:3:22::18:25:4\n")
    assert result["rows"][0]["salesPerDay"] == Decimal("0.4")
    assert result["rows"][0]["salesPerDayX10"] == 4
    assert result["rows"][0]["median30d"] is None


@pytest.mark.parametrize(
    "body",
    [
        b"AHL2|m|1|0",
        b"AHL1|m|1|2\n1:2:3:4:5:6:7:8:9",
        b"AHL1|m|1|1\n1:2:3",
        b"AHL1|m|1|1\n1:-2:3:4:5:6:7:8:9",
        b"AHL1|m|1|2\n1:2:3:4:5:6:7:8:9\n1:2:3:4:5:6:7:8:9",
    ],
)
def test_bad_price_tables(body):
    with pytest.raises(ValueError):
        parse_ahl1(body)


def test_ahl1_wrong_market():
    with pytest.raises(ValueError, match="market"):
        parse_ahl1(b"AHL1|a/b|1|0", "c.d")


def test_catalog_compact_schema():
    spec = JobSpec(
        dataset="catalog",
        url="https://ahledger.com/api/catalog",
        params={"class": "trade-goods"},
        context={"expected_count": 2, "class_name": "Trade Goods"},
    )
    result = parse(spec, b'{"rows":[[2589,"Linen Cloth","COMMON","Cloth",5,0,"icon","NON_EQUIP"]]}')
    assert result.status == "PARTIAL"
    assert result.items[2589]["itemClass"] == "Trade Goods"
    assert result.items[2589]["itemSubclass"] == "Cloth"
    with pytest.raises(ValueError):
        parse(spec, b'{"rows":[[2589,"Linen Cloth"]]}')


def test_discovery_class_counts():
    classes = index_classes(
        b'<a href="/wow-forever/items/trade-goods"><span>629</span><span>Trade Goods</span><p>desc</p></a>'
    )
    assert classes["trade-goods"] == {"name": "Trade Goods", "count": 629}


def test_pagination_never_silently_complete(spec, history):
    history["pagination"] = {"hasMore": True}
    assert parse(spec, json.dumps(history).encode()).status == "PARTIAL"


def test_server_cap_detected():
    spec = JobSpec(dataset="crafting", url="https://api.ahledger.com/v1/crafting/m")
    data = {
        "marketStatus": "scanned",
        "generatedAt": "2026-10-09T00:00:00Z",
        "filters": {"limit": 1},
        "recipes": [{"recipe": {"spellId": 1}, "result": {"id": 1}, "reagents": []}],
    }
    result = parse(spec, json.dumps(data).encode())
    assert result.status == "PARTIAL" and "limit" in result.warnings[0]


def test_currency_time_decimal_policy(spec, history):
    history["currency"] = "gold"
    with pytest.raises(ValueError, match="currency"):
        parse(spec, json.dumps(history).encode())
    assert timestamp("2026-10-09T05:00:00+05:00") == "2026-10-09T00:00:00+00:00"
    assert copper(Decimal("19.049999999999997"), estimated=True) == 19
    assert copper(Decimal("-3.9"), estimated=True) == -4
    with pytest.raises(ValueError):
        copper(1.5)
