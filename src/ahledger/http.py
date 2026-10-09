from __future__ import annotations

import asyncio
import os
import random
import re
import time
from datetime import UTC
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from .config import API, SITE, TOOLS, Settings
from .models import JobSpec
from .storage import RawArchive, State, job_key


def retry_after(value: str | None, now: float | None = None) -> float:
    if not value:
        return 0
    now = time.time() if now is None else now
    try:
        return max(0, float(value))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return max(0, dt.timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            return 0


def cache_expiry(headers: dict[str, str]) -> float:
    control = headers.get("cache-control", "").lower()
    if "no-store" in control or "no-cache" in control:
        return 0
    match = re.search(r"(?:^|[, ])max-age=(\d+)", control)
    if match:
        now = time.time()
        age = int(headers.get("age", "0"))
        if headers.get("date"):
            try:
                age = max(
                    age, int(max(0, now - parsedate_to_datetime(headers["date"]).timestamp()))
                )
            except (ValueError, TypeError):
                pass
        return now + max(0, int(match[1]) - age)
    if headers.get("expires"):
        return time.time() + retry_after(headers["expires"])
    return 0


class Deferred(Exception):
    def __init__(self, message: str, delay: float, raw_id: str | None = None):
        super().__init__(message)
        self.delay, self.raw_id = delay, raw_id


class RateLimiter:
    def __init__(self, settings: Settings, state: State):
        self.interval = 1 / settings.requests_per_second
        self.tool_interval = 1 / settings.tools_per_second
        self.state = state
        self.next_request = 0.0
        self.next_tool = 0.0
        self.multiplier = float(state.get("rate_multiplier", 1))
        self.lock = asyncio.Lock()

    @property
    def rate(self) -> float:
        return 1 / (self.interval * self.multiplier)

    async def acquire(self, tool: bool):
        async with self.lock:
            while True:
                now = time.monotonic()
                embargo = max(0, float(self.state.get("backoff_until", 0)) - time.time())
                target = max(now + embargo, self.next_request, self.next_tool if tool else 0)
                if target <= now:
                    break
                await asyncio.sleep(target - now)
            current = time.monotonic()
            self.next_request = current + self.interval * self.multiplier
            if tool:
                self.next_tool = current + self.tool_interval * self.multiplier

    def overload(self, delay: float):
        self.multiplier = min(16, self.multiplier * 1.5)
        self.state.set("rate_multiplier", self.multiplier)
        self.state.set(
            "backoff_until", max(float(self.state.get("backoff_until", 0)), time.time() + delay)
        )


class Client:
    def __init__(
        self,
        settings: Settings,
        state: State,
        archive: RawArchive,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings, self.state, self.archive = settings, state, archive
        self.limiter = RateLimiter(settings, state)
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.read_timeout, connect=settings.connect_timeout),
            limits=httpx.Limits(max_connections=settings.concurrency),
            headers={
                "User-Agent": "AHledgerHistorical/1.0 (public archival client)",
                "Accept-Encoding": "identity",
            },
            follow_redirects=False,
            transport=transport,
        )
        self.requests = 0
        self.bytes = 0
        self.hits = 0

    async def close(self):
        await self.client.aclose()

    async def fetch(self, spec: JobSpec, attempt: int = 1) -> tuple[bytes, dict, bool]:
        if urlsplit(spec.url).netloc not in {urlsplit(API).netloc, urlsplit(SITE).netloc}:
            raise ValueError("Only official AHledger hosts are allowed")
        cache = self.state.cache_entry(job_key(spec))
        cached = None
        if cache:
            try:
                cached = self.archive.metadata(cache["raw_id"])
                body = self.archive.read(cached)
            except (OSError, ValueError):
                cache = None
                cached = None
            else:
                if cache["expires"] > time.time():
                    self.hits += 1
                    return body, cached, True
        headers = {}
        if cache:
            if cache["etag"]:
                headers["If-None-Match"] = cache["etag"]
            if cache["last_modified"]:
                headers["If-Modified-Since"] = cache["last_modified"]
        if os.getenv("AHL_API_KEY") and spec.url.startswith(API + "/"):
            header = os.getenv("AHL_API_KEY_HEADER", "X-API-Key")
            if header.lower() not in {"x-api-key", "authorization"}:
                raise ValueError("AHL_API_KEY_HEADER must be X-API-Key or Authorization")
            headers[header] = os.environ["AHL_API_KEY"]
        await self.limiter.acquire(spec.dataset in TOOLS)
        self.requests += 1
        try:
            async with self.client.stream(
                "GET", spec.url, params=spec.params, headers=headers
            ) as response:
                chunks = bytearray()
                preloaded = response.is_stream_consumed
                if preloaded:
                    chunks.extend(response.content)
                else:
                    async for chunk in response.aiter_raw():
                        chunks.extend(chunk)
                        if len(chunks) > self.settings.max_response_bytes:
                            raise ValueError(
                                "Response exceeds configured size limit; job not complete"
                            )
                if len(chunks) > self.settings.max_response_bytes:
                    raise ValueError("Response exceeds configured size limit; job not complete")
                body = bytes(chunks)
                response_headers = dict(response.headers)
                status = response.status_code
        except httpx.HTTPError as exc:
            # Exception strings may contain proxy credentials; log only the exception type.
            meta = self.archive.save(spec, 0, b"", {})
            meta.update(validation="FAILED", error=type(exc).__name__)
            self.archive.write_metadata(meta)
            raise Deferred(
                type(exc).__name__, min(300, 2**attempt) + random.random(), meta["id"]
            ) from exc
        self.bytes += len(body)
        meta = self.archive.save(
            spec,
            status,
            body,
            response_headers,
            reused_sha=(cached.get("reused_sha256") or cached["sha256"])
            if status == 304 and cached
            else None,
        )
        if preloaded:
            meta["body_content_encoding"] = "identity"
        if status == 304 and cached:
            meta["reused_raw_id"] = cached["id"]
        self.archive.write_metadata(meta)
        if status in {429, 503, 500, 502, 504, 408}:
            delay = max(
                retry_after(response_headers.get("retry-after")),
                min(300, 2**attempt) + random.random(),
            )
            self.limiter.overload(delay)
            meta.update(validation="FAILED", error=f"HTTP {status}")
            self.archive.write_metadata(meta)
            raise Deferred(f"HTTP {status}", delay, meta["id"])
        if status == 304:
            if cached is None:
                raise ValueError("304 without an intact archived response")
            combined = {**cached["headers"], **response_headers}
            self.state.cache_response(job_key(spec), cached["id"], combined, cache_expiry(combined))
            return self.archive.read(cached), meta, False
        return self.archive.read(meta), meta, False

    def validated_cache(self, spec: JobSpec, meta: dict):
        if meta["http_status"] == 200 and "no-store" not in meta["headers"].get(
            "cache-control", ""
        ):
            self.state.cache_response(
                job_key(spec), meta["id"], meta["headers"], cache_expiry(meta["headers"])
            )
