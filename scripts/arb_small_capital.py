#!/usr/bin/env python3
"""Pure small-capital feasibility arithmetic for the cross-venue perp funding spread.

**Measurement only.** No keys, no wallets, no orders, no transactions, no config edits.
Every number is a pure function of (a) venue instrument metadata and (b) the programme's
already-measured spread series; the network path only *reads* public instrument
payloads and caches them under ``evidence/arbitrage/2026-10-03/small_capital/``.

The programme's only surviving candidate is the cross-venue perp funding spread on bases
**BAT** and **QNT**, pairs ``(binance|bybit|okx|bitget|gate) x kraken_futures``. The
reported net (``<$0.35/day``) assumed **$50/leg** and a *snapshot* spread. This module
answers the arithmetic question a **$50 or $100** account actually faces:

1. **Minimum order sizes.** For every leg, the minimum *placeable notional in USD* at the
   current price, and whether a $25 or $50 leg clears it. A venue's floor is the larger of
   its declared min order *value* and ``min_qty x contract_size x price``.
2. **Step-size rounding loss.** A leg of intended notional ``N`` can only be placed as a
   multiple of the lot step. The stranded fraction, in bps of the leg, is a real cost that
   *grows as capital shrinks*.
3. **Realistic net $/day** at $25/leg and $50/leg, charging 4 fee events at the project's
   verified taker tier (``engine.fees.FeeSchedule``: 5 bps futures), the **measured**
   half-spreads (not the snapshot), and the rounding loss.
4. **Minimum viable capital:** the smallest account where the trade is placeable on both
   legs, and the account at which net clears $0.10/day.

Run::

    uv run python scripts/arb_small_capital.py            # use cached payloads
    uv run python scripts/arb_small_capital.py --fetch    # refresh, then compute
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from crypto_brain.engine.fees import FeeSchedule  # noqa: E402

EVIDENCE = REPO / "evidence" / "arbitrage" / "2026-10-03"
CACHE = EVIDENCE / "small_capital"
WARP_FUNDING = EVIDENCE / "warp" / "funding_warp.json"
SPREAD_REPORT = EVIDENCE / "spread_series" / "spread_series_report.json"
RAW_BOOKS = EVIDENCE / "spread_series" / "raw"
OUT_PATH = EVIDENCE / "small_capital" / "small_capital.json"

#: The project's own fee model (DESIGN.md §10.2 base tier). 4 fee events = 2 perp
#: round-trips = 2 venues x in/out; taker 5 bps is the verified tier a sibling checked.
FEES = FeeSchedule()

#: 8h funding buckets -> 3 buckets per day (the spread series' own convention).
BUCKETS_PER_DAY = 3.0

#: Bases and the venue pair set under test.
BASES = ("BAT", "QNT")
KRAKEN = "kraken_futures"

#: URL per venue for the *fetch* path only; the compute path never touches the network.
INSTRUMENT_URLS: dict[str, str] = {
    "binance": "https://fapi.binance.com/fapi/v1/exchangeInfo",
    "bybit": "https://api.bybit.com/v5/market/instruments-info?category=linear",
    "okx": "https://www.okx.com/api/v5/public/instruments?instType=SWAP",
    "bitget": "https://api.bitget.com/api/v2/mix/market/contracts?productType=usdt-futures",
    "gate": "https://api.gateio.ws/api/v4/futures/usdt/contracts",
    KRAKEN: "https://futures.kraken.com/derivatives/api/v3/instruments",
}
KRAKEN_TICKERS = "https://futures.kraken.com/derivatives/api/v3/tickers"

CONCURRENCY = 2
MAX_RETRIES = 3
BACKOFF_BASE = 1.5

#: Published base-tier taker fees (bps) read from the venue instrument payloads, used
#: only as a *sensitivity*. Gate's payload says ``taker_fee_rate`` 0.00075 (7.5 bps) and
#: Bitget's ``takerFeeRate`` 0.0006 (6 bps); Binance/Bybit/OKX/Kraken publish 5 bps.
PUBLISHED_TAKER_BPS: dict[str, float] = {
    "binance": 5.0, "bybit": 5.5, "okx": 5.0, "bitget": 6.0, "gate": 7.5,
    KRAKEN: 5.0,
}

#: Every (venue, base) leg the 8 surviving pairs actually use, with its venue symbol.
LEG_SYMBOLS: dict[tuple[str, str], str] = {
    ("binance", "BAT"): "BATUSDT", ("binance", "QNT"): "QNTUSDT",
    ("bybit", "BAT"): "BATUSDT", ("bybit", "QNT"): "QNTUSDT",
    ("okx", "BAT"): "BAT-USDT-SWAP",
    ("bitget", "BAT"): "BATUSDT", ("bitget", "QNT"): "QNTUSDT",
    ("gate", "QNT"): "QNT_USDT",
    (KRAKEN, "BAT"): "PF_BATUSD", (KRAKEN, "QNT"): "PF_QNTUSD",
}


# --------------------------------------------------------------------- model


@dataclass(frozen=True, slots=True)
class LegSpec:
    """One venue leg's real order constraints, normalised to base-asset units.

    ``qty_step``/``min_qty`` are expressed in **contracts**; ``contract_size`` converts a
    contract to base-asset units (``ctVal`` on OKX, ``quanto_multiplier`` on Gate, 1 on
    Binance/Bybit/Bitget/Kraken). ``min_notional_usd`` is a venue-declared minimum order
    *value* (0 when the venue publishes none).
    """

    venue: str
    symbol: str
    base: str
    price_usd: float
    min_qty: float
    qty_step: float
    contract_size: float
    min_notional_usd: float
    source: str

    @property
    def step_notional_usd(self) -> float:
        """USD value of one lot step (the quantum of position size)."""
        return self.qty_step * self.contract_size * self.price_usd

    @property
    def min_qty_notional_usd(self) -> float:
        """USD value of the venue's minimum quantity."""
        return self.min_qty * self.contract_size * self.price_usd

    @property
    def min_placeable_notional_usd(self) -> float:
        """Smallest order this leg can express, in USD.

        The binding floor is the larger of the declared minimum order *value* and the
        minimum quantity's own notional — a $5 min-notional with a 0.1-contract min qty
        worth $24.80 is a $24.80 floor, not a $5 one.
        """
        return max(self.min_notional_usd, self.min_qty_notional_usd)

    def can_place(self, leg_usd: float) -> bool:
        """Can a leg of ``leg_usd`` intended notional be placed at all?"""
        if leg_usd < self.min_placeable_notional_usd:
            return False
        return math.floor(leg_usd / self.step_notional_usd) >= 1

    def max_price_for_leg(self, leg_usd: float) -> float:
        """Price above which a ``leg_usd`` leg stops placing (floor == ``leg_usd``).

        A leg that only just clears $25 today (Binance QNT at $248 against a $24.80
        minimum-quantity floor) stops clearing on a small price rise — the placeability
        is not stable through a price move.
        """
        if self.min_qty * self.contract_size <= 0:
            return math.inf
        return leg_usd / (self.min_qty * self.contract_size)


