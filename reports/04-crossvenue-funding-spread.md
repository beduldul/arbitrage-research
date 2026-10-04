# Cross-venue funding spread — the largest-capacity carry variant (2026-10-03)

**Question.** For one base asset listed as a USDT perp on ≥2 venues, is there a
*persistent* funding-rate difference that a delta-neutral two-perp position can collect?
Go **long the perp on the venue whose funding is negative**, **short the perp on the venue
whose funding is positive**. Both legs are perps — no spot leg, no hedgeability problem —
so price risk cancels and net income is the funding spread.

**Scope:** measurement only. No orders, no keys, no `.env` edits. Public unauthenticated
endpoints only.
**Script:** `scripts/arb_funding_spread.py`. **Output:** `spread_ranking.json`.
**Raw:** `raw/` (2,465 cached responses, 34 MB). **Probe evidence:** `reachability.json`.

---

## 1. Reachability — probed 5× per endpoint at 2.5s spacing (never concluded from one attempt)

Bybit / OKX / Bitget are **intermittent** from this host: a sibling worker saw them
TCP-refused (HTTP 000, sinkholed to `202.169.44.80`) minutes before this run, and the
earlier multi-venue report recorded them as blocked. **This run they all answered.**

| venue | endpoint | status per attempt (5) | ok | funding payload valid? |
|---|---|---|---:|---|
| gate | `futures/usdt/contracts` | 200,200,200,200,200 | 5/5 | — (enumerate) |
| gate | `futures/usdt/funding_rate` | 200,200,200,200,200 | 5/5 | **yes** (`{r,t}`) |
| bybit | `v5/market/instruments-info` | 200,200,200,200,200 | 5/5 | — (enumerate) |
| bybit | `v5/market/funding/history` | 200,200,200,200,200 | 5/5 | **yes** (`retCode:0`, `fundingRate`+`fundingRateTimestamp`) |
| okx | `v5/public/instruments` | 200,200,200,200,200 | 5/5 | — (enumerate) |
| okx | `v5/public/funding-rate-history` | 200,200,200,200,200 | 5/5 | **yes** (`code:"0"`, `fundingRate`+`fundingTime`) |
| bitget | `v2/mix/market/contracts` | 200,200,200,200,200 | 5/5 | — (enumerate) |
| bitget | `v2/mix/market/history-fund-rate` | 200,200,200,200,200 | 5/5 | **yes** (`code:"00000"`, `fundingRate`+`fundingTime`) |
| hyperliquid | `info` POST `metaAndAssetCtxs` | 200,200,200,200,200 | 5/5 | — (enumerate) |
| hyperliquid | `info` POST `fundingHistory` | 200,200,200,200,200 | 5/5 | **yes** (`fundingRate`+`time`) |

**Operational caveat (material).** Reachability is **not** stable across minutes. A
strategy that needs *simultaneous* access to both legs cannot be assumed to have it — a
venue can vanish mid-hold. This run's numbers are valid for the window it measured; they
do not establish that both venues stay reachable for the 5–30 day holds below.

## 2. Coverage — per-venue enumeration and usable history

| venue | USDT perps enumerated | liquid (≥$1M/24h) bases on ≥2 venues → history pulled | history OK | order book OK |
|---|---:|---:|---:|---:|
| gate | 584 | 194 | 194 | 194 |
| bybit | 783 | 246 | 246 | 246 |
| okx | 485 | 224 | 224 | 224 |
| bitget | 813 | 255 | 255 | 255 |
| hyperliquid | 178 | 121 | 121 | 121 |

- **2,843 perps enumerated** across 5 venues; **259 liquid bases** listed on ≥2 venues.
- **1,674 cross-venue pairs** evaluated (bases listed on 2–5 venues, every venue pair),
  each on **≥30 days** of funding history where the venue allowed pagination.
- Window achieved: **90 buckets** (30.0 days) for 1,641 pairs; median 90; min 12 (a few
  newly-listed pairs — all rejected by the persistence window below).
- Funding bucket = **8h** UTC-aligned; each venue's native interval (Gate 1/4/8h, Bybit
  8h, OKX 8h, Bitget 8h, HL 1h) is **summed into the 8h bucket**, so venues settle on
  different cadences but the spread is measured as income over the same wall-clock window.

