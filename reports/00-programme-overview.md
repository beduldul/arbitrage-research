# ARBITRAGE PROGRAMME REPORT — 2026-10-03

**Permanent record of the entire arbitrage research programme run on 2026-10-03.**
Measurement only. No orders, no keys, no wallets, no transfers, no `.env` / gate /
config / pipeline edits. Public unauthenticated endpoints only. Every number below
is traceable to one of the source files listed in §5; where two sources disagree the
disagreement is reported in §7 rather than silently resolved.

- **Capital basis:** $100.00 notional (the project's retail account).
- **Fee basis (headline):** project base tier — spot taker 10 bps/leg, perp taker
  5 bps/leg, maker 2 bps/leg, slippage k=0.5 exit ×1.5 (`engine/fees.py`,
  `engine/cost_model.round_trip_cost_pct`).
- **Venues reached:** Binance Vision (spot mirror + funding dumps), Gate.io, HTX,
  Hyperliquid, Bybit, OKX, Bitget, Jupiter (Solana DEX). Several venue families were
  TCP/DNS-blocked from this host (see §7.3).
- **Sources of truth:** the eight reports, the paper simulator output, and the
  reverse-engineering script listed in §5.

---

## 1. Executive verdict

Across eight arbitrage classes and one public performance claim, **the programme
found no tradeable retail edge at $100**. Every class is defeated by the same thing:
the fee/spread floor, not the signal. Where a positive gross dislocation exists it is
either smaller than the round-trip cost (cross-venue spot: max +11.0 bps vs a ≥20 bps
floor; CEX-DEX native: 1–3 bps vs 150.5 bps), or it is not an arbitrage at all but a
structural basis the market is paying you to hold (bridged-token CEX-DEX discount,
41–100 bps = bridge/redemption risk), or it is real but economically trivial and
capacity-bound (funding carry: $0.02–$0.08/day per $100, and the best-ranked names
have book depth below the $100 trade). The one class that is *statistically* rich —
cross-venue funding spread, 325 of 1,674 pairs passing every persistence gate — is
**not collectable at $100** for its top-ranked names (the #6-ranked `龙虾` has a
Gate-leg capacity of $75.66 < $100), and its median net income is ~$0.0073/day. The
paper carry simulator, run in REAL mode against real Gate funding, returned a
**negative** $/day. The public "$0.4–$0.7/min" claim normalises to $576–$1,008/day,
which on $100 would require 576–1,008%/day — arithmetically impossible; the claim is
consistent only with large, fee-advantaged, colocated capital that a $100 account is
not, and no verifiable track record (on-chain address / read-only API / audited
statement) was produced to support it.

| # | Class | Measured edge | Cost floor | Net | Verdict |
|---|---|---|---:|---:|---|
| 1 | Triangular (spot) | best mean net −28.5 bps; 0 of 100 sampled triangles (of 354 discovered) ever positive | 30 bps RT taker (6 bps maker what-if) | negative | **NO** |
| 2 | Stablecoin cross-rate / depeg | round trips −20.1 to −30.0 bps; widest genuine spread Gate TUSD ~10 bps; no depeg in window | 20 bps RT taker | negative | **NO** (rare-event bet only) |
| 3 | Cross-venue spot (same pair, 2–3 venues) | max gross dislocation +11.0 bps; 0 of 30 directions net-positive | ≥20 bps RT (2× taker) | negative | **NO** |
| 4 | Single-venue funding carry | best hedgeable ZROUSDT $0.0221/day; decisive survivors $0.040–$0.070/day; `龙虾` 208d $0.1096/day long-run but $0.0352/day recent-30d | 30 bps taker / 24 bps maker hedged RT (project model) | ~$0.02–$0.07/day per $100 | **marginal / illiquid-corner only** |
| 5 | Cross-venue funding spread | 325 of 1,674 pairs pass; best BTW $0.0815/day gross, $0.0775/day net; median candidate ~$0.0073/day net | 4 fee events (2 perp RTs) + measured half-spreads | tiny; top-ranked capacity-bound | **statistically real, not collectable at $100 for top names** |
| 6 | Maker spread capture | best VIP0 case −9.64 bps/fill; median spread < fee on 22/24 pairs | VIP0 maker == taker == 10 bps/leg | negative everywhere | **NO at VIP0 (0/24); 2 bps tier 1/24 = NOT ACHIEVABLE** |
| 7 | CEX-DEX native/deep | 1–3 bps gap (SOL, USDT/USDC) | 150.5 bps @$100 (42.05 @$1,000) | negative | **NO** |
| 7b | CEX-DEX bridged | 41–100 bps "gap" | 150.5 bps @$100 | structural basis, not mispricing | **NOT an arbitrage** — bridge-risk basis, captured by MEV searchers |
| 8 | Public "$0.4–0.7/min" claim | $576–$1,008/day required | — | 576–1,008%/day on $100 | **impossible for a $100 account; unverifiable as stated** |

**One-line summary:** *the retail fee floor, not the absence of signal, is what
defeats every class; the only statistically rich class is a funding spread that is
real, tiny, and capacity-bound below the trade size.*

---

## 2. PRECISION RULES (mandatory — this report is the permanent record)

These phrasings are binding on any downstream citation of this programme.

### 2.1 Triangular (spot)

Write exactly: **"0 of 100 sampled triangles (of 354 discovered) ever showed positive
net edge."** Do **not** write "0/354" — only 100 of the 354 discovered triangles were
sampled, and the claim is about the sampled set, not the discovered set.

- Source: `triangular/run_20261003T060859Z/summary.json` — `n_discovered_triangles`
  354, `n_sampled_triangles` 100, `triangles_ever_positive_taker` 0,
  `triangles_ever_positive_maker` 0, `sweep_positive` 0.
- Run: 400 cycles over 40.0 min @ 6 s, 302 books present, 33,903 latency samples
  (median 95.8 ms, p95 156.2 ms), 0 rate-limits. Fee 10 bps/leg taker = 30 bps RT;
  maker what-if 2 bps/leg = 6 bps RT.
- Best sampled case (BTC>USDC): mean net −29.821 bps, max −28.482 bps, 0% positive,
  max executable size $100,000. Even the maker what-if stays negative (mean −5.926 bps).
- Capturability caveat (quoted from the run's own `capturability_caveat`): a positive
  net-edge *sample* over REST is not evidence of capturable profit — triangular edges
  live for microseconds-to-milliseconds while measured REST round-trip is
  ~96–156 ms. "This measures flicker, not profit."

### 2.2 Cross-venue funding spread

Write exactly: **"325 of 1,674 pairs pass the statistical gates."** Then **immediately**
state that statistical persistence ≠ collectability:

- The **#6-ranked** base **`龙虾` (Longxia, Bitget↔Gate)** has **Gate-leg capacity
  $75.66 < $100** — the $100 two-leg position would already exceed the resting
  liquidity on the Gate leg at that price band. **Its spread is not collectable at
  that size; the #6 ranking is notional, not achievable.**
- **5 candidates have capacity below $100:** `BOME` (Gate↔HL) $32.57,
  `BOME` (HL↔OKX) $57.96, `CHIP` (Gate↔HL) $62.56, `SKY` (Gate↔HL) $73.36,
  `龙虾` (Bitget↔Gate) $75.66.
- Source: `crossvenue_funding/REPORT.md` §6–§7, `spread_ranking.json`
  (`n_pairs` 1674, `n_candidates` 325, `n_constructible` 1668).
- Full rejection breakdown: 1,199 rejected as flip-flops, 47 not positive in both
  halves, 45 cost not amortised in-window, 44 OOS CI includes zero, 8 too-short window.
- Best base `BTW` (Bitget long / Bybit short): $0.0815/day gross, $0.0775/day net,
  mean spread 5.43 bps/8h, 74% same-sign, OOS CI [4.23, 13.92]. Median across all 325
  candidates ≈ **$0.0073/day net**; 14 clear $0.03/day, 2 clear $0.05/day.

### 2.3 `龙虾_USDT` single-venue carry (208-day re-test)

State that on a **208-day** Gate history (2026-03-10 → 2026-10-03, the contract's full
life) it **survives** — **positive in both halves, OOS CI [+5.87, +15.48]**, funding
never persistently negative (13 negative days = 6.2%, longest consecutive negative run
5 days, worst day −2.9 bps) — **BUT** its edge is **regime-dependent**: **$0.110/day
long-run vs $0.035/day in the most recent 30 days**, and it is a **thin, illiquid-corner
name**. Present it as **"thin, illiquid, regime-sensitive"**, **never as a validated
strategy**.

- Source: `crossvenue_funding/LONGHISTORY_ADDENDUM.md`. 1,240 funding rows, 4h native
  interval, no duplicate timestamps. Gross funding $22.352, basis drift +$0.610
  (small), fees −$0.175, net $22.786 = **$0.1096/day** over 208 days; one-day-trimmed
  $0.1015/day; recent-30d **$0.0352/day**; IS (104d) +$12.751 ($0.1226/day),
  OOS (104d) +$10.035 ($0.0965/day). Gate fees verified: spot taker 0.10%, perp taker
  0.075%.
- The original ~$0.047/day (30-day decisive study) sits between the long-run and the
  recent-30d figure.

### 2.4 Maker spread capture

- The verdict is the **VIP0** case (**maker == taker == 10 bps at $100**) = **0 of 24
  venue:pairs positive** at any measured horizon. The **best VIP0 case is
  `gate:AVAXUSDT` at −9.64 bps/fill**; the median quoted spread is below the fee on
  **22 of 24** pairs, and **no** pair has a median spread above 2× the fee (20 bps).
- The **2 bps column (1 of 24 positive — `htx:LINKUSDT`)** must be labelled
  **"NOT ACHIEVABLE at $100 (high-VIP/rebate tier)"**. A $100 account is VIP0 with no
  rebate; this is exactly why DESIGN.md §10.2 sets `spot_maker_bps == spot_taker_bps`.
- Adverse selection must be labelled **"COARSE PROXY, LOWER BOUND"**: measured REST
  round-trip latency is ~100–465 ms while adverse selection acts at
  sub-millisecond-to-second scale, so every adverse number **understates** the true
  toxic cost; every positive figure is therefore an *upper* bound on real edge.
- Zero through-fills were observed for `htx:DOGEUSDT` and `htx:XRPUSDT` — that is
  absence of a crossing print, **not** a positive result.
- Source: `maker/MAKER_REPORT.md`. Window 25.0 min, 747 ticks @ 2.0 s, 8 pairs × 3
  venues, 47,375 tape rows, 0×429 / 0×5xx, REST latency mean 152.2 ms p95 328.9 ms.

### 2.5 CEX-DEX (native + bridged)

- **Native/deep** (SOL, USDT/USDC): gap **1–3 bps** (group median 2.01 bps @$100,
  max 3.11) against a **150.5 bps $100 cost stack** (20 bps CEX fee + 120 bps
  withdrawal + 0.5 bps gas + 10 bps slippage buffer). At $100 the fixed transfer fee
  alone (120 bps) is more than twice the widest gap ever seen.
- The **bridged-token 41–100 bps gap** (ARB median 42.6 bps, max 100.47 bps @$1,000)
  is a **bridge-risk basis, not a mispricing** — a persistent discount (ARB per-sample
  std ~2 bps on a ~41 bps mean) that compensates for bridge/redemption and
  wrapped-token liquidity risk, **captured by MEV searchers** with colocated
  infrastructure and private orderflow that a retail HTTP client cannot approach
  (cited: arXiv:2507.13023). Say so; do not present it as a retail opportunity.
- Scope limit: the DEX side is **Solana-only** via Jupiter (both EVM aggregators, 1inch
  and 0x, returned 401 without an API key).
- Source: `dex/REPORT.md`; 950 pooled samples, 25.2 min @ 1 s; cost stack
  150.5 bps @$100 / 42.05 bps @$1,000 (30.5 / 30.05 bps if inventory is pre-positioned
  on both sides).

### 2.6 Reachability

State that reachability is **intermittent**: the *same hosts* answered **5/5 OK** and
were **HTTP 000** minutes apart. This is an **operational caveat for any two-venue
strategy** — a strategy that needs *simultaneous, continuous* access to both legs
cannot be assumed to have it, because a venue can vanish mid-hold.

- `crossvenue_funding/reachability.json`: all 10 endpoints (Gate, Bybit, OKX, Bitget,
  Hyperliquid) answered **200 on 5/5 attempts** this run; a sibling worker saw
  Bybit/OKX/Bitget **TCP-refused (HTTP 000)** minutes before.
- `MULTIVENUE_REPORT.md`: Bybit, OKX, Bitget, Deribit, Kraken, Binance futures all
  **HTTP 000** from this host; Deribit/Kraken resolved to the ISP sinkhole
  **202.169.44.80**.
- `dex/REPORT.md`: Bybit/OKX/Bitget were reachable *this run* having been blackholed
  for the sibling worker.

---

## 3. The "$0.4–0.7/min" public claim

Reverse-engineered by `scripts/arb_claim_math.py` (run once, output captured below).

### 3.1 Normalisation

| claim | /hour | /day | /month (30d) | /year (365d) |
|---|---:|---:|---:|---:|
| $0.40/min | $24.00 | **$576.00** | $17,280 | $210,240 |
| $0.70/min | $42.00 | **$1,008.00** | $30,240 | $367,920 |

**Headline:** on $100, $0.40/min is **576%/day** and $0.70/min is **1,008%/day**.
Compounded annually that is not a rate; it is a contradiction.

### 3.2 Required capital (capital = daily_target / daily_return)

| net return | daily rate | cap for $0.4/min | cap for $0.7/min |
|---|---:|---:|---:|
| 5%/yr | 0.0134% | $4,308,777 | $7,540,360 |
| 20%/yr | 0.0500% | $1,152,840 | $2,017,469 |
| 50%/yr | 0.1111% | $518,228 | $906,898 |
| 100%/yr | 0.1901% | $303,024 | $530,293 |
| 200%/yr | 0.3014% | $191,081 | $334,391 |
| 500%/yr | 0.4921% | $117,049 | $204,836 |
| 5%/day | 5.0000% | $11,520 | $20,160 |
| 20%/day | 20.0000% | $2,880 | $5,040 |

### 3.3 Required volume / edge (notional/min = target / net_edge)

For target **$0.40/min** (`*` = gross edge within the measured 1–11 bps band):

| net edge | notional/min | notional/day | taker | maker | rebate |
|---|---:|---:|---:|---:|---:|
| 1.0 bp | $4,000 | $5,760,000 | 21.0 bps | 5.0 bps* | 0.0 bps* |
| 5.0 bp | $800 | $1,152,000 | 25.0 bps | 9.0 bps* | 4.0 bps* |
| 10.0 bp | $400 | $576,000 | 30.0 bps | 14.0 bps | 9.0 bps* |
| 20.0 bp | $200 | $288,000 | 40.0 bps | 24.0 bps | 19.0 bps |
| 50.0 bp | $80 | $115,200 | 70.0 bps | 54.0 bps | 49.0 bps |

For target **$0.70/min**: 1.0 bp → $7,000/min ($10,080,000/day); 5.0 bp → $1,400/min;
10.0 bp → $700/min; 20.0 bp → $350/min; 50.0 bp → $140/min.

*Interpretation:* at the project's measured 1–11 bps gross dislocations against a
20–30 bps fee floor, only a **maker/rebate** tier can make a 1–5 bp net edge
*arithmetically reachable* — and those tiers are exactly the ones a $100 account
cannot obtain. **Volume cannot fix a negative net edge.**

### 3.4 Mechanism table with prerequisites

| mechanism | prerequisites | $100 retail? |
|---|---|---|
| CEX-DEX arb, colocated + private mempool | colocated node/RPC, private tx relay, on-chain capital, gas war budget | **no** (latency in ms; $100 cannot pay one month of colocation) |
| MEV searcher (EVM/Solana) | validator/relay relationships, bundle-building infra, refundable stake | **no** (profit accrues to block-winners, not a retail taker) |
| Market making at scale, maker rebates | inventory across venues, quoting engine, negative-maker tier | **no** (rebates only above very high 30-day volume) |
| Funding/basis at scale | $1M+ perp notional, cross-venue hedges, margin buffers | **no** (measured ~$0.05/day per $100 = $0.000035/min; needs $1,152,000 for $0.4/min) |
| Cross-exchange latency arb | pre-positioned balances on many venues, low-latency links, fee tiers | **no** (capital split across venues lowers effective notional) |
| Prop-desk / institutional fee tiers | high-volume tier (negative maker), colocation, risk desk | **no** (the negative fee *is* the edge) |

### 3.5 What would make such a claim verifiable — and why absence is evidence

A public per-minute earnings claim becomes checkable only with at least one of:

1. a **public on-chain address** whose inflows match the claim;
2. a **read-only exchange API key** showing a live track record;
3. a **third-party audited statement**, or an **exchange-issued volume/fee tier**.

**None was produced.** Without any of these, the claim is marketing / a course /
someone else's dashboard. **The absence of verifiability is itself the evidence.**

---

## 4. What WOULD change the answer

Honest counterfactuals — the conditions under which the conclusion above flips:

1. **Maker rebate tier + colocation + $100k+ capital.** The maker/rebate economics
   (§3.3) and the cross-venue funding spreads (§2.2) are thin precisely because the
   fee tier is retail. A negative-maker rebate, verified venue fees, and colocated
   low-latency execution remove the fee floor that defeats every class. This is a
   *different account*, not a tweak to this one.
2. **A genuine depeg event wider than fees and lasting longer than round-trip
   latency.** The stablecoin class is a *rare-event bet*: no depeg occurred in the
   28-minute window, and a 20 bps round trip needs a depeg **wider than 20 bps** that
   persists long enough to execute. That is possible; it is not an edge, and it is not
   something this measurement observed.
3. **Longer / other regimes.** The `龙虾` 208-day re-test already shows the per-day
   figure is regime-dependent ($0.110/day long-run vs $0.035/day recent-30d), and the
   cross-venue spreads are measured on ~30-day windows with OOS halves of ~15 days.
   A different regime (higher funding, wider dislocations, a less efficient venue set)
   could change the numbers — but the programme's evidence is regime-specific and says
   so.

**What is NOT claimed:**

- **No profit promise.** Nothing here establishes a positive expected return for any
  account size or fee tier.
- **No validated strategy.** The only statistically surviving carry name (`龙虾`) is
  presented as *thin, illiquid, regime-sensitive*, not as validated.
- **No claim that the classes are impossible in principle** — only that they are not
  net-positive for a $100 retail taker on the fee schedule and venues measured here.

---

## 5. Reproduction index

Every number in this report can be re-derived from the artifacts below. No
measurement is re-run by this document; it cites the runs.

### 5.1 Scripts (`scripts/`)

| script | produces |
|---|---|
| `arb_common.py` | shared fetch/backoff/depth-walk helpers |
| `arb_triangular_scan.py` | triangular sweep (`triangular/run_*/`) |
| `arb_stable_crossrate.py` | stablecoin cross-rate / depeg (`stable/`) |
| `arb_crossvenue_spot.py` | cross-venue spot (`crossvenue/`) |
| `arb_funding_scan.py` | single-venue Binance funding scan (`REPORT.md`, `funding_ranking.json`) |
| `arb_funding_multivenue.py` | Gate + Hyperliquid funding (`MULTIVENUE_REPORT.md`, `multivenue_ranking.json`) |
| `arb_funding_decisive.py` | hedged carry + basis drift + IS/OOS (`DECISIVE_REPORT.md`, `decisive_carry_results.json`) |
| `arb_funding_spread.py` | cross-venue funding spread (`crossvenue_funding/REPORT.md`, `spread_ranking.json`) |
| `arb_longxia_longhistory.py` | `龙虾` 208-day re-test (`crossvenue_funding/longxia/longxia_longhistory.json`) |
| `arb_maker_scan.py` | maker spread capture (`maker/MAKER_REPORT.md`, `maker/maker_results.json`) |
| `arb_dex_scan.py` | CEX-DEX Solana scan (`dex/REPORT.md`) |
| `arb_paper_sim.py` | paper carry simulator (`sim_funding.json`) |
| `arb_claim_math.py` | $0.4–0.7/min reverse-engineering (§3) |

### 5.2 Tests (`tests/unit/`)

`test_arb_carry.py`, `test_arb_claim_math.py`, `test_arb_costs.py`,
`test_arb_decisive.py`, `test_arb_dex.py` (26 pass), `test_arb_funding.py`,
`test_arb_funding_spread.py` (51 pure-computation tests; 55 incl. `funding_regime`),
`test_arb_maker.py`, `test_arb_multivenue.py`, `test_arb_paper_book.py`,
`test_arb_simulator.py`, `test_arb_stable.py` (19 pass), `test_arb_triangular.py`.

### 5.3 Evidence paths

```
evidence/arbitrage/2026-10-03/REPORT.md                       (Binance funding, 777 perps)
evidence/arbitrage/2026-10-03/MULTIVENUE_REPORT.md            (Gate+HL, 762 perps)
evidence/arbitrage/2026-10-03/DECISIVE_REPORT.md              (hedged carry + basis + IS/OOS)
evidence/arbitrage/2026-10-03/stable_crossvenue_REPORT.md     (stable depeg + cross-venue spot)
evidence/arbitrage/2026-10-03/crossvenue_funding/REPORT.md    (funding spread, 1674 pairs / 325 pass)
evidence/arbitrage/2026-10-03/crossvenue_funding/LONGHISTORY_ADDENDUM.md (龙虾 208d)
evidence/arbitrage/2026-10-03/maker/MAKER_REPORT.md           (maker capture + adverse selection)
evidence/arbitrage/2026-10-03/dex/REPORT.md                   (CEX-DEX Solana)
evidence/arbitrage/2026-10-03/sim_funding.json                (paper carry, REAL mode)
evidence/arbitrage/2026-10-03/triangular/run_20261003T060859Z/summary.json (354/100/0)
evidence/arbitrage/2026-10-03/multivenue_ranking.json         (762 objects)
evidence/arbitrage/2026-10-03/decisive_carry_results.json
evidence/arbitrage/2026-10-03/crossvenue_funding/spread_ranking.json (1674 pairs)
evidence/arbitrage/2026-10-03/crossvenue_funding/reachability.json   (10 endpoints × 5 attempts)
evidence/arbitrage/2026-10-03/maker/maker_results.json
evidence/arbitrage/2026-10-03/dex/dex_reanalysis_20261003T082345Z.json
evidence/arbitrage/2026-10-03/reachability.json               (Binance endpoint matrix)
evidence/arbitrage/2026-10-03/stable/  crossvenue/  maker/raw/  dex/raw_quotes_*.json
```

### 5.4 Prior work (cited, not copied)

- `~/back/FUNDING_OR_ALT.md` — funding data obtainability + 21-symbol carry test
  (negative both IS/OOS halves; best maker case −3.03 bps/trade).
- `~/back/COST_FLOOR.md` — VIP0 taker fee verified at 5.0 bps/leg (not the code's
  4.0); corrected RT 12 bps incl. slippage; after the expanded-universe re-test, no
  per-trade edge anywhere in the project is net-positive at corrected VIP0.
- `~/polymarket/PROFIT_REALITY.md` — the same structural finding on Polymarket
  (YES+NO ask sum minimum 1.0010 vs the <1.000 needed).

### 5.5 Paper simulator detail

`sim_funding.json`: `mode = REAL`, `tier = vip0_taker`, `capital = 100.0`,
`total_net_pnl = −0.40896`, `dollars_per_day = −0.04090`, 29 pairs, 870 settlements
replayed, `coverage_days = 10.0` (warning: requested 30d, evidence covers 10d;
`$/day` uses actual coverage). `positive_settlement_share = 0.8`. Assumptions:
basis held flat (no price history in the funding evidence → basis PnL is 0 **by
construction, not by measurement**); isolated margin on the short perp only.

---

## 6. Reconciliation with prior work

The programme **extends** rather than contradicts the prior studies:

- The prior 21-symbol carry test was negative; the 777-perp tail scan asked whether
  *any* symbol flips the sign. Answer: the survivors are illiquid corners, and the
  single liquid survivor (`龙虾`) yields ~$0.05/day per $100 before execution risk.
  The structural finding is unchanged.
- **Cost-model note (important):** the prior doc charged a blended 20 bps taker /
  12 bps maker hedged round trip; this programme's own S9 model charges **30 bps taker
  / 24 bps maker** (spot taker 10 bps on *both* spot legs). The programme's bar is
  **stricter**, so agreement between the two is agreement under a harder bar.
- The `COST_FLOOR.md` finding that the VIP0 taker fee is 5.0 bps/leg (not the code's
  4.0) is consistent with this programme's fee basis and with the cross-venue funding
  study's API-verified Gate perp taker of 7.5 bps (higher than the 5 bps project
  default — see §7.1).

---

## 7. Source disagreements and caveats (reported, not resolved)

### 7.1 Gate perp taker fee: project default 5 bps vs API-verified 7.5 bps

`crossvenue_funding/REPORT.md` §3 and `DECISIVE_REPORT.md` both record that Gate's own
API exposes `taker_fee_rate = 0.00075` (**7.5 bps**) on every USDT perp, whereas the
project model assumes **5.0 bps**. The programme's Gate-leg dollar figures are
therefore **optimistic by ~2.5 bps/leg**. A sensitivity re-run charging Gate 7.5 bps
and Bitget 6.0 bps still leaves **324 of 325** candidates clearing the cost gate, so
the ranking is not an artifact of the under-charge — but the Gate-leg numbers are
~0.5–1.0 bps/leg better than reality. Gate's maker rate is a **−1 bps rebate** (API
`maker_fee_rate = −0.0001`), so a maker variant is materially cheaper and is **not
modelled**.

