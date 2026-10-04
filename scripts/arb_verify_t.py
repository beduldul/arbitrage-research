"""Falsification harness for the WARP report's single positive finding.

The report ``evidence/arbitrage/2026-10-03/warp/REPORT_WARP.md`` §2 claims a
cross-venue perp funding spread on base **``T``**: long Binance perp / short
Kraken Futures perp, mean spread 23.884 bps/8h, net **$0.354/day** at $100
($50/leg), capacity $1,053.

This module attacks that claim. It is measurement-only: no keys, no orders, no
transactions. All venue payloads are read from cached raw responses under
``evidence/arbitrage/2026-10-03/verify_t/raw/``.

The decisive test is **instrument identity**. ``arb_warp_scan.py`` derives the
Kraken Futures base with ``sym[3:].replace("XBT", "BTC").replace("USD", "")``
(line ~692). For ``PF_USDTUSD`` that yields ``"T"`` -- a string collision with
Binance's genuine ``T`` (Threshold Network) perp. Kraken's instrument metadata
says the contract is ``base=USDT, quote=USD, pair="USDT:USD"``: it is the
USDT/USD *stablecoin* perpetual, not a token ``T``. Price levels confirm it:
Binance T trades near $0.0054, Kraken PF_USDTUSD near $1.00.

Pure helpers are unit-tested in ``tests/unit/test_arb_verify_t.py``; ``main``
re-reads the cached payloads and writes ``verify_t.json``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = REPO_ROOT / "evidence" / "arbitrage" / "2026-10-03" / "verify_t"
RAW = EVIDENCE / "raw"

# ---- verified venue fee schedules (USDⓈ-M / Kraken Futures linear) ----------
# Binance USDⓈ-M futures VIP0: taker 0.05%, maker 0.02% (Binance futures fee FAQ
# 360033544231 + trading-parameters page). Kraken Futures: the live
# /derivatives/api/v3/feeschedules endpoint lists "MTF Linear Rebate Fees"
# maker 0.02% / taker 0.05% at $0 volume, and PF_USDTUSD's own
# feeScheduleUid resolves to that schedule.
BINANCE_TAKER_BPS = 5.0
BINANCE_MAKER_BPS = 2.0
KRAKEN_TAKER_BPS = 5.0
KRAKEN_MAKER_BPS = 2.0

LEG_USD = 50.0
BUCKET_HOURS = 8.0
CROSSINGS = 4  # 2 venues x in/out
REPORT_MEAN_SPREAD_BPS_8H = 23.8844
REPORT_WINDOW_DAYS = 30.0

BPS = 10_000.0


# ===========================================================================
# 1. Instrument identity
# ===========================================================================


def kraken_base_buggy(symbol: str) -> str:
    """The exact base-derivation the WARP scanner used (``arb_warp_scan.py``).

    Reproduced verbatim so the collision can be shown rather than asserted.
    """
    return symbol[3:].replace("XBT", "BTC").replace("USD", "")


def kraken_base_correct(instrument: dict[str, Any]) -> str:
    """The base Kraken's own instrument metadata reports."""
    return str(instrument.get("base", ""))


def identity_verdict(
    binance_base: str,
    binance_price: float,
    kraken_base_buggy_str: str,
    kraken_base_true: str,
    kraken_price: float,
    price_ratio_threshold: float = 10.0,
) -> dict[str, Any]:
    """Classify whether the two legs are the same asset.

    A genuine cross-venue perp spread trades at the same price up to basis, so a
    price ratio far from 1 (or a base mismatch once Kraken's metadata is used)
    means the pairing is a symbol collision, not an arbitrage.
    """
    ratio = (kraken_price / binance_price) if binance_price else float("inf")
    same_base_metadata = binance_base == kraken_base_true
    same_price = 1.0 / price_ratio_threshold <= ratio <= price_ratio_threshold
    collision = kraken_base_buggy_str == binance_base and not same_base_metadata
    return {
        "binance_base": binance_base,
        "kraken_base_from_buggy_parser": kraken_base_buggy_str,
        "kraken_base_from_metadata": kraken_base_true,
        "price_ratio_kraken_over_binance": round(ratio, 6),
        "same_base_by_metadata": same_base_metadata,
        "same_price_level": same_price,
        "symbol_collision": collision,
        "verdict": "FALSIFIED" if (collision or not same_price) else "CONFIRMED",
    }


# ===========================================================================
# 2. Funding mechanism compatibility
# ===========================================================================


