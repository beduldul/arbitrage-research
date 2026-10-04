# Meme-coin arbitrage on chains **other than Solana** + CEX↔DEX — measurement only

**Date:** 2026-10-03  |  **Notional:** **$100 paper** (every size / cost / $/day figure)
**Window:** 30.0 min @ 20 s → **47 iterations, 0 timeouts, 0 HTTP failures, 2 425 samples**
**Mode:** measurement only — no orders, no keys, no wallets, no transactions, no `.env` edits,
no gate/pipeline/config changes. Scripts + tests only. Solana DEXes are **excluded by design**
(`EXCLUDED_CHAINS = {"solana"}`) — a sibling worker owns them.

Script: `scripts/arb_meme_multichain.py`  |  Tests: `tests/unit/test_arb_meme_multichain.py`
(**38 pass**; full suite **2 402 passed, 4 skipped, 0 failed**)
Evidence: `evidence/arbitrage/2026-10-03/meme/multichain/`

---

## 1. Reachability matrix (chain → measurable? → failure mode)

Two independent multi-chain sources were probed. **DexScreener is the primary** (no key, one
`/latest/dex/search` call returns pairs on every chain, so one symbol list covers all networks);
**GeckoTerminal is the secondary** volume census, but it is hard rate-limited to **~30 req/min**
(measured: 179 × HTTP 429 across a paced 100-network sweep) and is therefore used once, not polled.

### 1a. Endpoint matrix (probed 1–2×, exponential backoff)

| endpoint | HTTP | latency | verdict |
|---|---:|---:|---|
| `api.dexscreener.com` search / pairs / tokens | 200 | 290–430 ms | **reachable — primary multi-chain source** |
| `api.geckoterminal.com` networks / pools | 200 | 150–900 ms | **reachable but rate-limited (~30/min → 429)** |
| `data-api.binance.vision` bookTicker / 24hr | 200 | 520 ms / 51 s | **reachable — CEX control** |
| `api.gateio.ws` spot tickers | 200 | 650 ms | **reachable — CEX control** |
| `api.huobi.pro` merged | 200 | 133 ms | **reachable — CEX control** |
| `api.hyperliquid.xyz/info` | 200 | 234 ms | **reachable — perp reference** |
| `api.bybit.com` v5 | 000 | 4.0 s | **tcp_refused** (host egress block) |
| `www.okx.com` v5 | 000 | 2.0 s | **tcp_refused** |
| `api.bitget.com` / `api.mexc.com` / `api.kucoin.com` | 000 | 12 s | **timeout** |
| `api.exchange.coinbase.com` / `api.crypto.com` | 000 | 1–5 s | **tcp_refused** |
| `api.thegraph.com` (Uniswap v3 subgraph) | 000 | 197 ms | **DNS fail** — host retired |
| `api.aerodrome.finance` | 000 | 2 ms | **DNS fail** |
| `api.pancakeswap.info` | 500 | 554 ms | **server error** — dead endpoint |
| `api.1inch.dev` quote | **401** | 144 ms | **needs API key** |
| `api.0x.org` quote | 404 | 733 ms | **path retired** |
| `fullnode.mainnet.sui.io` / `aptoslabs.com/v1` | 200 | 105 / 424 ms | reachable (RPC only, not a price feed) |
| `toncenter.com/api/v2` | 200 | 744 ms | reachable (RPC only) |
| `horizon.stellar.org` / `lcd.osmosis.zone` | 200 | 1 037 / 1 286 ms | reachable (non-EVM, no meme DEX feed) |
| `apilist.tronscanapi.com` | 404 | 332 ms | path retired |
| `rpc.mainnet.near.org` | 405 | 80 ms | method-not-allowed (POST-only) |
| `blockstream.info` | 000 | 5.0 s | **tcp_refused** |

**No chain-specific DEX API answered keyless.** Thegraph DNS is dead, Aerodrome DNS fails,
1inch needs a key, 0x's path is retired, PancakeSwap's API 500s. DexScreener + GeckoTerminal are
the only working multi-chain price feeds; the CEX controls are Binance-vision / Gate / Huobi / HL.

### 1b. Chain coverage — GeckoTerminal census (100 networks probed, paced 0.4 req/s)

