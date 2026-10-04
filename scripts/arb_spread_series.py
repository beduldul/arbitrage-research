#!/usr/bin/env python3
"""Half-spread time-series + bootstrap-seed robustness (MEASUREMENT ONLY).

Two mechanical measurements the WARP report left open:

  A. **Half-spread time-series.** The report's cross-venue funding spread used *live
     snapshot* order books for its cost side. Here we poll both legs' books on both
     venues over a bounded window and report the distribution (mean/median/p90/max) of
     the executable half-spread per leg, plus the 4-crossing round-trip total. We then
     compare that to the snapshot the report used and say whether the snapshot
     under- or over-stated cost.

  B. **Bootstrap-seed robustness.** The report ran ONE seed (20261003) at ONE block (5).
     We re-run the IS/OOS + moving-block-bootstrap gate over >=5 seeds x >=3 block sizes
     and count how many (seed, block) combinations keep the OOS CI excluding zero.

No keys, no wallets, no orders, no transactions. Public endpoints only. Raw responses are
cached under ``evidence/arbitrage/2026-10-03/spread_series/raw/``. The funding series are
read from the already-cached ``funding_warp.json`` run's raw files (reconstructed exactly).

Usage::

    uv run python scripts/arb_spread_series.py sample --minutes 25 --interval 12
    uv run python scripts/arb_spread_series.py robustness
    uv run python scripts/arb_spread_series.py report
"""

from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import arb_funding_decisive as dc  # noqa: E402
import arb_funding_spread as sp  # noqa: E402
import arb_warp_scan as ws  # noqa: E402
import instrument_identity as ii  # noqa: E402

BPS = 10_000.0
LEG_USD = 50.0
CROSSINGS = 4
DAY_MS = 86_400_000
BUCKET_MS = 28_800_000  # 8h

WARP_DIR = REPO_ROOT / "evidence" / "arbitrage" / "2026-10-03" / "warp"
FUNDING_JSON = WARP_DIR / "funding_warp.json"
WARP_RAW = WARP_DIR / "raw"
OUT_DIR = REPO_ROOT / "evidence" / "arbitrage" / "2026-10-03" / "spread_series"
RAW_OUT = OUT_DIR / "raw"

FUNDING_DAYS = 30.0

#: Bounded concurrency (siblings share this WARP IP) and per-host pacing.
CONCURRENCY = 3
RATE_PER_S = 5.0
TIMEOUT_S = 20.0

UA = "crypto-brain-arb-spread-series/1.0 (paper research; read-only)"


# ===========================================================================
# Pure computation — the part the unit tests pin
# ===========================================================================


@dataclass(frozen=True)
class Book:
    """Top-of-book levels: ``bids`` descending, ``asks`` ascending, (price, size)."""

    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]

    def best_bid(self) -> float:
        return max(p for p, _ in self.bids)

    def best_ask(self) -> float:
        return min(p for p, _ in self.asks)


