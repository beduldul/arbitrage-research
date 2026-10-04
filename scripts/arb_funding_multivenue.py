#!/usr/bin/env python3
"""Multi-venue perp funding-carry scanner (MEASUREMENT ONLY).

Answers, across every reachable venue's USDT perps, whether a $100 notional
cash-and-carry (long spot / short perp, collect funding) can clear its round-trip
cost, how long that takes, and how *persistent* the funding is.

**No orders. No keys. No live execution path. Writes only under ``evidence/``.**

Reachability (probed 2026-10-03, one attempt each, no retries)
-------------------------------------------------------------
From this host ``api.bybit.com``, ``www.okx.com``, ``api.bitget.com`` and
``www.deribit.com`` all refuse the TCP connection (HTTP 000) — the same geo-block
family already recorded in ``config/universe.yaml``. Two venues answer with real,
timestamped funding data:

* **Gate.io** ``api.gateio.ws/api/v4/futures/usdt`` — 1025 USDT-margined perps,
  native 8h settlement, full funding history via ``from``/``to``.
* **Hyperliquid** ``api.hyperliquid.xyz/info`` — 234 perps, hourly funding, full
  history via ``fundingHistory`` + ``startTime``.

Cost model — **not invented**
-----------------------------
Fees come from :class:`crypto_brain.engine.fees.FeeSchedule`; slippage from
:class:`crypto_brain.engine.cost_model.round_trip_cost_pct` (DESIGN.md §10.2/§10.3).
A cash-and-carry has two legs, each opened and closed, so the round-trip cost is the
spot-leg round trip **plus** the perp-leg round trip, both from the project's own
function with the config's ``slippage_k`` and ``exit_slippage_multiplier``.

The project default fee schedule is 5 bps futures taker / 2 bps futures maker
(DESIGN.md §10.2 base tier). Gate.io's published base futures taker (0.05%) matches
that. Hyperliquid's published base taker (0.035%) is *lower* than the project default,
so applying the project default to HL is deliberately conservative (it over-charges HL
and understates its carry); the fee provenance is recorded per row.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# Reuse the existing scanner's pure math and its battle-tested HTTP client shape so
# there is exactly one definition of "streak", "break-even" and "verdict".
import arb_funding_scan as afs  # noqa: E402

from crypto_brain.engine.cost_model import round_trip_cost_pct  # noqa: E402
from crypto_brain.engine.fees import FeeSchedule  # noqa: E402
from crypto_brain.engine.slippage import MIN_QUOTE_VOLUME_USD, SymbolCostProfile  # noqa: E402

GATE_BASE = "https://api.gateio.ws/api/v4/futures/usdt"
HL_INFO = "https://api.hyperliquid.xyz/info"
UA = "crypto-brain-arb-funding-multivenue/1.0 (paper research; read-only)"

PROJECT_FEES = afs.PROJECT_FEES  # 10/10 spot, 5/2 futures — DESIGN.md §10.2 base tier
SLIPPAGE_K = afs.SLIPPAGE_K
EXIT_MULTIPLIER = afs.EXIT_MULTIPLIER

#: Bounded concurrency — a sibling worker shares this IP's limits.
NETWORK_CONCURRENCY = 5
PER_DOMAIN_RATE_PER_S = 6.0
MAX_RETRIES = 6
BACKOFF_BASE_S = 1.5
TARGET_WINDOW_DAYS = 30.0

DEFAULT_OUT = REPO_ROOT / "evidence" / "arbitrage" / "2026-10-03"

#: The venue's own published base-tier futures fee, from its public docs. Marked
#: UNVERIFIED because neither venue exposes an unauthenticated fee endpoint, so this
#: cannot be confirmed by API this run. ``None`` means "unknown — project default used".
VENUE_FEE_NOTE: dict[str, dict[str, object]] = {
    "gate": {
        "published_base_taker_bps": 5.0,
        "published_base_maker_bps": 2.0,
        "verified": False,
        "source": "public docs (no unauthenticated fee endpoint)",
    },
    "hyperliquid": {
        "published_base_taker_bps": 3.5,
        "published_base_maker_bps": 1.0,
        "verified": False,
        "source": "public docs (no unauthenticated fee endpoint)",
    },
}


# ---------------------------------------------------------------------------
# HTTP: bounded concurrency + exponential backoff + raw caching
# ---------------------------------------------------------------------------


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url


class HttpClient:
    """Cached GET/POST client: global semaphore, per-host pacing, backoff on 429/418/5xx."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        concurrency: int = NETWORK_CONCURRENCY,
        rate_per_s: float = PER_DOMAIN_RATE_PER_S,
        timeout: float = 30.0,
    ) -> None:
        self.cache_dir = cache_dir
        self.timeout = timeout
        self._semaphore = threading.BoundedSemaphore(concurrency)
        self._min_gap = 1.0 / rate_per_s if rate_per_s > 0 else 0.0
        self._host_lock = threading.Lock()
        self._host_next: dict[str, float] = {}
        self.stats = {"requests": 0, "cache_hits": 0, "retries": 0, "errors": 0, "misses": 0}

    def _pace(self, host: str) -> None:
        if self._min_gap <= 0:
            return
        with self._host_lock:
            now = time.monotonic()
            slot = max(now, self._host_next.get(host, 0.0))
            self._host_next[host] = slot + self._min_gap
            wait = slot - now
        if wait > 0:
            time.sleep(wait)

    def _retry_delay(self, retry_after: str | None, attempt: int) -> float:
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return BACKOFF_BASE_S * (2**attempt)

    def request(self, url: str, *, cache_path: Path, data: bytes | None = None) -> object | None:
        """Return parsed JSON, or ``None`` for a definitive 404. Caches the raw bytes."""
        if cache_path.exists():
            self.stats["cache_hits"] += 1
            raw = cache_path.read_bytes()
            return json.loads(raw) if raw else None
        host = urllib.parse.urlparse(url).netloc
        last_status = 0
        for attempt in range(MAX_RETRIES + 1):
            with self._semaphore:
                self._pace(host)
                self.stats["requests"] += 1
                headers = {"User-Agent": UA}
                if data is not None:
                    headers["Content-Type"] = "application/json"
                request = urllib.request.Request(url, data=data, headers=headers)
                try:
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        raw = response.read()
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_bytes(raw)
                    return json.loads(raw) if raw else None
                except urllib.error.HTTPError as error:
                    last_status = error.code
                    if error.code == 404:
                        self.stats["misses"] += 1
                        cache_path.parent.mkdir(parents=True, exist_ok=True)
                        cache_path.with_suffix(cache_path.suffix + ".404").touch()
                        return None
                    retryable = error.code in (429, 418) or 500 <= error.code < 600
                    if not retryable or attempt == MAX_RETRIES:
                        self.stats["errors"] += 1
                        raise HttpError(error.code, url) from error
                    delay = self._retry_delay(error.headers.get("Retry-After"), attempt)
                except Exception:  # noqa: BLE001 — refused/reset; retry then give up
                    if attempt == MAX_RETRIES:
                        self.stats["errors"] += 1
                        raise
                    delay = self._retry_delay(None, attempt)
            self.stats["retries"] += 1
            time.sleep(delay)
        raise HttpError(last_status, url)


