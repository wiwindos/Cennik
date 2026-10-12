# Cennik Core v0.1

Cennik extends the existing AHledger application. Build/download/recovery commands
are unchanged. Open the existing web panel, choose one market in **Cennik Analytics**,
then click **Пересчитать аналитику**. The market selection is saved immediately.
Downloading or rebuilding AHledger does not recalculate Cennik.

## Runtime and persistence

`src/cennik/core` defines Pydantic contracts and `MarketDataProvider` / `Strategy`.
The DuckDB adapter reads `analytics/ahledger.duckdb` with `read_only=True`.
The price engine supplies source observations, history, liquidity estimates and
quality flags. Independent strategy modules implement `evaluate(context)`.
`AnalyticsService` runs registered strategies, validates scope, deduplicates and
ranks opportunities. A single manually created thread runs each requested analysis.
It ends when that run finishes; there is no scheduler or download loop.

The separate `cennik/cennik.sqlite` stores settings, all runs and signals. In Docker
this is `/data/cennik/cennik.sqlite` on the existing `archive` volume. No new service,
port, dependency or Compose volume is required. The source DuckDB is never modified.

An in-process lock plus the existing OS advisory lock on `cennik/.writer.lock`
protect calculations and settings changes across instances. SQLite additionally
allows only one `running` row. Startup records abandoned runs as `interrupted`
only when the OS lock is free; it never resumes them. HTTP settings changes and
repeat starts return 409 while a run owns the lock. Graceful shutdown joins the
manual worker before closing the app.

Source generation is the SHA-256 of the actual DuckDB file, not its download time
or the Parquet pointer. A manual run hashes before and after reading; if a rebuild
replaces the source during calculation, the run fails and preserves published results.
Status reads cache the fingerprint by file modification time and size.

Run identity includes market, source hash, validated config, selected strategy
versions and engine version. An identical successful calculation returns the same
run and republishes that market's saved pointer without duplicating rows. This reuse
also works after switching settings/markets back. `as_of`, rating, input data ages
and generated time remain those of the original successful run. Status separately
reports source age **now**, configuration mismatch and changed generations. Change
freshness configuration or load a new source generation to request a new calculation.

One SQLite transaction publishes signals, the completed run and the market pointer.
Failure rolls back publication and records an error; prior success remains visible.
All list/detail queries use the currently saved market and its published run.
Switching markets never deletes other markets' runs, and no calculation is triggered.
All history is retained in v0.1; administrators may back up the SQLite file with its
WAL using SQLite's backup API. Do not copy only the live `.sqlite` file mid-write.

## Data and calculations

The adapter filters observations by market and `observed_at <= as_of`. Dates are
UTC and refer to real scans or source aggregation buckets, never `retrieved_at`.
It chooses the newest observation with deterministic scan/hour/day tie-breaking.
It does not combine minimum prices and quantities from different timestamps.
An older source sales estimate is separately timestamped and flagged when stale.
Unknown quantity/auction counts remain null. Supply changes are never called sales.

History excludes the current observation's entire UTC day, so a price cannot be its
own reference. Inside each preceding day, scans replace overlapping hours and
intraday observations replace a daily aggregate. Exact time/granularity duplicates
are removed. Each covered day contributes one median with equal weight; then the
engine takes the median of those daily values. It records sample count, covered day
count, calendar span, granularities, newest historical timestamp, relative MAD and
quantile-capped standard deviation divided by the median. These are temporal
statistics; neither P10 nor Sell Front is computed from the missing order book.

Insufficient/old/unstable history and a declining recent three-day median reject
historical discounts with counted reasons. Stale/missing/non-positive purchase
prices and known zero supply reject all affected strategies. No observed prices
or only unknown prices fail the run explicitly rather than publishing “no deals”.
The archive has partial coverage; every opportunity states that limit and includes
source identity/hash where present. Diagnostics preserve metadata warnings and gaps.

Vendor Arbitrage evaluates **one indicative unit**: `vendor_sell - minimum_buy`.
There is no auction cut on an NPC sale. Missing NPC price produces no signal.

Historical Discount compares the current minimum with the historical median,
initially requiring 15% discount. Indicative profit is the historical sale reference
less auction cut and purchase cost. The historical sale price and liquidity remain
unconfirmed; lost deposits, relisting and slippage are explicitly excluded.

Simple Craft evaluates **one craft**, joining reagents by both `spell_id` and
`version_hash`. It checks the complete normalized input set against the archived
mechanics JSON; a partial join is rejected. Recipe versions require provenance from
the selected market or a global mechanics dataset. Market-specific valuations are
not used because their existing schema lacks a recipe version hash. Item facts and
mechanics are only included when their retrieval time is at/before `as_of`; the
archive cannot reconstruct discarded earlier item facts.

Materials use the cheaper positive observed auction minimum or explicit
`items.vendor_buy_copper`, preserving each source and amount. Missing materials
invalidate the craft; they are never assigned zero cost. NPC stock/access and market
depth are unconfirmed. Material price age contributes to the oldest input timestamp.
Revenue uses the result's observed minimum times `qty_min`; a yield range is not
averaged. Known bind-on-pickup results are rejected. Profession, skill, acquisition,
recipe version and full reagent breakdown remain in `details`, with access warnings.
Multiple known recipe versions remain separate indicative candidates; no guarantee
is made that an archived version is still usable in game.

Money is integer copper. Ratios, discounts, volatility, fees and ranking use Decimal.
Auction revenue is `gross - floor(gross * cut_percent / 100)`, adapted from Forever
Ledger's observed commission rounding. ROI is `100 * profit / capital`, rounded to
two decimal places. Commission is configurable; no NPC price is inferred from AH.
`quantity` is output units (one for flips); craft capital/profit refer to one recipe
execution, and `reference_price_copper` is gross revenue for its conservative output.

