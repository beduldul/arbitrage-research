# Spot-only arbitrage at $100 — classes A & B (measurement only)

**Date:** 2026-10-03  |  **Notional:** $100.00  |  **Window:** 28.0 min @ 5 s, **336 samples** each
**Mode:** measurement only — no orders, no keys, no `.env` edits, no transfers, no accounts.
**Public endpoints only.** Concurrency 3, exponential backoff on 429/418/5xx → **0 rate-limits hit**.

Sub-reports (full tables, all numbers): `stable/REPORT.md`, `crossvenue/REPORT.md`.

---

## 1. Reachability matrix (one probe each; status + latency)

| endpoint | HTTP | latency | verdict |
|---|---:|---:|---|
| `data-api.binance.vision` `/api/v3/depth` BTCUSDT | 200 | 465 ms | **reachable** |
| … stable pairs USDCUSDT / FDUSDUSDT / TUSDUSDT / USD1USDT | 200 | 92–118 ms | **reachable** |
| … USDPUSDT | 200 | 155 ms | reachable but **EMPTY book** (45-byte response) |
| … FDUSDUSDC (cross-stable) | 200 | 118 ms | **reachable** |
| `api.gateio.ws` `/api/v4/spot/order_book` (all pairs) | 200 | 96–438 ms | **reachable** |
| `api.huobi.pro` (HTX) `/market/depth` (all pairs) | 200 | 95–136 ms | **reachable** |
| `api.bybit.com` `/v5/market/orderbook` | **000** | — | **blocked** — DNS → `202.169.44.80` (blackhole) |
| `www.okx.com` `/api/v5/market/books` | **000** | — | **blocked** — same blackhole IP |
| `api.kraken.com` `/0/public/Depth` | **000** | — | **blocked** — same blackhole IP |
| `api.binance.com` (direct, non-mirror) | **000** | — | **blocked** — same blackhole IP |
| kucoin / mexc / bitget / coinbase / htx.com / gemini / coinex / bitfinex / crypto.com | **000** | — | **blocked** — same blackhole IP |
| fee-schedule pages (`binance.com/en/fee`, `gate.io/fee`, `htx.com/fee`) | **000** | — | **blocked** — fee tiers **UNVERIFIED** |

**The host's ISP sinkholes the DNS of almost every major venue to one blackhole IP.**
Only **Binance Vision, Gate.io, HTX** answer. Consequence: class B is a **three-venue**
measurement (not the five the brief hoped for), and every venue fee is carried as
**UNVERIFIED** because the pages that would verify it are unreachable.

---

## 2. Class A — stablecoin cross-rate / depeg

$100 round trip, **buy at ask → sell at bid**, depth-walked. Headline fee = the project's
own §10.2 base tier, **10 bps/leg = 20 bps round trip**; maker what-if = 2 bps/leg = 4 bps.

| venue | pair | spread bps | gross bps | net taker mean/max bps | p90 | %pos | run | net maker mean | max +$ |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| binance | USDCUSDT | 0.10 | −0.10 | −20.09 / −20.09 | −20.09 | 0.0% | 0 | −4.10 | 0 |
| binance | FDUSDUSDT | 1.00 | −1.00 | −20.99 / −20.99 | −20.99 | 0.0% | 0 | −5.00 | 0 |
| binance | TUSDUSDT | 2.00 | −2.00 | −21.99 / −21.99 | −21.99 | 0.0% | 0 | −6.00 | 0 |
| binance | USD1USDT | 0.10 | −0.10 | −20.09 / −20.09 | −20.09 | 0.0% | 0 | −4.10 | 0 |
| binance | USDPUSDT | — | — | *no executable samples (empty book)* | — | — | — | — | 0 |
| gate | USDCUSDT | 1.00 | −1.00 | −20.99 / −20.99 | −20.99 | 0.0% | 0 | −5.00 | 0 |
| gate | FDUSDUSDT | 1.00 | −1.00 | −20.99 / −20.99 | −20.99 | 0.0% | 0 | −5.00 | 0 |
| gate | TUSDUSDT | 10.03 | −10.03 | −30.00 / −30.00 | −30.00 | 0.0% | 0 | −14.02 | 0 |
| gate | USD1USDT | 1.00 | −1.00 | −20.99 / −20.99 | −20.99 | 0.0% | 0 | −5.00 | 0 |
| gate | USDPUSDT | 195.8 | −817.9 | −836.2 / −523.2 | −527.8 | 0.0% | 0 | −821.5 | 0 |

