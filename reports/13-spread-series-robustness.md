# Spread-series + robustness re-run — 2026-10-03

Measurement only. Code `scripts/arb_spread_series.py`, `scripts/instrument_identity.py`;
tests `tests/unit/test_arb_spread_series.py`. Raw books under `spread_series/raw/`.

## 0. Identity guard (blocking finding)

The WARP scan derived the Kraken base by string-stripping
(`sym[3:].replace('XBT','BTC').replace('USD','')`), turning `PF_USDTUSD` (USDT/USD
stablecoin perp) into base `T` and colliding it with Binance Threshold (`TUSDT`,
$0.0054). Same failure class confirmed independently in `../verify_t/verify_t.json`.
`instrument_identity.py` replaces string-stripping with each venue's own metadata
(`base`/`quote`/`category`/`type`) plus a price-sanity check.

- 3,220 pairs audited; 1,251 fail the guard.
- Of the report's top 12 executable pairs: **10 pass, 2 fail** —
  `T` (base mismatch T≠USDT + stablecoin leg) and `ONE bitget/okx` (18.6% price divergence).

## A. Half-spread time-series

47 rounds × 12 s ≈ 9.4 min per leg (bounded window; sampler stopped early). Half-spread
in bps; `4x tot` = `2*(hsA+hsB)` on the $50 leg, entry+exit both legs.

| base | pair | hsA mean | hsA p90 | hsB mean | hsB p90 | snap 4x | meas 4x | p90 4x | snap/meas |
|---|---|---|---|---|---|---|---|---|---|
| BAT | kraken_futures/okx | 6.65 | 13.21 | 4.14 | 6.50 | 78.70 | 21.57 | 39.41 | 0.27 |
| BAT | bybit/kraken_futures | 3.92 | 5.76 | 6.65 | 13.21 | 87.04 | 21.14 | 37.94 | 0.24 |
| BAT | binance/kraken_futures | 2.16 | 3.35 | 6.65 | 13.21 | 76.60 | 17.62 | 33.12 | 0.23 |
| BAT | bitget/kraken_futures | 6.50 | 8.38 | 6.65 | 13.21 | 86.03 | 26.30 | 43.18 | 0.31 |
| QNT | gate/kraken_futures | 1.14 | 1.62 | 15.40 | 19.26 | 37.62 | 33.07 | 41.77 | 0.88 |
| QNT | binance/kraken_futures | 0.26 | 0.40 | 15.40 | 19.26 | 35.22 | 31.32 | 39.33 | 0.89 |
| QNT | bybit/kraken_futures | 0.22 | 0.20 | 15.40 | 19.26 | 36.02 | 31.24 | 38.93 | 0.87 |
| QNT | bitget/kraken_futures | 0.75 | 1.22 | 15.40 | 19.26 | 35.22 | 32.31 | 40.96 | 0.92 |
| ARK | bybit/gate | 1.27 | 2.02 | 3.99 | 4.54 | 11.12 | 10.52 | 13.11 | 0.95 |
| LSK | bitget/bybit | 3.32 | 3.63 | 0.90 | 1.63 | 8.31 | 8.44 | 10.52 | 1.02 |
| T | binance/kraken_futures | 0.96 | 0.93 | 1.00 | 1.00 | 3.84 | 3.93 | 3.85 | 1.02 (INVALID pair) |

**Snapshot vs measured.** The snapshot half-spread was a *single instantaneous* sample,
which here landed on a wide moment for the BAT/Kraken books (36.7 bps at snapshot vs 6.7
bps mean measured) — so for BAT the snapshot **over**-stated cost by ~3–4×. For
QNT/Kraken the snapshot (17.4) was close to the mean (15.4). On these liquid legs the
snapshot is not systematically conservative; it is a high-variance single draw. The
report's $/day figures are therefore not cost-understated for BAT/QNT — they were
*over*-charged on spread.

## B. Seed × block robustness (21 combos = 7 seeds × 3 blocks, 2000 resamples)

| base | pair | report-seed/block5 CI | combos keeping CI>0 |
|---|---|---|---|
| T | binance/kraken_futures | [3.65, 17.99] | 21/21 (but pair INVALID) |
| BAT | kraken_futures/okx | [3.36, 47.28] | 21/21 |
| BAT | bybit/kraken_futures | [3.26, 47.51] | 21/21 |
| BAT | binance/kraken_futures | [2.92, 46.50] | 21/21 |
| BAT | bitget/kraken_futures | [2.99, 46.50] | 21/21 |
| QNT | gate/kraken_futures | [9.68, 48.46] | 21/21 |
| QNT | binance/kraken_futures | [9.56, 48.05] | 21/21 |
| QNT | bybit/kraken_futures | [8.66, 48.05] | 21/21 |
| QNT | bitget/kraken_futures | [8.66, 48.35] | 21/21 |
| ONE | bitget/okx | [-76.08, 152.77] | 0/21 |
| ARK | bybit/gate | [-5.73, 79.72] | 0/21 |
| LSK | bitget/bybit | [-8.98, 6.53] | 0/21 |

The report's single seed reproduces exactly (T: [3.65,17.99]). No pair that passed on
one seed flips within the grid — the 9 positive pairs are robust across all 21 combos;
the 3 failing pairs fail on all 21. The seed was *not* the fragile part.

## Verdict

Survive **both** the identity guard and the 21-combo robustness re-run: **8 pairs** —
4× BAT (kraken_futures/okx, bybit/kraken_futures, binance/kraken_futures,
bitget/kraken_futures) and 4× QNT (gate, binance, bybit, bitget / kraken_futures).
All remain sub-$0.35/day at $100. `T` is removed as a symbol collision; ONE/ARK/LSK fail
the robustness re-run.

Caveats: window is ~9.4 min (not the 25–30 min target) because the run was stopped on
budget; venue fee tiers remain unverified (docs only).
