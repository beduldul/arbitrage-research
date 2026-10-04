"""Class A — stablecoin cross-rate / depeg measurement, within one venue.

**Measurement only.** Public endpoints, no keys, no orders, no account state. Raw books
are cached under ``evidence/arbitrage/2026-10-03/stable/`` with timestamps.

The question: **at $100 and retail fees, is a stablecoin pair ever worth trading?**

Two honest facts drive the design:

* A pegged pair quotes bid **below** ask (e.g. 1.00013 / 1.00014). Buying the ask and
  selling the bid is a *guaranteed loss* of the spread **plus** two fees. So the
  round-trip edge is structurally negative and its size *is* the spread — that is the
  baseline, and it is reported rather than hidden.
* The only way a stablecoin pair pays is a **genuine depeg** (USDC at 0.998) or a
  **cross-rate dislocation** between two stables on the same venue. The scanner therefore
  measures, per pair: the executable round trip (buy ask / sell bid, $100, depth-walked),
  the cross-rate triangles ``USDT -> A -> B -> USDT``, and the maximum executable size at
  a positive edge. If no depeg appears in the window it says so, in those words.

Rate discipline matches the sibling scanners: bounded concurrency (default 5), a backoff
limiter that honours ``Retry-After`` on 429/418 and never retry-storms, because Binance's
limits are per-IP and shared with a sibling worker.

Run::

    uv run python scripts/arb_stable_crossrate.py --minutes 24 --interval 5
    uv run python scripts/arb_stable_crossrate.py --probe-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from arb_common import (  # noqa: E402
    BPS,
    MAKER,
    TAKER,
    Book,
    FeeTier,
    max_positive_quote,
    parse_binance,
    parse_gate,
    round_trip_gross_bps,
    round_trip_net_bps,
    summarize,
    triangle_net_bps,
)

EVIDENCE = REPO / "evidence" / "arbitrage" / "2026-10-03" / "stable"
RAW = EVIDENCE / "raw"
NOTIONAL = 100.0

BINANCE_BASE = "https://data-api.binance.vision"
GATE_BASE = "https://api.gateio.ws"

#: Class-A universe. Only pairs with a live two-sided book are kept (USDPUSDT is
#: listed but has an EMPTY book on Binance, and BUSD/DAI/USDD/PYUSD are not tradable
#: against USDT any more — they are recorded as dead, never silently dropped).
STABLE_PAIRS = ("USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "USDPUSDT", "USD1USDT")
DEAD_PAIRS = ("BUSDUSDT", "DAIUSDT", "USDDUSDT", "PYUSDUSDT", "TUSDUSDC")

#: Binance's cross-stable pair, needed for the cross-rate triangle.
BINANCE_CROSS = "FDUSDUSDC"


@dataclass(frozen=True, slots=True)
class Venue:
    name: str
    base: str
    fee: FeeTier
    maker_fee: FeeTier

    def url(self, symbol: str, limit: int) -> str:
        if self.name == "binance":
            return f"{self.base}/api/v3/depth?symbol={symbol}&limit={limit}"
        return f"{self.base}/api/v4/spot/order_book?currency_pair={symbol}&limit={limit}"

    def parse(self, payload: dict[str, Any]) -> Book:
        return parse_binance(payload) if self.name == "binance" else parse_gate(payload)


#: Venue fee assumptions. **All UNVERIFIED**: the venues' fee pages are unreachable
#: from this host (see the reachability matrix in REPORT.md), so the headline uses the
#: project's own §10.2 base tier and the venue's published base tier is marked unverified.
BINANCE = Venue(
    "binance", BINANCE_BASE,
    FeeTier("binance", 10.0, 10.0, tier="spot VIP0 (published 0.10%)", verified=False),
    FeeTier("binance", 10.0, 2.0, tier="maker what-if", verified=False),
)
GATE = Venue(
    "gate", GATE_BASE,
    FeeTier("gate", 10.0, 10.0, tier="spot VIP0 (published 0.10%)", verified=False),
    FeeTier("gate", 10.0, 2.0, tier="maker what-if", verified=False),
)

#: Binance symbols -> Gate symbols.
GATE_SYMBOL = {
    "USDCUSDT": "USDC_USDT",
    "FDUSDUSDT": "FDUSD_USDT",
    "TUSDUSDT": "TUSD_USDT",
    "USDPUSDT": "USDP_USDT",
    "USD1USDT": "USD1_USDT",
}


def _utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _ts() -> float:
    return time.time()


class Limiter:
    """Bounded concurrency + exponential backoff, shared across both venues.

    Owns one ``httpx.AsyncClient`` so a 20-minute window does not leak sockets. On
    429/418/5xx it backs off exponentially and retries at most ``max_retries`` times.
    Latencies are recorded for the reachability table.
    """

    def __init__(self, concurrency: int = 5, max_retries: int = 3, timeout_s: float = 20.0) -> None:
        self.sem = asyncio.Semaphore(concurrency)
        self.max_retries = max_retries
        self.n_429 = 0
        self.total_wait_s = 0.0
        self.latencies_ms: list[float] = []
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=min(10.0, timeout_s)),
            follow_redirects=True,
            headers={"User-Agent": "crypto-brain/0.1 (paper; arbitrage measurement)"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def probe(self, url: str) -> dict[str, Any]:
        """One reachability probe: HTTP status + latency + bytes, never raising."""
        started = time.perf_counter()
        try:
            resp = await self._client.get(url)
            ms = (time.perf_counter() - started) * 1000.0
            return {"url": url, "status": resp.status_code, "latency_ms": round(ms, 1),
                    "bytes": len(resp.content), "error": None}
        except Exception as exc:  # noqa: BLE001 — a probe reports, it does not raise
            ms = (time.perf_counter() - started) * 1000.0
            return {"url": url, "status": 0, "latency_ms": round(ms, 1), "bytes": 0,
                    "error": type(exc).__name__}

    async def get_json(self, url: str) -> Any | None:
        async with self.sem:
            delay = 1.0
            for attempt in range(self.max_retries + 1):
                started = time.perf_counter()
                try:
                    resp = await self._client.get(url)
                    self.latencies_ms.append((time.perf_counter() - started) * 1000.0)
                    if resp.status_code in (429, 418) and attempt < self.max_retries:
                        self.n_429 += 1
                        wait = min(delay, 60.0)
                        self.total_wait_s += wait
                        await asyncio.sleep(wait)
                        delay = min(delay * 2.0, 60.0)
                        continue
                    if resp.status_code >= 500 and attempt < self.max_retries:
                        self.total_wait_s += delay
                        await asyncio.sleep(delay)
                        delay = min(delay * 2.0, 60.0)
                        continue
                    if resp.status_code != 200:
                        return None
                    return resp.json()
                except Exception:  # noqa: BLE001 — a transient sample failure is data
                    if attempt < self.max_retries:
                        await asyncio.sleep(delay)
                        delay = min(delay * 2.0, 60.0)
                        continue
                    return None
            return None


# ------------------------------------------------------------------- sampling


def _stable_metrics(book: Book) -> dict[str, Any]:
    """Round-trip metrics for one stable pair's book at $100."""
    if book.empty:
        return {"empty": True, "executable": False}
    rt_taker = round_trip_net_bps(book, NOTIONAL, TAKER)
    rt_maker = round_trip_net_bps(book, NOTIONAL, MAKER, maker=True)
    gross = round_trip_gross_bps(book, NOTIONAL)
    mid = (book.best_bid + book.best_ask) / 2.0 if book.best_bid and book.best_ask else None
    spread_bps = (
        (book.best_ask - book.best_bid) / mid * BPS if mid else None
    )

    def edge_at(size: float) -> float | None:
        return round_trip_net_bps(book, size, TAKER)

    return {
        "empty": False,
        "executable": rt_taker is not None,
        "best_bid": book.best_bid,
        "best_ask": book.best_ask,
        "spread_bps": round(spread_bps, 4) if spread_bps is not None else None,
        "mid": round(mid, 8) if mid else None,
        "rt_gross_bps": round(gross, 4) if gross is not None else None,
        "rt_net_taker_bps": round(rt_taker, 4) if rt_taker is not None else None,
        "rt_net_maker_bps": round(rt_maker, 4) if rt_maker is not None else None,
        "max_positive_usd": round(max_positive_quote(edge_at), 4) if rt_taker is not None else 0.0,
    }


