# Cennik Addon v0.1

The addon executes procurement checks for published Cennik results. It does not
calculate strategies, change DuckDB, craft items, sell to NPCs or post auctions.
The four supported strategy IDs are `vendor_arbitrage`, `historical_discount`,
`simple_craft`, `craft_graph`. Server `Opportunity.execution_status` stays
`indicative`; addon states and reported transactions are stored separately.

## Audited basis and compatibility

Basis: branch `codex/analytics-layer`, commit
`26c4fd7a30b6f4b1597f8f62df9fa4674bec6059`. The supplied `Auctionator/` has version
340, a Forever `Source_Forever` branch, ModernAH and LegacyAH implementations.
Its code is unchanged. Cennik requires **ModernAH** capabilities at runtime;
LegacyAH is deliberately not silently treated as compatible.

Audited source:

* `Auctionator/Source/API/v1/MultiSearch.lua`: public `MultiSearch` opens
  Shopping. Public API v1 has no execution queue or live lot callback for Cennik.
* `Auctionator/Libs_ModernAH/LibAHTab/LibAHTab.lua`: `LibAHTab-1-0`, minor 5,
  `CreateTab`/`SetSelected`. Cennik uses the existing library, not a copied tab system.
* `Source_ModernAH/AH/Mixins/SearchScan.lua` and `AH/Wrappers.lua`: item cache,
  `SendSearchQuery(itemKey, sorts, splitOwnedItems)`, complete result flags.
* `Source_ModernAH/Tabs/Buying/Commodity/Mixins/Main.lua`: quantity/price levels,
  Start/price/Confirm sequence and commodity purchase events.
* `Source_ModernAH/Tabs/Buying/Item/Mixins/Dialog.lua`: `PlaceBid(auctionID,buyout)`.
* `Source_ModernAH/FullScan/Events.lua`, `Source/Shopping/Events.lua`:
  activity events. The adapter only observes these audited internals because v1
  does not expose busy state; it does not drive Auctionator private buying modules.

