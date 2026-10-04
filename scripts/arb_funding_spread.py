#!/usr/bin/env python3
"""Cross-venue perp funding-spread scanner (MEASUREMENT ONLY).

The largest-capacity carry variant is not single-venue cash-and-carry (which needs a
spot leg and was already settled as marginal). It is the **cross-venue funding spread**:
for one base asset listed as a USDT perp on two venues, go long the perp on the venue
whose funding is *negative* and short the perp on the venue whose funding is *positive*.
Both legs are perps, so price risk is hedged without any spot leg — that is the whole
advantage. Net income is the funding spread; the cost is four fee events (2 venues × in/out)
plus the bid-ask spread crossed on each leg, twice.

**No orders. No keys. No live execution path. Writes only under ``evidence/``.**

Reachability (this host, 2026-10-03) is INTERMITTENT for Bybit/OKX/Bitget: the same
hosts have answered with real funding JSON and also resolved to a sinkhole (202.169.44.80)
minutes apart. This scanner therefore probes **every endpoint >= 5 times with 2-3s
spacing**, records a per-attempt row (status, bytes, latency), and validates that the
payload actually contains a funding rate + timestamp before trusting it. A venue is only
reported unreachable if *all* attempts fail — one refusal is never a conclusion.

Cost model — **not invented**
-----------------------------
Fees come from :class:`crypto_brain.engine.fees.FeeSchedule` (project base tier:
5 bps perp taker, 2 bps perp maker, DESIGN.md §10.2). Each venue's *own* published base
tier is recorded and marked verified/unverified (no venue exposes an unauthenticated fee
endpoint, so all are UNVERIFIED). The measured bid-ask half-spread is added separately,
charged on every crossing (2 legs × in/out = 4 crossings).

Run::

    uv run python scripts/arb_funding_spread.py
    uv run python scripts/arb_funding_spread.py --offline   # reuse cached raw responses
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import arb_funding_decisive as dc  # noqa: E402
import arb_funding_multivenue as mv  # noqa: E402

from crypto_brain.engine.fees import FeeSchedule  # noqa: E402
from crypto_brain.engine.slippage import MIN_QUOTE_VOLUME_USD  # noqa: E402

PROJECT_FEES = FeeSchedule()  # DESIGN.md §10.2 base tier: 5 bps perp taker, 2 bps maker

UA = "crypto-brain-arb-funding-spread/1.0 (paper research; read-only)"
GATE_BASE = "https://api.gateio.ws/api/v4/futures/usdt"
HL_INFO = "https://api.hyperliquid.xyz/info"
BYBIT_BASE = "https://api.bybit.com/v5"
OKX_BASE = "https://www.okx.com/api/v5"
BITGET_BASE = "https://api.bitget.com/api/v2/mix"

#: Bounded concurrency — siblings share this IP.
NETWORK_CONCURRENCY = 4
PER_DOMAIN_RATE_PER_S = 5.0
MAX_RETRIES = 6
BACKOFF_BASE_S = 1.5

#: Reachability probe discipline (reinforced): >= 5 attempts, 2-3s spacing.
PROBE_ATTEMPTS = 5
PROBE_SPACING_S = 2.5

TARGET_WINDOW_DAYS = 30.0
BUCKET_HOURS = 8.0
DEFAULT_OUT = REPO_ROOT / "evidence" / "arbitrage" / "2026-10-03" / "crossvenue_funding"

#: Each venue's own published base-tier perp fee, from public docs. UNVERIFIED: no venue
#: exposes an unauthenticated fee endpoint. ``None`` => unknown, project default used.
VENUE_FEE_NOTE: dict[str, dict[str, object]] = {
    "gate": {
        "published_base_taker_bps": 5.0,
        "published_base_maker_bps": 2.0,
        "verified": False,
        "source": "public docs (no unauthenticated fee endpoint)",
    },
    "bybit": {
        "published_base_taker_bps": 5.5,
        "published_base_maker_bps": 2.0,
        "verified": False,
        "source": "public docs (no unauthenticated fee endpoint)",
    },
    "okx": {
        "published_base_taker_bps": 5.0,
        "published_base_maker_bps": 2.0,
        "verified": False,
        "source": "public docs (no unauthenticated fee endpoint)",
    },
    "bitget": {
        "published_base_taker_bps": 6.0,
        "published_base_maker_bps": 2.0,
        "verified": False,
        "source": "public docs (no unauthenticated fee endpoint)",
    },
    "hyperliquid": {
        "published_base_taker_bps": 3.5,
        "published_base_maker_bps": 1.0,
        "verified": False,
        "source": "public docs (no unauthenticated fee endpoint)",
    },
}

#: Hyperliquid enforces a $10 minimum order value; the others publish per-symbol floors.
HL_MIN_ORDER_USD = 10.0


# ===========================================================================
# Pure computation — the part the unit tests pin
# ===========================================================================


def bucket_index(t_ms: int, *, bucket_hours: float = BUCKET_HOURS) -> int:
    """Epoch-ms -> integer bucket number of ``bucket_hours`` width (UTC-aligned)."""
    if bucket_hours <= 0:
        raise ValueError("bucket_hours must be positive")
    return int(t_ms // int(bucket_hours * 3_600_000.0))


def bucket_funding(
    times_ms: list[int], rates: list[float], *, bucket_hours: float = BUCKET_HOURS
) -> dict[int, float]:
    """Sum funding rates (fractions) into fixed ``bucket_hours`` windows.

    A venue settling hourly contributes eight hourly rates to one 8h bucket; a venue
    settling 4-hourly contributes two. Summing within the bucket makes venues with
    different native intervals comparable *as income over the same wall-clock window* —
    which is what a delta-neutral spread actually collects.
    """
    out: dict[int, float] = {}
    for t, r in zip(times_ms, rates, strict=False):
        b = bucket_index(t, bucket_hours=bucket_hours)
        out[b] = out.get(b, 0.0) + r
    return out


def aligned_spread(
    a: dict[int, float], b: dict[int, float]
) -> tuple[list[int], list[float], list[float], list[float]]:
    """Align two bucketed funding series on shared buckets.

    Returns ``(buckets, a_vals, b_vals, spreads)`` sorted by bucket, where
    ``spread = a - b`` (positive => venue A pays more than venue B). Only buckets present
    in *both* series are returned — a bucket only one venue settled is not a spread.
    """
    shared = sorted(set(a) & set(b))
    av = [a[k] for k in shared]
    bv = [b[k] for k in shared]
    sv = [x - y for x, y in zip(av, bv, strict=True)]
    return shared, av, bv, sv


@dataclass(frozen=True)
class SpreadStats:
    """Summary of a per-bucket spread series (fractions)."""

    n: int
    mean_bps: float
    median_bps: float
    min_bps: float
    max_bps: float
    positive_share: float
    negative_share: float
    longest_same_sign_streak: int
    longest_same_sign_streak_days: float
    sign_flip_rate: float
    abs_mean_bps: float


def longest_same_sign_run(series: list[float]) -> int:
    """Longest run of consecutive strictly same-sign values (zeros break the run)."""
    best = 0
    run = 0
    sign = 0
    for v in series:
        s = 1 if v > 0 else (-1 if v < 0 else 0)
        if s != 0 and s == sign:
            run += 1
        elif s != 0:
            run = 1
            sign = s
        else:
            run = 0
            sign = 0
        best = max(best, run)
    return best


def spread_stats(spreads: list[float], *, bucket_hours: float = BUCKET_HOURS) -> SpreadStats:
    """Summarise a spread series given as **fractions per bucket**. Raises if empty."""
    if not spreads:
        raise ValueError("spread series is empty")
    bps = [s * 10_000.0 for s in spreads]
    n = len(bps)
    streak = longest_same_sign_run(spreads)
    flips = sum(1 for i in range(1, n) if (bps[i] > 0) != (bps[i - 1] > 0))
    return SpreadStats(
        n=n,
        mean_bps=statistics.fmean(bps),
        median_bps=statistics.median(bps),
        min_bps=min(bps),
        max_bps=max(bps),
        positive_share=sum(1 for x in bps if x > 0) / n,
        negative_share=sum(1 for x in bps if x < 0) / n,
        longest_same_sign_streak=streak,
        longest_same_sign_streak_days=streak * bucket_hours / 24.0,
        sign_flip_rate=(flips / (n - 1)) if n > 1 else 0.0,
        abs_mean_bps=statistics.fmean(abs(x) for x in bps),
    )


@dataclass(frozen=True)
class CrossVenueCost:
    """One-time round-trip cost in USD for a two-perp delta-neutral spread at ``leg_usd``.

    Four fee events (2 venues × in/out) plus the bid-ask spread crossed on each leg twice
    (in and out), i.e. ``2 legs × 2 crossings = 4`` half-spread charges.
    """

    leg_usd: float
    fees_taker_usd: float
    fees_maker_usd: float
    spread_usd: float
    total_taker_usd: float
    total_maker_usd: float
    total_taker_bps_of_total: float
    total_maker_bps_of_total: float


def cross_venue_cost(
    *,
    leg_usd: float,
    half_spread_bps_a: float,
    half_spread_bps_b: float,
    crossings: int = 4,
    taker_bps: float = PROJECT_FEES.futures_taker_bps,
    maker_bps: float = PROJECT_FEES.futures_maker_bps,
) -> CrossVenueCost:
    """One-time cost of opening and closing both perp legs.

    ``leg_usd`` is the notional on *each* leg (so the trade's total notional is
    ``2 * leg_usd``). Fees are charged per event on the leg notional: 4 events at the
    project's futures taker/maker rate. Spread is the measured half-spread charged on
    ``crossings`` (default 4: two legs, in and out) events on the leg notional.
    """
    if leg_usd <= 0:
        raise ValueError("leg_usd must be positive")
    if crossings < 0:
        raise ValueError("crossings must be non-negative")
    fees_taker = crossings * leg_usd * (taker_bps / 10_000.0)
    fees_maker = crossings * leg_usd * (maker_bps / 10_000.0)
    # Each crossing is half the two-leg spread cost; sum over both legs then x crossings/2
    # would double count, so charge each leg's own half-spread on its own crossings.
    half_events = crossings / 2.0
    spread = half_events * leg_usd * ((half_spread_bps_a + half_spread_bps_b) / 10_000.0)
    total_taker = fees_taker + spread
    total_maker = fees_maker + spread
    total_notional = 2.0 * leg_usd
    return CrossVenueCost(
        leg_usd=leg_usd,
        fees_taker_usd=fees_taker,
        fees_maker_usd=fees_maker,
        spread_usd=spread,
        total_taker_usd=total_taker,
        total_maker_usd=total_maker,
        total_taker_bps_of_total=total_taker / total_notional * 10_000.0,
        total_maker_bps_of_total=total_maker / total_notional * 10_000.0,
    )


def dollars_per_day(
    leg_usd: float, mean_spread_fraction: float, *, bucket_hours: float = BUCKET_HOURS
) -> float:
    """Funding-spread income per day, in USD, for a ``leg_usd`` position on each leg.

    The spread is collected on the leg notional (both legs are the same size), so income
    per bucket is ``leg_usd * spread``; scaling by buckets-per-day converts to a day.
    """
    if bucket_hours <= 0:
        raise ValueError("bucket_hours must be positive")
    return leg_usd * mean_spread_fraction * (24.0 / bucket_hours)


def dollars_per_minute(
    leg_usd: float, mean_spread_fraction: float, *, bucket_hours: float = BUCKET_HOURS
) -> float:
    """Funding-spread income per minute, in USD (same arithmetic, /1440)."""
    return dollars_per_day(leg_usd, mean_spread_fraction, bucket_hours=bucket_hours) / 1440.0


def split_is_oos(series: list[float]) -> tuple[list[float], list[float]]:
    """First half in-sample, second half out-of-sample (chronological)."""
    mid = len(series) // 2
    return series[:mid], series[mid:]


def min_leg_ok(min_notional_usd: float, leg_usd: float) -> bool:
    """Does the per-leg notional clear the venue's minimum order value?

    A zero/absent floor means no published minimum notional; the binding constraint is
    then the minimum quantity, which the caller has already folded into ``min_notional_usd``
    where it could (price × min qty). Zero therefore passes here.
    """
    if min_notional_usd <= 0:
        return True
    return leg_usd >= min_notional_usd


def capacity_usd(depths: list[float | None]) -> float | None:
    """Capacity bound = the shallowest measured leg depth within the price band.

    A spread trade is only as large as its thinner leg; ``None`` when no depth was
    measured. Depth values are USD notional resting within the band on each venue.
    """
    vals = [d for d in depths if d is not None and d > 0]
    if not vals:
        return None
    return min(vals)


def bootstrap_ci(
    series: list[float], *, block: int, n_boot: int = 2000, seed: int = 20261003
) -> tuple[float, float]:
    """95% moving-block bootstrap CI for the **total** of ``series`` (reuses the decisive
    study's estimator so both studies define significance identically)."""
    return dc.block_bootstrap_ci(series, block=block, n_boot=n_boot, seed=seed)


@dataclass(frozen=True)
class PairResult:
    """One row of ``spread_ranking.json`` — a candidate cross-venue spread."""

    base: str
    venue_a: str
    venue_b: str
    symbol_a: str
    symbol_b: str
    n_buckets: int
    window_days: float
    mean_spread_bps_8h: float
    median_spread_bps_8h: float
    abs_mean_spread_bps_8h: float
    positive_share: float
    negative_share: float
    longest_streak_days: float
    sign_flip_rate: float
    same_sign_share: float
    # direction actually traded: long the negative-funding venue, short the positive one
    long_venue: str
    short_venue: str
    gross_dollars_per_day: float
    gross_dollars_per_min: float
    cost_taker_usd: float
    cost_maker_usd: float
    cost_taker_bps_of_total: float
    cost_maker_bps_of_total: float
    net_dollars_per_day_taker: float
    net_dollars_per_day_maker: float
    break_even_days_taker: float | None
    break_even_days_maker: float | None
    is_mean_bps: float
    oos_mean_bps: float
    is_signed_bps: float
    oos_signed_bps: float
    oos_ci_low: float
    oos_ci_high: float
    positive_both_halves: bool
    # $100 constructibility
    min_notional_a_usd: float | None
    min_notional_b_usd: float | None
    constructible: bool
    depth_a_usd: float | None
    depth_b_usd: float | None
    capacity_usd: float | None
    half_spread_a_bps: float | None
    half_spread_b_bps: float | None
    verdict: str
    reason: str


def evaluate_pair(
    *,
    base: str,
    venue_a: str,
    symbol_a: str,
    venue_b: str,
    symbol_b: str,
    a_buckets: dict[int, float],
    b_buckets: dict[int, float],
    notional: float,
    block: int,
    n_boot: int,
    min_notional_a_usd: float | None,
    min_notional_b_usd: float | None,
    depth_a_usd: float | None,
    depth_b_usd: float | None,
    half_spread_a_bps: float | None,
    half_spread_b_bps: float | None,
    min_abs_mean_bps: float = 0.0,
    min_shared_buckets: int = 42,
    min_same_sign_share: float = 0.60,
) -> PairResult:
    """Pure per-pair computation: two bucketed funding series -> a full ranked row.

    ``min_shared_buckets`` (default 42 ≈ 14 days at 8h) and ``min_same_sign_share``
    (default 0.60) are robustness bars: a spread whose *sign* flips in most intervals, or
    that is observed over only a few days, cannot be called persistent no matter how large
    its mean — a mean driven by one outlier bucket is not a carry.
    """
    buckets, av, bv, spreads = aligned_spread(a_buckets, b_buckets)
    if not spreads:
        raise ValueError(f"{base}: no shared funding buckets between {venue_a}/{venue_b}")

    stats = spread_stats(spreads, bucket_hours=BUCKET_HOURS)
    leg_usd = notional / 2.0

    # Direction: long the venue with negative funding, short the venue with positive.
    # A positive mean spread (A pays more) means short A / long B.
    if stats.mean_bps >= 0:
        long_venue, short_venue = venue_b, venue_a
    else:
        long_venue, short_venue = venue_a, venue_b

    cost = cross_venue_cost(
        leg_usd=leg_usd,
        half_spread_bps_a=half_spread_a_bps or 0.0,
        half_spread_bps_b=half_spread_b_bps or 0.0,
    )

    # Gross income is the *absolute* spread (we always trade the profitable direction).
    gross_per_day = dollars_per_day(leg_usd, abs(stats.mean_bps) / 10_000.0)
    gross_per_min = dollars_per_minute(leg_usd, abs(stats.mean_bps) / 10_000.0)
    # Net $/day = steady-state funding income minus the one-time round-trip cost amortised
    # over the measured window. We do not invent a holding period: the window IS the
    # observed horizon, and ``break_even_days`` states the minimum hold for a profit.
    window_days = (max(buckets) - min(buckets)) * BUCKET_HOURS / 24.0 if len(buckets) > 1 else 0.0
    horizon_days = max(window_days, 1.0)
    net_taker = gross_per_day - cost.total_taker_usd / horizon_days
    net_maker = gross_per_day - cost.total_maker_usd / horizon_days
    be_taker = None if gross_per_day <= 0 else cost.total_taker_usd / gross_per_day
    be_maker = None if gross_per_day <= 0 else cost.total_maker_usd / gross_per_day

    # IS/OOS on the *signed* spread series (the sign must persist in both halves).
    is_half, oos_half = split_is_oos(spreads)
    is_mean = statistics.fmean(is_half) * 10_000.0 if is_half else 0.0
    oos_mean = statistics.fmean(oos_half) * 10_000.0 if oos_half else 0.0
    ci_lo, ci_hi = bootstrap_ci(oos_half, block=block, n_boot=n_boot) if oos_half else (0.0, 0.0)
    # CI is on the *total* of the OOS half; convert to per-bucket bps for readability.
    scale = 10_000.0 / max(1, len(oos_half))
    ci_lo_bps, ci_hi_bps = ci_lo * scale, ci_hi * scale

    # Direction we trade: the sign of the mean. Its persistence share is the robustness
    # bar — a spread that is only 41% same-sign is a flip-flop with a skewed mean.
    sign = 1.0 if stats.mean_bps >= 0 else -1.0
    same_sign_share = stats.positive_share if sign > 0 else stats.negative_share
    median_same_sign = (stats.median_bps > 0) == (sign > 0) and stats.median_bps != 0.0
    is_signed = sign * is_mean
    oos_signed = sign * oos_mean
    ci_lo_signed, ci_hi_signed = (ci_lo_bps, ci_hi_bps) if sign > 0 else (-ci_hi_bps, -ci_lo_bps)
    positive_both = is_signed > 0 and oos_signed > 0

    constructible = min_leg_ok(min_notional_a_usd or 0.0, leg_usd) and min_leg_ok(
        min_notional_b_usd or 0.0, leg_usd
    )
    cap = capacity_usd([depth_a_usd, depth_b_usd])


    verdict, reason = _classify(
        constructible=constructible,
        positive_both_halves=positive_both,
        oos_ci_low_signed=ci_lo_signed,
        gross_per_day=gross_per_day,
        cost_taker=cost.total_taker_usd,
        break_even_days_taker=be_taker,
        window_days=window_days,
        n_buckets=stats.n,
        same_sign_share=same_sign_share,
        median_same_sign=median_same_sign,
        min_shared_buckets=min_shared_buckets,
        min_same_sign_share=min_same_sign_share,
        min_abs_mean_bps=min_abs_mean_bps,
        abs_mean_bps=stats.abs_mean_bps,
        is_signed=is_signed,
        oos_signed=oos_signed,
    )

    return PairResult(
        base=base,
        venue_a=venue_a,
        venue_b=venue_b,
        symbol_a=symbol_a,
        symbol_b=symbol_b,
        n_buckets=stats.n,
        window_days=round(window_days, 2),
        mean_spread_bps_8h=round(stats.mean_bps, 4),
        median_spread_bps_8h=round(stats.median_bps, 4),
        abs_mean_spread_bps_8h=round(stats.abs_mean_bps, 4),
        positive_share=round(stats.positive_share, 4),
        negative_share=round(stats.negative_share, 4),
        longest_streak_days=round(stats.longest_same_sign_streak_days, 2),
        sign_flip_rate=round(stats.sign_flip_rate, 4),
        same_sign_share=round(same_sign_share, 4),
        long_venue=long_venue,
        short_venue=short_venue,
        gross_dollars_per_day=round(gross_per_day, 5),
        gross_dollars_per_min=round(gross_per_min, 6),
        cost_taker_usd=round(cost.total_taker_usd, 5),
        cost_maker_usd=round(cost.total_maker_usd, 5),
        cost_taker_bps_of_total=round(cost.total_taker_bps_of_total, 3),
        cost_maker_bps_of_total=round(cost.total_maker_bps_of_total, 3),
        net_dollars_per_day_taker=round(net_taker, 5),
        net_dollars_per_day_maker=round(net_maker, 5),
        break_even_days_taker=None if be_taker is None else round(be_taker, 2),
        break_even_days_maker=None if be_maker is None else round(be_maker, 2),
        is_mean_bps=round(is_mean, 4),
        oos_mean_bps=round(oos_mean, 4),
        is_signed_bps=round(is_signed, 4),
        oos_signed_bps=round(oos_signed, 4),
        oos_ci_low=round(ci_lo_signed, 4),
        oos_ci_high=round(ci_hi_signed, 4),
        positive_both_halves=positive_both,
        min_notional_a_usd=min_notional_a_usd,
        min_notional_b_usd=min_notional_b_usd,
        constructible=constructible,
        depth_a_usd=depth_a_usd,
        depth_b_usd=depth_b_usd,
        capacity_usd=cap,
        half_spread_a_bps=half_spread_a_bps,
        half_spread_b_bps=half_spread_b_bps,
        verdict=verdict,
        reason=reason,
    )


def _classify(
    *,
    constructible: bool,
    positive_both_halves: bool,
    oos_ci_low_signed: float,
    gross_per_day: float,
    cost_taker: float,
    break_even_days_taker: float | None,
    window_days: float,
    n_buckets: int,
    same_sign_share: float,
    median_same_sign: bool,
    min_shared_buckets: int,
    min_same_sign_share: float,
    min_abs_mean_bps: float,
    abs_mean_bps: float,
    is_signed: float,
    oos_signed: float,
) -> tuple[str, str]:
    """Verdict ladder, strictest first. Pure — no data access.

    Order of gates: noise floor -> constructibility -> observation length -> sign
    persistence -> both-halves positivity -> OOS CI -> cost amortisation. Each is a
    necessary condition for a *persistent* spread; failing any one is "no", with the
    reason naming which.
    """
    if abs_mean_bps < min_abs_mean_bps:
        return "no", (
            f"|mean spread| {abs_mean_bps:.3f} bps below the "
            f"{min_abs_mean_bps:.2f} bps noise bar"
        )
    if not constructible:
        return "unconstructible", "per-leg notional below a venue's minimum order value at $100"
    if n_buckets < min_shared_buckets:
        bar_days = min_shared_buckets * BUCKET_HOURS / 24
        return "no", (
            f"only {n_buckets} shared buckets (~{n_buckets * BUCKET_HOURS / 24:.1f}d) — "
            f"below the {min_shared_buckets}-bucket ({bar_days:.0f}d) persistence window"
        )
    if same_sign_share < min_same_sign_share:
        return "no", (
            f"traded sign holds in only {same_sign_share:.0%} of buckets "
            f"(bar {min_same_sign_share:.0%}) — a flip-flop, not a persistent spread"
        )
    if not median_same_sign:
        return "no", (
            "median spread has the opposite sign to the mean — the mean is driven by "
            "outlier buckets, not a persistent spread"
        )
    if not positive_both_halves:
        return (
            "no",
            f"traded direction not positive in both halves (IS {is_signed:+.3f} bps, "
            f"OOS {oos_signed:+.3f} bps)",
        )
    if oos_ci_low_signed <= 0:
        return "no", f"OOS 95% CI [{oos_ci_low_signed:.3f}, ...] includes zero"
    if break_even_days_taker is None or break_even_days_taker > window_days:
        be = "never" if break_even_days_taker is None else f"{break_even_days_taker:.1f}d"
        return (
            "no",
            f"one-time cost ${cost_taker:.4f} needs {be} to amortise at ${gross_per_day:.4f}/day "
            f"(window {window_days:.1f}d) — not recovered in the observed horizon",
        )
    return (
        "candidate",
        f"net ${gross_per_day - cost_taker / max(window_days, 1.0):.4f}/day taker after "
        f"${cost_taker:.4f} one-time cost (break-even {break_even_days_taker:.1f}d), "
        "positive both halves, OOS CI excludes zero",
    )


# ===========================================================================
# Reachability probe — >=5 attempts, 2-3s spacing, payload validation
# ===========================================================================


def _payload_has_funding(obj: object) -> bool:
    """True if the JSON payload contains a funding rate *and* a timestamp somewhere.

    Deliberately structural, not venue-specific: it guards against a sinkhole serving a
    valid-looking but empty/irrelevant body. A real funding payload always carries a
    numeric rate under a key containing ``funding``/``r`` and a timestamp under a key
    containing ``time``/``t``.
    """
    found_rate = False
    found_time = False

    def walk(node: object) -> None:
        nonlocal found_rate, found_time
        if isinstance(node, dict):
            for k, v in node.items():
                kl = str(k).lower()
                if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                    # Gate's funding_rate endpoint uses {"r": rate, "t": epoch_s}; other
                    # venues spell it out. Accept both, but only for a numeric value.
                    if kl == "r" or ("funding" in kl and ("rate" in kl or kl == "fundingrate")):
                        try:
                            float(v)
                            found_rate = True
                        except (TypeError, ValueError):
                            pass
                    if "funding" in kl and "time" in kl:
                        found_time = True
                    if kl in ("t", "time", "ts", "timestamp"):
                        found_time = True
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(obj)
    return found_rate and found_time


@dataclass
class ProbeResult:
    """Per-endpoint reachability across all attempts."""

    name: str
    url: str
    method: str
    attempts: list[dict[str, object]] = field(default_factory=list)
    ok_attempts: int = 0
    valid_attempts: int = 0
    reachable: bool = False
    payload_valid: bool = False

    def summary(self) -> dict[str, object]:
        return {
            "name": self.name,
            "url": self.url,
            "method": self.method,
            "attempts": self.attempts,
            "ok_attempts": self.ok_attempts,
            "valid_attempts": self.valid_attempts,
            "reachable": self.reachable,
            "payload_valid": self.payload_valid,
        }


def probe_endpoint(
    name: str,
    url: str,
    *,
    data: bytes | None = None,
    validate_funding: bool = False,
    attempts: int = PROBE_ATTEMPTS,
    spacing_s: float = PROBE_SPACING_S,
    timeout: float = 20.0,
) -> ProbeResult:
    """Probe one endpoint ``attempts`` times, spaced ``spacing_s`` apart.

    Records status/bytes/latency per attempt. ``reachable`` is True if *any* attempt
    returned HTTP 200; ``payload_valid`` if any 200 body actually contained a funding
    rate + timestamp. Never concludes "blocked" from a single failure.
    """
    result = ProbeResult(name=name, url=url, method="POST" if data else "GET")
    for i in range(attempts):
        if i:
            time.sleep(spacing_s)
        headers = {"User-Agent": UA}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers)
        row: dict[str, object] = {"attempt": i + 1, "status": 0, "bytes": 0, "latency_ms": 0.0}
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                row["status"] = resp.status
                row["bytes"] = len(raw)
                row["latency_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
                if resp.status == 200 and raw:
                    result.ok_attempts += 1
                    if validate_funding:
                        try:
                            parsed = json.loads(raw)
                        except json.JSONDecodeError:
                            parsed = None
                        if parsed is not None and _payload_has_funding(parsed):
                            result.valid_attempts += 1
                            row["payload_valid"] = True
                    else:
                        result.valid_attempts += 1
        except urllib.error.HTTPError as error:
            row["status"] = error.code
            row["latency_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
            row["error"] = f"HTTP {error.code}"
        except Exception as error:  # noqa: BLE001 — refused/reset/timeout: record and continue
            row["latency_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
            row["error"] = f"{type(error).__name__}: {error}"
        result.attempts.append(row)
    result.reachable = result.ok_attempts > 0
    result.payload_valid = result.valid_attempts > 0
    return result


# ===========================================================================
# Venue adapters — enumerate / funding history / order book / min size
# ===========================================================================


def _get(client: mv.HttpClient, url: str, path: Path) -> object | None:
    return client.request(url, cache_path=path)


def _post(client: mv.HttpClient, url: str, body: dict, path: Path) -> object | None:
    return client.request(url, cache_path=path, data=json.dumps(body).encode())


def _f(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def gate_enumerate(client: mv.HttpClient) -> dict[str, dict[str, object]]:
    contracts = _get(client, f"{GATE_BASE}/contracts", client.cache_dir / "gate" / "contracts.json")
    tickers = _get(client, f"{GATE_BASE}/tickers", client.cache_dir / "gate" / "tickers.json")
    assert isinstance(contracts, list) and isinstance(tickers, list)
    vol = {t["contract"]: t for t in tickers}
    out: dict[str, dict[str, object]] = {}
    for c in contracts:
        if (
            c.get("contract_type") not in ("", None)
            or c.get("in_delisting")
            or c.get("is_pre_market")
        ):
            continue
        name = c["name"]
        if not name.endswith("_USDT"):
            continue
        t = vol.get(name, {})
        price = _f(c.get("mark_price")) or _f(c.get("last_price"))
        min_usd = _f(c.get("order_size_min")) * _f(c.get("quanto_multiplier")) * price
        out[name[:-5]] = {
            "symbol": name,
            "interval_hours": _f(c.get("funding_interval"), 28_800.0) / 3600.0,
            "volume_24h_quote_usd": _f(t.get("volume_24h_quote")),
            "mark_price": price or None,
            "min_notional_usd": min_usd,
            "taker_bps_api": _f(c.get("taker_fee_rate")) * 10_000.0,
            "maker_bps_api": _f(c.get("maker_fee_rate")) * 10_000.0,
        }
    return out


def gate_history(client: mv.HttpClient, symbol: str, days: float) -> tuple[list[float], list[int]]:
    now = int(time.time())
    url = (
        f"{GATE_BASE}/funding_rate?contract={urllib.parse.quote(symbol)}"
        f"&from={now - int(days * 86400)}&to={now}&limit=1000"
    )
    rows = _get(client, url, client.cache_dir / "gate" / "funding" / f"{symbol}.json")
    if not isinstance(rows, list):
        return [], []
    rows = sorted(rows, key=lambda r: r["t"])
    return [float(r["r"]) for r in rows], [int(r["t"]) * 1000 for r in rows]


def gate_orderbook(client: mv.HttpClient, symbol: str) -> tuple[float | None, float | None]:
    data = _get(
        client,
        f"{GATE_BASE}/order_book?contract={urllib.parse.quote(symbol)}&limit=100",
        client.cache_dir / "gate" / "book" / f"{symbol}.json",
    )
    return _half_spread_and_depth(data, kind="gate")


def bybit_enumerate(client: mv.HttpClient) -> dict[str, dict[str, object]]:
    inst = _get(
        client,
        f"{BYBIT_BASE}/market/instruments-info?category=linear&limit=1000",
        client.cache_dir / "bybit" / "instruments.json",
    )
    tick = _get(
        client,
        f"{BYBIT_BASE}/market/tickers?category=linear",
        client.cache_dir / "bybit" / "tickers.json",
    )
    assert isinstance(inst, dict) and isinstance(tick, dict)
    vol = {t["symbol"]: t for t in tick["result"]["list"]}
    out: dict[str, dict[str, object]] = {}
    for item in inst["result"]["list"]:
        if item.get("contractType") != "LinearPerpetual" or item.get("quoteCoin") != "USDT":
            continue
        if item.get("status") != "Trading" or item.get("isPreListing"):
            continue
        sym = item["symbol"]
        t = vol.get(sym, {})
        lot = item.get("lotSizeFilter", {})
        price = _f(t.get("lastPrice"))
        min_usd = max(_f(lot.get("minOrderQty")) * price, _f(lot.get("minNotionalValue")))
        out[sym[:-4]] = {
            "symbol": sym,
            "interval_hours": _f(item.get("fundingInterval"), 480.0) / 60.0,
            "volume_24h_quote_usd": _f(t.get("turnover24h")),
            "mark_price": price or None,
            "min_notional_usd": min_usd,
        }
    return out


def bybit_history(client: mv.HttpClient, symbol: str, days: float) -> tuple[list[float], list[int]]:
    end = int(time.time() * 1000)
    floor_ms = end - int(days * 86_400_000)
    rates: list[float] = []
    times: list[int] = []
    page = 0
    while page < 10:
        url = (
            f"{BYBIT_BASE}/market/funding/history?category=linear"
            f"&symbol={urllib.parse.quote(symbol)}&limit=200&endTime={end}"
        )
        body = _get(client, url, client.cache_dir / "bybit" / "funding" / f"{symbol}_{page}.json")
        rows = (body or {}).get("result", {}).get("list", []) if isinstance(body, dict) else []
        if not rows:
            break
        rows.sort(key=lambda r: int(r["fundingRateTimestamp"]))
        for r in rows:
            ts = int(r["fundingRateTimestamp"])
            if ts >= floor_ms:
                rates.append(float(r["fundingRate"]))
                times.append(ts)
        oldest = min(int(r["fundingRateTimestamp"]) for r in rows)
        if oldest <= floor_ms or len(rows) < 200:
            break
        end = oldest - 1
        page += 1
    order = sorted(range(len(times)), key=lambda i: times[i])
    return [rates[i] for i in order], [times[i] for i in order]


def bybit_orderbook(client: mv.HttpClient, symbol: str) -> tuple[float | None, float | None]:
    data = _get(
        client,
        f"{BYBIT_BASE}/market/orderbook?category=linear&symbol={urllib.parse.quote(symbol)}&limit=100",
        client.cache_dir / "bybit" / "book" / f"{symbol}.json",
    )
    if isinstance(data, dict):
        data = data.get("result")
    return _half_spread_and_depth(data, kind="bybit")


def okx_enumerate(client: mv.HttpClient) -> dict[str, dict[str, object]]:
    inst = _get(
        client,
        f"{OKX_BASE}/public/instruments?instType=SWAP",
        client.cache_dir / "okx" / "instruments.json",
    )
    tick = _get(
        client,
        f"{OKX_BASE}/market/tickers?instType=SWAP",
        client.cache_dir / "okx" / "tickers.json",
    )
    assert isinstance(inst, dict) and isinstance(tick, dict)
    vol = {t["instId"]: t for t in tick["data"]}
    out: dict[str, dict[str, object]] = {}
    for item in inst["data"]:
        if item.get("ctType") != "linear" or item.get("settleCcy") != "USDT":
            continue
        if item.get("state") != "live" or not item["instId"].endswith("-USDT-SWAP"):
            continue
        sym = item["instId"]
        t = vol.get(sym, {})
        price = _f(t.get("last"))
        ct_val = _f(item.get("ctVal"))
        min_usd = _f(item.get("minSz")) * ct_val * price
        out[sym.split("-")[0]] = {
            "symbol": sym,
            "interval_hours": 8.0,  # OKX USDT swaps settle on the standard 8h cycle
            "volume_24h_quote_usd": _f(t.get("volCcy24h")) * price,
            "mark_price": price or None,
            "min_notional_usd": min_usd,
        }
    return out


def okx_history(client: mv.HttpClient, symbol: str, days: float) -> tuple[list[float], list[int]]:
    floor_ms = int((time.time() - days * 86_400) * 1000)
    rates: list[float] = []
    times: list[int] = []
    after = None
    page = 0
    while page < 10:
        url = (
            f"{OKX_BASE}/public/funding-rate-history?instId={urllib.parse.quote(symbol)}&limit=100"
        )
        if after is not None:
            url += f"&after={after}"
        body = _get(client, url, client.cache_dir / "okx" / "funding" / f"{symbol}_{page}.json")
        rows = (body or {}).get("data", []) if isinstance(body, dict) else []
        if not rows:
            break
        rows.sort(key=lambda r: int(r["fundingTime"]))
        for r in rows:
            ts = int(r["fundingTime"])
            if ts >= floor_ms:
                rates.append(float(r["fundingRate"]))
                times.append(ts)
        oldest = min(int(r["fundingTime"]) for r in rows)
        if oldest <= floor_ms or len(rows) < 100:
            break
        after = oldest
        page += 1
    order = sorted(range(len(times)), key=lambda i: times[i])
    return [rates[i] for i in order], [times[i] for i in order]


def okx_orderbook(client: mv.HttpClient, symbol: str) -> tuple[float | None, float | None]:
    data = _get(
        client,
        f"{OKX_BASE}/market/books?instId={urllib.parse.quote(symbol)}&sz=100",
        client.cache_dir / "okx" / "book" / f"{symbol}.json",
    )
    rows = (data or {}).get("data", []) if isinstance(data, dict) else []
    return _half_spread_and_depth(rows[0] if rows else None, kind="okx")


def bitget_enumerate(client: mv.HttpClient) -> dict[str, dict[str, object]]:
    inst = _get(
        client,
        f"{BITGET_BASE}/market/contracts?productType=usdt-futures",
        client.cache_dir / "bitget" / "contracts.json",
    )
    tick = _get(
        client,
        f"{BITGET_BASE}/market/tickers?productType=usdt-futures",
        client.cache_dir / "bitget" / "tickers.json",
    )
    assert isinstance(inst, dict) and isinstance(tick, dict)
    vol = {t["symbol"]: t for t in tick["data"]}
    out: dict[str, dict[str, object]] = {}
    for item in inst["data"]:
        if item.get("symbolType") != "perpetual" or item.get("symbolStatus") != "normal":
            continue
        if not item["symbol"].endswith("USDT"):
            continue
        sym = item["symbol"]
        t = vol.get(sym, {})
        price = _f(t.get("lastPr"))
        min_usd = max(_f(item.get("minTradeNum")) * price, _f(item.get("minTradeUSDT")))
        out[sym[:-4]] = {
            "symbol": sym,
            "interval_hours": _f(item.get("fundInterval"), 8.0),
            "volume_24h_quote_usd": _f(t.get("quoteVolume")),
            "mark_price": price or None,
            "min_notional_usd": min_usd,
            "taker_bps_api": _f(item.get("takerFeeRate")) * 10_000.0,
            "maker_bps_api": _f(item.get("makerFeeRate")) * 10_000.0,
        }
    return out


def bitget_history(
    client: mv.HttpClient, symbol: str, days: float
) -> tuple[list[float], list[int]]:
    floor_ms = int((time.time() - days * 86_400) * 1000)
    rates: list[float] = []
    times: list[int] = []
    page = 1
    while page <= 10:
        url = (
            f"{BITGET_BASE}/market/history-fund-rate?symbol={urllib.parse.quote(symbol)}"
            f"&productType=usdt-futures&pageSize=100&pageNo={page}"
        )
        body = _get(client, url, client.cache_dir / "bitget" / "funding" / f"{symbol}_{page}.json")
        rows = (body or {}).get("data", []) if isinstance(body, dict) else []
        if not rows:
            break
        rows.sort(key=lambda r: int(r["fundingTime"]))
        for r in rows:
            ts = int(r["fundingTime"])
            if ts >= floor_ms:
                rates.append(float(r["fundingRate"]))
                times.append(ts)
        oldest = min(int(r["fundingTime"]) for r in rows)
        if oldest <= floor_ms or len(rows) < 100:
            break
        page += 1
    order = sorted(range(len(times)), key=lambda i: times[i])
    return [rates[i] for i in order], [times[i] for i in order]


def bitget_orderbook(client: mv.HttpClient, symbol: str) -> tuple[float | None, float | None]:
    data = _get(
        client,
        f"{BITGET_BASE}/market/merge-depth?symbol={urllib.parse.quote(symbol)}"
        "&productType=usdt-futures&limit=100",
        client.cache_dir / "bitget" / "book" / f"{symbol}.json",
    )
    if isinstance(data, dict):
        data = data.get("data")
    return _half_spread_and_depth(data, kind="bitget")


def hl_enumerate(client: mv.HttpClient) -> dict[str, dict[str, object]]:
    data = _post(
        client, HL_INFO, {"type": "metaAndAssetCtxs"},
        client.cache_dir / "hyperliquid" / "meta.json",
    )
    assert isinstance(data, list) and len(data) == 2
    universe, ctxs = data[0]["universe"], data[1]
    out: dict[str, dict[str, object]] = {}
    for meta, ctx in zip(universe, ctxs, strict=False):
        if meta.get("isDelisted"):
            continue
        out[meta["name"]] = {
            "symbol": meta["name"],
            "interval_hours": 1.0,
            "volume_24h_quote_usd": _f(ctx.get("dayNtlVlm")),
            "mark_price": _f(ctx.get("markPx")) or None,
            "min_notional_usd": HL_MIN_ORDER_USD,
        }
    return out


def hl_history(client: mv.HttpClient, symbol: str, days: float) -> tuple[list[float], list[int]]:
    start = int((time.time() - days * 86_400) * 1000)
    rates: list[float] = []
    times: list[int] = []
    page = 0
    while page <= 6:
        body = _post(
            client,
            HL_INFO,
            {"type": "fundingHistory", "coin": symbol, "startTime": start},
            client.cache_dir / "hyperliquid" / "funding" / f"{symbol}_{page}.json",
        )
        if not isinstance(body, list) or not body:
            break
        body.sort(key=lambda r: int(r["time"]))
        rates.extend(float(r["fundingRate"]) for r in body)
        times.extend(int(r["time"]) for r in body)
        if len(body) < 500:
            break
        start = int(body[-1]["time"]) + 1
        page += 1
    return rates, times


def hl_orderbook(client: mv.HttpClient, symbol: str) -> tuple[float | None, float | None]:
    data = _post(
        client,
        HL_INFO,
        {"type": "l2Book", "coin": symbol},
        client.cache_dir / "hyperliquid" / "book" / f"{symbol}.json",
    )
    return _half_spread_and_depth(data, kind="hl")


def _half_spread_and_depth(data: object, *, kind: str) -> tuple[float | None, float | None]:
    """Return ``(half_spread_bps, depth_usd_within_50bps)`` from a venue order book.

    Handles the four book shapes (Gate/Bybit/Bitget list-of-{p,s}, OKX list-of-[px,sz],
    HL levels [[{px,sz}]]). ``None`` when the book is absent or malformed — never a fake
    zero, which would understate cost.
    """
    if not isinstance(data, dict):
        return None, None
    try:
        if kind == "hl":
            levels = data.get("levels") or []
            bids = [(float(x["px"]), float(x["sz"])) for x in levels[0]]
            asks = [(float(x["px"]), float(x["sz"])) for x in levels[1]]
        elif kind == "bybit":
            # Bybit v5 orderbook uses ``b``/``a`` with [price, size] string pairs.
            bids = [(float(x[0]), float(x[1])) for x in data.get("b", [])]
            asks = [(float(x[0]), float(x[1])) for x in data.get("a", [])]
        elif kind == "gate":
            bids = [(float(x["p"]), float(x["s"])) for x in data.get("bids", [])]
            asks = [(float(x["p"]), float(x["s"])) for x in data.get("asks", [])]
        else:  # okx and bitget: [[price, size], ...]
            bids = [(float(x[0]), float(x[1])) for x in data.get("bids", [])]
            asks = [(float(x[0]), float(x[1])) for x in data.get("asks", [])]
    except (KeyError, IndexError, TypeError, ValueError):
        return None, None
    if not bids or not asks:
        return None, None
    bid, ask = max(p for p, _ in bids), min(p for p, _ in asks)
    if bid <= 0 or ask <= 0:
        return None, None
    half_spread_bps = (ask - bid) / ((ask + bid) / 2.0) * 10_000.0 / 2.0
    limit = bid * 1.005  # within ~50 bps of the touch
    depth = sum(sz * p for p, sz in bids if p >= bid * 0.995) + sum(
        sz * p for p, sz in asks if p <= limit
    )
    return round(half_spread_bps, 4), round(depth, 2)


VENUES: dict[str, dict[str, object]] = {
    "gate": {"enumerate": gate_enumerate, "history": gate_history, "book": gate_orderbook,
             "enum_url": f"{GATE_BASE}/contracts",
             "fund_url": f"{GATE_BASE}/funding_rate?contract=BTC_USDT&limit=10"},
    "bybit": {"enumerate": bybit_enumerate, "history": bybit_history, "book": bybit_orderbook,
              "enum_url": f"{BYBIT_BASE}/market/instruments-info?category=linear&limit=1000",
              "fund_url": (
                  f"{BYBIT_BASE}/market/funding/history?category=linear&symbol=BTCUSDT&limit=10"
              )},
    "okx": {"enumerate": okx_enumerate, "history": okx_history, "book": okx_orderbook,
            "enum_url": f"{OKX_BASE}/public/instruments?instType=SWAP",
            "fund_url": f"{OKX_BASE}/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=10"},
    "bitget": {"enumerate": bitget_enumerate, "history": bitget_history, "book": bitget_orderbook,
               "enum_url": f"{BITGET_BASE}/market/contracts?productType=usdt-futures",
               "fund_url": (
                   f"{BITGET_BASE}/market/history-fund-rate?symbol=BTCUSDT"
                   "&productType=usdt-futures&pageSize=10&pageNo=1"
               )},
    "hyperliquid": {"enumerate": hl_enumerate, "history": hl_history, "book": hl_orderbook,
                    "enum_url": HL_INFO, "fund_url": HL_INFO},
}


def run_probes(*, attempts: int, spacing: float) -> list[dict[str, object]]:
    """Probe each venue's enumerate + funding endpoint ``attempts`` times, spaced."""
    out: list[dict[str, object]] = []
    for venue, adapter in VENUES.items():
        enum_data = b'{"type":"metaAndAssetCtxs"}' if venue == "hyperliquid" else None
        out.append(
            probe_endpoint(
                f"{venue}:enumerate",
                str(adapter["enum_url"]),
                data=enum_data,
                validate_funding=False,
                attempts=attempts,
                spacing_s=spacing,
            ).summary()
        )
        fund_data = (
            json.dumps(
                {"type": "fundingHistory", "coin": "BTC",
                 "startTime": int(time.time() * 1000) - 86_400_000}
            ).encode()
            if venue == "hyperliquid"
            else None
        )
        out.append(
            probe_endpoint(
                f"{venue}:funding",
                str(adapter["fund_url"]),
                data=fund_data,
                validate_funding=True,
                attempts=attempts,
                spacing_s=spacing,
            ).summary()
        )
    return out


# ===========================================================================
# Driver
# ===========================================================================


def collect_venue(
    client: mv.HttpClient,
    venue: str,
    *,
    days: float,
    min_volume_usd: float,
    max_symbols: int | None,
    need_history: set[str],
) -> dict[str, dict[str, object]]:
    """Enumerate a venue and pull history + book for the bases in ``need_history``."""
    adapter = VENUES[venue]
    meta = adapter["enumerate"](client)  # type: ignore[operator]
    bases = [b for b in meta if b in need_history]
    bases.sort(key=lambda b: meta[b]["volume_24h_quote_usd"], reverse=True)  # type: ignore[index]
    if max_symbols:
        bases = bases[:max_symbols]
    lock = threading.Lock()
    out: dict[str, dict[str, object]] = {}
    stats = {"history_ok": 0, "history_empty": 0, "book_ok": 0, "errors": 0}

    def work(base: str) -> None:
        m = meta[base]  # type: ignore[index]
        sym = str(m["symbol"])
        try:
            rates, times = adapter["history"](client, sym, days)  # type: ignore[operator]
            if not rates:
                with lock:
                    stats["history_empty"] += 1
                return
            hs, depth = adapter["book"](client, sym)  # type: ignore[operator]
            with lock:
                stats["history_ok"] += 1
                if hs is not None:
                    stats["book_ok"] += 1
                out[base] = {
                    "symbol": sym,
                    "interval_hours": m["interval_hours"],
                    "volume_24h_quote_usd": m["volume_24h_quote_usd"],
                    "mark_price": m["mark_price"],
                    "min_notional_usd": m["min_notional_usd"],
                    "rates": rates,
                    "times_ms": times,
                    "half_spread_bps": hs,
                    "depth_usd": depth,
                }
        except Exception as error:  # noqa: BLE001 — one symbol must not sink the venue
            print(f"  ! {venue}/{base}: {type(error).__name__}: {error}", file=sys.stderr)
            with lock:
                stats["errors"] += 1

    with ThreadPoolExecutor(max_workers=NETWORK_CONCURRENCY) as pool:
        list(pool.map(work, bases))
    print(f"[{venue}] {len(meta)} perps, {len(bases)} candidates -> {stats}", file=sys.stderr)
    return out


def build_pairs(
    venue_data: dict[str, dict[str, dict[str, object]]], *, notional: float, block: int, n_boot: int
) -> list[PairResult]:
    """Cross every base listed on >=2 venues, every venue pair, into PairResults."""
    from itertools import combinations

    base_venues: dict[str, list[str]] = {}
    for venue, rows in venue_data.items():
        for base in rows:
            base_venues.setdefault(base, []).append(venue)

    results: list[PairResult] = []
    for base, venues in sorted(base_venues.items()):
        if len(venues) < 2:
            continue
        for va, vb in combinations(sorted(venues), 2):
            ra, rb = venue_data[va][base], venue_data[vb][base]
            a_buckets = bucket_funding(ra["times_ms"], ra["rates"])  # type: ignore[arg-type]
            b_buckets = bucket_funding(rb["times_ms"], rb["rates"])  # type: ignore[arg-type]
            if len(set(a_buckets) & set(b_buckets)) < 10:
                continue  # too few shared buckets to say anything
            try:
                row = evaluate_pair(
                    base=base,
                    venue_a=va,
                    symbol_a=str(ra["symbol"]),
                    venue_b=vb,
                    symbol_b=str(rb["symbol"]),
                    a_buckets=a_buckets,
                    b_buckets=b_buckets,
                    notional=notional,
                    block=block,
                    n_boot=n_boot,
                    min_notional_a_usd=ra.get("min_notional_usd"),  # type: ignore[arg-type]
                    min_notional_b_usd=rb.get("min_notional_usd"),  # type: ignore[arg-type]
                    depth_a_usd=ra.get("depth_usd"),  # type: ignore[arg-type]
                    depth_b_usd=rb.get("depth_usd"),  # type: ignore[arg-type]
                    half_spread_a_bps=ra.get("half_spread_bps"),  # type: ignore[arg-type]
                    half_spread_b_bps=rb.get("half_spread_bps"),  # type: ignore[arg-type]
                )
            except Exception as error:  # noqa: BLE001 — one pair must not sink the run
                print(f"  ! {base} {va}/{vb}: {type(error).__name__}: {error}", file=sys.stderr)
                continue
            results.append(row)
    return results


def aggregate_api_fees(
    metas: dict[str, dict[str, dict[str, object]]], published: dict[str, dict[str, object]]
) -> dict[str, dict[str, object]]:
    """Fold any API-exposed fee rate into the published-fee note, marking it verified.

    Gate (``taker_fee_rate``/``maker_fee_rate``) and Bitget (``takerFeeRate``/
    ``makerFeeRate``) publish the account-tier fee in their instrument payloads, so those
    two become **verified by API**; Bybit/OKX/HL expose no unauthenticated fee field and
    stay UNVERIFIED (public-docs values only).
    """
    out: dict[str, dict[str, object]] = {v: dict(n) for v, n in published.items()}
    for venue, rows in metas.items():
        takers = [r["taker_bps_api"] for r in rows.values() if r.get("taker_bps_api")]  # type: ignore[union-attr]
        makers = [r["maker_bps_api"] for r in rows.values() if r.get("maker_bps_api") is not None]  # type: ignore[union-attr]
        if venue in out and takers:
            # Modal value across contracts — a stray tier on one symbol must not move it.
            out[venue]["api_taker_bps_mode"] = statistics.mode(takers)
            out[venue]["api_maker_bps_mode"] = statistics.mode(makers) if makers else None
            out[venue]["verified"] = True
            out[venue]["source"] = "venue instrument payload (taker_fee_rate / takerFeeRate)"
    return out


def render_table(results: list[PairResult], limit: int = 25) -> str:
    header = (
        "| # | base | A>B | dir (long/short) | n | mean bps/8h | +share | streak d | flip "
        "| gross $/day | cost $ | net $/day | IS bps | OOS bps | OOS CI "
        "| min $ a/b | cap $ | verdict |"
    )
    lines = [header, "|" + "---|" * 18]
    for i, r in enumerate(results[:limit], 1):
        mins = f"{_s(r.min_notional_a_usd)}/{_s(r.min_notional_b_usd)}"
        lines.append(
            f"| {i} | {r.base} | {r.venue_a}>{r.venue_b} | {r.long_venue}/{r.short_venue} "
            f"| {r.n_buckets} | {r.mean_spread_bps_8h:.3f} | {r.positive_share:.0%} "
            f"| {r.longest_streak_days:.1f} | {r.sign_flip_rate:.2f} "
            f"| {r.gross_dollars_per_day:.4f} | {r.cost_taker_usd:.4f} "
            f"| {r.net_dollars_per_day_taker:.4f} | {r.is_signed_bps:.3f} | {r.oos_signed_bps:.3f} "
            f"| [{r.oos_ci_low:.3f},{r.oos_ci_high:.3f}] | {mins} | {_s(r.capacity_usd)} "
            f"| {r.verdict} |"
        )
    return "\n".join(lines)


def _s(v: float | None) -> str:
    return "—" if v is None else f"{v:,.0f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venues", default="gate,bybit,okx,bitget,hyperliquid")
    parser.add_argument("--days", type=float, default=TARGET_WINDOW_DAYS)
    parser.add_argument("--notional", type=float, default=100.0)
    parser.add_argument("--min-volume-usd", type=float, default=MIN_QUOTE_VOLUME_USD)
    parser.add_argument("--max-symbols", type=int, default=None)
    parser.add_argument("--block", type=int, default=5)
    parser.add_argument("--n-boot", type=int, default=2000)
    parser.add_argument("--probe-attempts", type=int, default=PROBE_ATTEMPTS)
    parser.add_argument("--probe-spacing", type=float, default=PROBE_SPACING_S)
    parser.add_argument("--offline", action="store_true", help="skip probes; reuse cache")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    cache_dir = args.out / "raw"
    cache_dir.mkdir(parents=True, exist_ok=True)
    client = mv.HttpClient(cache_dir)

    probes: list[dict[str, object]] = []
    if not args.offline:
        print(
            f"probing endpoints x{args.probe_attempts} @ {args.probe_spacing}s spacing",
            file=sys.stderr,
        )
        probes = run_probes(attempts=args.probe_attempts, spacing=args.probe_spacing)
        (args.out / "reachability.json").write_text(json.dumps(probes, indent=2))
    else:
        # Reuse the probe table from the live run so the offline payload is not lossy.
        cached = args.out / "reachability.json"
        if cached.exists():
            probes = json.loads(cached.read_text())

    venues = [v.strip() for v in args.venues.split(",") if v.strip()]

    # Pass 1: enumerate every venue to find bases listed on >=2 venues.
    metas: dict[str, dict[str, dict[str, object]]] = {}
    for venue in venues:
        try:
            metas[venue] = VENUES[venue]["enumerate"](client)  # type: ignore[operator]
        except Exception as error:  # noqa: BLE001 — a dead venue must not sink the run
            print(f"! {venue} enumerate failed: {type(error).__name__}: {error}", file=sys.stderr)
            metas[venue] = {}

    listing: dict[str, list[str]] = {}
    for venue, meta in metas.items():
        for base, row in meta.items():
            if float(row["volume_24h_quote_usd"]) >= args.min_volume_usd:  # type: ignore[arg-type]
                listing.setdefault(base, []).append(venue)
    need_history = {b for b, vs in listing.items() if len(vs) >= 2}
    print(
        f"enumerated { {v: len(m) for v, m in metas.items()} }; "
        f"{len(need_history)} liquid bases on >=2 venues",
        file=sys.stderr,
    )

    # Pass 2: history + book for those bases, per venue.
    venue_data: dict[str, dict[str, dict[str, object]]] = {}
    for venue in venues:
        if not metas.get(venue):
            venue_data[venue] = {}
            continue
        venue_data[venue] = collect_venue(
            client,
            venue,
            days=args.days,
            min_volume_usd=args.min_volume_usd,
            max_symbols=args.max_symbols,
            need_history=need_history,
        )

    results = build_pairs(venue_data, notional=args.notional, block=args.block, n_boot=args.n_boot)
    order = {"candidate": 0, "no": 1, "unconstructible": 2}
    results.sort(
        key=lambda r: (order.get(r.verdict, 9), -r.gross_dollars_per_day, -r.abs_mean_spread_bps_8h)
    )

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "notional_usd": args.notional,
        "leg_usd": args.notional / 2.0,
        "window_days_target": args.days,
        "bucket_hours": BUCKET_HOURS,
        "bootstrap": {"block_buckets": args.block, "n_boot": args.n_boot, "seed": 20261003},
        "fee_model": {
            "source": "crypto_brain.engine.fees.FeeSchedule (DESIGN.md §10.2 base tier)",
            "futures_taker_bps": PROJECT_FEES.futures_taker_bps,
            "futures_maker_bps": PROJECT_FEES.futures_maker_bps,
            "note": (
                "4 fee events (2 venues x in/out); venue published fees recorded "
                "but UNVERIFIED"
            ),
        },
        "venue_fee_notes": aggregate_api_fees(metas, VENUE_FEE_NOTE),
        "probe_discipline": {
            "attempts_per_endpoint": args.probe_attempts,
            "spacing_s": args.probe_spacing,
            "note": (
                "reachability is intermittent for bybit/okx/bitget; "
                "never concluded from one attempt"
            ),
        },
        "probes": probes,
        "coverage": {
            venue: {
                "enumerated": len(metas.get(venue, {})),
                "measured_with_history": len(venue_data.get(venue, {})),
            }
            for venue in venues
        },
        "http_stats": client.stats,
        "n_pairs": len(results),
        "n_candidates": sum(1 for r in results if r.verdict == "candidate"),
        "n_constructible": sum(1 for r in results if r.constructible),
        "pairs": [asdict(r) for r in results],
    }
    out_json = args.out / "spread_ranking.json"
    out_json.write_text(json.dumps(payload, indent=2))

    print(render_table(results))
    print(
        f"\npairs {len(results)} · candidates {payload['n_candidates']} · "
        f"constructible {payload['n_constructible']} · http {client.stats}"
    )
    print(f"wrote {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
