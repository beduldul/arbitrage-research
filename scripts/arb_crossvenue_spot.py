"""Class B — cross-venue spot price differences for the same pair (measurement only).

**Measurement only. No transfers, no accounts, no orders, no keys.** This script never
attempts to move funds between venues; it only measures whether a *book-to-book* spread
exists between two venues quoting the same pair.

The honest caveat, stated once here and repeated in the report: **a positive book-to-book
spread is NOT capturable by a $100 account that must move funds.** Cross-venue
arbitrage needs *pre-positioned balances on both venues* (so the two legs settle
simultaneously, with no transfer), and it carries withdrawal/transfer latency, network
congestion, and the risk that the spread closes while the transfer is in flight. A $100
account that must move USDT between venues cannot capture a 5-bps spread — the transfer
cost and latency alone exceed it. This script quantifies the spread; it does not claim
capturability.

Reachable venues on this host are only **Binance Vision, Gate.io and HTX** — Bybit, OKX,
Kraken and every other major venue DNS-sinkhole to a blackhole IP here (see the
reachability matrix). The measurement is therefore three-venue.

Rate discipline: bounded concurrency (default 5), exponential backoff on 429/418/5xx.

Run::

    uv run python scripts/arb_crossvenue_spot.py --minutes 24 --interval 5
    uv run python scripts/arb_crossvenue_spot.py --probe-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Callable
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
    Book,
    FeeTier,
    max_positive_quote,
    parse_binance,
    parse_gate,
    parse_htx,
    summarize,
    walk_buy_asks,
    walk_sell_bids,
)

EVIDENCE = REPO / "evidence" / "arbitrage" / "2026-10-03" / "crossvenue"
RAW = EVIDENCE / "raw"
NOTIONAL = 100.0

BINANCE_BASE = "https://data-api.binance.vision"
GATE_BASE = "https://api.gateio.ws"
HTX_BASE = "https://api.huobi.pro"

#: Liquid pairs measured on every reachable venue. Same underlying asset, three books.
PAIRS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT")

GATE_SYMBOL = {p: f"{p[:-4]}_USDT" for p in PAIRS}
HTX_SYMBOL = {p: p.lower() for p in PAIRS}


@dataclass(frozen=True, slots=True)
class Venue:
    name: str
    base: str
    symbol_map: dict[str, str]
    parser: Callable[[dict[str, Any]], Book]
    fee: FeeTier
    #: Hard cap on the venue's depth parameter. HTX rejects ``depth>20`` with
    #: ``{"status":"error"}`` — a 100-level request silently returns an empty book, so
    #: the cap must be enforced here rather than discovered mid-window.
    max_depth: int = 100

    def symbol(self, pair: str) -> str:
        return self.symbol_map.get(pair, pair)

    def url(self, pair: str, limit: int) -> str:
        sym = self.symbol(pair)
        depth = min(limit, self.max_depth)
        if self.name == "binance":
            return f"{self.base}/api/v3/depth?symbol={sym}&limit={depth}"
        if self.name == "gate":
            return f"{self.base}/api/v4/spot/order_book?currency_pair={sym}&limit={depth}"
        return f"{self.base}/market/depth?symbol={sym}&depth={depth}&type=step0"


#: Venue fee assumptions, **all UNVERIFIED** — the fee-schedule pages are unreachable from
#: this host. Values are each venue's *published* base-tier spot taker rate, carried so the
#: netting is not silently uniform; the report also shows a uniform project-§10.2 column.
BINANCE = Venue("binance", BINANCE_BASE, {p: p for p in PAIRS}, parse_binance,
                FeeTier("binance", 10.0, 10.0, tier="spot VIP0 (published 0.10%)", verified=False))
GATE = Venue("gate", GATE_BASE, GATE_SYMBOL, parse_gate,
             FeeTier("gate", 10.0, 10.0, tier="spot VIP0 (published 0.10%)", verified=False))
HTX = Venue("htx", HTX_BASE, HTX_SYMBOL, parse_htx,
            FeeTier("htx", 20.0, 20.0, tier="spot base (published 0.20%)", verified=False),
            max_depth=20)

VENUES = (BINANCE, GATE, HTX)
#: Uniform project §10.2 taker on both legs — the conservative comparison column.
UNIFORM = FeeTier("project-base", 10.0, 10.0, tier="vip0_taker", verified=False)


def _utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _ts() -> float:
    return time.time()


class Limiter:
    """Bounded concurrency + exponential backoff over a shared async client."""

    def __init__(self, concurrency: int = 5, max_retries: int = 3, timeout_s: float = 20.0) -> None:
        self.sem = asyncio.Semaphore(concurrency)
        self.max_retries = max_retries
        self.n_429 = 0
        self.total_wait_s = 0.0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=min(10.0, timeout_s)),
            follow_redirects=True,
            headers={"User-Agent": "crypto-brain/0.1 (paper; arbitrage measurement)"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def probe(self, url: str) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            resp = await self._client.get(url)
            ms = (time.perf_counter() - started) * 1000.0
            return {"url": url, "status": resp.status_code, "latency_ms": round(ms, 1),
                    "bytes": len(resp.content), "error": None}
        except Exception as exc:  # noqa: BLE001
            ms = (time.perf_counter() - started) * 1000.0
            return {"url": url, "status": 0, "latency_ms": round(ms, 1), "bytes": 0,
                    "error": type(exc).__name__}

    async def get_json(self, url: str) -> Any | None:
        async with self.sem:
            delay = 1.0
            for attempt in range(self.max_retries + 1):
                try:
                    resp = await self._client.get(url)
                    if resp.status_code in (429, 418) and attempt < self.max_retries:
                        self.n_429 += 1
                        self.total_wait_s += min(delay, 60.0)
                        await asyncio.sleep(min(delay, 60.0))
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
                except Exception:  # noqa: BLE001
                    if attempt < self.max_retries:
                        await asyncio.sleep(delay)
                        delay = min(delay * 2.0, 60.0)
                        continue
                    return None
            return None


# --------------------------------------------------------------- edge math


def cross_venue_net_bps(
    buy_book: Book,
    sell_book: Book,
    buy_fee_bps: float,
    sell_fee_bps: float,
    notional_quote: float,
) -> float | None:
    """Buy on ``buy_book`` (its ask), sell the received base on ``sell_book`` (its bid).

    Both legs are charged: the buy venue's fee on the base received, the sell venue's
    fee on the quote proceeds. Depth-walked at the requested size; ``None`` when either
    book cannot fill. Never uses a mid.
    """
    base = walk_buy_asks(buy_book.asks, notional_quote, buy_fee_bps / BPS)
    if base is None:
        return None
    proceeds = walk_sell_bids(sell_book.bids, base, sell_fee_bps / BPS)
    if proceeds is None:
        return None
    return (proceeds - notional_quote) / notional_quote * BPS


def cross_venue_gross_bps(buy_book: Book, sell_book: Book, notional_quote: float) -> float | None:
    """Same direction, zero fees — the raw book-to-book spread, reference only."""
    base = walk_buy_asks(buy_book.asks, notional_quote, 0.0)
    if base is None:
        return None
    proceeds = walk_sell_bids(sell_book.bids, base, 0.0)
    if proceeds is None:
        return None
    return (proceeds - notional_quote) / notional_quote * BPS


# ----------------------------------------------------------------- collect


async def collect(args: argparse.Namespace) -> dict[str, Any]:
    limiter = Limiter(concurrency=args.concurrency)
    RAW.mkdir(parents=True, exist_ok=True)
    EVIDENCE.mkdir(parents=True, exist_ok=True)

    # Reachability: one probe per venue for the flagship pair, plus one per pair on HTX
    # (its symbol shape differs, so a bad mapping must surface here, not mid-window).
    probes: list[tuple[str, str]] = [
        ("binance_vision_depth_BTCUSDT", f"{BINANCE_BASE}/api/v3/depth?symbol=BTCUSDT&limit=20"),
        ("gate_spot_BTC_USDT",
         f"{GATE_BASE}/api/v4/spot/order_book?currency_pair=BTC_USDT&limit=20"),
        ("htx_spot_btcusdt", f"{HTX_BASE}/market/depth?symbol=btcusdt&depth=20&type=step0"),
    ]
    probes += [(f"htx_{s}", f"{HTX_BASE}/market/depth?symbol={s}&depth=20&type=step0")
               for s in HTX_SYMBOL.values()]
    reach: list[dict[str, Any]] = []
    for name, url in probes:
        row = await limiter.probe(url)
        row["name"] = name
        reach.append(row)
        print(
            f"[reach] {name:<34} {row['status']} {row['latency_ms']}ms {row['bytes']}B",
            flush=True,
        )

    if args.probe_only:
        await limiter.close()
        return {"reachability": reach, "samples": 0}

    directions = [(a, b) for a in VENUES for b in VENUES if a.name != b.name]
    raw_paths = {(v.name, p): RAW / f"{v.name}__{p}.jsonl" for v in VENUES for p in PAIRS}
    handles = {k: p.open("a", encoding="utf-8") for k, p in raw_paths.items()}

    series: dict[str, list[float | None]] = {}
    for a, b in directions:
        for p in PAIRS:
            for tag in ("net_pub", "net_uniform", "gross", "max_pos"):
                series[f"{a.name}>{b.name}__{p}__{tag}"] = []
    per_pair_last: dict[str, Any] = {}
    n_samples = 0
    deadline = time.time() + args.minutes * 60.0

    while time.time() < deadline:
        loop_started = time.time()
        jobs = [(v.name, p, v.url(p, args.depth_limit)) for v in VENUES for p in PAIRS]
        results = await asyncio.gather(*[limiter.get_json(url) for _, _, url in jobs])
        books: dict[tuple[str, str], Book] = {}
        by_name = {v.name: v for v in VENUES}
        for (venue_name, pair, _url), payload in zip(jobs, results, strict=False):
            if not isinstance(payload, dict):
                continue
            book = by_name[venue_name].parser(payload)
            books[(venue_name, pair)] = book
            handles[(venue_name, pair)].write(
                json.dumps({"ts": _ts(), "iso": _utc(), "pair": pair,
                            "bids": book.bids, "asks": book.asks}) + "\n"
            )

        for a, b in directions:
            for p in PAIRS:
                bk, sk = books.get((a.name, p)), books.get((b.name, p))
                if bk is None or sk is None or bk.empty or sk.empty:
                    for tag in ("net_pub", "net_uniform", "gross", "max_pos"):
                        series[f"{a.name}>{b.name}__{p}__{tag}"].append(None)
                    continue
                net_pub = cross_venue_net_bps(bk, sk, a.fee.taker_bps, b.fee.taker_bps, NOTIONAL)
                net_uni = cross_venue_net_bps(
                    bk, sk, UNIFORM.taker_bps, UNIFORM.taker_bps, NOTIONAL
                )
                gross = cross_venue_gross_bps(bk, sk, NOTIONAL)
                key = f"{a.name}>{b.name}__{p}"
                series[f"{key}__net_pub"].append(net_pub)
                series[f"{key}__net_uniform"].append(net_uni)
                series[f"{key}__gross"].append(gross)
                series[f"{key}__max_pos"].append(
                    round(max_positive_quote(
                        lambda size, bk=bk, sk=sk, a=a, b=b: cross_venue_net_bps(
                            bk, sk, a.fee.taker_bps, b.fee.taker_bps, size)), 4)
                    if net_pub is not None else None
                )
                per_pair_last[key] = {
                    "iso": _utc(), "buy_ask": bk.best_ask, "sell_bid": sk.best_bid,
                    "net_pub_bps": round(net_pub, 4) if net_pub is not None else None,
                    "gross_bps": round(gross, 4) if gross is not None else None,
                }

        n_samples += 1
        if n_samples % 12 == 0 or n_samples == 1:
            elapsed = time.time() - (deadline - args.minutes * 60)
            print(f"[sample {n_samples}] {_utc()} elapsed={elapsed:.0f}s",
                  flush=True)
        for handle in handles.values():
            handle.flush()
        await asyncio.sleep(max(0.0, args.interval - (time.time() - loop_started)))

    for handle in handles.values():
        handle.close()
    await limiter.close()
    return {
        "reachability": reach,
        "samples": n_samples,
        "series": series,
        "per_pair_last": per_pair_last,
        "rate": {"n_429": limiter.n_429, "total_wait_s": round(limiter.total_wait_s, 1),
                 "concurrency": args.concurrency},
        "venues": {v.name: {"taker_bps": v.fee.taker_bps, "tier": v.fee.tier,
                            "verified": v.fee.verified} for v in VENUES},
    }


# ------------------------------------------------------------------ report


def build_report(data: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    series = data.get("series", {})
    directions: dict[str, Any] = {}
    for key, vals in series.items():
        head, tag = key.rsplit("__", 1)
        directions.setdefault(head, {})[tag] = vals

    table: dict[str, Any] = {}
    for head, tags in directions.items():
        net_pub = summarize(tags.get("net_pub", []))
        net_uni = summarize(tags.get("net_uniform", []))
        gross = summarize(tags.get("gross", []))
        max_pos = tags.get("max_pos", [])
        table[head] = {
            "gross": gross,
            "net_pub": net_pub,
            "net_uniform": net_uni,
            "max_positive_usd": round(max((v for v in max_pos if v is not None), default=0.0), 2),
            "last": data.get("per_pair_last", {}).get(head),
        }

    positive_pub = [k for k, v in table.items() if (v["net_pub"]["mean_bps"] or -1) > 0]
    positive_gross = [k for k, v in table.items() if (v["gross"]["mean_bps"] or -1) > 0]
    return {
        "generated_iso": _utc(),
        "notional_usd": NOTIONAL,
        "window_minutes": args.minutes,
        "interval_s": args.interval,
        "samples": data.get("samples", 0),
        "reachability": data.get("reachability", []),
        "rate": data.get("rate", {}),
        "venues": data.get("venues", {}),
        "directions": table,
        "positive_mean_gross": positive_gross,
        "positive_mean_net_published": positive_pub,
        "verdict_counts": {
            "directions": len(table),
            "gross_positive_mean": len(positive_gross),
            "net_positive_mean_published_fees": len(positive_pub),
        },
    }


def write_report(report: dict[str, Any], args: argparse.Namespace) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (EVIDENCE / "run_meta.json").write_text(json.dumps({
        "generated_iso": report["generated_iso"], "window_minutes": args.minutes,
        "interval_s": args.interval, "concurrency": args.concurrency,
        "depth_limit": args.depth_limit, "notional_usd": NOTIONAL,
        "fee_note": "per-venue published base tier, ALL UNVERIFIED (fee pages unreachable); "
                    "net_uniform column uses the project's 10 bps on both legs",
    }, indent=2), encoding="utf-8")

    lines: list[str] = []
    lines.append("# Cross-venue spot measurement (same pair, multiple venues) — 2026-10-03")
    lines.append("")
    lines.append(
        f"- Notional: **${NOTIONAL:.2f}**  |  window: **{args.minutes} min** "
        f"@ {args.interval}s  |  samples: **{report['samples']}**"
    )
    lines.append(
        "- Model: buy at venue A's ask, sell the received base at venue B's bid, **never mid**."
    )
    lines.append("- Fees: each venue's published base-tier taker, **ALL UNVERIFIED** "
                 "(fee-schedule pages are unreachable from this host); a uniform 10 bps/leg "
                 "column is also shown.")
    lines.append("")
    lines.append(
        "> **Honest caveat — capturability.** A positive book-to-book spread is **NOT** capturable"
    )
    lines.append(
        "> by a $100 account that must move funds between venues. Cross-venue arbitrage "
        "requires"
    )
    lines.append(
        "> **pre-positioned balances on both venues** (both legs settle simultaneously, "
        "no transfer),"
    )
    lines.append(
        "> and even then carries withdrawal/transfer latency, congestion, and the risk that "
        "the spread"
    )
    lines.append(
        "> closes in flight. This table measures the spread; it does not claim a capturable edge."
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
    lines.append("## Venue fee assumptions (all UNVERIFIED)")
    lines.append("")
    lines.append("| venue | taker bps/leg | tier | verified |")
    lines.append("|---|---:|---|---|")
    for name, v in report["venues"].items():
        lines.append(f"| {name} | {v['taker_bps']} | {v['tier']} | {v['verified']} |")
    lines.append("")
    lines.append("## Direction table ($100, buy ask on A / sell bid on B)")
    lines.append("")
    lines.append(
        "| direction | pair | gross bps mean | net bps mean (pub fees) | "
        "net bps mean (10bps uniform) | %pos (net pub) | run | max +$ |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for head, v in report["directions"].items():
        direction, pair = head.split("__", 1)
        g, n, u = v["gross"], v["net_pub"], v["net_uniform"]
        lines.append(
            f"| {direction} | {pair} | {g['mean_bps']} | {n['mean_bps']} | {u['mean_bps']} | "
            f"{n['pct_positive']} | {n['longest_positive_run']} | {v['max_positive_usd']} |"
        )
    lines.append("")
    lines.append("## Verdicts")
    lines.append("")
    vc = report["verdict_counts"]
    lines.append(f"- directions measured: **{vc['directions']}**")
    lines.append(
        f"- directions with positive **mean gross** spread: **{vc['gross_positive_mean']}**"
    )
    lines.append(
        f"- directions with positive **mean net** at published fees: "
        f"**{vc['net_positive_mean_published_fees']}**"
    )
    lines.append("")
    (EVIDENCE / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--minutes", type=float, default=24.0)
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--depth-limit", type=int, default=100)
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--probe-only", action="store_true")
    args = p.parse_args(argv)

    started = time.time()
    data = asyncio.run(collect(args))
    report = build_report(data, args)
    write_report(report, args)
    print(f"[done] samples={report['samples']} "
          f"429s={data.get('rate', {}).get('n_429')} wall={time.time() - started:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