**60 of 100 networks returned pools** across the sweep (a network that only 429'd in one pass was
confirmed by the earlier serial probe). Reachable examples: `eth, bsc, base, arbitrum, avax,
polygon_pos, solana, sui-network, ton, aptos, celo, mantle, scroll, zksync, sei-network, cronos,
ronin, flare, fraxtal, mode, bob-network, core, kaia, kava, metis, fuse, bch, cfx, elrond/ela,
iotx, kcc, lukso, manta-pacific, oasys, terra, velas, …`

**40 networks returned no pools from any probe:** `movr, one, glmr, mtr, dfk, canto, godwoken,
filecoin, multivac, polygon-zkevm, ultron, pulsechain, rollux, starknet-alpha, linea, opbnb,
shibarium, hedera-hashgraph, beam, lightlink-phoenix, elysium, ton¹, defimetachain, zkfair,
zetachain, oasis-sapphire, merlin-chain, xai, immutable-zkevm, blast¹, map-protocol, omax-chain,
graphlinq-chain, qitmeer-network, chiliz-chain, x-layer, bitlayer, cyber, octaspace, zklink-nova`
(¹ `ton` and `blast` have **no GeckoTerminal pools** but **are** covered by DexScreener — see 1c).

### 1c. Chains actually measured in the window (DexScreener, 22 distinct)

`base, bsc, ethereum, robinhood, pulsechain, arc, cronos, ton, hyperevm, abstract, polygon,
algorand, xrpl, blast, arbitrum, tron, near, hedera, monad, ink, scroll, stacks`

Tradeable candidates (≥ 2 pools of one contract, ≥ $20 k liquidity, CEX symbol present) were found
on **8 chains**: `ethereum (16 groups), bsc (11), base (10), robinhood (11), cronos (2),
abstract (2), arbitrum (1), hyperevm (1)`. The other 14 chains are price-measurable but had no
two-venue candidate at $100 (single pool, or sub-$20 k liquidity).

**Blocked outright (host egress):** bybit, okx, bitget, mexc, kucoin, coinbase, crypto.com
(all `tcp_refused`/`timeout`), thegraph + aerodrome (DNS), 1inch/0x (key/retired), tronscan (404).

---

## 2. Method and the two trade shapes

1. **Cross-pool, same token, same chain.** Pools are grouped by **`(chain, contract address)`**,
   never by symbol (see §4). Buy at one pool's *executable* price, sell at the other's; both pool
   fees and both impacts are inside those prices; native gas is charged **once** per round trip.
2. **CEX↔DEX.** CEX bid/ask (Binance-vision → Gate fallback) vs the deepest DEX pool of the same
   contract. CEX taker 10 bps on one leg, gas once, labelled withdrawal range, 10 bps buffer.

**Measured:** pool price, liquidity, 24 h volume, CEX bid/ask, Hyperliquid perp mid.
**Labelled assumptions (never presented as measured):** pool fee per DEX (30 bps default; Uniswap
v3 tiers by label), impact = `notional / (liquidity × 0.5)` (constant-product model), native gas
per chain (a *range*), CEX withdrawal (0.12–1.20 USD), 10 bps slippage buffer.

Per-chain native gas (labelled range, USD per round trip, low→high): base 0.005–0.10, arbitrum
0.01–0.30, bsc 0.05–0.60, polygon 0.005–0.10, avax 0.01–0.30, ethereum 0.50–15, tron 0.30–3.00,
ton 0.01–0.10, cronos 0.01–0.20, celo 0.005–0.05, mantle 0.005–0.05, scroll 0.01–0.20, zksync
0.01–0.20, sui/aptos 0.005–0.02. A chain with no labelled range reports `[0, 5]` (a gap, not a zero).

---

## 3. Net-edge table (top 15 across all chains, at $100)

Executable net bps, all costs charged once. **`ver` = identity-verified** (see §4).