def floor_to_step(leg_usd: float, step_usd: float) -> float:
    """Largest step multiple that does not exceed ``leg_usd`` (0 if none fits)."""
    if step_usd <= 0:
        raise ValueError("step_usd must be positive")
    return math.floor(leg_usd / step_usd) * step_usd


def rounding_loss_bps(leg_usd: float, step_usd: float) -> float:
    """Fraction of an intended leg stranded by the lot step, in bps.

    A $25 leg with a $24.80 step rounds down to $24.80 and strands 8 bps; the same step
    on a $50 leg rounds to $49.60 and strands the same 8 bps — but the *absolute*
    stranded capital grows with the account, and at a smaller step the loss is larger.
    """
    if leg_usd <= 0:
        raise ValueError("leg_usd must be positive")
    placed = floor_to_step(leg_usd, step_usd)
    return (leg_usd - placed) / leg_usd * 10_000.0


def placed_leg_usd(leg_usd: float, step_usd: float) -> float:
    """The notional actually placeable for an intended ``leg_usd`` at ``step_usd``."""
    return floor_to_step(leg_usd, step_usd)


# --------------------------------------------------------------- pure: net $/day


@dataclass(frozen=True, slots=True)
class PairLegs:
    """One measured cross-venue pair, with the fields the net-$/day model needs."""

    base: str
    venue_a: str
    symbol_a: str
    venue_b: str
    symbol_b: str
    #: Signed mean funding spread (bps per 8h bucket). The report trades the profitable
    #: direction, so income uses ``abs(mean_spread_bps_8h)`` (its ``dollars_per_day`` call
    #: is ``dollars_per_day(leg_usd, abs(stats.mean_bps)/10_000)``), *not* the larger
    #: ``abs_mean`` of the series.
    mean_spread_bps_8h: float
    #: Mean *magnitude* of the spread series (bps/8h); reported for transparency only.
    abs_mean_spread_bps_8h: float
    half_spread_a_bps: float
    half_spread_b_bps: float
    horizon_days: float


