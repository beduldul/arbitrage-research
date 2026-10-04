#!/usr/bin/env python3
"""DECISIVE test — is any Gate.io / Hyperliquid funding-carry candidate actually tradeable?

Screening (`arb_funding_multivenue.py`) found 76 symbols whose *funding alone* clears a
round trip. This script asks the question that decides whether the class is real:

  1. **Spot-leg existence + liquidity** — a cash-and-carry needs a spot leg to hedge the
     short perp. No spot market on the venue ⇒ UNCONSTRUCTIBLE (a naked short, not carry).
  2. **Hedged PnL with basis drift** — funding (short perp) + realised basis change
     (spot leg − perp leg), minus 4 fee events, at the venue's real tier.
  3. **IS/OOS split** — positive in BOTH halves, with a block-bootstrap CI excluding zero.
  4. **The $100 reality** — $50/leg, and whether the venue's minimum order size even
     permits a $50 leg.

**No orders. No keys. Writes only under ``evidence/``.**

Fee provenance (this run)
-------------------------
* **Gate.io: VERIFIED.** `futures/usdt/contracts` exposes ``taker_fee_rate`` (0.00075) and
  ``maker_fee_rate`` (−0.0001) per contract; all 1025 agree. Spot taker is the published
  base 0.10% (Gate exposes no unauthenticated spot-fee endpoint).
* **Hyperliquid: UNVERIFIED.** No unauthenticated fee endpoint. Published base taker is
  0.035% (spot and perp), lower than the project default 0.05%; we use the project default
  perp taker (over-charging HL) and mark it unverified.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import arb_funding_multivenue as mv  # noqa: E402

GATE_BASE = mv.GATE_BASE
HL_INFO = mv.HL_INFO

#: Published base-tier fee fractions (per side, fraction of notional).
#: Gate: VERIFIED from `futures/usdt/contracts`. Gate spot taker = published 0.10%.
#: HL: UNVERIFIED; project-default perp taker (0.05%) is *higher* than HL's published
#: 0.035%, so using it is conservative against HL.
FEE: dict[str, dict[str, float | bool]] = {
    "gate": {"spot_taker": 0.0010, "perp_taker": 0.00075, "verified": True},
    "hyperliquid": {"spot_taker": 0.0005, "perp_taker": 0.0005, "verified": False},
}

#: Hyperliquid's documented minimum order value (USD). Used for the $50-leg check.
HL_MIN_ORDER_USD = 10.0

DEFAULT_RANKING = REPO_ROOT / "evidence" / "arbitrage" / "2026-10-03" / "multivenue_ranking.json"
DEFAULT_OUT = REPO_ROOT / "evidence" / "arbitrage" / "2026-10-03"


# ---------------------------------------------------------------------------
# Pure computation — the part the unit tests pin
# ---------------------------------------------------------------------------


def utc_day(epoch_s: float) -> int:
    """Epoch seconds -> integer UTC day number (days since 1970-01-01)."""
    return int(epoch_s // 86_400)


def daily_funding(times_ms: list[int], rates: list[float]) -> dict[int, float]:
    """Sum funding rates (fractions) per UTC day from settlement stamps.

    A short perp *receives* a positive rate, so the raw sum is already the short's income
    for the day — no sign flip here; the flip is the caller's contract.
    """
    out: dict[int, float] = {}
    for t, r in zip(times_ms, rates, strict=False):
        day = utc_day(t / 1000.0)
        out[day] = out.get(day, 0.0) + r
    return out


def basis_daily(spot_close: float, perp_close: float) -> float:
    """Signed basis as a fraction of spot: ``(spot - perp) / spot``.

    Positive when spot trades *above* the perp (the usual "backwardation" carry shape):
    the long-spot/short-perp pair then gains as the two converge.
    """
    if spot_close <= 0:
        raise ValueError("spot_close must be positive")
    return (spot_close - perp_close) / spot_close


def hedged_daily_net_dollars(
    days: list[int],
    spot_close: dict[int, float],
    perp_close: dict[int, float],
    funding: dict[int, float],
    *,
    leg_notional: float,
    spot_fee: float,
    perp_fee: float,
) -> list[float]:
    """Per-day net PnL in USD for one long-spot/short-perp pair of ``leg_notional`` each leg.

    Terms (all in USD):
      * funding: ``leg_notional * daily_funding`` — the short receives a positive rate.
      * basis:   ``leg_notional * ((S_d/S_{d-1}) - (P_d/P_{d-1}))`` — long spot minus
        short perp, i.e. the realised basis change. Day 0 has no prior close, so its
        basis term is zero and only the entry fee is charged.
      * fees:    entry ``leg*(spot+perp)`` on day 0, exit ``leg*(spot+perp)`` on the last
        day. Four fee events total, as a cash-and-carry actually pays them.

    Raises if a day is missing a spot or perp close — a gap is not silently a zero return.
    """
    if not days:
        raise ValueError("days is empty")
    out: list[float] = []
    for i, day in enumerate(days):
        if day not in spot_close or day not in perp_close:
            raise ValueError(f"missing close for day {day}")
        fund = leg_notional * funding.get(day, 0.0)
        basis = 0.0
        if i > 0:
            prev = days[i - 1]
            spot_ret = spot_close[day] / spot_close[prev] - 1.0
            perp_ret = perp_close[day] / perp_close[prev] - 1.0
            basis = leg_notional * (spot_ret - perp_ret)
        fee = 0.0
        if i == 0 or i == len(days) - 1:
            fee = -leg_notional * (spot_fee + perp_fee)
        out.append(fund + basis + fee)
    return out


def split_is_oos(daily: list[float]) -> tuple[list[float], list[float]]:
    """Split the daily series into an in-sample first half and out-of-sample second half."""
    mid = len(daily) // 2
    return daily[:mid], daily[mid:]


def block_bootstrap_ci(
    daily: list[float],
    *,
    block: int,
    n_boot: int = 2000,
    seed: int = 20261003,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """95% CI for the **total** net PnL, by a moving-block bootstrap.

    Blocks of ``block`` consecutive days are resampled with replacement to preserve the
    day-level autocorrelation (regime clustering) that an i.i.d. bootstrap destroys. The
    block length should be chosen from the clustering scale, not the sample size.
    """
    if not daily:
        raise ValueError("daily is empty")
    if block < 1:
        raise ValueError("block must be >= 1")
    n = len(daily)
    block = min(block, n)
    rng = random.Random(seed)
    starts = list(range(0, n - block + 1)) or [0]
    totals: list[float] = []
    for _ in range(n_boot):
        sample: list[float] = []
        while len(sample) < n:
            s = rng.choice(starts)
            sample.extend(daily[s : s + block])
        totals.append(sum(sample[:n]))
    totals.sort()
    lo = totals[int((alpha / 2) * n_boot)]
    hi = totals[int((1 - alpha / 2) * n_boot) - 1]
    return lo, hi


def gate_spot_min_notional(pair: dict[str, object], price: float) -> float:
    """Gate spot minimum order value in USD: the larger of the base and quote floors."""
    min_base = float(pair.get("min_base_amount") or 0.0)  # type: ignore[arg-type]
    min_quote = float(pair.get("min_quote_amount") or 0.0)  # type: ignore[arg-type]
    return max(min_base * price, min_quote)


def perp_min_notional(contract: dict[str, object], price: float) -> float:
    """Gate perp minimum order value: ``order_size_min * quanto_multiplier * price``."""
    size_min = float(contract.get("order_size_min") or 0.0)  # type: ignore[arg-type]
    quanto = float(contract.get("quanto_multiplier") or 0.0)  # type: ignore[arg-type]
    return size_min * quanto * price


# ---------------------------------------------------------------------------
# Data acquisition
# ---------------------------------------------------------------------------


def gate_base(symbol: str) -> str:
    """``BTC_USDT`` -> ``BTC``."""
    return symbol[: -len("_USDT")]


def load_ranking(path: Path, *, top_n: int) -> list[dict[str, object]]:
    """The top ``top_n`` candidates that meet the streak >= 7d and break-even <= 30d bar."""
    payload = json.loads(path.read_text())
    rows = [
        r
        for r in payload["symbols"]
        if r["verdict"] == "candidate"
        and r["longest_positive_streak_days"] >= 7.0
        and r["break_even_days"] is not None
        and r["break_even_days"] <= 30.0
    ]
    rows.sort(key=lambda r: r["dollars_per_day_net"], reverse=True)
    return rows[:top_n]


def gate_spot_market(client: mv.HttpClient) -> dict[str, dict[str, object]]:
    tickers = client.request(
        f"{GATE_BASE.replace('/futures/usdt', '')}/spot/tickers",
        cache_path=client.cache_dir / "gate" / "spot_tickers.json",
    )
    pairs = client.request(
        f"{GATE_BASE.replace('/futures/usdt', '')}/spot/currency_pairs",
        cache_path=client.cache_dir / "gate" / "spot_pairs.json",
    )
    assert isinstance(tickers, list) and isinstance(pairs, list)
    info = {p["id"]: p for p in pairs}
    out: dict[str, dict[str, object]] = {}
    for t in tickers:
        pair_id = t["currency_pair"]
        out[pair_id] = {
            "quote_volume_24h": float(t.get("quote_volume") or 0.0),
            "last": float(t["last"]) if t.get("last") else None,
            "pair": info.get(pair_id, {}),
        }
    return out


def hl_spot_market(client: mv.HttpClient) -> dict[str, dict[str, object]]:
    """HL spot pairs keyed by **resolved** ``BASE/QUOTE`` name.

    Hyperliquid names most spot pairs ``@N`` (``@1``, ``@2``…); the real ``BASE/QUOTE``
    is recovered from the pair's ``tokens`` indices into the ``tokens`` array. Matching on
    the raw ``@N`` name silently misses ~all of HL's spot markets, so resolution is not
    optional.
    """
    body = json.dumps({"type": "spotMetaAndAssetCtxs"}).encode()
    data = client.request(
        HL_INFO, cache_path=client.cache_dir / "hyperliquid" / "spot_meta.json", data=body
    )
    assert isinstance(data, list) and len(data) == 2
    universe, ctxs = data[0]["universe"], data[1]
    tokens = data[0].get("tokens", [])
    # Pairs reference token *index* values (which can exceed the list length — the list
    # is sparse), so key by each token's own `index`, not its list position.
    tok_by_index = {t["index"]: t["name"] for t in tokens}
    out: dict[str, dict[str, object]] = {}
    for meta, ctx in zip(universe, ctxs, strict=False):
        name = str(meta["name"])
        if name.startswith("@") and tok_by_index:
            base_idx, quote_idx = meta["tokens"]
            base = tok_by_index.get(base_idx, name)
            quote = tok_by_index.get(quote_idx, "?")
            resolved = f"{base}/{quote}"
        else:
            resolved = name
        out[resolved] = {
            "quote_volume_24h": float(ctx.get("dayNtlVlm") or 0.0),
            "mid": float(ctx["midPx"]) if ctx.get("midPx") else None,
            "is_canonical": bool(meta.get("isCanonical")),
            "raw_name": name,
        }
    return out


def find_hl_spot(symbol: str, spot: dict[str, dict[str, object]]) -> str | None:
    """The spot pair for a perp coin: ``<coin>/USDC`` (else ``/USDT``), if it exists."""
    for quote in ("USDC", "USDT"):
        name = f"{symbol}/{quote}"
        if name in spot:
            return name
    return None


def gate_daily_closes(client: mv.HttpClient, symbol: str) -> dict[int, float]:
    rows = client.request(
        f"{GATE_BASE}/candlesticks?contract={urllib.parse.quote(symbol)}&interval=1d&limit=60",
        cache_path=client.cache_dir / "gate" / "candles" / f"{symbol}.json",
    )
    if not isinstance(rows, list):
        return {}
    # Gate futures candles are objects: {"t": epoch_s, "c": close, ...}
    return {utc_day(float(r["t"])): float(r["c"]) for r in rows}


def gate_spot_daily_closes(client: mv.HttpClient, pair: str) -> dict[int, float]:
    rows = client.request(
        f"https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair={urllib.parse.quote(pair)}"
        "&interval=1d&limit=60",
        cache_path=client.cache_dir / "gate" / "spot_candles" / f"{pair}.json",
    )
    if not isinstance(rows, list):
        return {}
    # Gate spot candles are arrays: [t, quote_vol, close, high, low, open, base_vol, closed]
    return {utc_day(float(r[0])): float(r[2]) for r in rows}


def hl_daily_closes(client: mv.HttpClient, coin: str, *, kind: str) -> dict[int, float]:
    body = json.dumps(
        {"type": "candleSnapshot", "req": {"coin": coin, "interval": "1d", "startTime": 0}}
    ).encode()
    rows = client.request(
        HL_INFO,
        cache_path=client.cache_dir / "hyperliquid" / "candles" / f"{kind}_{coin.replace('/', '_')}.json",
        data=body,
    )
    if not isinstance(rows, list):
        return {}
    return {utc_day(float(r["t"]) / 1000.0): float(r["c"]) for r in rows}


def load_funding(client: mv.HttpClient, venue: str, symbol: str, days: float) -> tuple[list[float], list[int]]:
    if venue == "gate":
        return mv.gate_funding_history(client, symbol, days)
    return mv.hl_funding_history(client, symbol, days)


# ---------------------------------------------------------------------------
# Per-candidate evaluation
# ---------------------------------------------------------------------------


@dataclass
class CandidateVerdict:
    venue: str
    symbol: str
    spot_pair: str | None
    spot_exists: bool
    spot_quote_volume_24h_usd: float
    constructible: bool
    unconstructible_reason: str | None
    # liquidity / minimums at a $50 leg
    spot_min_notional_usd: float | None
    perp_min_notional_usd: float | None
    permits_50usd_leg: bool
    # economics over the measured window
    window_days: int
    n_days: int
    gross_funding_usd: float
    basis_drift_usd: float
    fees_usd: float
    net_usd: float
    net_usd_per_day: float
    is_net_usd: float
    oos_net_usd: float
    positive_in_both_halves: bool
    oos_ci_low: float
    oos_ci_high: float
    ci_excludes_zero: bool
    fee_verified: bool
    verdict: str
    reason: str


def evaluate(
    client: mv.HttpClient,
    *,
    row: dict[str, object],
    spot_market: dict[str, dict[str, object]],
    notional: float,
    days: float,
    block: int,
    n_boot: int,
) -> CandidateVerdict:
    venue = str(row["venue"])
    symbol = str(row["symbol"])
    leg = notional / 2.0
    fees = FEE[venue]
    spot_fee = float(fees["spot_taker"])
    perp_fee = float(fees["perp_taker"])
    fee_verified = bool(fees["verified"])

    # --- 1. spot leg existence ---
    spot_pair: str | None = None
    if venue == "gate":
        candidate = f"{gate_base(symbol)}_USDT"
        spot_pair = candidate if candidate in spot_market else None
    else:
        spot_pair = find_hl_spot(symbol, spot_market)
    spot_exists = spot_pair is not None
    spot_vol = spot_market[spot_pair]["quote_volume_24h"] if spot_pair else 0.0  # type: ignore[assignment]

    # --- liquidity / minimums ---
    spot_min: float | None = None
    perp_min: float | None = None
    permits = False
    if spot_pair:
        if venue == "gate":
            pair_info = spot_market[spot_pair]["pair"]  # type: ignore[assignment]
            perp_contract = None
            contracts = client.request(
                f"{GATE_BASE}/contracts", cache_path=client.cache_dir / "gate" / "contracts.json"
            )
            if isinstance(contracts, list):
                perp_contract = next((c for c in contracts if c["name"] == symbol), None)
            px = float(row.get("mark_price") or 0.0)
            spot_min = gate_spot_min_notional(pair_info, px) if px else None  # type: ignore[arg-type]
            perp_min = perp_min_notional(perp_contract, px) if perp_contract and px else None
            permits = (spot_min is None or spot_min <= leg) and (perp_min is None or perp_min <= leg)
        else:
            spot_min = HL_MIN_ORDER_USD
            perp_min = HL_MIN_ORDER_USD
            permits = leg >= HL_MIN_ORDER_USD

    if not spot_exists:
        return CandidateVerdict(
            venue=venue, symbol=symbol, spot_pair=None, spot_exists=False,
            spot_quote_volume_24h_usd=0.0, constructible=False,
            unconstructible_reason=f"no spot market on {venue} to hedge the short perp",
            spot_min_notional_usd=None, perp_min_notional_usd=None, permits_50usd_leg=False,
            window_days=0, n_days=0, gross_funding_usd=0.0, basis_drift_usd=0.0, fees_usd=0.0,
            net_usd=0.0, net_usd_per_day=0.0, is_net_usd=0.0, oos_net_usd=0.0,
            positive_in_both_halves=False, oos_ci_low=0.0, oos_ci_high=0.0,
            ci_excludes_zero=False, fee_verified=fee_verified, verdict="unconstructible",
            reason=f"no {venue} spot market for {gate_base(symbol) if venue == 'gate' else symbol}",
        )

    # --- 2. hedged PnL with basis drift ---
    rates, times = load_funding(client, venue, symbol, days)
    funding = daily_funding(times, rates)
    if venue == "gate":
        spot_close = gate_spot_daily_closes(client, spot_pair)
        perp_close = gate_daily_closes(client, symbol)
    else:
        # HL candles key on the RAW pair name ('@107'), not the resolved 'HYPE/USDC'.
        raw_spot = str(spot_market[spot_pair].get("raw_name") or spot_pair)
        spot_close = hl_daily_closes(client, raw_spot, kind="spot")
        perp_close = hl_daily_closes(client, symbol, kind="perp")

    common = sorted(set(spot_close) & set(perp_close))
    # restrict to the funding window so PnL and funding cover the same period
    if funding:
        lo, hi = min(funding), max(funding)
        common = [d for d in common if lo <= d <= hi]
    if len(common) < 4:
        return CandidateVerdict(
            venue=venue, symbol=symbol, spot_pair=spot_pair, spot_exists=True,
            spot_quote_volume_24h_usd=spot_vol, constructible=False,
            unconstructible_reason=f"only {len(common)} aligned daily closes (need >= 4)",
            spot_min_notional_usd=spot_min, perp_min_notional_usd=perp_min,
            permits_50usd_leg=permits, window_days=0, n_days=len(common),
            gross_funding_usd=0.0, basis_drift_usd=0.0, fees_usd=0.0, net_usd=0.0,
            net_usd_per_day=0.0, is_net_usd=0.0, oos_net_usd=0.0,
            positive_in_both_halves=False, oos_ci_low=0.0, oos_ci_high=0.0,
            ci_excludes_zero=False, fee_verified=fee_verified, verdict="no_data",
            reason="insufficient aligned price history for spot and perp",
        )

    daily = hedged_daily_net_dollars(
        common, spot_close, perp_close, funding,
        leg_notional=leg, spot_fee=spot_fee, perp_fee=perp_fee,
    )
    funding_only = [leg * funding.get(d, 0.0) for d in common]
    fees_only = [-leg * (spot_fee + perp_fee) if (i == 0 or i == len(common) - 1) else 0.0
                 for i in range(len(common))]
    gross_funding = sum(funding_only)
    fees_usd = sum(fees_only)
    basis_drift = sum(daily) - gross_funding - fees_usd
    net = sum(daily)

    # --- 3. IS/OOS + block bootstrap ---
    is_half, oos_half = split_is_oos(daily)
    is_net, oos_net = sum(is_half), sum(oos_half)
    both = is_net > 0.0 and oos_net > 0.0
    ci_lo, ci_hi = block_bootstrap_ci(oos_half, block=block, n_boot=n_boot)
    ci_ok = ci_lo > 0.0

    if both and ci_ok and permits:
        verdict, reason = "tradeable", f"net ${net:+.4f} over {len(common)}d, positive both halves, CI>0"
    elif both and not ci_ok:
        verdict, reason = "no", f"positive both halves but OOS CI [{ci_lo:.3f},{ci_hi:.3f}] includes zero"
    elif not both:
        verdict, reason = "no", f"not positive in both halves (IS ${is_net:+.4f}, OOS ${oos_net:+.4f})"
    elif not permits:
        verdict, reason = "no", "a $50 leg is below the venue's minimum order size"
    else:
        verdict, reason = "no", "failed"

    return CandidateVerdict(
        venue=venue, symbol=symbol, spot_pair=spot_pair, spot_exists=True,
        spot_quote_volume_24h_usd=spot_vol, constructible=True, unconstructible_reason=None,
        spot_min_notional_usd=spot_min, perp_min_notional_usd=perp_min,
        permits_50usd_leg=permits, window_days=len(common), n_days=len(common),
        gross_funding_usd=round(gross_funding, 4), basis_drift_usd=round(basis_drift, 4),
        fees_usd=round(fees_usd, 4), net_usd=round(net, 4),
        net_usd_per_day=round(net / len(common), 4),
        is_net_usd=round(is_net, 4), oos_net_usd=round(oos_net, 4),
        positive_in_both_halves=both, oos_ci_low=round(ci_lo, 4), oos_ci_high=round(ci_hi, 4),
        ci_excludes_zero=ci_ok, fee_verified=fee_verified, verdict=verdict, reason=reason,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ranking", type=Path, default=DEFAULT_RANKING)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--notional", type=float, default=100.0)
    parser.add_argument("--days", type=float, default=30.0)
    parser.add_argument("--block", type=int, default=5, help="bootstrap block length in days")
    parser.add_argument("--n-boot", type=int, default=2000)
    args = parser.parse_args(argv)

    cache_dir = args.out / "venues"
    client = mv.HttpClient(cache_dir)
    rows = load_ranking(args.ranking, top_n=args.top)
    print(f"evaluating top {len(rows)} candidates", file=sys.stderr)

    gate_spot = gate_spot_market(client)
    hl_spot = hl_spot_market(client)

    results: list[CandidateVerdict] = []
    lock = threading.Lock()

    def work(row: dict[str, object]) -> None:
        venue = str(row["venue"])
        market = gate_spot if venue == "gate" else hl_spot
        try:
            verdict = evaluate(
                client, row=row, spot_market=market, notional=args.notional,
                days=args.days, block=args.block, n_boot=args.n_boot,
            )
        except Exception as error:  # noqa: BLE001 — one candidate must not sink the run
            print(f"  ! {venue}/{row['symbol']}: {type(error).__name__}: {error}", file=sys.stderr)
            return
        with lock:
            results.append(verdict)

    with ThreadPoolExecutor(max_workers=mv.NETWORK_CONCURRENCY) as pool:
        list(pool.map(work, rows))

    order = {"tradeable": 0, "no": 1, "unconstructible": 2, "no_data": 3}
    results.sort(key=lambda r: (order.get(r.verdict, 9), -r.net_usd))

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "notional_usd": args.notional,
        "leg_usd": args.notional / 2.0,
        "bootstrap": {"block_days": args.block, "n_boot": args.n_boot, "seed": 20261003},
        "fee_model": FEE,
        "n_evaluated": len(results),
        "n_constructible": sum(1 for r in results if r.constructible),
        "n_tradeable": sum(1 for r in results if r.verdict == "tradeable"),
        "candidates": [asdict(r) for r in results],
        "http_stats": client.stats,
    }
    out_json = args.out / "decisive_carry_results.json"
    out_json.write_text(json.dumps(payload, indent=2))

    header = (
        "| venue | symbol | spot pair | spot 24h $ | min spot/perp $ | fund $ | basis $ | fees $ "
        "| net $ | $/day | IS $ | OOS $ | OOS 95% CI | verdict |"
    )
    print(header)
    print("|" + "---|" * 15)
    for r in results:
        mins = (
            f"{r.spot_min_notional_usd:.2f}/{r.perp_min_notional_usd:.2f}"
            if r.spot_min_notional_usd is not None and r.perp_min_notional_usd is not None
            else "-"
        )
        ci = f"[{r.oos_ci_low:.3f}, {r.oos_ci_high:.3f}]" if r.constructible else "-"
        print(
            f"| {r.venue} | {r.symbol} | {r.spot_pair or '—'} | {r.spot_quote_volume_24h_usd/1e6:,.2f}M "
            f"| {mins} | {r.gross_funding_usd:.4f} | {r.basis_drift_usd:.4f} | {r.fees_usd:.4f} "
            f"| {r.net_usd:.4f} | {r.net_usd_per_day:.4f} | {r.is_net_usd:.4f} | {r.oos_net_usd:.4f} "
            f"| {ci} | {r.verdict} |"
        )
    print(
        f"\nconstructible {payload['n_constructible']}/{len(results)} · "
        f"tradeable {payload['n_tradeable']}/{len(results)} · http {client.stats}"
    )
    print(f"wrote {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
