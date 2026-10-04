# METHODOLOGY

This archive is a record of *measurement*, not of a trading system. This document
explains the three things a reader needs to judge the numbers: the **cost model**, the
**IS/OOS + block-bootstrap** protocol, and the **identity guards** that killed the three
artifacts in the README.

Nothing here is a strategy. There are no orders, no keys, no wallets, no account state.

---

## 1. Cost model

Every class is judged against the project's own cost model, not a hand-picked number. The
assumptions come from `engine/fees.py` (`FeeSchedule`) and
`engine/cost_model.round_trip_cost_pct`. A vendored subset of those three modules is in
[`scripts/_vendor/crypto_brain/engine/`](scripts/_vendor/crypto_brain/engine/) so the
scanners can be re-run offline.

### 1.1 Fee schedule (base tier — "VIP0")

| market | taker | maker |
|---|---:|---:|
| spot | 10.0 bps/leg | **10.0 bps/leg** |
| futures (perp) | 5.0 bps/leg | 2.0 bps/leg |

The spot **maker == taker == 10 bps** is deliberate: a $100 account is VIP0 with no maker
rebate, so there is no cheaper resting-fill rate to assume. The ~2 bps "maker" figure that
appears in the maker study is a high-VIP/rebate tier this account **cannot reach** and is
reported only as a labelled what-if — never as the verdict.

### 1.2 Round-trip cost formula

```
round_trip_cost_pct =
    2 * taker_fee_pct          # in and out
  + 2 * half_spread_pct        # entry and exit, measured from the live book
  + entry_slippage_pct
  + exit_slippage_pct
  + expected_funding_pct       # 0 for spot; rate * (hold / interval) for perps
```

Slippage uses the project's `engine/slippage.py`: a square-root impact model with
`k = 0.5`, and an **exit multiplier of 1.5×** the entry slippage (the "most important
realism knob" — strategies exit into weakness). Symbols with 24h quote volume below
`MIN_QUOTE_VOLUME_USD = $1,000,000` are excluded **structurally**, not priced (a symbol is
vetoed, not charged a bigger slippage number).

### 1.3 Which classes pay which legs

- **Triangular / cross-venue spot / stablecoin:** 2 spot legs → 30 bps RT at taker
  (spot taker 10 bps × 2 legs × in/out). A maker what-if at 2 bps/leg = 6 bps RT is shown
  but is *not achievable at $100*.
- **Funding carry (single-venue):** 1 spot leg + 1 perp leg → **30 bps taker / 24 bps
  maker** hedged RT under this model.
- **Cross-venue funding spread:** both legs are perps, so **4 fee events** (2 venues ×
  in/out) at the perp taker rate, plus the **measured half-spread on 4 crossings**.
- **CEX↔DEX:** CEX taker ×2 legs + a withdrawal/transfer cost + gas + a slippage buffer.
  At $100 the fixed transfer cost (120 bps) dominates; the pool fee and price impact are
  **already inside** the DEX executable quote and are never subtracted twice.
- **Meme cross-DEX:** the on-chain landing stack (base + priority + tip ≈ 1–2 bps at $100)
  plus the **pool fee round trip** (8–60 bps, mean 34) plus price impact — the pool fee is
  the dominant term.

### 1.4 Fee provenance — where the model under-charges

The project model is not always the truth, and the reports say so:

- **Gate perp taker** is API-verified at **7.5 bps** (`taker_fee_rate = 0.00075`), *higher*
  than the 5.0 bps modelled. Gate-leg dollar figures are therefore optimistic; a
  sensitivity re-run at the verified fees still passes 324 of 325 candidates, so the
  ranking is not an artifact of the under-charge.
- **Bitget perp taker** is API-verified at 6.0 bps. **Bybit / OKX / Hyperliquid** are
  unverified (no unauthenticated fee field); where published rates are known they are
  *lower* than modelled, so those venues are **over**-charged.
- Venue fee pages were unreachable from the measurement host for several venues, so those
  tiers are carried as **UNVERIFIED**.

---

## 2. IS/OOS split + block bootstrap

Funding-class claims (classes 4–5, 7, and the `龙虾` re-test) use one shared protocol so
that every study defines "significant" identically.

1. **Chronological midpoint split.** The series is split in half. The traded direction must
   be **positive in BOTH halves** (in-sample AND out-of-sample) — a sign flip in either
   half rejects the candidate outright.
2. **Moving-block bootstrap on the OOS half.** Block length **5** buckets, **2,000**
   resamples, seed **20261003**; the 95% CI must **exclude zero**. A moving block (not an
   i.i.d. bootstrap) preserves autocorrelation in funding series.
3. **Persistence gates, run before the economics** (a large mean with an unstable sign is
   not a carry):
   - window **≥ 42 buckets** (~14 days) — reject short, newly-listed pairs;
   - traded sign holds in **≥ 60% of buckets** — reject flip-flops;
   - **median sign == mean sign** — reject means driven by one outlier bucket.

Funding is bucketed to a UTC-aligned **8-hour** grid; each venue's native interval (Gate
1/4/8h, Bybit 8h, OKX 8h, Bitget 8h, Hyperliquid 1h, Kraken 1h) is **summed** into the 8h
bucket, so the spread is compared as **income over the same wall-clock window**, not as a
per-settlement rate.

