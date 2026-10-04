#!/usr/bin/env python3
"""Falsification test for the `spread_series` "8 survivors" claim (MEASUREMENT ONLY).

The claim: after the identity guard, 8 pairs (4x BAT, 4x QNT, each <$0.35/day at $100)
survive because their OOS bootstrap CI excluded zero in 21/21 (seed x block) combos.

The suspected artifact: `robustness.json` is a PERFECT PARTITION by venue — every pair
with a `kraken_futures` leg scores 21/21, every pair without one scores 0/21. That split
tracks the VENUE, not the market. This script tests whether the "spread" is a
venue-mechanism LEVEL OFFSET (Kraken's hourly relative funding being structurally
different) rather than a token-specific, tradeable edge.

Five checks, all pure/measurement:

  1. LEVEL OFFSET — per-venue mean/median 8h funding (bps) for BAT and QNT legs; the
     CI-exclusion counts split by "has a kraken_futures leg" over the whole universe.
  2. OUTLIER-DRIVEN CI — mean vs median of each spread series; same-sign share; the
     share of total |income| contributed by the top 5% of buckets.
  3. THE PROGRAMME'S OWN GATE — apply `>=60% same-sign` AND median-sign == mean-sign to
     the spread series (not just the CI) and report pass/fail.
  4. KRAKEN MECHANISM — verify the published mechanism (hourly cadence, relative funding
     rate, price-dependent absolute rate) against the raw cached fields and the live
     ticker.
  5. VERDICT — real edge vs venue-mechanism artifact.

No keys, no wallets, no orders, no transactions. Public endpoints only; raw responses are
cached under ``evidence/arbitrage/2026-10-03/partition/``.

Usage::

    uv run python scripts/arb_partition_test.py run
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import arb_funding_spread as sp  # noqa: E402
import arb_spread_series as ss  # noqa: E402
import arb_warp_scan as ws  # noqa: E402

BPS = 10_000.0
MIN_SHARED_BUCKETS = 42
SAME_SIGN_BAR = 0.60
TOP_FRACTION = 0.05

OUT_DIR = REPO_ROOT / "evidence" / "arbitrage" / "2026-10-03" / "partition"
RAW_OUT = OUT_DIR / "raw"
WARP_DIR = REPO_ROOT / "evidence" / "arbitrage" / "2026-10-03" / "warp"
FUNDING_JSON = WARP_DIR / "funding_warp.json"

UA = "crypto-brain-arb-partition-test/1.0 (paper research; read-only)"
CONCURRENCY = 3
RATE_PER_S = 5.0
TIMEOUT_S = 20.0

KRAKEN = "kraken_futures"
KRAKEN_ALIASES = ("kraken_futures", "krakenfut")


# ===========================================================================
# Pure computation — the part the unit tests pin
# ===========================================================================


def mean_sign(series: list[float]) -> float:
    """+1 / -1 for the sign of the mean (0 => +1 by convention, matching the programme)."""
    return 1.0 if statistics.fmean(series) >= 0 else -1.0


def same_sign_share(series: list[float]) -> float:
    """Share of buckets whose sign matches the sign of the mean (strict; zeros don't count).

    This is the programme's own persistence bar (`evaluate_pair`): a spread that is only
    ~40% same-sign is a flip-flop whose mean is carried by a minority of buckets.
    """
    if not series:
        raise ValueError("empty series")
    sign = mean_sign(series)
    n = len(series)
    if sign > 0:
        return sum(1 for x in series if x > 0) / n
    return sum(1 for x in series if x < 0) / n


def mean_vs_median(series: list[float]) -> dict[str, float | bool]:
    """Mean vs median (bps) and whether the median shares the mean's sign.

    ``mean_gt_median`` (with a positive mean) or ``mean_lt_median`` (negative mean) flags a
    right/left-skewed series whose mean is driven by clustered outliers rather than a
    persistent sign — exactly the case the programme's median-sign gate exists to catch.
    """
    if not series:
        raise ValueError("empty series")
    mean_bps = statistics.fmean(series) * BPS
    median_bps = statistics.median(series) * BPS
    sign = mean_sign(series)
    return {
        "mean_bps": mean_bps,
        "median_bps": median_bps,
        "mean_abs_exceeds_median": abs(mean_bps) > abs(median_bps),
        "median_same_sign_as_mean": (median_bps > 0) == (sign > 0) and median_bps != 0.0,
    }


def top_income_share(series: list[float], *, fraction: float = TOP_FRACTION) -> dict[str, Any]:
    """Share of total |income| carried by the top ``fraction`` of buckets by |value|.

    Buckets are ranked by absolute contribution to income (we always trade the profitable
    direction). A concentration near 1.0 means a handful of buckets is the whole result.
    """
    if not series:
        raise ValueError("empty series")
    if not 0 < fraction <= 1:
        raise ValueError("fraction must be in (0, 1]")
    abs_vals = sorted((abs(x) for x in series), reverse=True)
    total = sum(abs_vals)
    k = max(1, int(round(fraction * len(abs_vals))))
    return {
        "n": len(series),
        "k": k,
        "top_share": (sum(abs_vals[:k]) / total) if total else 0.0,
    }


def gate_verdict(
    series: list[float], *, min_same_sign_share: float = SAME_SIGN_BAR
) -> dict[str, Any]:
    """Apply the programme's own robustness gates to a spread series.

    Pass requires BOTH: same-sign share >= ``min_same_sign_share`` AND the median sign
    equals the mean sign. A CI excluding zero alone does NOT satisfy these.
    """
    sss = same_sign_share(series)
    mm = mean_vs_median(series)
    return {
        "same_sign_share": sss,
        "median_same_sign_as_mean": mm["median_same_sign_as_mean"],
        "passes_same_sign_gate": sss >= min_same_sign_share,
        "passes_median_gate": bool(mm["median_same_sign_as_mean"]),
        "passes_both_gates": sss >= min_same_sign_share and bool(mm["median_same_sign_as_mean"]),
    }


def level_offset_series(*, n: int, offset: float, noise: float, seed: int = 0) -> list[float]:
    """A pure venue LEVEL OFFSET (constant) plus zero-mean symmetric noise.

    Models a venue-mechanism difference: the "spread" is the offset, not a token edge. The
    noise is symmetric so the median stays ~0 and the same-sign share ~0.5.
    """
    if n < 2:
        raise ValueError("n must be >= 2")
    rng = __import__("random").Random(seed)
    return [offset + rng.uniform(-noise, noise) for _ in range(n)]


def venue_artifact_series(
    *, n: int, baseline: float, spike: float, n_spike_buckets: int, clusters: int, seed: int = 0
) -> list[float]:
    """A venue-mechanism series whose mean is carried by a minority of clustered buckets.

    This is the shape of the real data: most buckets are small (even opposite-sign), while
    a few contiguous clusters carry large same-direction values (a venue's funding cap or
    hourly-spike mechanism). The mean is dominated by those clusters, so a bootstrap CI on
    the total excludes zero, yet the sign is NOT persistent across buckets and the
    programme's same-sign gate rejects it.
    """
    if n < 2 or clusters < 1 or n_spike_buckets < clusters:
        raise ValueError("invalid cluster geometry")
    per = max(1, n_spike_buckets // clusters)
    step = max(1, n // clusters)
    rng = __import__("random").Random(seed)
    out = [baseline + rng.uniform(-abs(baseline), abs(baseline)) for _ in range(n)]
    for c in range(clusters):
        for j in range(per):
            idx = (c * step + j) % n
            out[idx] = spike
    return out


def classify_series(series: list[float]) -> dict[str, Any]:
    """The full gate picture for one spread series (fractions per 8h bucket)."""
    mm = mean_vs_median(series)
    gate = gate_verdict(series)
    return {**mm, **gate}


def kraken_paired_counts(pairs: list[dict[str, Any]]) -> dict[str, int]:
    """Split pairs by 'has a kraken_futures leg' x 'CI excludes zero' (programme CIs).

    ``pairs`` are rows carrying ``venue_a``/``venue_b`` and ``oos_ci_low``/``oos_ci_high``.
    """
    out = {"kraken_n": 0, "kraken_excludes_zero": 0,
           "non_kraken_n": 0, "non_kraken_excludes_zero": 0}
    for p in pairs:
        lo, hi = p.get("oos_ci_low"), p.get("oos_ci_high")
        if lo is None or hi is None:
            continue
        excludes = lo > 0 or hi < 0
        venues = (p.get("venue_a"), p.get("venue_b"))
        has_kraken = any(v in KRAKEN_ALIASES for v in venues)
        if has_kraken:
            out["kraken_n"] += 1
            out["kraken_excludes_zero"] += int(excludes)
        else:
            out["non_kraken_n"] += 1
            out["non_kraken_excludes_zero"] += int(excludes)
    return out


def reconstructed_partition(
    pairs: list[dict[str, Any]], *, block: int = 5, n_boot: int = 2000, seed: int = 20261003
) -> dict[str, Any]:
    """Re-derive the CI-exclusion partition from raw funding, not the stored CI fields.

    For every reconstructible pair we recompute the report's OOS bootstrap CI (block 5,
    seed 20261003) and the same-sign gate, then cross-tabulate against 'has a Kraken leg'.
    If the venue partition is an artifact it must reproduce here too, and the same-sign
    gate must agree with the CI far more than the venue label does.
    """
    kraken_only = {"n": 0, "ci_excl": 0, "gates_pass": 0}
    other = {"n": 0, "ci_excl": 0, "gates_pass": 0}
    agree_venue = agree_gate = total = 0
    for p in pairs:
        spreads = _pair_series(p)
        if spreads is None:
            continue
        total += 1
        is_kraken = any(v in KRAKEN_ALIASES for v in (p["venue_a"], p["venue_b"]))
        _, oos = sp.split_is_oos(spreads)
        lo, hi = sp.bootstrap_ci(oos, block=block, n_boot=n_boot, seed=seed)
        ci_excl = lo > 0 or hi < 0
        gates = gate_verdict(spreads)["passes_both_gates"]
        bucket = kraken_only if is_kraken else other
        bucket["n"] += 1
        bucket["ci_excl"] += int(ci_excl)
        bucket["gates_pass"] += int(gates)
        agree_venue += int(ci_excl == is_kraken)
        agree_gate += int(ci_excl == gates)
    return {
        "n_pairs": total, "block": block, "n_boot": n_boot, "seed": seed,
        "kraken_paired": kraken_only, "non_kraken_paired": other,
        "venue_label_agreement": agree_venue / total if total else 0.0,
        "gate_agreement": agree_gate / total if total else 0.0,
    }


def kraken_field_ratio(sample: dict[str, float]) -> float | None:
    """``fundingRate / relativeFundingRate`` for one Kraken hourly row.

    If the published ticker ``fundingRate`` is the absolute per-hour rate and
    ``relativeFundingRate`` is that same rate divided by the index price, the ratio is the
    index price. A ratio far from the pair's price means the two fields are not
    interchangeable — the source of a level artifact.
    """
    rel = sample.get("relativeFundingRate")
    raw = sample.get("fundingRate")
    if not rel:
        return None
    return raw / rel


# ===========================================================================
# Data assembly
# ===========================================================================


def _kraken_legs() -> dict[str, str]:
    """base -> kraken symbol, from the cached raw files (``PF_<BASE>USD``)."""
    out: dict[str, str] = {}
    for p in sorted((WARP_DIR / "raw" / "krakenfut" / "funding").glob("PF_*.json")):
        base = p.stem[3:].replace("XBT", "BTC").removesuffix("USD")
        out[base] = p.stem
    return out


def _pair_series(p: dict[str, Any]) -> list[float] | None:
    """Reconstruct the aligned 8h spread series for one funding_warp pair row."""
    try:
        a = ss.bucket_8h(ss.load_funding(p["venue_a"], p["symbol_a"]))
        b = ss.bucket_8h(ss.load_funding(p["venue_b"], p["symbol_b"]))
    except (OSError, KeyError, json.JSONDecodeError):
        return None
    if not a or not b:
        return None
    _, _, _, spreads = sp.aligned_spread(a, b)
    return spreads if len(spreads) >= MIN_SHARED_BUCKETS else None


def leg_level_table(legs: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Per-venue mean/median 8h funding (bps) for a list of ``(venue, symbol)`` legs."""
    rows: list[dict[str, Any]] = []
    for venue, symbol in legs:
        try:
            buckets = ss.bucket_8h(ss.load_funding(venue, symbol))
        except (OSError, KeyError, json.JSONDecodeError):
            rows.append({"venue": venue, "symbol": symbol, "n": 0})
            continue
        vals = list(buckets.values())
        rows.append({
            "venue": venue, "symbol": symbol, "n": len(vals),
            "mean_bps": round(statistics.fmean(vals) * BPS, 4) if vals else None,
            "median_bps": round(statistics.median(vals) * BPS, 4) if vals else None,
        })
    return rows