### 7.2 `龙虾` 208-day addendum: internal "99-day" vs "104-day" halves

`LONGHISTORY_ADDENDUM.md` §3 says "positive in both **99-day** halves" while its own
table and §2 say IS/OOS are **104 days** each (208 total). The 104-day split is the
one consistent with the reported totals; the "99-day" phrasing appears to be a slip.
**Reported here rather than silently picked.**

### 7.3 Reachability disagreement (intermittent, not stable)

Different runs on the same day reached different venue sets: `MULTIVENUE_REPORT.md`
records Bybit/OKX/Bitget as HTTP 000; `crossvenue_funding/REPORT.md` and `dex/REPORT.md`
record them reachable. Both are true at different minutes — the operational caveat in
§2.6. No single "reachable venue list" is stable across the programme.

### 7.4 Suite counts changed during the session

`stable_crossvenue_REPORT.md` §6 recorded **2148 passed / 4 failed / 4 skipped**
(the 4 failures in `tests/integration/test_phase2_multipair.py`, attributed to a
sibling workstream, not this programme). `dex/REPORT.md` §6 recorded **2285 passed /
4 skipped / 0 failed** by end of session. The final state is reported in §8 below.

### 7.5 Standing caveats that apply to every positive number

1. **No basis-drift term in the funding-only screens** — the funding-only bar is a
   *lower bound* on the true hurdle (basis drift measured up to ±9,770 bps aggregate
   in the prior study; in the decisive study it ranged −$0.23 to +$0.51, comparable to
   the funding itself).
