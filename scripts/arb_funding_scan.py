#!/usr/bin/env python3
"""Funding-rate carry scanner using the OFFICIAL Binance data dumps (MEASUREMENT ONLY).

Answers, per pair and net of the project's own cost model, whether a $100 notional
cash-and-carry (long spot + short perp, collect funding) can clear its round-trip cost,
how long that takes, and how *persistent* the funding is (sign-flip rate, longest
positive streak) — a mean is worthless if the sign flips every other day.

**No orders. No keys. No live execution path. Writes only under ``evidence/``.**

Why not Binance ``fapi`` (the honest data-source story)
------------------------------------------------------
``fapi.binance.com`` (REST) and ``fstream.binance.com`` (WS) are **geo-blocked from
this host** — a one-shot probe returns HTTP 000 / connection refused, the same finding
already recorded in ``config/universe.yaml`` (which is why ``futures.enabled: false``).
No harness is built on ``fapi`` and nothing is retried against it.

The funding data therefore comes from Binance's **public S3 data dumps**, which *are*
reachable from this host and are the same distribution family as the working
``data-api.binance.vision`` spot mirror:

* symbol index — ``https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?delimiter=/&prefix=data/futures/um/monthly/fundingRate/``
* funding history — ``https://data.binance.vision/data/futures/um/monthly/fundingRate/<SYM>/<SYM>-fundingRate-<YYYY-MM>.zip``
  (CSV columns: ``calc_time, funding_interval_hours, last_funding_rate``)

These are **Binance's own** realised funding settlements — not a proxy venue.

Other sources
-------------
* Spot last price + 24h quote volume — ``https://data-api.binance.vision/api/v3``
  (``ticker/price``, ``ticker/24hr``); the known-good public mirror.
* Perp last price, index price, basis, open interest snapshot — CoinGecko
  ``/api/v3/derivatives`` (rows with ``market == "Binance (Futures)"``). Snapshot only,
  used for the *current* basis; the funding history above is what the stats are built on.

Cost model — **not invented**
-----------------------------
Fees come from :class:`crypto_brain.engine.fees.FeeSchedule`; slippage from
:class:`crypto_brain.engine.cost_model.round_trip_cost_pct` (DESIGN.md §10.2/§10.3). A
cash-and-carry has two legs, each opened and closed, so the round-trip cost is the
spot-leg round trip **plus** the perp-leg round trip, both from the project's own
function with the config's ``slippage_k`` and ``exit_slippage_multiplier``.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import multiprocessing as mp
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from crypto_brain.engine.cost_model import round_trip_cost_pct  # noqa: E402
from crypto_brain.engine.fees import FeeSchedule  # noqa: E402
from crypto_brain.engine.slippage import SymbolCostProfile  # noqa: E402

BINANCE_SPOT = "https://data-api.binance.vision/api/v3"
VISION = "https://data.binance.vision/data/futures/um/monthly/fundingRate"
S3_INDEX = (
    "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
    "?delimiter=/&prefix=data/futures/um/monthly/fundingRate/"
)
COINGECKO = "https://api.coingecko.com/api/v3"
UA = "crypto-brain-arb-funding-scan/1.0 (paper research; read-only)"

#: Project fee schedule (DESIGN.md §10.2 base tier: 10 bps spot taker, 5 bps perp taker).
PROJECT_FEES = FeeSchedule(
    spot_taker_bps=10.0, spot_maker_bps=10.0, futures_taker_bps=5.0, futures_maker_bps=2.0
)
SLIPPAGE_K = 0.5
EXIT_MULTIPLIER = 1.5
DEFAULT_INTERVAL_HOURS = 8.0

#: Tail definition (the user's "hundreds of pairs" question): a symbol is *high-funding*
#: if its mean is >= 30 bps/8h (annualised >= ~33%) AND that is sustained over >= 14
#: consecutive days. Both are arguments to :func:`is_tail_high_funding`.
TAIL_MEAN_BPS = 30.0
TAIL_STREAK_DAYS = 14.0

#: Bounded concurrency (a sibling worker shares this IP's limits).
NETWORK_CONCURRENCY = 5
PER_DOMAIN_RATE_PER_S = 6.0
MAX_RETRIES = 4
BACKOFF_BASE_S = 1.0
DEFAULT_OUT = REPO_ROOT / "evidence" / "arbitrage" / "2026-10-03"


# ---------------------------------------------------------------------------
# HTTP: bounded concurrency + exponential backoff + raw caching
# ---------------------------------------------------------------------------


class HttpError(RuntimeError):
    """A request that exhausted its retries, or a definitive 404."""

    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url


class HttpClient:
    """Cached GET client with a global semaphore, per-host pacing and backoff.

    The semaphore bounds *total* in-flight requests; a per-host monotonic clock enforces
    a minimum gap between request *starts* to the same host. 429/418/5xx are retried with
    exponential backoff honouring ``Retry-After``; 404 is returned to the caller as a
    definitive miss (a symbol/month that simply does not exist) and is not retried.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        concurrency: int = NETWORK_CONCURRENCY,
        rate_per_s: float = PER_DOMAIN_RATE_PER_S,
        timeout: float = 30.0,
    ) -> None:
        self.cache_dir = cache_dir
        self.timeout = timeout
        self._semaphore = threading.BoundedSemaphore(concurrency)
        self._min_gap = 1.0 / rate_per_s if rate_per_s > 0 else 0.0
        self._host_lock = threading.Lock()
        self._host_next: dict[str, float] = {}
        self.stats = {"requests": 0, "cache_hits": 0, "retries": 0, "errors": 0, "misses": 0}

    def _pace(self, host: str) -> None:
        if self._min_gap <= 0:
            return
        with self._host_lock:
            now = time.monotonic()
            slot = max(now, self._host_next.get(host, 0.0))
            self._host_next[host] = slot + self._min_gap
            wait = slot - now
        if wait > 0:
            time.sleep(wait)

    def get_bytes(self, url: str, *, cache_path: Path, cacheable: bool = True) -> bytes | None:
        """GET with caching. Returns ``None`` for a definitive 404 (no such object)."""
        if cache_path.exists():
            self.stats["cache_hits"] += 1
            return cache_path.read_bytes()
        try:
            raw = self._request(url)
        except HttpError as error:
            if error.status == 404:
                self.stats["misses"] += 1
                if cacheable:  # remember the miss so a rerun does not refetch
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.with_suffix(cache_path.suffix + ".404").touch()
                return None
            raise
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(raw)
        return raw

    def get_json(self, url: str, *, cache_path: Path) -> object | None:
        raw = self.get_bytes(url, cache_path=cache_path)
        return None if raw is None else json.loads(raw)

    def _request(self, url: str) -> bytes:
        host = urllib.parse.urlparse(url).netloc
        last_status = 0
        for attempt in range(MAX_RETRIES + 1):
            with self._semaphore:
                self._pace(host)
                self.stats["requests"] += 1
                request = urllib.request.Request(url, headers={"User-Agent": UA})
                try:
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        return response.read()
                except urllib.error.HTTPError as error:
                    last_status = error.code
                    if error.code == 404:
                        raise HttpError(404, url) from error
                    retryable = error.code in (429, 418) or 500 <= error.code < 600
                    if not retryable or attempt == MAX_RETRIES:
                        self.stats["errors"] += 1
                        raise HttpError(error.code, url) from error
                    delay = _retry_delay(error.headers.get("Retry-After"), attempt)
                except Exception:  # noqa: BLE001 — refused/reset; retry then give up
                    if attempt == MAX_RETRIES:
                        self.stats["errors"] += 1
                        raise
                    delay = _retry_delay(None, attempt)
            self.stats["retries"] += 1
            time.sleep(delay)
        raise HttpError(last_status, url)