**Cross-rate triangles** (Binance, `USDT→A→B→USDT`, 3 taker legs = 30 bps):

| triangle | net mean bps | net max bps | %pos |
|---|---:|---:|---:|
| USDT>USDC>FDUSD>USDT | −31.15 | −30.47 | 0.0% |
| USDT>FDUSD>USDC>USDT | −30.88 | −30.47 | 0.0% |

**Verdicts:** pairs with positive **mean** net edge — **0 / 10 at taker, 0 / 10 at the
maker what-if**. **No depeg observed in the window.** Every pair traded at peg; the
round trip's magnitude *is* the spread plus fees, exactly as the model predicts. The one
non-trivial spread is **Gate TUSD/USDT at ~10 bps** — still 3× too wide to trade, and it
is a wide *spread*, not a depeg (the mid never moved off 1.000). Dead/empty pairs
(BUSD, DAI, USDD, PYUSD, TUSD/USDC) are recorded in `stable/REPORT.md`, not dropped.

---

## 3. Class B — cross-venue spot (same pair, 2–3 venues simultaneously)

$100, buy at A's ask → sell the received base at B's bid, **never mid**. Both venues'
taker fees subtracted. Top 6 of 30 directions by gross dislocation:

| direction | pair | gross mean bps | gross **max** bps | net mean bps (pub fees) | net p90 | %pos | max +$ |
|---|---|---:|---:|---:|---:|---:|---:|
| htx→binance | SOLUSDT | +6.98 | **+10.99** | −23.02 | −21.31 | 0.0% | 0 |
| htx→gate | SOLUSDT | +6.61 | +10.99 | −23.39 | −21.61 | 0.0% | 0 |
| htx→binance | BTCUSDT | +3.36 | +4.58 | −26.63 | −25.67 | 0.0% | 0 |
| htx→binance | XRPUSDT | +2.20 | +6.19 | −27.79 | −26.28 | 0.0% | 0 |
| htx→gate | BTCUSDT | +3.07 | +4.45 | −26.92 | −25.81 | 0.0% | 0 |
| binance→gate | ETHUSDT | +0.81 | +2.6 | −19.18 | −18.9 | 0.0% | 0 |

**Verdicts:** directions measured **30**; positive **mean gross** spread **11**; positive
**mean net** at published fees **0**. The largest instantaneous dislocation seen anywhere
was **+11.0 bps** (HTX SOL vs Binance) against a ≥20 bps fee floor — the fee floor is
roughly **2× the entire observed dislocation**, before any slippage, latency, or transfer.

> **Honest caveat — capturability.** A positive book-to-book spread is **NOT** capturable by
> a $100 account that must move funds between venues. Cross-venue arbitrage requires
> **pre-positioned balances on both venues** so both legs settle simultaneously with no
> transfer, and even then it carries withdrawal/transfer latency, network congestion, and
> the risk that the spread closes in flight. A $100 account that must *move* USDT cannot
> capture even the +11 bps high-water mark: the transfer alone costs more than that and
> takes minutes-to-hours. This table measures the spread; it does not claim a capturable edge.

---

## 4. Unit tests (pure computation, no network/filesystem)

`tests/unit/test_arb_stable.py` — **19 tests**, all passing. Pinned:

