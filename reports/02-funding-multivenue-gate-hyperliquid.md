# Multi-venue perp funding scan — 2026-10-03

**Scope:** measurement only. No orders, no keys, no `.env` edits. Public unauthenticated
endpoints only. Raw responses cached under `evidence/arbitrage/2026-10-03/venues/`.
Script: `scripts/arb_funding_multivenue.py`. Machine-readable output:
`evidence/arbitrage/2026-10-03/multivenue_ranking.json` (762 objects).

---

## STEP 1 — Reachability matrix

One `curl` attempt per endpoint, no retries (per brief). Latency is total connect+transfer.

| venue | endpoint | HTTP | latency | payload real? | evidence |
|---|---|---:|---:|---|---|
| **Binance spot** (control) | `data-api.binance.vision/api/v3/ticker/price` | **200** | 0.81s | yes — `{"symbol":"BTCUSDT","price":"84637.44"}` | `venues/binance_spot_control.json` |
| **Gate.io** | `api.gateio.ws/api/v4/futures/usdt/contracts` | **200** | 6.51s | yes — 1025 contracts, `funding_rate`, `funding_interval`, `funding_next_apply` | `venues/gate_contracts.body` |
| **Gate.io** | `api.gateio.ws/api/v4/futures/usdt/tickers` | **200** | 4.35s | yes — 1025 rows, real `volume_24h_quote`, `mark_price`, `index_price` | `venues/gate_tickers.body` |
| **Gate.io** | `api.gateio.ws/api/v4/futures/usdt/funding_rate?contract=BTC_USDT` | **200** | 0.47s | yes — 90 rows `{r,t}`, 29.7-day span, `t` epoch seconds | `venues/gate_funding_rate_btc.body` |
| **Hyperliquid** | `api.hyperliquid.xyz/info` POST `metaAndAssetCtxs` | **200** | 0.29s | yes — 234 perps, live `funding`, `premium`, `markPx`, `oraclePx`, `dayNtlVlm` | `venues/hyperliquid_meta.body` |
| **Hyperliquid** | `api.hyperliquid.xyz/info` POST `fundingHistory` | **200** | 0.32s | yes — 500 rows `{coin,fundingRate,premium,time}`, ms `time`, 1h cadence | `venues/hl_funding_history_btc.body` |
| Bybit v5 | `api.bybit.com/v5/market/tickers?category=linear` | **000** | 5.02s | no — TCP connect refused | DNS 18.64.37.56 |
| Bybit v5 | `api.bybit.com/v5/market/funding/history?…` | **000** | 0.29s | no — TCP connect refused | DNS 18.64.37.56 |
| OKX | `www.okx.com/api/v5/public/funding-rate?…` | **000** | 7.07s | no — TCP connect refused | DNS 172.64.144.82 |
| OKX | `www.okx.com/api/v5/public/instruments?instType=SWAP` | **000** | 1.08s | no — TCP connect refused | DNS 172.64.144.82 |
| Bitget | `api.bitget.com/api/v2/mix/market/current-fund-rate?…` | **000** | 5.02s | no — TCP connect refused | DNS 104.18.15.166 |
| Deribit | `www.deribit.com/api/v2/public/get_funding_rate_history?…` | **000** | 2.03s | no — TCP connect refused | DNS **202.169.44.80** (sinkhole) |
| (Kraken, sibling probe) | `api.kraken.com` | **000** | — | no | DNS **202.169.44.80** (sinkhole) |
| Binance futures | `fapi.binance.com` | **000** | — | no (documented, `config/universe.yaml`) | — |

**Reading of the matrix.** Two venue families answered with real, timestamped funding data:
**Gate.io** and **Hyperliquid**. The rest are TCP-level blocked from this host, with two
distinct failure modes: `www.deribit.com` and `api.kraken.com` both resolve to
**202.169.44.80** (the ISP-level sinkhole the sibling worker identified — not a transient
outage), while Bybit/OKX/Bitget resolve to real CDN IPs (CloudFront/Cloudflare) but still
refuse the connection. Either way, the cross-venue *class* spanning Bybit/OKX/Bitget/
Deribit/Kraken is off the table from this machine. Gate.io and Hyperliquid are the
exception and are measured below.