[LibAHTab upstream](https://github.com/TheMouseNest/LibAHTab) and
[Forever Ledger BuyQueue](https://github.com/ivellos/forever-ledger/blob/main/BuyQueue.lua)
were also reviewed. Ledger confirms the commodity two-step sequence, throttle,
own-supply exclusion and Forever `AUCTION_HOUSE_PURCHASE_COMPLETED` event usage.
No Ledger/LibAHTab source was copied. Existing adapted Cennik economics is retained.

**Import and the tab have been checked in the user's Forever client; purchase
operations and taint still need live testing.** The shipped
TOC includes Forever 16001 (client 1.60.1), alongside 38000/120100/50504 from the
supplied Auctionator; unsupported APIs fail closed
with diagnostics. Lua 5.1 event tests do not replace the live checklist below.

## Files and responsibilities

* `CennikAddon/`: separate addon, dependent on Auctionator. `Core.lua` owns state,
  `Compatibility/Auctionator.lua` detects API/activity and opens public Shopping,
  `Scanner.lua` performs sequential targeted scans, `Validator.lua` checks frozen
  task economics, `BuyQueue.lua` prepares one operation, `Execution.lua` correlates
  replies and protects uncertain operations, `ImportExport.lua` handles packages,
  `UI/Main.lua` creates a LibAHTab tab with Opportunities / Buy Queue / History.
* `src/cennik/addon/`: exchange models, independent SQLite service and API router,
  a literal-only Lua parser/serializer and Windows Bridge CLI.
* `bridge/start_bridge.cmd`: uses the existing venv. No separate server or UI.

Frames are pooled on redraw. Auctionator works independently. The adapter observes
external AH calls and full/Shopping scans; a conflict cancels our scanner or blocks
uncertain purchase attribution. Search results must match the requested item/key
and be complete. Pagination is bounded to ten requests and a 30-second timeout.
Missing cache/type information requires a manual check, not an inferred result.

## Export from the web panel

1. Choose one active market; update/recalculate manually if needed.
2. Select published opportunities using the final checkbox column, across pages.
3. Click **Передать в аддон**. Review explicit per-unit AH price caps (integer
   copper) and the total package budget. NPC prices/quantities remain separate.
4. Create the package. Default caps are the original material prices, not an
   automatic slippage allowance. Quantities and CraftGraph steps stay fixed.
5. In configured local mode, creating or renewing a package automatically writes
   it to the installed addon. Then `/reload` in game loads `ImportedData.lua`.
   Remote mode still uses Bridge.

Local setup lives in the ignored `data/bridge/local.json` with `game_root` (the
absolute directory containing Interface and WTF) and `account` (one account
folder name). The page's **Синхронизировать с игрой** button reuses
`data/bridge/state.json`, including its existing device ID and acknowledged
events. It writes only `ImportedData.lua`; game-owned SavedVariables are read
as data and never overwritten. After game checks/purchases, `/reload`, click
sync, then open **История исполнения**. `/reload` again loads acknowledgments.
Sync does not update the market, run analytics or change DuckDB. File sync is
available only in the guarded loopback local mode; it does not accept paths
from web requests. CLI and page sync share a process-safe state lock.

With no gold, price scans and potential-cost/profit displays still work, but
the task cannot enter Buy Queue. Addon checks do not craft or sell anything;
professions, recipes, NPC access and cooldowns require player confirmation.

Item identity always uses numeric item IDs and exact auction ItemKeys. The addon
resolves display names, links, icons, material/result labels and Shopping search
terms from the current game client's item data by ID. A Russian client displays
and searches Russian names; switching the client locale changes those names
without changing tasks, quantities, limits or source Opportunity data. Imported
English names remain provenance, not search terms. Unknown names display the ID
and a loading status instead of using an English search fallback.

Uncached item data is requested once per pending ID. Load events and bounded
polling refresh the UI and start the explicitly requested Shopping search when
the name becomes available. Failure/15-second timeout is visible and allows an
explicit retry. Closing AH or starting a newer Shopping request prevents a late
load callback from opening an obsolete search. An unknown item type waits for
client data before scanning. Equipment still requires confirmation of the exact
variant; localized display names never change auction identity or price checks.

Shopping uses an unquoted `MultiSearch` request so suffix variants can appear.
The wider name search never authorizes a purchase: the chosen variant must have
an item ID belonging to the task, and live lot validation still checks exact
ItemKey, quantities and price limits. Choosing an unrelated result shows an error.

The header permanently shows whether the active market is confirmed for the
current character, realm and faction. The confirmation button reports success
or failure in a separate status line; successful confirmation changes its label.
Changing character/realm/faction/market invalidates that displayed confirmation.

List and text scroll frames use separate pools. Tab clicks reset their scroll
offset, recompute the child rectangle and redraw once after the frame is shown.
Nested redraws are deferred to avoid clearing a half-built view. The list shows
a task count and an explicit empty state rather than an unexplained blank area.
Every redraw explicitly restores the scroll child's TOPLEFT anchor after reuse;
setting an unchanged scroll child alone must not be relied on to restore points
cleared by the pool. Text content has a measured, nonzero height, and scroll
offsets are clamped when content shrinks. Tests include anchors and geometry,
including the deferred redraw that previously made history disappear.

Export rejects generation/config/strategy-version mismatch, old observations,
unknown IDs/markets, incomplete plans, concurrent analysis/update and duplicate
procurement of the same opportunity/run. Request IDs make retried requests
idempotent. Package lifetime defaults to 30 minutes (allowed 1–120 minutes).
**Продлить пакеты на 30 минут** explicitly renews only the current published run's
packages after the same freshness checks. Task/package IDs, game purchases and
budgets are preserved; renewal is not reissuing the procurement.

Four adapters consume existing Opportunity details. For flips the purchased item
is the result ID. For crafts the AH queue comes only from `details.materials` with
`source=auction`; `Opportunity.quantity` is output, not material quantity. The
whole original Opportunity, risks, source generation and fixed chain are retained.

## Bridge setup (after the client path is known)

Copy `CennikAddon/` to the game's `Interface/AddOns/CennikAddon/` beside the supplied
working Auctionator. Do not replace Auctionator with an unrelated version.

Start local Cennik with the existing `start_local.cmd`. From the repository:

```powershell
bridge/start_bridge.cmd --addon-dir "<game>/Interface/AddOns/CennikAddon" --saved-variables "<game>/WTF/Account/<account>/SavedVariables/CennikAddon.lua" --market forever.pvp.alliance.us
```

There are no network/game paths hardcoded into the bridge except the loopback
server default `http://127.0.0.1:8765`. `--watch` explicitly starts sync every ten
seconds; it never updates prices, recalculates analytics or sends game controls.
For a remote server use `--server https://...` and `CENNIK_API_KEY` in the environment.
Keyless mode is accepted only for a loopback server whose health reports local mode.
Secrets are not written into the addon, Lua export or Bridge state.

Bridge state defaults to ignored `data/bridge/state.json`; retain it for the stable
device ID and acknowledgement ledger. HTTP redirects/proxies are disabled. It
uploads unsent events first, commits acknowledgements after the server confirms
the entire batch, then atomically replaces `ImportedData.lua`. A failed sync never
marks unsent events as sent. SavedVariables are read only, with before/after stat
checks to avoid a file being written. Unsupported/executable Lua, duplicate table
keys, huge/deep files or unexpected assignments are rejected; Lua is never executed.

The game loads Lua imports at addon startup or `/reload`. It saves SavedVariables
on `/reload` or logout. A typical exchange takes two reloads: one saves reports for
Bridge to upload; after Bridge sync another loads new packages/acknowledgements.
There is no requirement for a server request in the middle of procurement.

## In-game operation

Open the AH and Cennik tab. First **Подтвердить рынок** explicitly binds the package
market to the current realm/character/faction; faction mismatch is rejected. Region
and realm-to-market mapping are not guessed. Review the displayed market yourself.
`/cennik market <market>` and `/cennik` are equivalent setup/open commands.

Check one or selected tasks. Every check clears the previous validation and scans;
missing supply below a price cap reports the item, available/needed quantity and cap.
Trade goods are classified by client class ID; NON_EQUIP is not equipment.
For equipment without a selected ItemKey, a full localized-name browse is filtered
by numeric ID. A unique key proceeds automatically; multiple keys require opening
Shopping and confirming the observed ItemKey via **Выбрать вариант**. Shopping targets
the blocked material automatically. No equivalence of different stat variants is assumed.
Incomplete browse results and timeouts block execution; no missing price is zero.

**Скрыть задание** removes the task from Opportunities and Buy Queue while retaining
purchases, spending and event history. The same import/renewal never restores it.
**Вернуть скрытые** restores current-market/current-run tasks for a fresh check;
neither action is allowed with an unresolved purchase. A newly calculated run may
contain a new task for the same item.

Details use a clipped FontString scroll viewport above the AH money/footer area.
Each material displays whether a fresh full scan confirms enough purchasable lots
under its cap; NPC availability still requires player confirmation.
Details show the source's daily-sales estimate, source and observation timestamp.
These are imported estimates, not confirmed transactions or sale-time predictions.
Craft liquidity evidence remains frozen with the exported opportunity.

The validator prices the required amount across real rows, excluding known own
offers, rejecting unknown ownership and respecting per-unit caps. On Forever,
item search groups describe available depth at a per-item price. Each PlaceBid
buys one representative AuctionID; it never buys the group as a whole. Commodity
purchases request the needed quantity and require a separate server quote.
Current result scans only change the sale *reference*, never establish a sale.
Historical discount uses the exported historical median/threshold, vendor uses the
exported vendor sell price. Craft validation uses the same original quantities,
NPC costs, recipe versions, steps and batches; no new Lua CraftGraph is implemented.
Fee/discount arithmetic uses rational integer fractions to avoid decimal rounding
errors. Unknown prices are never zero. Monetary exports are bounded to Lua's
exact integer range. Fee/discount export supports up to four decimal places.

For crafts, explicitly acknowledge NPC/profession/recipe/cooldown conditions.
Original risk flags remain visible. Add ready tasks to Buy Queue, then:

### Live decision stage

The web run remains a coarse, immutable comparison. **Проверить** scans current
AH result/material rows and computes the complete remaining procurement bundle
independently of old per-unit caps. The planner consumes only the required units
across price groups (for vendor arbitrage, prioritising NPC return minus cost).
Available group depth and one action's purchase quantity are stored separately.
Every subsequent item purchase requires a fresh representative ID and check.
Known own commodity units are removed without
discarding other owners' units at the same commodity price.

The in-game result shows cost including previous spending/NPC purchases, gross
sale reference, auction fee, potential profit, ROI (profit / cost), margin
(profit / gross revenue), and an integer break-even unit price. Negative profit
is retained. Missing quantities/prices, incomplete scans and unknown ownership do
not become zero-price materials or fabricated complete profit. All missing inputs
are listed; partial AH data remains visible. The current resale reference for
Historical Discount also comes from AH; the historical price remains the discount
comparison and the old web result. Buying at the current market floor usually
does not create an immediate resale margin after fees.

Every AH observation has a 90-second validity window. The list displays current
profit only for a fresh complete check; expired results show no current estimate.
NPC changes invalidate displayed complete figures. Estimates never establish an
actual sale or realised profit; deposits/relisting losses/crafting time remain
excluded as in the exported contract.

**Принять цены** opens a review form populated with current caps and task cost;
it does not approve a purchase. **Цены / лимиты** supports explicit editing of caps,
task total and package budget. Saving requires another
scan. Old caps/budget never silently increase, and a higher current price yields
`limits_exceeded` with complete economics rather than false `insufficient_supply`.
The draft survives refreshes and scan expiry while editing.

Vendor arbitrage automatically browses and scans every observed ItemKey for its
ID, up to 40 variants, checking ownership, exact AuctionID and NPC sale value. NPC
sale prices use client GetItemInfo for the exact result link when available,
then a player-confirmed value for that key, then the user-authorized stable DB
NPC price for the item ID. Every source is displayed; AH buy prices/caps never
substitute for NPC prices. If all sources are missing, profit is unknown and
**Цены / лимиты** provides per-variant manual sale-price fields. A slow/incomplete
multi-variant scan cannot be treated as fresh or complete.

NPC material buy prices prefer a recent same-character merchant/manual quote,
then the stable NPC price frozen in the exported DB materials. Unknown NPC prices
explicitly block full profit/ROI/margin. MERCHANT_SHOW/UPDATE captures gold-only
offers and bundle sizes; extended costs/unavailable offers are not treated as
ordinary gold prices. NPC bundles are rounded up and charged in full. Manual input
contains bundle price and size. Merchant/manual quotes expire after 15 minutes and
are character/realm/faction scoped; DB fallback is explicitly labelled separately.

Purchase submission still requires the player's click and a separate commodity
confirmation. Final validation rechecks price/key/AuctionID/NPC value, profit,
all caps, gold and the full remaining budget including reserved NPC costs.

1. **Подготовить / обновить** rechecks the entire remaining bundle and the final
   sale reference. If another material disappeared, procurement is held.
2. **Купить** performs one hardware-triggered item purchase or commodity reservation.
3. Commodity quotes require a separate **Подтвердить** player click. Any increase
   beyond caps, profit floor, remaining budget or gold cancels the quote.
4. Only a purchase-completion event counts quantity/cost. Submitting a command,
   a new bid event, supply disappearance or an unrelated UI error is not proof.
5. Each next purchase must be prepared and clicked again. Stop/skip retain already
   purchased quantities and spending. Completing AH materials means procurement
   only; no craft, NPC purchase or resale is claimed.

An unconfirmed purchase timeout/reload/stop remains an in-flight unknown operation;
new purchases are blocked. Late success is counted once. If external AH activity
makes attribution ambiguous, automated correlation is blocked. After checking mail
and native history, the player may explicitly declare bought/not bought and enter
actual amount/cost. These reports are labeled manually confirmed. Quantities,
spending, in-flight operation, budgets and duplicate guards persist in SavedVariables.
Imports never reset them; a renewal changes only expiry. Unsent reports are retained
until acknowledged; the in-game journal keeps its last 500 events separately.

## Existing-server API and storage

All routes inherit the existing X-API-Key dependency (or loopback-only local mode).
No new server/port/SQL/network execution endpoint is added.

| Endpoint | Purpose |
|---|---|
| POST `/api/addon/exports` | Create package from selected published opportunities and explicit limits |
| GET `/api/addon/packages?market=...` | Current-run, unexpired packages for Bridge |
| POST `/api/addon/packages/{id}/renew` | Explicit renewal, preserving all IDs and procurement |
| POST `/api/addon/reports` | Atomic batch of up to 500 idempotent game reports |
| GET `/api/addon/history` | Latest reported events for active market only |

Exchange tables (`addon_packages`, `addon_tasks`, `addon_events`) extend the existing
Cennik SQLite. Analytical Opportunities are unchanged. Reports are player/client
reports, not independently authenticated proof of a game transaction. Event IDs
are unique and conflicting retries are rejected; market/run/opportunity must match
the exported task. Scans are stored as game checks and never become historical
DuckDB price observations.

## Verification and deferred live acceptance

```powershell
venv/Scripts/python.exe -m pytest -q
venv/Scripts/python.exe -m ruff check src tests tools/check_web_ui.py
venv/Scripts/python.exe -m mypy src
node --check src/ahledger/static/analytics.js
venv/Scripts/python.exe tools/check_web_ui.py --local
```

Python tests cover export adapters, gates, authorization, idempotency, reports,
safe Lua parsing and Bridge retries. `tests/test_addon_lua.py` runs the real addon
files under Lua 5.1 (Lupa is a **development-only** dependency), with fixture game
events and hardware restrictions. Tests cover price levels, NPC separation,
group depth versus one-item purchases, fee/discount boundaries, variants, partial scans, budget, expiry, wrong
market/run, separate quote confirmation, timeout/late success, reload, external
conflicts, duplicate import, frame reuse and immutable report snapshots.
The browser check exports a fixture graph through the actual web API. Tests never
purchase anything or upload fixture market observations into the real source.

Live acceptance remains pending by the user's choice:

1. Confirm game interface/build and supplied Auctionator 340 working independently.
2. Install Cennik, open/close all AH tabs, verify no Lua errors/taint or stuck scans.
3. Verify exact variants and commodity/item result signatures/completeness on Forever.
4. Check commodity quotes/ordinary one-item buys with an intentionally tiny budget and
   real player clicks; compare event correlation, quantity, price and mail.
5. Test price changes, own listings, competing Auctionator searches, AH close,
   disconnect, timeout and a late completion without duplicate procurement.
6. Verify SavedVariables on `/reload`/logout, Bridge import/ack and server history.

Until this checklist passes, the package is code-tested and ready for client
validation; compatibility and real purchase execution are **not certified**.