def one_time_cost_usd(
    leg_usd: float, legs: PairLegs, *, venue_fee_bps: dict[str, float] | None = None
) -> float:
    """Open+close both perp legs: 4 fee events + 4 half-spread crossings.

    Mirrors ``arb_funding_spread.cross_venue_cost``: fees are ``4 x leg x taker_bps`` and
    spread is ``2 x leg x (hs_a + hs_b)`` — the measured half-spreads, never the snapshot.

    By default both legs are charged the project's §10.2 futures taker tier (5 bps). When
    ``venue_fee_bps`` is supplied, each leg is charged its own *published base-tier* taker
    (Gate 7.5 bps, Bitget 6 bps per their instrument payloads) — a sensitivity, not the
    headline, because those tiers are unverified for this account.
    """
    if venue_fee_bps is None:
        fees = 4.0 * leg_usd * (FEES.futures_taker_bps / 10_000.0)
    else:
        a_bps = venue_fee_bps.get(legs.venue_a, FEES.futures_taker_bps)
        b_bps = venue_fee_bps.get(legs.venue_b, FEES.futures_taker_bps)
        fees = 2.0 * leg_usd * ((a_bps + b_bps) / 10_000.0)
    spread = 2.0 * leg_usd * ((legs.half_spread_a_bps + legs.half_spread_b_bps) / 10_000.0)
    return fees + spread


def gross_per_day_usd(leg_usd: float, legs: PairLegs) -> float:
    """Funding-spread income per day on ``leg_usd`` per leg (both legs equal size)."""
    return leg_usd * (abs(legs.mean_spread_bps_8h) / 10_000.0) * BUCKETS_PER_DAY


def net_per_day_usd(
    leg_usd: float,
    legs: PairLegs,
    *,
    step_a_usd: float,
    step_b_usd: float,
    venue_fee_bps: dict[str, float] | None = None,
) -> dict[str, float]:
    """Net $/day for an intended ``leg_usd`` after fees, measured spread and rounding.

    Gross is earned on the *placed* notional (the legs must be balanced, so the binding
    size is the smaller of the two rounded legs). The one-time cost is amortised over the
    pair's measured horizon — the same convention the programme's own report uses; we do
    not invent a holding period.
    """
    placed_a = placed_leg_usd(leg_usd, step_a_usd)
    placed_b = placed_leg_usd(leg_usd, step_b_usd)
    placed = min(placed_a, placed_b)
    if placed <= 0:
        return {
            "leg_usd": leg_usd,
            "placed_leg_usd": 0.0,
            "gross_per_day_usd": 0.0,
            "cost_per_day_usd": 0.0,
            "net_per_day_usd": 0.0,
            "net_per_month_usd": 0.0,
            "stranded_leg_usd": leg_usd,
            "rounding_loss_bps": rounding_loss_bps(leg_usd, max(step_a_usd, step_b_usd)),
        }
    gross = gross_per_day_usd(placed, legs)
    cost = one_time_cost_usd(placed, legs, venue_fee_bps=venue_fee_bps) / legs.horizon_days
    net = gross - cost
    stranded = 2.0 * (leg_usd - placed)
    return {
        "leg_usd": leg_usd,
        "placed_leg_usd": placed,
        "gross_per_day_usd": gross,
        "cost_per_day_usd": cost,
        "net_per_day_usd": net,
        "net_per_month_usd": net * 30.0,
        "stranded_leg_usd": stranded,
        "rounding_loss_bps": rounding_loss_bps(leg_usd, max(step_a_usd, step_b_usd)),
    }


# --------------------------------------------------------------------- parsing


def _f(value: Any) -> float:
    return float(value)