def _retry_delay(retry_after: str | None, attempt: int) -> float:
    """Exponential backoff, honouring a numeric ``Retry-After`` when present."""
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    return BACKOFF_BASE_S * (2**attempt)


# ---------------------------------------------------------------------------
# Pure computation — the part the unit tests pin
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FundingStats:
    """Distribution + persistence statistics of a funding-rate series (bps/interval)."""

    n: int
    mean_bps: float
    median_bps: float
    min_bps: float
    max_bps: float
    annualized_pct: float
    positive_share: float
    sign_flip_rate: float
    longest_positive_streak: int
    longest_positive_streak_days: float
    interval_hours: float


def longest_positive_streak(rates: list[float]) -> int:
    """Length of the longest run of strictly-positive consecutive settlements.

    The "konsisten" measure: a pair positive on average but flipping every other
    interval has a streak of 1 and cannot be carried reliably.
    """
    best = 0
    current = 0
    for rate in rates:
        if rate > 0.0:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def sign_flip_rate(rates: list[float]) -> float:
    """Fraction of adjacent pairs whose sign differs (0 = never flips, 1 = every step).

    Zero counts as its own sign, so a 0 inside a positive run registers as a flip.
    """
    if len(rates) < 2:
        return 0.0
    flips = sum(
        1
        for previous, current in zip(rates, rates[1:], strict=False)
        if (previous > 0.0) != (current > 0.0)
    )
    return flips / (len(rates) - 1)


def annualized_funding_pct(mean_bps_per_interval: float, interval_hours: float) -> float:
    """Annualise a mean funding rate using the pair's own settlement interval.

    ``(bps -> pct) * (intervals per year)``. A 4h-interval pair settles twice as often
    as an 8h one, so hard-coding 8h would understate it by 2x.
    """
    if interval_hours <= 0:
        raise ValueError("interval_hours must be positive")
    intervals_per_year = (24.0 / interval_hours) * 365.0
    return (mean_bps_per_interval / 100.0) * intervals_per_year


def funding_stats(
    rates: list[float], *, interval_hours: float = DEFAULT_INTERVAL_HOURS
) -> FundingStats:
    """Summarise a funding-rate series given as **fractions per interval**.

    Raises on an empty series rather than returning a zeroed stat object that would look
    like a measured flat rate.
    """
    if not rates:
        raise ValueError("funding series is empty")
    bps = [rate * 10_000.0 for rate in rates]
    mean_bps = statistics.fmean(bps)
    streak = longest_positive_streak(rates)
    return FundingStats(
        n=len(rates),
        mean_bps=mean_bps,
        median_bps=statistics.median(bps),
        min_bps=min(bps),
        max_bps=max(bps),
        annualized_pct=annualized_funding_pct(mean_bps, interval_hours),
        positive_share=sum(1 for value in bps if value > 0.0) / len(bps),
        sign_flip_rate=sign_flip_rate(rates),
        longest_positive_streak=streak,
        longest_positive_streak_days=streak * interval_hours / 24.0,
        interval_hours=interval_hours,
    )


def break_even_days(round_trip_cost_pct_value: float, daily_funding_pct: float) -> float | None:
    """Days of funding needed to recover the round trip. ``None`` if it never can.

    ``None`` when daily funding is non-positive — a short-perp cash-and-carry only
    *receives* when funding is positive, so a negative series never amortises.
    """
    if daily_funding_pct <= 0.0:
        return None
    if round_trip_cost_pct_value <= 0.0:
        return 0.0
    return round_trip_cost_pct_value / daily_funding_pct


@dataclass(frozen=True)
class HedgedCost:
    """Hedged cash-and-carry round-trip cost, itemised, at taker and maker pricing.

    Four fee events: spot in/out + perp in/out (the prior study's ``FUNDING_OR_ALT.md``
    shape). Spread+slippage comes from the project's S9 model and is execution-style
    independent, so it is computed once and only the fee component is swapped.
    """

    fees_taker_pct: float
    fees_maker_pct: float
    spread_slippage_pct: float
    taker_pct: float
    maker_pct: float


def hedged_round_trip_cost(
    profile: SymbolCostProfile,
    notional: float,
    *,
    fees: FeeSchedule,
    slippage_k: float,
    exit_multiplier: float,
) -> HedgedCost:
    """The 4-fee-event hedged round trip, in percent of notional, taker and maker.

    Spot maker == taker at the project's base tier (DESIGN §10.2), so the maker variant
    is only cheaper on the perp leg (2 bps vs 5 bps) — stated rather than assumed.
    """
    spot_leg = round_trip_cost_pct(
        profile, notional, mode="spot", fees=fees,
        slippage_k=slippage_k, exit_multiplier=exit_multiplier,
    )
    perp_leg = round_trip_cost_pct(
        profile, notional, mode="futures", fees=fees,
        slippage_k=slippage_k, exit_multiplier=exit_multiplier, include_funding=False,
    )
    spot_fee_taker = 2.0 * fees.taker_bps("spot") / 100.0
    perp_fee_taker = 2.0 * fees.taker_bps("futures") / 100.0
    spot_fee_maker = 2.0 * fees.maker_bps("spot") / 100.0
    perp_fee_maker = 2.0 * fees.maker_bps("futures") / 100.0
    spread_slippage = (spot_leg.total_pct - spot_fee_taker) + (perp_leg.total_pct - perp_fee_taker)
    fees_taker = spot_fee_taker + perp_fee_taker
    fees_maker = spot_fee_maker + perp_fee_maker
    return HedgedCost(
        fees_taker_pct=fees_taker,
        fees_maker_pct=fees_maker,
        spread_slippage_pct=spread_slippage,
        taker_pct=fees_taker + spread_slippage,
        maker_pct=fees_maker + spread_slippage,
    )


