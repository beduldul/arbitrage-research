#!/usr/bin/env python3
"""Long-history decisive re-test of the single-venue carry survivor (MEASUREMENT ONLY).

The consolidation's one positive single-venue result, Gate ``龙虾_USDT`` (~$0.047/day at
$100), rests on a ~30-day funding window because Gate's funding endpoint caps a *single*
call at 1000 rows. This script asks whether a longer sample is obtainable and, if so,
whether the carry survives it — and whether funding ever goes *persistently negative*
(the regime flip that would kill a short-perp carry).

**Finding: longer history IS obtainable on Gate.** The endpoint accepts a ``to`` bound
without ``from``, so paginating backwards reaches the contract's launch:
``龙虾_USDT`` launched 2026-03-10; funding history reaches **2026-03-20 (197 days, 1180
rows)** and 1d candles reach **2026-03-10 (208 days)**. No other venue lists this base
(Bybit/OKX/Bitget refused on retry and do not carry it; Hyperliquid has no such coin).

The re-test is the decisive study's own hedged daily PnL (funding + basis drift − four
fee events) and its IS/OOS + block-bootstrap estimator, run on the full 197-day window.

**No orders. No keys. Writes only under ``evidence/``.**

Run::

    uv run python scripts/arb_longxia_longhistory.py
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import arb_funding_decisive as dc  # noqa: E402
import arb_funding_multivenue as mv  # noqa: E402

GATE_BASE = "https://api.gateio.ws/api/v4/futures/usdt"
DEFAULT_OUT = REPO_ROOT / "evidence" / "arbitrage" / "2026-10-03" / "crossvenue_funding" / "longxia"
SYMBOL = "龙虾_USDT"
SPOT_PAIR = "龙虾_USDT"

#: Gate base-tier fees, VERIFIED from ``futures/usdt/contracts`` (same as the decisive study).
SPOT_TAKER = 0.0010   # Gate spot published base taker 0.10%
PERP_TAKER = 0.00075  # Gate perp API-verified taker 0.075%
NOTIONAL = 100.0
LEG = NOTIONAL / 2.0


def fetch_funding_full(client: mv.HttpClient) -> tuple[list[float], list[int]]:
    """Full funding history by paginating backwards on ``to`` until the contract start.

    Gate rejects a ``from``/``to`` span wider than ~180 days, but ``to`` *alone* returns
    the 1000 rows ending at ``to`` — so paging ``to = oldest_returned - 1`` walks all the
    way back to the contract's first settlement (2026-03-10 for this contract).
    """
    sym = urllib.parse.quote(SYMBOL)
    now = int(time.time())
    rows: dict[int, float] = {}
    # First call: a bounded recent window (Gate rejects a from/to span > ~180d).
    first = client.request(
        f"{GATE_BASE}/funding_rate?contract={sym}&from={now - 180 * 86400}&to={now}&limit=1000",
        cache_path=client.cache_dir / "gate" / "longxia_funding_p0.json",
    )
    if not isinstance(first, list):
        return [], []
    for r in first:
        rows[int(r["t"])] = float(r["r"])
    oldest = min(int(r["t"]) for r in first)
    for page in range(1, 8):
        chunk = client.request(
            f"{GATE_BASE}/funding_rate?contract={sym}&to={oldest - 1}&limit=1000",
            cache_path=client.cache_dir / "gate" / f"longxia_funding_p{page}.json",
        )
        if not isinstance(chunk, list) or not chunk:
            break
        for r in chunk:
            rows[int(r["t"])] = float(r["r"])
        chunk_oldest = min(int(r["t"]) for r in chunk)
        if chunk_oldest >= oldest:  # no progress -> stop rather than loop
            break
        oldest = chunk_oldest
    ts = sorted(rows)
    return [rows[t] for t in ts], [t * 1000 for t in ts]


def fetch_candles_full(client: mv.HttpClient, *, spot: bool) -> dict[int, float]:
    """Full 1d candle history (``limit`` high enough to reach the contract start)."""
    sym = urllib.parse.quote(SYMBOL)
    if spot:
        url = (
            f"https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair={sym}"
            "&interval=1d&limit=1000"
        )
        path = client.cache_dir / "gate" / "longxia_spot_candles.json"
    else:
        url = f"{GATE_BASE}/candlesticks?contract={sym}&interval=1d&limit=1000"
        path = client.cache_dir / "gate" / "longxia_perp_candles.json"
    rows = client.request(url, cache_path=path)
    if not isinstance(rows, list):
        return {}
    if spot:
        # spot arrays: [t, quote_vol, close, high, low, open, base_vol, closed]
        return {dc.utc_day(float(r[0])): float(r[2]) for r in rows}
    return {dc.utc_day(float(r["t"])): float(r["c"]) for r in rows}


def funding_regime(funding: dict[int, float], days: list[int]) -> dict[str, object]:
    """Describe whether funding ever goes persistently negative over the window."""
    vals = [funding.get(d, 0.0) for d in days]
    n = len(vals)
    neg = [v for v in vals if v < 0]
    # Longest consecutive negative run (days).
    best = run = 0
    for v in vals:
        run = run + 1 if v < 0 else 0
        best = max(best, run)
    return {
        "n_days": n,
        "negative_days": len(neg),
        "negative_share": round(len(neg) / n, 4) if n else 0.0,
        "longest_negative_run_days": best,
        "worst_day": round(min(vals), 8) if vals else 0.0,
        "mean_daily_funding": round(statistics.fmean(vals), 8) if vals else 0.0,
    }


@dataclass(frozen=True)
class LongHistoryVerdict:
    symbol: str
    window_days: int
    n_days: int
    funding_rows: int
    gross_funding_usd: float
    basis_drift_usd: float
    fees_usd: float
    net_usd: float
    net_usd_per_day: float
    net_usd_per_day_trimmed: float
    net_usd_per_day_recent30: float
    is_net_usd: float
    oos_net_usd: float
    is_days: int
    oos_days: int
    positive_in_both_halves: bool
    oos_ci_low: float
    oos_ci_high: float
    ci_excludes_zero: bool
    funding_negative_days: int
    funding_negative_share: float
    longest_negative_run_days: int
    largest_day_share_of_gross: float
    verdict: str
    reason: str


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--block", type=int, default=5)
    parser.add_argument("--n-boot", type=int, default=2000)
    args = parser.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    cache_dir = args.out / "raw"
    cache_dir.mkdir(parents=True, exist_ok=True)
    client = mv.HttpClient(cache_dir)

    rates, times = fetch_funding_full(client)
    if not rates:
        print("no funding history", file=sys.stderr)
        return 1
    funding = dc.daily_funding(times, rates)
    spot_close = fetch_candles_full(client, spot=True)
    perp_close = fetch_candles_full(client, spot=False)

    common = sorted(set(spot_close) & set(perp_close) & set(funding))
    if len(common) < 4:
        print(f"only {len(common)} aligned days", file=sys.stderr)
        return 1

    daily = dc.hedged_daily_net_dollars(
        common, spot_close, perp_close, funding,
        leg_notional=LEG, spot_fee=SPOT_TAKER, perp_fee=PERP_TAKER,
    )
    funding_only = [LEG * funding.get(d, 0.0) for d in common]
    fees_only = [
        -LEG * (SPOT_TAKER + PERP_TAKER) if (i == 0 or i == len(common) - 1) else 0.0
        for i in range(len(common))
    ]
    gross_funding = sum(funding_only)
    fees_usd = sum(fees_only)
    basis_drift = sum(daily) - gross_funding - fees_usd
    net = sum(daily)

    is_half, oos_half = dc.split_is_oos(daily)
    is_net, oos_net = sum(is_half), sum(oos_half)
    ci_lo, ci_hi = dc.block_bootstrap_ci(oos_half, block=args.block, n_boot=args.n_boot)

    reg = funding_regime(funding, common)
    positive_both = is_net > 0 and oos_net > 0
    ci_excludes_zero = ci_lo > 0
    if not positive_both:
        verdict, reason = "no", (
            f"not positive in both halves (IS ${is_net:.4f}, OOS ${oos_net:.4f})"
        )
    elif not ci_excludes_zero:
        verdict, reason = "no", f"OOS CI [{ci_lo:.4f}, {ci_hi:.4f}] includes zero"
    else:
        verdict, reason = "survives", (
            "positive both halves, OOS CI excludes zero on the long window"
        )

    # --- robustness: outlier trim + recent-subwindow, so the headline is not one day ---
    daily_funding_usd = [LEG * funding.get(d, 0.0) for d in common]
    if daily_funding_usd:
        hi = max(daily_funding_usd)
        trimmed = [v for v in daily_funding_usd if v != hi]
        net_trimmed_per_day = statistics.fmean(trimmed) if trimmed else 0.0
        largest_share = hi / gross_funding if gross_funding else 0.0
    else:
        net_trimmed_per_day, largest_share = 0.0, 0.0
    recent30 = [LEG * funding.get(d, 0.0) for d in common[-30:]]
    recent30_per_day = statistics.fmean(recent30) if recent30 else 0.0

    v = LongHistoryVerdict(
        symbol=SYMBOL,
        window_days=(common[-1] - common[0]),
        n_days=len(common),
        funding_rows=len(rates),
        gross_funding_usd=round(gross_funding, 4),
        basis_drift_usd=round(basis_drift, 4),
        fees_usd=round(fees_usd, 4),
        net_usd=round(net, 4),
        net_usd_per_day=round(net / len(common), 5),
        net_usd_per_day_trimmed=round(net_trimmed_per_day, 5),
        net_usd_per_day_recent30=round(recent30_per_day, 5),
        is_net_usd=round(is_net, 4),
        oos_net_usd=round(oos_net, 4),
        is_days=len(is_half),
        oos_days=len(oos_half),
        positive_in_both_halves=positive_both,
        oos_ci_low=round(ci_lo, 4),
        oos_ci_high=round(ci_hi, 4),
        ci_excludes_zero=ci_excludes_zero,
        funding_negative_days=int(reg["negative_days"]),  # type: ignore[arg-type]
        funding_negative_share=float(reg["negative_share"]),  # type: ignore[arg-type]
        longest_negative_run_days=int(reg["longest_negative_run_days"]),  # type: ignore[arg-type]
        largest_day_share_of_gross=round(largest_share, 4),
        verdict=verdict,
        reason=reason,
    )

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "symbol": SYMBOL,
        "venue": "gate",
        "notional_usd": NOTIONAL,
        "leg_usd": LEG,
        "fees": {"spot_taker": SPOT_TAKER, "perp_taker": PERP_TAKER, "verified": True},
        "funding_window": {
            "oldest": datetime.fromtimestamp(min(times) / 1000, tz=UTC).isoformat(),
            "newest": datetime.fromtimestamp(max(times) / 1000, tz=UTC).isoformat(),
            "rows": len(rates),
        },
        "bootstrap": {"block_days": args.block, "n_boot": args.n_boot, "seed": 20261003},
        "verdict": asdict(v),
        "funding_regime": reg,
    }
    (args.out / "longxia_longhistory.json").write_text(json.dumps(payload, indent=2))

    print(f"{SYMBOL} @ Gate — {len(common)} aligned days, {len(rates)} funding rows")
    print(f"  gross funding ${gross_funding:.4f}  basis ${basis_drift:.4f}  fees ${fees_usd:.4f}")
    print(f"  net ${net:.4f}  ({net / len(common):.5f}/day)")
    print(f"  robustness: trimmed-one-day {net_trimmed_per_day:.5f}/day, "
          f"recent-30d {recent30_per_day:.5f}/day, largest day {largest_share:.1%} of gross")
    print(
        f"  IS ${is_net:.4f} ({len(is_half)}d)  OOS ${oos_net:.4f} ({len(oos_half)}d)  "
        f"CI [{ci_lo:.4f}, {ci_hi:.4f}]"
    )
    print(
        f"  funding negative {reg['negative_days']}/{len(common)} days "
        f"({reg['negative_share']:.1%}), longest negative run "
        f"{reg['longest_negative_run_days']}d"
    )
    print(f"  VERDICT: {verdict} — {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