### 2.1 Why the protocol matters — it is what caught artifact #3

A naive "CI excludes zero" filter alone reported 8 surviving pairs. Adding the programme's
**own same-sign and median-sign gates** revealed that every survivor had a
`kraken_futures` leg and a `same_sign_share` of 0.39–0.47 — **below the 0.60 bar**. The CI
was excluding zero because of a **venue-mechanism level offset**, not a token-specific
edge. The statistical test and the economic gate disagreed, and the economic gate wins.

### 2.2 Known limitations of the protocol

- **Windows are short.** ~30 days for funding (OOS halves ~15 days), 25–40 min for
  microstructure. Only the single `龙虾` re-test reaches 208 days.
- **No basis-drift term in the funding-only screens.** Where basis drift is measured it is
  material (−$0.23 to +$0.51 in the decisive study, comparable to the funding itself), so a
  funding-only screen is a **lower bound** on the true hurdle.
- **Regime dependence is real.** `龙虾` earns $0.110/day over 208 days but only $0.035/day
  in the most recent 30 — a long-run average is not what a current entrant experiences.

---

## 3. Identity guards

The single most important methodological result in this archive is that **unguarded
symbol matching fabricates order-of-magnitude edges**. Three guards are applied.

### 3.1 Cross-pool keying by `(chain, contract address)`

Two pools are only comparable if they hold the **same token on the same chain**, i.e. the
same contract address. Grouping by ticker is unsafe: three distinct `TRUMP` contracts
(0.03 / 2.34 / 5.05) collapsed into a fake ~100,000 bps spread, and the first-draft
headlines were **2,932 bps** (robinhood) and **1,287 bps** (ethereum CEX↔DEX). Correct
keying drops them to **493 bps** and **45 bps**. Pinned by test
(`test_same_symbol_different_contract_is_not_an_arb`).

### 3.2 CEX↔DEX `identity_verified` allowlist

A CEX↔DEX row is only valid when the pool's `(chain, address)` is a **known canonical
contract** for that symbol. An unknown address is *unverified*; the allowlist can never
**upgrade** a collision to verified. Pinned by test
(`test_unknown_or_wrong_contract_is_unverified`).

### 3.3 Venue-metadata symbol identity + price sanity

Deriving a base asset by **string-stripping** a venue symbol is unsafe. Stripping
`PF_USDTUSD` → `T` collided Kraken's USDT/USD stablecoin perp with Binance's Threshold
token (`TUSDT`) at a **184.67× price ratio**, producing a phantom $0.354/day "best" pair.
`instrument_identity.py` replaces string-stripping with each venue's **own metadata**
(`base` / `quote` / `category` / `type`) plus a **price-sanity check**; 1,251 of 3,220
pairs failed the guard, including 2 of the report's top 12. Verdict on the phantom:
**FALSIFIED**, corrected net $/day **0.0** (non-executable — the two legs are different
assets).

### 3.4 Partition test

When a "significant" set splits **perfectly by venue**, that is a mechanism test, not a
signal. `arb_partition_test.py` re-derives the CI-exclusion partition **from raw funding**
(not from stored CI fields), reports per-venue funding levels, the same-sign share, and the
share of total income contributed by the top 5% of buckets, then applies the programme's own
gates. Its output is [`evidence/partition/partition_report.json`](evidence/partition/partition_report.json).

---

## 4. Reachability as an operational caveat

Venue reachability from the measurement host was **intermittent**: the same hosts answered
**5/5 HTTP 200** and **HTTP 000** minutes apart; several venues (Bybit, OKX, Bitget,
Deribit, Kraken, Binance futures) were TCP-refused or DNS-sinkholed to `202.169.44.80` in
some runs and reachable in others. This is reported as a **material operational caveat for
any two-venue strategy**: a strategy that needs *simultaneous, continuous* access to both
legs cannot be assumed to have it — a venue can vanish mid-hold.

---

## 5. What would change the answer

Honest counterfactuals, not promises:

1. **A maker-rebate tier + colocation + $100k+ capital.** The maker/rebate economics and
   the cross-venue funding spreads are thin precisely *because* the fee tier is retail. This
   is a **different account**, not a tweak to this one.
2. **A genuine depeg wider than fees and lasting longer than round-trip latency.** The
   stablecoin class is a **rare-event bet**: no depeg occurred in the measured window.
3. **A different regime.** Higher funding, wider dislocations or a less efficient venue set
   could change the numbers — but this evidence is regime-specific and says so.