## 3. Costs — four fee events + measured bid-ask on both legs

Two perp round trips = **4 fee events** (2 venues × in/out) at the project's base tier
(`engine/fees.py`: perp taker 5 bps, maker 2 bps). The bid-ask **half-spread is measured
live** from each venue's order book (`raw/*/book/`) and charged on **4 crossings** (2 legs
× in/out) on the leg notional. At $100, leg = $50.

| venue | published taker bps | published maker bps | verified? | source |
|---|---:|---:|---|---|
| gate | 5.0 | 2.0 | **VERIFIED via API** | `contracts.taker_fee_rate` = **7.5 bps** (all 584 agree) — *higher* than the 5.0 project default |
| bitget | 6.0 | 2.0 | **VERIFIED via API** | `contracts.takerFeeRate` = 6.0 bps, `makerFeeRate` = 2.0 |
| bybit | 5.5 | 2.0 | unverified | public docs (no unauth fee field in payload) |
| okx | 5.0 | 2.0 | unverified | public docs |
| hyperliquid | 3.5 | 1.0 | unverified | public docs |

**The project model under-charges Gate.** Gate's own API exposes `taker_fee_rate` = 0.075%
(7.5 bps) on every USDT perp, not the 5.0 bps the project default assumes. Every candidate
row below uses the **project default (5 bps)** and is therefore **optimistic on any Gate
leg**. A sensitivity re-run charging Gate 7.5 bps and Bitget 6.0 bps (their verified
values) still leaves **324 of 325 candidates** clearing the cost gate — the ranking is not
an artifact of the under-charge, but the Gate-leg dollar figures are ~0.5–1.0 bps/leg
better than reality. Gate's maker rate is a **−1 bps rebate** (API `maker_fee_rate` =
−0.0001), so a *maker* variant is materially cheaper than the taker model shown.

## 4. IS/OOS split, block bootstrap, and persistence gates

Every pair is split chronologically at the midpoint: the traded direction must be
**positive in BOTH halves**, and the OOS half must have a **moving-block bootstrap 95% CI
excluding zero** (block = 5 buckets, 2,000 resamples, seed 20261003 — the decisive study's
estimator, reused so both studies define significance identically). Three persistence gates
run *before* the economics, because a large mean with an unstable sign is not a carry:

- **window ≥ 42 buckets (~14d)** — reject short, newly-listed pairs;
- **traded sign holds in ≥60% of buckets** — reject flip-flops;
- **median sign == mean sign** — reject means driven by one outlier bucket.

Of 1,674 pairs: **1,199 rejected as flip-flops**, 47 not positive in both halves, 45 cost
not amortised in-window, 44 OOS CI includes zero, 8 too-short window; **325 pass as
candidates**, 6 unconstructible at $100.

## 5. $100 reality — leg = $50 per venue

Each venue's perp minimum order value (Gate `order_size_min × quanto_multiplier × price`,
Bybit `lotSizeFilter.minOrderQty × price` / `minNotionalValue`, OKX `minSz × ctVal × price`,
Bitget `minTradeNum × price` / `minTradeUSDT`, HL $10) is compared to the $50 leg.
**6 pairs are UNCONSTRUCTIBLE** at $100 (leg below a venue minimum). The decisive table
lists each pair's leg minimums; the binding ones are Gate perps with a high
`order_size_min × quanto_multiplier × price` and HL's $10 floor.

## 6. Result — top 25 cross-venue funding spreads, best pair per base

Direction: long the negative-funding venue, short the positive-funding venue. `$/day` and
`$/min` are the **gross funding spread** at $100 ($50/leg); `net $/day` amortises the
one-time round-trip cost over the 30-day window. Fees = project taker 5 bps; spread =
measured half-spread.