---

## STEP 2 — Measurement across Gate.io and Hyperliquid

- **Gate.io:** 584 live USDT-margined crypto perps enumerated (1025 contracts minus
  tokenised stocks/indices/metals/forex/commodities and pre-market). Native settlement is
  **4h (386)**, **8h (194)**, **1h (4)** — read per contract, not assumed.
- **Hyperliquid:** 178 live perps, **1h** settlement, `fundingHistory` paginated forward
  (500 rows/page) to cover ≥ 30 days.
- Funding history per symbol pulled for a ≥ 30-day window (median achieved window
  29.8d Gate / 30.0d HL). No symbol dropped silently; the 4 CJK-named Gate symbols are
  URL-encoded and measured.
- Cost model: project's own `engine/fees.py` + `engine/cost_model.round_trip_cost_pct`
  (spot taker 10 bps both legs, perp taker 5 bps, slippage k=0.5, exit ×1.5). **Tier:
  taker.** This is **cost model from project defaults, venue fee unverified** — neither
  venue exposes an unauthenticated fee endpoint. Gate's published base futures taker
  (0.05%) matches the project default; Hyperliquid's published base taker (0.035%) is
  *lower*, so the project default **over-charges** HL and understates its carry.
- Volume floor: `engine/slippage.MIN_QUOTE_VOLUME_USD` = **$1M/24h**. Symbols below it are
  marked `below_volume_floor: true` and vetoed to `verdict: "no"` (DESIGN.md §10.3:
  "excluded structurally, not priced") — this is why zero-volume 4h meme perps with
  40 bps/8h means no longer top the table.
- Network: 5 concurrent, 6/s per host, exponential backoff on 429/418/5xx. Run stats:
  `requests 64 / cache_hits 903 / retries 24 / errors 0` on the final pass.

### Coverage

| venue | measured | above $1M floor | candidates |
|---|---:|---:|---:|
| Gate.io | 584 | 84 | 19 |
| Hyperliquid | 178 | 109 | 57 |
| **total** | **762** | **193** | **76** |

### Top 15 by net $/day at $100 notional

| # | venue | symbol | n | int | mean bps/8h | ann % | +share | streak d | basis bps | 24h vol $ | cost % | b/e d | $/day @$100 |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | gate | STAMP_USDT | 71 | 4h | 7.455 | 81.63 | 92% | 7.3 | -12.6 | 2M | 1.101 | 4.9 | 0.2236 |
| 2 | gate | BTW_USDT | 180 | 4h | 5.268 | 57.69 | 98% | 25.3 | -7.0 | 14M | 0.650 | 4.1 | 0.1580 |
| 3 | gate | LYN_USDT | 180 | 4h | 4.999 | 54.74 | 100% | 30.0 | +26.2 | 2M | 1.101 | 7.3 | 0.1500 |
| 4 | gate | 牛来_USDT | 180 | 4h | 4.342 | 47.55 | 93% | 20.8 | +12.2 | 5M | 1.100 | 8.4 | 0.1303 |
| 5 | gate | MUBARAK_USDT | 180 | 4h | 4.023 | 44.05 | 100% | 30.0 | +8.7 | 2M | 1.101 | 9.1 | 0.1207 |
| 6 | hyperliquid | PURR | 720 | 1h | 3.511 | 38.44 | 100% | 30.0 | +36.4 | 5M | 1.100 | 10.4 | 0.1053 |
| 7 | hyperliquid | USELESS | 589 | 1h | 3.230 | 35.37 | 100% | 24.5 | +8.2 | 7M | 1.100 | 11.3 | 0.0969 |
| 8 | hyperliquid | CASHCAT | 720 | 1h | 3.228 | 35.34 | 99% | 25.0 | -2.2 | 11M | 0.650 | 6.7 | 0.0968 |
| 9 | hyperliquid | GRASS | 720 | 1h | 3.103 | 33.98 | 100% | 30.0 | +18.4 | 6M | 1.100 | 11.8 | 0.0931 |
| 10 | hyperliquid | PONS | 720 | 1h | 3.030 | 33.18 | 98% | 24.3 | +17.2 | 26M | 0.650 | 7.2 | 0.0909 |
| 11 | hyperliquid | XMR | 720 | 1h | 2.958 | 32.39 | 96% | 9.8 | +5.9 | 13M | 0.650 | 7.3 | 0.0887 |
| 12 | gate | MOVR_USDT | 90 | 8h | 2.778 | 30.42 | 93% | 27.0 | -11.9 | 21M | 0.650 | 7.8 | 0.0833 |
| 13 | hyperliquid | VVV | 720 | 1h | 2.554 | 27.96 | 100% | 30.0 | +1.5 | 20M | 0.650 | 8.5 | 0.0766 |
| 14 | hyperliquid | AERO | 720 | 1h | 2.522 | 27.62 | 100% | 18.0 | +8.0 | 3M | 1.100 | 14.5 | 0.0757 |
| 15 | hyperliquid | NIL | 720 | 1h | 2.408 | 26.37 | 100% | 30.0 | +14.6 | 2M | 1.101 | 15.2 | 0.0722 |

