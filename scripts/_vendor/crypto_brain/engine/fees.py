"""S6 — Fee schedule (DESIGN.md §10.2).

**Reuse verdict (§21.2): the *values* are correct in NEXUS, the *structure* is not.**
NEXUS hardcodes ``MAKER_FEE_PCT = 0.02/100`` and ``TAKER_FEE_PCT = 0.04/100`` as
literals in ``paper_trader.py:47-48``, again in ``exit_rules.py:18``, again in
``grid_trader.py:13-14`` and again in ``backtest_engine.py:58-60`` — four copies with
no single source of truth, plus a ``paper_trading.fee_rate`` config key that is
validated (``config_loader.py:157-160``) and **never read**. That is the shape we do
not adopt.

We take the fee *values* from ``config.yaml``'s ``engine:`` section (which already
exists) and put them behind one object, so the paper engine, the cost model and the
gate cannot disagree about what a fee is.

**Base-tier taker fees only.** §10.2: *"We do **not** assume BNB discounts, VIP tiers,
or maker rebates — the user does not have them."* Maker credit requires resting proof
(§10.8), which the fill model enforces separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

__all__ = ["BPS_PER_PCT", "FeeSchedule", "bps_to_pct", "pct_to_bps"]

#: One basis point is 0.01%, i.e. 100 bps = 1%.
BPS_PER_PCT = 100.0

Mode = Literal["spot", "futures"]


def bps_to_pct(bps: float) -> float:
    """Basis points -> percent. ``5 bps -> 0.05``."""
    return bps / BPS_PER_PCT


def pct_to_bps(pct: float) -> float:
    """Percent -> basis points. ``0.05 -> 5``."""
    return pct * BPS_PER_PCT


class _EngineConfigLike(Protocol):
    """The subset of ``config.engine`` this module needs (structural, so tests can
    pass a stub without importing the config package)."""

    spot_taker_fee_bps: float
    futures_taker_fee_bps: float
    futures_maker_fee_bps: float


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Fee rates in basis points, per market.

    Defaults are DESIGN.md §10.2's base tier: 0.10% spot taker, 0.05% futures taker,
    0.02% futures maker.
    """

    spot_taker_bps: float = 10.0
    spot_maker_bps: float = 10.0
    futures_taker_bps: float = 5.0
    futures_maker_bps: float = 2.0

    @classmethod
    def from_config(cls, engine: _EngineConfigLike) -> FeeSchedule:
        """Build from ``config.yaml``'s ``engine:`` section.

        Spot has no maker key in the config because §10.2 prices spot maker == taker
        at the base tier (0.10% both ways).
        """
        return cls(
            spot_taker_bps=engine.spot_taker_fee_bps,
            spot_maker_bps=engine.spot_taker_fee_bps,
            futures_taker_bps=engine.futures_taker_fee_bps,
            futures_maker_bps=engine.futures_maker_fee_bps,
        )

    def taker_bps(self, mode: Mode) -> float:
        return self.spot_taker_bps if mode == "spot" else self.futures_taker_bps

    def maker_bps(self, mode: Mode) -> float:
        return self.spot_maker_bps if mode == "spot" else self.futures_maker_bps

    def rate(self, mode: Mode, *, maker: bool = False) -> float:
        """The fee as a fraction of notional (``5 bps -> 0.0005``)."""
        return (self.maker_bps(mode) if maker else self.taker_bps(mode)) / 10_000.0

    def fee(self, notional: float, mode: Mode, *, maker: bool = False) -> float:
        """The fee in quote currency for a given notional.

        ``maker=True`` must only be passed when a resting limit was provably filled by
        a later trade-through (§10.5 adverse-fill rule); the paper engine gates this,
        it is not a caller convention.
        """
        if notional < 0:
            raise ValueError(f"notional must be non-negative, got {notional!r}")
        return notional * self.rate(mode, maker=maker)