| # | base | venues (A>B) | traded dir (long/short) | mean bps/8h | same-sign | streak d | flip | gross $/day | cost $ | net $/day | gross $/min | IS bps | OOS bps | OOS 95% CI | leg min A/B $ | capacity $ |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | BTW | bitget>bybit | bitget / bybit | -5.43 | 74% | 4.3 | 0.11 | 0.0815 | 0.1200 | 0.0775 | 0.00006 | 3.05 | 7.82 | [4.23, 13.92] | 5.00 / 14.44 | 18,676 |
| 2 | MINA | gate>hyperliquid | hyperliquid / gate | 3.81 | 74% | 5.7 | 0.31 | 0.0571 | 0.1639 | 0.0516 | 0.00004 | 5.06 | 2.55 | [1.33, 3.60] | 0.02 / 10.00 | 19,588 |
| 3 | EGLD | bybit>gate | gate / bybit | 3.35 | 62% | 7.0 | 0.18 | 0.0503 | 0.1334 | 0.0458 | 0.00003 | 2.06 | 4.64 | [2.48, 6.84] | 5.00 / 0.45 | 133,319 |
| 4 | PURR | hyperliquid>okx | okx / hyperliquid | 3.46 | 99% | 22.7 | 0.02 | 0.0519 | 0.2340 | 0.0440 | 0.00004 | 3.43 | 3.49 | [1.66, 5.65] | 10.00 / 1.20 | 5,473 |
| 5 | LYN | bitget>gate | bitget / gate | -3.39 | 60% | 8.7 | 0.09 | 0.0509 | 0.2854 | 0.0413 | 0.00003 | 0.62 | 6.17 | [4.44, 8.21] | 5.00 / 0.27 | 728 |
| 6 | 龙虾 | bitget>gate | gate / bitget | 3.19 | 64% | 4.7 | 0.33 | 0.0478 | 0.2492 | 0.0394 | 0.00003 | 1.77 | 4.61 | [1.62, 8.85] | 5.00 / 4.72 | 76 |
| 7 | MUBARAK | bitget>gate | bitget / gate | -2.55 | 79% | 12.0 | 0.02 | 0.0383 | 0.1560 | 0.0330 | 0.00003 | 0.94 | 4.16 | [2.82, 6.50] | 5.00 / 0.63 | 3,004 |
| 8 | 2Z | bybit>gate | bybit / gate | -2.46 | 70% | 5.7 | 0.07 | 0.0369 | 0.1334 | 0.0324 | 0.00003 | 2.00 | 2.92 | [0.65, 4.36] | 5.00 / 4.48 | 149 |
| 9 | GRASS | bybit>hyperliquid | bybit / hyperliquid | -2.30 | 100% | 30.0 | 0.00 | 0.0345 | 0.1091 | 0.0308 | 0.00002 | 1.94 | 2.67 | [1.49, 4.11] | 5.00 / 10.00 | 110,489 |
| 10 | SOON | bitget>gate | gate / bitget | 2.22 | 71% | 7.0 | 0.27 | 0.0333 | 0.1270 | 0.0290 | 0.00002 | 1.99 | 2.45 | [0.28, 4.96] | 5.00 / 3.72 | 7,370 |
| 11 | CAP | gate>okx | okx / gate | 2.14 | 62% | 4.0 | 0.18 | 0.0321 | 0.1629 | 0.0266 | 0.00002 | 2.30 | 1.98 | [1.13, 3.36] | 7.17 / 7.16 | 214 |
| 12 | LAB | bitget>okx | bitget / okx | -2.07 | 74% | 6.3 | 0.07 | 0.0311 | 0.1381 | 0.0264 | 0.00002 | 1.58 | 2.56 | [1.29, 4.58] | 5.00 / 0.05 | 3,278 |
| 13 | CASHCAT | gate>hyperliquid | gate / hyperliquid | -2.08 | 94% | 17.0 | 0.04 | 0.0312 | 0.1833 | 0.0250 | 0.00002 | 2.29 | 1.87 | [0.17, 4.92] | 1.56 / 10.00 | 1,195 |
| 14 | VVV | gate>hyperliquid | gate / hyperliquid | -1.92 | 100% | 30.0 | 0.00 | 0.0288 | 0.1311 | 0.0244 | 0.00002 | 2.40 | 1.44 | [1.03, 1.94] | 2.73 / 10.00 | 31,865 |
| 15 | USELESS | gate>hyperliquid | gate / hyperliquid | -1.94 | 92% | 10.0 | 0.16 | 0.0291 | 0.1182 | 0.0242 | 0.00002 | 1.83 | 2.04 | [0.64, 3.40] | 2.20 / 10.00 | 7,559 |
| 16 | AERO | bybit>hyperliquid | bybit / hyperliquid | -1.80 | 100% | 30.0 | 0.00 | 0.0271 | 0.1309 | 0.0227 | 0.00002 | 0.99 | 2.62 | [1.56, 4.23] | 5.00 / 10.00 | 25,150 |
| 17 | CHIP | hyperliquid>okx | okx / hyperliquid | 1.79 | 94% | 14.3 | 0.07 | 0.0269 | 0.1453 | 0.0220 | 0.00002 | 1.80 | 1.78 | [1.14, 2.68] | 10.00 / 4.31 | 265 |
| 18 | KSM | bybit>okx | okx / bybit | 1.76 | 87% | 5.0 | 0.25 | 0.0263 | 0.1878 | 0.0200 | 0.00002 | 1.96 | 1.55 | [1.02, 2.12] | 5.00 / 0.51 | 39,608 |
| 19 | MON | hyperliquid>okx | okx / hyperliquid | 1.61 | 99% | 29.7 | 0.01 | 0.0242 | 0.1414 | 0.0194 | 0.00002 | 1.30 | 1.93 | [1.41, 2.58] | 10.00 / 0.31 | 15,004 |
| 20 | NIL | gate>hyperliquid | gate / hyperliquid | -1.74 | 90% | 10.3 | 0.11 | 0.0262 | 0.2014 | 0.0194 | 0.00002 | 0.71 | 2.78 | [0.80, 5.71] | 0.08 / 10.00 | 17,483 |
| 21 | MEGA | bybit>hyperliquid | bybit / hyperliquid | -1.59 | 100% | 30.0 | 0.00 | 0.0239 | 0.1356 | 0.0193 | 0.00002 | 1.28 | 1.90 | [0.96, 2.78] | 5.00 / 10.00 | 35,683 |
| 22 | SKY | bybit>hyperliquid | bybit / hyperliquid | -1.51 | 99% | 22.0 | 0.02 | 0.0226 | 0.1213 | 0.0186 | 0.00002 | 0.68 | 2.34 | [1.24, 3.83] | 5.00 / 10.00 | 44,857 |
| 23 | BEAT | bybit>okx | bybit / okx | -1.48 | 72% | 7.3 | 0.00 | 0.0222 | 0.1116 | 0.0185 | 0.00002 | 1.55 | 1.42 | [0.71, 2.01] | 5.00 / 0.09 | 4,119 |
| 24 | GRAM | bybit>hyperliquid | bybit / hyperliquid | -1.53 | 88% | 14.0 | 0.12 | 0.0230 | 0.1368 | 0.0184 | 0.00002 | 0.97 | 2.09 | [1.18, 3.45] | 5.00 / 10.00 | 112,646 |
| 25 | 0G | bybit>hyperliquid | bybit / hyperliquid | -1.37 | 93% | 26.7 | 0.06 | 0.0206 | 0.1267 | 0.0163 | 0.00001 | 0.21 | 2.54 | [1.05, 4.25] | 5.00 / 10.00 | 35,103 |

