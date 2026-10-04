[![CI](https://github.com/beduldul/arbitrage-research/actions/workflows/ci.yml/badge.svg)](https://github.com/beduldul/arbitrage-research/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue?style=flat-square)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![Research: negative results](https://img.shields.io/badge/research-negative%20results-orange?style=flat-square)](METHODOLOGY.md)

# Arbitrage research — measured, and mostly negative

**The question.** Does a $100 retail account have any tradeable arbitrage edge in crypto?

**What was measured.** 10+ arbitrage classes — triangular, stablecoin depeg/cross-rate,
cross-venue spot, single-venue and cross-venue funding carry, maker spread capture,
CEX↔DEX (native and bridged), Solana and multi-chain meme cross-DEX, and a public
"$0.4–0.7/min" earnings claim — on **real public order books**, against the project's own
fee/slippage cost model, at a **$100 notional basis**.

**The verdict.** No class is net-positive for a $100 retail taker. The blocker is the
**retail fee/spread floor**, not the absence of signal: where a gross dislocation exists it
is smaller than the round-trip cost, or it is not an arbitrage at all but a structural
basis (bridge risk), or it is statistically real but economically trivial and
capacity-bound below the trade size.

**The selling point is the methodology.** Along the way the work **caught three of its own
measurement artifacts** before they became false claims (see [Methodology](#methodology--three-artifacts-we-caught-on-ourselves)).

> **Paper only.** Measurement only. No orders, no keys, no wallets, no transfers, no
> account state. Public unauthenticated endpoints only. Every number is traceable to a
> file in [`evidence/`](evidence/) and reproducible from [`scripts/`](scripts/).

---

## Verdict table

| # | Class | Measured edge | Cost floor | Net | Verdict |
|---|---|---|---:|---:|---|
| 1 | Triangular (spot) | best mean net −28.5 bps; **0 of 100 sampled triangles (of 354 discovered)** ever positive | 30 bps RT taker (6 bps maker what-if) | negative | **NO** |
| 2 | Stablecoin cross-rate / depeg | round trips −20.1 to −30.0 bps; widest genuine spread Gate TUSD ~10 bps; **no depeg in window** | 20 bps RT taker | negative | **NO** (rare-event bet only) |
| 3 | Cross-venue spot (same pair, 2–3 venues) | max gross dislocation +11.0 bps; 0 of 30 directions net-positive | ≥20 bps RT (2× taker) | negative | **NO** |
| 4 | Single-venue funding carry | best hedgeable ZROUSDT $0.0221/day; decisive survivors $0.040–$0.070/day | 30 bps taker / 24 bps maker hedged RT | ~$0.02–$0.07/day per $100 | **marginal / illiquid-corner only** |
| 5 | Cross-venue funding spread | **325 of 1,674 pairs pass the statistical gates**; best BTW $0.0815/day gross, $0.0775/day net; median candidate ~$0.0073/day net | 4 fee events (2 perp RTs) + measured half-spreads | tiny; top-ranked capacity-bound | **statistically real, not collectable at $100 for top names** |
| 6 | Maker spread capture | best VIP0 case −9.64 bps/fill; median spread < fee on 22/24 pairs | VIP0 maker == taker == 10 bps/leg | negative everywhere | **NO at VIP0 (0/24); 2 bps tier 1/24 = NOT ACHIEVABLE** |
| 7 | CEX-DEX native/deep | 1–3 bps gap (SOL, USDT/USDC) | 150.5 bps @$100 (42.05 @$1,000) | negative | **NO** |
| 7b | CEX-DEX bridged | 41–100 bps "gap" | 150.5 bps @$100 | structural basis, not mispricing | **NOT an arbitrage** — bridge-risk basis, captured by MEV searchers |
| 8 | Public "$0.4–0.7/min" claim | $576–$1,008/day required | — | 576–1,008%/day on $100 | **impossible for a $100 account; unverifiable as stated** |
| 9 | Solana meme cross-DEX (90 venue-pairs) | **0 of 90 cross-venue venue-pairs ever net-positive**; widest gross **4.78 bps**; best net −5.43 bps; $0.0000/day | pool fee 8–60 bps RT (mean 34) + 1–2 bps on-chain + impact <5 bps = ≈35–60 bps required | negative | **NO** |
| 10 | Multi-chain meme cross-pool (22 chains measured, 8 with candidates) | positives only in pools ≤$70k; capacity $76–$577; 1.3–14.6% of samples | per-chain gas + pool fee + impact | capacity-bound, transient | **NOT a strategy** |
| 11 | CEX↔DEX meme (282 verified samples) | **0.0% net-positive**; best −7.2 bps | 10 bps CEX taker + 10 bps buffer + pool fee + gas | negative | **NO** |
| — | WARP expanded venues (9 venues) | best `T` $0.354/day — **FALSIFIED** (artifact #2) | 4 fee events + half-spreads | non-executable (different assets) | **artifact, not an edge** |

**One-line summary:** *the retail fee floor, not the absence of signal, defeats every
class; the only statistically rich class is a funding spread that is real, tiny, and
capacity-bound below the trade size.*

---

## Methodology — three artifacts we caught on ourselves

This is the part worth reading. Each of these would have been a headline "edge"; each was
caught and killed by an explicit identity/partition guard before it became a claim.

### 1. DEX symbol collision — three different `TRUMP` contracts

Unguarded **symbol grouping** produced phantom edges. On ethereum alone, search returned
**three distinct `TRUMP` contracts priced 0.03 / 2.34 / 5.05** — a symbol-grouped "spread"
of ~100,000 bps that is really three different assets.

| guard | robinhood headline | ethereum CEX↔DEX |
|---|---:|---:|
| grouped by **symbol** (first draft) | **2,932 bps** | **1,287 bps** |
| grouped by **(chain, contract address)** | **493 bps** | — |
| + CEX↔DEX canonical-contract allowlist | — | **45 bps** |

A collision is a fabricated **order-of-magnitude** edge, not a small bias. Guards:
cross-pool rows keyed by `(chain, contract address)`; CEX↔DEX rows carry
`identity_verified`, true only for a known canonical contract. Evidence:
[`reports/10-meme-arbitrage.md`](reports/10-meme-arbitrage.md) §6,
[`reports/11-meme-multichain-cex-dex.md`](reports/11-meme-multichain-cex-dex.md).

### 2. `T` ticker collision — Threshold vs a USDT/USD stablecoin perp

A WARP scan's best result (`T`, $0.354/day) was built by string-stripping the Kraken symbol
`PF_USDTUSD` into base `T`, colliding a **USDT/USD stablecoin perp** ($0.9998) with
Binance's **Threshold** token (`TUSDT`, $0.005414) — a **184.67× price ratio** between two
different assets. Verdict: **FALSIFIED**; corrected net $/day **0.0** (non-executable: the
two legs are different assets). Evidence:
[`evidence/verify_t/verify_t.json`](evidence/verify_t/verify_t.json).

### 3. Venue-partition artifact — the Kraken "survivors"

A `spread_series` robustness pass reported 8 surviving pairs (4× BAT, 4× QNT) whose OOS
bootstrap CI excluded zero in 21/21 seed×block combinations. The split was a **perfect
partition by venue**: every pair with a `kraken_futures` leg scored 21/21, every pair
without one scored 0/21. The falsification test showed the "spread" is a **venue-mechanism
level offset** — Kraken's hourly *relative* funding rate is structurally different from
the other venues' cadence — **and every one of those pairs fails the programme's own
same-sign gate** (`same_sign_share` 0.39–0.47 vs the 0.60 bar). Not a token-specific,
tradeable edge. Evidence:
[`evidence/partition/partition_report.json`](evidence/partition/partition_report.json),
[`evidence/spread_series/robustness.json`](evidence/spread_series/robustness.json).

---

## Precision rules (binding on any citation)

These phrasings are carried verbatim from the source reports and are mandatory:

- Triangular: write **"0 of 100 sampled triangles (of 354 discovered) ever showed positive
  net edge"** — **never** "0/354" (only 100 of the 354 discovered triangles were sampled).
- Cross-venue funding: write **"325 of 1,674 pairs pass the statistical gates"**, then
  immediately state that statistical persistence ≠ collectability: the **#6-ranked `龙虾`**
  has Gate-leg capacity **$75.66 < $100**, and **5 candidates have capacity below $100**
  (BOME×2, CHIP, SKY, 龙虾).
- `龙虾_USDT` 208-day re-test: **survives** (positive both halves, OOS CI [+5.87, +15.48])
  **but** is regime-dependent (**$0.110/day long-run vs $0.035/day most-recent-30d**) and
  thin/illiquid — present as **"thin, illiquid, regime-sensitive"**, never validated.
- Maker: the verdict is **VIP0 = 0/24 positive**; the 2 bps column (1/24) is
  **"NOT ACHIEVABLE at $100 (high-VIP/rebate tier)"**; adverse selection is a
  **"COARSE PROXY, LOWER BOUND"**.
- Meme: **"0 of 90 cross-venue venue-pairs ever net-positive"**; **"widest gross 4.78 bps
  vs a 35–60 bps required bar"**; multi-chain positives are **capacity-bound ($76–$577)
  and transient (1.3–14.6% of samples), NOT a strategy**; the meme simulator's `$/day` is
  **SYNTHETIC-fixture arithmetic only** (`sim_meme_SYNTHETIC.json`: mode SYNTHETIC,
  $629.52/day — never a market claim).
- Reachability is **intermittent**: the same hosts answered 5/5 OK and HTTP 000 minutes
  apart. A two-venue strategy cannot assume simultaneous, continuous access to both legs.

---

## Reproduction index — which script produces which report

| Script (`scripts/`) | Produces |
|---|---|
| `arb_common.py` | shared fetch / backoff / depth-walk helpers |
| `arb_triangular_scan.py` | `reports/00` §2.1, `evidence/triangular/` |
| `arb_stable_crossrate.py` | `reports/08` (class A), `evidence/stable/results.json` |
| `arb_crossvenue_spot.py` | `reports/08` (class B), `evidence/crossvenue/results.json` |
| `arb_funding_scan.py` | `reports/01`, `evidence/funding_ranking.json` |
| `arb_funding_multivenue.py` | `reports/02`, `evidence/multivenue_ranking.json` |
| `arb_funding_decisive.py` | `reports/03`, `evidence/decisive_carry_results.json` |
| `arb_funding_spread.py` | `reports/04`, `evidence/crossvenue_funding/spread_ranking.json` |
| `arb_longxia_longhistory.py` | `reports/05`, `evidence/crossvenue_funding/longxia_longhistory.json` |
| `arb_maker_scan.py` | `reports/06`, `evidence/maker/maker_results.json` |
| `arb_dex_scan.py` | `reports/07`, `evidence/dex/` |
| `arb_warp_scan.py` | `reports/09`, `evidence/warp/` |
| `arb_meme_solana.py` | `reports/10`, `reports/12`, `evidence/meme/` |
| `arb_meme_multichain.py` | `reports/11`, `evidence/meme/multichain_reanalysis.json` |
| `arb_meme_exec.py` | `reports/12`, `evidence/meme/raw_pool_fees.json` |
| `arb_spread_series.py` + `instrument_identity.py` | `reports/13`, `evidence/spread_series/` |
| `arb_partition_test.py` | artifact #3, `evidence/partition/partition_report.json` |
| `arb_verify_t.py` | artifact #2, `evidence/verify_t/verify_t.json` |
| `arb_paper_sim.py` / `arb_meme_sim.py` | paper simulators (`sim_funding.json`, `sim_meme.json`) |
| `arb_claim_math.py` | `reports/00` §3 (the "$0.4–0.7/min" reverse-engineering) |
| `arb_small_capital.py` | `evidence/small_capital/small_capital.json` (venue minimums at $25/$50) |

See [`METHODOLOGY.md`](METHODOLOGY.md) for the cost model, the IS/OOS + block-bootstrap
approach, and the identity guards. See [`scripts/README.md`](scripts/README.md) for how to
run the scanners (they import the project's cost model; a vendored subset is included).

---

## What this does NOT claim

- **No profit promise.** Nothing here establishes a positive expected return for any
  account size or fee tier.
- **No validated strategy.** The only statistically surviving carry name (`龙虾`) is
  presented as *thin, illiquid, regime-sensitive* — **not** validated.
- **No claim that these classes are impossible in principle** — only that they are not
  net-positive for a **$100 retail taker** on the fee schedules and venues measured here.
- **Paper-only / measurement-only.** No orders were placed, no keys or wallets were used,
  no account or book state is included, no strategy code is included.
- **$100 retail basis.** Fees are the project's base tier (spot taker 10 bps, perp taker
  5 bps, maker 2 bps; slippage k=0.5, exit ×1.5). A negative-maker rebate tier, colocation
  or $100k+ capital is a *different account*, not a tweak to this one.
- **Point-in-time.** Windows are short (25–40 min for microstructure classes, ~30 days for
  funding, 208 days only for the single `龙虾` re-test). These are one regime on one host
  on 2026-10-03, not a permanent property of the market.
- **Venue fees are partly unverified** where the fee pages were unreachable; where
  verified, the project model **under-charges** (Gate perp taker is API-verified 7.5 bps vs
  the 5.0 bps modelled). Execution, latency, market impact and partial fills are unmodelled
  or proxied; adverse selection in the maker study is a **lower bound**.

## Prior work cited (not copied)

- `~/back/FUNDING_OR_ALT.md` — funding-data obtainability + a 21-symbol carry test
  (negative in both IS/OOS halves).
- `~/back/COST_FLOOR.md` — VIP0 taker fee verified at 5.0 bps/leg; corrected RT 12 bps;
  no per-trade edge in the project is net-positive at corrected VIP0.
- `~/polymarket/PROFIT_REALITY.md` — the same structural finding on Polymarket (YES+NO ask
  sum minimum 1.0010 vs the <1.000 needed for a riskless pair).

## License

MIT — see [`LICENSE`](LICENSE).
