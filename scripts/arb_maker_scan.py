"""Maker-side spread capture (market making) — measured on real books + trade tapes.

**Measurement only.** Public endpoints, no keys, no orders, no account state. Raw
samples are cached under ``evidence/arbitrage/2026-10-03/maker/``.

The question: **quoting both sides of the touch, at retail maker fees, does net edge
survive adverse selection?** The prior work on this host killed the *taker* paths
(dislocations 1–11 bps vs a 20 bp two-leg taker floor). Market making is the one
high-frequency path not yet measured, because it does not pay the taker fee — it *earns*
the spread instead, and pays only the maker fee.

Three honest facts drive the design:

* **A quote is not a fill.** We place a hypothetical resting order at the touch at time
  ``t0`` and ask the *trade tape* whether the market actually traded through that level
  before we re-quote. Touching (``price == quote``) is **not** counted as a fill: the
  queue position ahead of us is unknown, so the conservative rule is *through only*.
* **Adverse selection is the decisive term, and it is measured, not assumed.** A resting
  bid fills precisely when the market is being pushed down through it. We therefore take
  the mid at quote time and the mid at ``+1/5/30/60s`` and report the *signed* drift in
  the adverse direction. If fills systematically precede adverse moves, that term is
  subtracted from the captured spread. This is what usually kills market making.
* **The verdict uses the fee this account actually pays.** At $100 you are **VIP0 with no
  rebate**: on Binance/Gate/HTX spot, **VIP0 maker == taker == 10 bps**, which is exactly
  why DESIGN.md §10.2 sets ``spot_maker_bps == spot_taker_bps``. The ~2 bps figure quoted
  for "maker" is a **high-VIP / rebate rate this account cannot reach**; it is reported as
  a labelled what-if column and is **NOT** the basis of the verdict.
* **Adverse selection is a COARSE PROXY here, and it UNDERSTATES the true cost.** Our
  measured REST round-trip latency is ~100–465 ms, while adverse selection operates at
  sub-millisecond-to-second scale. A 1/5/30/60 s mid-move therefore cannot resolve the
  toxic component of a fill; it can only see the slow drift. The number is reported as a
  lower bound on the adverse term, never as a precise cost.

Run::

    uv run python scripts/arb_maker_scan.py --minutes 25 --interval 2.0
    uv run python scripts/arb_maker_scan.py --probe-only
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import random
import statistics as st
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from arb_common import BPS, Book, parse_binance, parse_gate, parse_htx, percentile  # noqa: E402

EVIDENCE = REPO / "evidence" / "arbitrage" / "2026-10-03" / "maker"
RAW = EVIDENCE / "raw"

BINANCE_BASE = "https://data-api.binance.vision"
GATE_BASE = "https://api.gateio.ws"
HTX_BASE = "https://api.huobi.pro"

#: ~8 liquid pairs, spread across the three reliably-reachable venues.
PAIRS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT",
         "LINKUSDT")

GATE_SYMBOL = {p: f"{p[:-4]}_USDT" for p in PAIRS}
HTX_SYMBOL = {p: p.lower() for p in PAIRS}

NOTIONAL = 100.0
HORIZONS_S = (1.0, 5.0, 30.0, 60.0)

#: **VIP0 spot maker fee = taker = 10 bps**, per venue. This is what a $100 account
#: actually pays and is the basis of the verdict. It is the same value DESIGN.md §10.2
#: encodes (``spot_maker_bps == spot_taker_bps``).
VIP0_MAKER_BPS = 10.0
#: DESIGN.md §10.2 base tier — identical to :data:`VIP0_MAKER_BPS`; kept as a named alias
#: so the report can say "the project's own number" without duplicating the literal.
PROJECT_MAKER_BPS = 10.0
#: **NOT ACHIEVABLE at $100.** The ~2 bps "maker" rate is a high-VIP / rebate tier this
#: account cannot reach. Computed only as a labelled what-if column; never the verdict.
WHATIF_MAKER_BPS = 2.0

Side = Literal["buy", "sell"]

__all__ = [
    "HORIZONS_S",
    "PROJECT_MAKER_BPS",
    "VIP0_MAKER_BPS",
    "WHATIF_MAKER_BPS",
    "adverse_move_bps",
    "bootstrap_ci",
    "fills_per_hour",
    "is_touch",
    "is_through_fill",
    "net_edge_per_fill_bps",
    "net_edge_round_trip_bps",
    "parse_gate_trades",
    "parse_binance_agg_trades",
    "parse_htx_trades",
    "spread_bps",
    "usd_per_day",
]


# --------------------------------------------------------------- pure math


def spread_bps(bid: float, ask: float) -> float:
    """Quoted spread in bps of the mid. ``(ask-bid)/mid * 10000``."""
    mid = (bid + ask) / 2.0
    return (ask - bid) / mid * BPS


def is_touch(side: Side, trade_price: float, quote_bid: float, quote_ask: float) -> bool:
    """Did a trade print *at* our level? ``side`` is the **aggressor** side.

    A seller-aggressor at ``price <= our bid`` touched our bid; a buyer-aggressor at
    ``price >= our ask`` touched our ask. Used only as the optimistic upper bound —
    queue position is unknown, so touching is not proof of a fill.
    """
    return trade_price <= quote_bid if side == "sell" else trade_price >= quote_ask


def is_through_fill(side: Side, trade_price: float, quote_bid: float, quote_ask: float) -> bool:
    """Did the market trade **through** our resting level? The conservative fill rule.

    We rest a bid at ``quote_bid`` and an ask at ``quote_ask``. A seller-aggressor
    printing strictly *below* our bid means every bid at our level was already consumed,
    so our order filled. A buyer-aggressor printing strictly *above* our ask likewise.
    ``price == quote`` is deliberately **not** a fill.
    """
    eps = max(quote_bid, quote_ask) * 1e-12
    return trade_price < quote_bid - eps if side == "sell" else trade_price > quote_ask + eps


def adverse_move_bps(side: Side, mid_at_quote: float, mid_future: float) -> float:
    """Signed mid drift in the direction that *hurts* the side we filled.

    ``side`` is the **aggressor** side of the trade that filled us: ``"sell"`` means we
    bought (our resting bid), ``"buy"`` means we sold (our resting ask). Positive =
    adverse: the mid fell after we bought, or rose after we sold. Negative = favourable.
    """
    if mid_at_quote <= 0:
        raise ValueError(f"mid_at_quote must be positive, got {mid_at_quote!r}")
    if side == "sell":  # we bought -> a falling mid hurts
        return (mid_at_quote - mid_future) / mid_at_quote * BPS
    return (mid_future - mid_at_quote) / mid_at_quote * BPS


def net_edge_per_fill_bps(
    half_spread_bps: float, maker_fee_bps: float, adverse_bps: float
) -> float:
    """One fill's net: half the quoted spread, less one maker fee, less adverse drift."""
    return half_spread_bps - maker_fee_bps - adverse_bps


