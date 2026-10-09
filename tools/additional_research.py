"""Three small price queries, one per other confirmed active market. No bulk crawl."""

from research import get

for market in ("forever.normal.alliance.us", "forever.normal.horde.us", "forever.pvp.alliance.us"):
    get(
        "prices-" + market.replace(".", "-"),
        f"https://api.ahledger.com/v1/prices/{market}/2589?range=90d",
    )