def funding_mechanism(
    binance_interval_h: float, kraken_interval_h: float, binance_settles_per_bucket: int
) -> dict[str, Any]:
    """Describe whether the two funding mechanisms can be compared directly.

    Both venues charge a funding payment on a fixed interval against the perp's
    own mark/index. Summing each venue's native payments into a common
    ``BUCKET_HOURS`` window makes *income over the same wall-clock window*
    comparable; it does not make the *rates* comparable, because the settlement
    cadence (and thus compounding) differs.
    """
    return {
        "binance_interval_h": binance_interval_h,
        "kraken_interval_h": kraken_interval_h,
        "binance_settlements_per_8h_bucket": binance_settles_per_bucket,
        "kraken_settlements_per_8h_bucket": int(BUCKET_HOURS / kraken_interval_h),
        "comparable_as": "income per wall-clock 8h bucket (sum of native payments)",
        "not_comparable_as": "per-settlement rate (cadence differs: "
        f"{binance_interval_h}h vs {kraken_interval_h}h)",
        "apples_to_apples": binance_interval_h == kraken_interval_h,
    }


# ===========================================================================
# 3. Fees
# ===========================================================================


def round_trip_cost_usd(
    leg_usd: float, taker_bps: float, crossings: int = CROSSINGS
) -> float:
    """One-time fee cost of opening+closing both legs, at ``taker_bps`` per event."""
    if leg_usd <= 0:
        raise ValueError("leg_usd must be positive")
    if taker_bps < 0 or crossings < 0:
        raise ValueError("taker_bps and crossings must be non-negative")
    return crossings * leg_usd * (taker_bps / BPS)


def round_trip_cost_mixed_usd(
    leg_usd: float,
    taker_bps_a: float,
    taker_bps_b: float,
    crossings_per_leg: int = 2,
) -> float:
    """Round-trip fee cost charging each leg its own venue's taker fee.

    ``crossings_per_leg`` defaults to 2 (open + close); each crossing is charged
    on the full leg notional at that venue's rate.
    """
    if leg_usd <= 0:
        raise ValueError("leg_usd must be positive")
    if taker_bps_a < 0 or taker_bps_b < 0 or crossings_per_leg < 0:
        raise ValueError("fees and crossings must be non-negative")
    return crossings_per_leg * leg_usd * (taker_bps_a + taker_bps_b) / BPS


def gross_per_day(leg_usd: float, mean_spread_bps_8h: float) -> float:
    """Funding-spread income per day at ``leg_usd`` per leg (matches the report)."""
    if leg_usd <= 0:
        raise ValueError("leg_usd must be positive")
    return leg_usd * (mean_spread_bps_8h / BPS) * (24.0 / BUCKET_HOURS)


def net_per_day(
    leg_usd: float,
    mean_spread_bps_8h: float,
    taker_bps: float,
    window_days: float = REPORT_WINDOW_DAYS,
    crossings: int = CROSSINGS,
) -> float:
    """Amortised net $/day = gross/day minus one-time round-trip cost over the window."""
    if window_days <= 0:
        raise ValueError("window_days must be positive")
    return gross_per_day(leg_usd, mean_spread_bps_8h) - round_trip_cost_usd(
        leg_usd, taker_bps, crossings
    ) / window_days


def break_even_taker_bps(
    leg_usd: float,
    mean_spread_bps_8h: float,
    window_days: float = REPORT_WINDOW_DAYS,
    crossings: int = CROSSINGS,
) -> float:
    """Per-event taker fee (bps) at which the trade's net/day crosses zero."""
    if leg_usd <= 0 or crossings <= 0 or window_days <= 0:
        raise ValueError("leg_usd, crossings, window_days must be positive")
    gross = gross_per_day(leg_usd, mean_spread_bps_8h) * window_days
    return gross / (crossings * leg_usd) * BPS


# ===========================================================================
# 4. Minimum order sizes
# ===========================================================================


def binance_min_leg_ok(
    price: float, min_qty: float, min_notional_usd: float, leg_usd: float
) -> dict[str, Any]:
    """Can ``leg_usd`` be placed on the Binance perp?"""
    if price <= 0:
        raise ValueError("price must be positive")
    qty = leg_usd / price
    return {
        "qty": round(qty, 6),
        "min_qty": min_qty,
        "min_notional_usd": min_notional_usd,
        "notional_usd": round(qty * price, 4),
        "qty_ok": qty >= min_qty,
        "notional_ok": qty * price >= min_notional_usd,
        "placeable": qty >= min_qty and qty * price >= min_notional_usd,
    }


