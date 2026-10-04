# Maker-side spread capture — is market making viable at $100? (2026-10-03)

**Measurement only.** Public depth + public trade tape; no orders, no keys, no account state. Script `scripts/arb_maker_scan.py`; raw samples under `evidence/arbitrage/2026-10-03/maker/raw/`.

- Window: **25.0 min**, 747 ticks @ 2.0s, 8 pairs × 3 venues, 47375 tape rows.
- Rate: 429s=0, 5xx=0, backoff wait=0.0s; REST latency mean=152.2ms p95=328.9ms.
- **Queue assumption:** we count a fill only when the tape trades *strictly through* our resting level; a trade *at* our level is a touch, not a fill (queue position unknown). Fills/hour is reported both ways; the headline is through-only.
- **Inventory:** no hedging is modelled; we accumulate the losing side. The adverse term below is the proxy for that cost.
- **Fee basis for the VERDICT: VIP0, maker == taker == 10 bps/leg.** A $100 account is VIP0 with no rebate; this is exactly why DESIGN.md §10.2 sets `spot_maker_bps == spot_taker_bps`. The ~2 bps 'maker' figure is a high-VIP/rebate rate **this account cannot reach** — reported only as a labelled what-if column.

> **Adverse selection below is a COARSE PROXY, not a precise cost — and it systematically UNDERSTATES the true adverse term.** Our measured REST round-trip latency is ~100–465 ms, while adverse selection acts at sub-millisecond-to-second scale. A 1/5/30/60 s mid-move can only see slow drift, so the real toxic cost of a fill is *larger* than anything tabulated here. Treat every adverse number as a lower bound.

## 1. Quoted spread distribution (bps of mid)

| venue:pair | mean | median | p10 | p90 | % time spread > 2×VIP0 maker fee (20 bps) |
|---|---:|---:|---:|---:|---:|
| binance:ADAUSDT | 4.086 | 4.086 | 4.079 | 4.092 | 0.0% |
| binance:AVAXUSDT | 0.919 | 0.919 | 0.918 | 0.921 | 0.0% |
| binance:BTCUSDT | 0.001 | 0.001 | 0.001 | 0.001 | 0.0% |
| binance:DOGEUSDT | 1.077 | 1.076 | 1.076 | 1.078 | 0.0% |
| binance:ETHUSDT | 0.037 | 0.037 | 0.037 | 0.037 | 0.0% |
| binance:LINKUSDT | 0.713 | 0.713 | 0.713 | 0.714 | 0.0% |
| binance:SOLUSDT | 0.838 | 0.838 | 0.837 | 0.838 | 0.0% |
| binance:XRPUSDT | 0.674 | 0.674 | 0.674 | 0.674 | 0.0% |
| gate:ADAUSDT | 1.100 | 1.224 | 0.408 | 2.044 | 0.0% |
| gate:AVAXUSDT | 3.407 | 3.670 | 2.754 | 3.684 | 0.0% |
| gate:BTCUSDT | 0.012 | 0.012 | 0.012 | 0.012 | 0.0% |
| gate:DOGEUSDT | 1.081 | 1.077 | 1.076 | 1.078 | 0.0% |
| gate:ETHUSDT | 0.037 | 0.037 | 0.037 | 0.037 | 0.0% |
| gate:LINKUSDT | 1.444 | 1.427 | 0.713 | 2.141 | 0.0% |
| gate:SOLUSDT | 0.838 | 0.838 | 0.837 | 0.838 | 0.0% |
| gate:XRPUSDT | 0.680 | 0.674 | 0.674 | 0.674 | 0.0% |
| htx:ADAUSDT | 3.979 | 2.449 | 1.103 | 8.073 | 0.1% |
| htx:AVAXUSDT | 13.257 | 13.441 | 9.839 | 17.933 | 2.1% |
| htx:BTCUSDT | 0.001 | 0.001 | 0.001 | 0.001 | 0.0% |
| htx:DOGEUSDT | 6.236 | 7.863 | 0.753 | 11.742 | 0.3% |
| htx:ETHUSDT | 0.087 | 0.037 | 0.037 | 0.037 | 0.0% |
| htx:LINKUSDT | 11.263 | 11.559 | 5.994 | 15.260 | 0.7% |
| htx:SOLUSDT | 1.803 | 1.777 | 0.008 | 3.655 | 0.0% |
| htx:XRPUSDT | 4.772 | 5.396 | 0.472 | 9.304 | 0.0% |

## 2. Fill rate (through-only, conservative)

| venue:pair | tape rows | through fills | fills/hour | touch fills/hour (upper bd) |
|---|---:|---:|---:|---:|
| binance:ADAUSDT | 1576 | 156 | 374.7 | 1354.6 |
| binance:AVAXUSDT | 1965 | 523 | 1256.1 | 2046.3 |
| binance:BTCUSDT | 6225 | 2086 | 5010.1 | 12160.2 |
| binance:DOGEUSDT | 1627 | 64 | 153.7 | 1407.4 |
| binance:ETHUSDT | 3588 | 1277 | 3067.1 | 5682.6 |
| binance:LINKUSDT | 2272 | 497 | 1193.7 | 2752.4 |
| binance:SOLUSDT | 2100 | 155 | 372.3 | 2435.4 |
| binance:XRPUSDT | 1632 | 73 | 175.3 | 1450.7 |
| gate:ADAUSDT | 1497 | 141 | 338.7 | 778.2 |
| gate:AVAXUSDT | 1368 | 59 | 141.7 | 439.5 |
| gate:BTCUSDT | 1843 | 169 | 405.9 | 1760.5 |
| gate:DOGEUSDT | 1417 | 63 | 151.3 | 850.2 |
| gate:ETHUSDT | 2032 | 367 | 881.5 | 2043.9 |
| gate:LINKUSDT | 1380 | 55 | 132.1 | 672.5 |
| gate:SOLUSDT | 1431 | 77 | 184.9 | 879.0 |
| gate:XRPUSDT | 1478 | 122 | 293.0 | 1013.5 |
| htx:ADAUSDT | 266 | 9 | 21.6 | 43.2 |
| htx:AVAXUSDT | 439 | 2 | 4.8 | 16.8 |
| htx:BTCUSDT | 12016 | 6160 | 14794.9 | 15580.3 |
| htx:DOGEUSDT | 117 | 0 | 0.0 | 4.8 |
| htx:ETHUSDT | 308 | 79 | 189.7 | 341.1 |
| htx:LINKUSDT | 491 | 5 | 12.0 | 16.8 |
| htx:SOLUSDT | 176 | 8 | 19.2 | 84.1 |
| htx:XRPUSDT | 131 | 0 | 0.0 | 7.2 |