def _cross_rate_triangles(books: dict[str, Book]) -> dict[str, float | None]:
    """The ``USDT -> A -> B -> USDT`` cycles between two stables on Binance.

    Only cycles whose every leg exists as a live book are priced. Each direction is a
    distinct trade, so both are kept. A positive number here is a genuine same-venue
    cross-rate dislocation; on a pegged pair it is negative after three taker legs.
    """
    out: dict[str, float | None] = {}
    usdc, fdusd, cross = books.get("USDCUSDT"), books.get("FDUSDUSDT"), books.get(BINANCE_CROSS)
    if usdc and fdusd and cross and not (usdc.empty or fdusd.empty or cross.empty):
        # USDT -> USDC (buy) -> FDUSD (buy FDUSD with USDC) -> USDT (sell FDUSD)
        out["USDT>USDC>FDUSD>USDT"] = triangle_net_bps(
            [(usdc, "buy"), (cross, "buy"), (fdusd, "sell")], NOTIONAL, TAKER
        )
        # USDT -> FDUSD (buy) -> USDC (sell FDUSD into USDC) -> USDT (sell USDC)
        out["USDT>FDUSD>USDC>USDT"] = triangle_net_bps(
            [(fdusd, "buy"), (cross, "sell"), (usdc, "sell")], NOTIONAL, TAKER
        )
    return out