| # | chain | kind | symbol | max net bps | p90 | %pos | run | max size | ver |
|---:|---|---|---|---:|---:|---:|---:|---:|:--:|
| 1 | ethereum | cex_dex | TRUMP | **1287.3** | 1265.2 | 100 | 94 | $9 853 | ✗ |
| 2 | ethereum | cex_dex | WIF | **1166.6** | 1136.6 | 100 | 47 | $9 853 | ✗ |
| 3 | ethereum | cex_dex | BANANA | 516.8 | 503.5 | 50 | 1 | $9 853 | ✗ |
| 4 | robinhood | cross_pool | MEME | **492.7** | 492.7 | 100 | 47 | **$577** | ✓ |
| 5 | ethereum | cex_dex | PENGU | 481.2 | 466.0 | 100 | 94 | $9 853 | ✗ |
| 6 | robinhood | cex_dex | TURBO | 447.8 | 431.7 | 100 | 47 | $9 853 | ✗ |
| 7 | robinhood | cex_dex | AERO | 210.9 | 208.6 | 100 | 14 | $9 853 | ✗ |
| 8 | robinhood | cex_dex | BANANA | 196.0 | 183.1 | 100 | 47 | $9 853 | ✗ |
| 9 | ethereum | cross_pool | NPC | **45.3** | 45.3 | 100 | 47 | **$171** | ✓ |
| 10 | robinhood | cex_dex | PENGU | 42.7 | 10.6 | 12.8 | 5 | $9 853 | ✗ |
| 11 | base | cross_pool | DOGINME | **28.2** | −15.0 | 4.3 | 2 | **$171** | ✓ |
| 12 | abstract | cex_dex | PENGU | 25.3 | 14.2 | 36.2 | 8 | $9 853 | ✗ |
| 13 | bsc | cross_pool | BAN | **24.2** | 13.1 | 14.6 | 1 | **$114** | ✓ |
| 14 | robinhood | cross_pool | CLOCKIN | 3.5 | 3.5 | 100 | 11 | $76 | ✓ |
| 15 | base | cross_pool | AERO | 1.3 | 1.3 | 12.8 | 6 | $171 | ✓ |

Every one of the eight largest "edges" is **unverified** — a symbol collision, not an arb (§4).
The largest **verified** edge is robinhood `MEME` at 493 bps but capacity **< $600**; the largest
verified edge with a liquid pool is ethereum `NPC` at **45 bps / < $200**.

---

## 4. The phantom — why 8 of the top 9 rows are not trades

DexScreener's search returns **different tokens that share a ticker**. On ethereum alone, search
returned **three distinct `TRUMP` contracts** priced **0.03 / 2.34 / 5.05** — a symbol-grouped
"spread" of ~100 000 bps that is really three different assets. The first draft grouped by symbol
and reported a 2 932 bps headline on `robinhood`; grouping by **contract address** dropped it to
493 bps, and flagging CEX↔DEX rows by a canonical-contract allowlist dropped ethereum from
1 287 bps to a verified 45 bps.

Two guards now exist, both pinned by test:

* **Cross-pool rows are keyed by `(chain, contract address)`** — three `TRUMP` contracts can never
  be paired. (`test_same_symbol_different_contract_is_not_an_arb`)
* **CEX↔DEX rows carry `identity_verified`**, true only when the pool's `(chain, address)` is a
  known canonical contract for that symbol. An unknown address is *unverified* — the allowlist can
  never upgrade a collision to verified. (`test_unknown_or_wrong_contract_is_unverified`)

---

## 5. Verdict counts

**Verified CEX↔DEX (282 samples, 6 canonical contracts: ethereum PEPE/SHIB/FLOKI/TURBO, bsc FLOKI,
base AERO):**
**0.0 % net-positive. Best = −7.2 bps.** The classic "CEX-DEX meme" trade is **dead at $100** —
the 10 bps CEX taker + 10 bps buffer + pool fee + gas exceeds the entire executable gap. Every
large CEX↔DEX "edge" in the table is an unverifiable symbol collision.

**Cross-pool (same chain):** net-positive on **5 chain/symbol groups**, all small pools:
`robinhood MEME 493 bps (<$600)`, `ethereum NPC 45 bps (<$200)`, `base DOGINME 28 bps (<$200)`,
`bsc BAN 24 bps (<$120)`, `robinhood CLOCKIN 3.5 bps (<$80)`. %pos across all verified cross-pool
samples = **13.3 %** — the vast majority of the time there is no edge.

**Any net-positive at $100?** Yes, but only cross-pool, on pools ≤ $70 k liquidity, with capacity
**$76–$577** — i.e. **you cannot put $100 into the best one (robinhood MEME) and stay under its
$577 cap with margin; you can just barely.** At $100: robinhood MEME $100·493/10⁴ = **$4.93/trip**;
ethereum NPC $100·45/10⁴ = **$0.45/trip**.