## 3. Adverse selection — the decisive term (COARSE PROXY, lower bound)

Signed mid drift after a fill, in the direction that hurts us (positive = adverse). Horizon is the *effective* elapsed time to the snapshot used. **This understates the true cost** — see the box above.

| venue:pair | horizon | eff. Δs | n | adverse mean | median | p90 | std |
|---|---|---:|---:|---:|---:|---:|---:|
| binance:ADAUSDT | 1s | 2.0 | 156 | 4.842 | 8.165 | 8.165 | 3.862 |
| binance:ADAUSDT | 5s | 5.9 | 156 | 7.746 | 8.165 | 8.165 | 1.475 |
| binance:ADAUSDT | 30s | 30.5 | 156 | 7.851 | 8.165 | 8.165 | 1.351 |
| binance:ADAUSDT | 60s | 60.2 | 156 | 7.694 | 8.165 | 8.165 | 1.959 |
| binance:AVAXUSDT | 1s | 2.1 | 523 | 4.004 | 3.672 | 9.187 | 3.091 |
| binance:AVAXUSDT | 5s | 6.1 | 523 | 4.769 | 3.680 | 10.105 | 4.213 |
| binance:AVAXUSDT | 30s | 30.9 | 508 | 5.211 | 5.507 | 9.185 | 6.288 |
| binance:AVAXUSDT | 60s | 60.8 | 508 | 3.888 | 2.756 | 12.888 | 6.728 |
| binance:BTCUSDT | 1s | 2.0 | 2086 | 0.419 | 0.110 | 0.779 | 0.684 |
| binance:BTCUSDT | 5s | 6.0 | 2086 | 1.071 | 0.946 | 1.894 | 0.639 |
| binance:BTCUSDT | 30s | 31.0 | 2084 | 1.037 | 1.116 | 2.601 | 1.260 |
| binance:BTCUSDT | 60s | 60.7 | 2026 | 0.941 | 1.020 | 3.007 | 1.256 |
| binance:DOGEUSDT | 1s | 2.1 | 64 | 1.918 | 1.078 | 5.380 | 1.641 |
| binance:DOGEUSDT | 5s | 6.0 | 64 | 2.726 | 2.156 | 6.455 | 1.804 |
| binance:DOGEUSDT | 30s | 31.2 | 64 | 2.844 | 3.229 | 5.380 | 2.114 |
| binance:DOGEUSDT | 60s | 60.8 | 64 | 2.289 | 2.156 | 5.388 | 2.689 |
| binance:ETHUSDT | 1s | 2.0 | 1277 | 0.434 | 0.000 | 1.193 | 0.610 |
| binance:ETHUSDT | 5s | 6.0 | 1266 | 1.268 | 1.007 | 2.499 | 0.815 |
| binance:ETHUSDT | 30s | 31.1 | 1250 | 1.475 | 1.419 | 3.321 | 1.664 |
| binance:ETHUSDT | 60s | 60.9 | 1250 | 1.403 | 1.493 | 3.953 | 1.880 |
| binance:LINKUSDT | 1s | 2.0 | 497 | 2.504 | 2.138 | 6.422 | 2.335 |
| binance:LINKUSDT | 5s | 6.0 | 497 | 3.446 | 2.850 | 7.140 | 2.638 |
| binance:LINKUSDT | 30s | 31.0 | 482 | 3.309 | 3.569 | 8.563 | 3.852 |
| binance:LINKUSDT | 60s | 60.7 | 480 | 3.184 | 2.852 | 11.406 | 5.226 |
| binance:SOLUSDT | 1s | 2.1 | 155 | 1.361 | 0.837 | 2.512 | 1.689 |
| binance:SOLUSDT | 5s | 6.0 | 155 | 2.635 | 1.676 | 4.187 | 2.153 |
| binance:SOLUSDT | 30s | 30.9 | 155 | 2.770 | 3.347 | 5.861 | 2.722 |
| binance:SOLUSDT | 60s | 60.7 | 155 | 3.489 | 3.352 | 6.695 | 3.033 |
| binance:XRPUSDT | 1s | 2.0 | 73 | 1.108 | 0.674 | 3.099 | 1.083 |
| binance:XRPUSDT | 5s | 6.0 | 73 | 1.994 | 2.021 | 3.371 | 0.930 |
| binance:XRPUSDT | 30s | 31.4 | 73 | 1.689 | 2.022 | 3.371 | 1.877 |
| binance:XRPUSDT | 60s | 60.8 | 73 | 0.720 | 0.674 | 2.695 | 1.685 |
| gate:ADAUSDT | 1s | 2.1 | 141 | 0.519 | 0.000 | 1.635 | 1.057 |
| gate:ADAUSDT | 5s | 6.0 | 141 | 2.722 | 2.042 | 6.737 | 2.591 |
| gate:ADAUSDT | 30s | 30.9 | 140 | 2.071 | 1.637 | 8.778 | 4.781 |
| gate:ADAUSDT | 60s | 60.7 | 140 | 1.687 | 1.842 | 7.372 | 5.221 |
| gate:AVAXUSDT | 1s | 2.1 | 59 | 1.324 | 0.000 | 4.129 | 2.228 |
| gate:AVAXUSDT | 5s | 6.0 | 59 | 5.786 | 4.142 | 11.027 | 4.235 |
| gate:AVAXUSDT | 30s | 30.8 | 57 | 6.495 | 5.522 | 11.025 | 7.126 |
| gate:AVAXUSDT | 60s | 60.8 | 57 | 5.004 | 5.511 | 11.042 | 6.252 |
| gate:BTCUSDT | 1s | 2.1 | 169 | 0.236 | 0.000 | 0.804 | 0.349 |
| gate:BTCUSDT | 5s | 6.1 | 169 | 0.948 | 0.899 | 1.548 | 0.800 |
| gate:BTCUSDT | 30s | 31.2 | 168 | 0.797 | 0.827 | 3.027 | 1.245 |
| gate:BTCUSDT | 60s | 60.8 | 167 | 0.584 | 0.319 | 1.513 | 0.865 |
| gate:DOGEUSDT | 1s | 2.0 | 63 | 0.881 | 1.076 | 2.156 | 0.855 |
| gate:DOGEUSDT | 5s | 6.0 | 63 | 2.112 | 1.616 | 3.233 | 1.337 |
| gate:DOGEUSDT | 30s | 31.0 | 62 | 2.311 | 2.960 | 4.311 | 2.142 |
| gate:DOGEUSDT | 60s | 60.8 | 62 | 2.433 | 2.155 | 5.389 | 2.768 |
| gate:ETHUSDT | 1s | 2.0 | 367 | 0.149 | 0.000 | 0.560 | 0.314 |
| gate:ETHUSDT | 5s | 6.1 | 362 | 1.223 | 1.194 | 2.537 | 0.742 |
| gate:ETHUSDT | 30s | 31.1 | 342 | 1.617 | 1.586 | 3.509 | 1.476 |
| gate:ETHUSDT | 60s | 60.8 | 342 | 1.773 | 1.492 | 3.729 | 1.708 |
| gate:LINKUSDT | 1s | 2.0 | 55 | 1.174 | 0.714 | 2.856 | 1.377 |
| gate:LINKUSDT | 5s | 6.0 | 55 | 1.777 | 1.785 | 3.779 | 1.716 |
| gate:LINKUSDT | 30s | 31.0 | 52 | 1.852 | 1.784 | 6.604 | 3.349 |
| gate:LINKUSDT | 60s | 60.7 | 50 | 0.984 | 0.714 | 8.544 | 4.916 |
| gate:SOLUSDT | 1s | 2.0 | 77 | 0.707 | 0.000 | 1.676 | 0.992 |
| gate:SOLUSDT | 5s | 6.1 | 77 | 2.022 | 1.675 | 3.351 | 1.918 |
| gate:SOLUSDT | 30s | 30.8 | 77 | 2.391 | 2.512 | 5.855 | 2.419 |
| gate:SOLUSDT | 60s | 60.6 | 77 | 2.859 | 2.514 | 7.031 | 3.472 |
| gate:XRPUSDT | 1s | 2.0 | 122 | 0.569 | 0.674 | 1.348 | 0.710 |
| gate:XRPUSDT | 5s | 6.0 | 122 | 1.514 | 1.348 | 2.023 | 0.835 |
| gate:XRPUSDT | 30s | 31.1 | 121 | 1.172 | 1.347 | 3.370 | 1.754 |
| gate:XRPUSDT | 60s | 60.7 | 121 | 1.094 | 1.347 | 3.370 | 1.809 |
| htx:ADAUSDT | 1s | 2.2 | 9 | 2.080 | 2.592 | 3.226 | 1.759 |
| htx:ADAUSDT | 5s | 6.0 | 9 | 5.103 | 6.775 | 6.775 | 2.166 |
| htx:ADAUSDT | 30s | 30.7 | 9 | 5.484 | 6.775 | 6.903 | 2.008 |
| htx:ADAUSDT | 60s | 60.6 | 9 | 5.527 | 6.775 | 7.238 | 2.583 |
| htx:AVAXUSDT | 1s | 2.3 | 2 | 6.577 | 6.577 | 6.577 | 0.000 |
| htx:AVAXUSDT | 5s | 5.8 | 2 | 6.577 | 6.577 | 6.577 | 0.000 |
| htx:AVAXUSDT | 30s | 31.0 | 2 | 9.060 | 9.060 | 9.060 | 0.000 |
| htx:AVAXUSDT | 60s | 60.9 | 2 | 9.336 | 9.336 | 9.336 | 0.000 |
| htx:BTCUSDT | 1s | 2.0 | 6160 | 0.016 | 0.000 | 0.000 | 0.123 |
| htx:BTCUSDT | 5s | 6.0 | 6160 | 1.139 | 1.205 | 1.337 | 0.271 |
| htx:BTCUSDT | 30s | 31.1 | 6140 | 1.151 | 1.310 | 1.337 | 0.304 |
| htx:BTCUSDT | 60s | 60.4 | 6088 | 1.396 | 1.327 | 2.368 | 0.775 |
| htx:ETHUSDT | 1s | 2.1 | 79 | 0.119 | 0.000 | 0.656 | 0.383 |
| htx:ETHUSDT | 5s | 6.1 | 78 | 1.036 | 1.232 | 2.070 | 0.983 |
| htx:ETHUSDT | 30s | 31.1 | 76 | 0.743 | 0.989 | 2.219 | 1.365 |
| htx:ETHUSDT | 60s | 61.0 | 76 | 0.742 | 0.858 | 2.724 | 1.662 |
| htx:LINKUSDT | 1s | 2.0 | 5 | 1.284 | 0.856 | 2.389 | 0.919 |
| htx:LINKUSDT | 5s | 6.1 | 5 | 3.345 | 2.425 | 5.997 | 2.537 |
| htx:LINKUSDT | 30s | 30.6 | 5 | 2.289 | 2.425 | 4.585 | 2.103 |
| htx:LINKUSDT | 60s | 61.0 | 5 | 1.490 | 2.032 | 5.070 | 3.557 |
| htx:SOLUSDT | 1s | 2.1 | 8 | 0.663 | 0.000 | 1.723 | 1.633 |
| htx:SOLUSDT | 5s | 6.3 | 8 | 1.159 | 0.142 | 3.238 | 1.947 |
| htx:SOLUSDT | 30s | 30.8 | 8 | 0.591 | 0.019 | 2.527 | 1.309 |
| htx:SOLUSDT | 60s | 60.6 | 8 | 0.951 | 1.024 | 2.610 | 1.503 |