- pegged stable pair (1 bp raw) **negative** after 20 bps fees (`≈ −21 bps`);
- depeg scenario (+30 bp raw) **positive** after 20 bps fees (`≈ +10 bps`);
- cross-venue netting subtracts **both** venues' fees (10 + 20 → −30 bps on a flat price);
- **bid/ask never mid**: a flat-mid book with a 200 bp spread must read negative;
- **maker tier is really the maker rate** (2 bps/leg, not a silent copy of the 10 bps
  taker) — a regression test for a bug found and fixed during this run;
- depth wall: an unfillable book returns `None` / `0`, never a phantom edge;
- parsers normalise Binance / Gate / HTX shapes and drop malformed levels.

## 5. Evidence paths

```
evidence/arbitrage/2026-10-03/stable_crossvenue_REPORT.md   (this consolidated report)
evidence/arbitrage/2026-10-03/stable/REPORT.md        results.json  run_meta.json  run.log
evidence/arbitrage/2026-10-03/stable/raw/*.jsonl      (11 files, 3,696 timestamped book snapshots)
evidence/arbitrage/2026-10-03/crossvenue/REPORT.md    results.json  run_meta.json  run.log
evidence/arbitrage/2026-10-03/crossvenue/raw/*.jsonl  (15 files, 5,040 timestamped book snapshots)
```

Window: `2026-10-03T06:10:36Z → 06:38:31Z` (336 samples @ ~5 s). Every raw line carries
`ts` (epoch) + `iso`. The earlier 12/9-sample smoke runs are quarantined under
`raw/_warmup/` so the cached window is exactly the reported run; aggregation is
reproducible offline via `--rebuild-from-cache`.

## 6. Test-suite status

`uv run pytest -o addopts="" -q` → **2148 passed, 4 failed, 4 skipped** (the 4 skips are the
opt-in live-network tests). The **4 failures are all in `tests/integration/test_phase2_multipair.py`**
(`pipeline/multi_pair.py` promote-slot logic). They are **not mine and not caused by me**:
my modules are referenced nowhere in `src/` or `tests/integration/` (verified by grep), and
the failing symbol set **changed between two consecutive runs while I edited nothing**
(2 → 4 failures, different symbols) — a sibling workstream is editing that file live
(mtime 13:08, matching its uncommitted `+327` line diff). I did not touch it and did not
re-pin anything. **My own tests: 18/18 pass; my scripts: ruff clean.**

---

## 7. Honest bottom line — is either class viable at $100 with retail fees?

**No. Neither class is viable at $100 with retail fees, and the arithmetic says why.**

- **Class A (stablecoin cross-rate/depeg):** a pegged pair is a *guaranteed* loss of the
  spread **plus 20 bps** — the measured round trips were −20.1 to −30.0 bps, 0% positive,
  longest positive run **0**, max executable positive size **$0**. The only way this class
  pays is a **genuine depeg**, and **none occurred in the 28-minute window** (nor would a
  28-minute sample be expected to catch one — depeg events are rare, minutes-to-hours
  affairs, and by the time a retail taker sees USDC at 0.998 the market-makers have
  usually closed it). So the honest statement is: *no depeg observed in the window*, and
  the strategy would need a depeg **wider than 20 bps** and lasting **longer than the
  round-trip latency** to clear fees at all. That is a rare-event bet, not an edge.
- **Class B (cross-venue spot):** the entire observed dislocation — even its +11 bps
  high-water mark — is **smaller than the ~20 bps round-trip taker fee**, so **0 of 30
  directions** had a positive mean net edge, and **0 of 30** had a positive net edge at
  *any* instant. On top of that, capture requires pre-positioned balances on both venues
  and is not available to a $100 account that must transfer.

This is the same cost-floor result the project's own NEXUS falsification work reached
(`COSTFLOOR.md`, `COSTFIX.md`): **the retail fee floor, not the signal, is what defeats
these strategies.** Nothing here changes that. The measurement is the deliverable; there
is no edge to claim.
