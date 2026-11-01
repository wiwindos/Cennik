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

CraftGraph reuses the existing adapted `cheapest_material`, `auction_net`, shared
price quality and Opportunity helpers. The bounded hyperedge search is local code;
none of the reviewed sources supplies a compatible versioned, integer-batch solver
with AHledger provenance. No additional upstream code or dependency was imported.

Disenchant reference data (reviewed 2026-10-24): item metadata and numeric yield
estimates from AHledger's public catalog and 0.9.6 release model are compiled into
`reference/disenchant.json`. The original evidence, release SHA256 and comparisons
are recorded in `research/disenchant-2026-10-24/REPORT.ru.txt`. The Lua implementation
was evaluated only in the isolated research environment; no AHledger source code
is embedded in Cennik. The runtime evaluates explicit per-item numeric pools using
local Decimal arithmetic. Data attribution is displayed in the source panel and
opportunity details; AHledger data must not be presented as our own scans or resold
as a feed. The source-available addon's All Rights Reserved license is not treated
as permission to redistribute its implementation.

NPC reference data (2026-10-24): `reference/npc_catalog.json` contains numeric
buy/sell values and item names from the public auctionhouse.fyi items endpoint.
The original response SHA-256, retrieval time, build and source description are
retained. Source: https://auctionhouse.fyi/api/v1/items. Its source description
includes a Classic Era ItemSparse fallback; these are client reference values,
not observed merchant offers. No auctionhouse.fyi, GoldCap or Everloot source
code was copied. GoldCap data is not bundled. No open redistribution licence
for auctionhouse.fyi data was established by the research; this local development
reference does not assert one. Recheck data availability and reuse terms when
preparing public distribution. Research: `research/npc-api-comparison-2026-10-24/`.