Full 1,674-pair table: `spread_ranking.json` (`pairs[]`).

> **Addendum (long-history re-test):** see `LONGHISTORY_ADDENDUM.md`. Gate's funding
> endpoint is paginable past 30 days (page backwards on `to` to the contract launch), so
> the single-venue survivor `龙虾_USDT` was re-tested on **208 days** — it **survives**
> (positive both 104-day halves, OOS CI [+5.87, +15.48], funding never persistently
> negative, longest negative run 5d), but its edge is **regime-dependent** ($0.110/day
> long-run vs $0.035/day most-recent-30d) and, as a cross-venue pair, **not collectable at
> $100** (Gate-leg book depth $75.66 < $100).

## 7. Verdict

**How many symbols have a persistent cross-venue funding spread that clears all costs in
both halves?** — **325 pairs across 105 distinct bases** pass every gate (≥14d window,
≥60% same-sign, median-sign agreement, positive in both IS and OOS, OOS block-bootstrap CI
excluding zero, cost amortised within the window). This is **not zero** — the cross-venue
class is materially richer than single-venue carry, exactly because no spot leg is needed.

**At what $/day and $/min at $100?**

- **Best base: `BTW` (Bitget long / Bybit short) — $0.0815/day gross, $0.0775/day net**
  ($0.00006/min gross), mean spread 5.43 bps/8h, 74% same-sign, OOS CI [4.23, 13.92].