2. **Windows are short** — 25–40 min for microstructure classes, ~30 days for funding
   (OOS halves ~15 days), 208 days only for the single `龙虾` re-test.
3. **Venue fees unverified where the fee pages are unreachable** (Binance/Gate/HTX fee
   pages HTTP 000 from this host); where verified, the project model under-charges.
4. **Execution is not modelled** — latency, market impact at size, partial fills, and
   funding-sign regime flips mid-hold are all unmodelled or proxied.
5. **Adverse selection in the maker study is a lower bound** (§2.4).

---

## 8. Test-suite status

`uv run pytest -o addopts="" -q 2>&1 | tail -3` was run at the end of this writing
task with **no code changes** and **no gate/config/.env edits**. Actual final counts:

```
2286 passed, 4 skipped in 32.42s
```

i.e. **2286 passed, 4 skipped, 0 failed**. The 4 skips are the opt-in live-network
tests. No test was modified, focused, or skipped by this workstream.

**Disagreement with the brief:** the brief expected "2285 passed"; the actual run is
**2286 passed, 0 failed**. Reported as measured rather than forced to match. The
`dex/REPORT.md` §6 figure of 2285 passed / 4 skipped was itself taken mid-session; the
suite count moved by +1 between that capture and this one (sibling workstreams were
active in `tests/integration/` during the day — see §7.4).

