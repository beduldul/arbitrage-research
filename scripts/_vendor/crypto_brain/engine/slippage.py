"""S9 — Per-symbol, per-fill slippage model (DESIGN.md §10.3).

**This module is the §21.3 correction. Read this before changing it.**

NEXUS's ``max_slippage_pct`` (``0.0005``, ``config/agents.yaml:47``) is used in
``exchange_adapter.py:151`` and applied at ``:160-163`` (clamp the limit price) and
``:229-235`` (abort the order if the market has drifted past the cap). Verified
negative proof: ``grep slippage src/engine/paper_trader.py`` returns **zero** matches —
no slippage term exists in NEXUS's entry fee, exit fee, liquidation or balance formula.
It is a **rejection cap, not a cost**. Adopting it would make paper PnL optimistic,
which §10.1 forbids.

What we implement instead is §10.3's shape:

.. code-block:: text

    base_bps(symbol)     = f(24h quote volume)          # volume tiers, below
    entry_slippage_bps   = base_bps + k * (order_notional / depth_within_50bps)
    exit_slippage_bps    = exit_multiplier * entry_slippage_bps     # default 1.5x

The **1.5× exit multiplier** is described by §10.3 as *"the most important realism
knob"*: strategies systematically exit into weakness, and a symmetric model is the
classic source of fake paper profits.

**Depth estimation.** §10.3 says ``depth_within_50bps`` comes from the live book
*"where available; otherwise estimated from 24h volume and spread."* In Phase 1 no book
is available (NEXUS's DB has none and the exchange REST is unreachable), so we estimate
it as a fixed fraction of 24h quote volume and record the assumption explicitly in the
returned profile so it is auditable rather than buried. The half-spread is likewise
estimated per volume tier. Both are the honest kind of estimate: stated, sourced, and
revisable in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "DEPTH_FRACTION_OF_DAILY_VOLUME",
    "VOLUME_TIERS",
    "SymbolCostProfile",
    "entry_slippage_bps",
    "exit_slippage_bps",
]

#: Fraction of 24h quote volume assumed to rest within 50 bps of mid, used only when
#: no order book is available (§10.3's "otherwise estimated" branch). 0.2% of daily
#: volume is the order of magnitude for a major pair's ±50 bps band; it is deliberately
#: *conservative* (a smaller depth makes slippage larger, which makes paper results
#: worse, not better — §10.1).
DEPTH_FRACTION_OF_DAILY_VOLUME = 0.002

#: ``(min_24h_quote_volume_usd, base_bps, half_spread_bps)``, highest tier first.
#: base_bps is verbatim from §10.3. half_spread_bps is our estimate of one side of the
#: spread, scaled with the same liquidity ordering (§5.1 gives futures half-spread as
#: 0.5-5 bps).
VOLUME_TIERS: tuple[tuple[float, float, float], ...] = (
    (1_000_000_000.0, 1.0, 0.5),   # > $1B     -> majors (BTC, ETH)
    (100_000_000.0, 2.0, 1.0),     # $100M-$1B
    (10_000_000.0, 5.0, 2.5),      # $10M-$100M
    (1_000_000.0, 12.0, 5.0),      # $1M-$10M  -> thin alts
)

#: Below this the symbol is excluded from the universe entirely (§10.3, §5.3 item 4).
MIN_QUOTE_VOLUME_USD = 1_000_000.0


@dataclass(frozen=True, slots=True)
class SymbolCostProfile:
    """The per-symbol inputs to the slippage and cost models.

    Built once per cycle from data and cached in the ``NumberLedger``, so the cost gate
    and the fill model read the *same* numbers (§7.6: cost is never model-supplied).
    """

    symbol: str
    quote_volume_24h_usd: float
    base_bps: float
    half_spread_bps: float
    depth_usd: float
    excluded: bool = False
    reason: str | None = None
    assumptions: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_volume(
        cls,
        symbol: str,
        quote_volume_24h_usd: float,
        *,
        depth_fraction: float = DEPTH_FRACTION_OF_DAILY_VOLUME,
    ) -> SymbolCostProfile:
        """Classify a symbol by 24h quote volume and derive its cost parameters."""
        volume = max(0.0, float(quote_volume_24h_usd))
        for minimum, base_bps, half_spread_bps in VOLUME_TIERS:
            if volume >= minimum:
                return cls(
                    symbol=symbol,
                    quote_volume_24h_usd=volume,
                    base_bps=base_bps,
                    half_spread_bps=half_spread_bps,
                    depth_usd=max(volume * depth_fraction, 1.0),
                    assumptions={
                        "depth_fraction_of_daily_volume": depth_fraction,
                        "depth_source": 0.0,  # 0 = estimated, 1 = live book
                    },
                )
        return cls(
            symbol=symbol,
            quote_volume_24h_usd=volume,
            base_bps=VOLUME_TIERS[-1][1],
            half_spread_bps=VOLUME_TIERS[-1][2],
            depth_usd=max(volume * depth_fraction, 1.0),
            excluded=True,
            reason=(
                f"24h quote volume ${volume:,.0f} is below the ${MIN_QUOTE_VOLUME_USD:,.0f} "
                "universe floor (DESIGN.md §10.3) — the worst slippage regime, excluded "
                "structurally rather than priced"
            ),
            assumptions={"depth_fraction_of_daily_volume": depth_fraction, "depth_source": 0.0},
        )

    @classmethod
    def with_book(
        cls,
        symbol: str,
        quote_volume_24h_usd: float,
        *,
        depth_usd: float,
        half_spread_bps: float,
    ) -> SymbolCostProfile:
        """The live-book path of §10.3, used when depth data exists (Phase 2+).

        Kept here so the estimation branch is not mistaken for the model itself: when a
        book is available the measured values win.
        """
        tier = cls.from_volume(symbol, quote_volume_24h_usd)
        return cls(
            symbol=symbol,
            quote_volume_24h_usd=tier.quote_volume_24h_usd,
            base_bps=tier.base_bps,
            half_spread_bps=half_spread_bps,
            depth_usd=max(depth_usd, 1.0),
            excluded=tier.excluded,
            reason=tier.reason,
            assumptions={"depth_fraction_of_daily_volume": 0.0, "depth_source": 1.0},
        )


def entry_slippage_bps(profile: SymbolCostProfile, order_notional: float, k: float) -> float:
    """``base_bps + k * (order_notional / depth_within_50bps)`` (§10.3).

    The book-impact term is what makes a large order on a thin book cost more than a
    small order on a deep one — the property a flat constant cannot express.
    """
    if profile.depth_usd <= 0:
        raise ValueError(f"{profile.symbol}: depth_usd must be positive")
    impact = k * (max(0.0, order_notional) / profile.depth_usd)
    return profile.base_bps + impact


def exit_slippage_bps(
    profile: SymbolCostProfile,
    order_notional: float,
    k: float,
    multiplier: float = 1.5,
) -> float:
    """``multiplier * entry_slippage_bps`` (§10.3). Default 1.5 — exits happen in stress."""
    if multiplier < 1.0:
        raise ValueError(
            f"exit slippage multiplier must be >= 1.0 (got {multiplier}); a symmetric "
            "model is the classic source of fake paper profits (DESIGN.md §10.3)"
        )
    return multiplier * entry_slippage_bps(profile, order_notional, k)
