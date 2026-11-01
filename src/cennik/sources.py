"""Bounded upstream reads and owner-only credentials shared by server markets."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import Field

from ahledger.util import atomic_json
from cennik.core.models import Model
from cennik.engines.npc_catalog import NpcCatalogItem, NpcPriceDocument
from cennik.markets import valid_market

AHL = "https://api.ahledger.com/v1"
FYI = "https://auctionhouse.fyi/api/v1/items"


class TokenUpdate(Model):
    token: str = Field(default="", max_length=500)
    clear: bool = False


class Sources:
    def __init__(self, root: Path, transport=None):
        self.root = root / "upstreams"
        self.transport = transport
        self.lock = threading.RLock()

    def read(self, name, default):
        path = self.root / name
        return json.loads(path.read_text("utf-8")) if path.exists() else default

    def token(self):
        return self.read("credentials.json", {}).get("ahl_token", os.getenv("AHL_API_KEY", ""))

    def public(self):
        return {"ahl_token_set": bool(self.token())}

    def save_token(self, value: TokenUpdate):
        if value.clear:
            token = ""
        else:
            token = value.token.strip()
            if not token:
                return self.public()
            if any(c.isspace() for c in token) or any(ord(c) < 33 or ord(c) > 126 for c in token):
                raise ValueError("Введите только токен AHledger, без Bearer и пробелов")
        with self.lock:
            atomic_json(self.root / "credentials.json", {"ahl_token": token})
            (self.root / "credentials.json").chmod(0o600)
        return self.public()

    def headers(self, url):
        headers = {"User-Agent": "Cennik/0.3", "Accept-Encoding": "identity"}
        if url.startswith(AHL + "/") and self.token():
            headers["Authorization"] = "Bearer " + self.token()
        return headers

    def fetch(self, url, *, headers=None, max_bytes=32_000_000):
        cooldowns = self.read("cooldowns.json", {})
        if cooldowns.get(url, 0) > time.time():
            raise RuntimeError("Источник запросил паузу; повторите позже")
        with httpx.Client(
            timeout=30, follow_redirects=False, trust_env=False, transport=self.transport
        ) as client:
            with client.stream(
                "GET", url, headers={**self.headers(url), **(headers or {})}
            ) as response:
                if response.status_code == 304:
                    return None, dict(response.headers)
                if response.status_code in {429, 503}:
                    from ahledger.http import retry_after

                    cooldowns[url] = time.time() + max(
                        60, retry_after(response.headers.get("retry-after"))
                    )
                    atomic_json(self.root / "cooldowns.json", cooldowns)
                if response.status_code != 200:
                    raise RuntimeError(f"Источник ответил HTTP {response.status_code}")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise ValueError("Ответ источника превышает допустимый размер")
                return bytes(body), dict(response.headers)

    def markets(self, *, fresh=False):
        with self.lock:
            cached = self.read("markets.json", {})
            if not fresh and cached.get("expires", 0) > time.time():
                return {"items": cached["items"], "cached": True}
            body, _ = self.fetch(AHL + "/markets", max_bytes=1_000_000)
            data = json.loads(body or b"{}")
            values = data.get("markets", [])
            if not isinstance(values, list) or len(values) > 1000:
                raise ValueError("Некорректный каталог рынков AHledger")
            items = []
            for row in values:
                identity = row.get("id")
                if valid_market(identity) and identity.split(".")[-2] in {"alliance", "horde"}:
                    _, ruleset, faction, region = identity.split(".")
                    items.append(
                        {
                            "id": identity,
                            "ruleset": ruleset,
                            "faction": faction,
                            "region": region,
                            "label": str(row.get("label") or identity)[:160],
                        }
                    )
            if not items:
                raise ValueError("AHledger не вернул поддерживаемые рынки")
            atomic_json(self.root / "markets.json", {"items": items, "expires": time.time() + 3600})
            return {"items": items, "cached": False}

    def check_token(self):
        # No market prices or user data are transmitted by this probe.
        return {
            **self.public(),
            "ok": bool(self.markets(fresh=True)["items"]),
            "note": "Запрос принят. Это не подтверждает повышенную квоту API",
        }

    def fyi_document(self, previous: NpcPriceDocument, *, force=False):
        with self.lock:
            state = self.read("fyi.json", {})
            path = self.root / "fyi-document.json"
            if not force and state.get("expires", 0) > time.time() and path.exists():
                cached = NpcPriceDocument.model_validate_json(path.read_bytes())
                return previous if cached.raw_sha256 == previous.raw_sha256 else cached
            headers = (
                {"If-None-Match": state["etag"]} if state.get("etag") and path.exists() else {}
            )
            body, received = self.fetch(FYI, headers=headers)
            if body is None:
                document = NpcPriceDocument.model_validate_json(path.read_bytes())
            else:
                data = json.loads(body)
                if (
                    data.get("v") != 1
                    or not isinstance(data.get("items"), dict)
                    or not data["items"]
                ):
                    raise ValueError("FYI вернул неизвестный формат каталога")
                # A date or a changed ETag never proves that beta data is release data.
                phase = (
                    "release"
                    if data.get("phase") == "release"
                    else previous.phase
                    if data.get("build") == previous.build
                    else "beta"
                )
                items = {}
                for identity, row in data["items"].items():
                    for field in ("buy", "sell"):
                        if row.get(field) is not None and (
                            type(row[field]) is not int or row[field] < 0
                        ):
                            raise ValueError("Некорректная цена NPC в FYI")
                    items[int(identity)] = NpcCatalogItem(
                        name=row["n"],
                        buy_copper=row.get("buy") or None,
                        sell_copper=row.get("sell"),
                    )
                if len(items) < len(previous.items) // 2:
                    raise ValueError(
                        "FYI вернул существенно сокращённый каталог; требуется проверка владельцем"
                    )
                digest = hashlib.sha256(body).hexdigest()
                if digest == previous.raw_sha256 or (
                    items == previous.items
                    and data["build"] == previous.build
                    and data["source"] == previous.source
                    and phase == previous.phase
                ):
                    document = previous
                else:
                    document = NpcPriceDocument(
                        version="ahf-" + data["build"] + "-" + digest[:16],
                        phase=phase,
                        build=data["build"],
                        source=data["source"],
                        source_url="https://auctionhouse.fyi/api/v1/items",
                        retrieved_at=datetime.now(UTC),
                        raw_sha256=digest,
                        items=items,
                    )
                atomic_json(path, document.model_dump(mode="json"))
            atomic_json(
                self.root / "fyi.json",
                {"etag": received.get("etag", state.get("etag")), "expires": time.time() + 21600},
            )
            return document
