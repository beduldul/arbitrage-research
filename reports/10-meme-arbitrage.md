# MEME-COIN ARBITRAGE REPORT — 2026-10-03

**Permanent record of the meme-coin arbitrage workstream run on 2026-10-03, later the
same day as the earlier CEX / funding programme.**

Measurement only. No orders, no keys, no wallets, no transfers, no account state, no
transactions, and none may be added. Public unauthenticated endpoints only. No `.env`,
gate, config or pipeline edits. Every number below is traceable to a source file in
§9; where two sources disagree the disagreement is reported rather than silently
resolved.

- **Capital basis:** $100.00 notional (the project's retail paper account).
- **Date:** 2026-10-03. Solana reference price SOL ≈ **$119.4**.
- **Companion reports:** the earlier programme is
  [`ARBITRAGE_PROGRAMME_REPORT.md`](./ARBITRAGE_PROGRAMME_REPORT.md); this report
  appends to it and does not replace it.
- **Sources of truth:** `meme/MEME_REPORT.txt` (Solana cross-venue + EVM),
  `meme/exec/EXEC_REPORT.md` (execution cost + MEV race), `meme/multichain/REPORT.md`
  (non-Solana chains + CEX↔DEX), `meme/sim_meme.json` (the $100 paper simulator).

---

## 0. Executive verdict

The earlier programme found the CEX floor — a 10 bps taker on each of two legs plus a
withdrawal fee — defeats every class at $100. Meme coins were worth testing separately
because the **on-chain** version of that trade removes the CEX taker and withdrawal
fees entirely and replaces them with a fee that is, at $100, **1–2 bps** (§3). If the
fee floor was the whole obstacle, an on-chain cross-DEX meme round trip should be
where it finally clears.

It does not. Three independent measurements agree:

1. **Solana cross-venue (90 venue-pairs).** **0 of 90 cross-venue venue-pairs were ever
   net-positive.** The **widest gross spread** ever seen was **4.78 bps** against a
   **35–60 bps required bar**; the best cross-venue net was **−5.43 bps**; **$0.0000/day
   at $100**.
2. **Non-Solana chains (22 chains measured).** Cross-pool positives exist, but only in
   pools **≤ $70 k liquidity** with capacity **$76–$577**, appearing in **1.3–14.6 %**
   of samples — **capacity-bound and transient, not a strategy**. Verified CEX↔DEX was
   **0 % net-positive, best −7.2 bps**.
3. **The MEV race.** Even a spread that existed could not be landed: retail sits in the
   **20 % unstaked lane at 100–460 ms** against searchers in the **80 % staked lane at
   sub-30 ms**, with **Jito = 98.8 %** of staked SOL.

**One-line summary:** *the CEX floor was not the only obstacle — removing it exposes a
pool-fee floor (8–60 bps round trip) and an MEV race that retail cannot win, and the
only positive spreads found live in pools too small to absorb $100.*

---

## 1. Why meme coins were worth testing separately

The earlier programme's defeat mechanism was uniform: the **retail fee floor**, not the
absence of signal. A meme cross-DEX round trip is structurally different in three ways,
and each removes a fee the earlier classes paid:

| earlier class paid | on-chain meme round trip pays |
|---|---|
| CEX spot taker 10 bps **per leg** (20 bps RT) | **no CEX taker fee** — DEX swap only |
| withdrawal fee (CEX-DEX: **120 bps** @ $100) | **no withdrawal fee** — the token never leaves the chain |
| gas ~0.5 bps + 10 bps slippage buffer | **gas $0.0006 on Solana** (1–2 bps total on-chain stack) |

The hypothesis was therefore narrow and testable: **on a chain where gas is ~$0.001 and
there is no CEX taker fee, do meme-pool cross-DEX spreads exceed the real cost stack at
$100?** The scripts were written to answer exactly that, with the pool fee and price
impact left **inside** the executable quote (never subtracted twice) and only gas,
priority fee, Jito tip and a labelled slippage buffer added on top — see §3 and §9.

The answer, in one sentence: the CEX fee floor is replaced by a **pool-fee floor** that
is *larger* for meme pools, and by an **MEV race** that no retail HTTP client wins.

---

## 2. Reachability — which chains, venues and aggregators actually answer

Reachability was probed directly and recorded with HTTP status and latency
(`meme/meme_report_reanalyzed_20261003T135753Z.json` → `reachability`). This is an
operational finding, not a footnote: a two-venue strategy needs *simultaneous* access to
both legs.

### 2.1 Solana price sources

| endpoint | status | latency | verdict |
|---|---:|---:|---|
| `lite-api.jup.ag/swap/v1/quote` | 200 | 188.6 ms | ✅ executable quote (primary) |
| `lite-api.jup.ag/swap/v1/quote?dexes=Raydium` | 200 | 35.3 ms | ✅ per-venue executable quote |
| `lite-api.jup.ag/price/v3` | 200 | 49.4 ms | ✅ reference price |
| `lite-api.jup.ag/swap/v1/program-id-to-label` | 200 | 33.9 ms | ✅ DEX label map |
| `api-v3.raydium.io/pools/info/list` | 200 | 175.5 ms | ✅ pool census |
| `api.orca.so/v2/solana/pools` | 200 | 172.6 ms | ✅ per-pool `feeRate` |
| `api.geckoterminal.com/.../solana/pools` | 200 | 74.8 ms | ✅ secondary census |
| `api.dexscreener.com/latest/dex/search` | 200 | 96.3 ms | ✅ multi-chain search |
| `bundles.jito.wtf/api/v1/bundles/tip_floor` | 200 | 348.5 ms | ✅ live Jito tip floor |
| `dlmm-api.meteora.ag/pair/all` | **404** | 108.5 ms | ❌ path retired |
| `frontend-api.pump.fun/...` | **530** | 78.5 ms | ❌ Cloudflare origin DNS error |
| `public-api.birdeye.so/defi/price` | **401** | 88.1 ms | ❌ requires API key |

**9 of 12 Solana endpoints answered; 3 were blocked** — one retired path, one
Cloudflare-blocked frontend, one key-gated. The Jupiter `lite-api` per-venue `dexes=`
constraint is what makes executable cross-venue quotes possible without keys.

### 2.2 Multi-chain and CEX controls (non-Solana workstream)

| endpoint | status | verdict |
|---|---:|---|
| `api.dexscreener.com` (search / pairs / tokens) | 200 | **primary multi-chain source** — one call covers every chain |
| `api.geckoterminal.com` (networks / pools) | 200 | reachable but **rate-limited ~30/min → 429** |
| `data-api.binance.vision` (bookTicker / 24hr) | 200 | ✅ CEX control |
| `api.gateio.ws` spot tickers | 200 | ✅ CEX control |
| `api.huobi.pro` merged | 200 | ✅ CEX control |
| `api.hyperliquid.xyz/info` | 200 | ✅ perp reference |
| `api.bybit.com` / `www.okx.com` | 000 | ❌ tcp_refused (host egress block) |
| `api.bitget.com` / `api.mexc.com` / `api.kucoin.com` | 000 | ❌ timeout |
| `api.exchange.coinbase.com` / `api.crypto.com` | 000 | ❌ tcp_refused |
| `api.thegraph.com` (Uniswap v3) | 000 | ❌ DNS fail — host retired |
| `api.aerodrome.finance` | 000 | ❌ DNS fail |
| `api.pancakeswap.info` | 500 | ❌ dead endpoint |
| `api.1inch.dev` quote | **401** | ❌ needs API key |
| `api.0x.org` quote | 404 | ❌ path retired |
| `apilist.tronscanapi.com` | 404 | ❌ path retired |
| `rpc.mainnet.near.org` | 405 | ❌ POST-only |
| `blockstream.info` | 000 | ❌ tcp_refused |

**No chain-specific DEX API answered keyless.** Both EVM aggregators (1inch, 0x) are
key-gated or retired; the graph is DNS-dead. **DexScreener + GeckoTerminal are the only
working multi-chain price feeds**, and the CEX controls are Binance-vision / Gate / Huobi
/ Hyperliquid only.

### 2.3 Chain coverage

- **GeckoTerminal census (100 networks probed, paced 0.4 req/s): 60 of 100 networks
  returned pools; 40 returned none** from any probe (linea, opbnb, zetachain,
  polygon-zkevm, starknet-alpha, shibarium, hedera-hashgraph, filecoin, and 32 others).
- **DexScreener symbol sweep reached 22 distinct chains**; **8 chains had a two-venue
  candidate** at $100: `ethereum (16 groups), bsc (11), base (10), robinhood (11),
  cronos (2), abstract (2), arbitrum (1), hyperevm (1)`. The other 14 were
  price-measurable but had no two-venue candidate (single pool, or sub-$20 k liquidity).
- **Blocked outright:** bybit, okx, bitget, mexc, kucoin, coinbase, crypto.com.

Reachability is **intermittent across the day** (the same hosts answered 200 and HTTP
000 minutes apart in the earlier programme) — the operational caveat in
`ARBITRAGE_PROGRAMME_REPORT.md` §2.6 applies here unchanged.

---

## 3. The measured cost stack, per chain class

Costs are split into **on-chain landing cost** (measured) and **pool fee** (measured from
each pool's own `feeRate` / `lpFeeRate`). Pool fee and price impact are **inside every
executable quote**; they are not added on top again.

### 3.1 Solana — the on-chain landing stack (measured)

Source: `meme/MEME_REPORT.txt` → `cost_stack_solana`; `meme/exec/EXEC_REPORT.md` §2.

| component | value | source |
|---|---|---|
| base fee | $0.000597 (5,000 lamports @ SOL=$119.49) | measured |
| priority fee | **$0.000000** (p75 = 0 µLamports/CU × 300,000 CU) | measured |
| Jito tip | $0.001536 (75th-percentile landed tip) | measured |
| slippage buffer | 10.0 bps | labelled assumption |
| **total added** | **10.213 bps of $100 ($0.0021)** | |

The single-transaction execution-cost model (exec report §2) composes the landing stack
into three regimes:

| regime | priority + tip | total | **bps of $100** |
|---|---|---:|---:|
| floor (p90 prio, p50 tip) | 189,474 + 6.6e-6 SOL | $0.01043 | **1.04** |
| competitive (p95 prio, p75 tip) | 346,534 + 2.0e-5 SOL | $0.01955 | **1.95** |
| aggressive (p99 prio, p95 tip) | 1,744,440 + 1.7e-4 SOL | $0.10394 | **10.39** |

Two measurement notes that matter:

- **The median priority fee is 0 and is therefore useless.** With an *empty* account
  list every public RPC returns all zeros — a naive probe would report "priority fees
  are free." Passing the hot writable accounts (SOL, USDC, pump.fun, Jupiter v6,
  ComputeBudget) turns the same call into a real distribution, and **only 20.7 % of slots
  show a nonzero fee**. The fee that lands a transaction in a *contested* slot is the
  p90+ tail (p90 = 189,474 µLamports/CU).
- **ATA rent (2,039,280 lamports = $0.2435 = 24.35 bps) is refundable.** It is a
  *capital lock-up*, not a sunk cost, and is reported separately. It appears only if a
  new token account is opened.
- **At $1,000 the on-chain fee floor is 0.10–1.04 bps** — i.e. the on-chain fee stack is
  **not** what stops a $100 account.

### 3.2 The pool fee — the dominant cost (measured)

Source: `meme/exec/EXEC_REPORT.md` §4b (Orca `lpFeeRate`, deepest pool per token).

| token | deepest pool | fee / leg | **round trip** | TVL |
|---|---|---:|---:|---:|
| BONK | BONK/SOL | 30 bps | **60 bps** | $1,142,142 |
| PENGU | PENGU/SOL | 30 bps | **60 bps** | $2,454,615 |
| TRUMP | TRUMP/USDC | 16 bps | **32 bps** | $760,971 |
| POPCAT | POPCAT/SOL | 5 bps | **10 bps** | $729,887 |
| WIF | WIF/SOL | 4 bps | **8 bps** | $528,316 |
| | | | **mean 34 bps** | |

Even the **deepest** meme pools charge 16–30 bps per leg; thin meme pools charge 1–2 %
(100–200 bps). For comparison, Orca's major pairs charge SOL/USDC 4 bps and SOL/JitoSOL
1 bps. **An arbitrage round trip pays the pool fee twice.**

### 3.3 Price impact at $100 (measured)

Source: `meme/exec/EXEC_REPORT.md` §4a (Jupiter quote, USDC → token, reference $10).

| token | $100 impact | $1,000 impact |
|---|---:|---:|
| BONK | −2.89 bps | 5.28 bps |
| WIF | 4.33 bps | 37.42 bps |
| POPCAT | 2.82 bps | 23.65 bps |
| PENGU | −1.28 bps | −2.22 bps |
| TRUMP | 0.47 bps | 0.84 bps |
| **mean** | **~0.7 bps** | **~13.0 bps** |

At **$100 the price impact is under 5 bps** (negative values are route-selection jitter,
not a rebate). At $1,000 it rises to 5–37 bps. **Slippage at retail size is not the
binding constraint** — which is exactly why the pool fee matters so much.

### 3.4 EVM gas ranges (measured gas price × labelled gas assumption)

Source: `meme/meme_report_reanalyzed_20261003T135753Z.json` → `cost_stack_evm`. Assumed
150,000 gas per round trip (labelled).

| chain | native price | gas price | gas / round trip |
|---|---:|---:|---:|
| ethereum | $2,680.96 | 0.0694 gwei | **$0.0279** |
| bsc | $773.21 | 0.05 gwei | **$0.0058** |
| base | $2,678.87 | 0.006 gwei | **$0.0024** |

The multichain workstream additionally carries **labelled native-gas ranges** per chain
(not measured): base 0.005–0.10, arbitrum 0.01–0.30, bsc 0.05–0.60, polygon 0.005–0.10,
ethereum 0.50–15, tron 0.30–3.00, ton 0.01–0.10, and others. **A chain with no labelled
range reports `[0, 5]` — a gap, not a zero.** EVM gas is charged **once** per round trip.

### 3.5 The composed bar

| cost layer | bps of $100 | measured? |
|---|---|---|
| Solana on-chain landing stack (base + priority p90–p95 + tip p50–p75) | **1.0 – 2.0** | ✅ measured |
| pool fee, round trip (2 swaps) | **8 – 60** (mean 34) | ✅ measured |
| price impact at $100 | **~1 – 5** | ✅ measured |
| ATA rent (only if opening a new token account) | 24.35 (refundable) | ✅ protocol constant |
| **realistic required spread** | **≈ 35 – 60 bps** | |

**≈ 35–40 bps** for the deepest meme pools (WIF/POPCAT), rising to **≈ 60–70 bps** for
BONK/PENGU and **> 200 bps** for thin meme pools. This is a **floor**; it excludes the
race (§5), so the *capturable* bar is higher again.

---

## 4. The spread measurements

### 4.1 Solana cross-venue — 0 of 90 venue-pairs ever net-positive

Source: `meme/MEME_REPORT.txt`; `meme/meme_report_reanalyzed_20261003T135753Z.json`.
Window 1,887.9 s, 11 Solana cycles, effective cycle 171.62 s, 1,375 pair-samples,
6 venues (Raydium, Whirlpool, Meteora DLMM, Raydium CLMM, Meteora, Bonkswap),
7 tokens (BONK, WIF, POPCAT, FARTCOIN, TRUMP, PENGU, MEW).

| metric | value |
|---|---|
| venue-pairs measured | **90** |
| cross-venue venue-pairs **ever net-positive** | **0 of 90** |
| cross-venue samples | 990 |
| same-venue samples (calibration) | 385 |
| **widest gross spread seen** | **4.78 bps** (mean −240.48, worst −1,184.47) |
| **max net edge after cost stack** | **−5.43 bps** (mean −250.67) |
| cross-venue samples net-positive | 0 (0.0 %) |
| best cross-venue pair | `solana:BONK:Raydium CLMM→Whirlpool` (gross 4.78 → net −5.43 bps) |
| **$/day at $100 for best cross-venue pair** | **$0.0000** |

The report's own verdict counts also record **1 venue-pair ever net-positive (0.8 %)** —
that single pair is the **same-venue calibration pair `BONK:Whirlpool→Whirlpool`**, not a
cross-venue trade. The cross-venue verdict — *buy venue ≠ sell venue, the actual trade* —
is **0 net-positive of 990 samples and 0 of 90 cross-venue venue-pairs**. The size sweep
confirms it: for TRUMP Whirlpool→Raydium CLMM, Raydium CLMM→Whirlpool and Meteora
DLMM→Whirlpool, the largest $ that still nets positive is **$0.0**.

**The widest gross spread ever measured (4.78 bps) is roughly one-eighth of the smallest
required bar (35 bps).**

### 4.2 Multi-chain cross-pool — positive only in sub-$70 k pools

Source: `meme/multichain/REPORT.md`. Window 30.0 min @ 20 s → 47 iterations, 0 timeouts,
0 HTTP failures, 2,425 samples. Solana is **excluded by design**.

Cross-pool (same chain, same **contract address**) net-positive on **5 chain/symbol
groups** — every one in a small pool:

| chain | symbol | max net | capacity | % of samples positive |
|---|---|---:|---:|---:|
| robinhood | MEME | 493 bps | **$577** | 100 (47 runs) |
| ethereum | NPC | 45 bps | **$171** | 100 (47 runs) |
| base | DOGINME | 28 bps | **$171** | 4.3 |
| bsc | BAN | 24 bps | **$114** | 14.6 |
| robinhood | CLOCKIN | 3.5 bps | **$76** | 100 (11 runs) |

**% positive across all verified cross-pool samples = 13.3 %**; the individual
positives appear in **1.3–14.6 %** of samples. Every positive lives in a pool
**≤ $70 k liquidity** with capacity **$76–$577** — i.e. **capacity-bound and transient,
NOT a strategy**. At $100: robinhood MEME = $4.93/trip, ethereum NPC = $0.45/trip *if the
edge persists* — and the run-length and %pos columns show it does not.

### 4.3 CEX↔DEX — verified 0 % net-positive

Source: `meme/multichain/REPORT.md` §5. **282 samples, 6 canonical contracts** (ethereum
PEPE/SHIB/FLOKI/TURBO, bsc FLOKI, base AERO):

**0.0 % net-positive. Best = −7.2 bps.**

The classic "CEX-DEX meme" trade is **dead at $100**: the 10 bps CEX taker + 10 bps
buffer + pool fee + gas exceeds the entire executable gap. Every large CEX↔DEX "edge"
in the raw table (up to 1,287 bps) is an **unverifiable symbol collision** — see §6.

---

## 5. The MEV race — even a real spread could not be landed

Source: `meme/exec/EXEC_REPORT.md` §3.

### 5.1 Jito is the priority lane

Measured 2026-10-03 via `getVoteAccounts` + `kobe.mainnet.jito.network/.../validators`:

- Jito-connected active stake: **436,786,159 SOL** across **648 validators**
- Total network active stake: **442,013,190 SOL**
- → **98.8 % of staked SOL is Jito-connected.**

### 5.2 The 80/20 lane split

**Stake-Weighted QoS (SWQoS)** reserves **80 % of a leader's TPU capacity** for
connections proxied through staked validators; everyone else competes for the remaining
**20 % "spam lane."** A retail HTTP client to a public RPC has no stake and no
staked-validator peering, so it sits in the crowded 20 % **before any fee is even
considered**.

### 5.3 Measured retail latency and the capture-rate band

| p95 latency | expected capture rate | position |
|---|---|---|
| <30 ms | 80–90 % | highly competitive (searchers) |
| 30–100 ms | 50–70 % | competitive |
| 100–200 ms | 20–40 % | marginal |
| >200 ms | <10 % | non-competitive |

**Our measured REST round-trip:** Jupiter quote **p50 ~116 ms** (up to 2.5 s under load),
Jito tip-floor **~330–440 ms**, RPC `getRecentPrioritizationFees` **~93 ms**; the prior
workstream measured **~90–460 ms**. A retail HTTP client therefore lands in the
**100–460 ms band → 20 % capture at best, often <10 %.** Co-located searchers receive
shreds directly from leaders (ShredStream "saving hundreds of milliseconds") and submit
through the staked 80 % lane.

The tip floor is not static: across the sampling window the p50 tip moved between
**1.0e-6 and 1.0e-4 SOL** (a ~100× swing) and the p99 between **6.7e-5 and 4.1e-3 SOL**.
It is bid up by searchers whose entire business is paying it.

**Structural conclusion (stated as structure, not modelled away): a retail HTTP client
can see the spread; it cannot reliably land the transaction that captures it.**

---

## 6. The symbol-collision measurement artifact (key methodological finding)

DexScreener's search returns **different tokens that share a ticker**. This is not a
footnote — it is the single most important methodological result of the workstream,
because **unguarded, it produces a large fake "profit."**

**The measurement.** On ethereum alone, search returned **three distinct `TRUMP`
contracts priced 0.03 / 2.34 / 5.05** — a symbol-grouped "spread" of ~100,000 bps that is
really three different assets. The first draft grouped pools **by symbol**:

| guard | robinhood headline | ethereum CEX↔DEX |
|---|---:|---:|
| grouped by **symbol** (first draft) | **2,932 bps** | **1,287 bps** |
| grouped by **(chain, contract address)** | **493 bps** | — |
| + CEX↔DEX canonical-contract allowlist | — | **45 bps** |

So **the naive method would have reported a 2,932 bps "arbitrage" on robinhood and a
1,287 bps "arbitrage" on ethereum** — both phantoms. The largest *verified* edge is
robinhood MEME at 493 bps with capacity **< $600**; the largest verified edge with a
liquid pool is ethereum NPC at 45 bps / < $200. In the raw top-15, **every one of the
eight largest "edges" was unverified.**

**The guards, both pinned by test** (`tests/unit/test_arb_meme_multichain.py`):

1. **Cross-pool rows are keyed by `(chain, contract address)`** — three `TRUMP`
   contracts can never be paired.
   (`test_same_symbol_different_contract_is_not_an_arb`)
2. **CEX↔DEX rows carry `identity_verified`**, true only when the pool's
   `(chain, address)` is a known canonical contract for that symbol. An unknown address
   is *unverified* — the allowlist can never upgrade a collision to verified.
   (`test_unknown_or_wrong_contract_is_unverified`)

**Why this is the key finding:** it is a **retracted-positive class of error**. A
symbol-collision "edge" is not a small measurement bias — it is an order-of-magnitude
fabrication that a pipeline would happily trade on. Any downstream citation of a meme
"spread" must state whether identity was verified; if it was not, the number is
**untradeable by construction**.

---

## 7. The required-spread bar versus the observed spreads

| measurement | observed | required bar | verdict |
|---|---:|---:|---|
| Solana cross-venue, widest **gross** | **4.78 bps** | 35–60 bps | **fails by ~7–12×** |
| Solana cross-venue, best **net** | **−5.43 bps** | > 0 | **negative** |
| Solana $/day at $100 | **$0.0000** | — | **no trade** |
| multi-chain verified cross-pool | 24–493 bps | capacity $76–$577 | **capacity-bound, transient** |
| multi-chain verified CEX↔DEX | **−7.2 bps** best | > 0 | **0 % positive** |
| naive (unguarded) symbol grouping | up to 2,932 bps | — | **phantom — not a trade** |

**The observed spreads are one to two orders of magnitude below the bar on Solana, and
where they do exceed the bar on other chains they are too small to absorb $100.**

---

## 8. Honest verdict and what would change it

### 8.1 Verdict

**The meme-arb thesis fails at retail on the cost floor alone (~35–60 bps required),
before the race is even considered.** The on-chain fee stack is cheap (1–2 bps) — that
was the whole reason to test meme coins separately, and it is real. But the **pool fee
(8–60 bps round trip, mean 34)** and the **MEV race (20 % lane at 100–460 ms vs the 80 %
lane at sub-30 ms)** are not. The only positive spreads found anywhere live in pools
**≤ $70 k** with capacity **$76–$577** and appear in **1.3–14.6 %** of samples — they are
capacity-bound transient mispricings, **not a strategy**.

**What is NOT claimed:**

- **No profit promise.** Nothing here establishes a positive expected return for any
  account size.
- **No validated strategy.** The sub-$70 k cross-pool positives are presented as
  capacity-bound and transient, never as a strategy.
- **No claim the classes are impossible in principle** — only that they are not
  net-positive for a $100 retail taker on the venues and fee schedules measured here.

### 8.2 What WOULD change the answer

1. **Co-located, staked execution.** A client inside the 80 % SWQoS lane at sub-30 ms
   would move from <10–20 % capture to 80–90 % — but that is a *different
   infrastructure and account*, not a tweak to a retail HTTP client.
2. **A liquidity regime where the deepest pools are deeper and the fee tiers lower.**
   The bar is 8–60 bps *because* meme pool fees are 16–30 bps/leg; a venue set with
   1–5 bps fee tiers would lower the bar toward the measured 4.78 bps gross — still
   marginal, but the arithmetic changes.
3. **A dislocation larger than the bar that persists long enough to land.** The
   measured window saw a maximum gross of 4.78 bps; a sustained >35 bps cross-venue
   dislocation would be the first observed instance, not a strategy.
4. **Larger size on the multi-chain positives.** The capacity-bound edges (robinhood
   MEME, ethereum NPC) would need pools deep enough to absorb $100 *with margin* — they
   are not.

### 8.3 The paper simulator — SYNTHETIC arithmetic only

`meme/sim_meme.json` is the $100 paper simulator output:

- `mode = "SYNTHETIC"`, `mode_label = "SYNTHETIC INPUT — NOT EVIDENCE"`, `chain = base`
- `dollars_per_day = 629.52`, `dollars_per_min = 0.437`, `total_net_pnl = 2.18585`
- `constructible_count = 1`, `unconstructible_count = 1`

**These $/day figures are SYNTHETIC-fixture arithmetic only and are never a market
claim.** The simulator exists to prove the *ledger arithmetic* is correct (costs
reconstruct by hand, the capacity guard excludes unconstructible trades, and a profit
line is never printed without its mode label). It consumes REAL measured evidence when
present and falls back to a documented fixture otherwise. **No number in §8.3 may be
quoted as an observed market return.**

---

## 9. Evidence and reproduction index

| path | contents |
|---|---|
| `meme/MEME_REPORT.txt` | Solana cross-venue + EVM coverage summary (90 pairs, 0 cross-venue positive) |
| `meme/meme_report_reanalyzed_20261003T135753Z.json` | final re-analyzed report: reachability, cost stacks, verdict, cross-venue verdict |
| `meme/exec/EXEC_REPORT.md` | execution cost stack, Jito/SWQoS race, slippage-at-size, pool fees |
| `meme/exec/raw_exec_*.json` | fee/tip/slippage window (summary + verdict) |
| `meme/exec/raw_slippage_*.json` | standalone slippage-at-size table (10/100/1000) |
| `meme/exec/raw_pool_fees_*.json` | Orca per-pool `lpFeeRate` |
| `meme/exec/kobe_validators_*.json` | Jito validator MEV field |
| `meme/exec/vote_accounts_*.json` | network stake (SWQoS share) |
| `meme/multichain/REPORT.md` | non-Solana chains + CEX↔DEX, reachability, collision artifact |
| `meme/multichain/reachability*.json`, `gt_*.json` | 39 endpoints + 100-network GT census |
| `meme/multichain/raw_multichain_*.json` | 47 cross-sections, full pool + CEX-ref payload |
| `meme/sim_meme.json` | $100 paper simulator (SYNTHETIC mode) |
| `meme/solana/`, `meme/evm/` | per-cycle raw quotes (11 cycles each) |
| `scripts/arb_meme_solana.py` | Solana cross-venue + EVM measurement |
| `scripts/arb_meme_exec.py` | execution cost + MEV race measurement |
| `scripts/arb_meme_multichain.py` | non-Solana chains + CEX↔DEX measurement |
| `scripts/arb_meme_sim.py` | $100 paper simulator CLI |
| `src/crypto_brain/arb/meme.py` | simulator core (mode label + capacity guard) |
| `tests/unit/test_arb_meme_solana.py` (30) | Solana measurement tests |
| `tests/unit/test_arb_meme_exec.py` (28) | pure cost / break-even tests |
| `tests/unit/test_arb_meme_multichain.py` (38) | per-chain cost netting + collision guards |
| `tests/unit/test_arb_meme_sim.py` (20) | simulator arithmetic + guard tests |

**Assumptions, labelled (not measured):** CU budget 400,000 for the two-leg Solana tx
(exposed as a low/central/high band); ATA rent 0.00203928 SOL (protocol constant,
refundable); EVM gas 150,000 per round trip; pool fee per DEX where the fee endpoint was
unreachable (30 bps default; Uniswap v3 tiers by label); impact = `notional /
(liquidity × 0.5)` on the multichain constant-product model; native gas per chain as a
**range**; CEX withdrawal 0.12–1.20 USD; 10 bps slippage buffer.

---

## 10. Precision rules (binding — this report is the permanent record)

1. Write **"0 of 90 cross-venue venue-pairs ever net-positive"** — never "0/90" alone
   without the cross-venue context. (The report's separate "1 venue-pair ever positive"
   is the same-venue calibration pair `BONK:Whirlpool→Whirlpool`, **not** a cross-venue
   trade.)
2. Write **"widest gross spread 4.78 bps vs a 35–60 bps required bar"** — gross, not net,
   and against the bar, not in isolation.
3. State that the multi-chain cross-pool positives are **capacity-bound ($76–$577) and
   transient (1.3–14.6 % of samples), NOT a strategy**.
4. State that the simulator's **$/day figure is SYNTHETIC-fixture arithmetic only, never
   a market claim.**
5. Note the **symbol-collision artifact explicitly** (§6): unguarded symbol grouping
   produced a 2,932 bps phantom on robinhood and a 1,287 bps phantom on ethereum; the
   guards are `(chain, contract address)` keying and the CEX↔DEX `identity_verified`
   allowlist.
6. Reachability is **intermittent**; blocked venues and 429/401/404/530 failures are
   reported as measured, not smoothed over.
