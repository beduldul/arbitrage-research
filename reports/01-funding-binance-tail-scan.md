# Funding carry scan — Binance data dumps — 2026-10-03

- **Data source: Binance official funding dumps** (`data.binance.vision`), Binance spot mirror, CoinGecko perp snapshot. `fapi.binance.com` is geo-blocked (HTTP 000) and was not used.
- Notional per pair: **$100.00**
- Funding window requested: **6 months** (2026-04 .. 2026-09)
- Fee schedule: project base tier 10bps spot taker / 5bps perp taker, both legs round trip
- Slippage: project S9 model, k=0.5, exit×1.5
- Network: 5 concurrent, 6.0/s per host, exponential backoff on 429/418/5xx; 404 = definitive miss, not retried
- Compute: 8 processes; wall-clock 908.59s (enumerate 0.0s, fetch 908.27s, compute 0.19s)

## Endpoint reachability

| endpoint | HTTP | latency | bytes |
|---|---:|---:|---:|
| fapi_fundingRate | 0 | 60.5ms | 0 |
| fapi_premiumIndex | 0 | 1050.9ms | 0 |
| vision_monthly_funding_zip | 200 | 148.9ms | 924 |
| vision_daily_funding_zip | 404 | 135.2ms | 0 |
| vision_s3_funding_index | 200 | 1036.6ms | 94167 |
| data_api_spot_price | 200 | 430.7ms | 45 |
| coingecko_derivatives | 200 | 6196.7ms | 9245267 |

## Universe census (nothing dropped silently)

| stage | pairs |
|---|---:|
| binance_usdt_perps_in_index | 901 |
| months_requested | 6 |
| months | ['2026-04', '2026-05', '2026-06', '2026-07', '2026-08', '2026-09'] |
| with_funding_history | 777 |
| with_spot_volume_ge_1m | 203 |
| with_spot_volume_ge_5m | 87 |
| with_perp_snapshot | 733 |
| ranked (funding parsed) | 777 |

## Funding distribution across all ranked pairs

- mean funding (bps/8h): min -20.70, p25 -0.32, median 0.26, p75 0.58, max 9.59
- pairs with mean funding ≥ 0 bps/8h: 513 / 777
- pairs with mean funding ≥ 5 bps/8h: 3 / 777
- pairs with mean funding ≥ 30 bps/8h: 0 / 777
- pairs with annualised funding ≥ 30%: 54 / 777

## Top 15 by hedged net carry

| pair | mean bps/8h | pos% | flips | streak d | basis bps | hedged cost % (taker/maker) | hedged BE d | $/day | verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| HIPPOUSDT | 9.59 | 98% | 0.05 | 5.8 | n/a | 3.60 / 3.54 | 6 | 0.57542 | candidate |
| DAMUSDT | 4.84 | 98% | 0.02 | 15.5 | n/a | 3.60 / 3.54 | 12 | 0.29045 | candidate |
| BTWUSDT | 3.96 | 99% | 0.01 | 95.5 | n/a | 3.60 / 3.54 | 15 | 0.23740 | candidate |
| 1000000BOBUSDT | 3.25 | 97% | 0.04 | 27.8 | n/a | 3.60 / 3.54 | 18 | 0.19507 | candidate |
| FIOUSDT | 3.06 | 99% | 0.02 | 14.2 | n/a | 1.11 / 1.05 | 6 | 0.18374 | candidate |
| BNCUSDT | 6.18 | 43% | 0.41 | 2.7 | n/a | 3.60 / 3.54 | 19 | 0.18555 | no |
| BROCCOLIF3BUSDT | 3.09 | 92% | 0.11 | 18.5 | n/a | 3.60 / 3.54 | 19 | 0.18528 | candidate |
| LYNUSDT | 2.91 | 99% | 0.03 | 60.8 | n/a | 3.60 / 3.54 | 21 | 0.17488 | candidate |
| MPUSDT | 5.47 | 80% | 0.25 | 1.3 | n/a | 3.60 / 3.54 | 22 | 0.16408 | no |
| BULLAUSDT | 2.68 | 98% | 0.03 | 77.8 | n/a | 3.60 / 3.54 | 22 | 0.16052 | candidate |
| STARUSDT | 2.43 | 96% | 0.05 | 34.0 | n/a | 3.60 / 3.54 | 25 | 0.14580 | candidate |
| ESPORTSUSDT | 2.41 | 90% | 0.06 | 48.5 | n/a | 3.60 / 3.54 | 25 | 0.14489 | candidate |
| SPORTFUNUSDT | 2.32 | 100% | 0.00 | 182.8 | n/a | 3.60 / 3.54 | 26 | 0.13904 | candidate |
| KOMAUSDT | 2.23 | 93% | 0.12 | 24.0 | n/a | 3.60 / 3.54 | 27 | 0.13376 | candidate |
| POWERUSDT | 2.17 | 99% | 0.01 | 146.7 | n/a | 3.60 / 3.54 | 28 | 0.13030 | candidate |

## Liquid intersection — hedgeable AND clears the bar

The highest-funding symbols are largely **unhedgeable**: they have no spot pair to buy against the short perp, so the 'market-neutral' leg cannot be built. Filtering to a spot 24h quote volume of at least $1M / $5M:

| filter | symbols |
|---|---|
| clears taker AND spot vol ≥ $1M | 1: MARSCOINUSDT |
| clears taker AND spot vol ≥ $5M | 1: MARSCOINUSDT |
| verdict candidate AND spot vol ≥ $1M | 6: HFTUSDT, MARSCOINUSDT, AIUSDT, ASTERUSDT, SKYUSDT, ZROUSDT |
| verdict candidate AND spot vol ≥ $5M | 4: MARSCOINUSDT, ASTERUSDT, SKYUSDT, ZROUSDT |

**The $100 practical read:** even the surviving hedgeable names are thin (XMR/XVG/ATA spot ≈ $0.4–0.6M/24h). At $100 split ~$50/leg, round-trip cost is ~1.1% (taker) and net carry ~17–22% annualised — i.e. **~$0.05–0.06/day**, before the basis-drift term this test omits. MARSCOINUSDT pays funding every **4h** (not 8h) and is the one ≥$1M-liquidity name that clears the taker bar.

**New-listing caveat (the dominant trap in this tail):** the top-funding symbols are mostly **recent listings** with only weeks of history (HIPPOUSDT 7 days, DAMUSDT 28 days, MARSCOINUSDT 29 days, FIOUSDT 14 days). New perps routinely open at a large funding premium that decays; a 7-day streak is not persistence. The symbols with a full **182-day** window and a liquid hedge are XMR/XVG/ATA — and their funding is only **~1.7–2.1 bps/8h (~19–23% annualised)**, at the margin of the cost bar, not a high-funding tail.

## Reconciliation with prior work (`~/back/FUNDING_OR_ALT.md`)

- The prior study used **21 symbols × 4 months (2026-05..08)** and concluded the hedged cash-and-carry was **negative in both IS/OOS halves at taker cost**, with the best maker case (N=21 OOS) at **−3.03 bps/trade** and a CI straddling zero. Its structural finding: OOS funding income ≈ **6.5 bps per hold** versus a **20 bps** hedged taker round trip.
- This scan **extends** that to the full index (hundreds of symbols × 6 months) and asks the *tail* question the prior work did not: is there any symbol whose funding is high and persistent enough to flip the sign? The answer is in the decisive-test section above.
- **Cost-model note:** the prior doc charged a *blended* hedged round trip of 20 bps taker / 12 bps maker (perp 2×5 + spot 2×5, plus 5 bps slippage). This scan uses the project's own S9 model, which charges **spot taker 10 bps on both spot legs**: 30 bps taker / 24 bps maker. The project's number is *stricter*, so agreement between the two is agreement under a harder bar — and a symbol failing here would also fail the prior doc's looser 20/12 bps bar.

## Verdict counts

- pairs ranked: **777**
- pairs with ≥30 days of funding history: **723**
- candidate: **26**
- no: **751**
- tail (mean ≥ 30 bps/8h AND streak ≥ 14d): **0**
- clears hedged cost in BOTH IS/OOS halves @ taker: **14**
- clears hedged cost in BOTH IS/OOS halves @ maker: **18**
- ... AND has a spot mirror to hedge with, @ taker: **4**
- ... AND has a spot mirror to hedge with, @ maker: **6**

## Tail — high-funding symbols (mean ≥ 30 bps/8h, streak ≥ 14 days)

No symbol has mean funding ≥ 30 bps/8h sustained ≥ 14 consecutive days.

## The decisive test — does ANY symbol clear the cost floor in BOTH halves?

- @ taker (economics only): HIPPOUSDT, BTWUSDT, 1000000BOBUSDT, BNCUSDT, BROCCOLIF3BUSDT, LYNUSDT, MPUSDT, BULLAUSDT, STARUSDT, KOMAUSDT, MARSCOINUSDT, ATAUSDT, XMRUSDT, XVGUSDT
- @ maker (economics only): HIPPOUSDT, DAMUSDT, BTWUSDT, 1000000BOBUSDT, BNCUSDT, BROCCOLIF3BUSDT, LYNUSDT, MPUSDT, BULLAUSDT, STARUSDT, SPORTFUNUSDT, KOMAUSDT, MARSCOINUSDT, ATAUSDT, XMRUSDT, XVGUSDT, KITEUSDT, ASTERUSDT
- @ taker AND hedgeable (spot mirror exists): MARSCOINUSDT, ATAUSDT, XMRUSDT, XVGUSDT
- @ maker AND hedgeable (spot mirror exists): MARSCOINUSDT, ATAUSDT, XMRUSDT, XVGUSDT, KITEUSDT, ASTERUSDT

**Caveat (in the generous direction):** this test nets *funding income* against the hedged cost only. It does **not** include the spot–perp **basis drift** term, which `FUNDING_OR_ALT.md` measured as material (up to ±9,770 bps aggregate). A symbol that passes this funding-only bar could still fail once basis drift is included, so this bar is a *lower* bound on the true hurdle, not an upper one. The hedged cost here (project model: 2×10 bps spot + 2×5 bps perp = **30 bps** taker; 2×10 + 2×2 = **24 bps** maker) is also *stricter* than the prior doc's 20 bps / 12 bps, because it charges spot taker 10 bps on both spot legs rather than a blended 3–5 bps.

Best candidate (hedgeable, most liquid): **ZROUSDT** — 7.42% net annualised, $0.02210/day ≈ $8.07/yr at $100, hedged break-even 29.4d, longest positive streak 16.2d, spot 24h vol $25,891,571.
Highest net-carry hedgeable candidate: **FIOUSDT** (65.96%/yr, spot vol $186,887, 14d history).