# ---------------------------------------------------------------------------
# Pure computation — the part the unit tests pin
# ---------------------------------------------------------------------------


def normalize_to_8h(rate_fraction: float, interval_hours: float) -> float:
    """Convert a per-interval funding rate (fraction) to an 8h-equivalent fraction.

    A 1h-interval venue settles 8x as often as an 8h one, so its per-interval rate must
    be scaled up by 8 to be comparable. This is a *rate normalisation only* — the
    annualised and dollar figures use the true interval, never this.
    """
    if interval_hours <= 0:
        raise ValueError("interval_hours must be positive")
    return rate_fraction * (8.0 / interval_hours)


def streak_intervals_for_days(days: float, interval_hours: float) -> int:
    """How many settlements of ``interval_hours`` make up ``days`` (the persistence bar).

    The 7-day "konsisten" bar is 21 intervals on an 8h venue but 168 on an hourly one;
    hard-coding 21 would silently make hourly venues pass a 21-hour test.
    """
    if interval_hours <= 0:
        raise ValueError("interval_hours must be positive")
    return max(1, round(days * 24.0 / interval_hours))


def dollars_per_day(notional: float, mean_rate_fraction: float, interval_hours: float) -> float:
    """Funding received per day by a short-perp leg at a constant mean rate."""
    if interval_hours <= 0:
        raise ValueError("interval_hours must be positive")
    return notional * mean_rate_fraction * (24.0 / interval_hours)