def parse_binance(payload: dict[str, Any], symbol: str, base: str, price: float) -> LegSpec:
    row = next(s for s in payload["symbols"] if s["symbol"] == symbol)
    filt = {x["filterType"]: x for x in row["filters"]}
    return LegSpec(
        venue="binance", symbol=symbol, base=base, price_usd=price,
        min_qty=_f(filt["LOT_SIZE"]["minQty"]),
        qty_step=_f(filt["LOT_SIZE"]["stepSize"]),
        contract_size=1.0,
        min_notional_usd=_f(filt["MIN_NOTIONAL"]["notional"]),
        source="/fapi/v1/exchangeInfo LOT_SIZE+MIN_NOTIONAL",
    )


def parse_bybit(payload: dict[str, Any], symbol: str, base: str, price: float) -> LegSpec:
    row = next(s for s in payload["result"]["list"] if s["symbol"] == symbol)
    lot = row["lotSizeFilter"]
    return LegSpec(
        venue="bybit", symbol=symbol, base=base, price_usd=price,
        min_qty=_f(lot["minOrderQty"]),
        qty_step=_f(lot["qtyStep"]),
        contract_size=1.0,
        min_notional_usd=_f(lot.get("minNotionalValue", 0)),
        source="v5/instruments-info lotSizeFilter",
    )


def parse_okx(payload: dict[str, Any], symbol: str, base: str, price: float) -> LegSpec:
    row = next(s for s in payload["data"] if s["instId"] == symbol)
    return LegSpec(
        venue="okx", symbol=symbol, base=base, price_usd=price,
        min_qty=_f(row["minSz"]),
        qty_step=_f(row["lotSz"]),
        contract_size=_f(row["ctVal"]) * _f(row.get("ctMult", 1)),
        min_notional_usd=0.0,
        source="v5/public/instruments minSz/lotSz/ctVal/ctMult",
    )


def parse_bitget(payload: dict[str, Any], symbol: str, base: str, price: float) -> LegSpec:
    row = next(s for s in payload["data"] if s["symbol"] == symbol)
    return LegSpec(
        venue="bitget", symbol=symbol, base=base, price_usd=price,
        min_qty=_f(row["minTradeNum"]),
        qty_step=_f(row["sizeMultiplier"]),
        contract_size=1.0,
        min_notional_usd=_f(row.get("minTradeUSDT", 0)),
        source="v2/mix/market/contracts minTradeNum/sizeMultiplier/minTradeUSDT",
    )


def parse_gate(payload: list[dict[str, Any]], symbol: str, base: str, price: float) -> LegSpec:
    row = next(s for s in payload if s["name"] == symbol)
    return LegSpec(
        venue="gate", symbol=symbol, base=base, price_usd=price,
        min_qty=_f(row["order_size_min"]),
        qty_step=1.0,  # Gate contracts are integers; no order_size_round is published
        contract_size=_f(row["quanto_multiplier"]),
        min_notional_usd=0.0,
        source="v4/futures/usdt/contracts order_size_min/quanto_multiplier",
    )


def parse_kraken(payload: dict[str, Any], symbol: str, base: str, price: float) -> LegSpec:
    """Kraken exposes ``contractSize`` and ``contractValueTradePrecision``, no min field.

    The lot step is ``10**-contractValueTradePrecision`` contracts and the minimum order is
    one lot (Kraken's own contract pages list "Lot size" and "Minimum order" as the same
    quantum — e.g. PF_BATUSD lot size 1, minimum order 1 BAT). ``contractSize`` is 1 for
    these perps, so one contract is one base unit.
    """
    row = next(s for s in payload["instruments"] if s["symbol"] == symbol)
    step = 10.0 ** (-int(row["contractValueTradePrecision"]))
    return LegSpec(
        venue=KRAKEN, symbol=symbol, base=base, price_usd=price,
        min_qty=step,
        qty_step=step,
        contract_size=_f(row["contractSize"]),
        min_notional_usd=0.0,
        source="derivatives/api/v3/instruments contractSize/contractValueTradePrecision",
    )


def build_specs(
    payloads: dict[str, Any], kraken_tickers: dict[str, Any], prices: dict[tuple[str, str], float]
) -> dict[tuple[str, str], LegSpec]:
    """Parse every (venue, symbol) leg for BAT and QNT from cached payloads."""
    specs: dict[tuple[str, str], LegSpec] = {}
    for (venue, base), s in LEG_SYMBOLS.items():
        price = prices[(venue, base)]
        if venue == "binance":
            spec = parse_binance(payloads[venue], s, base, price)
        elif venue == "bybit":
            spec = parse_bybit(payloads[venue], s, base, price)
        elif venue == "okx":
            spec = parse_okx(payloads[venue], s, base, price)
        elif venue == "bitget":
            spec = parse_bitget(payloads[venue], s, base, price)
        elif venue == "gate":
            spec = parse_gate(payloads[venue], s, base, price)
        else:
            spec = parse_kraken(payloads[venue], s, base, price)
        specs[(venue, base)] = spec
    return specs