def fetch_kraken_mechanism(legs: list[tuple[str, str]]) -> dict[str, Any]:
    """Cache the live Kraken ticker funding fields for the pairs under test."""
    net = ws.Net(RAW_OUT, concurrency=CONCURRENCY, rate_per_s=RATE_PER_S, timeout=TIMEOUT_S)
    out: dict[str, Any] = {}
    for venue, symbol in legs:
        if venue not in KRAKEN_ALIASES:
            continue
        rel = f"krakenfut/ticker/{symbol}.json"
        try:
            d = net.get(
                f"https://futures.kraken.com/derivatives/api/v3/tickers/"
                f"{symbol}",
                rel,
            )
        except Exception:  # noqa: BLE001 — one leg must not sink the probe
            out[symbol] = {"error": "fetch_failed"}
            continue
        t = (d or {}).get("ticker") if isinstance(d, dict) else None
        if not isinstance(t, dict):
            out[symbol] = {"error": "no_ticker"}
            continue
        out[symbol] = {
            "last": t.get("last"),
            "markPrice": t.get("markPrice"),
            "indexPrice": t.get("indexPrice"),
            "fundingRate": t.get("fundingRate"),
            "fundingRatePrediction": t.get("fundingRatePrediction"),
        }
    out["_stats"] = dict(net.stats)
    return out