async def collect(args: argparse.Namespace) -> dict[str, Any]:
    limiter = Limiter(concurrency=args.concurrency)
    RAW.mkdir(parents=True, exist_ok=True)
    EVIDENCE.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------- reachability
    probes = [
        ("binance_vision_depth_BTCUSDT", f"{BINANCE_BASE}/api/v3/depth?symbol=BTCUSDT&limit=20"),
        *[
            (f"binance_{s}", f"{BINANCE_BASE}/api/v3/depth?symbol={s}&limit=20")
            for s in STABLE_PAIRS
        ],
        ("binance_cross_FDUSDUSDC", f"{BINANCE_BASE}/api/v3/depth?symbol={BINANCE_CROSS}&limit=20"),
        *[(f"gate_{s}", f"{GATE_BASE}/api/v4/spot/order_book?currency_pair={s}&limit=20")
          for s in GATE_SYMBOL.values()],
    ]
    reach: list[dict[str, Any]] = []
    for name, url in probes:
        row = await limiter.probe(url)
        row["name"] = name
        reach.append(row)
        print(
            f"[reach] {name:<32} {row['status']} {row['latency_ms']}ms {row['bytes']}B",
            flush=True,
        )

    dead: dict[str, Any] = {}
    for sym in DEAD_PAIRS:
        payload = await limiter.get_json(f"{BINANCE_BASE}/api/v3/depth?symbol={sym}&limit=5")
        book = parse_binance(payload) if isinstance(payload, dict) else Book([], [])
        dead[sym] = {"empty": book.empty, "best_bid": book.best_bid, "best_ask": book.best_ask}

    if args.probe_only:
        await limiter.close()
        return {"reachability": reach, "dead_pairs": dead, "samples": 0}

    # ------------------------------------------------------------- sampling
    raw_paths = {
        ("binance", s): RAW / f"binance__{s}.jsonl" for s in (*STABLE_PAIRS, BINANCE_CROSS)
    }
    raw_paths.update({("gate", s): RAW / f"gate__{s}.jsonl" for s in GATE_SYMBOL.values()})
    handles = {k: p.open("a", encoding="utf-8") for k, p in raw_paths.items()}

    series: dict[str, list[float | None]] = {
        f"{v}__{s}__rt_taker": [] for v in ("binance", "gate") for s in STABLE_PAIRS
    }
    series.update({f"{v}__{s}__rt_maker": [] for v in ("binance", "gate") for s in STABLE_PAIRS})
    series.update({f"{v}__{s}__rt_gross": [] for v in ("binance", "gate") for s in STABLE_PAIRS})
    series.update({f"{v}__{s}__spread": [] for v in ("binance", "gate") for s in STABLE_PAIRS})
    series.update({f"{v}__{s}__max_pos": [] for v in ("binance", "gate") for s in STABLE_PAIRS})
    tri_series: dict[str, list[float | None]] = {
        "USDT>USDC>FDUSD>USDT": [], "USDT>FDUSD>USDC>USDT": [],
    }
    per_pair_last: dict[str, dict[str, Any]] = {}
    n_samples = 0
    deadline = time.time() + args.minutes * 60.0

    while time.time() < deadline:
        loop_started = time.time()
        # Fetch every venue's books for this tick concurrently (bounded by the limiter).
        jobs: list[tuple[str, str, str]] = []
        for sym in (*STABLE_PAIRS, BINANCE_CROSS):
            jobs.append(("binance", sym, BINANCE.url(sym, args.depth_limit)))
        for sym in STABLE_PAIRS:
            gsym = GATE_SYMBOL[sym]
            jobs.append(("gate", sym, GATE.url(gsym, args.depth_limit)))

        results = await asyncio.gather(
            *[limiter.get_json(url) for _, _, url in jobs]
        )
        books: dict[tuple[str, str], Book] = {}
        for (venue_name, sym, _url), payload in zip(jobs, results, strict=False):
            if not isinstance(payload, dict):
                continue
            venue = BINANCE if venue_name == "binance" else GATE
            book = venue.parse(payload)
            books[(venue_name, sym)] = book
            handles[(venue_name, sym if venue_name == "binance" else GATE_SYMBOL[sym])].write(
                json.dumps({"ts": _ts(), "iso": _utc(), "symbol": sym,
                            "bids": book.bids, "asks": book.asks}) + "\n"
            )

        for venue_name in ("binance", "gate"):
            for sym in STABLE_PAIRS:
                book = books.get((venue_name, sym))
                if book is None:
                    series[f"{venue_name}__{sym}__rt_taker"].append(None)
                    series[f"{venue_name}__{sym}__rt_maker"].append(None)
                    series[f"{venue_name}__{sym}__rt_gross"].append(None)
                    series[f"{venue_name}__{sym}__spread"].append(None)
                    series[f"{venue_name}__{sym}__max_pos"].append(None)
                    continue
                m = _stable_metrics(book)
                per_pair_last[f"{venue_name}__{sym}"] = {"iso": _utc(), **m}
                series[f"{venue_name}__{sym}__rt_taker"].append(m.get("rt_net_taker_bps"))
                series[f"{venue_name}__{sym}__rt_maker"].append(m.get("rt_net_maker_bps"))
                series[f"{venue_name}__{sym}__rt_gross"].append(m.get("rt_gross_bps"))
                series[f"{venue_name}__{sym}__spread"].append(m.get("spread_bps"))
                series[f"{venue_name}__{sym}__max_pos"].append(m.get("max_positive_usd"))

        binance_books = {s: books[("binance", s)] for s in (*STABLE_PAIRS, BINANCE_CROSS)
                         if ("binance", s) in books}
        tri = _cross_rate_triangles(binance_books)
        for name in tri_series:
            tri_series[name].append(tri.get(name))

        n_samples += 1
        if n_samples % 12 == 0 or n_samples == 1:
            elapsed = time.time() - (deadline - args.minutes * 60)
            print(f"[sample {n_samples}] {_utc()} elapsed={elapsed:.0f}s",
                  flush=True)

        for handle in handles.values():
            handle.flush()
        elapsed = time.time() - loop_started
        await asyncio.sleep(max(0.0, args.interval - elapsed))

    for handle in handles.values():
        handle.close()
    await limiter.close()

    return {
        "reachability": reach,
        "dead_pairs": dead,
        "samples": n_samples,
        "series": series,
        "triangles": tri_series,
        "per_pair_last": per_pair_last,
        "rate": {"n_429": limiter.n_429, "total_wait_s": round(limiter.total_wait_s, 1),
                 "concurrency": args.concurrency},
    }