Confidence is a qualitative data grade (`limited`, `moderate`, `strong`), never a
sale probability. Built-in opportunities are limited/moderate given aggregate data.
The deterministic rating is:

```text
100 * (0.45 * profit/(profit+profit_scale)
       + 0.25 * min(ROI/roi_cap,1)
       + 0.15 * max(0,1-age/max_price_age)
       + 0.15 * quality)
    / (1+risk_penalty*number_of_flags) * liquidity
quality: limited=0.4, moderate=0.7, strong=1
liquidity: 1 when unknown/stale; otherwise
           1+min(1,source_estimated_sales/observed_supply)/10
```

Inputs and formula are attached to every result. This modest liquidity adjustment
is a ranking proxy, not an execution forecast. Grade, fees and all limits are visible.

## Configuration and API

All routes inherit the existing `X-API-Key` dependency. The old endpoints and UI
remain available. No new endpoint accepts SQL, Python, shell commands or URLs.

| Route | Behavior |
| --- | --- |
| GET `/api/analytics/markets` | Actual DuckDB markets, labels and latest observation times |
| GET `/api/analytics/settings` | Saved active market and full configuration |
| PUT `/api/analytics/settings` | Replace validated settings; unknown markets/strategies return 422 |
| GET `/api/analytics/status` | Ready state, active market, source hash/age, compact latest/published run summaries, count, errors |
| POST `/api/analytics/recalculate` | 202 with run metadata; `reused` indicates an identical successful run |
| GET `/api/analytics/opportunities` | Published active-market results and total/limit/offset |
| GET `/api/analytics/opportunities/{id}` | Full active-market calculation; another market's ID returns 404 |

List parameters: `strategy`, `min_profit` (copper), `min_roi` (%), `confidence`,
`sort=rating|profit|roi|freshness`, `limit` (1–500), `offset`. All filters are combined.
Decimal input values may be JSON strings. A PUT replaces the full document; omitted
config fields use defaults. The web panel preserves other settings when changing a
market, commission or discount.

Defaults: all three strategies enabled; discount 15%; auction cut 5%; history window
30 days; at least 4 covered days spanning 3 days; latest history gap <=3 days;
current price age <=86400 seconds; relative MAD <=0.30; capped volatility <=0.50;
temporal outlier cap 0.10; recent median decline <=15%; minimum profit 1 copper.
Ranking defaults: profit scale 10000 copper, ROI cap 100%, risk penalty 0.08 per flag.
All thresholds are fields of `AnalyticsConfig`, validated by Pydantic. Source sales
and supply are optional; there are no invented substitutes.

Example after obtaining a market from `/markets`:

```json
{"active_market":"<market from DuckDB>","config":{"discount_percent":"15","auction_cut_percent":"5"}}
```

The UI shows gold/silver/copper, timestamps, manual progress, filters, pagination,
profit/ROI/rating/freshness sorting, explanations and compact diagnostics. Source
observations and estimates are labeled separately. It polls status only while a
manually requested calculation is running. AHledger's existing downloader status
poll is unchanged and does not trigger analytics.

Run diagnostics in status responses contain rejection counters by strategy, signal
counts and source-quality totals (`quality.metadata_summary`), not the full
`quality.metadata` array. The full run remains in `runs.payload` in Cennik SQLite;
`runs.summary` stores the compact polling payload. Existing databases receive the
nullable summary column automatically, and older runs get their summaries on first
read using SQLite JSON queries without loading source metadata into Python or
recalculating signals. Subsequent status, result-list and cached-run reads use that
stored summary. The UI shows reason/count tables and never prints the full run JSON.

## Verification and future modules

```powershell
venv/Scripts/python.exe -m pytest -q
venv/Scripts/python.exe -m ruff check src tests tools/check_cennik_sample.py tools/check_web_ui.py
venv/Scripts/python.exe -m mypy src
node --check src/ahledger/static/analytics.js
venv/Scripts/python.exe tools/check_web_ui.py
venv/Scripts/python.exe tools/check_cennik_sample.py <sample>/analytics/ahledger.duckdb --output data/reports/cennik-sample.json
```

Fixtures in `tests/test_cennik.py` are deliberately invented, isolated in pytest's
temporary directories and never use the network. An additional integration uses
the real AHledger archive builder with fixture responses. The sample tool creates
an isolated copy of a saved real DuckDB, asserts both source/copy hashes remain
unchanged, checks idempotency and reports rejections. It explicitly widens freshness
only for the normal-time sample check and also evaluates the historical snapshot
time. It never persists settings in the user's real Cennik directory.

Register a new independent `Strategy` with `service.registry.register(...)` and
include its ID in `enabled_strategies`. The engine/API require no changes. A custom
module may declare `settings_model`, a Pydantic model inheriting `core.models.Model`.
Its validated defaults/parameters live under `config.strategy_settings[strategy_id]`
and are included in run identity. Unknown parameters or settings for unregistered
modules are rejected. Built-in strategies use the shared configuration fields above.
A custom
`MarketDataProvider` can supply later local scans using the same market/as_of contract.
Future CraftGraph, backtesting, liquidity prediction, portfolio, journal and addon
bridge modules should build on these contracts; no empty implementations are added.

v0.2 priorities: an authenticated live order book with lot depth, verified recipe
version availability and NPC access, recorded real executions/deposit costs, and
then backtesting/liquidity evaluation. Current signals remain `indicative`.

See [`src/cennik/THIRD_PARTY.md`](../src/cennik/THIRD_PARTY.md) for pinned sources,
reused MIT implementations and why other reviewed code was not copied.