## 4. Net edge at VIP0 (10 bps/leg) — THE VERDICT TABLE

Per-fill = half spread − 1 VIP0 fee − adverse proxy. Round trip = full spread − 2 VIP0 fees − 2×adverse proxy. $/day and $/min assume the measured through-fill frequency and $100 notional; they are an **upper bound** because the adverse proxy is a lower bound.

| venue:pair | horizon | half spread | adverse proxy | **net/fill bps** | 95% CI | net/round trip | $/day @ $100 | $/min @ $100 |
|---|---|---:|---:|---:|---|---:|---:|---:|
| binance:ADAUSDT | 1s | 2.041 | 4.842 | **-12.800** | [-13.40, -12.20] | -25.601 | -1151.035 | -0.79933 |
| binance:ADAUSDT | 5s | 2.041 | 7.746 | **-15.705** | [-15.91, -15.47] | -31.410 | -1412.233 | -0.98072 |
| binance:ADAUSDT | 30s | 2.041 | 7.851 | **-15.810** | [-15.99, -15.60] | -31.620 | -1421.654 | -0.98726 |
| binance:ADAUSDT | 60s | 2.041 | 7.694 | **-15.652** | [-15.94, -15.34] | -31.305 | -1407.511 | -0.97744 |
| binance:AVAXUSDT | 1s | 0.460 | 4.004 | **-13.544** | [-13.82, -13.28] | -27.088 | -4083.123 | -2.83550 |
| binance:AVAXUSDT | 5s | 0.460 | 4.769 | **-14.309** | [-14.69, -13.96] | -28.618 | -4313.740 | -2.99565 |
| binance:AVAXUSDT | 30s | 0.460 | 5.211 | **-14.752** | [-15.33, -14.22] | -29.504 | -4447.254 | -3.08837 |
| binance:AVAXUSDT | 60s | 0.460 | 3.888 | **-13.429** | [-14.05, -12.87] | -26.858 | -4048.408 | -2.81140 |
| binance:BTCUSDT | 1s | 0.001 | 0.419 | **-10.418** | [-10.45, -10.39] | -20.836 | -12527.076 | -8.69936 |
| binance:BTCUSDT | 5s | 0.001 | 1.071 | **-11.070** | [-11.10, -11.04] | -22.141 | -13311.386 | -9.24402 |
| binance:BTCUSDT | 30s | 0.001 | 1.037 | **-11.037** | [-11.09, -10.98] | -22.074 | -13271.039 | -9.21600 |
| binance:BTCUSDT | 60s | 0.001 | 0.941 | **-10.940** | [-10.99, -10.88] | -21.880 | -13154.466 | -9.13505 |
| binance:DOGEUSDT | 1s | 0.539 | 1.918 | **-11.380** | [-11.80, -10.99] | -22.759 | -419.808 | -0.29153 |
| binance:DOGEUSDT | 5s | 0.539 | 2.726 | **-12.187** | [-12.62, -11.77] | -24.374 | -449.602 | -0.31222 |
| binance:DOGEUSDT | 30s | 0.539 | 2.844 | **-12.305** | [-12.79, -11.80] | -24.611 | -453.961 | -0.31525 |
| binance:DOGEUSDT | 60s | 0.539 | 2.289 | **-11.750** | [-12.39, -11.09] | -23.500 | -433.480 | -0.30103 |
| binance:ETHUSDT | 1s | 0.019 | 0.434 | **-10.415** | [-10.45, -10.38] | -20.831 | -7666.748 | -5.32413 |
| binance:ETHUSDT | 5s | 0.019 | 1.268 | **-11.249** | [-11.29, -11.20] | -22.498 | -8280.513 | -5.75036 |
| binance:ETHUSDT | 30s | 0.019 | 1.475 | **-11.456** | [-11.55, -11.36] | -22.912 | -8432.769 | -5.85609 |
| binance:ETHUSDT | 60s | 0.019 | 1.403 | **-11.384** | [-11.49, -11.28] | -22.769 | -8379.958 | -5.81942 |
| binance:LINKUSDT | 1s | 0.357 | 2.504 | **-12.147** | [-12.36, -11.95] | -24.294 | -3479.956 | -2.41664 |
| binance:LINKUSDT | 5s | 0.357 | 3.446 | **-13.089** | [-13.32, -12.86] | -26.178 | -3749.755 | -2.60400 |
| binance:LINKUSDT | 30s | 0.357 | 3.309 | **-12.952** | [-13.30, -12.61] | -25.904 | -3710.506 | -2.57674 |
| binance:LINKUSDT | 60s | 0.357 | 3.184 | **-12.827** | [-13.30, -12.36] | -25.655 | -3674.854 | -2.55198 |
| binance:SOLUSDT | 1s | 0.419 | 1.361 | **-10.942** | [-11.21, -10.68] | -21.884 | -977.632 | -0.67891 |
| binance:SOLUSDT | 5s | 0.419 | 2.635 | **-12.217** | [-12.55, -11.89] | -24.433 | -1091.497 | -0.75798 |
| binance:SOLUSDT | 30s | 0.419 | 2.770 | **-12.352** | [-12.75, -11.92] | -24.704 | -1103.582 | -0.76638 |
| binance:SOLUSDT | 60s | 0.419 | 3.489 | **-13.070** | [-13.52, -12.59] | -26.140 | -1167.759 | -0.81094 |
| binance:XRPUSDT | 1s | 0.337 | 1.108 | **-10.771** | [-11.05, -10.53] | -21.541 | -453.223 | -0.31474 |
| binance:XRPUSDT | 5s | 0.337 | 1.994 | **-11.657** | [-11.89, -11.45] | -23.314 | -490.512 | -0.34063 |
| binance:XRPUSDT | 30s | 0.337 | 1.689 | **-11.352** | [-11.80, -10.90] | -22.705 | -477.697 | -0.33173 |
| binance:XRPUSDT | 60s | 0.337 | 0.720 | **-10.383** | [-10.80, -10.01] | -20.766 | -436.906 | -0.30341 |
| gate:ADAUSDT | 1s | 0.471 | 0.519 | **-10.048** | [-10.24, -9.87] | -20.096 | -816.649 | -0.56712 |
| gate:ADAUSDT | 5s | 0.471 | 2.722 | **-12.251** | [-12.72, -11.85] | -24.501 | -995.686 | -0.69145 |
| gate:ADAUSDT | 30s | 0.471 | 2.071 | **-11.600** | [-12.41, -10.82] | -23.201 | -942.839 | -0.65475 |
| gate:ADAUSDT | 60s | 0.471 | 1.687 | **-11.217** | [-12.05, -10.37] | -22.433 | -911.637 | -0.63308 |
| gate:AVAXUSDT | 1s | 1.682 | 1.324 | **-9.642** | [-10.22, -9.10] | -19.283 | -327.900 | -0.22771 |
| gate:AVAXUSDT | 5s | 1.682 | 5.786 | **-14.103** | [-15.17, -13.08] | -28.207 | -479.646 | -0.33309 |
| gate:AVAXUSDT | 30s | 1.682 | 6.495 | **-14.813** | [-16.74, -12.97] | -29.625 | -503.763 | -0.34984 |
| gate:AVAXUSDT | 60s | 1.682 | 5.004 | **-13.322** | [-15.03, -11.72] | -26.644 | -453.065 | -0.31463 |
| gate:BTCUSDT | 1s | 0.006 | 0.236 | **-10.230** | [-10.29, -10.18] | -20.460 | -996.574 | -0.69207 |
| gate:BTCUSDT | 5s | 0.006 | 0.948 | **-10.942** | [-11.06, -10.82] | -21.883 | -1065.887 | -0.74020 |
| gate:BTCUSDT | 30s | 0.006 | 0.797 | **-10.791** | [-10.98, -10.60] | -21.582 | -1051.229 | -0.73002 |
| gate:BTCUSDT | 60s | 0.006 | 0.584 | **-10.578** | [-10.70, -10.44] | -21.157 | -1030.492 | -0.71562 |
| gate:DOGEUSDT | 1s | 0.556 | 0.881 | **-10.325** | [-10.56, -10.12] | -20.650 | -374.948 | -0.26038 |
| gate:DOGEUSDT | 5s | 0.556 | 2.112 | **-11.556** | [-11.92, -11.25] | -23.112 | -419.652 | -0.29142 |
| gate:DOGEUSDT | 30s | 0.556 | 2.311 | **-11.755** | [-12.28, -11.22] | -23.510 | -426.882 | -0.29645 |
| gate:DOGEUSDT | 60s | 0.556 | 2.433 | **-11.877** | [-12.57, -11.16] | -23.754 | -431.310 | -0.29952 |
| gate:ETHUSDT | 1s | 0.019 | 0.149 | **-10.130** | [-10.16, -10.10] | -20.260 | -2143.019 | -1.48821 |
| gate:ETHUSDT | 5s | 0.019 | 1.223 | **-11.204** | [-11.28, -11.13] | -22.408 | -2370.240 | -1.64600 |
| gate:ETHUSDT | 30s | 0.019 | 1.617 | **-11.599** | [-11.75, -11.45] | -23.197 | -2453.660 | -1.70393 |
| gate:ETHUSDT | 60s | 0.019 | 1.773 | **-11.754** | [-11.94, -11.58] | -23.509 | -2486.595 | -1.72680 |
| gate:LINKUSDT | 1s | 0.597 | 1.174 | **-10.577** | [-10.97, -10.21] | -21.154 | -335.324 | -0.23286 |
| gate:LINKUSDT | 5s | 0.597 | 1.777 | **-11.180** | [-11.65, -10.73] | -22.360 | -354.447 | -0.24614 |
| gate:LINKUSDT | 30s | 0.597 | 1.852 | **-11.255** | [-12.10, -10.35] | -22.510 | -356.819 | -0.24779 |
| gate:LINKUSDT | 60s | 0.597 | 0.984 | **-10.387** | [-11.66, -8.93] | -20.774 | -329.305 | -0.22868 |
| gate:SOLUSDT | 1s | 0.419 | 0.707 | **-10.288** | [-10.53, -10.08] | -20.576 | -456.632 | -0.31711 |
| gate:SOLUSDT | 5s | 0.419 | 2.022 | **-11.603** | [-12.07, -11.21] | -23.207 | -515.009 | -0.35764 |
| gate:SOLUSDT | 30s | 0.419 | 2.391 | **-11.973** | [-12.53, -11.43] | -23.946 | -531.410 | -0.36904 |
| gate:SOLUSDT | 60s | 0.419 | 2.859 | **-12.440** | [-13.19, -11.67] | -24.881 | -552.161 | -0.38344 |
| gate:XRPUSDT | 1s | 0.337 | 0.569 | **-10.232** | [-10.37, -10.11] | -20.464 | -719.554 | -0.49969 |
| gate:XRPUSDT | 5s | 0.337 | 1.514 | **-11.177** | [-11.33, -11.04] | -22.353 | -785.980 | -0.54582 |
| gate:XRPUSDT | 30s | 0.337 | 1.172 | **-10.835** | [-11.13, -10.52] | -21.671 | -761.991 | -0.52916 |
| gate:XRPUSDT | 60s | 0.337 | 1.094 | **-10.757** | [-11.09, -10.43] | -21.515 | -756.508 | -0.52535 |
| htx:ADAUSDT | 1s | 0.908 | 2.080 | **-11.172** | [-12.64, -9.72] | -22.344 | -57.960 | -0.04025 |
| htx:ADAUSDT | 5s | 0.908 | 5.103 | **-14.195** | [-15.79, -12.30] | -28.390 | -73.642 | -0.05114 |
| htx:ADAUSDT | 30s | 0.908 | 5.484 | **-14.577** | [-16.27, -12.84] | -29.153 | -75.621 | -0.05251 |
| htx:ADAUSDT | 60s | 0.908 | 5.527 | **-14.620** | [-16.48, -12.57] | -29.239 | -75.845 | -0.05267 |
| htx:AVAXUSDT | 1s | 5.565 | 6.577 | **-11.012** | [-11.01, -11.01] | -22.024 | -12.695 | -0.00882 |
| htx:AVAXUSDT | 5s | 5.565 | 6.577 | **-11.012** | [-11.01, -11.01] | -22.024 | -12.695 | -0.00882 |
| htx:AVAXUSDT | 30s | 5.565 | 9.060 | **-13.495** | [-13.49, -13.49] | -26.991 | -15.558 | -0.01080 |
| htx:AVAXUSDT | 60s | 5.565 | 9.336 | **-13.771** | [-13.77, -13.77] | -27.543 | -15.876 | -0.01103 |
| htx:BTCUSDT | 1s | 0.001 | 0.016 | **-10.015** | [-10.02, -10.01] | -20.030 | -35561.171 | -24.69526 |
| htx:BTCUSDT | 5s | 0.001 | 1.139 | **-11.139** | [-11.14, -11.13] | -22.277 | -39550.439 | -27.46558 |
| htx:BTCUSDT | 30s | 0.001 | 1.151 | **-11.150** | [-11.16, -11.14] | -22.301 | -39592.764 | -27.49498 |
| htx:BTCUSDT | 60s | 0.001 | 1.396 | **-11.395** | [-11.41, -11.38] | -22.790 | -40460.912 | -28.09785 |
| htx:ETHUSDT | 1s | 0.058 | 0.119 | **-10.061** | [-10.16, -9.98] | -20.123 | -458.171 | -0.31817 |
| htx:ETHUSDT | 5s | 0.058 | 1.036 | **-10.978** | [-11.19, -10.78] | -21.955 | -499.892 | -0.34715 |
| htx:ETHUSDT | 30s | 0.058 | 0.743 | **-10.685** | [-11.00, -10.36] | -21.370 | -486.566 | -0.33789 |
| htx:ETHUSDT | 60s | 0.058 | 0.742 | **-10.684** | [-11.07, -10.31] | -21.369 | -486.543 | -0.33788 |
| htx:LINKUSDT | 1s | 6.127 | 1.284 | **-5.157** | [-6.21, -3.96] | -10.314 | -14.863 | -0.01032 |
| htx:LINKUSDT | 5s | 6.127 | 3.345 | **-7.218** | [-9.02, -6.17] | -14.436 | -20.803 | -0.01445 |
| htx:LINKUSDT | 30s | 6.127 | 2.289 | **-6.162** | [-7.38, -4.86] | -12.325 | -17.761 | -0.01233 |
| htx:LINKUSDT | 60s | 6.127 | 1.490 | **-5.363** | [-7.90, -2.31] | -10.726 | -15.458 | -0.01073 |
| htx:SOLUSDT | 1s | 0.343 | 0.663 | **-10.320** | [-10.89, -10.00] | -20.640 | -47.591 | -0.03305 |
| htx:SOLUSDT | 5s | 0.343 | 1.159 | **-10.817** | [-11.70, -10.14] | -21.633 | -49.879 | -0.03464 |
| htx:SOLUSDT | 30s | 0.343 | 0.591 | **-10.248** | [-10.89, -9.78] | -20.496 | -47.258 | -0.03282 |
| htx:SOLUSDT | 60s | 0.343 | 0.951 | **-10.608** | [-11.75, -9.59] | -21.216 | -48.917 | -0.03397 |