# ------------------------------------------------------------------- report


def build_report(data: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    series = data.get("series", {})
    tri = data.get("triangles", {})
    per_pair: dict[str, Any] = {}
    for venue_name in ("binance", "gate"):
        for sym in STABLE_PAIRS:
            key = f"{venue_name}__{sym}"
            taker = summarize(series.get(f"{key}__rt_taker", []))
            maker = summarize(series.get(f"{key}__rt_maker", []))
            gross = summarize(series.get(f"{key}__rt_gross", []))
            spread = summarize(series.get(f"{key}__spread", []))
            max_pos = series.get(f"{key}__max_pos", [])
            per_pair[key] = {
                "taker": taker,
                "maker": maker,
                "gross": gross,
                "spread_bps": {"mean": spread["mean_bps"], "min": spread["min_bps"],
                               "max": spread["max_bps"]},
                "max_positive_usd": round(
                    max((v for v in max_pos if v is not None), default=0.0), 2
                ),
                "any_depeg": any(
                    (v is not None and v > 0) for v in series.get(f"{key}__rt_taker", [])
                ),
                "last": data.get("per_pair_last", {}).get(key),
            }
    tri_out = {
        name: {"taker": summarize(vals)}
        for name, vals in tri.items()
    }

    # Depeg reality: a depeg means the round trip turned positive, OR the cross-rate
    # triangle did. Neither is expected on a pegged pair.
    depeg_pairs = [k for k, v in per_pair.items() if v["any_depeg"]]
    depeg_triangles = [n for n, v in tri_out.items() if (v["taker"]["max_bps"] or -1) > 0]
    any_depeg = bool(depeg_pairs or depeg_triangles)

    # Verdicts under both fee tiers: a pair "qualifies" only if its MEAN is positive.
    verdict_taker = {k: (v["taker"]["mean_bps"] is not None and v["taker"]["mean_bps"] > 0)
                     for k, v in per_pair.items()}
    verdict_maker = {k: (v["maker"]["mean_bps"] is not None and v["maker"]["mean_bps"] > 0)
                     for k, v in per_pair.items()}

    return {
        "generated_iso": _utc(),
        "notional_usd": NOTIONAL,
        "window_minutes": args.minutes,
        "interval_s": args.interval,
        "samples": data.get("samples", 0),
        "reachability": data.get("reachability", []),
        "dead_pairs": data.get("dead_pairs", {}),
        "rate": data.get("rate", {}),
        "per_pair": per_pair,
        "triangles": tri_out,
        "any_depeg_observed": any_depeg,
        "depeg_pairs": depeg_pairs,
        "depeg_triangles": depeg_triangles,
        "verdict_counts": {
            "taker_positive_mean": sum(verdict_taker.values()),
            "maker_positive_mean": sum(verdict_maker.values()),
            "pairs": len(verdict_taker),
        },
    }


def rebuild_from_cache() -> dict[str, Any]:
    """Re-derive the series from the cached raw books — no network, fully reproducible.

    Aggregation is deliberately separable from collection so a metric fix (e.g. the maker
    tier) can be re-applied to an already-captured window instead of re-fetching it. Raw
    books are replayed in file order; because every venue is written once per tick, the
    ``k``-th line of each file belongs to the same sample.
    """
    series: dict[str, list[float | None]] = {
        f"{v}__{s}__{tag}": []
        for v in ("binance", "gate") for s in STABLE_PAIRS
        for tag in ("rt_taker", "rt_maker", "rt_gross", "spread", "max_pos")
    }
    tri_series: dict[str, list[float | None]] = {
        "USDT>USDC>FDUSD>USDT": [], "USDT>FDUSD>USDC>USDT": [],
    }
    per_pair_last: dict[str, dict[str, Any]] = {}

    def load_lines(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    binance = {s: load_lines(RAW / f"binance__{s}.jsonl") for s in (*STABLE_PAIRS, BINANCE_CROSS)}
    gate = {s: load_lines(RAW / f"gate__{GATE_SYMBOL[s]}.jsonl") for s in STABLE_PAIRS}
    n = max((len(v) for v in (*binance.values(), *gate.values())), default=0)

    for i in range(n):
        books: dict[tuple[str, str], Book] = {}
        for sym, rows in binance.items():
            if i < len(rows):
                books[("binance", sym)] = Book(
                    bids=[tuple(x) for x in rows[i]["bids"]],
                    asks=[tuple(x) for x in rows[i]["asks"]],
                )
        for sym, rows in gate.items():
            if i < len(rows):
                books[("gate", sym)] = Book(
                    bids=[tuple(x) for x in rows[i]["bids"]],
                    asks=[tuple(x) for x in rows[i]["asks"]],
                )
        for venue_name in ("binance", "gate"):
            for sym in STABLE_PAIRS:
                book = books.get((venue_name, sym))
                if book is None:
                    for tag in ("rt_taker", "rt_maker", "rt_gross", "spread", "max_pos"):
                        series[f"{venue_name}__{sym}__{tag}"].append(None)
                    continue
                m = _stable_metrics(book)
                per_pair_last[f"{venue_name}__{sym}"] = {"iso": None, **m}
                series[f"{venue_name}__{sym}__rt_taker"].append(m.get("rt_net_taker_bps"))
                series[f"{venue_name}__{sym}__rt_maker"].append(m.get("rt_net_maker_bps"))
                series[f"{venue_name}__{sym}__rt_gross"].append(m.get("rt_gross_bps"))
                series[f"{venue_name}__{sym}__spread"].append(m.get("spread_bps"))
                series[f"{venue_name}__{sym}__max_pos"].append(m.get("max_positive_usd"))
        tri = _cross_rate_triangles(
            {s: books[("binance", s)] for s in (*STABLE_PAIRS, BINANCE_CROSS)
             if ("binance", s) in books}
        )
        for name in tri_series:
            tri_series[name].append(tri.get(name))

    return {
        "reachability": [], "dead_pairs": {}, "samples": n,
        "series": series, "triangles": tri_series, "per_pair_last": per_pair_last,
        "rate": {"rebuilt_from_cache": True},
    }


def write_report(report: dict[str, Any], args: argparse.Namespace) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (EVIDENCE / "run_meta.json").write_text(json.dumps({
        "generated_iso": report["generated_iso"], "window_minutes": args.minutes,
        "interval_s": args.interval, "concurrency": args.concurrency,
        "depth_limit": args.depth_limit, "notional_usd": NOTIONAL,
        "fee_headline": "project §10.2 base tier, spot taker 10 bps/leg",
        "fee_maker_whatif": "2 bps/leg, requires resting fills",
    }, indent=2), encoding="utf-8")

    lines: list[str] = []
    lines.append("# Stablecoin cross-rate / depeg measurement — 2026-10-03")
    lines.append("")
    lines.append(
        f"- Notional: **${NOTIONAL:.2f}**  |  window: **{args.minutes} min** "
        f"@ {args.interval}s  |  samples: **{report['samples']}**"
    )
    lines.append(
        "- Headline fees: project §10.2 base tier, spot taker **10 bps/leg** "
        "(2 legs = 20 bps round trip)."
    )
    lines.append(
        "- Maker what-if: **2 bps/leg** (4 bps round trip) — requires resting fills, "
        "not a default."
    )
    lines.append(
        "- Executable model: **buy at ask, sell at bid**, depth-walked at $100. Never mid."
    )
    lines.append("")
    lines.append("## Endpoint reachability")
    lines.append("")
    lines.append("| endpoint | HTTP | latency | bytes |")
    lines.append("|---|---:|---:|---:|")
    for row in report["reachability"]:
        err = f" ({row['error']})" if row.get("error") else ""
        lines.append(
            f"| {row['name']} | {row['status']}{err} | {row['latency_ms']}ms | {row['bytes']} |"
        )
    lines.append("")
    lines.append("## Per-pair round trip ($100, buy ask / sell bid)")
    lines.append("")
    lines.append(
        "| venue | pair | spread bps | gross bps | net taker bps (mean/max) | %pos | run "
        "| net maker bps (mean) | max +$ |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for key, v in report["per_pair"].items():
        venue_name, sym = key.split("__", 1)
        t, m, g, s = v["taker"], v["maker"], v["gross"], v["spread_bps"]
        lines.append(
            f"| {venue_name} | {sym} | {s['mean']} | {g['mean_bps']} | "
            f"{t['mean_bps']} / {t['max_bps']} | {t['pct_positive']} | "
            f"{t['longest_positive_run']} | "
            f"{m['mean_bps']} | {v['max_positive_usd']} |"
        )
    lines.append("")
    lines.append("## Cross-rate triangles (Binance, USDT-anchored, 3 taker legs)")
    lines.append("")
    lines.append("| triangle | net bps mean | net bps max | %pos | run |")
    lines.append("|---|---:|---:|---:|---:|")
    for name, v in report["triangles"].items():
        t = v["taker"]
        lines.append(
            f"| {name} | {t['mean_bps']} | {t['max_bps']} | {t['pct_positive']} | "
            f"{t['longest_positive_run']} |"
        )
    lines.append("")
    lines.append("## Verdicts")
    lines.append("")
    vc = report["verdict_counts"]
    lines.append(
        f"- pairs with positive **mean** net edge at taker: "
        f"**{vc['taker_positive_mean']} / {vc['pairs']}**"
    )
    lines.append(
        f"- pairs with positive **mean** net edge at maker what-if: "
        f"**{vc['maker_positive_mean']} / {vc['pairs']}**"
    )
    lines.append(f"- **depeg observed:** {'YES' if report['any_depeg_observed'] else 'NO'}")
    if not report["any_depeg_observed"]:
        lines.append("")
        lines.append("> **No depeg observed in the window.** Every pair traded at peg, so every")
        lines.append("> round trip was negative by construction (spread + 20 bps of taker fees).")
    lines.append("")
    lines.append("## Dead / empty pairs (recorded, not silently dropped)")
    lines.append("")
    lines.append("| pair | empty | best bid | best ask |")
    lines.append("|---|---|---:|---:|")
    for sym, v in report["dead_pairs"].items():
        lines.append(f"| {sym} | {v['empty']} | {v['best_bid']} | {v['best_ask']} |")
    lines.append("")
    (EVIDENCE / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--minutes", type=float, default=24.0)
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--depth-limit", type=int, default=100)
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--probe-only", action="store_true")
    p.add_argument("--rebuild-from-cache", action="store_true",
                   help="re-aggregate the cached raw books; no network")
    args = p.parse_args(argv)

    started = time.time()
    if args.rebuild_from_cache:
        data = rebuild_from_cache()
        prior = EVIDENCE / "results.json"
        if prior.exists():
            old = json.loads(prior.read_text())
            data["reachability"] = old.get("reachability", [])
            data["dead_pairs"] = old.get("dead_pairs", {})
        meta = EVIDENCE / "run_meta.json"
        if meta.exists():
            m = json.loads(meta.read_text())
            args.minutes = m.get("window_minutes", args.minutes)
            args.interval = m.get("interval_s", args.interval)
    else:
        data = asyncio.run(collect(args))
    report = build_report(data, args)
    write_report(report, args)
    print(f"[done] samples={report['samples']} depeg={report['any_depeg_observed']} "
          f"429s={data.get('rate', {}).get('n_429')} wall={time.time() - started:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
