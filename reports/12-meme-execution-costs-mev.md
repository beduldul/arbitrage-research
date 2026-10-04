# Solana meme arbitrage — execution cost and the MEV race (retail $100 basis)

**Measurement only.** Public endpoints, no keys, no wallets, no orders, no account
state, no transactions. Raw responses cached under
`evidence/arbitrage/2026-10-03/meme/exec/`. Generated 2026-10-03, SOL ≈ **$119.4**.

The question: **even if a meme spread exists, can a retail $100 account capture it?**
The answer is decided by two measured things — the cost floor and the competitive
field — plus one thing that is not modelled away: slippage at size.

Script: `scripts/arb_meme_exec.py` · Tests: `tests/unit/test_arb_meme_exec.py`

---

## 1. Fee / tip endpoint matrix (probed, with real numbers)

| endpoint | method | status | latency (p50) | result |
|---|---|---|---|---|
| `https://bundles.jito.wtf/api/v1/bundles/tip_floor` | GET | **200** | ~330 ms | ✅ live tip floor (SOL percentiles) |
| `https://api.jito.wtf/api/v1/bundles/tip_floor` | GET | **000** | — | ❌ unreachable (DNS/conn) |
| `https://kobe.mainnet.jito.network/api/v1/tip_floor` | GET | **404** | — | ❌ not a valid path |
| `https://kobe.mainnet.jito.network/api/v1/validators` | GET | **200** | — | ✅ validator MEV field (used for §3) |
| `https://api.mainnet-beta.solana.com` `getRecentPrioritizationFees` | POST | **200** | ~93 ms | ✅ fee distribution **with hot accounts** |
| `https://solana-rpc.publicnode.com` `getRecentPrioritizationFees` | POST | **200** | — | ✅ identical distribution |
| `https://rpc.ankr.com/solana` | POST | **403** | — | ❌ "API key is not allowed to access blockchain" |
| `https://solana.drpc.org` | POST | **error** | — | ❌ "chain is not available on free plan" |
| `https://api.helius.xyz/v0/...` | GET | **401** | — | ❌ requires API key |
| `https://api.mainnet.orca.so/v1/whirlpool/list` | GET | **200** | ~4 s | ✅ per-pool `lpFeeRate` (used for §4b) |

**Critical measurement note on `getRecentPrioritizationFees`.** With an empty account
list (`params: [[]]`) every public RPC returns **all zeros** — a naive probe would
report "priority fees are free." Passing the hot writable accounts (SOL, USDC,
pump.fun, Jupiter v6, ComputeBudget) turns the same call into a real distribution:

| percentile | micro-lamports/CU | @ 400,000 CU (2 legs) |
|---|---|---|
| p25 / p50 / p75 | 0 | 0 lamports |
| **p90** | 189,474 | 75,790 lamports = $0.0090 |
| **p95** | 346,534 | 138,614 lamports = $0.0166 |
| **p99** | 1,744,440 | 697,776 lamports = $0.0833 |
| max | 1,900,000 | 760,000 lamports |

**The median priority fee is 0 and is therefore useless** — only 20.7% of slots show a
nonzero fee. The fee that lands a transaction in a *contested* slot is the p90+ tail.

### Jito tip floor (measured, SOL percentiles → USD at $119.36)

| percentile | SOL | USD |
|---|---|---|
| p25 | 1.565e-6 | $0.000187 |
| **p50** | 6.595e-6 | **$0.000787** |
| **p75** | 2.009e-5 | **$0.002397** |
| p95 | 1.677e-4 | $0.020014 |
| p99 | 5.152e-4 | $0.061494 |

The tip floor endpoint serves **only the latest snapshot** (no history window; `?limit=`
is ignored). The time series in §3 is therefore built from our own 4–6 min sampling
window, not from a historical API.

---

## 2. Total execution cost for one atomic two-leg arb, at $100

Model (lamports): `base_fee(5,000 × signatures) + priority(µLamports/CU × CU / 1e6) +
jito_tip + rent`. An atomic two-leg DEX arb is **one transaction, one signature, two
swap instructions**; base fee is per *signature* (5,000, not 10,000), priority fee is
per CU for the whole tx (400,000 CU central, 200k/leg).