## 5. What-if at a 2 bps maker tier — NOT ACHIEVABLE at $100

Same measurement at a high-VIP/rebate maker rate this account cannot access. Shown for reference only; it is **not** the verdict.

| venue:pair | horizon | net/fill bps | net/round trip bps |
|---|---|---:|---:|
| binance:ADAUSDT | 1s | -4.800 | -9.601 |
| binance:ADAUSDT | 5s | -7.705 | -15.410 |
| binance:ADAUSDT | 30s | -7.810 | -15.620 |
| binance:ADAUSDT | 60s | -7.652 | -15.305 |
| binance:AVAXUSDT | 1s | -5.544 | -11.088 |
| binance:AVAXUSDT | 5s | -6.309 | -12.618 |
| binance:AVAXUSDT | 30s | -6.752 | -13.504 |
| binance:AVAXUSDT | 60s | -5.429 | -10.858 |
| binance:BTCUSDT | 1s | -2.418 | -4.836 |
| binance:BTCUSDT | 5s | -3.070 | -6.141 |
| binance:BTCUSDT | 30s | -3.037 | -6.074 |
| binance:BTCUSDT | 60s | -2.940 | -5.880 |
| binance:DOGEUSDT | 1s | -3.380 | -6.759 |
| binance:DOGEUSDT | 5s | -4.187 | -8.374 |
| binance:DOGEUSDT | 30s | -4.305 | -8.611 |
| binance:DOGEUSDT | 60s | -3.750 | -7.500 |
| binance:ETHUSDT | 1s | -2.415 | -4.831 |
| binance:ETHUSDT | 5s | -3.249 | -6.498 |
| binance:ETHUSDT | 30s | -3.456 | -6.912 |
| binance:ETHUSDT | 60s | -3.384 | -6.769 |
| binance:LINKUSDT | 1s | -4.147 | -8.294 |
| binance:LINKUSDT | 5s | -5.089 | -10.178 |
| binance:LINKUSDT | 30s | -4.952 | -9.904 |
| binance:LINKUSDT | 60s | -4.827 | -9.655 |
| binance:SOLUSDT | 1s | -2.942 | -5.884 |
| binance:SOLUSDT | 5s | -4.217 | -8.433 |
| binance:SOLUSDT | 30s | -4.352 | -8.704 |
| binance:SOLUSDT | 60s | -5.070 | -10.140 |
| binance:XRPUSDT | 1s | -2.771 | -5.541 |
| binance:XRPUSDT | 5s | -3.657 | -7.314 |
| binance:XRPUSDT | 30s | -3.352 | -6.705 |
| binance:XRPUSDT | 60s | -2.383 | -4.766 |
| gate:ADAUSDT | 1s | -2.048 | -4.096 |
| gate:ADAUSDT | 5s | -4.251 | -8.501 |
| gate:ADAUSDT | 30s | -3.600 | -7.201 |
| gate:ADAUSDT | 60s | -3.217 | -6.433 |
| gate:AVAXUSDT | 1s | -1.642 | -3.283 |
| gate:AVAXUSDT | 5s | -6.103 | -12.207 |
| gate:AVAXUSDT | 30s | -6.813 | -13.625 |
| gate:AVAXUSDT | 60s | -5.322 | -10.644 |
| gate:BTCUSDT | 1s | -2.230 | -4.460 |
| gate:BTCUSDT | 5s | -2.942 | -5.883 |
| gate:BTCUSDT | 30s | -2.791 | -5.582 |
| gate:BTCUSDT | 60s | -2.578 | -5.157 |
| gate:DOGEUSDT | 1s | -2.325 | -4.650 |
| gate:DOGEUSDT | 5s | -3.556 | -7.112 |
| gate:DOGEUSDT | 30s | -3.755 | -7.510 |
| gate:DOGEUSDT | 60s | -3.877 | -7.754 |
| gate:ETHUSDT | 1s | -2.130 | -4.260 |
| gate:ETHUSDT | 5s | -3.204 | -6.408 |
| gate:ETHUSDT | 30s | -3.599 | -7.197 |
| gate:ETHUSDT | 60s | -3.754 | -7.509 |
| gate:LINKUSDT | 1s | -2.577 | -5.154 |
| gate:LINKUSDT | 5s | -3.180 | -6.360 |
| gate:LINKUSDT | 30s | -3.255 | -6.510 |
| gate:LINKUSDT | 60s | -2.387 | -4.774 |
| gate:SOLUSDT | 1s | -2.288 | -4.576 |
| gate:SOLUSDT | 5s | -3.603 | -7.207 |
| gate:SOLUSDT | 30s | -3.973 | -7.946 |
| gate:SOLUSDT | 60s | -4.440 | -8.881 |
| gate:XRPUSDT | 1s | -2.232 | -4.464 |
| gate:XRPUSDT | 5s | -3.177 | -6.353 |
| gate:XRPUSDT | 30s | -2.835 | -5.671 |
| gate:XRPUSDT | 60s | -2.757 | -5.515 |
| htx:ADAUSDT | 1s | -3.172 | -6.344 |
| htx:ADAUSDT | 5s | -6.195 | -12.390 |
| htx:ADAUSDT | 30s | -6.577 | -13.153 |
| htx:ADAUSDT | 60s | -6.620 | -13.239 |
| htx:AVAXUSDT | 1s | -3.012 | -6.024 |
| htx:AVAXUSDT | 5s | -3.012 | -6.024 |
| htx:AVAXUSDT | 30s | -5.495 | -10.991 |
| htx:AVAXUSDT | 60s | -5.771 | -11.543 |
| htx:BTCUSDT | 1s | -2.015 | -4.030 |
| htx:BTCUSDT | 5s | -3.139 | -6.277 |
| htx:BTCUSDT | 30s | -3.150 | -6.301 |
| htx:BTCUSDT | 60s | -3.395 | -6.790 |
| htx:ETHUSDT | 1s | -2.061 | -4.123 |
| htx:ETHUSDT | 5s | -2.978 | -5.955 |
| htx:ETHUSDT | 30s | -2.685 | -5.370 |
| htx:ETHUSDT | 60s | -2.684 | -5.369 |
| htx:LINKUSDT | 1s | 2.843 | 5.686 |
| htx:LINKUSDT | 5s | 0.782 | 1.564 |
| htx:LINKUSDT | 30s | 1.838 | 3.675 |
| htx:LINKUSDT | 60s | 2.637 | 5.274 |
| htx:SOLUSDT | 1s | -2.320 | -4.640 |
| htx:SOLUSDT | 5s | -2.817 | -5.633 |
| htx:SOLUSDT | 30s | -2.248 | -4.496 |
| htx:SOLUSDT | 60s | -2.608 | -5.216 |

