# Reports

Curated, **verbatim** reports from the 2026-10-03 arbitrage research programme. The files
are renamed to clear names but the contents are unchanged from the source, including any
internal cross-links to the original file names (which no longer resolve here — use this
index instead).

| File | What it is |
|---|---|
| [`00-programme-overview.md`](00-programme-overview.md) | **Start here.** The consolidated programme report: all classes, the verdict table, precision rules, reconciliation, caveats. |
| [`01-funding-binance-tail-scan.md`](01-funding-binance-tail-scan.md) | Single-venue funding carry across 777 Binance perps (S3 dumps). |
| [`02-funding-multivenue-gate-hyperliquid.md`](02-funding-multivenue-gate-hyperliquid.md) | Funding carry on Gate + Hyperliquid (762 perps). |
| [`03-funding-carry-decisive-test.md`](03-funding-carry-decisive-test.md) | Decisive test: hedged carry + basis drift + IS/OOS on the top candidates. |
| [`04-crossvenue-funding-spread.md`](04-crossvenue-funding-spread.md) | Cross-venue funding spread, 1,674 pairs → 325 pass the gates. |
| [`05-longxia-208d-longhistory.md`](05-longxia-208d-longhistory.md) | The single carry survivor (`龙虾_USDT`) re-tested on 208 days. |
| [`06-maker-spread-capture.md`](06-maker-spread-capture.md) | Maker-side spread capture + adverse selection at VIP0. |
| [`07-cex-dex-solana.md`](07-cex-dex-solana.md) | CEX↔DEX price gap (Solana/Jupiter) at $100 and $1,000. |
| [`08-stablecoin-and-crossvenue-spot.md`](08-stablecoin-and-crossvenue-spot.md) | Stablecoin cross-rate/depeg + cross-venue spot. |
| [`09-warp-expanded-venues.md`](09-warp-expanded-venues.md) | Expanded venue set via WARP; contains the `T`-collision artifact. |
| [`10-meme-arbitrage.md`](10-meme-arbitrage.md) | Meme-coin arbitrage (Solana cross-DEX, multi-chain, CEX↔DEX) + the symbol-collision artifact. |
| [`11-meme-multichain-cex-dex.md`](11-meme-multichain-cex-dex.md) | Non-Solana chains + CEX↔DEX detail. |
| [`12-meme-execution-costs-mev.md`](12-meme-execution-costs-mev.md) | Meme execution cost stack + the MEV/SWQoS race. |
| [`13-spread-series-robustness.md`](13-spread-series-robustness.md) | Half-spread time-series + the identity guard that killed the `T` collision. |

See [`../METHODOLOGY.md`](../METHODOLOGY.md) for the cost model and statistical protocol,
and the top-level [`README.md`](../README.md) for the verdict table and precision rules.
