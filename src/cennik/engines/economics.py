from decimal import ROUND_FLOOR, Decimal


def auction_net(gross: int, cut_percent: Decimal) -> int:
    """Adapted from Forever Ledger Values.lua:AfterCut (MIT; see THIRD_PARTY.md)."""
    cut = int((Decimal(gross) * cut_percent / 100).to_integral_value(rounding=ROUND_FLOOR))
    return gross - cut


def roi(profit: int, capital: int) -> Decimal:
    return (Decimal(profit) * 100 / Decimal(capital)).quantize(Decimal("0.01"))


def cheapest_material(auction: int | None, vendor: int | None) -> tuple[int, str] | None:
    """Port of Forever Ledger Values.lua:MaterialCost, retaining source and unknowns."""
    offers = [(value, source) for value, source in [(vendor, "npc"), (auction, "auction")]
              if value is not None and value > 0]
    return min(offers, key=lambda pair: pair[0]) if offers else None


def capped_volatility(values: list[Decimal], cap: Decimal, center: Decimal) -> Decimal:
    """Adapt Pricer analysis._predict_item_prices quantile clipping (MIT).

    Keep its bounded-outlier dispersion calculation, replace pandas/scipy and
    binary floats with Decimal; the historical reference itself stays a median.
    These are temporal quantiles, never order-book price tiers.
    """
    ordered = sorted(values)

    def quantile(q: Decimal) -> Decimal:
        position = (len(ordered) - 1) * q
        lower = int(position)
        fraction = position - lower
        return ordered[lower] + fraction * (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower])

    low, high = quantile(cap), quantile(1 - cap)
    clipped = [max(low, min(high, value)) for value in ordered]
    average = sum(clipped, Decimal(0)) / len(clipped)
    variance = sum(((value - average) ** 2 for value in clipped), Decimal(0)) / len(clipped)
    return variance.sqrt() / center
