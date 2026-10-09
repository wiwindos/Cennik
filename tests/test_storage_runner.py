import asyncio
import os

import httpx
import pytest

from ahledger.runner import Downloader
from ahledger.storage import RawArchive
from ahledger.util import atomic_write, digest, exclusive_lock


def test_job_dedup_and_crash_recovery(state, spec):
    assert state.enqueue(spec)
    assert not state.enqueue(spec)
    state.db.commit()
    state.claim()
    assert state.counts() == {"RUNNING": 1}
    state.recover()
    assert state.counts() == {"PENDING": 1}
    assert state.claim()["attempts"] == 1


def test_raw_hash_dedup_and_header_redaction(state, spec):
    archive = RawArchive(state)
    a = archive.save(spec, 200, b"exact bytes\r\n", {"ETag": "a", "Set-Cookie": "secret"})
    b = archive.save(spec, 200, b"exact bytes\r\n", {})
    assert a["sha256"] == b["sha256"] == digest(b"exact bytes\r\n")
    assert len(list((archive.root / "blobs").glob("*/*.zst"))) == 1
    assert archive.read(a) == b"exact bytes\r\n"
    assert "secret" not in str(a)


def test_atomic_write_survives_replace_failure(tmp_path, monkeypatch):
    path = tmp_path / "blob"
    atomic_write(path, b"old")

    def broken_replace(source, target):
        raise OSError("simulated failure")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        atomic_write(path, b"new")
    assert path.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [path]


def test_concurrent_writer_lock(tmp_path):
    with exclusive_lock(tmp_path):
        with pytest.raises(RuntimeError, match="writer"):
            with exclusive_lock(tmp_path):
                pass


async def test_repeat_run_does_not_refetch_success(state, settings, spec, history):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=history)

    state.enqueue(spec)
    state.db.commit()
    for _ in range(2):
        runner = Downloader(settings, state, transport=httpx.MockTransport(handler))
        runner.expand = False
        await runner.run(max_jobs=1, quiet=True)
    assert calls == 1 and state.counts() == {"COMPLETE": 1}


async def test_cancelled_request_goes_back_to_pending(state, settings, spec):
    async def handler(request):
        await asyncio.sleep(20)
        return httpx.Response(200, json={})

    state.enqueue(spec)
    state.db.commit()
    runner = Downloader(settings, state, transport=httpx.MockTransport(handler))
    runner.expand = False
    await runner.run(max_seconds=0.03, quiet=True)
    assert state.counts() == {"PENDING": 1}


async def test_bad_json_saved_before_validation(state, settings, spec):
    state.enqueue(spec)
    state.db.commit()
    runner = Downloader(
        settings,
        state,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"{broken")),
    )
    runner.expand = False
    await runner.run(max_jobs=1, quiet=True)
    assert state.counts() == {"FAILED": 1}
    meta = next(RawArchive(state).manifests())
    assert RawArchive(state).read(meta) == b"{broken" and meta["validation"] == "FAILED"


async def test_retry_budget_is_persistent(state, settings, spec):
    settings.max_attempts = 1
    state.enqueue(spec)
    state.db.commit()
    runner = Downloader(
        settings, state, transport=httpx.MockTransport(lambda r: httpx.Response(503))
    )
    await runner.run(max_jobs=1, quiet=True)
    assert state.counts() == {"FAILED": 1}
    assert state.db.execute("SELECT attempts FROM jobs").fetchone()[0] == 1


async def test_404_not_available_not_empty_history(state, settings, spec):
    state.enqueue(spec)
    state.db.commit()
    runner = Downloader(
        settings,
        state,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(404, json={"error": "item missing"})
        ),
    )
    await runner.run(max_jobs=1, quiet=True)
    assert state.counts() == {"NOT_AVAILABLE": 1}


async def test_no_scans_market_remains_empty(state, settings, spec, history):
    history.update(latest=None, series=[])
    state.enqueue(spec)
    state.db.commit()
    runner = Downloader(
        settings, state, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=history))
    )
    runner.expand = False
    await runner.run(max_jobs=1, quiet=True)
    assert state.counts() == {"NO_DATA": 1}


async def test_selected_market_skips_existing_other_jobs_and_finishes(
    state, settings, spec, history
):
    from ahledger.models import JobSpec

    selected = spec.market
    other = "forever.normal.alliance.us"
    settings.markets = [selected]
    other_spec = spec.model_copy(
        update={"market": other, "url": spec.url.replace(selected, other), "priority": -100}
    )
    for job in (
        other_spec,
        spec,
        JobSpec(dataset="documentation", url="https://ahledger.com/data", priority=0),
    ):
        state.enqueue(job)
    state.db.commit()
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return (
            httpx.Response(200, json=history)
            if "/prices/" in request.url.path
            else httpx.Response(200, text="Public documentation")
        )

    runner = Downloader(settings, state, transport=httpx.MockTransport(handler))

    async def immediate_acquire(tool):
        pass

    runner.client.limiter.acquire = immediate_acquire
    result = await asyncio.wait_for(runner.run(quiet=True), timeout=1)
    assert result == {"COMPLETE": 2}
    assert len(urls) == 2 and not any(other in url for url in urls)
    assert state.counts() == {"COMPLETE": 2, "PENDING": 1}
    assert state.claim(markets=settings.markets) is None
    assert state.claim()["market"] == other


def test_scoped_claim_uses_market_index_without_sorting_entire_queue(state, spec):
    state.enqueue(spec)
    state.db.commit()
    statements = []
    state.db.set_trace_callback(statements.append)
    assert state.claim(markets=[spec.market])["market"] == spec.market
    state.db.set_trace_callback(None)
    claims = [sql for sql in statements if sql.startswith("SELECT * FROM jobs")]
    assert len(claims) == 2
    for sql in claims:
        explanation = [row[3] for row in state.db.execute("EXPLAIN QUERY PLAN " + sql)]
        assert any("SEARCH jobs USING INDEX queue_market_claim" in detail for detail in explanation)
        assert not any("TEMP B-TREE" in detail for detail in explanation)