def parse_book(venue: str, payload: object) -> Book | None:
    """Venue payload -> ``Book``, or ``None`` if absent/malformed (never a fake zero)."""
    try:
        if venue == "binance":
            d = payload if isinstance(payload, dict) else {}
            bids = [(float(p), float(s)) for p, s in d["bids"]]
            asks = [(float(p), float(s)) for p, s in d["asks"]]
        elif venue == "kraken_futures":
            ob = (payload or {}).get("orderBook") if isinstance(payload, dict) else None
            if not isinstance(ob, dict):
                return None
            bids = [(float(p), float(s)) for p, s in ob["bids"]]
            asks = [(float(p), float(s)) for p, s in ob["asks"]]
        elif venue == "bybit":
            res = (payload or {}).get("result") if isinstance(payload, dict) else None
            if not isinstance(res, dict):
                return None
            bids = [(float(p), float(s)) for p, s in res["b"]]
            asks = [(float(p), float(s)) for p, s in res["a"]]
        elif venue == "okx":
            rows = (payload or {}).get("data") if isinstance(payload, dict) else None
            if not rows:
                return None
            row = rows[0]
            bids = [(float(p), float(s)) for p, s, *_ in row["bids"]]
            asks = [(float(p), float(s)) for p, s, *_ in row["asks"]]
        elif venue == "gate":
            d = payload if isinstance(payload, dict) else {}
            bids = [(float(lvl["p"]), float(lvl["s"])) for lvl in d["bids"]]
            asks = [(float(lvl["p"]), float(lvl["s"])) for lvl in d["asks"]]
        elif venue == "bitget":
            d = (payload or {}).get("data") if isinstance(payload, dict) else None
            if not isinstance(d, dict):
                return None
            bids = [(float(p), float(s)) for p, s in d["bids"]]
            asks = [(float(p), float(s)) for p, s in d["asks"]]
        elif venue == "hyperliquid":
            levels = (payload or {}).get("levels") if isinstance(payload, dict) else None
            if not levels:
                return None
            bids = [(float(x["px"]), float(x["sz"])) for x in levels[0]]
            asks = [(float(x["px"]), float(x["sz"])) for x in levels[1]]
        else:
            return None
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    bids = tuple(sorted(((p, s) for p, s in bids if p > 0 and s > 0), key=lambda x: -x[0]))
    asks = tuple(sorted(((p, s) for p, s in asks if p > 0 and s > 0), key=lambda x: x[0]))
    if not bids or not asks:
        return None
    return Book(bids=bids, asks=asks)


def half_spread_bps(book: Book) -> float:
    """Top-of-book half-spread in bps: ``(ask - bid) / mid / 2 * 1e4``.

    Matches the report's snapshot metric (``_half_spread_and_depth``) exactly, so the
    time-series and the snapshot are directly comparable.
    """
    bid, ask = book.best_bid(), book.best_ask()
    if bid <= 0 or ask <= 0:
        raise ValueError("non-positive touch")
    return (ask - bid) / ((ask + bid) / 2.0) * BPS / 2.0


def walk_cost_bps(book: Book, *, side: str, notional_usd: float) -> float:
    """Average fill price for ``notional_usd`` vs the mid, in bps (one side, one crossing).

    ``side='buy'`` consumes asks, ``side='sell'`` consumes bids. Returns ``None``-free
    ``inf`` if the book cannot fill the notional. This is the *executable* cost the
    snapshot's top-of-book number ignores.
    """
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    if notional_usd <= 0:
        raise ValueError("notional_usd must be positive")
    levels = book.asks if side == "buy" else book.bids
    remaining = notional_usd
    filled_usd = 0.0
    filled_qty = 0.0
    for price, size in levels:
        level_usd = price * size
        take = min(remaining, level_usd)
        filled_usd += take
        filled_qty += take / price
        remaining -= take
        if remaining <= 1e-9:
            break
    if filled_qty <= 0 or remaining > 1e-6:
        return float("inf")
    avg = filled_usd / filled_qty
    mid = (book.best_bid() + book.best_ask()) / 2.0
    sign = 1.0 if side == "buy" else -1.0
    return sign * (avg - mid) / mid * BPS


def four_crossing_total_bps(hs_a: float, hs_b: float, *, crossings: int = CROSSINGS) -> float:
    """Round-trip spread cost in bps of *leg* notional for both legs, entry + exit.

    Each leg is crossed once to open and once to close, and each crossing costs that
    leg's half-spread: total = ``(crossings / 2) * (hs_a + hs_b)``.
    """
    return (crossings / 2.0) * (hs_a + hs_b)


def percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolation percentile (``q`` in [0, 1]) of an ascending list."""
    if not sorted_values:
        raise ValueError("empty series")
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def summarize(values: list[float]) -> dict[str, float]:
    """mean / median / p90 / max / n for a series of half-spreads (bps)."""
    if not values:
        raise ValueError("empty series")
    ordered = sorted(values)
    return {
        "n": float(len(values)),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "p90": percentile(ordered, 0.90),
        "max": ordered[-1],
    }


def robustness_scan(
    spreads: list[float],
    *,
    blocks: tuple[int, ...] = (3, 5, 10),
    seeds: tuple[int, ...] = (20261003, 1, 7, 42, 99, 1234, 31337),
    n_boot: int = 2000,
    alpha: float = 0.05,
) -> dict[str, Any]:
    """Run the IS/OOS + moving-block-bootstrap gate over a grid of (seed, block).

    Returns per-combination signed OOS CI and the count that exclude zero, plus the
    single (seed, block) the report used. ``spreads`` are fractions per bucket.
    """
    if not spreads:
        raise ValueError("empty spreads")
    sign = 1.0 if statistics.fmean(spreads) >= 0 else -1.0
    _, oos = sp.split_is_oos(spreads)
    if not oos:
        raise ValueError("no OOS half")
    n_oos = len(oos)
    combos: list[dict[str, Any]] = []
    n_excl = 0
    for block in blocks:
        for seed in seeds:
            lo, hi = dc.block_bootstrap_ci(oos, block=block, n_boot=n_boot, seed=seed, alpha=alpha)
            scale = BPS / max(1, n_oos)
            lo_bps, hi_bps = lo * scale, hi * scale
            slo, shi = (lo_bps, hi_bps) if sign > 0 else (-hi_bps, -lo_bps)
            excl = slo > 0.0
            n_excl += int(excl)
            combos.append(
                {"block": block, "seed": seed, "ci_low_bps": round(slo, 4),
                 "ci_high_bps": round(shi, 4), "excludes_zero": excl}
            )
    return {
        "sign": sign,
        "n_oos": n_oos,
        "n_combos": len(combos),
        "n_excludes_zero": n_excl,
        "combos": combos,
    }


# ===========================================================================
# Funding reconstruction (from the already-cached funding_warp run)
# ===========================================================================


def _pages(venue: str, symbol: str) -> list[object]:
    paths = sorted(
        glob.glob(str(WARP_RAW / venue / "funding" / f"{symbol}_*.json")),
        key=lambda p: int(p.rsplit("_", 1)[1].split(".")[0]),
    )
    out: list[object] = []
    for p in paths:
        try:
            out.append(json.loads(Path(p).read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _funding_anchor_ms() -> int:
    """The report run's own clock, hour-truncated: the floor its adapters used.

    ``arb_funding_spread`` floors history at ``time.time() - 30d`` *at run time*; anchoring
    to the run's ``generated_at`` (not wall-clock now) reproduces its series bit-for-bit.
    """
    generated = json.loads(FUNDING_JSON.read_text())["generated_at"]
    ms = int(datetime.fromisoformat(generated).timestamp() * 1000)
    return ms // 3_600_000 * 3_600_000


def load_funding(venue: str, symbol: str, *, days: float = FUNDING_DAYS) -> list[tuple[int, float]]:
    """Reconstruct ``(time_ms, rate)`` for one venue leg from the cached raw files.

    Mirrors each venue adapter in ``arb_funding_spread``/``arb_warp_scan`` exactly (the
    report's numbers reproduce bit-for-bit), so the robustness re-run uses the same series.
    """
    floor_ms = _funding_anchor_ms() - days * DAY_MS
    if venue == "binance":
        rows = json.loads((WARP_RAW / "binance" / "fapi_funding" / f"{symbol}.json").read_text())
        return sorted(
            (int(r["fundingTime"]), float(r["fundingRate"]))
            for r in rows
            if int(r["fundingTime"]) >= floor_ms
        )
    if venue == "kraken_futures":
        d = json.loads((WARP_RAW / "krakenfut" / "funding" / f"{symbol}.json").read_text())
        out: list[tuple[int, float]] = []
        for r in d["rates"]:
            ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).timestamp() * 1000
            if ts >= floor_ms:
                out.append((int(ts), float(r["relativeFundingRate"])))
        return sorted(out)
    if venue == "gate":
        rows = json.loads((WARP_RAW / "gate" / "funding" / f"{symbol}.json").read_text())
        return sorted(
            (int(r["t"]) * 1000, float(r["r"])) for r in rows if int(r["t"]) * 1000 >= floor_ms
        )
    out = []
    for page in _pages(venue, symbol):
        if venue == "bybit":
            rows = ((page or {}).get("result", {}) or {}).get("list", [])
            for r in rows:
                ts = int(r["fundingRateTimestamp"])
                if ts >= floor_ms:
                    out.append((ts, float(r["fundingRate"])))
        elif venue in ("okx", "bitget"):
            for r in (page or {}).get("data", []):
                ts = int(r["fundingTime"])
                if ts >= floor_ms:
                    out.append((ts, float(r["fundingRate"])))
    return sorted(set(out))


def bucket_8h(pts: list[tuple[int, float]]) -> dict[int, float]:
    """Sum funding into UTC-aligned 8h buckets (same convention as ``sp.bucket_funding``)."""
    out: dict[int, float] = {}
    for ts, rate in pts:
        key = ts // BUCKET_MS
        out[key] = out.get(key, 0.0) + rate
    return out


# ===========================================================================
# Pair selection
# ===========================================================================


def select_pairs(
    funding: dict[str, Any], limit: int = 12, *, require_identity: bool = True
) -> list[dict[str, Any]]:
    """Top ``limit`` executable pairs by net $/day that pass the identity guard.

    The guard rejects symbol collisions (``PF_USDTUSD`` parsed as base ``T``) and price
    divergences (ONE bitget/okx ~18.6%), so only genuinely delta-neutral pairs survive.
    ``T`` is force-included as the report's headline pair even when it fails the guard —
    it must be reported as INVALID, not silently dropped.
    """
    rows = [
        p
        for p in funding["pairs"]
        if p.get("constructible")
        and p.get("half_spread_a_bps") is not None
        and p.get("half_spread_b_bps") is not None
        and (p.get("capacity_usd") or 0) >= 100
        and p["venue_a"] != p["venue_b"]
    ]
    rows.sort(key=lambda p: -p["net_dollars_per_day_taker"])
    if require_identity:
        rows = [p for p in ii.audit_pairs(rows) if p["identity"]["valid"]]
    top = rows[:limit]
    headline = next(
        (
            p
            for p in funding["pairs"]
            if p["base"] == "T" and p["venue_a"] == "binance" and p["venue_b"] == "kraken_futures"
        ),
        None,
    )
    if headline is not None and not any(
        p["base"] == "T" and p["venue_a"] == "binance" for p in top
    ):
        top.append({**headline, "identity": ii.audit_pairs([headline])[0]["identity"]})
    return top


def unique_legs(pairs: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """De-duplicated ``(venue, symbol)`` legs across the selected pairs."""
    seen: dict[tuple[str, str], None] = {}
    for p in pairs:
        seen[(p["venue_a"], p["symbol_a"])] = None
        seen[(p["venue_b"], p["symbol_b"])] = None
    return sorted(seen)


# ===========================================================================
# Book fetching
# ===========================================================================


def book_url(venue: str, symbol: str) -> tuple[str, bytes | None]:
    """Return ``(url, post_body)`` for one venue leg. ``body=None`` => GET."""
    if venue == "binance":
        return f"https://fapi.binance.com/fapi/v1/depth?symbol={symbol}&limit=20", None
    if venue == "kraken_futures":
        return f"https://futures.kraken.com/derivatives/api/v3/orderbook?symbol={symbol}", None
    if venue == "bybit":
        return (
            f"https://api.bybit.com/v5/market/orderbook?category=linear&symbol={symbol}&limit=20",
            None,
        )
    if venue == "okx":
        return f"https://www.okx.com/api/v5/market/books?instId={symbol}&sz=20", None
    if venue == "gate":
        return (
            f"https://api.gateio.ws/api/v4/futures/usdt/order_book?contract={symbol}&limit=20",
            None,
        )
    if venue == "bitget":
        return (
            f"https://api.bitget.com/api/v2/mix/market/merge-depth?symbol={symbol}"
            "&productType=usdt-futures&limit=20",
            None,
        )
    if venue == "hyperliquid":
        body = json.dumps({"type": "l2Book", "coin": symbol}).encode()
        return "https://api.hyperliquid.xyz/info", body
    raise ValueError(f"unknown venue {venue}")


class Sampler:
    """Cached book client: bounded concurrency, per-host pacing, retry + backoff."""

    def __init__(self, out_dir: Path, *, concurrency: int = CONCURRENCY,
                 rate_per_s: float = RATE_PER_S, timeout: float = TIMEOUT_S) -> None:
        self.out_dir = out_dir
        self.timeout = timeout
        self._net = ws.Net(out_dir, concurrency=concurrency, rate_per_s=rate_per_s,
                           timeout=timeout)
        self.stats = {"requests": 0, "errors": 0, "ok": 0}

    def fetch(self, venue: str, symbol: str, *, rel: str) -> object | None:
        url, body = book_url(venue, symbol)
        try:
            payload = self._net.get(url, rel, data=body)
        except Exception:  # noqa: BLE001 — one leg must not sink the round
            self.stats["errors"] += 1
            return None
        self.stats["requests"] += 1
        return payload


def run_sample(*, minutes: float, interval: float, limit: int = 12) -> dict[str, Any]:
    """Poll both legs of the top pairs for ``minutes`` at ``interval`` seconds."""
    funding = json.loads(FUNDING_JSON.read_text())
    pairs = select_pairs(funding, limit=limit)
    legs = unique_legs(pairs)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_OUT).mkdir(parents=True, exist_ok=True)
    sampler = Sampler(RAW_OUT)

    start = time.time()
    deadline = start + minutes * 60.0
    rounds = 0
    manifest: list[dict[str, Any]] = []
    while time.time() < deadline:
        rounds += 1
        round_start = time.time()
        ts_ms = int(round_start * 1000)
        for venue, symbol in legs:
            rel = f"{venue}/{symbol}/{rounds:05d}.json"
            payload = sampler.fetch(venue, symbol, rel=rel)
            book = parse_book(venue, payload)
            rec: dict[str, Any] = {
                "round": rounds, "ts_ms": ts_ms, "venue": venue, "symbol": symbol,
                "ok": book is not None,
            }
            if book is not None:
                sampler.stats["ok"] += 1
                rec["best_bid"] = book.best_bid()
                rec["best_ask"] = book.best_ask()
                rec["half_spread_bps"] = round(half_spread_bps(book), 6)
                for side in ("buy", "sell"):
                    wc = walk_cost_bps(book, side=side, notional_usd=LEG_USD)
                    rec[f"walk_{side}_bps"] = None if wc == float("inf") else round(wc, 6)
            manifest.append(rec)
        elapsed = time.time() - round_start
        time.sleep(max(0.0, interval - elapsed))
    result = {
        "generated_at": datetime.now(UTC).isoformat(),
        "minutes": minutes, "interval_s": interval,
        "rounds": rounds, "legs": [{"venue": v, "symbol": s} for v, s in legs],
        "pairs": [
            {"base": p["base"], "venue_a": p["venue_a"], "symbol_a": p["symbol_a"],
             "venue_b": p["venue_b"], "symbol_b": p["symbol_b"],
             "snapshot_half_spread_a_bps": p["half_spread_a_bps"],
             "snapshot_half_spread_b_bps": p["half_spread_b_bps"],
             "snapshot_cost_taker_usd": p["cost_taker_usd"],
             "identity": p.get("identity", {})}
            for p in pairs
        ],
        "stats": sampler.stats,
        "samples": manifest,
    }
    (OUT_DIR / "series_manifest.json").write_text(json.dumps(result, indent=1))
    return result


# ===========================================================================
# Analysis / report
# ===========================================================================


def _load_manifest() -> dict[str, Any]:
    return json.loads((OUT_DIR / "series_manifest.json").read_text())


def rebuild_manifest(*, limit: int = 12) -> dict[str, Any]:
    """Reconstruct the manifest from the cached raw book files (crash-safe).

    The sampler writes one raw file per (venue, symbol, round); if it is interrupted
    before writing ``series_manifest.json``, the series is still fully recoverable here.
    """
    funding = json.loads(FUNDING_JSON.read_text())
    pairs = select_pairs(funding, limit=limit)
    legs = unique_legs(pairs)
    samples: list[dict[str, Any]] = []
    for venue, symbol in legs:
        files = sorted(glob.glob(str(RAW_OUT / venue / symbol / "*.json")))
        for f in files:
            path = Path(f)
            try:
                round_no = int(path.stem)
                payload = json.loads(path.read_text())
            except (ValueError, OSError, json.JSONDecodeError):
                continue
            ts_ms = int(path.stat().st_mtime * 1000)
            book = parse_book(venue, payload)
            rec: dict[str, Any] = {
                "round": round_no, "ts_ms": ts_ms, "venue": venue, "symbol": symbol,
                "ok": book is not None,
            }
            if book is not None:
                rec["best_bid"] = book.best_bid()
                rec["best_ask"] = book.best_ask()
                rec["half_spread_bps"] = round(half_spread_bps(book), 6)
                for side in ("buy", "sell"):
                    wc = walk_cost_bps(book, side=side, notional_usd=LEG_USD)
                    rec[f"walk_{side}_bps"] = None if wc == float("inf") else round(wc, 6)
            samples.append(rec)
    rounds = max((r["round"] for r in samples), default=0)
    result = {
        "generated_at": datetime.now(UTC).isoformat(),
        "rebuilt_from_raw": True,
        "rounds": rounds,
        "legs": [{"venue": v, "symbol": s} for v, s in legs],
        "pairs": [
            {"base": p["base"], "venue_a": p["venue_a"], "symbol_a": p["symbol_a"],
             "venue_b": p["venue_b"], "symbol_b": p["symbol_b"],
             "snapshot_half_spread_a_bps": p["half_spread_a_bps"],
             "snapshot_half_spread_b_bps": p["half_spread_b_bps"],
             "snapshot_cost_taker_usd": p["cost_taker_usd"],
             "identity": p.get("identity", {})}
            for p in pairs
        ],
        "samples": samples,
    }
    (OUT_DIR / "series_manifest.json").write_text(json.dumps(result, indent=1))
    return result


def analyze_series(manifest: dict[str, Any]) -> dict[str, Any]:
    """Per-leg half-spread distribution and per-pair 4-crossing totals vs snapshot."""
    by_leg: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for rec in manifest["samples"]:
        if rec.get("ok"):
            by_leg.setdefault((rec["venue"], rec["symbol"]), []).append(rec)

    leg_stats: dict[str, dict[str, Any]] = {}
    for (venue, symbol), recs in by_leg.items():
        hs = [r["half_spread_bps"] for r in recs]
        walk_buy = [r["walk_buy_bps"] for r in recs if r.get("walk_buy_bps") is not None]
        walk_sell = [r["walk_sell_bps"] for r in recs if r.get("walk_sell_bps") is not None]
        leg_stats[f"{venue}|{symbol}"] = {
            "venue": venue, "symbol": symbol, "n": len(hs),
            "half_spread": summarize(hs),
            "walk_buy_mean": statistics.fmean(walk_buy) if walk_buy else None,
            "walk_sell_mean": statistics.fmean(walk_sell) if walk_sell else None,
        }

    pair_rows: list[dict[str, Any]] = []
    for p in manifest["pairs"]:
        ka = f"{p['venue_a']}|{p['symbol_a']}"
        kb = f"{p['venue_b']}|{p['symbol_b']}"
        if ka not in leg_stats or kb not in leg_stats:
            continue
        a, b = leg_stats[ka]["half_spread"], leg_stats[kb]["half_spread"]
        snap_a = p["snapshot_half_spread_a_bps"]
        snap_b = p["snapshot_half_spread_b_bps"]
        meas_a, meas_b = a["mean"], b["mean"]
        pair_rows.append({
            "base": p["base"],
            "venue_a": p["venue_a"], "symbol_a": p["symbol_a"],
            "venue_b": p["venue_b"], "symbol_b": p["symbol_b"],
            "n_samples": min(leg_stats[ka]["n"], leg_stats[kb]["n"]),
            "hs_a_mean": meas_a, "hs_a_median": a["median"], "hs_a_p90": a["p90"],
            "hs_a_max": a["max"],
            "hs_b_mean": meas_b, "hs_b_median": b["median"], "hs_b_p90": b["p90"],
            "hs_b_max": b["max"],
            "snapshot_total_bps": four_crossing_total_bps(snap_a, snap_b),
            "measured_total_bps": four_crossing_total_bps(meas_a, meas_b),
            "measured_total_p90_bps": four_crossing_total_bps(a["p90"], b["p90"]),
            "measured_total_max_bps": four_crossing_total_bps(a["max"], b["max"]),
            "snapshot_vs_measured_ratio": (
                four_crossing_total_bps(meas_a, meas_b) / four_crossing_total_bps(snap_a, snap_b)
                if four_crossing_total_bps(snap_a, snap_b) > 0 else None
            ),
        })
    return {"legs": leg_stats, "pairs": pair_rows}


def pair_legs_from_funding(limit: int = 12) -> list[dict[str, Any]]:
    """The selected pairs expressed as plain leg dicts (works without a sample run)."""
    funding = json.loads(FUNDING_JSON.read_text())
    return [
        {"base": p["base"], "venue_a": p["venue_a"], "symbol_a": p["symbol_a"],
         "venue_b": p["venue_b"], "symbol_b": p["symbol_b"]}
        for p in select_pairs(funding, limit=limit)
    ]


def analyze_robustness(manifest: dict[str, Any]) -> dict[str, Any]:
    """Re-run the IS/OOS + bootstrap gate over the (seed x block) grid, per pair."""
    rows: list[dict[str, Any]] = []
    for p in manifest["pairs"]:
        a = bucket_8h(load_funding(p["venue_a"], p["symbol_a"]))
        b = bucket_8h(load_funding(p["venue_b"], p["symbol_b"]))
        _, _, _, spreads = sp.aligned_spread(a, b)
        scan = robustness_scan(spreads)
        rows.append({
            "base": p["base"], "venue_a": p["venue_a"], "venue_b": p["venue_b"],
            "n_buckets": len(spreads),
            "n_combos": scan["n_combos"],
            "n_excludes_zero": scan["n_excludes_zero"],
            "sign": scan["sign"],
            "combos": scan["combos"],
        })
    return {"blocks": [3, 5, 10], "seeds": [20261003, 1, 7, 42, 99, 1234, 31337],
            "n_boot": 2000, "pairs": rows}


def run_report() -> dict[str, Any]:
    manifest_path = OUT_DIR / "series_manifest.json"
    manifest = _load_manifest() if manifest_path.exists() else rebuild_manifest()
    series = analyze_series(manifest)
    rob = analyze_robustness(manifest)
    out = {
        "generated_at": datetime.now(UTC).isoformat(),
        "rebuilt_from_raw": manifest.get("rebuilt_from_raw", False),
        "sampling": {
            "rounds": manifest.get("rounds"),
            "legs": manifest.get("legs"),
            "stats": manifest.get("stats"),
        },
        "identity": {p["base"] + "/" + p["venue_a"] + "/" + p["venue_b"]: p.get("identity", {})
                     for p in manifest["pairs"]},
        "series": series,
        "robustness": rob,
    }
    (OUT_DIR / "spread_series_report.json").write_text(json.dumps(out, indent=1))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["sample", "robustness", "report"])
    parser.add_argument("--minutes", type=float, default=25.0)
    parser.add_argument("--interval", type=float, default=12.0)
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args(argv)

    if args.command == "sample":
        res = run_sample(minutes=args.minutes, interval=args.interval, limit=args.limit)
        print(f"sampled {res['rounds']} rounds, {res['stats']['ok']} ok / "
              f"{res['stats']['requests']} requests, {res['stats']['errors']} errors")
    elif args.command == "robustness":
        manifest_path = OUT_DIR / "series_manifest.json"
        if manifest_path.exists():
            manifest = _load_manifest()
        else:
            manifest = {"pairs": pair_legs_from_funding(limit=args.limit)}
        rob = analyze_robustness(manifest)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "robustness.json").write_text(json.dumps(rob, indent=1))
        for row in rob["pairs"]:
            print(f"{row['base']:>5} {row['venue_a']}/{row['venue_b']}: "
                  f"{row['n_excludes_zero']}/{row['n_combos']}")
    else:
        rep = run_report()
        print(json.dumps(rep["sampling"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