def basis_bps(price_a: float | None, price_b: float | None) -> float | None:
    """``(a - b) / b`` in bps. ``None`` if either price is missing or b is zero."""
    if price_a is None or price_b is None or price_b == 0:
        return None
    return (price_a - price_b) / price_b * 10_000.0


@dataclass(frozen=True)
class SymbolResult:
    """One row of ``multivenue_ranking.json`` — everything a simulator would need."""

    venue: str
    symbol: str
    n_funding: int
    interval_hours: float
    window_days: float
    window_start: str | None
    window_end: str | None
    # per-interval, normalised to an 8h interval for cross-venue comparability
    mean_bps_8h: float
    median_bps_8h: float
    min_bps_8h: float
    max_bps_8h: float
    # native per-interval bps, as actually charged
    mean_bps_native: float
    annualized_funding_pct: float
    positive_share: float
    sign_flip_rate: float
    longest_positive_streak: int
    longest_positive_streak_days: float
    current_basis_bps: float | None
    current_premium_bps: float | None
    volume_24h_quote_usd: float
    below_volume_floor: bool
    mark_price: float | None
    index_price: float | None
    # cost model
    fee_tier: str
    venue_fee_verified: bool
    spot_round_trip_pct: float
    perp_round_trip_pct: float
    round_trip_cost_pct: float
    break_even_days: float | None
    dollars_per_day_gross: float
    dollars_per_day_net: float
    dollars_per_year_net: float
    verdict: str
    reason: str


