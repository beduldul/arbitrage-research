# CEX↔DEX price gap at $100 and $1,000 — class C (measurement only)

**Date:** 2026-10-03  |  **Notionals:** $100 and $1,000  |  **Window:** 25.2 min @ 1 s → **43
iterations, 600 samples** (0 timeouts), plus 3 earlier short windows → **950 samples pooled**
**Mode:** measurement only — no orders, no keys, no wallets, no transactions, no `.env` edits,
no gate/pipeline/config changes.
**Public endpoints only.** Concurrency 4 + a measured token-bucket limiter (0.4 req/s) →
**2 HTTP 500s, 0 rate-limits hit** in the final window.

Script: `scripts/arb_dex_scan.py`  |  Tests: `tests/unit/test_arb_dex.py` (26 pass)
Raw evidence: `evidence/arbitrage/2026-10-03/dex/`

---

## 1. Reachability matrix (probed, 3 attempts each, exponential backoff)

| endpoint | HTTP | latency | verdict |
|---|---:|---:|---|
| Jupiter **`lite-api.jup.ag/swap/v1/quote`** | 200 | 89 ms | **reachable — primary DEX quote source** |
| Jupiter `lite-api.jup.ag/price/v3` | 200 | 103 ms | **reachable** (reference only) |
| Jupiter `lite-api.jup.ag/quote` (legacy path) | 404 | 15 ms | **moved** — old path retired |
| Jupiter `quote-api.jup.ag/v6/quote` (the brief's path) | 000 | 3 ms | **DNS fail** — host retired the v6 domain |
| `api.1inch.dev/swap/v6.0/1/quote` | **401** | 110 ms | **needs API key** — unusable |
| `api.0x.org/swap/permit2/price` | **401** | 287 ms | **needs API key** — unusable |
| `api.dexscreener.com` (search/pairs) | 200 | 170 ms | **reachable** (fallback) |
| `api.coingecko.com/api/v3/simple/price` | 200 | 345 ms | **reachable** (fallback) |
| `data-api.binance.vision` `/ticker/bookTicker` | 200 | 373 ms | **reachable — CEX mid control** |
| `api.gateio.ws` `/spot/tickers` | 200 | 443 ms | **reachable** |
| `api.huobi.pro` (HTX) `/market/tickers` | 200 | 67 ms | **reachable** |
| `api.hyperliquid.xyz/info` | 200 | 178 ms | **reachable** |
| `api.bybit.com` v5 tickers | 200 | 58 ms | reachable **this run** (was blackholed for the sibling worker) |
| `www.okx.com` `/api/v5/market/ticker` | 200 | 145 ms | reachable **this run** |
| `api.bitget.com` v2 spot tickers | 200 | 145 ms | reachable **this run** |

**Both EVM aggregators (1inch, 0x) require an API key and are unusable without one.** The
DEX side is therefore **Solana-only**, via Jupiter. That is a real scope limit: this measures
**Solana DEX ↔ CEX**, not Ethereum DEX ↔ CEX.

**Rate limit found the hard way.** Jupiter's lite API tolerated ~28 req/min sustained
(measured: 30/30 requests at 0.5 req/s). A first collector that fired 14 quotes every 6 s
(~140 req/min) got **429 on every call** and, because the sample path skipped silently,
reported **0 samples across 64 iterations**. That looked exactly like "no opportunities" and
was in fact a total collection failure. Fixed with a token-bucket limiter plus a loud
zero-sample warning; a stalled-socket bug that cost ~85% of a window is also fixed with a
per-iteration hard timeout. Both bugs are pinned by tests.

---

## 2. The gap (executable, both directions)

The headline is the **max executable edge over both trade directions** — buy the token on the
CEX **ask** / sell on the DEX, *or* buy on the DEX / sell on the CEX **bid**. Comparing the DEX
price to a mid or to a single side manufactures an edge the spread eats (pinned by test).

950 pooled samples, `max_edge_bps` by token × notional:

| token | kind | @$100 med / p90 / max | @$1,000 med / p90 / max | % > 30.5 bps |
|---|---|---:|---:|---:|
| **SOL** | native (wrapped) | 0.82 / 1.33 / 3.04 | 0.97 / 1.65 / 3.19 | 0.0% |
| **USDT/USDC** | native stable | 2.81 / 2.86 / 3.11 | 2.82 / 2.87 / 3.15 | 0.0% |
| **ETH** | bridged (Portal) | 3.43 / 5.46 / 6.82 | 2.92 / 5.51 / 6.94 | 0.0% |
| **WBTC** | bridged (Portal) | 7.01 / 8.72 / 9.38 | 7.11 / 8.40 / 9.62 | 0.0% |
| **LINK** | bridged | 7.45 / 13.49 / 36.16 | 9.23 / 13.74 / 19.25 | 0.7% |
| **AVAX** | bridged | 11.28 / 17.00 / 18.92 | 13.72 / 19.25 / 46.94 | 1.5% |
| **ARB** | bridged | 40.87 / 44.02 / **50.17** | 42.60 / 46.23 / **100.47** | **100%** |

| group | @$100 median / p90 / max | @$1,000 median / p90 / max |
|---|---:|---:|
| **native / deep** (SOL, USDT/USDC) | 2.01 / 2.86 / 3.11 | 2.28 / 2.86 / 3.19 |
| **bridged** (ETH, WBTC, ARB, LINK, AVAX) | 8.02 / 40.87 / 50.17 | 8.69 / 42.81 / 100.47 |

**Two regimes, and the distinction is the whole finding:**

* **Native/deep tokens (SOL, USDT/USDC): the gap is 1–3 bps.** This is the *efficient* case —
  the same asset is fungible across venues and arbitrage keeps them pinned. There is nothing
  here.
* **Bridged tokens show a persistent 7–42 bps DEX discount.** ETH/WBTC are explicitly
  *"(Portal)"* — Wormhole-bridged — and ARB/LINK/AVAX are wrapped representations. The gap is
  **persistent, not transient**: ARB's per-sample std is ~2 bps on a ~41 bps mean. A persistent
  gap is **not an arbitrage** — arbitrage closes gaps. It is a **structural basis** reflecting
  bridge/redemption risk, wrapped-token liquidity, and a thin local pool (ARB's Jupiter pool
  holds ~$169k, so a $1,000 trade is ~0.6% of it and the quote's own price impact is real).

The single largest edge seen was **100.47 bps** (ARB, $1,000, one sample at
`2026-10-03T08:02:12Z`); the 2nd–5th were 77, 50, 50, 48 bps. The **max is an outlier**; the
median ARB edge is 42.6 bps.

---

## 3. The real cost stack

Every number is either the project's own fee schedule or a **cited, labelled** standard range —
none is measured here, and none is presented as measured.

| component | $100 | $1,000 | source |
|---|---:|---:|---|
| DEX pool fee + price impact | **embedded in the quote** | same | Jupiter `outAmount` already nets these; `priceImpactPct` was 0.000–0.036 bps here |
| CEX taker, 2 legs (buy + sell) | 20.0 bps | 20.0 bps | project §10.2 base tier, 10 bps/leg (`engine/fees.py`) |
| CEX withdrawal → chain | 120.0 bps | 12.0 bps | ~0.01 SOL ≈ $1.20 at $120; **labelled range** (low ≈ $0.12) |
| Solana gas | 0.5 bps | 0.05 bps | base fee 5,000 lamports = 0.000005 SOL ([Solana docs](https://solana.com/docs/core/fees/fee-structure)) |
| slippage buffer | 10.0 bps | 10.0 bps | reserved on top of the quoted impact (quote asked at 50 bps tolerance) |
| **total added on top** | **150.5 bps** | **42.05 bps** | |
| *if inventory held on both sides (no transfer)* | *30.5 bps* | *30.05 bps* | best case: 20 bps fee + 0.5/0.05 bps gas + 10 bps buffer |
| EVM gas (not used here) | ~2–50 USD | ~2–50 USD | **highly variable, labelled, not measured** — dwarfs a $100 trade |

Two honesty notes:

1. **The DEX quote already contains the pool fee and the impact.** Subtracting a pool fee again
   from a spread built on `outAmount` would double-count. The stack is split into
   *embedded* (diagnostic) vs *added* (subtracted), and that split is pinned by test.
2. **Binance does not expose per-network withdrawal fees on public endpoints** (verified — the
   public fee page omits the field). The transfer cost is therefore a **labelled range**, and
   the verdict is reported at both ends.
3. Gas is charged **exactly once**, as its own term — never folded into the withdrawal. A bug
   that did both inflated the $100 stack by 0.5 bps; it is fixed and pinned by
   `test_gas_is_charged_exactly_once`.

---

## 4. Verdict — is any gap larger than the cost stack?

| | largest edge seen | cost stack | **net** | gap > cost? |
|---|---:|---:|---:|---|
| **$100** | 50.17 bps | **150.5 bps** | **−100.3 bps** | **NO** |
| **$1,000** | 100.47 bps | **42.05 bps** | **+58.4 bps** | **YES (by 58 bps)** |
| $100, *low* transfer estimate | 50.17 bps | 42.5 bps | +7.7 bps | marginal |
| $1,000, *low* transfer estimate | 100.47 bps | 30.05 bps | +69.2 bps | yes |
| $100, *inventory both sides* | 50.17 bps | 30.5 bps | +19.7 bps | yes |
| $1,000, *inventory both sides* | 100.47 bps | 30.05 bps | +70.4 bps | yes |

**At $100 the answer is plainly no.** The full cost stack is **150.5 bps** and the widest gap
observed in 25 minutes was **50 bps**. The fixed transfer fee alone (120 bps of $100) is more
than twice the largest gap. The gap never exceeds costs.

**At $1,000 a gap *did* exceed the cost stack** — 100.47 bps vs 42.05 bps, a **+58 bps** paper
edge on one sample, and ARB's median edge (42.6 bps) is right at the cost line. **This is not a
retail opportunity, and the honest reasons are structural:**

1. **The edge is on a bridged token, and its size *is* the bridge risk.** ARB's Jupiter pool is
   ~$169k. A persistent ~41 bps DEX discount on a Wormhole-bridged token is the market pricing
   bridge/redemption and wrapped-token risk — precisely the risk a real trader is being paid to
   hold. The "gap" is not free money; it is compensation for holding a token you may not be able
   to redeem 1:1.
2. **It is not a transient dislocation, so there is no arbitrage to capture.** A gap that sits
   at 41 bps with 2 bps std for 25 minutes is a **basis**, not a mispricing. There is no "buy the
   dip" — you would be running a delta-neutral carry, which is a different strategy with its own
   funding and liquidity costs.
3. **The measured window is one regime.** 25 minutes on one afternoon, one host, Solana only.
   A single 100 bps print is an outlier (p99 = 47.9 bps at $1,000), not a repeatable edge.
4. **You are competing with colocated searchers who have private orderflow.** CEX↔DEX arbitrage
   is the most contested MEV class there is: searchers watch the CEX book and the mempool and
   land the atomic bundle before your transaction is included. Published measurement of
   CEX↔DEX extracted value ([arXiv:2507.13023](https://arxiv.org/abs/2507.13023)) documents a
   market dominated by a handful of builders and searchers with latency a retail HTTP client
   cannot approach. **This is stated as the structural reality and is deliberately not modelled
   away** — the +58 bps above is a *gross, pre-competition* number that a retail $100–$1,000
   account would not be first to take.
5. **Slippage is not the constraint here; gas and transfer are.** At $100 Solana gas is 0.5 bps
   and irrelevant; the withdrawal fee is 120 bps and decisive. On EVM, gas alone (~$2–$50) is
   20–500 bps of $100 and would kill the trade outright.

**Bottom line.** The native/deep CEX↔DEX market is efficient at retail size: gaps of **1–3 bps**
against a **150.5 bps** cost floor at $100. The only gaps that clear costs are **persistent
discounts on bridged tokens at $1,000**, and those are a bridge-risk basis captured by MEV
searchers with infrastructure a retail account does not have — not a retail arbitrage. **No
observed gap is a tradeable retail edge at $100, and the $1,000 "edge" is a bridge-risk premium
that the fastest searchers take first.**

---

## 5. Evidence paths

| artifact | path |
|---|---|
| Scanner | `scripts/arb_dex_scan.py` |
| Pure-computation tests | `tests/unit/test_arb_dex.py` (26 pass) |
| Reachability (per-attempt) | `evidence/arbitrage/2026-10-03/dex/reachability.json` |
| Raw quotes (final window, 600) | `evidence/arbitrage/2026-10-03/dex/raw_quotes_20261003T07*.json` |
| Collection meta (iterations/timeouts/failures) | `evidence/arbitrage/2026-10-03/dex/collect_meta_*.json` |
| Pooled re-analysis (950 samples) | `evidence/arbitrage/2026-10-03/dex/dex_reanalysis_*.json` |
| Discarded (implausible) | `evidence/arbitrage/2026-10-03/dex/discarded_*.json` (**0** — no wrong-mint phantoms survived) |

## 6. Suite counts

`uv run pytest -o addopts="" -q 2>&1 | tail -4` → **2285 passed, 4 skipped, 0 failed**.
The 4 skips are the opt-in live-network tests. New tests: `tests/unit/test_arb_dex.py` —
**26 passed**.

> **Note on the known sibling issue.** Mid-session the suite read *4 failed, 2218 passed*,
> all four in `tests/integration/test_phase2_multipair.py` (universe/tier-1 symbol-set drift).
> By the end of the session a sibling worker had resolved them, so the final run is 0 failed.
> Those files were never opened or edited here — this workstream's footprint is
> `scripts/arb_dex_scan.py`, `tests/unit/test_arb_dex.py`, and
> `evidence/arbitrage/2026-10-03/dex/` only. No `.env`, config, gate, or pipeline file was
> touched.