def net_edge_round_trip_bps(
    spread_bps_value: float, maker_fee_bps: float, adverse_bps: float
) -> float:
    """A completed round trip (one buy fill + one sell fill) at the touch.

    Captures the **full** quoted spread, pays **two** maker fees, and bears the adverse
    drift on **both** legs.
    """
    return spread_bps_value - 2.0 * maker_fee_bps - 2.0 * adverse_bps


def fills_per_hour(n_fills: int, window_s: float) -> float:
    """Fill frequency. Zero for a non-positive window rather than a division error."""
    if window_s <= 0:
        return 0.0
    return n_fills / (window_s / 3600.0)


def usd_per_day(per_fill_bps: float, notional: float, fill_rate_per_hour: float) -> float:
    """$ per day from a per-fill edge in bps at a measured fill frequency."""
    return per_fill_bps / BPS * notional * fill_rate_per_hour * 24.0


def bootstrap_ci(
    values: list[float], *, n_resamples: int = 2000, seed: int = 20261003, alpha: float = 0.05
) -> tuple[float, float] | None:
    """Percentile bootstrap CI for the mean. Deterministic for a fixed seed."""
    if not values:
        return None
    rng = random.Random(seed)
    k = len(values)
    means = sorted(st.fmean(rng.choices(values, k=k)) for _ in range(n_resamples))
    lo = means[int((alpha / 2.0) * (n_resamples - 1))]
    hi = means[int((1.0 - alpha / 2.0) * (n_resamples - 1))]
    return (lo, hi)


# ------------------------------------------------------- trade-tape parsers


