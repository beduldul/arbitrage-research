"""Shared pure math for the two spot-only arbitrage measurement scripts.

**Measurement only.** No orders, no keys, no account state, no writes. Everything in
this module is a *pure function* of order-book data, so it is unit-testable without a
network or a filesystem. The two consumers are:

* :mod:`arb_stable_crossrate` — class A, stablecoin cross-rate / depeg, within one venue.
* :mod:`arb_crossvenue_spot`   — class B, the same pair's book on two venues.

**Bid/ask, never mid.** An edge only exists if it is *executable*: you BUY at the ask
and SELL at the bid. A mid-price model can manufacture a profit that the spread eats, so
no function here ever reads a mid. This mirrors ``scripts/arb_triangular_scan.py``.

**Fees come from the project, not from here.** The default leg fees are the project's
own §10.2 base tier (``crypto_brain.engine.fees.FeeSchedule``: spot taker 10 bps). The
optimistic maker what-if (2 bps/leg) is named explicitly and reported as a separate
column — it is never the headline.
"""

from __future__ import annotations

import statistics as st
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "BPS",
    "Book",
    "FeeTier",
    "TAKER",
    "MAKER",
    "best_bid",
    "best_ask",
    "longest_positive_run",
    "max_positive_quote",
    "parse_binance",
    "parse_gate",
    "parse_htx",
    "percentile",
    "round_trip_gross_bps",
    "round_trip_net_bps",
    "summarize",
    "triangle_net_bps",
    "walk_buy_asks",
    "walk_sell_bids",
]

#: Basis points per unit fraction. ``1.0 -> 10_000 bps``.
BPS = 10_000.0

Side = Literal["buy", "sell"]


# --------------------------------------------------------------------- books


def _levels(raw: Sequence[Sequence[Any]] | None) -> list[tuple[float, float]]:
    """Coerce a venue's ``[[price, qty], ...]`` into ``[(float, float), ...]``.

    Drops malformed rows rather than crashing mid-sample: a single bad level must not
    kill a 20-minute collection window.
    """
    out: list[tuple[float, float]] = []
    for row in raw or []:
        try:
            price, qty = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if price > 0 and qty > 0:
            out.append((price, qty))
    return out


@dataclass(frozen=True, slots=True)
class Book:
    """One order book, normalised best-first (bids descending, asks ascending)."""

    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def empty(self) -> bool:
        return not self.bids or not self.asks


def best_bid(book: Book) -> float | None:
    return book.best_bid


def best_ask(book: Book) -> float | None:
    return book.best_ask


def parse_binance(payload: dict[str, Any]) -> Book:
    """Binance ``/api/v3/depth``: ``{"bids": [["p","q"]], "asks": [...]}``."""
    return Book(
        bids=sorted(_levels(payload.get("bids")), key=lambda x: -x[0]),
        asks=sorted(_levels(payload.get("asks")), key=lambda x: x[0]),
    )


def parse_gate(payload: dict[str, Any]) -> Book:
    """Gate ``/api/v4/spot/order_book``: ``{"asks": [["p","q"]], "bids": [...]}``."""
    return Book(
        bids=sorted(_levels(payload.get("bids")), key=lambda x: -x[0]),
        asks=sorted(_levels(payload.get("asks")), key=lambda x: x[0]),
    )


def parse_htx(payload: dict[str, Any]) -> Book:
    """HTX ``/market/depth``: ``{"tick": {"bids": [["p","q"]], "asks": [...]}}``."""
    tick = payload.get("tick") or {}
    return Book(
        bids=sorted(_levels(tick.get("bids")), key=lambda x: -x[0]),
        asks=sorted(_levels(tick.get("asks")), key=lambda x: x[0]),
    )


# ---------------------------------------------------------------------- fees


@dataclass(frozen=True, slots=True)
class FeeTier:
    """A per-leg fee assumption, in basis points, for one venue.

    ``verified`` is **False** whenever the number was not read from the venue's own
    published schedule during this run — which, on this host, is *every* venue: the
    fee-schedule pages are unreachable (see the reachability matrix in the report). The
    headline therefore uses the project's own ``FeeSchedule`` value, and the venue-stated
    column is marked UNVERIFIED rather than silently trusted.
    """

    venue: str
    taker_bps: float
    maker_bps: float
    tier: str = "base/VIP0"
    verified: bool = False
    note: str = ""

    def leg_bps(self, *, maker: bool = False) -> float:
        return self.maker_bps if maker else self.taker_bps

    def round_trip_bps(self, *, maker: bool = False) -> float:
        """Two legs (in and out) at this venue's rate."""
        return 2.0 * self.leg_bps(maker=maker)


#: The project's §10.2 base tier: spot taker 10 bps. Used as the headline because the
#: project's own cost gate reads it (``engine.fees.FeeSchedule`` defaults).
TAKER = FeeTier("project-base", taker_bps=10.0, maker_bps=10.0, tier="vip0_taker",
                verified=False, note="DESIGN.md §10.2 base tier; spot maker == taker")

#: The optimistic maker what-if named in the brief (0.02%/leg). NOT the project default
#: for spot (which is 10 bps both ways); reported as an explicit lower bound.
MAKER = FeeTier("optimistic-maker", taker_bps=10.0, maker_bps=2.0, tier="maker",
                verified=False, note="0.02%/leg maker what-if; requires resting fills")