def kraken_min_leg_ok(
    price: float, contract_size: float, leg_usd: float, min_contracts: float | None
) -> dict[str, Any]:
    """Can ``leg_usd`` be placed on the Kraken Futures perp?

    ``min_contracts`` is ``None`` when the instrument payload publishes no
    minimum-order field -- that is an *unverified* minimum, not a zero one.
    """
    if price <= 0 or contract_size <= 0:
        raise ValueError("price and contract_size must be positive")
    contracts = leg_usd / (price * contract_size)
    placeable = None if min_contracts is None else contracts >= min_contracts
    return {
        "contracts": round(contracts, 4),
        "contract_notional_usd": round(price * contract_size, 6),
        "min_contracts": min_contracts,
        "placeable": placeable,
        "min_verified": min_contracts is not None,
    }


# ===========================================================================
# Runner
# ===========================================================================


def _load(name: str) -> Any:
    with (RAW / name).open() as fh:
        return json.load(fh)


def _bucket8(times_ms: list[int], rates: list[float]) -> dict[int, float]:
    """Sum native funding payments into 8h wall-clock buckets."""
    out: dict[int, float] = {}
    for t, r in zip(times_ms, rates, strict=False):
        b = int(t // int(BUCKET_HOURS * 3_600_000.0))
        out[b] = out.get(b, 0.0) + r
    return out


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / n
    sx = (sum((v - mx) ** 2 for v in xs) / n) ** 0.5
    sy = (sum((v - my) ** 2 for v in ys) / n) ** 0.5
    if sx == 0 or sy == 0:
        return None
    return cov / (sx * sy)


def _funding_series_block() -> dict[str, Any]:
    """Correlate the two cached funding histories on shared 8h buckets.

    If the two legs were the same asset, their funding rates would co-move
    strongly. A near-zero (or negative) correlation means the "spread" is just
    one venue's unhedged funding, not a hedged cross-venue differential.
    """
    from datetime import datetime

    b_rows = _load("binance_funding_TUSDT.json")
    bt = [int(r["fundingTime"]) for r in b_rows]
    br = [float(r["fundingRate"]) for r in b_rows]
    k_rows = _load("krakenfut_funding_PF_USDTUSD.json")["rates"]
    kt = [
        int(datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).timestamp() * 1000)
        for r in k_rows
    ]
    kr = [float(r["relativeFundingRate"]) for r in k_rows]
    b_b = _bucket8(bt, br)
    k_b = _bucket8(kt, kr)
    shared = sorted(set(b_b) & set(k_b))
    bv = [b_b[x] * BPS for x in shared]
    kv = [k_b[x] * BPS for x in shared]
    n = len(shared)
    return {
        "shared_8h_buckets": n,
        "binance_T_mean_bps_8h": round(sum(bv) / n, 4) if n else None,
        "kraken_USDTUSD_mean_bps_8h": round(sum(kv) / n, 4) if n else None,
        "pearson_corr": (
            round(_pearson(bv, kv), 4) if _pearson(bv, kv) is not None else None
        ),
        "spread_mean_bps_8h": round(sum(bv[i] - kv[i] for i in range(n)) / n, 4) if n else None,
        "note": "the spread is Binance Threshold funding; the Kraken leg is inert",
    }


def _first(items: list[dict[str, Any]], **match: Any) -> dict[str, Any] | None:
    for it in items:
        if all(it.get(k) == v for k, v in match.items()):
            return it
    return None