## 6. Assumptions and limitations (read before the verdict)

1. **Adverse selection is a lower bound, not a measurement of toxicity.** REST latency is ~100–465 ms; the toxic component of a fill is faster than our sampling can see. True adverse cost ≥ what §3 shows.
2. **Fill rate is a lower bound.** We re-quote every tick (2 s) and only count a fill when a trade prints *strictly through* the snapshot touch. A real maker re-quotes continuously and holds queue position, so it fills more often — but also gets picked off more often. Both effects push the same way: more fills of a negative-edge trade.
3. **Inventory risk is not hedged.** We accumulate the losing side; the adverse term is the proxy for that cost, and it is a lower bound.
4. **Fees are VIP0 = 10 bps/leg.** The 2 bps what-if (§5) is a tier a $100 account cannot reach and is not the verdict.
5. **Zero through-fills observed** for: htx:DOGEUSDT, htx:XRPUSDT. These have no net-edge estimate; it is absence of a crossing print in the window, not a positive result.
6. **Single 25-minute window.** Microstructure regimes shift; this is one snapshot of one afternoon.

## Verdict — at VIP0 (10 bps/leg), is maker-only spread capture net-positive at $100?

**NO.** No venue:pair in the window produced a positive per-fill net edge at the VIP0 maker rate, at any measured horizon.