# --------------------------------------------------------------------- loaders


def _book_mid(venue: str, symbol: str) -> float:
    """Mid of the first cached order book for a leg (same source as the half-spreads)."""
    files = sorted((RAW_BOOKS / venue / symbol).glob("*.json"))
    if not files:
        raise FileNotFoundError(f"no cached book for {venue}/{symbol}")
    d = json.loads(files[0].read_text())
    if venue == "binance":
        b, a = float(d["bids"][0][0]), float(d["asks"][0][0])
    elif venue == "bybit":
        b, a = float(d["result"]["b"][0][0]), float(d["result"]["a"][0][0])
    elif venue == "bitget":
        b, a = float(d["data"]["bids"][0][0]), float(d["data"]["asks"][0][0])
    elif venue == "okx":
        b, a = float(d["data"][0]["bids"][0][0]), float(d["data"][0]["asks"][0][0])
    elif venue == "gate":
        b, a = float(d["bids"][0]["p"]), float(d["asks"][0]["p"])
    elif venue == KRAKEN:
        b, a = float(d["orderBook"]["bids"][0][0]), float(d["orderBook"]["asks"][0][0])
    else:
        raise ValueError(f"unknown venue {venue!r}")
    return (a + b) / 2.0


def load_prices(kraken_tickers: dict[str, Any]) -> dict[tuple[str, str], float]:
    """Per-leg price: cached book mid for CEX legs, ticker mid for Kraken legs."""
    prices: dict[tuple[str, str], float] = {}
    for (venue, base), s in LEG_SYMBOLS.items():
        if venue == KRAKEN:
            continue
        prices[(venue, base)] = _book_mid(venue, s)
    tick = {t["symbol"]: t for t in kraken_tickers["tickers"]}
    for base, s in (("BAT", "PF_BATUSD"), ("QNT", "PF_QNTUSD")):
        t = tick[s]
        prices[(KRAKEN, base)] = (float(t["bid"]) + float(t["ask"])) / 2.0
    return prices


def load_cached() -> tuple[dict[str, Any], dict[str, Any]]:
    payloads: dict[str, Any] = {}
    for venue in ("binance", "bybit", "okx", "bitget", "gate", KRAKEN):
        payloads[venue] = json.loads((CACHE / f"{venue}_instruments.json").read_text())
    tickers = json.loads((CACHE / "kraken_futures_tickers.json").read_text())
    return payloads, tickers


async def fetch_payloads() -> tuple[dict[str, Any], dict[str, Any]]:
    """Bounded-concurrency, backoff-honouring refresh of the cached instrument payloads."""
    import httpx

    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(client: httpx.AsyncClient, name: str, url: str) -> Any:
        async with sem:
            delay = 1.0
            for attempt in range(1, MAX_RETRIES + 1):
                resp = await client.get(url)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code in (429, 418) and attempt < MAX_RETRIES:
                    await asyncio.sleep(delay)
                    delay *= BACKOFF_BASE
                    continue
                resp.raise_for_status()
            raise RuntimeError(f"{name}: exhausted retries")

    async def bybit_all(client: httpx.AsyncClient) -> dict[str, Any]:
        """Bybit paginates ``instruments-info`` at 500/page; follow the cursor."""
        first = await one(client, "bybit", INSTRUMENT_URLS["bybit"])
        rows = list(first["result"]["list"])
        cursor = first["result"].get("nextPageCursor")
        while cursor:
            page = await one(
                client, "bybit", f"{INSTRUMENT_URLS['bybit']}&cursor={cursor}"
            )
            rows.extend(page["result"]["list"])
            cursor = page["result"].get("nextPageCursor")
        return {"result": {"category": first["result"]["category"], "list": rows}}

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        names = [n for n in INSTRUMENT_URLS if n != "bybit"] + ["kraken_tickers"]
        urls = [INSTRUMENT_URLS[n] for n in names[:-1]] + [KRAKEN_TICKERS]
        results = await asyncio.gather(
            bybit_all(client), *(one(client, n, u) for n, u in zip(names, urls, strict=True))
        )
    out = {"bybit": results[0], **dict(zip(names, results[1:], strict=True))}
    for name in list(INSTRUMENT_URLS):
        (CACHE / f"{name}_instruments.json").write_text(json.dumps(out[name]))
    (CACHE / "kraken_futures_tickers.json").write_text(json.dumps(out["kraken_tickers"]))
    payloads = {v: out[v] for v in INSTRUMENT_URLS}
    return payloads, out["kraken_tickers"]