def compute_symbol(
    *,
    venue: str,
    symbol: str,
    rates: list[float],
    times_ms: list[int],
    interval_hours: float,
    volume_24h_quote_usd: float,
    notional: float,
    mark_price: float | None = None,
    index_price: float | None = None,
    premium_bps: float | None = None,
    fee_tier: str = "taker",
    venue_fee_verified: bool = False,
    min_positive_share: float = 0.60,
    min_streak_days: float = 7.0,
    max_break_even_days: float = 30.0,
    min_volume_usd: float = MIN_QUOTE_VOLUME_USD,
) -> SymbolResult:
    """Pure per-symbol computation: funding series + prices + volume → a full row."""
    if not rates:
        raise ValueError(f"{symbol}: funding series is empty")

    stats = afs.funding_stats(rates, interval_hours=interval_hours)
    per8 = 8.0 / interval_hours
    bps_8h = [r * 10_000.0 * per8 for r in rates]

    profile = SymbolCostProfile.from_volume(symbol, volume_24h_quote_usd)
    spot_leg = round_trip_cost_pct(
        profile,
        notional,
        mode="spot",
        fees=PROJECT_FEES,
        slippage_k=SLIPPAGE_K,
        exit_multiplier=EXIT_MULTIPLIER,
    )
    perp_leg = round_trip_cost_pct(
        profile,
        notional,
        mode="futures",
        fees=PROJECT_FEES,
        slippage_k=SLIPPAGE_K,
        exit_multiplier=EXIT_MULTIPLIER,
        include_funding=False,
    )
    total_cost_pct = spot_leg.total_pct + perp_leg.total_pct

    daily_funding_pct = (stats.mean_bps / 100.0) * (24.0 / interval_hours)
    be_days = afs.break_even_days(total_cost_pct, daily_funding_pct)
    per_day = dollars_per_day(notional, statistics.fmean(rates), interval_hours)

    verdict, reason = afs.classify(
        mean_funding_bps=stats.mean_bps,
        positive_share=stats.positive_share,
        longest_streak=stats.longest_positive_streak,
        break_even_days_value=be_days,
        min_positive_share=min_positive_share,
        min_streak_intervals=streak_intervals_for_days(min_streak_days, interval_hours),
        max_break_even_days=max_break_even_days,
    )
    # DESIGN.md §10.3 / §5.3 item 4: a symbol below the universe floor is *excluded
    # structurally, not priced*. A 4h-interval meme perp with a 40 bps mean prints a
    # spectacular annualised number and no tradeable spot leg; the floor vetoes it
    # rather than letting it top the table.
    below_floor = volume_24h_quote_usd < min_volume_usd
    if below_floor:
        verdict = "no"
        reason = (
            f"24h quote volume ${volume_24h_quote_usd:,.0f} is below the "
            f"${min_volume_usd:,.0f} universe floor (DESIGN.md §10.3) — excluded "
            "structurally, not priced"
        )

    window_start = datetime.fromtimestamp(min(times_ms) / 1000, tz=UTC).isoformat() if times_ms else None
    window_end = datetime.fromtimestamp(max(times_ms) / 1000, tz=UTC).isoformat() if times_ms else None
    window_days = (max(times_ms) - min(times_ms)) / 86_400_000.0 if len(times_ms) > 1 else 0.0

    return SymbolResult(
        venue=venue,
        symbol=symbol,
        n_funding=stats.n,
        interval_hours=interval_hours,
        window_days=round(window_days, 2),
        window_start=window_start,
        window_end=window_end,
        mean_bps_8h=round(statistics.fmean(bps_8h), 4),
        median_bps_8h=round(statistics.median(bps_8h), 4),
        min_bps_8h=round(min(bps_8h), 4),
        max_bps_8h=round(max(bps_8h), 4),
        mean_bps_native=round(stats.mean_bps, 4),
        annualized_funding_pct=round(stats.annualized_pct, 3),
        positive_share=round(stats.positive_share, 4),
        sign_flip_rate=round(stats.sign_flip_rate, 4),
        longest_positive_streak=stats.longest_positive_streak,
        longest_positive_streak_days=round(stats.longest_positive_streak_days, 2),
        current_basis_bps=None if (b := basis_bps(mark_price, index_price)) is None else round(b, 3),
        current_premium_bps=premium_bps,
        volume_24h_quote_usd=volume_24h_quote_usd,
        below_volume_floor=below_floor,
        mark_price=mark_price,
        index_price=index_price,
        fee_tier=fee_tier,
        venue_fee_verified=venue_fee_verified,
        spot_round_trip_pct=round(spot_leg.total_pct, 4),
        perp_round_trip_pct=round(perp_leg.total_pct, 4),
        round_trip_cost_pct=round(total_cost_pct, 4),
        break_even_days=None if be_days is None else round(be_days, 2),
        dollars_per_day_gross=round(per_day, 5),
        dollars_per_day_net=0.0 if be_days is None else round(per_day, 5),
        dollars_per_year_net=0.0 if be_days is None else round(per_day * 365.0, 2),
        verdict=verdict,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Venue adapters
# ---------------------------------------------------------------------------


def gate_enumerate(client: HttpClient) -> list[dict[str, object]]:
    """Every live USDT-margined crypto perp on Gate.io with its 24h quote volume."""
    contracts = client.request(
        f"{GATE_BASE}/contracts", cache_path=client.cache_dir / "gate" / "contracts.json"
    )
    tickers = client.request(
        f"{GATE_BASE}/tickers", cache_path=client.cache_dir / "gate" / "tickers.json"
    )
    assert isinstance(contracts, list) and isinstance(tickers, list)
    vol = {t["contract"]: t for t in tickers}
    out: list[dict[str, object]] = []
    for c in contracts:
        # contract_type == "" is a crypto perp; the rest are tokenised stocks/indices/
        # metals/forex/commodities, which have no spot leg to hedge against.
        if c.get("contract_type") not in ("", None):
            continue
        if c.get("in_delisting") or c.get("is_pre_market"):
            continue
        name = c["name"]
        if not name.endswith("_USDT"):
            continue
        t = vol.get(name, {})
        out.append(
            {
                "symbol": name,
                "interval_hours": float(c.get("funding_interval", 28800)) / 3600.0,
                "volume_24h_quote_usd": float(t.get("volume_24h_quote") or 0.0),
                "mark_price": float(c["mark_price"]) if c.get("mark_price") else None,
                "index_price": float(c["index_price"]) if c.get("index_price") else None,
                "premium_bps": None,
            }
        )
    return out


def gate_funding_history(client: HttpClient, symbol: str, days: float) -> tuple[list[float], list[int]]:
    now = int(time.time())
    url = (
        f"{GATE_BASE}/funding_rate?contract={urllib.parse.quote(symbol)}"
        f"&from={now - int(days * 86400)}&to={now}&limit=1000"
    )
    rows = client.request(url, cache_path=client.cache_dir / "gate" / "funding" / f"{symbol}.json")
    if not isinstance(rows, list):
        return [], []
    rows = sorted(rows, key=lambda r: r["t"])
    return [float(r["r"]) for r in rows], [int(r["t"]) * 1000 for r in rows]


def hl_enumerate(client: HttpClient) -> list[dict[str, object]]:
    """Every live Hyperliquid perp with its 24h notional volume and current premium."""
    body = json.dumps({"type": "metaAndAssetCtxs"}).encode()
    data = client.request(HL_INFO, cache_path=client.cache_dir / "hyperliquid" / "meta.json", data=body)
    assert isinstance(data, list) and len(data) == 2
    universe, ctxs = data[0]["universe"], data[1]
    out: list[dict[str, object]] = []
    for meta, ctx in zip(universe, ctxs, strict=False):
        if meta.get("isDelisted"):
            continue
        premium = float(ctx.get("premium") or 0.0)
        out.append(
            {
                "symbol": meta["name"],
                "interval_hours": 1.0,  # Hyperliquid settles hourly
                "volume_24h_quote_usd": float(ctx.get("dayNtlVlm") or 0.0),
                "mark_price": float(ctx["markPx"]) if ctx.get("markPx") else None,
                "index_price": float(ctx["oraclePx"]) if ctx.get("oraclePx") else None,
                "premium_bps": round(premium * 10_000.0, 3),
            }
        )
    return out


def hl_funding_history(client: HttpClient, symbol: str, days: float) -> tuple[list[float], list[int]]:
    start = int((time.time() - days * 86400) * 1000)
    rates: list[float] = []
    times: list[int] = []
    page = 0
    while True:
        body = json.dumps({"type": "fundingHistory", "coin": symbol, "startTime": start}).encode()
        rows = client.request(
            HL_INFO,
            cache_path=client.cache_dir / "hyperliquid" / "funding" / f"{symbol}_{page}.json",
            data=body,
        )
        if not isinstance(rows, list) or not rows:
            break
        rows.sort(key=lambda r: r["time"])
        rates.extend(float(r["fundingRate"]) for r in rows)
        times.extend(int(r["time"]) for r in rows)
        if len(rows) < 500:  # a short page means we have reached the present
            break
        start = int(rows[-1]["time"]) + 1
        page += 1
        if page > 6:  # safety: 7*500 hourly rows ≈ 145 days, far past the target
            break
    return rates, times


VENUES: dict[str, dict[str, object]] = {
    "gate": {"enumerate": gate_enumerate, "history": gate_funding_history},
    "hyperliquid": {"enumerate": hl_enumerate, "history": hl_funding_history},
}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def scan_venue(
    client: HttpClient, venue: str, *, days: float, notional: float, max_symbols: int | None
) -> list[SymbolResult]:
    adapter = VENUES[venue]
    symbols = adapter["enumerate"](client)  # type: ignore[operator]
    symbols.sort(key=lambda s: s["volume_24h_quote_usd"], reverse=True)  # type: ignore[index]
    if max_symbols:
        symbols = symbols[:max_symbols]
    results: list[SymbolResult] = []
    lock = threading.Lock()

    def work(meta: dict[str, object]) -> None:
        symbol = str(meta["symbol"])
        try:
            rates, times = adapter["history"](client, symbol, days)  # type: ignore[operator]
            if not rates:
                return
            row = compute_symbol(
                venue=venue,
                symbol=symbol,
                rates=rates,
                times_ms=times,
                interval_hours=float(meta["interval_hours"]),  # type: ignore[arg-type]
                volume_24h_quote_usd=float(meta["volume_24h_quote_usd"]),  # type: ignore[arg-type]
                notional=notional,
                mark_price=meta.get("mark_price"),  # type: ignore[arg-type]
                index_price=meta.get("index_price"),  # type: ignore[arg-type]
                premium_bps=meta.get("premium_bps"),  # type: ignore[arg-type]
                fee_tier="taker",
                venue_fee_verified=bool(VENUE_FEE_NOTE[venue]["verified"]),
            )
        except Exception as error:  # noqa: BLE001 — one symbol must not sink the scan
            print(f"  ! {venue}/{symbol}: {type(error).__name__}: {error}", file=sys.stderr)
            return
        with lock:
            results.append(row)

    with ThreadPoolExecutor(max_workers=NETWORK_CONCURRENCY) as pool:
        list(pool.map(work, symbols))
    print(f"[{venue}] enumerated {len(symbols)} symbols, measured {len(results)}", file=sys.stderr)
    return results


def render_table(results: list[SymbolResult], limit: int = 15) -> str:
    header = (
        "| # | Venue | Symbol | n | int | mean bps/8h | ann % | +share | streak d "
        "| basis bps | 24h vol $ | cost % | b/e d | $/day @$100 | verdict |"
    )
    sep = "|" + "---|" * 15
    lines = [header, sep]
    for i, r in enumerate(results[:limit], 1):
        lines.append(
            f"| {i} | {r.venue} | {r.symbol} | {r.n_funding} | {r.interval_hours:g}h "
            f"| {r.mean_bps_8h:.3f} | {r.annualized_funding_pct:.2f} "
            f"| {r.positive_share:.0%} | {r.longest_positive_streak_days:.1f} "
            f"| {'' if r.current_basis_bps is None else f'{r.current_basis_bps:.1f}'} "
            f"| {r.volume_24h_quote_usd/1e6:,.0f}M | {r.round_trip_cost_pct:.3f} "
            f"| {'' if r.break_even_days is None else f'{r.break_even_days:.1f}'} "
            f"| {r.dollars_per_day_net:.4f} | {r.verdict} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venues", default="gate,hyperliquid")
    parser.add_argument("--days", type=float, default=TARGET_WINDOW_DAYS)
    parser.add_argument("--notional", type=float, default=100.0)
    parser.add_argument("--max-symbols", type=int, default=None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    cache_dir = args.out / "venues"
    cache_dir.mkdir(parents=True, exist_ok=True)
    client = HttpClient(cache_dir)

    results: list[SymbolResult] = []
    for venue in [v.strip() for v in args.venues.split(",") if v.strip()]:
        results.extend(
            scan_venue(client, venue, days=args.days, notional=args.notional, max_symbols=args.max_symbols)
        )

    # Rank: candidates first, then by net dollars/day, then by annualised funding.
    results.sort(
        key=lambda r: (r.verdict == "candidate", r.dollars_per_day_net, r.annualized_funding_pct),
        reverse=True,
    )

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "notional_usd": args.notional,
        "window_days_target": args.days,
        "venue_fee_notes": VENUE_FEE_NOTE,
        "cost_model": {
            "source": "crypto_brain.engine.fees.FeeSchedule + engine.cost_model.round_trip_cost_pct",
            "futures_taker_bps": PROJECT_FEES.futures_taker_bps,
            "futures_maker_bps": PROJECT_FEES.futures_maker_bps,
            "slippage_k": SLIPPAGE_K,
            "exit_multiplier": EXIT_MULTIPLIER,
            "note": "cost model from project defaults, venue fee unverified",
        },
        "http_stats": client.stats,
        "n_measured": len(results),
        "n_candidates": sum(1 for r in results if r.verdict == "candidate"),
        "by_venue": {
            v: sum(1 for r in results if r.venue == v)
            for v in sorted({r.venue for r in results})
        },
        "symbols": [asdict(r) for r in results],
    }
    out_json = args.out / "multivenue_ranking.json"
    out_json.write_text(json.dumps(payload, indent=2))

    print(render_table(results))
    print(
        f"\nmeasured {len(results)} symbols across "
        f"{payload['by_venue']} — {payload['n_candidates']} candidates "
        f"(notional ${args.notional:.0f}, window >= {args.days:.0f}d)"
    )
    print(f"http: {client.stats}")
    print(f"wrote {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