| component | lamports | USD | **bps of $100** |
|---|---|---|---|
| base fee (1 sig) | 5,000 | $0.000597 | 0.060 |
| priority p90 | 75,790 | $0.009049 | 0.905 |
| priority p95 | 138,614 | $0.016550 | 1.655 |
| priority p99 | 697,776 | $0.083314 | 8.331 |
| tip p50 | 6,595 | $0.000787 | 0.079 |
| tip p75 | 20,090 | $0.002399 | 0.240 |
| tip p95 | 167,700 | $0.020023 | 2.002 |
| tip p99 | 515,200 | $0.061515 | 6.152 |
| **ATA rent (new token acct)** | **2,039,280** | **$0.243490** | **24.349** |

**Composed stacks (the number the spread must beat), at $100:**

| regime | priority + tip | total | USD | **bps of $100** |
|---|---|---|---|---|
| floor (p90 prio, p50 tip) | 189,474 + 6.6e-6 SOL | 87,385 lam | $0.01043 | **1.04** |
| competitive (p95 prio, p75 tip) | 346,534 + 2.0e-5 SOL | 163,704 lam | $0.01955 | **1.95** |
| aggressive (p99 prio, p95 tip) | 1,744,440 + 1.7e-4 SOL | 870,476 lam | $0.10394 | **10.39** |

**Rent is refundable** — the 24.35 bps is a *capital lock-up*, not a sunk cost, and is
reported separately for that reason. It only appears if a new token account is opened.

**At $1,000 the on-chain fee floor is 0.10–1.04 bps** — i.e. the on-chain fee stack is
*not* what stops a $100 account. The bar that does is the pool fee (§4b).

---

## 3. The race — who actually captures Solana DEX arbitrage

**The priority lane is the Jito lane.** Measured on 2026-10-03 via
`getVoteAccounts` + `kobe.../validators`:

- Jito-connected active stake: **436,786,159 SOL** across 648 validators
- Total network active stake: **442,013,190 SOL**
- → **98.8% of staked SOL is Jito-connected.**

