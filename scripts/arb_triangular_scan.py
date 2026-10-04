"""Measure triangular arbitrage on Binance spot from REAL order books (research only).

Answers one question, with numbers instead of promises: **at $100 of paper capital and
the project's own base-tier fee, does any USDT-anchored triangle on Binance spot show a
*net-positive* executable edge — and is any such edge persistent enough to matter?**

Method (read-only, public endpoints, no orders, no keys):

1. ``GET /api/v3/exchangeInfo`` -> every tradable symbol + lot/tick/min-notional filters.
2. ``GET /api/v3/ticker/24hr`` (one call, weight 80) -> 24h quote volume per symbol.
3. **Enumerate every USDT-anchored triangle** ``USDT -> A -> B -> USDT`` where both
   ``A/USDT`` and ``B/USDT`` exist and a direct cross ``A/B`` or ``B/A`` exists. Both
   cyclic directions are kept (they are distinct trades). Triangles are tagged by class:
   ``stable`` (path crosses USDC/FDUSD/TUSD/USD1/…), ``btc`` (path crosses BTC), else
   ``crypto``.
4. For each triangle, fetch live depth (top ``--depth-limit`` levels) for the 3 legs and
   walk the book for the $100 size using **bid/ask** (BUY at ask, SELL at bid) — never
   mid — charging the fee from :mod:`crypto_brain.engine.fees` on every leg, honouring
   the venue lot step and minimum notional.
5. Phase A: one broad liquidity sweep over **every** enumerated triangle, recording the
   executable edge and the **maximum executable size** at that instant.
6. Phase B: poll the top candidates every ``--interval`` s for ``--minutes`` minutes and
   record the time series; report max / mean / p90 / %positive / longest positive run /
   mean gap between positive windows.

**Fee assumptions are reported separately and never conflated.** The headline verdict uses
``FeeSchedule()`` defaults (DESIGN.md §10.2 base tier) = **10 bps per leg = 30 bps per
3-leg round trip** (Binance spot VIP0, no BNB discount, no maker credit). A maker/rebate
variant (**2 bps per leg = 6 bps round trip**) is computed as an explicit what-if.

**Rate discipline.** Binance limits are per-IP and shared with other local workers, so
concurrency is bounded (default 5), every request passes a small backoff limiter that
honours ``Retry-After`` on 429/418 and never retry-storms, and raw books are cached under
``evidence/`` with timestamps. This is deliberately *below* the research layer's
``ResearchLimiter`` cap of 1 req/s per domain: that pacing would make a 400-book sweep
take 7 minutes per cycle, so a bounded-concurrency pool with backoff is used instead.

Run::

    uv run python scripts/arb_triangular_scan.py --minutes 45 --interval 6
    uv run python scripts/arb_triangular_scan.py --discover-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing as mp
import statistics as st
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from crypto_brain.data.sources.binance_spot import (  # noqa: E402
    HttpxSpotClient,
    SpotSourceError,
)
from crypto_brain.engine.fees import FeeSchedule  # noqa: E402

Side = Literal["buy", "sell"]

BASE = "https://data-api.binance.vision"
EVIDENCE_ROOT = REPO / "evidence" / "arbitrage" / "2026-10-03" / "triangular"

#: §10.2 base tier: 0.10% spot taker. Three legs -> 30 bps round trip.
TAKER_FEES = FeeSchedule()
TAKER_RATE = TAKER_FEES.rate("spot")  # fraction of notional per leg

#: What-if only: a maker/rebate scenario at 2 bps per leg (Binance spot VIP0 maker is
#: 0.10% *and equal to taker*, so 2 bps is an optimistic assumption used solely to show
#: what the arithmetic would need — never the headline.
MAKER_RATE = 0.0002

LEGS = 3
STABLES = {"USDC", "FDUSD", "TUSD", "USD1", "BUSD", "DAI", "USDP", "AEUR", "EUR"}


# --------------------------------------------------------------------- math
# Pure, network-free. These are the functions the unit tests pin.


def walk_buy_asks(
    asks: list[tuple[float, float]], quote_amount: float, fee_rate: float
) -> float | None:
    """Spend ``quote_amount`` walking ``asks``; return base received net of fee.

    ``asks`` is ``[(price, qty), ...]`` best-first. Returns ``None`` when the book
    cannot absorb the whole quote amount (not executable at this size).
    """
    if quote_amount <= 0:
        return None
    remaining = quote_amount
    base_gross = 0.0
    for price, qty in asks:
        if price <= 0 or qty <= 0:
            continue
        level_quote = price * qty
        take = min(remaining, level_quote)
        base_gross += take / price
        remaining -= take
        if remaining <= 1e-12:
            break
    if remaining > max(1e-9, quote_amount * 1e-12):
        return None
    return base_gross * (1.0 - fee_rate)


def walk_sell_bids(
    bids: list[tuple[float, float]], base_amount: float, fee_rate: float
) -> float | None:
    """Sell ``base_amount`` walking ``bids``; return quote received net of fee.

    ``bids`` is ``[(price, qty), ...]`` best-first. Returns ``None`` when the book
    cannot absorb the whole base amount.
    """
    if base_amount <= 0:
        return None
    remaining = base_amount
    quote_gross = 0.0
    for price, qty in bids:
        if price <= 0 or qty <= 0:
            continue
        take = min(remaining, qty)
        quote_gross += take * price
        remaining -= take
        if remaining <= 1e-12:
            break
    if remaining > max(1e-12, base_amount * 1e-9):
        return None
    return quote_gross * (1.0 - fee_rate)


def floor_step(amount: float, step: float | None) -> float:
    """Floor ``amount`` down to the exchange lot/tick step (no-op if step falsy)."""
    if not step or step <= 0:
        return amount
    return int(amount / step) * step


def value_at_bid(bids: list[tuple[float, float]], base_amount: float) -> float:
    """Best-effort liquidation value (gross, no fee) of a base remainder at the bid.

    Used for the sub-lot inventory a step-constrained order cannot sell. Depth beyond
    the visible book is conservatively dropped.
    """
    if base_amount <= 0 or not bids:
        return 0.0
    remaining = base_amount
    quote = 0.0
    for price, qty in bids:
        if price <= 0 or qty <= 0:
            continue
        take = min(remaining, qty)
        quote += take * price
        remaining -= take
        if remaining <= 1e-12:
            break
    return quote


@dataclass(frozen=True)
class Book:
    """One order book side pair, best-first."""

    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]


@dataclass(frozen=True)
class Leg:
    """One conversion in a triangle.

    ``side='buy'`` converts quote -> base using ``asks``; ``side='sell'`` converts
    base -> quote using ``bids``. ``step`` is the lot step of the asset being *ordered*
    (the quote we spend on a buy, the base we sell on a sell).
    """

    symbol: str
    side: Side
    step: float | None = None
    min_notional: float = 0.0


@dataclass(frozen=True)
class Triangle:
    """``USDT -> A -> B -> USDT`` (or the reverse direction)."""

    name: str
    legs: tuple[Leg, Leg, Leg]
    min_volume_usd: float
    path: tuple[str, str, str, str]
    klass: str = "crypto"

    def leg_symbols(self) -> list[str]:
        return [leg.symbol for leg in self.legs]


@dataclass(frozen=True)
class RoundTrip:
    """Result of executing a triangle at a fixed starting quote amount.

    ``final_quote`` is realized USDT; ``residual_usd`` marks the sub-lot leftovers
    (unspent quote + unsold base) at the prevailing bid. ``total_usd`` is the
    mark-to-market end value the edge is computed from. ``executable`` is False when any
    leg could not be filled from the visible book (or fell under minimum notional) —
    such a sample is *not* a tradeable opportunity.
    """

    final_quote: float
    residual_usd: float
    total_usd: float
    executable: bool
    legs_completed: int


def simulate_triangle(
    start_quote: float,
    books: dict[str, Book],
    triangle: Triangle,
    fee_rate: float,
) -> RoundTrip | None:
    """Round-trip ``start_quote`` through the triangle.

    Returns ``None`` only when a required book is missing. Otherwise returns a
    :class:`RoundTrip` whose ``executable`` flag records whether every leg actually
    filled at this size.
    """
    amount = start_quote
    residual = 0.0
    for legs_done, leg in enumerate(triangle.legs):
        book = books.get(leg.symbol)
        if book is None:
            return None
        if leg.side == "buy":
            if leg.min_notional and amount < leg.min_notional:
                return RoundTrip(amount, residual, amount + residual, False, legs_done)
            order = floor_step(amount, leg.step) if leg.step else amount
            if order <= 0:
                return RoundTrip(amount, residual, amount + residual, False, legs_done)
            out = walk_buy_asks(book.asks, order, fee_rate)
            if out is None:
                return RoundTrip(amount, residual, amount + residual, False, legs_done)
            residual += amount - order  # unspent quote stays in inventory
            amount = out
        else:
            order = floor_step(amount, leg.step) if leg.step else amount
            if order <= 0:
                return RoundTrip(amount, residual, amount + residual, False, legs_done)
            out = walk_sell_bids(book.bids, order, fee_rate)
            if out is None:
                return RoundTrip(amount, residual, amount + residual, False, legs_done)
            residual += value_at_bid(book.bids, amount - order)  # sub-lot base
            amount = out
    return RoundTrip(amount, residual, amount + residual, True, len(triangle.legs))


def net_edge_bps(start_quote: float, final_quote: float | None) -> float | None:
    """Net executable edge in basis points, or ``None`` when not executable."""
    if final_quote is None:
        return None
    return (final_quote / start_quote - 1.0) * 10_000.0


def triangle_edge_bps(start_quote: float, result: RoundTrip | None) -> float | None:
    """Edge of a :class:`RoundTrip` in bps, or ``None`` when it was not executable."""
    if result is None or not result.executable:
        return None
    return net_edge_bps(start_quote, result.total_usd)


def max_executable_size(
    books: dict[str, Book],
    triangle: Triangle,
    fee_rate: float,
    cap: float = 100_000.0,
    start: float = 100.0,
    probes: int = 24,
) -> float:
    """Largest start size (USD) at which every leg still fills from the visible book.

    Anchored on ``start`` (the traded size): grows geometrically while executable, then
    bisects; if ``start`` is not executable, shrinks toward the venue minimum. Returns
    ``0.0`` when even the minimum-notional size is not executable.
    """
    floor = max(1.0, triangle.legs[0].min_notional or 0.0)
    if not _is_executable(start, books, triangle, fee_rate):
        if not _is_executable(floor, books, triangle, fee_rate):
            return 0.0
        lo, hi = floor, start
        for _ in range(probes):
            mid = (lo + hi) / 2.0
            if _is_executable(mid, books, triangle, fee_rate):
                lo = mid
            else:
                hi = mid
        return lo
    lo = hi = start
    while hi < cap:
        lo = hi
        hi = min(hi * 2.0, cap)
        if not _is_executable(hi, books, triangle, fee_rate):
            break
    else:
        return cap
    for _ in range(probes):
        mid = (lo + hi) / 2.0
        if _is_executable(mid, books, triangle, fee_rate):
            lo = mid
        else:
            hi = mid
    return lo


def _is_executable(
    size: float, books: dict[str, Book], triangle: Triangle, fee_rate: float
) -> bool:
    result = simulate_triangle(size, books, triangle, fee_rate)
    return result is not None and result.executable


# ------------------------------------------------------------------ discovery


@dataclass
class Market:
    base: str
    quote: str
    volume_usd: float = 0.0
    step_size: float = 0.0
    min_notional: float = 0.0


def parse_symbols(exchange_info: dict[str, Any]) -> dict[str, Market]:
    out: dict[str, Market] = {}
    for sym in exchange_info.get("symbols", []):
        if sym.get("status") != "TRADING":
            continue
        if not sym.get("isSpotTradingAllowed", True):
            continue
        base, quote = sym.get("baseAsset"), sym.get("quoteAsset")
        if not base or not quote:
            continue
        step = 0.0
        min_notional = 0.0
        for f in sym.get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                step = float(f.get("stepSize", 0) or 0)
            elif f.get("filterType") in ("NOTIONAL", "MIN_NOTIONAL"):
                min_notional = float(f.get("minNotional", 0) or 0)
        out[sym["symbol"]] = Market(base, quote, 0.0, step, min_notional)
    return out


def apply_volumes(markets: dict[str, Market], tickers: list[dict[str, Any]]) -> None:
    for t in tickers:
        sym = t.get("symbol")
        if sym in markets:
            markets[sym].volume_usd = float(t.get("quoteVolume", 0) or 0)


def classify(path: tuple[str, str, str, str]) -> str:
    assets = set(path[1:3])
    if assets & STABLES:
        return "stable"
    if "BTC" in assets or "ETH" in assets:
        return "major"
    return "crypto"


def discover_triangles(
    markets: dict[str, Market],
    *,
    usdt_volume_floor: float,
    cross_volume_floor: float,
) -> list[Triangle]:
    """Every USDT-anchored triangle (both directions) above the stated volume floors."""
    usdt_pairs: dict[str, tuple[str, Market]] = {}
    for sym, m in markets.items():
        if m.quote == "USDT" and m.base != "USDT" and m.volume_usd >= usdt_volume_floor:
            usdt_pairs[m.base] = (sym, m)

    by_pair: dict[tuple[str, str], tuple[str, Market]] = {}
    for sym, m in markets.items():
        if m.base == m.quote:
            continue
        by_pair[(m.base, m.quote)] = (sym, m)

    bases = sorted(usdt_pairs)
    triangles: list[Triangle] = []
    for i, a in enumerate(bases):
        for b in bases[i + 1 :]:
            cross = by_pair.get((a, b))
            orientation = "ab"
            if cross is None:
                cross = by_pair.get((b, a))
                orientation = "ba"
            if cross is None:
                continue
            cross_sym, cross_market = cross
            if cross_market.volume_usd < cross_volume_floor:
                continue
            a_sym, a_mkt = usdt_pairs[a]
            b_sym, b_mkt = usdt_pairs[b]
            min_vol = min(a_mkt.volume_usd, b_mkt.volume_usd, cross_market.volume_usd)

            if orientation == "ab":
                # cross A/B (base A, quote B): selling A yields B.
                legs_ab = (
                    Leg(a_sym, "buy", a_mkt.step_size, a_mkt.min_notional),
                    Leg(cross_sym, "sell", cross_market.step_size, cross_market.min_notional),
                    Leg(b_sym, "sell", b_mkt.step_size, b_mkt.min_notional),
                )
                legs_ba = (
                    Leg(b_sym, "buy", b_mkt.step_size, b_mkt.min_notional),
                    Leg(cross_sym, "buy", cross_market.step_size, cross_market.min_notional),
                    Leg(a_sym, "sell", a_mkt.step_size, a_mkt.min_notional),
                )
            else:
                # cross B/A (base B, quote A): buying B with A.
                legs_ab = (
                    Leg(a_sym, "buy", a_mkt.step_size, a_mkt.min_notional),
                    Leg(cross_sym, "buy", cross_market.step_size, cross_market.min_notional),
                    Leg(b_sym, "sell", b_mkt.step_size, b_mkt.min_notional),
                )
                legs_ba = (
                    Leg(b_sym, "buy", b_mkt.step_size, b_mkt.min_notional),
                    Leg(cross_sym, "sell", cross_market.step_size, cross_market.min_notional),
                    Leg(a_sym, "sell", a_mkt.step_size, a_mkt.min_notional),
                )

            path_ab = ("USDT", a, b, "USDT")
            path_ba = ("USDT", b, a, "USDT")
            triangles.append(
                Triangle(f"{a}>{b}", legs_ab, min_vol, path_ab, classify(path_ab))
            )
            triangles.append(
                Triangle(f"{b}>{a}", legs_ba, min_vol, path_ba, classify(path_ba))
            )
    triangles.sort(key=lambda t: t.min_volume_usd, reverse=True)
    return triangles


# --------------------------------------------------------------------- stats


def percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolated percentile of ``values`` (``pct`` in [0, 100])."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def longest_positive_run(series: list[float | None]) -> int:
    """Longest streak of consecutive samples with ``net_bps > 0`` (None breaks it)."""
    best = cur = 0
    for v in series:
        if v is not None and v > 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def positive_windows(series: list[float | None]) -> int:
    """Number of maximal runs of consecutive positive samples."""
    windows = 0
    prev = False
    for v in series:
        pos = v is not None and v > 0
        if pos and not prev:
            windows += 1
        prev = pos
    return windows


def summarize_series(
    name: str,
    path: tuple[str, str, str, str],
    klass: str,
    leg_symbols: list[str],
    min_volume_usd: float,
    series: list[float | None],
    interval_s: float,
    max_size_usd: float,
) -> dict[str, Any]:
    """One triangle's time-series summary. Pure; safe to run in a worker process."""
    executable = [v for v in series if v is not None]
    positive = [v for v in executable if v > 0]
    runs = positive_windows(series)
    best = max(executable) if executable else None
    window_s = len(series) * interval_s
    return {
        "triangle": name,
        "path": "->".join(path),
        "class": klass,
        "legs": leg_symbols,
        "min_leg_volume_24h_usd": round(min_volume_usd, 0),
        "samples": len(series),
        "executable_samples": len(executable),
        "non_executable_samples": len(series) - len(executable),
        "max_net_bps": round(best, 3) if best is not None else None,
        "mean_net_bps": round(st.mean(executable), 3) if executable else None,
        "median_net_bps": round(st.median(executable), 3) if executable else None,
        "p90_net_bps": round(percentile(executable, 90), 3) if executable else None,
        "min_net_bps": round(min(executable), 3) if executable else None,
        "pct_samples_positive": round(100.0 * len(positive) / len(series), 3)
        if series
        else None,
        "positive_samples": len(positive),
        "longest_positive_run_samples": longest_positive_run(series),
        "positive_windows": runs,
        "mean_gap_between_windows_s": round(window_s / runs, 1) if runs else None,
        "time_to_first_positive_s": _time_to_first(series, interval_s),
        "max_executable_size_usd": round(max_size_usd, 2),
        "profit_usd_per_100_at_best": round(best / 100.0, 4) if best is not None else None,
    }