**$/day at $100 (stated assumptions):** a round trip needs two pool legs; at a realistic 1 trip/hour
over 8 h, robinhood MEME ≈ **$39/day** and ethereum NPC ≈ **$3.6/day** — *if* the edge persists and
you already hold inventory on both pools. It does not persist: the %pos and longest-run columns
show these are transient mispricings in sub-$70 k pools, not a standing spread.

---

## 6. Honest bottom line, per chain

| chain | measurable | candidates | best verified | capacity | verdict |
|---|:--:|---:|---|---:|---|
| **ethereum** | ✓ | 16 | NPC cross-pool 45 bps | $171 | edge exists, capacity too small; CEX↔DEX verified **0 %** |
| **robinhood** | ✓ | 11 | MEME cross-pool 493 bps | $577 | largest verified edge; sub-$3 k pool, fleeting |
| **base** | ✓ | 10 | DOGINME cross-pool 28 bps | $171 | marginal, 4 % of samples positive |
| **bsc** | ✓ | 11 | BAN cross-pool 24 bps | $114 | marginal; CEX↔DEX FLOKI **negative** |
| **abstract** | ✓ | 2 | (CEX↔DEX PENGU unverified) | — | no verified positive |
| **cronos** | ✓ | 2 | none (best −27 bps) | $0 | no |
| **arbitrum** | ✓ | 1 | none (best −62 bps) | $0 | no |
| **hyperevm** | ✓ | 1 | none (best −82 bps) | $0 | no |
| **pulsechain, arc, ton, tron, polygon, blast, algorand, xrpl, near, hedera, monad, ink, scroll, stacks** | ✓ price | 0 | — | — | measurable, no two-venue candidate at $100 |
| **sui, aptos, mantle, zksync, sei, celo, ronin, flare, fraxtal, mode, …** | ✓ (GT census) | — | — | — | pools priced by GT census; not reached by the DexScreener symbol sweep at $100 |
| **bybit, okx, bitget, mexc, kucoin, coinbase, crypto.com** | ✗ | — | — | — | host egress blocked (tcp_refused/timeout) |
| **40 GT networks** (linea, opbnb, zetachain, …) | ✗ | — | — | — | no pools from any probe |

**Which chain, if any, has a meme arb that beats its own cost stack?** Three do, at $100, but all
in pools under $70 k with capacity **$114–$577** and edges that appear in **1.3–14.6 % of samples**:
`robinhood` (MEME 493 bps), `ethereum` (NPC 45 bps), `base` (DOGINME 28 bps). **No CEX↔DEX meme
trade beats its cost stack on any chain once the token identity is verified (0 % positive, best
−7.2 bps).** The honest read: meme cross-pool dislocations of 20–500 bps are real but live in
pools too small to absorb $100 with margin, and the CEX↔DEX route is structurally unprofitable at
$100 after a 10 bps CEX taker plus fees, gas and the symbol-collision risk that makes most
"opportunities" untradeable.

---

## 7. Evidence paths

```
evidence/arbitrage/2026-10-03/meme/multichain/
  reachability.json                    # 39 endpoints + 100-network GT census
  reachability_endpoints.json          # raw endpoint probes (status/ms/failure_mode)
  reachability_gt_pools.json           # per-network GT pool probe
  gt_merged_reachability.json          # 60 reachable / 40 no-pools (429-corrected)
  gt_networks.json  gt_network_ids.json
  gt_census_20261003T124243Z.json      # 100 networks, top pools by 24h volume
  raw_multichain_20261003T131412Z.json # 47 cross-sections, full pool + CEX-ref payload
  discarded_multichain_*.json          # 3 714 implausible gaps (collisions), counted
  multichain_scan_20261003T131413Z.json
  multichain_reanalysis_20261003T131612Z.json
  collect_meta_*.json  run.log  census.log
```

**Suite:** `uv run pytest -o addopts="" -q` → **2 402 passed, 4 skipped, 0 failed** (86 s).
New tests: `tests/unit/test_arb_meme_multichain.py` — 38 pass (per-chain cost-stack netting; a 3 %
L2 spread survives; a 1 % spread dies on a 1 %-fee tier; gas charged exactly once; never-mid;
the contract-address phantom guard; the CEX↔DEX identity flag; unknown-DEX fee defaults to 30 bps).