def load_pairs() -> list[PairLegs]:
    """The programme's 8 surviving BAT/QNT pairs, with measured half-spreads."""
    funding = json.loads(WARP_FUNDING.read_text())
    report = json.loads(SPREAD_REPORT.read_text())
    measured: dict[tuple[str, str], dict[str, float]] = {}
    for row in report["series"]["pairs"]:
        key = (row["base"], row["venue_a"], row["venue_b"])
        measured[key] = row
    pairs: list[PairLegs] = []
    for row in funding["pairs"]:
        if row["base"] not in BASES or KRAKEN not in (row["venue_a"], row["venue_b"]):
            continue
        key = (row["base"], row["venue_a"], row["venue_b"])
        m = measured[key]
        pairs.append(
            PairLegs(
                base=row["base"],
                venue_a=row["venue_a"],
                symbol_a=row["symbol_a"],
                venue_b=row["venue_b"],
                symbol_b=row["symbol_b"],
                mean_spread_bps_8h=row["mean_spread_bps_8h"],
                abs_mean_spread_bps_8h=row["abs_mean_spread_bps_8h"],
                half_spread_a_bps=m["hs_a_mean"],
                half_spread_b_bps=m["hs_b_mean"],
                horizon_days=row["window_days"],
            )
        )
    return pairs


# --------------------------------------------------------------------- report


def analyze(specs: dict[tuple[str, str], LegSpec], pairs: list[PairLegs]) -> dict[str, Any]:
    legs_out: list[dict[str, Any]] = []
    for (venue, base), spec in sorted(specs.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        legs_out.append(
            {
                "base": base, "venue": venue, "symbol": spec.symbol,
                "price_usd": round(spec.price_usd, 8),
                "min_qty": spec.min_qty, "qty_step": spec.qty_step,
                "contract_size": spec.contract_size,
                "min_notional_declared_usd": spec.min_notional_usd,
                "step_notional_usd": round(spec.step_notional_usd, 6),
                "min_placeable_notional_usd": round(spec.min_placeable_notional_usd, 6),
                "can_place_25": spec.can_place(25.0),
                "can_place_50": spec.can_place(50.0),
                # The price above which a $25 leg no longer places (floor = $25). A leg
                # that only just clears $25 today stops clearing on a small price rise.
                "max_price_for_25_leg": round(spec.max_price_for_leg(25.0), 4),
                "max_price_for_50_leg": round(spec.max_price_for_leg(50.0), 4),
                "rounding_loss_bps_at_25": round(
                    rounding_loss_bps(25.0, spec.step_notional_usd), 2
                ),
                "rounding_loss_bps_at_50": round(
                    rounding_loss_bps(50.0, spec.step_notional_usd), 2
                ),
                "source": spec.source,
            }
        )

    pairs_out: list[dict[str, Any]] = []
    for p in pairs:
        sa = specs[(p.venue_a, p.base)]
        sb = specs[(p.venue_b, p.base)]
        row: dict[str, Any] = {
            "base": p.base,
            "venue_a": p.venue_a, "symbol_a": p.symbol_a,
            "venue_b": p.venue_b, "symbol_b": p.symbol_b,
            "mean_spread_bps_8h": p.mean_spread_bps_8h,
            "abs_mean_spread_bps_8h": p.abs_mean_spread_bps_8h,
            "half_spread_a_bps": p.half_spread_a_bps,
            "half_spread_b_bps": p.half_spread_b_bps,
            "horizon_days": p.horizon_days,
            "placeable_25": sa.can_place(25.0) and sb.can_place(25.0),
            "placeable_50": sa.can_place(50.0) and sb.can_place(50.0),
            "min_capital_usd": 2.0 * max(
                sa.min_placeable_notional_usd, sb.min_placeable_notional_usd
            ),
        }
        for label, leg in (("25", 25.0), ("50", 50.0)):
            net = net_per_day_usd(
                leg, p, step_a_usd=sa.step_notional_usd, step_b_usd=sb.step_notional_usd
            )
            row[f"net_{label}"] = {k: round(v, 6) for k, v in net.items()}
        sens = net_per_day_usd(
            50.0, p, step_a_usd=sa.step_notional_usd, step_b_usd=sb.step_notional_usd,
            venue_fee_bps=PUBLISHED_TAKER_BPS,
        )
        row["net_50_published_fees"] = round(sens["net_per_day_usd"], 6)
        pairs_out.append(row)

    # Minimum viable capital for $0.10/day: net is ~linear in leg notional above the floor,
    # so the slope from the $50/leg row extrapolates. Only pairs placeable at $50 count.
    threshold = 0.10
    candidates = []
    for row in pairs_out:
        net50 = row["net_50"]["net_per_day_usd"]
        if net50 <= 0 or not row["placeable_50"]:
            continue
        leg_for_threshold = 50.0 * (threshold / net50)
        capital = max(2.0 * leg_for_threshold, row["min_capital_usd"])
        candidates.append({"base": row["base"], "pair": f"{row['venue_a']}/{row['venue_b']}",
                           "capital_usd": round(capital, 2)})
    candidates.sort(key=lambda c: c["capital_usd"])
    return {
        "generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "fee_model": {"futures_taker_bps": FEES.futures_taker_bps,
                      "fee_events": 4, "source": "crypto_brain.engine.fees.FeeSchedule"},
        "legs": legs_out,
        "pairs": pairs_out,
        "min_viable_capital": {
            "threshold_usd_per_day": threshold,
            "candidates": candidates[:5],
            "smallest_placeable_usd": round(
                min(r["min_capital_usd"] for r in pairs_out), 2
            ),
        },
    }


