"""Convert a downloaded auctionhouse.fyi items response into an offline reference.

No network requests or database writes. A new release snapshot is a new version;
the API's buy field remains unnormalised unless a purchase quantity is established.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from cennik.engines.npc_catalog import NpcCatalogItem, NpcPriceDocument


def convert(raw: bytes, *, version: str, phase: str, retrieved_at: str) -> NpcPriceDocument:
    data = json.loads(raw)
    if data.get("v") != 1 or not isinstance(data.get("items"), dict):
        raise ValueError("Ожидается полный ответ auctionhouse.fyi /api/v1/items")
    rows = {}
    for identity, item in data["items"].items():
        for key in ("buy", "sell"):
            number = item.get(key)
            if number is not None and (type(number) is not int or number < 0):
                raise ValueError("Неверная цена NPC: " + identity)
        rows[int(identity)] = NpcCatalogItem(name=item["n"],
            buy_copper=item.get("buy") or None, sell_copper=item.get("sell"))
    return NpcPriceDocument(version=version, phase=phase, build=data["build"],  # type: ignore[arg-type]
        source=data["source"], source_url="https://auctionhouse.fyi/api/v1/items",
        retrieved_at=retrieved_at, raw_sha256=hashlib.sha256(raw).hexdigest(), items=rows)  # type: ignore[arg-type]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--phase", choices=("beta", "release"), required=True)
    parser.add_argument("--retrieved-at", required=True, help="Actual UTC download time (ISO 8601)")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output exists; use a new file/version")
    if args.input.stat().st_size > 32_000_000:
        parser.error("Input exceeds 32 MB")
    value = convert(args.input.read_bytes(), version=args.version, phase=args.phase, retrieved_at=args.retrieved_at)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(value.model_dump_json(exclude_none=True) + "\n", encoding="utf-8")
    print(json.dumps(value.metadata(), ensure_ascii=False))


if __name__ == "__main__":
    main()
