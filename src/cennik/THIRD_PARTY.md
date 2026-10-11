# Upstream review and adapted algorithms

Reviewed 2026-10-11. Only the two MIT sources below contributed implementations.
Their complete license texts are shipped in `licenses/`. No upstream project is
installed or executed, and no new runtime dependency is needed.

| Project / reviewed commit | Relevant source | Use in Cennik v0.1 |
| --- | --- | --- |
| [Forever Ledger](https://github.com/ivellos/forever-ledger/tree/6ec9956fc1fb98d80fca88d8a1984552d007cee6), MIT | [Values.lua](https://github.com/ivellos/forever-ledger/blob/6ec9956fc1fb98d80fca88d8a1984552d007cee6/Values.lua), `AfterCut`, `MaterialCost`; Deals.lua | Port `gross-floor(gross*cut)` and choose the cheaper known vendor/auction material price in `engines/economics.py`. Preserve provenance, Decimal arithmetic and unknown values. Review of conservative sale valuation informed using minimum output price for craft. No addon logic is included. |
| [Pricer](https://github.com/bluemania/wow_auctions/tree/77ed4665e0b4e543b09c45fff4d9675bf5016d34), MIT | [analysis.py](https://github.com/bluemania/wow_auctions/blob/77ed4665e0b4e543b09c45fff4d9675bf5016d34/src/pricer/analysis.py), `_predict_item_prices` | Adapt temporal quantile clipping before dispersion measurement to `capped_volatility`, using Decimal and the standard library. Keep a median reference rather than its exponentially weighted mean; do not copy its zero-fill behavior. |
| [WOW4E AH Trader](https://github.com/1nd1v1d/WOW4E_AH_Trader/tree/efeae37125e6449e86135ca5d23fb74085cf03fd) | [Calculator.lua](https://github.com/1nd1v1d/WOW4E_AH_Trader/blob/efeae37125e6449e86135ca5d23fb74085cf03fd/Calculator.lua), margins/material breakdown | Studied. No license declared in repository metadata at review time, so no code transferred. Its estimated NPC sell price and deposit heuristic are inappropriate for AHledger's unknown inputs. |
| [Auction Classic ML](https://github.com/AngelUrq/auction_classic/tree/2fa4a5d1605fbf741cbf74715d0bd2f1c9b1ba56) | [price_features.py](https://github.com/AngelUrq/auction_classic/blob/2fa4a5d1605fbf741cbf74715d0bd2f1c9b1ba56/src/data/price_features.py) | Studied. Per-listing competitor features require an order book unavailable in this archive. No declared license in metadata; no code transferred. ML is outside v0.1. |
| [WoWderhoi](https://github.com/BrandNewJimZhang/wowderhoi-ah/tree/abb5459b4494efed57ff772b31080ef1f2b19ed3), AGPL-3.0 | [analytics.ts](https://github.com/BrandNewJimZhang/wowderhoi-ah/blob/abb5459b4494efed57ff772b31080ef1f2b19ed3/src/lib/analytics.ts), [crafting.ts](https://github.com/BrandNewJimZhang/wowderhoi-ah/blob/abb5459b4494efed57ff772b31080ef1f2b19ed3/src/lib/crafting.ts) | Studied missing-material handling and output quantities. No source copied: it uses curated TBC recipes, and transferring AGPL code would impose additional distribution obligations. Cennik uses AHledger versioned mechanics. |

Existing local reuse: `ahledger.util` supplies JSON serialization, hashing and
the OS writer-lock implementation. The existing DuckDB schemas, atomic builds,
FastAPI authorization, static-file packaging and Docker volume are retained.
