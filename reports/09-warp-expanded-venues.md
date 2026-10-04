# WARP Arbitrage Measurement — 2026-10-03

**Measurement only.** No keys, no wallets, no orders, no transactions. Public
unauthenticated endpoints; raw responses cached under `evidence/arbitrage/2026-10-03/warp/raw/`.
Cloudflare WARP was on (egress 104.28.250.136, colo SIN) for the whole run.

## What WARP opened

Self-probe (`probe_self.json`), 3 attempts each, all **3/3 HTTP 200**:
bybit, okx, bitget, coinbase, kraken, deribit, **kraken_futures**, **cryptocom**,
**coinbase_intx** — plus, the programme's biggest prior gap, **Binance**:

| endpoint | result |
|---|---|
| `fapi.binance.com/fapi/v1/ping` | 3/3 200 |
| `/fapi/v1/fundingRate`, `/premiumIndex`, `/exchangeInfo`, `/ticker/24hr` | 3/3 200 |
| `dapi.binance.com/dapi/v1/ping` | 3/3 200 |
| `api.binance.com/api/v3/time` | 3/3 200 |
| `fstream.binance.com/ws/...` | 400 (correct — plain GET is not a WS upgrade) |

`/fapi/v1/exchangeInfo`: **920 symbols, 659 USDT perpetuals, 528 with status=TRADING**.
This is the first time the programme can use **live** Binance futures data instead of
monthly S3 dumps.

## 1. Triangular arbitrage on the newly-reachable venues' own books

$100 notional, buy-at-ask/sell-at-bid, each venue's own published taker tier
(Coinbase 60, Kraken 26, Deribit 5 [verified from instrument payload], Crypto.com 7.5,
Bybit 10, OKX 10, Binance 10 bps). Polled ~4.5 min/venue at 7 s; 12 liquid triangles per
venue. Script: `scripts/arb_warp_scan.py tri`.

| venue | triangles enum | samples | best max net bps | mean net bps | % samples positive | longest positive run |
|---|---|---|---|---|---|---|
| coinbase | 39 | 20×12 | −179.2 | −182.8 | 0.0 | 0 |
| kraken | 76 | 19×12 | −74.6 | −76.2 | 0.0 | 0 |
| deribit | 4 | 29×4 | −16.7 | −29.3 | 0.0 | 0 |
| cryptocom | 25 | 19×12 | −21.3 | −30.7 | 0.0 | 0 |
| bybit | 29 | 19×12 | −28.1 | −33.1 | 0.0 | 0 |
| okx | 22 | 19×12 | −27.5 | −31.1 | 0.0 | 0 |
| binance | 110 | 19×12 | −28.2 | −29.4 | 0.0 | 0 |

**305 triangles enumerated, 1,496 polled samples, 0 ever positive.** No triangle even
approached break-even; the widest (Coinbase, 60 bps/leg) loses ~180 bps per cycle.
Matches the earlier programme's 0/100. Full series: `triangular_warp.json`.

## 2. Cross-venue perp funding spread (extended to the new venues)

Same method as `arb_funding_spread.py` (8 h buckets, ≥30 d, sign-persistence +
median-sign + both-halves + block-bootstrap gates, 4 fee events + measured half-spreads,
$100 = $50/leg, per-venue minimum check). 9 venues: binance, deribit, kraken_futures,
coinbase_intx, gate, hyperliquid, bybit, okx, bitget.

- **670 bases listed on ≥2 venues → 3,220 pairs → 849 pass the statistical gates.**
- **603 are honestly executable**: both legs' half-spreads measured *and* capacity ≥ $100
  *and* $50/leg clears the venue minimum. (Coinbase INTX exposes no public order book, so
  its 236 candidates have an *unmeasured* half-spread and their cost is understated — they
  are excluded from the executable set.)
