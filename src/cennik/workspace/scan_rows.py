"""Shared validation of packed auction snapshots; no market selection here."""
from __future__ import annotations


def unpack_scan(scan, identity, observed):
    variants = scan.get("variants") or []
    lines = scan["data"].split(";") if scan["data"] else []
    offers, warnings = [], []
    for index, line in enumerate(lines):
        try:
            values = [int(value) for value in line.split(":")]
            if len(values) not in {6, 7}:
                raise ValueError("expected six or seven fields")
            item, quantity, buyout, bid, remaining, variant_id = values[:6]
            ownership = values[6] if len(values) == 7 else 0
            if ownership not in {0, 1, 2}:
                raise ValueError("invalid ownership flag")
            if item <= 0 or quantity <= 0 or any(value < 0 or value > 2**53 - 1 for value in values):
                raise ValueError("invalid numbers")
            variant = variants[variant_id - 1] if 0 < variant_id <= len(variants) else {}
            offers.append({"scan_id": identity, "sequence": index, "item_id": item,
                "variant": variant.get("link"), "name": variant.get("name"), "quantity": quantity,
                "buyout": buyout, "bid": bid, "time_left": remaining, "divisible": variant.get("divisible"),
                "vendor_sell": variant.get("vendor_sell"), "vendor_buy": None, "observed_at": observed,
                "owned": None if not ownership else ownership == 1})
        except (ValueError, TypeError, IndexError) as error:
            if len(warnings) < 20:
                warnings.append(f"Строка {index + 1}: {error}")
    complete = (bool(scan.get("full")) and len(offers) == scan.get("count") == len(lines)
                and not scan.get("skipped", 0) and not scan.get("error")
                and scan.get("reported_count", scan.get("count")) == len(lines)
                and (bool(lines) or scan.get("empty_market_confirmed") is True))
    if not lines and scan.get("empty_market_confirmed") is not True:
        warnings.append("Пустой ответ API не подтверждает отсутствие товаров на рынке")
    if scan.get("error"):
        warnings.append("Сборщик: " + str(scan["error"])[:1000])
    if not complete:
        warnings.append("Покрытие неполное: предыдущая рабочая книга не заменена")
    return offers, warnings, complete

# EOF
