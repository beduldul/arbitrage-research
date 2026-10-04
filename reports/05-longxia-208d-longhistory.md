# Addendum — long-history re-test of the single-venue carry survivor (2026-10-03)

**Question.** The single-venue survivor `龙虾_USDT` (Gate) was measured on only ~30 days
because a *single* Gate funding call caps at 1000 rows. Is longer history obtainable, and
does the carry survive a longer sample and a real regime flip?

**Answer: YES — longer history is obtainable on Gate, and the carry survives a 208-day
window (back to the contract's first settlement).** Script: `scripts/arb_longxia_longhistory.py`. Output: `longxia_longhistory.json`.
Raw: `longxia/raw/`. **Window: 208 days (2026-03-10 → 2026-10-03), the contract's full life.**

---

## 1. Longer history — per venue, with endpoint/evidence

| venue | base listed? | longer history? | evidence |
|---|---|---|---|
| **Gate** | yes (`龙虾_USDT`) | **YES — 208 days** | `funding_rate` accepts a `to` bound with **no** `from`, so paging backwards on `to` reaches the contract's first settlement. **1,240 rows back to 2026-03-10** (the contract launch date). |
| Bybit | yes, romanised `LONGXIAUSDT` | not obtainable this run | endpoint **TCP-refused (HTTP 000) on every retry** (6 attempts, 3s spacing) during this addendum; intermittency again. Its 8h history is *not* in cache. |
| Bitget | yes (`龙虾USDT`) | only ~30d cached | endpoint also **TCP-refused** this run; cached scan pages cover 2026-08-31 → 2026-10-03 only. |
| OKX | **no** | n/a | not listed (`instType=SWAP` enumeration). |
| Hyperliquid | **no** | n/a | not in `meta.universe`. |

**Gate endpoint detail.** `api.gateio.ws/api/v4/futures/usdt/funding_rate`:
- `from`+`to` together are capped at ~180 days (a wider span returns HTTP 400);
- `to` **alone** (no `from`) returns the 1000 rows ending at `to` — so paging
  `to = oldest_returned − 1` walks backwards to the contract's first settlement
  (2026-03-10, the launch date).
- 1d candles: `candlesticks?interval=1d&limit=1000` returns **208 days** (2026-03-10 →
  2026-10-03); spot `spot/candlesticks` returns **210 days** (2026-03-09 → 2026-10-04).
  Both cover the full funding window, so the hedged PnL has a close for every day.

**Data integrity.** 1,240 rows, 4h native interval (gaps 14,397–14,403s), **no duplicate
timestamps** across pages. Daily funding = sum of the 6 four-hourly rates per UTC day.

## 2. Re-run — decisive hedged PnL, IS/OOS + block bootstrap, 208-day window

Same method as the decisive study: daily PnL = funding (short perp) + basis drift
(`leg·[(S_d/S_{d−1}) − (P_d/P_{d−1})]`) − four fee events (spot in/out + perp in/out),
leg = $50. Gate fees **verified**: spot taker 0.10%, perp taker 0.075%. IS/OOS split at the
midpoint; moving-block bootstrap (block = 5 days, 2,000 resamples, seed 20261003) on OOS.

| metric | 30-day (prior) | **208-day (this run)** |
|---|---:|---:|
| aligned days | 30 | **208** |
| gross funding $ | — | 22.352 |
| basis drift $ | — | **+0.610** (small — the basis term is a footnote, not the driver) |
| fees $ | — | −0.175 |
| net $ | — | **22.786** |
| **net $/day @ $100** | ~0.047 | **0.1096** |
| net $/day, one-day-trimmed | — | 0.1015 |
| net $/day, most-recent 30d | — | **0.0352** |
| IS (104d) net $ | — | **+12.751** ($0.1226/day) |
| OOS (104d) net $ | — | **+10.035** ($0.0965/day) |
| OOS 95% CI | — | **[+5.87, +15.48]** (excludes zero) |
| verdict | candidate (30d) | **survives** |

**Regime flip — does funding ever go persistently negative?** Over 208 days:
**13 negative days (6.2%)**, **longest consecutive negative run 5 days**, worst single day
−0.00059 (−2.9 bps). **No persistent-negative regime.** A short-perp carry would have
survived every stretch of this window.

## 3. Honest reading — the long window strengthens but also *revises down* the headline

- The carry **survives**: positive in both 99-day halves, OOS CI strictly positive, no
  negative regime. This is materially more than the 30-day sample could show.
- But the **per-day figure is regime-dependent**: $0.110/day over 208 days, yet only
  **$0.035/day over the most recent 30 days** — i.e. the recent window is *weaker* than the
  long-run average, not stronger. The original ~$0.047/day sits between the two.
- **One day is 6.0% of gross funding**; trimming it still leaves $0.102/day, so the long-run
  average is not one-day-driven — but the recent-30d $0.035/day is the number a *current*
  entrant would experience, and it is below the prior headline.
- **Basis drift is small (+$0.61 over 208 days, ~$0.003/day)**, confirming this is a funding-only
  trade, but that also means the PnL is entirely a function of Gate's funding formula — the
  thing a regime flip would change.

## 4. Capacity — the cross-venue `龙虾` row is NOT collectable at $100 (confirmed)

Reading from `spread_ranking.json` (base `龙虾`, Bitget↔Gate, 90 buckets, mean spread
3.19 bps/8h, gross $0.0478/day, net $0.0394/day, ranked #6):

- `capacity_usd = 75.66`, set by the **Gate leg's** order-book depth within ~50 bps
  (`depth_b_usd = 75.66`; the Bitget leg is $16,086). **The Gate leg is the shallow one.**
- **$75.66 < $100 trade size** ⇒ the $100 two-leg position would already exceed the
  resting liquidity on the Gate leg at that price band. **The measured spread is not
  collectable at $100 on this venue pair.** The #6 ranking is notional, not achievable.
- Both legs also carry a wide measured half-spread (Bitget 7.09 bps, Gate 7.83 bps), which
  is charged into the cost column.

## 5. Presentation guidance for the consolidation (one line)

> `龙虾_USDT` is the **only** single-venue carry survivor, and on Gate's **208-day** history
> it *does* survive (positive both 104-day halves, OOS CI [+5.87, +15.48], no negative
> regime) — but its edge is **regime-dependent ($0.110/day long-run vs $0.035/day in the
> most recent 30 days)** and, as a cross-venue pair, it is **not collectable at $100**
> (Gate-leg book depth $75.66 < $100); present it as a **thin, illiquid-corner,
> regime-sensitive carry**, not a validated strategy.

## 6. Evidence paths

- `evidence/arbitrage/2026-10-03/crossvenue_funding/longxia/longxia_longhistory.json` — full verdict
- `evidence/arbitrage/2026-10-03/crossvenue_funding/longxia/raw/gate/longxia_funding_p*.json` — 1,240 funding rows (2026-03-10 → 2026-10-03)
- `evidence/arbitrage/2026-10-03/crossvenue_funding/longxia/raw/gate/longxia_{spot,perp}_candles.json` — 1d candles
- `scripts/arb_longxia_longhistory.py` — re-test; `tests/unit/test_arb_funding_spread.py` — incl. `funding_regime` tests (55 total)
