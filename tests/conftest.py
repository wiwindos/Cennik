import json

import pytest

from ahledger.config import API, Settings
from ahledger.models import JobSpec
from ahledger.storage import RawArchive, State

MARKET = "forever.pvp.horde.us"


@pytest.fixture
def state(tmp_path):
    value = State(tmp_path / "archive")
    yield value
    value.close()


@pytest.fixture
def settings(state):
    return Settings(data_dir=state.root, concurrency=1, enrich_items="none")


@pytest.fixture
def history():
    return {
        "market": MARKET,
        "item": {"id": 2589, "name": "Linen Cloth"},
        "latest": {
            "observedAt": "2026-10-09T08:00:00Z",
            "minBuyout": 18,
            "median": 20,
            "quantity": 3,
            "source": "Addon scan",
        },
        "range": "7d",
        "sales": {"estimatedPerDay": 0.4, "coveredHours": 5},
        "series": [
            {
                "t": "2026-10-09T07:00:00Z",
                "min": 17,
                "median": 21,
                "p25": 19,
                "p75": 24,
                "quantity": 3,
                "auctions": 1,
            }
        ],
    }


@pytest.fixture
def spec():
    return JobSpec(
        dataset="history",
        url=API + "/v1/prices/" + MARKET + "/2589",
        market=MARKET,
        item_id=2589,
        params={"range": "7d"},
    )


def store_response(state, spec, data, status=200, headers=None):
    from ahledger.parsers import parse

    body = json.dumps(data).encode() if not isinstance(data, bytes) else data
    archive = RawArchive(state)
    state.enqueue(spec)
    state.db.commit()
    meta = archive.save(spec, status, body, headers or {})
    parsed = parse(spec, body)
    meta.update(validation=parsed.status, records=parsed.count)
    archive.write_metadata(meta)
    from ahledger.storage import job_key

    state.finish(job_key(spec), parsed.status, meta["id"], parsed.count)
    state.candidates(parsed.items)
    state.db.commit()
    return meta