def build_report() -> dict[str, Any]:
    """Read cached venue payloads and assemble the falsification report."""
    ei = _load("binance_exchangeinfo.json")
    prem = _load("binance_premiumindex.json")
    depth = _load("binance_depth_TUSDT.json")
    kf_inst = _load("krakenfut_instruments.json")["instruments"]
    kf_tk = _load("krakenfut_tickers.json")["tickers"]
    kf_book = _load("krakenfut_book_PF_USDTUSD.json")["orderBook"]

    b_sym = _first(ei["symbols"], symbol="TUSDT")
    if b_sym is None:
        raise SystemExit("Binance TUSDT missing from cached exchangeInfo")
    b_meta = _first(prem, symbol="TUSDT")
    b_price = float(b_meta["markPrice"]) if b_meta else float(b_sym["baseAsset"])
    k_inst = _first(kf_inst, symbol="PF_USDTUSD")
    k_tk = _first(kf_tk, symbol="PF_USDTUSD")
    if k_inst is None or k_tk is None:
        raise SystemExit("Kraken PF_USDTUSD missing from cached payloads")
    k_price = float(k_tk["markPrice"])

    min_f = next(
        (f for f in b_sym["filters"] if f["filterType"] == "MIN_NOTIONAL"), {}
    )
    lot_f = next((f for f in b_sym["filters"] if f["filterType"] == "LOT_SIZE"), {})

    identity = identity_verdict(
        binance_base=b_sym["baseAsset"],
        binance_price=b_price,
        kraken_base_buggy_str=kraken_base_buggy("PF_USDTUSD"),
        kraken_base_true=kraken_base_correct(k_inst),
        kraken_price=k_price,
    )
    identity.update(
        {
            "binance_symbol": "TUSDT",
            "kraken_symbol": "PF_USDTUSD",
            "binance_price_usd": b_price,
            "kraken_price_usd": k_price,
            "kraken_pair": k_inst.get("pair"),
            "kraken_category": k_inst.get("category"),
            "kraken_24h_volume_usd": k_tk.get("volumeQuote"),
            "kraken_open_interest_contracts": k_tk.get("openInterest"),
        }
    )

    mech = funding_mechanism(
        binance_interval_h=4.0,  # /fapi/v1/fundingInfo -> fundingIntervalHours
        kraken_interval_h=1.0,
        binance_settles_per_bucket=2,
    )

    gross = gross_per_day(LEG_USD, REPORT_MEAN_SPREAD_BPS_8H)
    fees = {
        "binance": {"taker_bps": BINANCE_TAKER_BPS, "maker_bps": BINANCE_MAKER_BPS},
        "kraken_futures": {"taker_bps": KRAKEN_TAKER_BPS, "maker_bps": KRAKEN_MAKER_BPS},
        "round_trip_cost_usd": round(
            round_trip_cost_mixed_usd(LEG_USD, BINANCE_TAKER_BPS, KRAKEN_TAKER_BPS), 6
        ),
        "break_even_taker_bps_per_event": round(
            break_even_taker_bps(LEG_USD, REPORT_MEAN_SPREAD_BPS_8H), 3
        ),
    }

    b_min = binance_min_leg_ok(
        price=b_price,
        min_qty=float(lot_f.get("minQty", 0.0)),
        min_notional_usd=float(min_f.get("notional", 0.0)),
        leg_usd=LEG_USD,
    )
    k_min = kraken_min_leg_ok(
        price=k_price,
        contract_size=float(k_inst.get("contractSize", 1.0)),
        leg_usd=LEG_USD,
        min_contracts=None,  # no published min-order field in the payload
    )

    # The report's own capacity = min(depth_a, depth_b). Reproduce depth_b from
    # the Kraken ticker (bid*bidSize + ask*askSize) to show what it actually is.
    depth_b = float(k_tk["bid"]) * float(k_tk["bidSize"]) + float(k_tk["ask"]) * float(
        k_tk["askSize"]
    )
    depth_a = sum(float(p) * float(q) for p, q in depth["bids"][:50]) + sum(
        float(p) * float(q) for p, q in depth["asks"][:50]
    )
    k_best_bid = float(kf_book["bids"][0][0])
    k_best_ask = float(kf_book["asks"][0][0])

    return {
        "generated_at_note": "measurement only; cached raw venue payloads",
        "identity": identity,
        "funding_mechanism": mech,
        "funding_series": _funding_series_block(),
        "fees": fees,
        "minimums_at_50_usd_leg": {"binance_TUSDT": b_min, "kraken_PF_USDTUSD": k_min},
        "net": {
            "report_gross_usd_day": round(gross, 6),
            "report_net_usd_day_taker": round(
                net_per_day(LEG_USD, REPORT_MEAN_SPREAD_BPS_8H, BINANCE_TAKER_BPS), 6
            ),
            "corrected_net_usd_day": 0.0,
            "corrected_note": "non-executable: the two legs are different assets",
        },
        "capacity": {
            "binance_T_depth_usd": round(depth_a, 2),
            "kraken_USDTUSD_ticker_depth_usd": round(depth_b, 2),
            "report_capacity_usd": 1052.88,
            "kraken_book_best_bid": k_best_bid,
            "kraken_book_best_ask": k_best_ask,
            "kraken_book_degenerate": k_best_bid < 0.9,
            "note": "binding leg is the USDT/USD stablecoin book, not a T-token market",
        },
    }


def main() -> int:
    report = build_report()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE / "verify_t.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    idn = report["identity"]
    print("=== INSTRUMENT IDENTITY ===")
    print(f"binance TUSDT  base={idn['binance_base']}  price=${idn['binance_price_usd']}")
    print(
        f"kraken  PF_USDTUSD  base(metadata)={idn['kraken_base_from_metadata']} "
        f"pair={idn['kraken_pair']}  price=${idn['kraken_price_usd']}"
    )
    print(
        f"buggy parser base={idn['kraken_base_from_buggy_parser']}  "
        f"ratio={idn['price_ratio_kraken_over_binance']}x  "
        f"collision={idn['symbol_collision']}  -> {idn['verdict']}"
    )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