---

## Appendix — meme-coin arbitrage (2026-10-03, later same day)

This appendix is **added after** the body above and does not rewrite it. The meme
workstream re-tested the fee-floor thesis on-chain, where the CEX taker and withdrawal
fees do not apply. Full report:
[`MEME_ARBITRAGE_REPORT.md`](./MEME_ARBITRAGE_REPORT.md).

**Class table (new rows):**

| # | Class | Measured edge | Cost floor | Net | Verdict |
|---|---|---|---:|---:|---|
| 9 | Solana meme cross-DEX (90 venue-pairs) | **0 of 90 cross-venue venue-pairs ever net-positive**; widest gross **4.78 bps**; best net **−5.43 bps** | pool fee 8–60 bps RT (mean 34) + on-chain 1–2 bps + impact <5 bps = **≈35–60 bps required** | negative; **$0.0000/day** at $100 | **NO** |
| 10 | Multi-chain meme cross-pool (22 chains measured, 8 with candidates) | positives only in pools **≤$70 k**; capacity **$76–$577**; **1.3–14.6 %** of samples | per-chain gas (EVM $0.002–$0.028 measured) + pool fee + impact | capacity-bound, transient | **NOT a strategy** (capacity-bound + transient) |
| 11 | CEX↔DEX meme (282 verified samples, 6 canonical contracts) | **0.0 % net-positive**, best **−7.2 bps** | 10 bps CEX taker + 10 bps buffer + pool fee + gas | negative | **NO** |

**Cross-cutting finding (methodological):** unguarded **symbol grouping** produced
phantom "edges" — three distinct `TRUMP` contracts on ethereum at 0.03 / 2.34 / 5.05
gave a **2,932 bps** robinhood headline and a **1,287 bps** ethereum CEX↔DEX headline;
keying by `(chain, contract address)` and the CEX↔DEX `identity_verified` allowlist
dropped these to **493 bps / 45 bps**. A collision is a fabricated order-of-magnitude
edge, not a small bias.

**MEV-race finding:** **Jito = 98.8 %** of staked SOL; SWQoS reserves **80 %** of leader
TPU capacity for staked connections, leaving retail in the **20 %** lane at measured
**100–460 ms** round-trip vs sub-30 ms searchers → **20 % capture at best, often <10 %**.

**Precision rules for these rows:** write "0 of 90 cross-venue venue-pairs ever
net-positive"; "widest gross 4.78 bps vs a 35–60 bps required bar"; the multi-chain
cross-pool positives are **capacity-bound ($76–$577) and transient (1.3–14.6 % of
samples), NOT a strategy**; the simulator's $/day figure is **SYNTHETIC-fixture
arithmetic only**; the collision artifact must be noted explicitly.
