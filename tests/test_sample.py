import hashlib
import time

import httpx
import pytest
from conftest import store_response

from ahledger.config import API
from ahledger.models import JobSpec
from ahledger.runner import Downloader
from ahledger.storage import RawArchive, State
from tools.prepare_sample import fetch_jobs, import_saved


def test_sample_import_excludes_other_market_and_leaves_source_unchanged(
    state, spec, history, tmp_path
):
    first = store_response(state, spec, history)
    other = spec.model_copy(
        update={
            "market": "forever.normal.alliance.us",
            "url": API + "/v1/prices/forever.normal.alliance.us/2589",
        }
    )
    store_response(state, other, history | {"market": other.market})
    files = list((state.root / "raw").rglob("*"))
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file()}
    source_jobs = [tuple(r) for r in state.db.execute("SELECT * FROM jobs ORDER BY id")]
    target = State(tmp_path / "sample")
    try:
        assert import_saved(state.root, target, spec.market) == 1
        assert [m["id"] for m in RawArchive(target).manifests()] == [first["id"]]
        assert target.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
        assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before}
        assert source_jobs == [tuple(r) for r in state.db.execute("SELECT * FROM jobs ORDER BY id")]
    finally:
        target.close()


@pytest.mark.asyncio
async def test_sample_fetch_never_expands_catalog_into_full_history(state, settings):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"rows": [[2589, "Linen Cloth", "COMMON", "cloth", 5, 0, "cloth", 0]]}
        )
    )
    downloader = Downloader(settings, state, transport=transport)
    downloader.expand = False
    spec = JobSpec(
        dataset="catalog", url="https://ahledger.com/api/catalog", params={"class": "trade-goods"}
    )
    try:
        await fetch_jobs(downloader, [spec], time.monotonic() + 5)
        assert state.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
        assert state.db.execute("SELECT count(*) FROM item_candidates").fetchone()[0] == 1
        assert downloader.client.requests == 1
    finally:
        await downloader.client.close()


@pytest.mark.asyncio
async def test_sample_expired_budget_does_not_send_requests(state, settings, spec):
    downloader = Downloader(
        settings,
        state,
        transport=httpx.MockTransport(
            lambda request: pytest.fail("Budget expired; request must not be sent")
        ),
    )
    downloader.expand = False
    try:
        await fetch_jobs(downloader, [spec], time.monotonic() - 1)
        assert downloader.client.requests == 0
        assert state.counts() == {"PENDING": 1}
    finally:
        await downloader.client.close()
