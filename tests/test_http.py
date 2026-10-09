import time

import httpx
import pytest

from ahledger.http import Client, Deferred, cache_expiry, retry_after
from ahledger.storage import RawArchive


def unthrottled(client):
    async def acquire(tool):
        pass

    client.limiter.acquire = acquire


async def test_etag_304_reuses_raw(state, settings, spec, history):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200, json=history, headers={"ETag": '"v1"', "Cache-Control": "max-age=0"}
            )
        assert request.headers["If-None-Match"] == '"v1"'
        return httpx.Response(304, headers={"ETag": '"v1"', "Cache-Control": "max-age=60"})

    archive = RawArchive(state)
    client = Client(settings, state, archive, httpx.MockTransport(handler))
    unthrottled(client)
    first, meta, _ = await client.fetch(spec)
    meta["validation"] = "COMPLETE"
    archive.write_metadata(meta)
    client.validated_cache(spec, meta)
    second, unchanged, _ = await client.fetch(spec)
    assert second == first and unchanged["http_status"] == 304
    assert unchanged["reused_sha256"] == meta["sha256"]
    assert archive.read_sha(unchanged["sha256"]) == b""
    third, _, cached = await client.fetch(spec)
    assert cached and third == first and len(requests) == 2
    await client.close()


async def test_cache_control_freshness(state, settings, spec, history):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, json=history, headers={"Cache-Control": "public, max-age=60", "Age": "10"}
        )

    client = Client(settings, state, RawArchive(state), httpx.MockTransport(handler))
    unthrottled(client)
    body, meta, _ = await client.fetch(spec)
    client.validated_cache(spec, meta)
    assert (await client.fetch(spec))[2]
    assert calls == 1
    await client.close()


@pytest.mark.parametrize("status", [429, 503, 500, 502, 504])
async def test_overload_persists_retry_after(state, settings, spec, status):
    client = Client(
        settings,
        state,
        RawArchive(state),
        httpx.MockTransport(lambda request: httpx.Response(status, headers={"Retry-After": "37"})),
    )
    unthrottled(client)
    with pytest.raises(Deferred) as caught:
        await client.fetch(spec)
    assert caught.value.delay >= 37
    assert client.limiter.multiplier > 1
    assert state.get("backoff_until") > time.time() + 36
    assert RawArchive(state).metadata(caught.value.raw_id)["http_status"] == status
    await client.close()


async def test_network_drop_does_not_expose_proxy_credentials(state, settings, spec):
    def handler(request):
        raise httpx.ConnectError("proxy password: secret", request=request)

    client = Client(settings, state, RawArchive(state), httpx.MockTransport(handler))
    unthrottled(client)
    with pytest.raises(Deferred) as caught:
        await client.fetch(spec)
    assert str(caught.value) == "ConnectError"
    assert "secret" not in str(list(RawArchive(state).manifests()))
    await client.close()


async def test_304_without_archive_fails(state, settings, spec):
    client = Client(
        settings, state, RawArchive(state), httpx.MockTransport(lambda r: httpx.Response(304))
    )
    unthrottled(client)
    with pytest.raises(ValueError, match="304"):
        await client.fetch(spec)
    await client.close()


def test_retry_after_http_date_and_age():
    assert retry_after("Thu, 01 Jan 1970 00:01:00 GMT", now=30) == 30
    assert retry_after("10") == 10
    assert retry_after("bad date") == 0
    assert cache_expiry({"cache-control": "no-store,max-age=900"}) == 0
    assert cache_expiry({"cache-control": "max-age=60", "age": "59"}) <= time.time() + 1.1


async def test_secrets_not_archived(state, settings, spec, history, monkeypatch):
    monkeypatch.setenv("AHL_API_KEY", "secret-key")

    def handler(request):
        assert request.headers["X-API-Key"] == "secret-key"
        return httpx.Response(
            200,
            json=history,
            headers={"Set-Cookie": "secret-cookie", "X-API-Key": "secret-key", "ETag": "ok"},
        )

    client = Client(settings, state, RawArchive(state), httpx.MockTransport(handler))
    unthrottled(client)
    _, meta, _ = await client.fetch(spec)
    assert "secret" not in str(meta)
    await client.close()


async def test_global_rate_spacing_and_separate_tools(state, settings):
    from ahledger.http import RateLimiter

    settings.requests_per_second = 5
    settings.tools_per_second = 1
    limiter = RateLimiter(settings, state)
    await limiter.acquire(False)
    start = time.monotonic()
    await limiter.acquire(False)
    assert time.monotonic() - start >= 0.18
    await limiter.acquire(True)
    start = time.monotonic()
    await limiter.acquire(True)
    assert time.monotonic() - start >= 0.9


async def test_foreign_host_is_never_requested(state, settings, spec):
    client = Client(
        settings,
        state,
        RawArchive(state),
        httpx.MockTransport(lambda r: pytest.fail("network called")),
    )
    bad = spec.model_copy(update={"url": "https://evil.example/v1/items/1"})
    with pytest.raises(ValueError, match="official"):
        await client.fetch(bad)
    await client.close()