def _time_to_first(series: list[float | None], interval_s: float) -> float | None:
    for i, v in enumerate(series):
        if v is not None and v > 0:
            return round(i * interval_s, 1)
    return None


def _summarize_worker(args: tuple) -> dict[str, Any]:
    return summarize_series(*args)


# ------------------------------------------------------------------- network


class BackoffLimiter:
    """Bounded concurrency + exponential backoff over the project's own HTTP layer.

    The transport is :class:`~crypto_brain.data.sources.binance_spot.HttpxSpotClient`
    (the existing seam, which already targets ``data-api.binance.vision``), *not* a new
    client. That client is synchronous, so each call runs in a worker thread; the
    semaphore keeps in-flight requests low (Binance limits are per-IP and shared with a
    sibling worker downloading data dumps).

    On 429/418 it honours ``Retry-After`` (or an exponentially growing delay) and
    retries at most ``max_retries`` times — never a retry-storm. On 5xx it backs off
    once or twice. ``SpotSourceError`` carries the status, so the wrapper inspects the
    message rather than re-implementing HTTP.
    """

    def __init__(self, concurrency: int, max_retries: int = 3, timeout_s: float = 20.0) -> None:
        self.sem = asyncio.Semaphore(concurrency)
        self.max_retries = max_retries
        self.n_429 = 0
        self.total_wait_s = 0.0
        self.latencies_ms: list[float] = []
        self._client = HttpxSpotClient(timeout_s=timeout_s)

    def close(self) -> None:
        self._client.close()

    async def get_json(self, path: str, **params: Any) -> Any:
        async with self.sem:
            delay = 1.0
            for attempt in range(self.max_retries + 1):
                started = time.perf_counter()
                try:
                    payload = await asyncio.to_thread(
                        self._client.get_json, path, params or None
                    )
                    self.latencies_ms.append((time.perf_counter() - started) * 1000.0)
                    return payload
                except SpotSourceError as exc:
                    msg = str(exc)
                    if ("HTTP 429" in msg or "HTTP 418" in msg) and attempt < self.max_retries:
                        self.n_429 += 1
                        wait = min(delay, 60.0)
                        self.total_wait_s += wait
                        await asyncio.sleep(wait)
                        delay = min(delay * 2.0, 60.0)
                        continue
                    if "HTTP 5" in msg and attempt < self.max_retries:
                        self.total_wait_s += delay
                        await asyncio.sleep(delay)
                        delay = min(delay * 2.0, 60.0)
                        continue
                    raise
            raise SpotSourceError(f"{path}: exhausted retries")

    def latency_summary(self) -> dict[str, float | int | None]:
        lat = sorted(self.latencies_ms)
        if not lat:
            return {"n": 0, "min_ms": None, "median_ms": None, "p95_ms": None, "max_ms": None}
        return {
            "n": len(lat),
            "min_ms": round(lat[0], 1),
            "median_ms": round(lat[len(lat) // 2], 1),
            "p95_ms": round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 1),
            "max_ms": round(lat[-1], 1),
        }


def parse_book(payload: dict[str, Any]) -> Book:
    return Book(
        bids=[(float(p), float(q)) for p, q in payload.get("bids", [])],
        asks=[(float(p), float(q)) for p, q in payload.get("asks", [])],
    )


async def fetch_books(
    limiter: BackoffLimiter,
    symbols: list[str],
    limit: int,
) -> dict[str, Book | None]:
    async def one(sym: str) -> tuple[str, Book | None]:
        try:
            payload = await limiter.get_json("/api/v3/depth", symbol=sym, limit=limit)
            return sym, parse_book(payload)
        except Exception:  # noqa: BLE001 - a missing book must not kill the sweep
            return sym, None

    results = await asyncio.gather(*(one(s) for s in symbols))
    return dict(results)


# ---------------------------------------------------------------------- main


async def run(args: argparse.Namespace) -> int:
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    raw_dir = EVIDENCE_ROOT / f"run_{stamp}"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_ROOT / "latest_run.txt").write_text(raw_dir.name)
    wall_start = time.time()

    limiter = BackoffLimiter(args.concurrency)
    try:
        t0 = time.time()
        info_path = EVIDENCE_ROOT / "exchange_info.json"
        if info_path.exists() and not args.refresh_info:
            exchange_info = json.loads(info_path.read_text())
            info_src = "cache"
        else:
            exchange_info = await limiter.get_json("/api/v3/exchangeInfo")
            info_path.write_text(json.dumps(exchange_info))
            info_src = "live"
        t_info = time.time() - t0

        t1 = time.time()
        tickers = await limiter.get_json("/api/v3/ticker/24hr")
        t_tick = time.time() - t1

        markets = parse_symbols(exchange_info)
        apply_volumes(markets, tickers)

        triangles = discover_triangles(
            markets,
            usdt_volume_floor=args.usdt_floor,
            cross_volume_floor=args.cross_floor,
        )
        n_usdt_pairs = sum(
            1
            for m in markets.values()
            if m.quote == "USDT" and m.base != "USDT" and m.volume_usd >= args.usdt_floor
        )
        classes: dict[str, int] = {}
        for t in triangles:
            classes[t.klass] = classes.get(t.klass, 0) + 1

        print(
            f"[discovery] exchangeInfo={info_src} ({t_info:.1f}s) "
            f"tickers={len(tickers)} ({t_tick:.1f}s) markets={len(markets)} "
            f"usdt_pairs(>={args.usdt_floor:,.0f})={n_usdt_pairs} "
            f"triangles={len(triangles)} classes={classes}"
        )
        (raw_dir / "discovery.json").write_text(
            json.dumps(
                {
                    "usdt_volume_floor": args.usdt_floor,
                    "cross_volume_floor": args.cross_floor,
                    "n_markets": len(markets),
                    "n_usdt_pairs": n_usdt_pairs,
                    "n_triangles": len(triangles),
                    "classes": classes,
                    "taker_bps_per_leg": TAKER_FEES.taker_bps("spot"),
                    "maker_what_if_bps_per_leg": MAKER_RATE * 10_000,
                    "triangles": [
                        {
                            "name": t.name,
                            "path": list(t.path),
                            "legs": t.leg_symbols(),
                            "class": t.klass,
                            "min_volume_usd": t.min_volume_usd,
                        }
                        for t in triangles
                    ],
                },
                indent=2,
            )
        )

        if args.discover_only or not triangles:
            return 0

        start_quote = args.capital

        # ---- Phase A: broad liquidity sweep over EVERY enumerated triangle.
        sweep_symbols = sorted({s for t in triangles for s in t.leg_symbols()})
        print(
            f"[sweep] fetching {len(sweep_symbols)} books (concurrency={args.concurrency})..."
        )
        t2 = time.time()
        books = await fetch_books(limiter, sweep_symbols, args.depth_limit)
        t_sweep = time.time() - t2
        missing = [s for s, b in books.items() if b is None]
        complete_books = len(sweep_symbols) - len(missing)

        # Parallel local compute: per-triangle executable edge + max size at the taker fee.
        sweep_inputs = [(t, books) for t in triangles]
        sweep_rows = _parallel_sweep(sweep_inputs, TAKER_RATE, start_quote, args.workers)
        sweep_pos = [r for r in sweep_rows if (r["net_bps"] or 0) > 0]
        print(
            f"[sweep] {len(triangles)} triangles / {len(sweep_symbols)} books "
            f"({complete_books} with books, {len(missing)} missing) in {t_sweep:.1f}s "
            f"(wall {time.time() - t2:.1f}s); executable="
            f"{sum(1 for r in sweep_rows if r['net_bps'] is not None)} positive={len(sweep_pos)}"
        )
        (raw_dir / "sweep.json").write_text(
            json.dumps(
                {
                    "ts": time.time(),
                    "elapsed_s": t_sweep,
                    "n_triangles": len(triangles),
                    "n_books": len(sweep_symbols),
                    "n_books_present": complete_books,
                    "n_missing": len(missing),
                    "missing_symbols": missing,
                    "rows": sweep_rows,
                },
                indent=2,
            )
        )

        # ---- Phase B: time-series poll of the top candidates.
        if sweep_pos:
            sweep_pos.sort(key=lambda r: r["net_bps"], reverse=True)
            names = [r["triangle"] for r in sweep_pos][: args.top_n]
        else:
            names = [t.name for t in triangles[: args.top_n]]
        selected = [t for t in triangles if t.name in set(names)]
        poll_symbols = sorted({s for t in selected for s in t.leg_symbols()})
        est_weight = len(poll_symbols) * 2
        print(
            f"[poll] {len(selected)} triangles / {len(poll_symbols)} books "
            f"(~{est_weight} weight/cycle), every {args.interval}s for {args.minutes}min"
        )

        series: dict[str, list[float | None]] = {t.name: [] for t in selected}
        maker_series: dict[str, list[float | None]] = {t.name: [] for t in selected}
        max_size: dict[str, float] = {
            r["triangle"]: r["max_executable_size_usd"] for r in sweep_rows
        }
        samples_path = raw_dir / "samples.jsonl"
        deadline = time.time() + args.minutes * 60
        cycle = 0
        cycle_times: list[float] = []
        with samples_path.open("w") as fh:
            while time.time() < deadline:
                cycle += 1
                cycle_start = time.time()
                books = await fetch_books(limiter, poll_symbols, args.depth_limit)
                ts = time.time()
                for tri in selected:
                    result = simulate_triangle(start_quote, books, tri, TAKER_RATE)
                    bps = triangle_edge_bps(start_quote, result)
                    maker_bps = triangle_edge_bps(
                        start_quote, simulate_triangle(start_quote, books, tri, MAKER_RATE)
                    )
                    series[tri.name].append(bps)
                    maker_series[tri.name].append(maker_bps)
                    fh.write(
                        json.dumps(
                            {
                                "ts": ts,
                                "cycle": cycle,
                                "triangle": tri.name,
                                "net_bps": bps,
                                "maker_net_bps": maker_bps,
                                "executable": result.executable if result else False,
                                "final_usdt": round(result.total_usd, 6) if result else None,
                            }
                        )
                        + "\n"
                    )
                fh.flush()
                cycle_times.append(time.time() - cycle_start)
                if cycle % 10 == 1:
                    latest = (series[t.name][-1] for t in selected)
                    best_now = max(
                        (v for v in latest if v is not None), default=float("-inf")
                    )
                    print(
                        f"  cycle {cycle}: {len(poll_symbols)} books in "
                        f"{cycle_times[-1]:.1f}s, best now {best_now:.2f} bps"
                    )
                sleep = args.interval - (time.time() - cycle_start)
                if sleep > 0:
                    await asyncio.sleep(sleep)

        # ---- Parallel local compute: per-triangle time-series summaries.
        stat_inputs = [
            (
                t.name,
                t.path,
                t.klass,
                t.leg_symbols(),
                t.min_volume_usd,
                series[t.name],
                args.interval,
                max_size.get(t.name, 0.0),
            )
            for t in selected
        ]
        summaries = _parallel_summarize(stat_inputs, args.workers)
        maker_inputs = [
            (
                t.name,
                t.path,
                t.klass,
                t.leg_symbols(),
                t.min_volume_usd,
                maker_series[t.name],
                args.interval,
                max_size.get(t.name, 0.0),
            )
            for t in selected
        ]
        maker_summaries = {
            s["triangle"]: s for s in _parallel_summarize(maker_inputs, args.workers)
        }
        for s in summaries:
            s["maker_max_net_bps"] = maker_summaries[s["triangle"]]["max_net_bps"]
            s["maker_mean_net_bps"] = maker_summaries[s["triangle"]]["mean_net_bps"]
            s["maker_p90_net_bps"] = maker_summaries[s["triangle"]]["p90_net_bps"]
            s["maker_pct_samples_positive"] = maker_summaries[s["triangle"]][
                "pct_samples_positive"
            ]
            s["maker_longest_positive_run_samples"] = maker_summaries[s["triangle"]][
                "longest_positive_run_samples"
            ]
            s["maker_profit_usd_per_100_at_best"] = maker_summaries[s["triangle"]][
                "profit_usd_per_100_at_best"
            ]

        ever_positive = [s for s in summaries if (s["max_net_bps"] or -1) > 0]
        maker_ever_positive = [
            s for s in summaries if (s["maker_max_net_bps"] or -1) > 0
        ]
        latency = limiter.latency_summary()
        summary = {
            "generated_at": time.time(),
            "run_dir": str(raw_dir.relative_to(REPO)),
            "wall_clock_s": round(time.time() - wall_start, 1),
            "capital_usd": start_quote,
            "taker_fee_bps_per_leg": TAKER_FEES.taker_bps("spot"),
            "taker_fee_bps_round_trip": TAKER_FEES.taker_bps("spot") * LEGS,
            "maker_what_if_bps_per_leg": MAKER_RATE * 10_000,
            "maker_what_if_bps_round_trip": MAKER_RATE * 10_000 * LEGS,
            "fee_note": (
                "Headline uses FeeSchedule() defaults = Binance spot VIP0 taker, no BNB "
                "discount, no maker credit. Maker figures are an explicit what-if only."
            ),
            "capturability_caveat": (
                "A positive net-edge SAMPLE observed over REST is NOT evidence of "
                "capturable profit. REST round-trip here is the measured latency below "
                "(tens-to-hundreds of ms), while triangular edges of that size live for "
                "microseconds-to-milliseconds and top-of-book edges vanish at size. Every "
                "sample walks real depth for the full $100 (buy at ask, sell at bid); "
                "max_executable_size_usd reports the largest size that still filled. No "
                "positive sample is demonstrated to be capturable at this latency: this "
                "measures flicker, not profit."
            ),
            "rest_latency_ms": latency,
            "cycles": cycle,
            "window_minutes": args.minutes,
            "interval_s": args.interval,
            "mean_cycle_s": round(st.mean(cycle_times), 2) if cycle_times else None,
            "n_discovered_triangles": len(triangles),
            "n_books_fetched": len(sweep_symbols),
            "n_books_present": complete_books,
            "n_sampled_triangles": len(selected),
            "sweep_positive": len(sweep_pos),
            "triangles_ever_positive_taker": len(ever_positive),
            "triangles_ever_positive_maker": len(maker_ever_positive),
            "rate_limit_429s": limiter.n_429,
            "rate_limit_wait_s": round(limiter.total_wait_s, 1),
            "triangles": summaries,
        }
        (raw_dir / "summary.json").write_text(json.dumps(summary, indent=2))

        print(
            f"\n[done] {cycle} cycles, {len(selected)} triangles. "
            f"EVER positive net edge: taker={len(ever_positive)} "
            f"maker(what-if 2bp/leg)={len(maker_ever_positive)}. "
            f"429s={limiter.n_429} waited={limiter.total_wait_s:.1f}s "
            f"REST latency median={latency['median_ms']}ms p95={latency['p95_ms']}ms"
        )
        for s in sorted(
            summaries, key=lambda x: x["max_net_bps"] or -1e9, reverse=True
        )[:15]:
            print(
                f"  {s['triangle']:<14} max={s['max_net_bps']:>8.2f}bp "
                f"mean={s['mean_net_bps']:>8.2f}bp p90={s['p90_net_bps']:>8.2f}bp "
                f"pos={s['pct_samples_positive']:>5.1f}% run={s['longest_positive_run_samples']} "
                f"maxsz=${s['max_executable_size_usd']:.0f} "
                f"${s['profit_usd_per_100_at_best']:.4f}/$100 | "
                f"maker max={s['maker_max_net_bps']}bp"
            )
        print(f"\nEvidence: {raw_dir.relative_to(REPO)}")
        return 0
    finally:
        limiter.close()