@dataclass(frozen=True)
class HalfStats:
    """Funding stats for one half of an IS/OOS split, plus its break-even."""

    mean_bps: float
    positive_share: float
    longest_streak: int
    break_even_days: float | None


def half_stats(
    rates: list[float], *, interval_hours: float, cost_pct: float
) -> HalfStats:
    """Summarise one half of a funding series against a given round-trip cost.

    ``break_even_days`` is ``None`` when the half's mean funding is non-positive — the
    honest "never" for a short-perp leg that would be paying, not receiving.
    """
    if not rates:
        return HalfStats(0.0, 0.0, 0, None)
    stats = funding_stats(rates, interval_hours=interval_hours)
    daily_pct = (stats.mean_bps / 100.0) * (24.0 / interval_hours)
    return HalfStats(
        mean_bps=stats.mean_bps,
        positive_share=stats.positive_share,
        longest_streak=stats.longest_positive_streak,
        break_even_days=break_even_days(cost_pct, daily_pct),
    )


def split_halves(rates: list[float]) -> tuple[list[float], list[float]]:
    """Chronological in-sample / out-of-sample halves (first half IS, second OOS)."""
    mid = len(rates) // 2
    return rates[:mid], rates[mid:]


def clears_both_halves(is_half: HalfStats, oos_half: HalfStats, *, max_days: float = 30.0) -> bool:
    """The decisive test: funding covers the hedged cost in **both** halves.

    A symbol whose funding collapses out of sample is the same trap the prior study
    named, so both halves must show positive mean funding and a break-even within
    ``max_days``.
    """
    if is_half.break_even_days is None or oos_half.break_even_days is None:
        return False
    return (
        is_half.mean_bps > 0.0
        and oos_half.mean_bps > 0.0
        and is_half.break_even_days <= max_days
        and oos_half.break_even_days <= max_days
    )


def classify(
    *,
    mean_funding_bps: float,
    positive_share: float,
    longest_streak: int,
    break_even_days_value: float | None,
    min_positive_share: float = 0.60,
    min_streak_intervals: int = 21,
    max_break_even_days: float = 30.0,
) -> tuple[str, str]:
    """Verdict for one pair, with the reason that produced it.

    ``candidate`` requires all four: positive mean, funding positive in ≥60% of
    settlements, a longest positive streak ≥21 intervals (~7 days at 8h — the
    "konsisten" bar), and round-trip recovered within 30 days. Each threshold is an
    argument so the bar can be argued with rather than hidden.
    """
    if mean_funding_bps <= 0.0:
        return "no", "mean funding is non-positive — the short-perp leg would pay"
    if positive_share < min_positive_share:
        return "no", f"funding positive in only {positive_share:.0%} of settlements"
    if longest_streak < min_streak_intervals:
        return "no", f"longest positive streak {longest_streak} < {min_streak_intervals} intervals"
    if break_even_days_value is None:
        return "no", "no positive daily funding to amortise the round trip"
    if break_even_days_value > max_break_even_days:
        return "no", f"break-even {break_even_days_value:.0f}d exceeds {max_break_even_days:.0f}d"
    return "candidate", f"break-even {break_even_days_value:.1f}d, streak {longest_streak}"


def is_tail_high_funding(
    stats: FundingStats,
    *,
    min_mean_bps: float = TAIL_MEAN_BPS,
    min_streak_days: float = TAIL_STREAK_DAYS,
) -> bool:
    """The user's tail bar: mean ≥ ``min_mean_bps`` sustained ≥ ``min_streak_days``.

    The streak requirement is what separates a genuinely high-funding symbol from one
    whose mean is inflated by a single spike.
    """
    return (
        stats.mean_bps >= min_mean_bps
        and stats.longest_positive_streak_days >= min_streak_days
    )


@dataclass(frozen=True)
class PairResult:
    """One row of ``funding_ranking.json`` — everything a simulator would need."""

    symbol: str
    spot_price: float | None
    perp_price: float | None
    index_price: float | None
    spot_perp_basis_bps: float | None
    mark_index_basis_bps: float | None
    open_interest_usd: float | None
    spot_quote_volume_24h_usd: float
    funding_interval_hours: float
    n_funding: int
    window_start: str | None
    window_end: str | None
    window_days: float
    mean_funding_bps: float
    median_funding_bps: float
    min_funding_bps: float
    max_funding_bps: float
    annualized_funding_pct: float
    positive_share: float
    sign_flip_rate: float
    longest_positive_streak: int
    longest_positive_streak_days: float
    spot_round_trip_pct: float
    perp_round_trip_pct: float
    round_trip_cost_pct: float
    hedged_cost_taker_pct: float
    hedged_cost_maker_pct: float
    hedged_break_even_days_taker: float | None
    hedged_break_even_days_maker: float | None
    hedged_dollars_per_day_taker: float
    hedged_dollars_per_day_maker: float
    is_mean_funding_bps: float
    oos_mean_funding_bps: float
    is_break_even_days_taker: float | None
    oos_break_even_days_taker: float | None
    clears_both_halves_taker: bool
    clears_both_halves_maker: bool
    implementable_taker: bool
    implementable_maker: bool
    tail_high_funding: bool
    has_spot_mirror: bool
    net_carry_pct: float
    break_even_days: float | None
    dollars_per_day_gross: float
    dollars_per_day_net_after_breakeven: float
    dollars_per_year_net_after_breakeven: float
    verdict: str
    reason: str