- Best: **`T` long/short binance↔kraken_futures, mean spread 23.9 bps/8h, sign-persistent
  91 % / 15 d, net $0.354/day taker, capacity $1,053** (IS +38.4, OOS +9.7, OOS CI [3.7, 18.0]).
- 12 pairs clear $0.10/day. Earlier programme best was $0.0775/day with capacity
  $75.66 (< $100, unexecutable). **Opening the new venues raised the best net edge ~4.6×
  and, for the first time, the top names clear the $100 size.**

## 3. Live Binance USDⓈ-M funding + spot-perp basis (full 528-perp universe)

`binance_futures_warp.json`.

- Instantaneous funding across 528 TRADING USDT perps: median **1.00 bps/8h**, mean 0.13,
  p10 0.22, p90 1.36; only 7 symbols exceed |30 bps/8h| at this instant.
- Funding history gated for the 59 highest-|income| symbols (≥30 d): **0 reach a
  sign-persistent mean ≥ 30 bps/8h**; the largest persistent mean is 13.3 bps/8h
  (`ONEUSDT`) and it *fails* sign-persistence (52 % vs the 60 % bar).
- **Verdict vs the S3-dump study: UNCHANGED.** `REPORT.md` found no tail with mean
  ≥ 30 bps/8h + streak ≥ 14 d and median funding 0.26 bps/8h. Live data gives median
  1.0 bps/8h and the same null on the persistent tail. Live Binance futures data does
  **not** change the earlier conclusion.

## 4. Spot-perp basis carry (venues with both legs)

`basis_warp.json`, $50/leg, hedged, entry basis = short-perp-bid vs long-spot-ask, cost =
2×(spot taker + perp taker). Best net entry per venue:

| venue | best pair | entry basis | round-trip cost | net |
|---|---|---|---|---|
| binance | BNB | +5.1 bps | 30 bps | **−24.9 bps** |
| bybit | ETH | −4.2 bps | 30 bps | **−34.2 bps** |
| okx | BTC | −2.8 bps | 30 bps | **−32.8 bps** |
| deribit | BTC | −3.7 bps | 20 bps | **−23.7 bps** |

Every venue's cash-and-carry entry is negative after cost — the basis is smaller than the
round-trip fees. Coinbase INTX has no reachable order book, so no INTX basis row.

## Positive after ALL costs

The **only** class with any positive-after-cost result is the **cross-venue perp funding
spread** (both legs perps, delta-neutral, no spot leg). It is small and capacity-limited:

| finding | venue / instrument | size | $/day at $100 | capacity | unverified |
|---|---|---|---|---|---|
| best | `T` perp, long binance / short kraken_futures (or reverse) | $50/leg | **$0.354** (taker) | $1,053 | venue fee tiers unverified (docs only); half-spreads are live snapshots, not time-series; funding is perp-only (no spot leg) |

Nothing else is positive after all costs. Triangular: 0/305. Spot-perp basis: negative on
all 4 venues. Binance single-venue funding: no persistent ≥30 bps/8h tail.

## Bottom line

**Opening these venues does not change the programme's verdict.** It sharpens it:
triangular is still structurally dead (now confirmed on 7 more venues, 0/1,496 samples);
live Binance futures confirms the S3-dump null; spot-perp basis is negative everywhere.
The one improvement is the cross-venue funding spread: best net edge rises from
$0.0775/day to $0.354/day at $100, and the top names now clear the $100 size instead of
being capacity-bound. That is still sub-$0.40/day for a $100 book — real, delta-neutral,
but marginal.

## Evidence

- `probe_self.json`, `probe_binance.json` — reachability, 3 attempts/endpoint
- `triangular_warp.json`, `tri_run.log` — class 1
- `funding_warp.json`, `funding_run.log` — class 2
- `binance_futures_warp.json` — class 3
- `basis_warp.json` — class 4
- `raw/` — every cached HTTP response
- code `scripts/arb_warp_scan.py`; tests `tests/unit/test_arb_warp.py`