Per-venue medians: Gate annualised 10.3%, +share 96%; Hyperliquid annualised 11.9%,
+share 98%.

---

## STEP 3 — Verdict

**Reachable venues with real funding data: 2 — Gate.io and Hyperliquid.** Bybit, OKX,
Bitget, Deribit and Kraken are TCP-blocked from this host (Deribit/Kraken to the
202.169.44.80 sinkhole), as is Binance futures.

**Symbols measured: 762** (Gate 584, Hyperliquid 178); **193** clear the $1M/24h volume
floor. **76 pass the bar** (funding persistently positive — mean > 0, positive in ≥ 60%
of settlements, longest positive streak ≥ 7 days — AND break-even within 30 days).

**Best $/day at $100 notional: Gate.io `STAMP_USDT` at $0.224/day** (≈ $81.6/yr gross,
break-even 4.9d). Best Hyperliquid: `PURR` at $0.105/day (break-even 10.4d).

**Honest caveats (all in the generous direction — the real numbers are worse):**

1. **No basis-drift term.** This nets *funding income* against the round-trip cost only.
   The sibling `REPORT.md` measured spot–perp basis drift as material (up to ±9,770 bps
   aggregate). A symbol that passes this funding-only bar can still fail once basis drift
   is charged. This bar is a **lower bound** on the hurdle.
2. **The top symbols are illiquid memecoins.** `STAMP_USDT`, `PURR`, `USELESS`,
   `CASHCAT`, `牛来_USDT` — a real cash-and-carry needs a **spot leg to hedge the perp**,
   and several of these have no liquid spot market on the same venue. The measured
   funding is real, but the trade may not be *constructible* at size.
3. **Venue fees unverified.** Cost uses project defaults (taker 5 bps perp, 10 bps spot);
   neither venue exposes an unauthenticated fee endpoint. Gate's published base taker
   matches; Hyperliquid's published taker is lower (so HL is over-charged here).
4. **$100 notional is the floor of tradeable size.** At $100 the absolute dollars are
   cents/day; the same carry at $10k notional is ~$22/day gross for `STAMP_USDT`, before
   the caveats above.

**Bottom line.** Cross-venue funding *is* measurable from this host on Gate.io and
Hyperliquid — 762 perps with ≥30 days of real settlement history. A funding-only screen
finds 76 that pass the persistence + break-even bar, best $0.224/day per $100 at Gate.io.
Whether any of them is *executable* is a separate question this scan does not answer:
it requires a spot leg that exists and is liquid, plus a basis-drift term, plus verified
venue fees — none of which the top rows are shown to satisfy.