def _sweep_one(
    item: tuple[Triangle, dict[str, Book]], fee_rate: float, start_quote: float
) -> dict[str, Any]:
    tri, books = item
    result = simulate_triangle(start_quote, books, tri, fee_rate)
    bps = triangle_edge_bps(start_quote, result)
    maker_bps = triangle_edge_bps(
        start_quote, simulate_triangle(start_quote, books, tri, MAKER_RATE)
    )
    size = (
        max_executable_size(books, tri, fee_rate, start=start_quote)
        if result and result.executable
        else 0.0
    )
    return {
        "triangle": tri.name,
        "class": tri.klass,
        "path": "->".join(tri.path),
        "legs": tri.leg_symbols(),
        "net_bps": round(bps, 3) if bps is not None else None,
        "maker_net_bps": round(maker_bps, 3) if maker_bps is not None else None,
        "final_usdt": round(result.total_usd, 6) if result else None,
        "executable": bool(result and result.executable),
        "max_executable_size_usd": round(size, 2),
    }


def _parallel_sweep(
    items: list[tuple[Triangle, dict[str, Book]]],
    fee_rate: float,
    start_quote: float,
    workers: int,
) -> list[dict[str, Any]]:
    """Run the per-triangle sweep in a process pool (falls back to serial on error)."""
    if workers <= 1 or len(items) < 8:
        return [_sweep_one(it, fee_rate, start_quote) for it in items]
    try:
        with mp.Pool(processes=workers) as pool:
            return pool.starmap(
                _sweep_one, [(it, fee_rate, start_quote) for it in items]
            )
    except Exception:  # noqa: BLE001 - pool problems must not lose the sweep
        return [_sweep_one(it, fee_rate, start_quote) for it in items]


def _parallel_summarize(inputs: list[tuple], workers: int) -> list[dict[str, Any]]:
    if workers <= 1 or len(inputs) < 8:
        return [_summarize_worker(i) for i in inputs]
    try:
        with mp.Pool(processes=workers) as pool:
            return pool.map(_summarize_worker, inputs)
    except Exception:  # noqa: BLE001
        return [_summarize_worker(i) for i in inputs]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--minutes", type=float, default=45.0)
    p.add_argument("--interval", type=float, default=6.0)
    p.add_argument("--capital", type=float, default=100.0)
    p.add_argument("--usdt-floor", type=float, default=1_000_000.0)
    p.add_argument("--cross-floor", type=float, default=100_000.0)
    p.add_argument("--depth-limit", type=int, default=20)
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--top-n", type=int, default=50)
    p.add_argument("--discover-only", action="store_true")
    p.add_argument("--refresh-info", action="store_true")
    return asyncio.run(run(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