- The top 25 range **$0.0775 → $0.0163/day net** ($0.00006 → $0.00002/min gross).
- Median across all 325 candidates: **~$0.0073/day net**; 14 clear $0.03/day, 2 clear
  $0.05/day. Annualised at $100 the best base is ~$28/yr — the same order as the single-
  venue liquid survivor, but with **no spot-leg hedgeability constraint**.

**Capacity limit (how much notional before depth binds).** The spread is only as large as
the **shallower leg's book depth within ~50 bps of the touch** (measured live, `raw/*/book/`).
Capacity varies enormously:

- **Deep, capacity-irrelevant:** `EGLD` $133K, `GRAM` $113K, `GRASS` $110K — the $100
  trade is invisible to these books.
- **Thin, capacity-binds first:** `龙虾` (Longxia) **$76**, `2Z` $149, `CAP` $214, `CHIP`
  $265. **5 candidates have capacity below $100** — the $100 trade itself would already
  move the book, so their measured spread is not collectable at that size. `龙虾`'s $76
  depth makes its #6 ranking **notional, not achievable**.

**Beyond depth, the deeper capacity limit is the spread itself.** These are funding-rate
differences of 1.5–5.5 bps/8h; nothing here is arbitraged away by size because the
*mechanism* (a venue's funding formula) is not a resting order — but a spread this thin
means the real constraint is **fee tier and reachability**, not book depth. Gate's
**verified 7.5 bps taker** (vs the 5 bps modelled) removes ~2.5 bps/leg = ~5 bps per round
trip; on a 2–3 bps/8h spread that is the difference between a candidate and a reject for
the marginal rows. The **maker variant** (Gate maker = −1 bps rebate, Bitget 2 bps) is
where the economics actually live, and it is not modelled here.

## 8. Honest caveats

1. **Reachability is intermittent.** All 10 endpoints answered 5/5 *this run*; minutes
   earlier siblings saw Bybit/OKX/Bitget TCP-refused. A two-venue trade needs both venues
   reachable *simultaneously and continuously* for the hold — not demonstrated.
2. **Gate is under-charged by the project model.** API-verified 7.5 bps taker vs 5 bps
   assumed. Sensitivity re-run with verified fees still passes 324/325, but Gate-leg dollar
   figures are optimistic.
3. **Sign risk over the hold.** The spreads are persistent over 30 days at ≥60% same-sign,
   but the streak column shows some (e.g. `CAP` 4.0d, `BTW` 4.3d) flip far more often than
   the mean suggests. A regime flip mid-hold turns the carry negative.
4. **Execution is not modelled.** Entry/exit would cross the book twice per leg under real
   latency; the half-spread is charged but not market impact at size or partial fills.
5. **Funding-sign convention.** Income assumes the venue with positive funding is shorted
   and the negative one longed; if a venue's funding formula differs (impact-price vs
   premium-index), the captured rate is the venue's *charged* rate, which is what the
   history endpoint reports — consistent across venues here.

## 9. Evidence paths

- `evidence/arbitrage/2026-10-03/crossvenue_funding/spread_ranking.json` — 1,674 pairs, all fields
- `evidence/arbitrage/2026-10-03/crossvenue_funding/reachability.json` — per-attempt probe table (10 endpoints × 5 attempts)
- `evidence/arbitrage/2026-10-03/crossvenue_funding/run.log` — run transcript
- `evidence/arbitrage/2026-10-03/crossvenue_funding/raw/{gate,bybit,okx,bitget,hyperliquid}/` — cached contracts, funding history, order books
- `scripts/arb_funding_spread.py` — scanner; `tests/unit/test_arb_funding_spread.py` — 51 pure-computation tests