# ===========================================================================
# Run
# ===========================================================================


def run() -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_OUT.mkdir(parents=True, exist_ok=True)
    funding = json.loads(FUNDING_JSON.read_text())

    # 1. Partition counts over the whole universe (programme's own stored CIs).
    partition = kraken_paired_counts(funding["pairs"])
    # 1b. Same partition, re-derived from raw funding (does not trust stored CI fields).
    reconstruction = reconstructed_partition(funding["pairs"])

    # 2. Level tables for BAT and QNT legs.
    bat_legs = [("binance", "BATUSDT"), ("bybit", "BATUSDT"), ("okx", "BAT-USDT-SWAP"),
                ("bitget", "BATUSDT"), ("gate", "BAT_USDT"), (KRAKEN, "PF_BATUSD")]
    qnt_legs = [("binance", "QNTUSDT"), ("bybit", "QNTUSDT"), ("okx", "QNT-USDT-SWAP"),
                ("bitget", "QNTUSDT"), ("gate", "QNT_USDT"), (KRAKEN, "PF_QNTUSD")]
    levels = {"BAT": leg_level_table(bat_legs), "QNT": leg_level_table(qnt_legs)}

    # 3. Per-pair gate/outlier stats for the report's 12 pairs.
    report_pairs = ss.select_pairs(funding, limit=12)
    pair_stats: list[dict[str, Any]] = []
    for p in report_pairs:
        spreads = _pair_series(p)
        if spreads is None:
            continue
        row = {
            "base": p["base"], "venue_a": p["venue_a"], "venue_b": p["venue_b"],
            "n_buckets": len(spreads),
            "identity_valid": bool(p.get("identity", {}).get("valid")),
            **classify_series(spreads),
            **top_income_share(spreads),
        }
        pair_stats.append(row)

    # 4. Kraken mechanism: raw field ratio (offline) + live ticker (cached).
    mech_rows: dict[str, Any] = {}
    for sym in ("PF_BATUSD", "PF_QNTUSD", "PF_USDTUSD"):
        path = WARP_DIR / "raw" / "krakenfut" / "funding" / f"{sym}.json"
        if not path.exists():
            continue
        rates = json.loads(path.read_text())["rates"]
        ratios = [r for r in (kraken_field_ratio(x) for x in rates[-200:]) if r]
        mech_rows[sym] = {
            "n_hourly_rows": len(rates),
            "raw_over_relative_median": round(statistics.median(ratios), 4) if ratios else None,
            "fundingRate_last": rates[-1]["fundingRate"],
            "relativeFundingRate_last": rates[-1]["relativeFundingRate"],
        }
    tickers = fetch_kraken_mechanism(bat_legs + qnt_legs)

    out = {
        "generated_at": datetime.now(UTC).isoformat(),
        "partition_counts": partition,
        "reconstruction": reconstruction,
        "levels_bps_per_8h": levels,
        "pair_stats": pair_stats,
        "kraken_mechanism": {"historical_fields": mech_rows, "live_tickers": tickers},
    }
    (OUT_DIR / "partition_report.json").write_text(json.dumps(out, indent=1))
    return out


