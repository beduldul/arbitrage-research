"""Reverse-engineer the public claim "crypto arbitrage earns $0.4–$0.7 per minute".

**Research only.** This script touches no orders, no keys, no account state, no
network. Every number it prints is a *pure function* of the claim and a stated
return/fee assumption, so the arithmetic is reproducible and auditable.

It answers four questions:

1. **Normalisation** — what does $0.4/min mean per hour / day / month / year?
2. **Required capital** — at a given net return, how much capital is needed to
   produce that cash flow? (And the headline: what must a $100 account earn?)
3. **Required volume/edge** — at a given net edge per trade, how much notional
   must turn over per minute, and what fee tier makes it possible?
4. **Mechanisms** — which real-world mechanisms could produce the cash flow, what
   they require, and whether a $100 retail account can access them.

The project's own §10.2 base tier is spot taker 10 bps/leg (20 bps round trip) and
futures maker 2 bps/leg (4 bps round trip); ``engine/fees.py`` is the source of
truth. ``venue_minimums.json`` (2026-10-03) measured taker round-trip break-even
edges of 23.5–60 bps for $100 tickets, and ``evidence/arbitrage/2026-10-03``
measured *gross* dislocations of only 1–11 bps — below the fee floor.

Run::

    uv run python scripts/arb_claim_math.py
"""

from __future__ import annotations

from dataclasses import dataclass

#: The two endpoints of the public claim, in USD per minute.
CLAIM_PER_MIN = (0.4, 0.7)

#: Minutes in the units we normalise to. Month = 30 days (a stated convention, not
#: a calendar fact); year = 365 days.
MIN_PER_HOUR = 60.0
MIN_PER_DAY = 24.0 * MIN_PER_HOUR  # 1440
MIN_PER_MONTH = 30.0 * MIN_PER_DAY  # 43_200
MIN_PER_YEAR = 365.0 * MIN_PER_DAY  # 525_600

#: Net return assumptions for the capital table.
ANNUAL_RATES = (0.05, 0.20, 0.50, 1.00, 2.00, 5.00)
DAILY_RATES = (0.05, 0.20)

#: Net edge per trade, in basis points, for the volume table.
NET_EDGES_BPS = (1.0, 5.0, 10.0, 20.0, 50.0)

#: Fee tiers, in bps per leg. Round trip = 2 legs.
#: ``taker`` = the project's spot base tier (10 bps). ``maker`` = Binance spot
#: maker 2 bps (the analyst-supplied what-if). ``rebate`` = a high-volume
#: negative-maker tier (−0.5 bps, i.e. the venue pays you).
FEE_TIERS_BPS_PER_LEG = {"taker": 10.0, "maker": 2.0, "rebate": -0.5}

#: Measured gross dislocation band from evidence/arbitrage/2026-10-03.
MEASURED_GROSS_EDGE_BPS = (1.0, 11.0)

#: The retail account the project measured at.
RETAIL_CAPITAL_USD = 100.0

#: Measured single-venue funding carry: ~$0.05/day per $100 (DECISIVE_REPORT.md).
FUNDING_DAILY_PER_100 = 0.05


def funding_capital_for_target(daily_target_usd: float) -> float:
    """Capital needed for a funding-carry-only strategy to hit a daily target.

    Carry yield is ``FUNDING_DAILY_PER_100 / 100`` per day on capital, so
    ``capital = daily_target / yield``. This is the arithmetic behind the
    "funding at scale" mechanism.
    """
    yield_per_day = FUNDING_DAILY_PER_100 / 100.0
    return daily_target_usd / yield_per_day


# --------------------------------------------------------------- conversions


def normalise(usd_per_min: float) -> dict[str, float]:
    """Convert a per-minute cash flow into hour/day/month/year, all derived."""
    return {
        "per_min": usd_per_min,
        "per_hour": usd_per_min * MIN_PER_HOUR,
        "per_day": usd_per_min * MIN_PER_DAY,
        "per_month": usd_per_min * MIN_PER_MONTH,
        "per_year": usd_per_min * MIN_PER_YEAR,
    }


def daily_rate_from_annual(annual: float) -> float:
    """Compound the annual rate down to one day: ``(1+r)^(1/365) - 1``."""
    return (1.0 + annual) ** (1.0 / 365.0) - 1.0


