"""Bounded discovery, never a bulk downloader. Standard library only."""

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "research"
ROOT.mkdir(exist_ok=True)


def get(name, url):
    started = time.monotonic()
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "AHledgerHistorical/1.0 (API research)",
            "Accept-Encoding": "identity",
        },
    )
    try:
        response = urllib.request.urlopen(request, timeout=15)
    except urllib.error.HTTPError as exc:
        response = exc
    except Exception as exc:
        print(name, type(exc).__name__, str(exc), flush=True)
        return None
    with response:
        body = response.read()
        meta = {
            "url": url,
            "status": response.status,
            "headers": {
                k.lower(): v
                for k, v in response.headers.items()
                if k.lower()
                in {
                    "etag",
                    "last-modified",
                    "cache-control",
                    "age",
                    "date",
                    "expires",
                    "content-type",
                    "content-length",
                    "content-encoding",
                    "retry-after",
                    "x-ratelimit-limit",
                    "x-ratelimit-remaining",
                    "x-ratelimit-reset",
                }
            },
            "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    (ROOT / (name + ".body")).write_bytes(body)
    (ROOT / (name + ".meta.json")).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    try:
        data = json.loads(body)
        preview = json.dumps(data, ensure_ascii=False)[:4500]
    except (ValueError, UnicodeError):
        data = None
        preview = body.decode("utf-8", errors="replace")[:500]
    print(name, meta["status"], len(body), preview, flush=True)
    time.sleep(max(0, 1.1 - (time.monotonic() - started)))
    return data


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        get(sys.argv[1], sys.argv[2])
    else:
        get("markets", "https://api.ahledger.com/v1/markets")
        get("catalog-trade-goods", "https://ahledger.com/api/catalog?class=trade-goods")
        get("catalog-options", "https://ahledger.com/api/catalog?class=invalid")
        get(
            "crafting-options",
            "https://api.ahledger.com/v1/crafting/forever.pvp.horde.us?profession=invalid&all=1",
        )
        for range_name in ("24h", "7d", "30d", "90d"):
            get(
                "prices-" + range_name,
                "https://api.ahledger.com/v1/prices/forever.pvp.horde.us/2589?range=" + range_name,
            )
        get("pricetable", "https://api.ahledger.com/v1/pricetable/forever.pvp.horde.us")
        get("no-scans", "https://api.ahledger.com/v1/prices/forever.rp.horde.us/2589?range=90d")
        get(
            "crafting",
            "https://api.ahledger.com/v1/crafting/forever.pvp.horde.us?profession=Tailoring&all=1",
        )
        get("disenchant", "https://api.ahledger.com/v1/disenchant/forever.pvp.horde.us?all=true")
        get("economy", "https://api.ahledger.com/v1/economy")
        get("economy-market", "https://api.ahledger.com/v1/economy/forever.pvp.horde.us")
        get("structure", "https://api.ahledger.com/v1/structure/forever.pvp.horde.us")
        get(
            "signals", "https://api.ahledger.com/v1/signals/forever.pvp.horde.us?discount=20&days=7"
        )
        get(
            "deals",
            "https://api.ahledger.com/v1/deals/forever.pvp.horde.us?discount=15&units=1&sells=0&limit=200",
        )
        get("item", "https://api.ahledger.com/v1/items/2589?game=forever")
        get("builds", "https://ahledger.com/feed/builds.xml")