# ------------------------------------------------------------------- walking


def walk_buy_asks(
    asks: Sequence[tuple[float, float]], quote_amount: float, fee_rate: float
) -> float | None:
    """Spend ``quote_amount`` walking ``asks``; return base received net of fee.

    ``None`` when the book cannot absorb the whole quote amount (not executable at this
    size) — never a phantom fill against depth that is not there.
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
    bids: Sequence[tuple[float, float]], base_amount: float, fee_rate: float
) -> float | None:
    """Sell ``base_amount`` walking ``bids``; return quote received net of fee.

    ``None`` when the book cannot absorb the whole base amount.
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


# ------------------------------------------------------------- round-trip edge


def round_trip_net_bps(
    book: Book, notional_quote: float, fee: FeeTier, *, maker: bool = False
) -> float | None:
    """Buy at the ask, immediately sell the result at the bid, at ``notional_quote``.

    Returns net basis points of the starting notional, or ``None`` when the book cannot
    absorb the size. This is the class-A instrument: for a pegged pair it is *always*
    negative (the ask is above the bid), and its size is the spread plus fees. A genuine
    depeg shows up as this number becoming *less* negative, or as a positive
    cross-rate triangle (:func:`triangle_net_bps`).
    """
    rate = fee.leg_bps(maker=maker) / BPS
    base = walk_buy_asks(book.asks, notional_quote, rate)
    if base is None:
        return None
    proceeds = walk_sell_bids(book.bids, base, rate)
    if proceeds is None:
        return None
    return (proceeds - notional_quote) / notional_quote * BPS


def round_trip_gross_bps(book: Book, notional_quote: float) -> float | None:
    """Same walk with zero fees — the raw book-to-book spread, for reference only."""
    base = walk_buy_asks(book.asks, notional_quote, 0.0)
    if base is None:
        return None
    proceeds = walk_sell_bids(book.bids, base, 0.0)
    if proceeds is None:
        return None
    return (proceeds - notional_quote) / notional_quote * BPS


def triangle_net_bps(
    legs: Sequence[tuple[Book, Side]],
    start_quote: float,
    fee: FeeTier,
    *,
    maker: bool = False,
) -> float | None:
    """Walk a 3-leg ``quote -> A -> B -> quote`` cycle and return net bps.

    Each leg is ``(book, side)`` where ``side="buy"`` spends the running amount as quote
    on the asks and ``side="sell"`` sells the running base amount into the bids. The
    running amount is always the *output* of the previous leg, so depth compounds the
    way it does in reality. ``None`` if any leg cannot fill.
    """
    rate = fee.leg_bps(maker=maker) / BPS
    amount = start_quote
    for book, side in legs:
        if side == "buy":
            out = walk_buy_asks(book.asks, amount, rate)
        else:
            out = walk_sell_bids(book.bids, amount, rate)
        if out is None:
            return None
        amount = out
    return (amount - start_quote) / start_quote * BPS


def max_positive_quote(
    edge_at: Callable[[float], float | None],
    *,
    cap_quote: float = 1.0e7,
    iters: int = 48,
) -> float:
    """Largest notional whose net edge is still positive, by bisection.

    ``edge_at`` maps a notional to net bps (or ``None`` when unfillable). Net edge is
    monotone non-increasing in size (walking deeper only gets worse), so bisection is
    exact to ``cap_quote / 2**iters``. Returns ``0.0`` when even a small size is
    non-positive or unfillable, and ``cap_quote`` when the edge survives the cap.
    """
    if cap_quote <= 0:
        return 0.0
    top = edge_at(cap_quote)
    if top is not None and top > 0:
        return cap_quote
    lo, hi = 0.0, cap_quote
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        edge = edge_at(mid)
        if edge is not None and edge > 0:
            lo = mid
        else:
            hi = mid
    return lo


# ------------------------------------------------------------------- stats


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Linear-interpolated percentile; ``None`` for an empty series."""
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    rank = (pct / 100.0) * (len(clean) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(clean) - 1)
    frac = rank - lo
    return clean[lo] * (1.0 - frac) + clean[hi] * frac


def longest_positive_run(series: Sequence[float | None]) -> int:
    """Longest consecutive run of strictly-positive samples (``None`` breaks it)."""
    best = run = 0
    for value in series:
        if value is not None and value > 0:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def summarize(series: Sequence[float | None]) -> dict[str, Any]:
    """Distribution stats for one metric's time series.

    ``n`` counts *attempted* samples; ``n_exec`` counts fillable ones. ``% positive`` is
    over attempted samples, so a window that is unfillable half the time cannot look
    better than it is.
    """
    attempted = len(series)
    clean = [v for v in series if v is not None]
    positives = [v for v in clean if v > 0]
    return {
        "n": attempted,
        "n_exec": len(clean),
        "n_unfillable": attempted - len(clean),
        "max_bps": round(max(clean), 4) if clean else None,
        "mean_bps": round(st.fmean(clean), 4) if clean else None,
        "median_bps": round(st.median(clean), 4) if clean else None,
        "p90_bps": round(percentile(clean, 90), 4) if clean else None,
        "min_bps": round(min(clean), 4) if clean else None,
        "pct_positive": round(100.0 * len(positives) / attempted, 2) if attempted else None,
        "longest_positive_run": longest_positive_run(series),
    }