def capital_for_daily_target(daily_target_usd: float, daily_rate: float) -> float:
    """Capital whose *daily net return* equals ``daily_target_usd``.

    ``capital = daily_target / daily_rate``. A zero or negative rate has no
    finite answer, so it is rejected rather than silently returning ``inf``.
    """
    if daily_rate <= 0.0:
        raise ValueError(f"daily_rate must be positive, got {daily_rate!r}")
    return daily_target_usd / daily_rate


def daily_pct_for_capital(capital_usd: float, daily_target_usd: float) -> float:
    """The %/day a given capital must earn to produce ``daily_target_usd``."""
    if capital_usd <= 0.0:
        raise ValueError(f"capital_usd must be positive, got {capital_usd!r}")
    return daily_target_usd / capital_usd


# ------------------------------------------------------------ volume and edge


def notional_per_min(net_edge_bps: float, target_per_min_usd: float) -> float:
    """Notional that must turn over per minute at a given *net* edge.

    ``notional = target / (edge_bps / 10_000)``. Net edge already has fees
    subtracted; this is the volume that yields the target *after* costs.
    """
    if net_edge_bps <= 0.0:
        raise ValueError(f"net_edge_bps must be positive, got {net_edge_bps!r}")
    return target_per_min_usd / (net_edge_bps / 10_000.0)


def gross_edge_required_bps(net_edge_bps: float, fee_bps_per_leg: float) -> float:
    """Gross dislocation needed so that *net* edge survives a 2-leg fee.

    ``gross = net + 2 * fee_per_leg``. With a negative maker fee this is smaller
    than the net edge — the venue's rebate is itself part of the edge.
    """
    return net_edge_bps + 2.0 * fee_bps_per_leg


def tier_can_reach(net_edge_bps: float, fee_bps_per_leg: float) -> bool:
    """Whether a tier is *possible at all*: the required gross edge must be
    non-negative (a negative gross would mean selling below the buy price)."""
    return gross_edge_required_bps(net_edge_bps, fee_bps_per_leg) >= 0.0


# --------------------------------------------------------------- mechanisms


def _money(x: float) -> str:
    return f"${x:,.0f}" if abs(x) >= 1000 else f"${x:,.2f}"


@dataclass(frozen=True, slots=True)
class Mechanism:
    """One candidate mechanism for a $0.4–0.7/min cash flow."""

    name: str
    prerequisites: str
    retail_100_accessible: bool
    note: str


MECHANISMS: tuple[Mechanism, ...] = (
    Mechanism(
        "CEX-DEX arb, colocated + private mempool",
        "colocated node/RPC, private tx relay, on-chain capital, gas war budget",
        False,
        "latency measured in ms; $100 cannot pay one month of colocation",
    ),
    Mechanism(
        "MEV searcher (EVM/Solana)",
        "validator/relay relationships, bundle-building infra, refundable stake",
        False,
        "searcher profit accrues to those who win blocks, not to a retail taker",
    ),
    Mechanism(
        "Market making at scale, maker rebates",
        "inventory across venues, quoting engine, negative-maker fee tier",
        False,
        "rebates only exist above very high 30-day volume; requires inventory",
    ),
    Mechanism(
        "Funding/basis at scale",
        "$1M+ perp notional, cross-venue hedges, margin buffers",
        False,
        "measured ~$0.05/day per $100 = $0.000035/min; needs "
        f"{_money(funding_capital_for_target(normalise(0.4)['per_day']))} for $0.4/min",
    ),
    Mechanism(
        "Cross-exchange latency arb",
        "pre-positioned balances on many venues, low-latency links, fee tiers",
        False,
        "capital is split across venues, so effective notional is far below gross",
    ),
    Mechanism(
        "Prop-desk / institutional fee tiers",
        "high-volume tier (negative maker), colocation, risk desk",
        False,
        "the negative fee *is* the edge; unavailable below volume thresholds",
    ),
)


# ------------------------------------------------------------------ printing


def print_normalisation() -> None:
    print("=" * 78)
    print("1. NORMALISATION OF THE CLAIM")
    print("=" * 78)
    print(f"{'claim':>10} {'/hour':>12} {'/day':>12} {'/month(30d)':>14} {'/year(365d)':>14}")
    for per_min in CLAIM_PER_MIN:
        n = normalise(per_min)
        print(
            f"${per_min:>9.2f} {_money(n['per_hour']):>12} {_money(n['per_day']):>12} "
            f"{_money(n['per_month']):>14} {_money(n['per_year']):>14}"
        )
    print()