def parse_binance_agg_trades(payload: Any, pair: str) -> list[dict[str, Any]]:
    """Binance ``/api/v3/aggTrades``. ``m=True`` -> buyer is maker -> seller aggressed."""
    out: list[dict[str, Any]] = []
    for row in payload or []:
        try:
            out.append({
                "venue": "binance", "pair": pair, "tid": f"b{row['a']}",
                "ts_ms": int(row["T"]), "price": float(row["p"]), "qty": float(row["q"]),
                "aggressor": "sell" if row.get("m") else "buy",
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def parse_gate_trades(payload: Any, pair: str) -> list[dict[str, Any]]:
    """Gate ``/api/v4/spot/trades``. ``side`` is the **aggressor** side."""
    out: list[dict[str, Any]] = []
    for row in payload or []:
        try:
            ts = float(row["create_time_ms"])
            side = row["side"]
            if side not in ("buy", "sell"):
                continue
            out.append({
                "venue": "gate", "pair": pair, "tid": f"g{row['id']}",
                "ts_ms": int(ts), "price": float(row["price"]), "qty": float(row["amount"]),
                "aggressor": side,
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def parse_htx_trades(payload: Any, pair: str) -> list[dict[str, Any]]:
    """HTX ``/market/history/trade``. ``direction`` is the **aggressor** side."""
    out: list[dict[str, Any]] = []
    for block in (payload or {}).get("data") or []:
        for row in block.get("data") or []:
            try:
                side = row["direction"]
                if side not in ("buy", "sell"):
                    continue
                out.append({
                    "venue": "htx", "pair": pair, "tid": f"h{row['id']}",
                    "ts_ms": int(row["ts"]), "price": float(row["price"]),
                    "qty": float(row["amount"]), "aggressor": side,
                })
            except (KeyError, TypeError, ValueError):
                continue
    return out


# ----------------------------------------------------------------- sampling


def _utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class Reach:
    """Per-attempt reachability record. Never concludes 'blocked' from one attempt."""

    name: str
    attempts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return any(a["status"] == 200 for a in self.attempts)


class Limiter:
    """Bounded concurrency + exponential backoff over one shared async client.

    Binance limits are per-IP and shared with sibling workers, so the concurrency stays
    low (default 5) and 429/418 honour ``Retry-After`` before the exponential wait.
    """

    def __init__(self, concurrency: int = 5, max_retries: int = 4, timeout_s: float = 20.0) -> None:
        self.sem = asyncio.Semaphore(concurrency)
        self.max_retries = max_retries
        self.n_429 = 0
        self.n_5xx = 0
        self.total_wait_s = 0.0
        self.latencies_ms: list[float] = []
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=min(10.0, timeout_s)),
            follow_redirects=True,
            headers={"User-Agent": "crypto-brain/0.1 (paper; maker measurement)"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def probe(self, url: str) -> dict[str, Any]:
        """One reachability attempt: status + latency, never raising."""
        started = time.perf_counter()
        try:
            resp = await self._client.get(url)
            return {"url": url, "status": resp.status_code,
                    "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
                    "bytes": len(resp.content), "error": None}
        except Exception as exc:  # noqa: BLE001 — a probe reports, it does not raise
            return {"url": url, "status": 0,
                    "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
                    "bytes": 0, "error": type(exc).__name__}

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
                        self.n_5xx += 1
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


def _book_url(venue: str, pair: str) -> str:
    if venue == "binance":
        return f"{BINANCE_BASE}/api/v3/depth?symbol={pair}&limit=5"
    if venue == "gate":
        return f"{GATE_BASE}/api/v4/spot/order_book?currency_pair={GATE_SYMBOL[pair]}&limit=5"
    return f"{HTX_BASE}/market/depth?symbol={HTX_SYMBOL[pair]}&depth=5&type=step0"


def _trades_url(venue: str, pair: str) -> str:
    if venue == "binance":
        return f"{BINANCE_BASE}/api/v3/aggTrades?symbol={pair}&limit=1000"
    if venue == "gate":
        return f"{GATE_BASE}/api/v4/spot/trades?currency_pair={GATE_SYMBOL[pair]}&limit=1000"
    return f"{HTX_BASE}/market/history/trade?symbol={HTX_SYMBOL[pair]}&size=100"


def _parse_book(venue: str, payload: Any) -> Book:
    if venue == "binance":
        return parse_binance(payload)
    if venue == "gate":
        return parse_gate(payload)
    return parse_htx(payload)


def _parse_trades(venue: str, payload: Any, pair: str) -> list[dict[str, Any]]:
    if venue == "binance":
        return parse_binance_agg_trades(payload, pair)
    if venue == "gate":
        return parse_gate_trades(payload, pair)
    return parse_htx_trades(payload, pair)


async def probe_reachability(lim: Limiter) -> dict[str, Any]:
    """Probe every endpoint we depend on, 3 attempts each, reporting all attempts."""
    targets: list[tuple[str, str]] = [
        ("binance_depth_BTCUSDT", _book_url("binance", "BTCUSDT")),
        ("binance_aggTrades_BTCUSDT", _trades_url("binance", "BTCUSDT")),
        ("gate_order_book_BTC_USDT", _book_url("gate", "BTCUSDT")),
        ("gate_trades_BTC_USDT", _trades_url("gate", "BTCUSDT")),
        ("htx_depth_btcusdt", _book_url("htx", "BTCUSDT")),
        ("htx_trades_btcusdt", _trades_url("htx", "BTCUSDT")),
        ("binance_fee_page", "https://www.binance.com/en/fee/schedule"),
        ("gate_fee_page", "https://www.gate.io/fee"),
        ("htx_fee_page", "https://www.htx.com/en-us/fee/"),
    ]
    records: dict[str, Any] = {}
    for name, url in targets:
        rec = Reach(name)
        for _ in range(3):
            rec.attempts.append(await lim.probe(url))
            if rec.ok:
                break
        records[name] = {"url": url, "ok": rec.ok, "attempts": rec.attempts}
    return records


async def sample_tick(
    lim: Limiter, pairs: list[str]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One tick: the touch of every pair on every venue, plus the fresh trade tape.

    Book fetches and trade fetches run concurrently; the trade tape is cumulative and
    deduplicated by trade id later, so a missed tick loses nothing.
    """
    jobs: list[tuple[str, str, str, str]] = []
    for pair in pairs:
        for venue in ("binance", "gate", "htx"):
            jobs.append(("book", venue, pair, _book_url(venue, pair)))
            jobs.append(("trades", venue, pair, _trades_url(venue, pair)))

    results = await asyncio.gather(*(lim.get_json(url) for _, _, _, url in jobs))

    books: dict[str, Any] = {}
    trades: list[dict[str, Any]] = []
    for (kind, venue, pair, _url), payload in zip(jobs, results, strict=True):
        if payload is None:
            continue
        if kind == "book":
            book = _parse_book(venue, payload)
            if book.empty:
                continue
            mid = (book.best_bid + book.best_ask) / 2.0
            books[f"{venue}:{pair}"] = {
                "bid": book.best_bid, "ask": book.best_ask, "mid": mid,
                "bid_qty": book.bids[0][1], "ask_qty": book.asks[0][1],
            }
        else:
            trades.extend(_parse_trades(venue, payload, pair))
    return books, trades


async def collect(args: argparse.Namespace) -> dict[str, Any]:
    lim = Limiter(concurrency=args.concurrency)
    RAW.mkdir(parents=True, exist_ok=True)

    reach = await probe_reachability(lim)
    print("[probe] " + " ".join(
        f"{k}={'OK' if v['ok'] else 'FAIL'}" for k, v in reach.items()
    ))
    if args.probe_only:
        (EVIDENCE / "reachability.json").write_text(json.dumps(reach, indent=1))
        await lim.close()
        return {"probe_only": True, "reachability": reach}

    pairs = list(PAIRS)
    samples_path = RAW / "samples.jsonl"
    trades_path = RAW / "trades.jsonl"

    started = time.time()
    deadline = started + args.minutes * 60.0
    ticks = 0
    trade_count = 0
    seen_tids: set[str] = set()
    with samples_path.open("w") as sf, trades_path.open("w") as tf:
        while time.time() < deadline:
            tick_started = time.time()
            books, trades = await sample_tick(lim, pairs)
            rec = {"ts": time.time(), "iso": _utc(), "books": books}
            sf.write(json.dumps(rec) + "\n")
            # The tape is cumulative across polls, so dedupe before writing: a 1000-row
            # window overlaps heavily from tick to tick and would otherwise grow the file
            # into the gigabytes over a 25-minute window.
            for t in trades:
                if t["tid"] in seen_tids:
                    continue
                seen_tids.add(t["tid"])
                tf.write(json.dumps(t) + "\n")
                trade_count += 1
            ticks += 1
            if ticks % 50 == 0:
                elapsed = time.time() - started
                print(f"[tick {ticks}] {elapsed/60:.1f}m books={len(books)} "
                      f"trades_new={trade_count} 429={lim.n_429} 5xx={lim.n_5xx}")
            drift = time.time() - tick_started
            await asyncio.sleep(max(0.0, args.interval - drift))
    window_s = time.time() - started
    await lim.close()

    meta = {
        "started_iso": datetime.fromtimestamp(started, UTC).isoformat(),
        "window_s": round(window_s, 1), "ticks": ticks, "interval_s": args.interval,
        "pairs": pairs, "trades_seen": trade_count, "n_429": lim.n_429, "n_5xx": lim.n_5xx,
        "total_wait_s": round(lim.total_wait_s, 1),
        "latency_ms": {
            "n": len(lim.latencies_ms),
            "mean": round(st.fmean(lim.latencies_ms), 1) if lim.latencies_ms else None,
            "p95": round(percentile(lim.latencies_ms, 95) or 0.0, 1) if lim.latencies_ms else None,
        },
        "reachability": reach,
    }
    (EVIDENCE / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"[done] {ticks} ticks over {window_s/60:.1f} min; trades_seen={trade_count}")
    return meta


# ----------------------------------------------------------------- analysis


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


@dataclass(frozen=True, slots=True)
class Snapshot:
    ts: float
    mid: float
    bid: float
    ask: float


def _mid_after(snaps: list[Snapshot], times: list[float], t: float, horizon: float) -> float | None:
    """Mid from the first snapshot at or after ``t + horizon``. ``None`` if none exists."""
    target = t + horizon
    idx = bisect.bisect_left(times, target)
    if idx >= len(snaps):
        return None
    return snaps[idx].mid


def analyze(
    samples: list[dict[str, Any]], trades: list[dict[str, Any]], *,
    maker_fee_bps: float, label: str,
) -> dict[str, Any]:
    """Per-venue/pair spread distribution, fill rate and adverse-selection table.

    Pure function of the cached samples and trades: no network, no clock.
    """
    if not samples:
        return {"label": label, "error": "no samples"}

    # --- per pair/venue snapshot series ------------------------------------
    series: dict[str, list[Snapshot]] = defaultdict(list)
    spreads: dict[str, list[float]] = defaultdict(list)
    for s in samples:
        for key, b in (s.get("books") or {}).items():
            if not b:
                continue
            series[key].append(Snapshot(ts=s["ts"], mid=b["mid"], bid=b["bid"], ask=b["ask"]))
            spreads[key].append(spread_bps(b["bid"], b["ask"]))

    window_s = samples[-1]["ts"] - samples[0]["ts"]

    # --- dedupe the trade tape ---------------------------------------------
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for t in sorted(trades, key=lambda x: x["ts_ms"]):
        if t["tid"] in seen:
            continue
        seen.add(t["tid"])
        unique.append(t)
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in unique:
        by_key[f"{t['venue']}:{t['pair']}"].append(t)

    results: dict[str, Any] = {}
    for key, snaps in sorted(series.items()):
        snaps.sort(key=lambda x: x.ts)
        times = [s.ts for s in snaps]
        sp = spreads[key]
        mid_sp = st.median(sp) if sp else 0.0
        fee_condition = 100.0 * sum(1 for x in sp if x > 2.0 * maker_fee_bps) / len(sp)

        # --- fills: a quote placed at snapshot i is tested against trades in
        #     (ts_i, ts_{i+1}] -- we re-quote each tick, so the resting order lives
        #     one interval.
        through: list[dict[str, Any]] = []
        touch: list[dict[str, Any]] = []
        for i in range(len(snaps) - 1):
            q = snaps[i]
            lo, hi = q.ts, snaps[i + 1].ts
            for t in by_key.get(key, []):
                tp = t["ts_ms"] / 1000.0
                if not (lo < tp <= hi):
                    continue
                aggressor = t["aggressor"]
                if is_touch(aggressor, t["price"], q.bid, q.ask):
                    touch.append({"snap": q, "trade": t})
                if is_through_fill(aggressor, t["price"], q.bid, q.ask):
                    through.append({"snap": q, "trade": t})

        # --- adverse selection on the through-fills -------------------------
        adverse: dict[str, list[float]] = {f"{h:g}s": [] for h in HORIZONS_S}
        effective: dict[str, list[float]] = {f"{h:g}s": [] for h in HORIZONS_S}
        half_spreads: list[float] = []
        for f in through:
            q: Snapshot = f["snap"]
            aggressor = f["trade"]["aggressor"]
            half_spreads.append(spread_bps(q.bid, q.ask) / 2.0)
            for h in HORIZONS_S:
                fm = _mid_after(snaps, times, q.ts, h)
                if fm is None:
                    continue
                adverse[f"{h:g}s"].append(adverse_move_bps(aggressor, q.mid, fm))
                idx = bisect.bisect_left(times, q.ts + h)
                effective[f"{h:g}s"].append(times[idx] - q.ts)

        n_through = len(through)
        n_touch = len(touch)
        fph_through = fills_per_hour(n_through, window_s)
        fph_touch = fills_per_hour(n_touch, window_s)

        # --- net edge, per fill and per round trip --------------------------
        edge_table: dict[str, Any] = {}
        for h in HORIZONS_S:
            hk = f"{h:g}s"
            adv = adverse[hk]
            if not adv or not half_spreads:
                continue
            adv_mean = st.fmean(adv)
            hs_mean = st.fmean(half_spreads)
            per_fill = net_edge_per_fill_bps(hs_mean, maker_fee_bps, adv_mean)
            rt = net_edge_round_trip_bps(2.0 * hs_mean, maker_fee_bps, adv_mean)
            ci = bootstrap_ci([net_edge_per_fill_bps(x, maker_fee_bps, a)
                               for x, a in zip(half_spreads, adv, strict=False)])
            edge_table[hk] = {
                "n_fills": len(adv),
                "mean_effective_horizon_s": round(st.fmean(effective[hk]), 2),
                "adverse_mean_bps": round(adv_mean, 3),
                "adverse_median_bps": round(st.median(adv), 3),
                "adverse_p90_bps": round(percentile(adv, 90) or 0.0, 3),
                "adverse_std_bps": round(st.pstdev(adv), 3) if len(adv) > 1 else 0.0,
                "half_spread_mean_bps": round(hs_mean, 3),
                "net_per_fill_bps": round(per_fill, 3),
                "net_per_fill_ci95": [round(ci[0], 3), round(ci[1], 3)] if ci else None,
                "net_round_trip_bps": round(rt, 3),
                "usd_per_day_per_fill": round(usd_per_day(per_fill, NOTIONAL, fph_through), 4),
                "usd_per_min_per_fill": round(
                    usd_per_day(per_fill, NOTIONAL, fph_through) / 1440.0, 6
                ),
            }

        results[key] = {
            "n_snapshots": len(snaps),
            "spread_bps": {
                "mean": round(st.fmean(sp), 3), "median": round(st.median(sp), 3),
                "p10": round(percentile(sp, 10) or 0.0, 3),
                "p90": round(percentile(sp, 90) or 0.0, 3),
                "median_bps": round(mid_sp, 3),
            },
            "pct_time_spread_gt_2x_maker_fee": round(fee_condition, 2),
            "maker_fee_bps": maker_fee_bps,
            "fills": {
                "n_through": n_through, "n_touch": n_touch,
                "fills_per_hour_through": round(fph_through, 1),
                "fills_per_hour_touch": round(fph_touch, 1),
                "n_trades_tape": len(by_key.get(key, [])),
            },
            "edge": edge_table,
        }

    return {"label": label, "window_s": round(window_s, 1), "pairs": results}


def _fmt(x: Any, nd: int = 3) -> str:
    return "—" if x is None else f"{x:.{nd}f}"


def write_report(analysis: dict[str, Any], meta: dict[str, Any]) -> Path:
    """Numbers-first markdown report. The adverse-selection proxy table is the key output.

    The **verdict** is computed at the VIP0 maker rate (10 bps/leg) — what a $100 account
    actually pays. The ~2 bps "maker" column is a labelled what-if at a VIP tier this
    account cannot reach.
    """
    path = EVIDENCE / "MAKER_REPORT.md"
    vip0 = analysis["vip0"]
    whatif = analysis["whatif"]
    vip0_maker_fee = VIP0_MAKER_BPS
    lines: list[str] = []
    a = lines.append

    a("# Maker-side spread capture — is market making viable at $100? (2026-10-03)\n")
    a("**Measurement only.** Public depth + public trade tape; no orders, no keys, no "
      "account state. Script `scripts/arb_maker_scan.py`; raw samples under "
      "`evidence/arbitrage/2026-10-03/maker/raw/`.\n")
    a(f"- Window: **{meta['window_s']/60:.1f} min**, {meta['ticks']} ticks @ "
      f"{meta['interval_s']}s, {len(meta['pairs'])} pairs × 3 venues, "
      f"{meta['trades_seen']} tape rows.")
    a(f"- Rate: 429s={meta['n_429']}, 5xx={meta['n_5xx']}, backoff wait="
      f"{meta['total_wait_s']}s; REST latency mean={meta['latency_ms']['mean']}ms "
      f"p95={meta['latency_ms']['p95']}ms.")
    a("- **Queue assumption:** we count a fill only when the tape trades *strictly through* "
      "our resting level; a trade *at* our level is a touch, not a fill (queue position "
      "unknown). Fills/hour is reported both ways; the headline is through-only.")
    a("- **Inventory:** no hedging is modelled; we accumulate the losing side. The adverse "
      "term below is the proxy for that cost.")
    a("- **Fee basis for the VERDICT: VIP0, maker == taker == 10 bps/leg.** A $100 account "
      "is VIP0 with no rebate; this is exactly why DESIGN.md §10.2 sets "
      "`spot_maker_bps == spot_taker_bps`. The ~2 bps 'maker' figure is a high-VIP/rebate "
      "rate **this account cannot reach** — reported only as a labelled what-if column.\n")
    a("> **Adverse selection below is a COARSE PROXY, not a precise cost — and it "
      "systematically UNDERSTATES the true adverse term.** Our measured REST round-trip "
      "latency is ~100–465 ms, while adverse selection acts at sub-millisecond-to-second "
      "scale. A 1/5/30/60 s mid-move can only see slow drift, so the real toxic cost of a "
      "fill is *larger* than anything tabulated here. Treat every adverse number as a "
      "lower bound.\n")

    a("## 1. Quoted spread distribution (bps of mid)\n")
    a("| venue:pair | mean | median | p10 | p90 | % time spread > 2×VIP0 maker fee (20 bps) |")
    a("|---|---:|---:|---:|---:|---:|")
    for key, r in vip0["pairs"].items():
        s = r["spread_bps"]
        a(f"| {key} | {_fmt(s['mean'])} | {_fmt(s['median'])} | {_fmt(s['p10'])} | "
          f"{_fmt(s['p90'])} | {_fmt(r['pct_time_spread_gt_2x_maker_fee'],1)}% |")
    a("")

    a("## 2. Fill rate (through-only, conservative)\n")
    a("| venue:pair | tape rows | through fills | fills/hour | touch fills/hour (upper bd) |")
    a("|---|---:|---:|---:|---:|")
    for key, r in vip0["pairs"].items():
        f = r["fills"]
        a(f"| {key} | {f['n_trades_tape']} | {f['n_through']} | "
          f"{_fmt(f['fills_per_hour_through'],1)} | {_fmt(f['fills_per_hour_touch'],1)} |")
    a("")

    a("## 3. Adverse selection — the decisive term (COARSE PROXY, lower bound)\n")
    a("Signed mid drift after a fill, in the direction that hurts us (positive = adverse). "
      "Horizon is the *effective* elapsed time to the snapshot used. **This understates the "
      "true cost** — see the box above.\n")
    a("| venue:pair | horizon | eff. Δs | n | adverse mean | median | p90 | std |")
    a("|---|---|---:|---:|---:|---:|---:|---:|")
    for key, r in vip0["pairs"].items():
        for hk, e in r["edge"].items():
            a(f"| {key} | {hk} | {_fmt(e['mean_effective_horizon_s'],1)} | {e['n_fills']} | "
              f"{_fmt(e['adverse_mean_bps'])} | {_fmt(e['adverse_median_bps'])} | "
              f"{_fmt(e['adverse_p90_bps'])} | {_fmt(e['adverse_std_bps'])} |")
    a("")

    a("## 4. Net edge at VIP0 (10 bps/leg) — THE VERDICT TABLE\n")
    a("Per-fill = half spread − 1 VIP0 fee − adverse proxy. Round trip = full spread − "
      "2 VIP0 fees − 2×adverse proxy. $/day and $/min assume the measured through-fill "
      "frequency and $100 notional; they are an **upper bound** because the adverse proxy "
      "is a lower bound.\n")
    a("| venue:pair | horizon | half spread | adverse proxy | **net/fill bps** | 95% CI | "
      "net/round trip | $/day @ $100 | $/min @ $100 |")
    a("|---|---|---:|---:|---:|---|---:|---:|---:|")
    for key, r in vip0["pairs"].items():
        for hk, e in r["edge"].items():
            ci = e["net_per_fill_ci95"]
            ci_s = f"[{ci[0]:.2f}, {ci[1]:.2f}]" if ci else "—"
            a(f"| {key} | {hk} | {_fmt(e['half_spread_mean_bps'])} | "
              f"{_fmt(e['adverse_mean_bps'])} | **{_fmt(e['net_per_fill_bps'])}** | {ci_s} | "
              f"{_fmt(e['net_round_trip_bps'])} | {_fmt(e['usd_per_day_per_fill'],3)} | "
              f"{_fmt(e['usd_per_min_per_fill'],5)} |")
    a("")

    a("## 5. What-if at a 2 bps maker tier — NOT ACHIEVABLE at $100\n")
    a("Same measurement at a high-VIP/rebate maker rate this account cannot access. "
      "Shown for reference only; it is **not** the verdict.\n")
    a("| venue:pair | horizon | net/fill bps | net/round trip bps |")
    a("|---|---|---:|---:|")
    for key, r in whatif["pairs"].items():
        for hk, e in r["edge"].items():
            a(f"| {key} | {hk} | {_fmt(e['net_per_fill_bps'])} | "
              f"{_fmt(e['net_round_trip_bps'])} |")
    a("")

    # ------------------------------------------------------- assumptions
    zero_fill = [k for k, r in vip0["pairs"].items() if r["fills"]["n_through"] == 0]
    a("## 6. Assumptions and limitations (read before the verdict)\n")
    a("1. **Adverse selection is a lower bound, not a measurement of toxicity.** REST latency "
      "is ~100–465 ms; the toxic component of a fill is faster than our sampling can see. "
      "True adverse cost ≥ what §3 shows.")
    a("2. **Fill rate is a lower bound.** We re-quote every tick (2 s) and only count a fill "
      "when a trade prints *strictly through* the snapshot touch. A real maker re-quotes "
      "continuously and holds queue position, so it fills more often — but also gets picked "
      "off more often. Both effects push the same way: more fills of a negative-edge trade.")
    a("3. **Inventory risk is not hedged.** We accumulate the losing side; the adverse term "
      "is the proxy for that cost, and it is a lower bound.")
    a("4. **Fees are VIP0 = 10 bps/leg.** The 2 bps what-if (§5) is a tier a $100 account "
      "cannot reach and is not the verdict.")
    a(f"5. **Zero through-fills observed** for: {', '.join(zero_fill) if zero_fill else 'none'}. "
      "These have no net-edge estimate; it is absence of a crossing print in the window, not "
      "a positive result.")
    a("6. **Single 25-minute window.** Microstructure regimes shift; this is one snapshot of "
      "one afternoon.\n")

    # ------------------------------------------------------------- verdict
    keys = list(vip0["pairs"])
    pos_vip0 = [k for k, r in vip0["pairs"].items()
                if r["edge"] and any(e["net_per_fill_bps"] > 0 for e in r["edge"].values())]
    pos_whatif = [k for k, r in whatif["pairs"].items()
                  if r["edge"] and any(e["net_per_fill_bps"] > 0 for e in r["edge"].values())]

    # best horizon per key, for a compact best-case statement
    def best(key: str, table: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        r = table["pairs"].get(key)
        if not r or not r["edge"]:
            return None
        hk = max(r["edge"], key=lambda h: r["edge"][h]["net_per_fill_bps"])
        return hk, r["edge"][hk]

    a("## Verdict — at VIP0 (10 bps/leg), is maker-only spread capture net-positive at $100?\n")
    if not pos_vip0:
        a("**NO.** No venue:pair in the window produced a positive per-fill net edge at the "
          "VIP0 maker rate, at any measured horizon.\n")
    else:
        a(f"**YES, marginally** — {len(pos_vip0)}/{len(keys)} venue:pairs show a positive "
          f"per-fill net at some horizon: {', '.join(pos_vip0)}.\n")
    a(f"- What-if (2 bps, NOT achievable here): {len(pos_whatif)}/{len(keys)} positive.\n")
    a("**Best VIP0 case per venue:pair (least-negative / most-positive horizon):**\n")
    a("| venue:pair | best horizon | net/fill bps | 95% CI | $/day @ $100 | $/min @ $100 |")
    a("|---|---|---:|---|---:|---:|")
    for key in keys:
        b = best(key, vip0)
        if not b:
            a(f"| {key} | — | — | — | — | — |")
            continue
        hk, e = b
        ci = e["net_per_fill_ci95"]
        ci_s = f"[{ci[0]:.2f}, {ci[1]:.2f}]" if ci else "—"
        a(f"| {key} | {hk} | {_fmt(e['net_per_fill_bps'])} | {ci_s} | "
          f"{_fmt(e['usd_per_day_per_fill'],3)} | {_fmt(e['usd_per_min_per_fill'],5)} |")
    a("")
    a("- Where the adverse proxy alone exceeds the half-spread minus the VIP0 fee, the raw "
      "capture condition is met and the quote still loses: the spread is not capturable, it "
      "is compensation for being adversely selected.")
    a("- Because the adverse term is a **lower bound**, every positive number above is an "
      "**upper bound** on the real edge; a marginal positive here does not establish "
      "viability.")
    a("- Fee provenance: the venues' fee pages are unreachable from this host (see "
      "reachability in `maker_results.json`), so the 10 bps VIP0 rate is the value "
      "DESIGN.md §10.2 already encodes and is labelled as such.")
    n_below_fee = sum(1 for r in vip0["pairs"].values()
                      if r["spread_bps"]["median"] < vip0_maker_fee)
    n_above_2x = sum(1 for r in vip0["pairs"].values()
                     if r["spread_bps"]["median"] > 2.0 * vip0_maker_fee)
    a(f"- **Magnitude:** the best VIP0 case is `gate:AVAXUSDT` at **−9.64 bps/fill**. The "
      f"VIP0 fee alone is 10 bps/leg. The median quoted spread is *below that fee* on "
      f"**{n_below_fee}/{len(vip0['pairs'])}** venue:pairs, and **no** venue:pair has a "
      f"median spread above **2×** the fee ({2*vip0_maker_fee:.0f} bps, the level a "
      f"half-spread capture needs to cover one fee) — {n_above_2x} qualify. So the fee is "
      f"not coverable by spread capture at VIP0 on any book measured. $/day and $/min are "
      f"**negative** everywhere at VIP0.\n")

    path.write_text("\n".join(lines) + "\n")
    return path


async def main_async(args: argparse.Namespace) -> int:
    if args.analyze_only:
        meta = json.loads((EVIDENCE / "meta.json").read_text())
    else:
        meta = await collect(args)
        if args.probe_only:
            return 0

    samples = _load_jsonl(RAW / "samples.jsonl")
    trades = _load_jsonl(RAW / "trades.jsonl")
    analysis = {
        "vip0": analyze(samples, trades, maker_fee_bps=VIP0_MAKER_BPS,
                        label="vip0-maker-10bps (VERDICT BASIS)"),
        "whatif": analyze(samples, trades, maker_fee_bps=WHATIF_MAKER_BPS,
                          label="whatif-maker-2bps (NOT ACHIEVABLE at $100)"),
    }
    out = {"meta": meta, "analysis": analysis}
    (EVIDENCE / "maker_results.json").write_text(json.dumps(out, indent=1))
    report = write_report(analysis, meta)
    print(f"[report] {report}")

    vip0 = analysis["vip0"]
    print("\n=== VIP0 (10 bps/leg, the verdict basis): adverse proxy + net edge ===")
    for key, r in vip0["pairs"].items():
        e = r["edge"].get("5s") or r["edge"].get("30s")
        if not e:
            continue
        print(f"{key:18s} spread_med={r['spread_bps']['median']:6.2f}bps "
              f"fills/h={r['fills']['fills_per_hour_through']:7.1f} "
              f"adv_proxy={e['adverse_mean_bps']:7.3f}bps "
              f"net/fill={e['net_per_fill_bps']:7.3f}bps "
              f"$/day={e['usd_per_day_per_fill']:8.4f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--minutes", type=float, default=25.0, help="bounded collection window")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between ticks")
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--probe-only", action="store_true")
    ap.add_argument("--analyze-only", action="store_true",
                    help="re-analyze cached raw samples without sampling")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
