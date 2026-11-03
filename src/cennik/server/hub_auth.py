"""Authenticated requests from the separate WIW Hub container."""
import base64
import hashlib
import hmac
import json
import time


def verify_hub(request, secret):
    value = request.headers.get("x-cennik-hub", "")
    signature = request.headers.get("x-cennik-signature", "")
    if not secret or len(value) > 4000:
        return None
    try:
        expected = hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        claims = json.loads(base64.urlsafe_b64decode(value))
        path = request.url.path + ("?" + request.url.query if request.url.query else "")
        if (abs(time.time() - claims["at"]) > 60 or claims["method"] != request.method
            or claims["path"] != path or claims["access"] not in {"cennik", "relay"}):
            return None
        return claims
    except (ValueError, KeyError, TypeError):
        return None
