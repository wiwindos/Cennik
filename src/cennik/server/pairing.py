"""Short-lived local app pairing; only an authenticated owner can approve a PC."""

import hashlib
import json
import secrets
import threading
import time

from fastapi import HTTPException
from pydantic import Field

from ahledger.util import atomic_json
from cennik.core.models import Model

from .bridge import RegisterDevice


class PairStart(Model):
    label: str = Field(min_length=1, max_length=100)


class PairPoll(Model):
    secret: str = Field(min_length=40, max_length=100)


class PairApprove(Model):
    code: str = Field(pattern=r"^\d{8}$")
    profile_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class Pairing:
    def __init__(self, bridge):
        self.bridge = bridge
        self.root = bridge.catalog.root / "pairing"
        self.lock = threading.RLock()
        self.rates = {}

    def path(self, secret):
        return self.root / (hashlib.sha256(secret.encode()).hexdigest() + ".json")

    def pending(self):
        result = []
        for path in self.root.glob("*.json"):
            row = json.loads(path.read_text("utf-8"))
            if row["expires"] <= time.time():
                path.unlink(missing_ok=True)
            elif not row.get("device"):
                result.append({k: row[k] for k in ("code", "label", "expires")})
        return result

    def start(self, value, address):
        with self.lock:
            start, count = self.rates.get(address, (time.monotonic(), 0))
            if time.monotonic() - start > 600:
                start, count = time.monotonic(), 0
            if count >= 10 or len(self.pending()) >= 100:
                raise HTTPException(429, "Слишком много подключений; повторите позже")
            self.rates[address] = start, count + 1
            used = {row["code"] for row in self.pending()}
            code = f"{secrets.randbelow(100000000):08d}"
            while code in used:
                code = f"{secrets.randbelow(100000000):08d}"
            secret = secrets.token_urlsafe(36)
            row = {"label": value.label, "code": code, "expires": int(time.time()) + 600}
            path = self.path(secret)
            atomic_json(path, row)
            path.chmod(0o600)
            return {**row, "secret": secret}

    def poll(self, value):
        with self.lock:
            path = self.path(value.secret)
            if not path.exists():
                raise HTTPException(410, "Код подключения истёк или уже использован")
            row = json.loads(path.read_text("utf-8"))
            if row["expires"] <= time.time():
                path.unlink(missing_ok=True)
                raise HTTPException(410, "Код подключения истёк")
            return (
                {"state": "approved", **row["device"]}
                if row.get("device")
                else {"state": "pending"}
            )

    def approve(self, value):
        with self.lock:
            self.pending()
            path = next(
                (
                    p
                    for p in self.root.glob("*.json")
                    if json.loads(p.read_text("utf-8"))["code"] == value.code
                ),
                None,
            )
            if path is None:
                raise ValueError("Код не найден или истёк")
            row = json.loads(path.read_text("utf-8"))
            if row.get("device"):
                return {"approved": True}
            row["device"] = self.bridge.register(
                value.profile_id, RegisterDevice(label=row["label"], clients=[])
            )
            atomic_json(path, row)
            path.chmod(0o600)
            return {"approved": True}

    def acknowledge(self, value):
        self.poll(value)
        self.path(value.secret).unlink(missing_ok=True)
        return {"acknowledged": True}