def print_report(out: dict[str, Any]) -> None:
    print("=== MIN NOTIONAL / PLACEABILITY ===")
    print(f"{'base':4} {'venue':15} {'symbol':16} {'min$':>9} {'step$':>9} "
          f"{'$25?':>5} {'$50?':>5} {'rl@25bps':>9} {'maxP@25':>9}")
    for leg in out["legs"]:
        print(
            f"{leg['base']:4} {leg['venue']:15} {leg['symbol']:16} "
            f"{leg['min_placeable_notional_usd']:9.4f} {leg['step_notional_usd']:9.4f} "
            f"{'YES' if leg['can_place_25'] else 'NO':>5} "
            f"{'YES' if leg['can_place_50'] else 'NO':>5} "
            f"{leg['rounding_loss_bps_at_25']:9.1f} {leg['max_price_for_25_leg']:9.4f}"
        )
    print("\n=== NET $/DAY (measured half-spreads, 4 fee events @5bps) ===")
    print(f"{'base':4} {'pair':34} {'net@25/leg':>11} {'net@50/leg':>11} "
          f"{'$/mo@50':>9} {'$/mo@100':>9} {'@pub.fees':>10}")
    for row in out["pairs"]:
        print(
            f"{row['base']:4} {row['venue_a']+'/'+row['venue_b']:34} "
            f"{row['net_25']['net_per_day_usd']:11.4f} {row['net_50']['net_per_day_usd']:11.4f} "
            f"{row['net_25']['net_per_month_usd']:9.3f} {row['net_50']['net_per_month_usd']:9.3f} "
            f"{row['net_50_published_fees']:10.4f}"
        )
    mv = out["min_viable_capital"]
    print(f"\nsmallest placeable account: ${mv['smallest_placeable_usd']}")
    for c in mv["candidates"]:
        print(f"  $0.10/day needs ${c['capital_usd']:>7} on {c['base']} {c['pair']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fetch", action="store_true", help="refresh cached payloads first")
    args = ap.parse_args()

    if args.fetch or not (CACHE / "kraken_futures_tickers.json").exists():
        payloads, tickers = asyncio.run(fetch_payloads())
    else:
        payloads, tickers = load_cached()

    prices = load_prices(tickers)
    specs = build_specs(payloads, tickers, prices)
    pairs = load_pairs()
    out = analyze(specs, pairs)
    out["prices"] = {f"{v}|{b}": round(p, 8) for (v, b), p in sorted(prices.items())}
    OUT_PATH.write_text(json.dumps(out, indent=1))
    print_report(out)
    print(f"\nwrote {OUT_PATH.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
