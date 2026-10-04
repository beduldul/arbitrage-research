"""S9 — ``round_trip_cost_pct`` per symbol and order size (DESIGN.md §10.3).

This is the number the cost gate reads, and §7.6 is explicit that it is **never
model-supplied**: *"``round_trip_cost_pct`` is computed by S9. The model may reference
it, never restate it."*

The formula is §10.3 verbatim:

.. code-block:: text

    round_trip_cost_pct =
        2 * taker_fee_pct
      + 2 * half_spread_pct
      + entry_slippage_pct
      + exit_slippage_pct
      + expected_funding_pct          # 0 spot; rate * (hold / interval) perps

**§21.3 status: EXTENDED, NOT ADOPTED.** NEXUS has no equivalent function. Its cost
handling is split between a rejection cap (``max_slippage_pct``), a flat backtest
constant (``backtest_engine.py:62`` ``SLIPPAGE_PCT = 0.0002``) and a gate-only net-edge
check (``trade_quality_gate.py:133-146``) — none of which produces a per-symbol,
per-size round-trip figure, and none of which charges funding. This module is new work.
"""

from __future__ import annotations

from dataclasses import dataclass

from .fees import FeeSchedule, Mode
from .slippage import (
    SymbolCostProfile,
    entry_slippage_bps,
    exit_slippage_bps,
)

__all__ = ["CostBreakdown", "round_trip_cost_pct"]


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """The round-trip cost, itemised in **percent** so every term is auditable.

    §5.3 requires the dashboard to show gross/fees/funding/net separately; the same
    discipline applies to the *forecast* cost, so the gate's rejection reason can name
    the term that killed it rather than reporting one opaque number.
    """

    symbol: str
    order_notional: float
    mode: Mode
    fees_pct: float
    spread_pct: float
    entry_slippage_pct: float
    exit_slippage_pct: float
    funding_pct: float
    total_pct: float
    excluded: bool = False
    reason: str | None = None

    @property
    def slippage_pct(self) -> float:
        return self.entry_slippage_pct + self.exit_slippage_pct

    def as_dict(self) -> dict[str, float]:
        """A flat numeric view for the ``NumberLedger`` (§7.2)."""
        return {
            "fees_pct": self.fees_pct,
            "spread_pct": self.spread_pct,
            "entry_slippage_pct": self.entry_slippage_pct,
            "exit_slippage_pct": self.exit_slippage_pct,
            "slippage_pct": self.slippage_pct,
            "funding_pct": self.funding_pct,
            "total_pct": self.total_pct,
        }


def round_trip_cost_pct(
    profile: SymbolCostProfile,
    order_notional: float,
    *,
    mode: Mode,
    fees: FeeSchedule,
    slippage_k: float,
    exit_multiplier: float,
    expected_funding_rate: float = 0.0,
    hold_hours: float = 8.0,
    funding_interval_hours: float = 8.0,
    include_funding: bool = True,
) -> CostBreakdown:
    """Compute the full round-trip cost of one position in percent of notional.

    Args:
        profile: the per-symbol slippage/spread parameters.
        order_notional: the order size in quote currency — slippage is size-dependent.
        mode: ``spot`` or ``futures``; spot pays no funding.
        fees: the configured fee schedule.
        slippage_k: §10.3's ``k`` (default 0.5 in config).
        exit_multiplier: §10.3's exit stress multiplier (default 1.5 in config).
        expected_funding_rate: the per-interval funding rate as a fraction
            (``0.0001`` = 0.01%). Signed: a positive rate is a cost for longs.
        hold_hours: the expected holding period, which scales the funding term.
        funding_interval_hours: the settlement interval (8h on Binance USD-M).
        include_funding: when ``False`` the funding term is zeroed — used by tests to
            isolate the other terms, and by spot (where it is not applicable at all).

    Returns:
        A :class:`CostBreakdown`. When the symbol is below the universe floor the
        breakdown is still returned with ``excluded=True`` so the gate can reject it
        with a specific reason rather than a division by a fabricated cost.
    """
    if order_notional < 0:
        raise ValueError(f"order_notional must be non-negative, got {order_notional!r}")

    taker_pct = fees.taker_bps(mode) / 100.0
    fees_pct = 2.0 * taker_pct

    spread_pct = 2.0 * profile.half_spread_bps / 100.0

    entry_bps = entry_slippage_bps(profile, order_notional, slippage_k)
    exit_bps = exit_slippage_bps(profile, order_notional, slippage_k, exit_multiplier)
    entry_pct = entry_bps / 100.0
    exit_pct = exit_bps / 100.0

    funding_pct = 0.0
    if mode == "futures" and include_funding and funding_interval_hours > 0:
        # §10.3: funding_rate * (hold_time / funding_interval). The rate is signed, so
        # a short receiving funding gets a negative (i.e. negative-cost) term.
        funding_pct = expected_funding_rate * (hold_hours / funding_interval_hours) * 100.0

    total = fees_pct + spread_pct + entry_pct + exit_pct + funding_pct

    return CostBreakdown(
        symbol=profile.symbol,
        order_notional=order_notional,
        mode=mode,
        fees_pct=fees_pct,
        spread_pct=spread_pct,
        entry_slippage_pct=entry_pct,
        exit_slippage_pct=exit_pct,
        funding_pct=funding_pct,
        total_pct=total,
        excluded=profile.excluded,
        reason=profile.reason,
    )
