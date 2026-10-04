# Evidence

Machine-readable evidence behind the reports. Each file is a raw or re-analysed JSON
output from a scanner in [`../scripts/`](../scripts/); the reports cite these paths.

| Path | Contents |
|---|---|
| `funding_ranking.json` | 777 Binance perps ranked by hedged net carry (`reports/01`). |
| `multivenue_ranking.json` | 762 Gate + Hyperliquid perps (`reports/02`). |
| `decisive_carry_results.json` | Top-20 decisive hedged-carry test, IS/OOS + CI (`reports/03`). |
| `carry_results.json` | Carry-test intermediates. |
| `sim_funding.json` | Paper carry simulator, **REAL** mode: `total_net_pnl` −0.40896, `dollars_per_day` −0.04090, 29 pairs, 10-day coverage. |
| `universe_census.json`, `venue_minimums.json` | Universe enumeration and per-venue minimum order sizes. |
| `reachability_binance.json`, `telemetry.json` | Endpoint reachability + run telemetry. |
| `triangular/run_20261003T060859Z_summary.json` | 354 discovered / 100 sampled / 0 ever positive. |
| `crossvenue_funding/spread_ranking.json` | 1,674 pairs, all fields (`reports/04`). |
| `crossvenue_funding/reachability.json` | 10 endpoints × 5 attempts. |
| `crossvenue_funding/longxia_longhistory.json` | `龙虾_USDT` 208-day verdict (`reports/05`). |
| `maker/maker_results.json` | Maker spread + adverse-selection samples (`reports/06`). |
| `dex/dex_reanalysis_20261003T082345Z.json` | 950 pooled CEX↔DEX samples (`reports/07`). |
| `dex/reachability.json`, `dex/discarded.json` | DEX reachability; discarded-implausible list (empty). |
| `stable/results.json`, `crossvenue/results.json` | Stablecoin + cross-venue spot runs (`reports/08`). |
| `warp/triangular_warp.json`, `warp/basis_warp.json`, `warp/binance_futures_warp.json`, `warp/funding_warp.json` | WARP expanded-venue runs (`reports/09`). |
| `warp/probe_self.json`, `warp/probe_binance.json` | WARP reachability probes. |
| `meme/meme_report_reanalyzed.json` | Solana cross-venue re-analysis, 0 of 90 (`reports/10`). |
| `meme/sim_meme_SYNTHETIC.json` | Paper simulator, **SYNTHETIC** mode — arithmetic only, **never a market claim**. |
| `meme/multichain_reanalysis.json`, `meme/raw_pool_fees.json` | Multi-chain cross-pool + Orca pool fees (`reports/11`, `reports/12`). |
| `verify_t/verify_t.json` | **Artifact #2**: the `T` ticker collision, verdict FALSIFIED. |
| `partition/partition_report.json` | **Artifact #3**: the Kraken venue-partition test. |
| `spread_series/robustness.json`, `spread_series/spread_series_report.json` | Half-spread series + 21-combo bootstrap robustness (`reports/13`). |
| `small_capital/small_capital.json` | Venue minimum order sizes at $25/$50 legs. |

Raw venue payloads (order books, funding history, tape) are **not** included — they are
hundreds of MB and contain no claim not already summarised above. The scripts in
`../scripts/` re-fetch them from public endpoints.