def print_capital_table() -> None:
    print("=" * 78)
    print("2. REQUIRED CAPITAL (capital = daily_target / daily_return)")
    print("=" * 78)
    print(f"{'net return':>14} {'daily rate':>12} {'cap for $0.4/min':>20} {'cap for $0.7/min':>20}")
    rows: list[tuple[str, float]] = [(f"{r * 100:g}%/yr", daily_rate_from_annual(r)) for r in ANNUAL_RATES]
    rows += [(f"{d * 100:g}%/day", d) for d in DAILY_RATES]
    for label, rate in rows:
        caps = [capital_for_daily_target(normalise(p)["per_day"], rate) for p in CLAIM_PER_MIN]
        print(f"{label:>14} {rate * 100:>11.4f}% {_money(caps[0]):>20} {_money(caps[1]):>20}")

    print()
    print("HEADLINE — what a $100 account must earn to make $0.4/min:")
    for per_min in CLAIM_PER_MIN:
        pct = daily_pct_for_capital(RETAIL_CAPITAL_USD, normalise(per_min)["per_day"])
        print(f"  ${per_min:.2f}/min -> {_money(normalise(per_min)['per_day'])}/day on $100 = {pct * 100:,.0f}%/day")
    print("  (compounded annually that is not a rate; it is a contradiction.)")
    print()


def print_volume_table() -> None:
    print("=" * 78)
    print("3. REQUIRED VOLUME/EDGE  (notional/min = target / net_edge)")
    print("=" * 78)
    for per_min in CLAIM_PER_MIN:
        print(f"\n-- target ${per_min:.2f}/min --")
        header = f"{'net edge':>9} {'notional/min':>16} {'notional/day':>16} " + " ".join(
            f"{t:>18}" for t in FEE_TIERS_BPS_PER_LEG
        )
        print(header)
        for edge in NET_EDGES_BPS:
            nmin = notional_per_min(edge, per_min)
            cells = []
            for tier, fee in FEE_TIERS_BPS_PER_LEG.items():
                gross = gross_edge_required_bps(edge, fee)
                ok = tier_can_reach(edge, fee)
                feasible = ok and gross <= MEASURED_GROSS_EDGE_BPS[1]
                cells.append(f"{gross:>7.1f}bps{'*' if feasible else ' '} {tier:>7}")
            print(f"{edge:>7.1f}bp {_money(nmin):>16} {_money(nmin * MIN_PER_DAY):>16} " + " ".join(cells))
        print("  cell = gross edge required (= net + 2*fee/leg); '*' = within measured 1-11 bps band")
    print()


def print_mechanism_table() -> None:
    print("=" * 78)
    print("4. MECHANISM TABLE")
    print("=" * 78)
    for m in MECHANISMS:
        flag = "YES" if m.retail_100_accessible else "no"
        print(f"\n* {m.name}")
        print(f"    prerequisites : {m.prerequisites}")
        print(f"    $100 retail?  : {flag}  ({m.note})")
    print()
    print("  Verification of a public claim (absence is itself evidence):")
    print("    - public on-chain address whose inflows match the claim")
    print("    - read-only exchange API key showing a live track record")
    print("    - third-party audited statement, or an exchange-issued volume/fee tier")
    print("    - without any of these it is marketing / a course / someone else's dashboard.")
    print()


def print_verdict() -> None:
    print("=" * 78)
    print("5. VERDICT")
    print("=" * 78)
    print("  - $0.4/min is $576/day, $210,240/yr. On $100 that is 576%/day — impossible.")
    print("  - Consistent only with large, fee-advantaged, low-latency capital:")
    print("      market making / latency arb / funding at $0.5M-$10M+ notional,")
    print("      with maker rebates and colocated infra. None are a $100 retail account.")
    print("  - At the project's measured 1-11 bps gross dislocations vs a 20-30 bps fee")
    print("    floor, a $100 retail taker's net edge is negative; volume cannot fix that.")
    print("  - If the claim is real it is almost certainly not a $100 account. The specific")
    print("    things that would make it verifiable are listed above; their absence is the")
    print("    signal.")
    print()


def main() -> None:
    print_normalisation()
    print_capital_table()
    print_volume_table()
    print_mechanism_table()
    print_verdict()


if __name__ == "__main__":
    main()