**Stake-Weighted QoS (SWQoS)** reserves **80% of a leader's TPU capacity** for
connections proxied through staked validators; everyone else competes for the remaining
**20% "spam lane."** Sources: [Solana SWQoS guide](https://solana.com/developers/guides/advanced/stake-weighted-qos),
[ERPC](https://erpc.global/en/staked-connection). A retail HTTP client to a public RPC
has no stake, no staked-validator peering, and therefore sits in the crowded 20% — *before
any fee is even considered*.

**Latency bands and capture rate** (industry-measured, [Dwellir](https://www.dwellir.com/blog/mev-arbitrage-bot-infrastructure)):

| p95 latency | expected capture rate | position |
|---|---|---|
| <30 ms | 80–90% | highly competitive |
| 30–100 ms | 50–70% | competitive |
| 100–200 ms | 20–40% | marginal |
| >200 ms | <10% | non-competitive |

**Our measured REST round-trip (this study):** Jupiter quote **p50 ~116 ms** (up to
2.5 s under load), Jito tip-floor **~330–440 ms**, RPC `getRecentPrioritizationFees`
**~93 ms**. The prior workstream measured **~90–460 ms** round-trip. A retail HTTP
client therefore lands in the **100–460 ms** band → **20% capture at best, often <10%.**

**Tip-floor movement.** Across our sampling window the p50 tip moved between
**1.0e-6 and 1.0e-4 SOL** (a ~100× swing) and the p99 between **6.7e-5 and 4.1e-3 SOL**;
the 95th/99th percentiles sit two-to-three orders of magnitude above the median. The
floor is not static — it is bid up by searchers whose whole business is paying it.

**Structural conclusion.** A retail HTTP client competes in the 20% lane, at 100–460 ms,
against co-located searchers receiving shreds directly from leaders (ShredStream "saving
hundreds of milliseconds") and submitting through the staked 80% lane. It can see the
spread; it cannot reliably land the transaction that captures it. This is stated as
structure, not modelled away.

---

## 4. Slippage at size — the bound independent of the spread

### 4a. Executable price impact (Jupiter quote, USDC → token, reference $10)

| token | $10 | **$100** | $1,000 |
|---|---|---|---|
| BONK | 0 (ref) | −2.89 bps | 5.28 bps |
| WIF | 0 (ref) | 4.33 bps | 37.42 bps |
| POPCAT | 0 (ref) | 2.82 bps | 23.65 bps |
| PENGU | 0 (ref) | −1.28 bps | −2.22 bps |
| TRUMP | 0 (ref) | 0.47 bps | 0.84 bps |
| **mean** | — | **~0.7 bps** | **~13.0 bps** |

At **$100 the price impact is under 5 bps** (within quote noise; negative values are
route-selection jitter, not a rebate). At **$1,000 it rises to 5–37 bps**. **Slippage at
retail size is not the binding constraint** — which is exactly why the pool fee matters
so much.

### 4b. The pool fee — the dominant cost, measured (Orca `lpFeeRate`, deepest pool)

| token | deepest pool | fee / leg | **round trip** | TVL |
|---|---|---|---|---|
| BONK | BONK/SOL | 30 bps | **60 bps** | $1,142,142 |
| PENGU | PENGU/SOL | 30 bps | **60 bps** | $2,454,615 |
| TRUMP | TRUMP/USDC | 16 bps | **32 bps** | $760,971 |
| POPCAT | POPCAT/SOL | 5 bps | **10 bps** | $729,887 |
| WIF | WIF/SOL | 4 bps | **8 bps** | $528,316 |
| | | | **mean 34 bps** | |

This fee is **inside every Jupiter quote** — it is not added on top, but it is a cost
the spread must still overcome, and an arb round trip pays it **twice**. Meme pools
charge **1–2%** (100–200 bps) for the *thin* pools; even the deepest charge 16–30 bps
per leg. Compare Orca's major pairs: SOL/USDC 4 bps, SOL/JitoSOL 1 bps.

---

## 5. Bottom line — the spread a retail $100 account must beat

| cost layer | bps of $100 | measured? |
|---|---|---|
| on-chain fee stack (base + priority p90–p95 + tip p50–p75) | **1.0 – 2.0** | ✅ measured |
| pool fee, round trip (2 swaps) | **8 – 60** (mean 34) | ✅ measured |
| price impact at $100 | **~1 – 5** | ✅ measured |
| ATA rent (only if opening a new token account) | 24.35 (refundable) | ✅ protocol constant |
| **realistic bar** | **≈ 35 – 60 bps** | |

**What spread must a Solana meme arb show for a retail $100 account to profit?**
**≈ 35–40 bps** for the deepest meme pools (WIF/POPCAT), rising to **≈ 60–70 bps** for
BONK/PENGU and **>200 bps** for thin meme pools. And this is a *floor*: it excludes the
race, so the *capturable* bar is higher again.

**Is that spread plausible?** **No, not at retail.** Two independent measurements agree:

1. The sibling workstream (`evidence/arbitrage/2026-10-03/meme/MEME_REPORT.txt`) measured
   **17 venue-pairs and found 0 (0.0%) ever net-positive**; its own Solana added-cost
   stack was **10.7 bps** — which, added to the 34 bps pool-fee round trip measured here,
   reproduces the ~35–60 bps bar and leaves no room for the spread.
2. The spread must be captured in the **20% unstaked lane at 100–460 ms**, against
   searchers in the **80% staked lane at sub-30 ms**. Even a spread that *existed* would
   be taken before a retail HTTP client could land the transaction.

**Verdict: the meme-arb thesis fails at retail on the cost floor alone (~35–60 bps
required), before the race is even considered.** The on-chain fee stack is cheap
(1–2 bps); the pool fee and the competitive field are not.

---

## Evidence

| path | contents |
|---|---|
| `evidence/.../meme/exec/raw_exec_*.json` | fee/tip/slippage window (summary + verdict) |
| `evidence/.../meme/exec/raw_slippage_*.json` | standalone slippage-at-size table (10/100/1000) |
| `evidence/.../meme/exec/raw_pool_fees_*.json` | Orca per-pool `lpFeeRate` |
| `evidence/.../meme/exec/kobe_validators_*.json` | Jito validator MEV field |
| `evidence/.../meme/exec/vote_accounts_*.json` | network stake (SWQoS share) |
| `scripts/arb_meme_exec.py` | measurement + pure cost/break-even computation |
| `tests/unit/test_arb_meme_exec.py` | 28 pure-computation tests |

**Assumptions, labelled (not measured):** CU budget 400,000 for the two-leg tx
(exposed as a low/central/high band); ATA rent 0.00203928 SOL (protocol constant,
refundable). **Rate-limit note:** Jupiter `lite-api` returned 429s during long combined
windows (27–45 per run), which is why the slippage table is taken from the dedicated
`--slippage-only` pass; the fee/tip series is unaffected (separate endpoints).
