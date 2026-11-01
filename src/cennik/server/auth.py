"""Owner browser sessions; worker and collector credentials stay separate."""
import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException


class OwnerAuth:
    cookie = "cennik_owner"

    def __init__(self, secret, origin):
        if len(secret) < 32:
            raise ValueError("Нужен отдельный ключ владельца минимум 32 символа")
        self.secret = secret.encode()
        self.origin = origin.rstrip('/')
        self.attempts = {}
        self.sessions = {}

    def login(self, value, address):
        start, count = self.attempts.get(address, (time.monotonic(), 0))
        if time.monotonic()-start > 600:
            start, count = time.monotonic(), 0
        if count >= 10:
            raise HTTPException(429, "Слишком много попыток входа; повторите через 10 минут")
        self.attempts[address] = start, count+1
        if not secrets.compare_digest(value.encode(), self.secret):
            raise HTTPException(401, "Неверный ключ владельца")
        payload = f"{int(time.time())+43200}.{secrets.token_hex(16)}"
        token = payload+"."+hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        self.sessions = {k: v for k, v in self.sessions.items() if v > time.time()}
        self.sessions[hashlib.sha256(token.encode()).hexdigest()] = int(time.time())+43200
        return token

    def logout(self, request):
        token = request.cookies.get(self.cookie, "")
        self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)

    def authenticated(self, request):
        key = request.headers.get("X-Owner-Key", "")
        if key and secrets.compare_digest(key.encode(), self.secret):
            return True
        value = request.cookies.get(self.cookie, "")
        try:
            expiry, nonce, signature = value.split(".")
            expected = hmac.new(self.secret, (expiry+"."+nonce).encode(), hashlib.sha256).hexdigest()
            return int(expiry) > time.time() and hmac.compare_digest(expected, signature) and hashlib.sha256(value.encode()).hexdigest() in self.sessions
        except (ValueError, TypeError):
            return False