- What-if (2 bps, NOT achievable here): 1/24 positive.

**Best VIP0 case per venue:pair (least-negative / most-positive horizon):**

| venue:pair | best horizon | net/fill bps | 95% CI | $/day @ $100 | $/min @ $100 |
|---|---|---:|---|---:|---:|
| binance:ADAUSDT | 1s | -12.800 | [-13.40, -12.20] | -1151.035 | -0.79933 |
| binance:AVAXUSDT | 60s | -13.429 | [-14.05, -12.87] | -4048.408 | -2.81140 |
| binance:BTCUSDT | 1s | -10.418 | [-10.45, -10.39] | -12527.076 | -8.69936 |
| binance:DOGEUSDT | 1s | -11.380 | [-11.80, -10.99] | -419.808 | -0.29153 |
| binance:ETHUSDT | 1s | -10.415 | [-10.45, -10.38] | -7666.748 | -5.32413 |
| binance:LINKUSDT | 1s | -12.147 | [-12.36, -11.95] | -3479.956 | -2.41664 |
| binance:SOLUSDT | 1s | -10.942 | [-11.21, -10.68] | -977.632 | -0.67891 |
| binance:XRPUSDT | 60s | -10.383 | [-10.80, -10.01] | -436.906 | -0.30341 |
| gate:ADAUSDT | 1s | -10.048 | [-10.24, -9.87] | -816.649 | -0.56712 |
| gate:AVAXUSDT | 1s | -9.642 | [-10.22, -9.10] | -327.900 | -0.22771 |
| gate:BTCUSDT | 1s | -10.230 | [-10.29, -10.18] | -996.574 | -0.69207 |
| gate:DOGEUSDT | 1s | -10.325 | [-10.56, -10.12] | -374.948 | -0.26038 |
| gate:ETHUSDT | 1s | -10.130 | [-10.16, -10.10] | -2143.019 | -1.48821 |
| gate:LINKUSDT | 60s | -10.387 | [-11.66, -8.93] | -329.305 | -0.22868 |
| gate:SOLUSDT | 1s | -10.288 | [-10.53, -10.08] | -456.632 | -0.31711 |
| gate:XRPUSDT | 1s | -10.232 | [-10.37, -10.11] | -719.554 | -0.49969 |
| htx:ADAUSDT | 1s | -11.172 | [-12.64, -9.72] | -57.960 | -0.04025 |
| htx:AVAXUSDT | 1s | -11.012 | [-11.01, -11.01] | -12.695 | -0.00882 |
| htx:BTCUSDT | 1s | -10.015 | [-10.02, -10.01] | -35561.171 | -24.69526 |
| htx:DOGEUSDT | — | — | — | — | — |
| htx:ETHUSDT | 1s | -10.061 | [-10.16, -9.98] | -458.171 | -0.31817 |
| htx:LINKUSDT | 1s | -5.157 | [-6.21, -3.96] | -14.863 | -0.01032 |
| htx:SOLUSDT | 30s | -10.248 | [-10.89, -9.78] | -47.258 | -0.03282 |
| htx:XRPUSDT | — | — | — | — | — |

- Where the adverse proxy alone exceeds the half-spread minus the VIP0 fee, the raw capture condition is met and the quote still loses: the spread is not capturable, it is compensation for being adversely selected.
- Because the adverse term is a **lower bound**, every positive number above is an **upper bound** on the real edge; a marginal positive here does not establish viability.
- Fee provenance: the venues' fee pages are unreachable from this host (see reachability in `maker_results.json`), so the 10 bps VIP0 rate is the value DESIGN.md §10.2 already encodes and is labelled as such.
- **Magnitude:** the best VIP0 case is `gate:AVAXUSDT` at **−9.64 bps/fill**. The VIP0 fee alone is 10 bps/leg. The median quoted spread is *below that fee* on **22/24** venue:pairs, and **no** venue:pair has a median spread above **2×** the fee (20 bps, the level a half-spread capture needs to cover one fee) — 0 qualify. So the fee is not coverable by spread capture at VIP0 on any book measured. $/day and $/min are **negative** everywhere at VIP0.