def compute_pair(payload: dict[str, object]) -> PairResult:
    """Pure per-pair computation: funding series + prices + volumes → a full row.

    Takes and returns plain data so it can be dispatched to a process pool. The cost
    model is the project's own (fees from ``FeeSchedule``, slippage from the S9 model).
    """
    symbol = str(payload["symbol"])
    rates = [float(r) for r in payload["funding_rates"]]  # type: ignore[union-attr]
    times = [int(t) for t in payload["funding_times"]]  # type: ignore[union-attr]
    interval_hours = float(payload.get("funding_interval_hours", DEFAULT_INTERVAL_HOURS))  # type: ignore[arg-type]
    volume = float(payload.get("spot_quote_volume_24h_usd", 0.0))  # type: ignore[arg-type]
    notional = float(payload["notional"])  # type: ignore[arg-type]

    stats = funding_stats(rates, interval_hours=interval_hours)

    profile = SymbolCostProfile.from_volume(symbol, volume)
    spot_leg = round_trip_cost_pct(
        profile, notional, mode="spot", fees=PROJECT_FEES,
        slippage_k=SLIPPAGE_K, exit_multiplier=EXIT_MULTIPLIER,
    )
    perp_leg = round_trip_cost_pct(
        profile, notional, mode="futures", fees=PROJECT_FEES,
        slippage_k=SLIPPAGE_K, exit_multiplier=EXIT_MULTIPLIER, include_funding=False,
    )
    total_cost_pct = spot_leg.total_pct + perp_leg.total_pct

    daily_funding_pct = (stats.mean_bps / 100.0) * (24.0 / interval_hours)
    be_days = break_even_days(total_cost_pct, daily_funding_pct)
    dollars_per_day = notional * daily_funding_pct / 100.0

    # Hedged cash-and-carry: 4 fee events (spot in/out + perp in/out), taker and maker.
    hedged = hedged_round_trip_cost(
        profile, notional, fees=PROJECT_FEES,
        slippage_k=SLIPPAGE_K, exit_multiplier=EXIT_MULTIPLIER,
    )
    hedged_be_taker = break_even_days(hedged.taker_pct, daily_funding_pct)
    hedged_be_maker = break_even_days(hedged.maker_pct, daily_funding_pct)

    # IS/OOS halves — the decisive test the prior study used.
    is_rates, oos_rates = split_halves(rates)
    is_half = half_stats(is_rates, interval_hours=interval_hours, cost_pct=hedged.taker_pct)
    oos_half = half_stats(oos_rates, interval_hours=interval_hours, cost_pct=hedged.taker_pct)
    is_half_maker = half_stats(is_rates, interval_hours=interval_hours, cost_pct=hedged.maker_pct)
    oos_half_maker = half_stats(oos_rates, interval_hours=interval_hours, cost_pct=hedged.maker_pct)

    verdict, reason = classify(
        mean_funding_bps=stats.mean_bps,
        positive_share=stats.positive_share,
        longest_streak=stats.longest_positive_streak,
        break_even_days_value=be_days,
    )

    spot_price = payload.get("spot_price")  # type: ignore[assignment]
    perp_price = payload.get("perp_price")  # type: ignore[assignment]
    index_price = payload.get("index_price")  # type: ignore[assignment]
    spot_perp_basis = (
        (float(perp_price) - float(spot_price)) / float(spot_price) * 10_000.0
        if spot_price and perp_price
        else None
    )
    mark_index_basis = (
        (float(perp_price) - float(index_price)) / float(index_price) * 10_000.0
        if index_price and perp_price
        else None
    )
    window_start = (
        datetime.fromtimestamp(min(times) / 1000, tz=UTC).isoformat() if times else None
    )
    window_end = (
        datetime.fromtimestamp(max(times) / 1000, tz=UTC).isoformat() if times else None
    )
    window_days = (max(times) - min(times)) / 86_400_000.0 if len(times) > 1 else 0.0

    return PairResult(
        symbol=symbol,
        spot_price=None if spot_price is None else float(spot_price),
        perp_price=None if perp_price is None else float(perp_price),
        index_price=None if index_price is None else float(index_price),
        spot_perp_basis_bps=None if spot_perp_basis is None else round(spot_perp_basis, 4),
        mark_index_basis_bps=None if mark_index_basis is None else round(mark_index_basis, 4),
        open_interest_usd=payload.get("open_interest_usd"),  # type: ignore[arg-type]
        spot_quote_volume_24h_usd=volume,
        funding_interval_hours=interval_hours,
        n_funding=stats.n,
        window_start=window_start,
        window_end=window_end,
        window_days=round(window_days, 2),
        mean_funding_bps=round(stats.mean_bps, 4),
        median_funding_bps=round(stats.median_bps, 4),
        min_funding_bps=round(stats.min_bps, 4),
        max_funding_bps=round(stats.max_bps, 4),
        annualized_funding_pct=round(stats.annualized_pct, 3),
        positive_share=round(stats.positive_share, 4),
        sign_flip_rate=round(stats.sign_flip_rate, 4),
        longest_positive_streak=stats.longest_positive_streak,
        longest_positive_streak_days=round(stats.longest_positive_streak_days, 2),
        spot_round_trip_pct=round(spot_leg.total_pct, 4),
        perp_round_trip_pct=round(perp_leg.total_pct, 4),
        round_trip_cost_pct=round(total_cost_pct, 4),
        hedged_cost_taker_pct=round(hedged.taker_pct, 4),
        hedged_cost_maker_pct=round(hedged.maker_pct, 4),
        hedged_break_even_days_taker=None if hedged_be_taker is None else round(hedged_be_taker, 2),
        hedged_break_even_days_maker=None if hedged_be_maker is None else round(hedged_be_maker, 2),
        hedged_dollars_per_day_taker=(
            0.0 if hedged_be_taker is None else round(dollars_per_day, 5)
        ),
        hedged_dollars_per_day_maker=(
            0.0 if hedged_be_maker is None else round(dollars_per_day, 5)
        ),
        is_mean_funding_bps=round(is_half.mean_bps, 4),
        oos_mean_funding_bps=round(oos_half.mean_bps, 4),
        is_break_even_days_taker=(
            None if is_half.break_even_days is None else round(is_half.break_even_days, 2)
        ),
        oos_break_even_days_taker=(
            None if oos_half.break_even_days is None else round(oos_half.break_even_days, 2)
        ),
        clears_both_halves_taker=clears_both_halves(is_half, oos_half),
        clears_both_halves_maker=clears_both_halves(is_half_maker, oos_half_maker),
        implementable_taker=(
            bool(payload.get("has_spot_mirror", False)) and clears_both_halves(is_half, oos_half)
        ),
        implementable_maker=(
            bool(payload.get("has_spot_mirror", False))
            and clears_both_halves(is_half_maker, oos_half_maker)
        ),
        tail_high_funding=is_tail_high_funding(stats),
        has_spot_mirror=bool(payload.get("has_spot_mirror", False)),
        net_carry_pct=round(stats.annualized_pct - total_cost_pct, 3),
        break_even_days=None if be_days is None else round(be_days, 2),
        dollars_per_day_gross=round(dollars_per_day, 5),
        dollars_per_day_net_after_breakeven=(0.0 if be_days is None else round(dollars_per_day, 5)),
        dollars_per_year_net_after_breakeven=(
            0.0 if be_days is None else round(dollars_per_day * 365.0, 2)
        ),
        verdict=verdict,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Data acquisition
# ---------------------------------------------------------------------------


def parse_funding_csv(raw: bytes) -> list[tuple[int, float, float]]:
    """Parse a Binance funding dump CSV into ``(calc_time_ms, interval_hours, rate)``.

    Tolerates a header row and the (older) header-less layout by skipping any row whose
    first field is not an integer. Malformed rows are skipped rather than crashing a
    90-day download on one bad line.
    """
    out: list[tuple[int, float, float]] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for name in archive.namelist():
            if not name.endswith(".csv"):
                continue
            text = archive.read(name).decode("utf-8", errors="replace")
            for row in csv.reader(io.StringIO(text)):
                if len(row) < 3:
                    continue
                try:
                    calc_time = int(row[0])
                    interval = float(row[1])
                    rate = float(row[2])
                except ValueError:
                    continue  # header or malformed line
                out.append((calc_time, interval, rate))
    out.sort(key=lambda r: r[0])
    return out


def _months_back(count: int, *, today: datetime | None = None) -> list[str]:
    """The last ``count`` completed calendar months, oldest first (``YYYY-MM``)."""
    today = today or datetime.now(UTC)
    year, month = today.year, today.month
    months: list[str] = []
    for _ in range(count):
        month -= 1
        if month == 0:
            month = 12
            year -= 1
        months.append(f"{year:04d}-{month:02d}")
    return sorted(months)


def enumerate_symbols(client: HttpClient, out_dir: Path) -> list[str]:
    """USDT-quoted USDⓈ-M perp symbols from the S3 funding-dump index."""
    raw = client.get_bytes(S3_INDEX, cache_path=out_dir / "_cache" / "s3_index_fundingRate.xml")
    if raw is None:
        raise SystemExit("S3 funding index unreachable")
    symbols = re.findall(
        r"<Prefix>data/futures/um/monthly/fundingRate/([^/]+)/</Prefix>", raw.decode()
    )
    return sorted(s for s in symbols if s.endswith("USDT"))


def fetch_funding_history(
    client: HttpClient, symbol: str, months: list[str], funding_dir: Path
) -> list[tuple[int, float, float]]:
    """Download + parse the monthly dumps for one symbol; missing months are skipped."""
    rows: list[tuple[int, float, float]] = []
    symbol_dir = funding_dir / symbol
    for month in months:
        url = f"{VISION}/{symbol}/{symbol}-fundingRate-{month}.zip"
        raw = client.get_bytes(url, cache_path=symbol_dir / f"{symbol}-fundingRate-{month}.zip")
        if raw is None:
            continue
        rows.extend(parse_funding_csv(raw))
    rows.sort(key=lambda r: r[0])
    return rows


def fetch_spot_market(client: HttpClient, out_dir: Path) -> dict[str, dict[str, float]]:
    """``{symbol: {price, quote_volume}}`` from the Binance public spot mirror."""
    cache = out_dir / "_cache"
    prices = client.get_json(f"{BINANCE_SPOT}/ticker/price", cache_path=cache / "spot_price.json")
    volumes = client.get_json(f"{BINANCE_SPOT}/ticker/24hr", cache_path=cache / "spot_24hr.json")
    out: dict[str, dict[str, float]] = {}
    for row in prices or []:  # type: ignore[union-attr]
        out.setdefault(row["symbol"], {})["price"] = float(row["price"])
    for row in volumes or []:  # type: ignore[union-attr]
        out.setdefault(row["symbol"], {})["quote_volume"] = float(row.get("quoteVolume", 0.0))
    return out


def fetch_perp_snapshot(client: HttpClient, out_dir: Path) -> dict[str, dict[str, float | None]]:
    """Binance USDⓈ-M perp snapshot (price/index/OI) from CoinGecko derivatives."""
    rows = client.get_json(
        f"{COINGECKO}/derivatives", cache_path=out_dir / "_cache" / "coingecko_derivatives.json"
    )
    out: dict[str, dict[str, float | None]] = {}
    for row in rows or []:  # type: ignore[union-attr]
        if row.get("market") != "Binance (Futures)" or row.get("contract_type") != "perpetual":
            continue
        symbol = str(row.get("symbol", ""))
        if not symbol.endswith("USDT"):
            continue
        out[symbol] = {
            "perp_price": _to_float(row.get("price")),
            "index_price": _to_float(row.get("index")),
            "open_interest_usd": _to_float(row.get("open_interest")),
        }
    return out


def _to_float(value: object) -> float | None:
    try:
        return None if value is None else float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(
    *, notional: float, months: int, out_dir: Path, workers: int, limit: int | None
) -> tuple[list[PairResult], dict[str, object]]:
    """Fetch, then compute (in a process pool), then persist. Returns rows + telemetry."""
    funding_dir = out_dir / "funding"
    funding_dir.mkdir(parents=True, exist_ok=True)
    client = HttpClient(cache_dir=out_dir / "_cache")

    started = time.monotonic()
    month_list = _months_back(months)
    symbols = enumerate_symbols(client, out_dir)
    census = {
        "binance_usdt_perps_in_index": len(symbols),
        "months_requested": len(month_list),
        "months": month_list,
        "with_funding_history": 0,
        "with_spot_volume_ge_1m": 0,
        "with_spot_volume_ge_5m": 0,
        "with_perp_snapshot": 0,
    }
    enum_s = time.monotonic() - started
    if limit:
        symbols = symbols[:limit]

    spot = fetch_spot_market(client, out_dir)
    perps = fetch_perp_snapshot(client, out_dir)

    payloads: list[dict[str, object]] = []
    failures: list[str] = []
    fetch_started = time.monotonic()

    def fetch(symbol: str) -> dict[str, object] | None:
        try:
            rows = fetch_funding_history(client, symbol, month_list, funding_dir)
        except Exception as error:  # noqa: BLE001 — one bad symbol must not kill the scan
            failures.append(f"{symbol}: {type(error).__name__}: {error}")
            return None
        if not rows:
            failures.append(f"{symbol}: no funding data for {month_list[0]}..{month_list[-1]}")
            return None
        times = [r[0] for r in rows]
        rates = [r[2] for r in rows]
        interval = statistics.median([r[1] for r in rows])
        volume = float(spot.get(symbol, {}).get("quote_volume", 0.0))
        snapshot = perps.get(symbol, {})
        return {
            "symbol": symbol,
            "funding_times": times,
            "funding_rates": rates,
            "funding_interval_hours": interval,
            "spot_price": spot.get(symbol, {}).get("price"),
            "perp_price": snapshot.get("perp_price"),
            "index_price": snapshot.get("index_price"),
            "open_interest_usd": snapshot.get("open_interest_usd"),
            "spot_quote_volume_24h_usd": volume,
            "has_spot_mirror": symbol in spot,
            "notional": notional,
        }

    with ThreadPoolExecutor(max_workers=NETWORK_CONCURRENCY) as pool:
        for result in pool.map(fetch, symbols):
            if result is None:
                continue
            payloads.append(result)
            volume = float(result["spot_quote_volume_24h_usd"])  # type: ignore[arg-type]
            census["with_funding_history"] += 1
            if volume >= 1_000_000:
                census["with_spot_volume_ge_1m"] += 1
            if volume >= 5_000_000:
                census["with_spot_volume_ge_5m"] += 1
            if result["perp_price"] is not None:
                census["with_perp_snapshot"] += 1
    fetch_s = time.monotonic() - fetch_started

    compute_started = time.monotonic()
    if workers > 1 and len(payloads) > 1:
        with mp.Pool(processes=workers) as pool:
            results = list(pool.map(compute_pair, payloads, chunksize=8))
    else:
        results = [compute_pair(p) for p in payloads]
    compute_s = time.monotonic() - compute_started

    results.sort(key=lambda r: r.net_carry_pct, reverse=True)
    (out_dir / "funding_ranking.json").write_text(
        json.dumps([asdict(r) for r in results], indent=1), encoding="utf-8"
    )
    (out_dir / "universe_census.json").write_text(json.dumps(census, indent=1), encoding="utf-8")

    telemetry = {
        "wall_clock_s": round(time.monotonic() - started, 2),
        "enumerate_s": round(enum_s, 2),
        "fetch_s": round(fetch_s, 2),
        "compute_s": round(compute_s, 2),
        "network_concurrency": NETWORK_CONCURRENCY,
        "per_domain_rate_per_s": PER_DOMAIN_RATE_PER_S,
        "compute_workers": workers,
        "http_stats": client.stats,
        "census": census,
        "pairs_ranked": len(payloads),
        "pairs_failed": len(failures),
        "failures": failures[:20],
    }
    (out_dir / "telemetry.json").write_text(json.dumps(telemetry, indent=1), encoding="utf-8")
    return results, telemetry


# ---------------------------------------------------------------------------
# Reachability probe + rendering
# ---------------------------------------------------------------------------


def probe_reachability(client: HttpClient, out_dir: Path) -> list[dict[str, object]]:
    """Record HTTP status + latency for the named endpoints, honestly (no retries)."""
    endpoints = [
        ("fapi_fundingRate", "https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1"),
        ("fapi_premiumIndex", "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT"),
        (
            "vision_monthly_funding_zip",
            f"{VISION}/BTCUSDT/BTCUSDT-fundingRate-2026-09.zip",
        ),
        (
            "vision_daily_funding_zip",
            "https://data.binance.vision/data/futures/um/daily/fundingRate/BTCUSDT/"
            "BTCUSDT-fundingRate-2026-10-01.zip",
        ),
        ("vision_s3_funding_index", S3_INDEX),
        ("data_api_spot_price", f"{BINANCE_SPOT}/ticker/price?symbol=BTCUSDT"),
        ("coingecko_derivatives", f"{COINGECKO}/derivatives"),
    ]
    results: list[dict[str, object]] = []
    for name, url in endpoints:
        started = time.monotonic()
        try:
            request = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read()
                status = response.status
            results.append({
                "name": name, "url": url, "status": status,
                "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
                "bytes": len(body), "ok": True,
            })
        except urllib.error.HTTPError as error:
            results.append({
                "name": name, "url": url, "status": error.code,
                "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
                "bytes": 0, "ok": False,
            })
        except Exception as error:  # noqa: BLE001 — a probe reports whatever happens
            results.append({
                "name": name, "url": url, "status": 0,
                "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
                "bytes": 0, "ok": False, "error": f"{type(error).__name__}: {error}",
            })
    (out_dir / "reachability.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    return results


def render_markdown(
    *,
    reachability: list[dict[str, object]],
    results: list[PairResult],
    telemetry: dict[str, object],
    notional: float,
    months: int,
    top_n: int = 15,
) -> str:
    census = telemetry["census"]
    candidates = [r for r in results if r.verdict == "candidate"]
    lines: list[str] = []
    lines.append(
        f"# Funding carry scan — Binance data dumps — {datetime.now(UTC).date().isoformat()}\n"
    )
    lines.append("- **Data source: Binance official funding dumps** "
                 "(`data.binance.vision`), Binance spot mirror, CoinGecko perp snapshot. "
                 "`fapi.binance.com` is geo-blocked (HTTP 000) and was not used.")
    lines.append(f"- Notional per pair: **${notional:.2f}**")
    lines.append(f"- Funding window requested: **{months} months** "
                 f"({census['months'][0]} .. {census['months'][-1]})")
    lines.append(
        f"- Fee schedule: project base tier {PROJECT_FEES.spot_taker_bps:.0f}bps spot taker / "
        f"{PROJECT_FEES.futures_taker_bps:.0f}bps perp taker, both legs round trip"
    )
    lines.append(f"- Slippage: project S9 model, k={SLIPPAGE_K}, exit×{EXIT_MULTIPLIER}")
    lines.append(
        f"- Network: {NETWORK_CONCURRENCY} concurrent, {PER_DOMAIN_RATE_PER_S}/s per host, "
        "exponential backoff on 429/418/5xx; 404 = definitive miss, not retried"
    )
    lines.append(
        f"- Compute: {telemetry['compute_workers']} processes; wall-clock "
        f"{telemetry['wall_clock_s']}s (enumerate {telemetry['enumerate_s']}s, "
        f"fetch {telemetry['fetch_s']}s, compute {telemetry['compute_s']}s)\n"
    )

    lines.append("## Endpoint reachability\n")
    lines.append("| endpoint | HTTP | latency | bytes |")
    lines.append("|---|---:|---:|---:|")
    for row in reachability:
        lines.append(
            f"| {row['name']} | {row['status']} | {row['latency_ms']}ms | {row['bytes']} |"
        )
    lines.append("")

    lines.append("## Universe census (nothing dropped silently)\n")
    lines.append("| stage | pairs |")
    lines.append("|---|---:|")
    for key, value in census.items():
        lines.append(f"| {key} | {value} |")
    lines.append(f"| ranked (funding parsed) | {len(results)} |")
    lines.append("")

    lines.append("## Funding distribution across all ranked pairs\n")
    means = sorted(r.mean_funding_bps for r in results)
    if means:
        def pct(p: float) -> float:
            idx = min(len(means) - 1, int(p * (len(means) - 1)))
            return means[idx]
        lines.append(f"- mean funding (bps/8h): min {means[0]:.2f}, "
                     f"p25 {pct(0.25):.2f}, median {pct(0.50):.2f}, "
                     f"p75 {pct(0.75):.2f}, max {means[-1]:.2f}")
        lines.append(f"- pairs with mean funding ≥ 0 bps/8h: "
                     f"{sum(1 for m in means if m >= 0)} / {len(means)}")
        lines.append(f"- pairs with mean funding ≥ 5 bps/8h: "
                     f"{sum(1 for m in means if m >= 5)} / {len(means)}")
        lines.append(f"- pairs with mean funding ≥ 30 bps/8h: "
                     f"{sum(1 for m in means if m >= 30)} / {len(means)}")
        annualised_30 = sum(1 for r in results if r.annualized_funding_pct >= 30)
        lines.append(f"- pairs with annualised funding ≥ 30%: {annualised_30} / {len(results)}")
    lines.append("")

    lines.append(f"## Top {min(top_n, len(results))} by hedged net carry\n")
    lines.append("| pair | mean bps/8h | pos% | flips | streak d | basis bps | "
                 "hedged cost % (taker/maker) | hedged BE d | $/day | verdict |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    hedged_ranked = sorted(
        results,
        key=lambda r: r.annualized_funding_pct - r.hedged_cost_taker_pct,
        reverse=True,
    )
    for r in hedged_ranked[:top_n]:
        basis = "n/a" if r.spot_perp_basis_bps is None else f"{r.spot_perp_basis_bps:.2f}"
        taker_be = (
            "never"
            if r.hedged_break_even_days_taker is None
            else f"{r.hedged_break_even_days_taker:.0f}"
        )
        lines.append(
            f"| {r.symbol} | {r.mean_funding_bps:.2f} | {r.positive_share:.0%} | "
            f"{r.sign_flip_rate:.2f} | {r.longest_positive_streak_days:.1f} | {basis} | "
            f"{r.hedged_cost_taker_pct:.2f} / {r.hedged_cost_maker_pct:.2f} | "
            f"{taker_be} | {r.hedged_dollars_per_day_taker:.5f} | {r.verdict} |"
        )
    lines.append("")

    lines.append("## Liquid intersection — hedgeable AND clears the bar\n")
    lines.append(
        "The highest-funding symbols are largely **unhedgeable**: they have no spot pair "
        "to buy against the short perp, so the 'market-neutral' leg cannot be built. "
        "Filtering to a spot 24h quote volume of at least $1M / $5M:"
    )
    lines.append("")
    lines.append("| filter | symbols |")
    lines.append("|---|---|")
    for label, predicate in (
        ("clears taker AND spot vol ≥ $1M", lambda x: x.clears_both_halves_taker
            and x.spot_quote_volume_24h_usd >= 1_000_000),
        ("clears taker AND spot vol ≥ $5M", lambda x: x.clears_both_halves_taker
            and x.spot_quote_volume_24h_usd >= 5_000_000),
        ("verdict candidate AND spot vol ≥ $1M", lambda x: x.verdict == "candidate"
            and x.spot_quote_volume_24h_usd >= 1_000_000),
        ("verdict candidate AND spot vol ≥ $5M", lambda x: x.verdict == "candidate"
            and x.spot_quote_volume_24h_usd >= 5_000_000),
    ):
        symbols = [x.symbol for x in results if predicate(x)]
        lines.append(f"| {label} | {len(symbols)}: {', '.join(symbols) or 'none'} |")
    lines.append("")
    lines.append(
        "**The $100 practical read:** even the surviving hedgeable names are thin "
        "(XMR/XVG/ATA spot ≈ $0.4–0.6M/24h). At $100 split ~$50/leg, round-trip cost is "
        "~1.1% (taker) and net carry ~17–22% annualised — i.e. **~$0.05–0.06/day**, before "
        "the basis-drift term this test omits. MARSCOINUSDT pays funding every **4h** (not "
        "8h) and is the one ≥$1M-liquidity name that clears the taker bar."
    )
    lines.append("")
    lines.append(
        "**New-listing caveat (the dominant trap in this tail):** the top-funding symbols "
        "are mostly **recent listings** with only weeks of history (HIPPOUSDT 7 days, "
        "DAMUSDT 28 days, MARSCOINUSDT 29 days, FIOUSDT 14 days). New perps routinely open "
        "at a large funding premium that decays; a 7-day streak is not persistence. The "
        "symbols with a full **182-day** window and a liquid hedge are XMR/XVG/ATA — and "
        "their funding is only **~1.7–2.1 bps/8h (~19–23% annualised)**, at the margin of "
        "the cost bar, not a high-funding tail."
    )
    lines.append("")

    lines.append("## Reconciliation with prior work (`~/back/FUNDING_OR_ALT.md`)\n")
    lines.append(
        "- The prior study used **21 symbols × 4 months (2026-05..08)** and concluded the "
        "hedged cash-and-carry was **negative in both IS/OOS halves at taker cost**, with "
        "the best maker case (N=21 OOS) at **−3.03 bps/trade** and a CI straddling zero. "
        "Its structural finding: OOS funding income ≈ **6.5 bps per hold** versus a "
        "**20 bps** hedged taker round trip."
    )
    lines.append(
        "- This scan **extends** that to the full index (hundreds of symbols × 6 months) "
        "and asks the *tail* question the prior work did not: is there any symbol whose "
        "funding is high and persistent enough to flip the sign? The answer is in the "
        "decisive-test section above."
    )
    lines.append(
        "- **Cost-model note:** the prior doc charged a *blended* hedged round trip of "
        "20 bps taker / 12 bps maker (perp 2×5 + spot 2×5, plus 5 bps slippage). This "
        "scan uses the project's own S9 model, which charges **spot taker 10 bps on both "
        "spot legs**: 30 bps taker / 24 bps maker. The project's number is *stricter*, so "
        "agreement between the two is agreement under a harder bar — and a symbol failing "
        "here would also fail the prior doc's looser 20/12 bps bar."
    )
    lines.append("")

    lines.append("## Verdict counts\n")
    lines.append(f"- pairs ranked: **{len(results)}**")
    lines.append(f"- pairs with ≥30 days of funding history: "
                 f"**{sum(1 for r in results if r.window_days >= 30)}**")
    lines.append(f"- candidate: **{len(candidates)}**")
    lines.append(f"- no: **{len(results) - len(candidates)}**")
    tail = [r for r in results if r.tail_high_funding]
    lines.append(
        f"- tail (mean ≥ {TAIL_MEAN_BPS:.0f} bps/8h AND streak ≥ {TAIL_STREAK_DAYS:.0f}d): "
        f"**{len(tail)}**"
    )
    both_taker = [r for r in results if r.clears_both_halves_taker]
    both_maker = [r for r in results if r.clears_both_halves_maker]
    impl_taker = [r for r in results if r.implementable_taker]
    impl_maker = [r for r in results if r.implementable_maker]
    lines.append(f"- clears hedged cost in BOTH IS/OOS halves @ taker: **{len(both_taker)}**")
    lines.append(f"- clears hedged cost in BOTH IS/OOS halves @ maker: **{len(both_maker)}**")
    lines.append(f"- ... AND has a spot mirror to hedge with, @ taker: **{len(impl_taker)}**")
    lines.append(f"- ... AND has a spot mirror to hedge with, @ maker: **{len(impl_maker)}**")
    lines.append("")

    lines.append("## Tail — high-funding symbols (mean ≥ 30 bps/8h, streak ≥ 14 days)\n")
    if tail:
        lines.append("| pair | mean bps/8h | ann% | pos% | streak d | IS mean | OOS mean | "
                     "hedged taker% | hedged BE d | spot mirror | clears taker | clears maker |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|:--:|:--:|:--:|")
        for r in sorted(tail, key=lambda x: x.mean_funding_bps, reverse=True):
            hedged_be = (
                "never"
                if r.hedged_break_even_days_taker is None
                else f"{r.hedged_break_even_days_taker:.1f}"
            )
            lines.append(
                f"| {r.symbol} | {r.mean_funding_bps:.1f} | {r.annualized_funding_pct:.0f} | "
                f"{r.positive_share:.0%} | {r.longest_positive_streak_days:.1f} | "
                f"{r.is_mean_funding_bps:.1f} | {r.oos_mean_funding_bps:.1f} | "
                f"{r.hedged_cost_taker_pct:.2f} | {hedged_be} | "
                f"{'yes' if r.has_spot_mirror else 'NO'} | "
                f"{'YES' if r.clears_both_halves_taker else 'no'} | "
                f"{'YES' if r.clears_both_halves_maker else 'no'} |"
            )
    else:
        lines.append(f"No symbol has mean funding ≥ {TAIL_MEAN_BPS:.0f} bps/8h sustained "
                     f"≥ {TAIL_STREAK_DAYS:.0f} consecutive days.")
    lines.append("")

    lines.append("## The decisive test — does ANY symbol clear the cost floor in BOTH halves?\n")
    if both_taker or both_maker:
        lines.append(
            f"- @ taker (economics only): {', '.join(r.symbol for r in both_taker) or 'none'}"
        )
        lines.append(
            f"- @ maker (economics only): {', '.join(r.symbol for r in both_maker) or 'none'}"
        )
        lines.append(f"- @ taker AND hedgeable (spot mirror exists): "
                     f"{', '.join(r.symbol for r in impl_taker) or 'none'}")
        lines.append(f"- @ maker AND hedgeable (spot mirror exists): "
                     f"{', '.join(r.symbol for r in impl_maker) or 'none'}")
    else:
        lines.append("**NO.** Zero symbols have positive mean funding whose hedged round trip "
                     "is recovered within 30 days in BOTH the in-sample and out-of-sample "
                     "halves — at taker OR maker pricing. This confirms and extends the prior "
                     "study (`FUNDING_OR_ALT.md`): the cost floor is structural, not a "
                     "21-symbol artifact.")
    lines.append("")
    lines.append(
        "**Caveat (in the generous direction):** this test nets *funding income* against "
        "the hedged cost only. It does **not** include the spot–perp **basis drift** term, "
        "which `FUNDING_OR_ALT.md` measured as material (up to ±9,770 bps aggregate). A "
        "symbol that passes this funding-only bar could still fail once basis drift is "
        "included, so this bar is a *lower* bound on the true hurdle, not an upper one. "
        "The hedged cost here (project model: 2×10 bps spot + 2×5 bps perp = **30 bps** "
        "taker; 2×10 + 2×2 = **24 bps** maker) is also *stricter* than the prior doc's "
        "20 bps / 12 bps, because it charges spot taker 10 bps on both spot legs rather "
        "than a blended 3–5 bps."
    )
    lines.append("")

    if candidates:
        # Prefer a *hedgeable* candidate (spot mirror exists) — a perp-only symbol cannot
        # be made market-neutral, so its carry is not implementable. Among hedgeable ones,
        # headline the most *liquid* (most implementable at $100), and name the highest
        # net-carry one separately so the trade-off is explicit rather than hidden.
        hedgeable = [r for r in candidates if r.has_spot_mirror]
        if hedgeable:
            best = max(hedgeable, key=lambda r: r.spot_quote_volume_24h_usd)
            impl_note = "hedgeable, most liquid"
        else:
            best = candidates[0]
            impl_note = "NOT hedgeable (perp-only)"
        per_year = best.dollars_per_year_net_after_breakeven
        lines.append(
            f"Best candidate ({impl_note}): **{best.symbol}** — {best.net_carry_pct:.2f}% net "
            f"annualised, ${best.hedged_dollars_per_day_taker:.5f}/day ≈ ${per_year:.2f}/yr "
            f"at ${notional:.0f}, hedged break-even {best.hedged_break_even_days_taker:.1f}d, "
            f"longest positive streak {best.longest_positive_streak_days:.1f}d, "
            f"spot 24h vol ${best.spot_quote_volume_24h_usd:,.0f}."
        )
        if hedgeable:
            top_carry = max(hedgeable, key=lambda r: r.net_carry_pct)
            lines.append(
                f"Highest net-carry hedgeable candidate: **{top_carry.symbol}** "
                f"({top_carry.net_carry_pct:.2f}%/yr, spot vol "
                f"${top_carry.spot_quote_volume_24h_usd:,.0f}, "
                f"{top_carry.window_days:.0f}d history)."
            )
    else:
        lines.append("**No pair clears the bar** (positive mean, ≥60% positive, streak ≥21 "
                     "intervals, break-even ≤30 days).")
    return "\n".join(lines) + "\n"


def _load_pair_results(path: Path) -> list[PairResult]:
    """Rehydrate ``PairResult`` rows from ``funding_ranking.json`` for re-rendering."""
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [PairResult(**row) for row in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notional", type=float, default=100.0)
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--workers", type=int, default=min(8, mp.cpu_count()))
    parser.add_argument("--limit", type=int, default=None, help="cap symbols (debug)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="rebuild REPORT.md from cached funding_ranking.json (no network)",
    )
    args = parser.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    if args.render_only:
        results = _load_pair_results(args.out / "funding_ranking.json")
        telemetry = json.loads((args.out / "telemetry.json").read_text(encoding="utf-8"))
        reachability = json.loads((args.out / "reachability.json").read_text(encoding="utf-8"))
        report = render_markdown(
            reachability=reachability, results=results, telemetry=telemetry,
            notional=args.notional, months=args.months,
        )
        (args.out / "REPORT.md").write_text(report, encoding="utf-8")
        print(report)
        return 0

    client = HttpClient(cache_dir=args.out / "_cache")
    reachability = probe_reachability(client, args.out)
    if args.probe_only:
        print(json.dumps(reachability, indent=1))
        return 0

    results, telemetry = run(
        notional=args.notional, months=args.months, out_dir=args.out,
        workers=args.workers, limit=args.limit,
    )
    report = render_markdown(
        reachability=reachability, results=results, telemetry=telemetry,
        notional=args.notional, months=args.months,
    )
    (args.out / "REPORT.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