def _print(out: dict[str, Any]) -> None:
    pc = out["partition_counts"]
    print(f"Kraken-paired pairs: {pc['kraken_excludes_zero']}/{pc['kraken_n']} CI excludes zero")
    print(f"Non-Kraken pairs:    {pc['non_kraken_excludes_zero']}/{pc['non_kraken_n']} "
          "CI excludes zero")
    rc = out["reconstruction"]
    print(f"Re-derived (raw funding): kraken {rc['kraken_paired']['ci_excl']}/"
          f"{rc['kraken_paired']['n']} CI-excl, {rc['kraken_paired']['gates_pass']} pass gates; "
          f"non-kraken {rc['non_kraken_paired']['ci_excl']}/{rc['non_kraken_paired']['n']} "
          f"CI-excl, {rc['non_kraken_paired']['gates_pass']} pass gates")
    print(f"  venue-label agreement {rc['venue_label_agreement']:.3f} vs "
          f"gate agreement {rc['gate_agreement']:.3f} (n={rc['n_pairs']})")
    for base, rows in out["levels_bps_per_8h"].items():
        print(f"-- {base} funding level (bps/8h)")
        for r in rows:
            if r["n"]:
                print(f"   {r['venue']:>14} n={r['n']:3} mean={r['mean_bps']:8.3f} "
                      f"median={r['median_bps']:8.3f}")
    print("-- pair stats")
    for r in out["pair_stats"]:
        print(f"   {r['base']:>4} {r['venue_a']:>14}/{r['venue_b']:<14} "
              f"mean={r['mean_bps']:7.2f} med={r['median_bps']:7.2f} "
              f"sss={r['same_sign_share']:.3f} top5={r['top_share']:.3f} "
              f"gates={r['passes_both_gates']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run"])
    args = parser.parse_args(argv)
    if args.command == "run":
        _print(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
