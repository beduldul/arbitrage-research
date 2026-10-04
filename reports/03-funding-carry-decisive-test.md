# Decisive test — is any funding-carry candidate actually tradeable? (2026-10-03)

Screening (`multivenue_ranking.json`) found 76 symbols whose **funding alone** clears a
round trip. This tests the top 20 for the four things that decide tradeability.
Script: `scripts/arb_funding_decisive.py`. Output: `decisive_carry_results.json`.

**Scope:** measurement only. No orders, no keys. Raw responses cached under
`evidence/arbitrage/2026-10-03/venues/`.

## Method

1. **Spot-leg existence + liquidity.** Gate: `spot/tickers` + `spot/currency_pairs`.
   HL: `spotMetaAndAssetCtxs`. HL names most spot pairs `@N`; the real `BASE/QUOTE` is
   resolved from the pair's `tokens` indices into a **sparse** token table (indices reach
   1022 while the list holds 503 entries — keying by list position is wrong). Candles for
   HL spot key on the **raw** `@N` name, not the resolved one.
2. **Hedged PnL with basis drift.** Per day: funding (short perp) + basis
   (`leg·[(S_d/S_{d−1}) − (P_d/P_{d−1})]`) − fees. Four fee events (spot in/out, perp
   in/out) on the first and last day.
3. **IS/OOS split** at the midpoint, positive required in **both** halves, plus a moving
   **block bootstrap** (block = 5 days, 2000 resamples, seed 20261003) 95% CI on the OOS
   total.
4. **$100 reality.** $50/leg; checked against Gate spot `min_quote_amount` / perp
   `order_size_min × quanto_multiplier × price`, and HL's $10 minimum order.

**Fee provenance.** **Gate: VERIFIED** — `futures/usdt/contracts` exposes
`taker_fee_rate` = 0.00075 (all 1025 contracts agree); spot taker is the published 0.10%.
**Hyperliquid: UNVERIFIED** — no unauthenticated fee endpoint; project-default perp taker
(0.05%) is used, which is *higher* than HL's published 0.035%, so HL is over-charged.

## Result — top 20 candidates

| # | venue | symbol | spot pair | spot 24h $ | min spot/perp $ | fund $ | basis $ | fees $ | net $ | $/day | IS $ | OOS $ | OOS 95% CI | verdict |
|---|---|---|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| 1 | gate | 牛来_USDT | 牛来_USDT | 0.40M | 3.00/8.55 | 1.954 | 0.387 | −0.175 | 2.166 | 0.0699 | 0.999 | 1.168 | [0.454, 1.815] | **tradeable** |
| 2 | gate | MUBARAK_USDT | MUBARAK_USDT | 0.48M | 3.00/0.62 | 1.810 | 0.097 | −0.175 | 1.732 | 0.0559 | 0.165 | 1.567 | [1.121, 2.413] | **tradeable** |
| 3 | gate | 龙虾_USDT | 龙虾_USDT | **36.10M** | 3.00/4.72 | 1.107 | 0.510 | −0.175 | 1.443 | 0.0465 | 0.541 | 0.902 | [0.243, 1.887] | **tradeable** |
| 4 | hyperliquid | PURR | PURR/USDC | 4.51M | 10.00/10.00 | 1.580 | −0.232 | −0.100 | 1.248 | 0.0403 | 0.127 | 1.122 | [0.143, 2.228] | **tradeable** |
| 5 | gate | BTW_USDT | BTW_USDT | 3.83M | 3.00/**143.55** | 2.371 | −0.025 | −0.175 | 2.171 | — | 0.906 | 1.265 | [0.151, 3.038] | no — $50 leg below perp minimum |
| 6 | gate | MOVR_USDT | MOVR_USDT | 2.32M | 3.00/0.02 | 1.250 | 0.396 | −0.175 | 1.471 | — | **−0.016** | 1.487 | [0.846, 2.518] | no — IS half negative |
| 7–20 | — | 14 symbols | — | — | — | — | — | — | — | — | — | — | — | **unconstructible** (no venue spot market) |

Unconstructible: Gate `STAMP_USDT`, `LYN_USDT`; HL `USELESS`, `CASHCAT`, `GRASS`,
`PONS`, `XMR`, `VVV`, `AERO`, `NIL`, `0G`, `SYRUP`, `GRAM`, `SKY`. Hyperliquid lists far
more perps than spot pairs, so the hedge leg simply does not exist there.

## Verdict

- **(a) Constructible: 6 / 20** (4 Gate, 1 HL `PURR`, plus 2 Gate that then failed economics).
- **(b) Net-positive in BOTH halves with a CI excluding zero: 4 / 20** — `牛来_USDT`,
  `MUBARAK_USDT`, `龙虾_USDT`, `PURR`.
- **(c) $/day at $100 notional: $0.040–$0.070/day** (龙虾_USDT $0.0465, PURR $0.0403).

**This is NOT zero** — but read the caveats, they are material:

1. **Only 2 of the 4 have a liquid spot leg.** `牛来_USDT` ($0.40M) and `MUBARAK_USDT`
   ($0.48M) are *below the same $1M volume floor* the screening stage enforces — they
   clear the economics bar only because they are too small to matter. The two that
   survive on liquidity are **`龙虾_USDT` (spot $36.1M)** and **`PURR` (spot $4.5M)**.
2. **Basis drift is a real term, not a footnote** — it ranges from −$0.23 to +$0.51 here,
   comparable to the funding itself, confirming the prior study's finding.
3. **The window is 30 days; the OOS half is ~15 days.** The CIs are wide
   (`龙虾_USDT` [0.243, 1.887]) and rest on a handful of weeks.
4. **The $100 reality binds.** `BTW_USDT` — the highest funding ($2.37) — is untradeable
   because Gate's perp minimum (`order_size_min × quanto_multiplier × price` = **$143.55**)
   exceeds the $50 leg. At $100 notional the trade is not constructible at all.

**Remains unverified:** execution latency and the spot-leg slippage at size (the $100
model charges the project's flat slippage, not a measured book); funding-sign risk (a
regime flip mid-hold turns the carry negative, and the streak evidence is 30 days);
Hyperliquid's fee tier (unverified; HL is over-charged here, so its true number is
*better* than shown).

**Reconciliation with the prior study (`~/back/FUNDING_OR_ALT.md`).** The prior work's
21-major, short-hold (N≤21) test was negative. This is not a contradiction: the two
select for different things. The prior study held days; the persistent-funding tail it
did not scan requires **25–30 day holds** to amortise, which is exactly where the four
survivors live (break-evens 4–15 days). The structural finding is unchanged: the class is
positive only in illiquid corners, and the one liquid survivor (`龙虾_USDT`, $36M spot)
yields ~$0.05/day per $100 — about $17/yr, before execution risk.
