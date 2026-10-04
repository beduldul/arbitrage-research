#!/usr/bin/env python3
"""WARP-enabled arbitrage measurement (MEASUREMENT ONLY — no keys, no orders).

Cloudflare WARP (egress 104.28.250.136, colo SIN) unblocked every venue the earlier
programme could not reach: Coinbase, Kraken, Deribit, Crypto.com, Coinbase INTX,
Kraken Futures — and, most importantly, **Binance USDⓈ-M futures** (``fapi.binance.com``),
which the earlier study could only approach through monthly S3 dumps. This module
measures four classes on real, live, public data:

1. ``tri``      — triangular arbitrage on each newly-reachable venue's own books, $100,
                  buy-at-ask/sell-at-bid, that venue's own published taker tier.
2. ``funding``  — cross-venue perp funding spread, extending
                  ``arb_funding_spread.py`` with Binance/Deribit/Kraken-Futures/INTX/CDC.
3. ``binance``  — live Binance USDⓈ-M funding + spot-perp basis over the FULL perp
                  universe (the gap the S3-dump study could not close).
4. ``basis``    — hedged spot-perp cash-and-carry on venues that have both legs, with
                  basis drift included as a term (the earlier study showed it is material).

Everything writes under ``evidence/arbitrage/2026-10-03/warp/``. Fees come from
:class:`crypto_brain.engine.fees.FeeSchedule` (project §10.2 base tier) or from a venue's
own published tier, always marked verified/unverified. No function here can place an
order: there is no account, key, or write path beyond ``evidence/``.

Run::

    uv run python scripts/arb_warp_scan.py binance
    uv run python scripts/arb_warp_scan.py funding
    uv run python scripts/arb_warp_scan.py tri --minutes 25 --interval 7
    uv run python scripts/arb_warp_scan.py basis
"""

from __future__ import annotations

import argparse
import json
import statistics as st
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

import arb_common as ac  # noqa: E402
import arb_funding_decisive as dc  # noqa: E402
import arb_funding_spread as sp  # noqa: E402

from crypto_brain.engine.fees import FeeSchedule  # noqa: E402

FEES = FeeSchedule()
OUT = REPO / "evidence" / "arbitrage" / "2026-10-03" / "warp"
UA = "crypto-brain-arb-warp-scan/1.0 (paper research; read-only)"
BPS = 10_000.0

#: Per-venue spot taker fee, published base/VIP0 tier. ``verified`` only when the venue
#: itself returned the number in an unauthenticated payload (Deribit does).
VENUE_SPOT_FEE: dict[str, dict[str, Any]] = {
    "coinbase": {"taker_bps": 60.0, "verified": False, "tier": "Exchange base <$10k 30d"},
    "kraken": {"taker_bps": 26.0, "verified": False, "tier": "spot base $0 tier"},
    "deribit": {"taker_bps": 5.0, "verified": True, "tier": "spot taker (instrument payload)"},
    "cryptocom": {"taker_bps": 7.5, "verified": False, "tier": "Exchange base tier"},
    "bybit": {"taker_bps": 10.0, "verified": False, "tier": "spot base VIP0"},
    "okx": {"taker_bps": 10.0, "verified": False, "tier": "spot base Lv1"},
    "binance": {"taker_bps": 10.0, "verified": False, "tier": "spot VIP0 (project §10.2)"},
}

#: Perp taker fee per venue (published base tier), for the funding/basis legs.
VENUE_PERP_FEE: dict[str, dict[str, Any]] = {
    "binance": {"taker_bps": 5.0, "maker_bps": 2.0, "verified": False},
    "deribit": {"taker_bps": 5.0, "maker_bps": 2.0, "verified": False},
    "kraken_futures": {"taker_bps": 5.0, "maker_bps": 2.0, "verified": False},
    "cryptocom": {"taker_bps": 7.5, "maker_bps": 2.5, "verified": False},
    "coinbase_intx": {"taker_bps": 5.0, "maker_bps": 2.0, "verified": False},
}

ANCHOR_QUOTES = ("USD", "USDT", "USDC")

# ===========================================================================
# Pure computation — the part the unit tests pin
# ===========================================================================


def summarize_series(series: list[float | None], interval_s: float) -> dict[str, Any]:
    """Max/mean/p90 net-bps, %positive, longest positive run, max executable size.

    ``series`` may contain ``None`` (unfillable at $100); those are *not* executable and
    are excluded from the bps statistics but counted in the denominator of
    ``pct_samples_positive`` — a triangle that fills in 1 of 100 samples is not an edge.
    """
    executable = [v for v in series if v is not None]
    positive = [v for v in executable if v > 0]
    best = max(executable) if executable else None
    return {
        "samples": len(series),
        "executable_samples": len(executable),
        "max_net_bps": round(best, 3) if best is not None else None,
        "mean_net_bps": round(st.fmean(executable), 3) if executable else None,
        "median_net_bps": round(st.median(executable), 3) if executable else None,
        "p90_net_bps": round(ac.percentile(executable, 90), 3) if executable else None,
        "min_net_bps": round(min(executable), 3) if executable else None,
        "pct_samples_positive": round(100.0 * len(positive) / len(series), 3) if series else None,
        "longest_positive_run_samples": ac.longest_positive_run(series),
        "longest_positive_run_s": round(ac.longest_positive_run(series) * interval_s, 1),
    }


def find_triangles(
    pairs: dict[tuple[str, str], dict[str, Any]],
    *,
    min_volume_usd: float,
    top_bases: int = 25,
    anchors: tuple[str, ...] = ANCHOR_QUOTES,
) -> list[dict[str, Any]]:
    """Enumerate anchor->A->B->anchor cycles from a ``(base, quote) -> info`` map.

    Pure: no network. Both cross directions are emitted because they are different trades
    (buy A/B vs sell A/B). Bases are pre-filtered by 24h quote volume so a venue's thin
    tail cannot dominate the sweep.
    """
    out: list[dict[str, Any]] = []
    for anchor in anchors:
        bases = sorted(
            (
                (b, info)
                for (b, q), info in pairs.items()
                if q == anchor and float(info.get("volume_24h_quote_usd", 0.0)) >= min_volume_usd
            ),
            key=lambda kv: -float(kv[1].get("volume_24h_quote_usd", 0.0)),
        )[:top_bases]
        names = [b for b, _ in bases]
        # Each *unordered* base pair is visited once; the two cross-book directions
        # (buy B/A vs sell A/B) are distinct trades and both are emitted.
        for a, b in combinations(names, 2):
            if (b, a) in pairs:  # buy the B/A cross: anchor -> a -> b -> anchor
                out.append(
                    {
                        "path": (anchor, a, b, anchor),
                        "legs": [((a, anchor), "buy"), ((b, a), "buy"), ((b, anchor), "sell")],
                        "min_leg_volume_usd": round(
                            min(
                                float(pairs[(a, anchor)]["volume_24h_quote_usd"]),
                                float(pairs[(b, a)]["volume_24h_quote_usd"]),
                                float(pairs[(b, anchor)]["volume_24h_quote_usd"]),
                            ),
                            0,
                        ),
                    }
                )
            if (a, b) in pairs:  # sell the A/B cross: anchor -> a -> b -> anchor
                out.append(
                    {
                        "path": (anchor, a, b, anchor),
                        "legs": [((a, anchor), "buy"), ((a, b), "sell"), ((b, anchor), "sell")],
                        "min_leg_volume_usd": round(
                            min(
                                float(pairs[(a, anchor)]["volume_24h_quote_usd"]),
                                float(pairs[(a, b)]["volume_24h_quote_usd"]),
                                float(pairs[(b, anchor)]["volume_24h_quote_usd"]),
                            ),
                            0,
                        ),
                    }
                )
    return out


def triangle_net_bps(
    books: list[tuple[ac.Book, str]], start_quote: float, taker_bps: float
) -> float | None:
    """Walk a 3-leg cycle at ``start_quote`` and return net bps after a flat per-leg fee.

    Thin wrapper over :func:`arb_common.triangle_net_bps` so the fee is a single per-leg
    rate (the venue's own taker tier) rather than the project's two-mode schedule.
    """
    fee = ac.FeeTier(venue="warp", taker_bps=taker_bps, maker_bps=taker_bps, verified=False)
    return ac.triangle_net_bps(books, start_quote, fee)


def funding_per_day(
    leg_usd: float, rates: list[float], times_ms: list[int], *, interval_hours: float | None = None
) -> float:
    """USD of funding collected per day by a constant ``leg_usd`` position.

    When ``interval_hours`` is given (the venue's published settlement interval) income is
    ``leg_usd * mean(rate) * 24/interval``. When it is omitted the interval is inferred
    from the median spacing of ``times_ms``, so an 8h venue and an hourly venue both come
    out as income/day. Raises on an empty series or a non-positive window — a missing
    history is not a zero return.
    """
    if not rates or not times_ms:
        raise ValueError("funding series is empty")
    if len(rates) != len(times_ms):
        raise ValueError("rates and times must be the same length")
    if interval_hours is not None:
        if interval_hours <= 0:
            raise ValueError("interval_hours must be positive")
        return leg_usd * st.fmean(rates) * (24.0 / interval_hours)
    ordered = sorted(times_ms)
    gaps = [b - a for a, b in zip(ordered, ordered[1:], strict=False) if b > a]
    if not gaps:
        raise ValueError("cannot infer funding interval from a single settlement")
    interval_hours = st.median(gaps) / 3_600_000.0
    if interval_hours <= 0:
        raise ValueError("inferred funding interval is not positive")
    return leg_usd * st.fmean(rates) * (24.0 / interval_hours)


def basis_carry_net_usd(
    days: list[int],
    spot_close: dict[int, float],
    perp_close: dict[int, float],
    funding: dict[int, float],
    *,
    leg_usd: float,
    spot_fee: float,
    perp_fee: float,
) -> list[float]:
    """Per-day net USD of a hedged long-spot/short-perp carry (reuses the decisive study).

    Kept as a named wrapper so the basis term is explicit: the earlier programme found
    basis drift material, and this function never drops it.
    """
    return dc.hedged_daily_net_dollars(
        days, spot_close, perp_close, funding,
        leg_notional=leg_usd, spot_fee=spot_fee, perp_fee=perp_fee,
    )


def premium_bps(mark: float | None, index: float | None) -> float | None:
    """Perp premium over the spot index in bps: ``(mark - index)/index``."""
    if mark is None or index is None or index == 0:
        return None
    return (mark - index) / index * BPS


def min_notional_ok(min_usd: float | None, leg_usd: float) -> bool:
    """Does a ``leg_usd`` order clear the venue's minimum notional (absent => yes)."""
    return sp.min_leg_ok(min_usd or 0.0, leg_usd)


# ===========================================================================
# HTTP
# ===========================================================================


class Net:
    """Cached GET client with bounded concurrency and per-host pacing."""

    def __init__(self, cache_dir: Path, *, concurrency: int = 4, rate_per_s: float = 5.0,
                 timeout: float = 30.0) -> None:
        self.cache_dir = cache_dir
        self.timeout = timeout
        self._sem = threading.BoundedSemaphore(concurrency)
        self._gap = 1.0 / rate_per_s if rate_per_s > 0 else 0.0
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}
        #: When False, GETs bypass the read/write cache entirely — used for the live
        #: polling phase, where replaying a cached book would fake a constant series.
        self.cache_enabled = True
        self.stats = {"requests": 0, "cache_hits": 0, "errors": 0}

    def _pace(self, host: str) -> None:
        if self._gap <= 0:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next.get(host, 0.0))
            self._next[host] = slot + self._gap
            wait = slot - now
        if wait > 0:
            time.sleep(wait)

    def get(self, url: str, rel: str, *, data: bytes | None = None, retries: int = 4) -> Any:
        path = self.cache_dir / rel
        if self.cache_enabled and path.exists():
            self.stats["cache_hits"] += 1
            raw = path.read_bytes()
            return json.loads(raw) if raw else None
        host = urllib.parse.urlparse(url).netloc
        for attempt in range(retries + 1):
            with self._sem:
                self._pace(host)
                self.stats["requests"] += 1
                headers = {"User-Agent": UA, "Accept": "application/json"}
                if data is not None:
                    headers["Content-Type"] = "application/json"
                req = urllib.request.Request(url, data=data, headers=headers)
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        raw = resp.read()
                    if self.cache_enabled:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(raw)
                    return json.loads(raw) if raw else None
                except urllib.error.HTTPError as err:
                    if err.code == 404:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.with_suffix(path.suffix + ".404").touch()
                        return None
                    if err.code not in (429, 418) and not 500 <= err.code < 600:
                        self.stats["errors"] += 1
                        return None
                    delay = 1.5 * (2**attempt)
                except Exception:  # noqa: BLE001 — refused/reset: retry then give up
                    if attempt == retries:
                        self.stats["errors"] += 1
                        return None
                    delay = 1.5 * (2**attempt)
            time.sleep(delay)
        return None

    def request(self, url: str, *, cache_path: Path, data: bytes | None = None) -> Any:
        """``mv.HttpClient``-compatible entry point so existing venue adapters run on Net.

        ``cache_path`` is mapped to a path relative to ``cache_dir`` when possible, so the
        on-disk layout matches the existing scanner's raw cache.
        """
        try:
            rel = str(cache_path.relative_to(self.cache_dir))
        except ValueError:
            rel = cache_path.name
        return self.get(url, rel, data=data)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _book_from_levels(bids: Any, asks: Any) -> ac.Book:
    return ac.Book(bids=ac._levels(bids), asks=ac._levels(asks))


# ===========================================================================
# Triangular adapters — enumerate pairs + fetch books, per venue
# ===========================================================================


def _cb_pairs(net: Net) -> dict[tuple[str, str], dict[str, Any]]:
    # Advanced Trade bulk products carry price + 24h base volume in one call, so quote
    # volume = volume_24h * price; the Exchange volume-summary only reports base units.
    d = net.get("https://api.coinbase.com/api/v3/brokerage/market/products?limit=1000",
                "coinbase/at_products.json")
    rows = (d or {}).get("products", []) if isinstance(d, dict) else []
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for p in rows:
        if p.get("status") != "online" or p.get("trading_disabled"):
            continue
        pid = p.get("product_id", "")
        if "-" not in pid:
            continue
        base, quote = pid.split("-", 1)
        price = _f(p.get("price"))
        out[(base, quote)] = {
            "symbol": pid,
            "volume_24h_quote_usd": _f(p.get("volume_24h")) * price,
            "min_notional_usd": _f(p.get("quote_min_size"), 1.0),
        }
    return out


def _cb_book(net: Net, symbol: str) -> ac.Book | None:
    d = net.get(f"https://api.exchange.coinbase.com/products/{symbol}/book?level=2",
                f"coinbase/book/{symbol}.json")
    if not isinstance(d, dict):
        return None
    b = _book_from_levels(d.get("bids"), d.get("asks"))
    return None if b.empty else b


def _kraken_pairs(net: Net) -> dict[tuple[str, str], dict[str, Any]]:
    ap = net.get("https://api.kraken.com/0/public/AssetPairs", "kraken/assetpairs.json")
    tk = net.get("https://api.kraken.com/0/public/Ticker", "kraken/ticker.json")
    if not isinstance(ap, dict):
        return {}
    res = ap.get("result", {})
    tkr = (tk or {}).get("result", {})
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for name, info in res.items():
        ws = info.get("wsname")
        base = str(ws.split("/")[0]) if ws else info.get("base")
        quote = str(ws.split("/")[1]) if ws else info.get("quote")
        # Normalise Kraken's X/Z legacy prefixes (XXBT->BTC, ZUSD->USD, XBT->BTC).
        base = {"XBT": "BTC", "XXBT": "BTC", "XDG": "DOGE"}.get(base, base.lstrip("XZ") or base)
        quote = {"ZUSD": "USD", "USDT": "USDT", "ZUSDT": "USDT", "USDC": "USDC"}.get(
            quote, quote.lstrip("Z") or quote
        )
        t = tkr.get(name, {})
        vol = _f((t.get("v") or [0, 0])[1]) * _f((t.get("c") or [0])[0]) if t else 0.0
        out[(base, quote)] = {"symbol": name, "volume_24h_quote_usd": vol,
                              "min_notional_usd": 0.0}
    return out


def _kraken_book(net: Net, symbol: str) -> ac.Book | None:
    d = net.get(f"https://api.kraken.com/0/public/Depth?pair={symbol}&count=100",
                f"kraken/book/{symbol}.json")
    if not isinstance(d, dict):
        return None
    res = d.get("result", {})
    if not res:
        return None
    first = next(iter(res.values()))
    b = _book_from_levels(first.get("bids"), first.get("asks"))
    return None if b.empty else b


def _deribit_pairs(net: Net) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for ccy in ("BTC", "ETH", "USDC", "USDT", "SOL", "XRP", "AVAX", "LINK", "DOGE"):
        d = net.get(f"https://www.deribit.com/api/v2/public/get_instruments?currency={ccy}&kind=spot",
                    f"deribit/spot_{ccy}.json")
        # The book summary carries per-instrument volume in BASE units; quote = vol * mark.
        summ = net.get(f"https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
                       f"?currency={ccy}&kind=spot", f"deribit/spotsum_{ccy}.json")
        volmap = {r["instrument_name"]: (_f(r.get("volume")), _f(r.get("mark_price")))
                  for r in (summ or {}).get("result", [])}
        for inst in (d or {}).get("result", []):
            name = inst["instrument_name"]
            if "_" not in name or not inst.get("is_active"):
                continue
            base, quote = name.split("_", 1)
            vol, mark = volmap.get(name, (0.0, 0.0))
            out[(base, quote)] = {
                "symbol": name,
                "volume_24h_quote_usd": vol * mark,
                "min_notional_usd": 0.0,
            }
    return out


def _deribit_book(net: Net, symbol: str) -> ac.Book | None:
    d = net.get(f"https://www.deribit.com/api/v2/public/get_order_book?instrument_name={symbol}"
                "&depth=100", f"deribit/book/{symbol}.json")
    res = (d or {}).get("result") if isinstance(d, dict) else None
    if not isinstance(res, dict):
        return None
    b = _book_from_levels(res.get("bids"), res.get("asks"))
    return None if b.empty else b


def _cdc_pairs(net: Net) -> dict[tuple[str, str], dict[str, Any]]:
    d = net.get("https://api.crypto.com/exchange/v1/public/get-instruments",
                "cryptocom/instruments.json")
    tk = net.get("https://api.crypto.com/exchange/v1/public/get-tickers",
                 "cryptocom/tickers.json")
    rows = ((d or {}).get("result", {}) or {}).get("data", []) if isinstance(d, dict) else []
    vols = {t["i"]: _f(t.get("vv")) for t in ((tk or {}).get("result", {}) or {}).get("data", [])}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for inst in rows:
        if inst.get("inst_type") != "CCY_PAIR" or not inst.get("tradable"):
            continue
        sym = inst["symbol"]
        out[(inst["base_ccy"], inst["quote_ccy"])] = {
            "symbol": sym,
            "volume_24h_quote_usd": vols.get(sym, 0.0),
            "min_notional_usd": _f(inst.get("qty_tick_size")),
        }
    return out


def _cdc_book(net: Net, symbol: str) -> ac.Book | None:
    d = net.get(f"https://api.crypto.com/exchange/v1/public/get-book?instrument_name={symbol}"
                "&depth=50", f"cryptocom/book/{symbol}.json")
    data = ((d or {}).get("result", {}) or {}).get("data", []) if isinstance(d, dict) else []
    if not data:
        return None
    row = data[0]
    b = _book_from_levels(row.get("bids"), row.get("asks"))
    return None if b.empty else b


def _bybit_spot_pairs(net: Net) -> dict[tuple[str, str], dict[str, Any]]:
    inst = net.get("https://api.bybit.com/v5/market/instruments-info?category=spot&limit=1000",
                   "bybit/spot_instruments.json")
    tk = net.get("https://api.bybit.com/v5/market/tickers?category=spot", "bybit/spot_tickers.json")
    rows = ((inst or {}).get("result", {}) or {}).get("list", [])
    vols = {t["symbol"]: _f(t.get("turnover24h"))
            for t in ((tk or {}).get("result", {}) or {}).get("list", [])}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for it in rows:
        if it.get("status") != "Trading":
            continue
        sym = it["symbol"]
        out[(it["baseCoin"], it["quoteCoin"])] = {
            "symbol": sym,
            "volume_24h_quote_usd": vols.get(sym, 0.0),
            "min_notional_usd": _f((it.get("lotSizeFilter") or {}).get("minOrderAmt")),
        }
    return out


def _bybit_spot_book(net: Net, symbol: str) -> ac.Book | None:
    d = net.get(f"https://api.bybit.com/v5/market/orderbook?category=spot&symbol={symbol}&limit=100",
                f"bybit/spot_book/{symbol}.json")
    res = (d or {}).get("result") if isinstance(d, dict) else None
    if not isinstance(res, dict):
        return None
    b = _book_from_levels(res.get("b"), res.get("a"))
    return None if b.empty else b


def _okx_spot_pairs(net: Net) -> dict[tuple[str, str], dict[str, Any]]:
    inst = net.get("https://www.okx.com/api/v5/public/instruments?instType=SPOT",
                   "okx/spot_instruments.json")
    tk = net.get("https://www.okx.com/api/v5/market/tickers?instType=SPOT", "okx/spot_tickers.json")
    rows = (inst or {}).get("data", []) if isinstance(inst, dict) else []
    vols = {t["instId"]: _f(t.get("volCcy24h")) * _f(t.get("last"))
            for t in ((tk or {}).get("data", []) or [])}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for it in rows:
        if it.get("state") != "live":
            continue
        out[(it["baseCcy"], it["quoteCcy"])] = {
            "symbol": it["instId"],
            "volume_24h_quote_usd": vols.get(it["instId"], 0.0),
            "min_notional_usd": _f(it.get("minSz")),
        }
    return out


def _okx_spot_book(net: Net, symbol: str) -> ac.Book | None:
    d = net.get(f"https://www.okx.com/api/v5/market/books?instId={symbol}&sz=100",
                f"okx/spot_book/{symbol}.json")
    data = (d or {}).get("data", []) if isinstance(d, dict) else []
    if not data:
        return None
    row = data[0]
    b = _book_from_levels(row.get("bids"), row.get("asks"))
    return None if b.empty else b


def _binance_spot_pairs(net: Net) -> dict[tuple[str, str], dict[str, Any]]:
    ei = net.get("https://api.binance.com/api/v3/exchangeInfo", "binance/spot_exchangeinfo.json")
    tk = net.get("https://api.binance.com/api/v3/ticker/24hr", "binance/spot_ticker24.json")
    rows = (ei or {}).get("symbols", []) if isinstance(ei, dict) else []
    vols = {t["symbol"]: _f(t.get("quoteVolume")) for t in (tk or []) if isinstance(tk, list)}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for it in rows:
        if it.get("status") != "TRADING":
            continue
        out[(it["baseAsset"], it["quoteAsset"])] = {
            "symbol": it["symbol"],
            "volume_24h_quote_usd": vols.get(it["symbol"], 0.0),
            "min_notional_usd": 5.0,
        }
    return out


def _binance_spot_book(net: Net, symbol: str) -> ac.Book | None:
    d = net.get(f"https://api.binance.com/api/v3/depth?symbol={symbol}&limit=100",
                f"binance/spot_book/{symbol}.json")
    if not isinstance(d, dict):
        return None
    b = _book_from_levels(d.get("bids"), d.get("asks"))
    return None if b.empty else b


TRI_VENUES: dict[str, dict[str, Any]] = {
    "coinbase": {"pairs": _cb_pairs, "book": _cb_book},
    "kraken": {"pairs": _kraken_pairs, "book": _kraken_book},
    "deribit": {"pairs": _deribit_pairs, "book": _deribit_book},
    "cryptocom": {"pairs": _cdc_pairs, "book": _cdc_book},
    "bybit": {"pairs": _bybit_spot_pairs, "book": _bybit_spot_book},
    "okx": {"pairs": _okx_spot_pairs, "book": _okx_spot_book},
    "binance": {"pairs": _binance_spot_pairs, "book": _binance_spot_book},
}


# ===========================================================================
# Cross-venue funding adapters for the NEW venues
# ===========================================================================


def binance_fut_enumerate(net: Net) -> dict[str, dict[str, Any]]:
    ei = net.get("https://fapi.binance.com/fapi/v1/exchangeInfo", "binance/fapi_exchangeinfo.json")
    pi = net.get("https://fapi.binance.com/fapi/v1/premiumIndex", "binance/fapi_premiumindex.json")
    tk = net.get("https://fapi.binance.com/fapi/v1/ticker/24hr", "binance/fapi_ticker24.json")
    fi = net.get("https://fapi.binance.com/fapi/v1/fundingInfo", "binance/fapi_fundinginfo.json")
    intervals = {r["symbol"]: _f(r.get("fundingIntervalHours"), 8.0) for r in (fi or [])}
    vols = {t["symbol"]: _f(t.get("quoteVolume")) for t in (tk or [])}
    marks = {p["symbol"]: _f(p.get("markPrice")) for p in (pi or [])}
    out: dict[str, dict[str, Any]] = {}
    for s in (ei or {}).get("symbols", []):
        if s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") != "USDT":
            continue
        if s.get("status") != "TRADING":
            continue
        sym = s["symbol"]
        base = s["baseAsset"]
        mn = next((_f(f.get("notional")) for f in s.get("filters", [])
                   if f.get("filterType") == "MIN_NOTIONAL"), 0.0)
        out[base] = {
            "symbol": sym,
            "interval_hours": intervals.get(sym, 8.0),
            "volume_24h_quote_usd": vols.get(sym, 0.0),
            "mark_price": marks.get(sym) or None,
            "min_notional_usd": mn,
        }
    return out


def binance_fut_history(net: Net, symbol: str, days: float) -> tuple[list[float], list[int]]:
    end = int(time.time() * 1000)
    floor_ms = end - int(days * 86_400_000)
    url = (f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={urllib.parse.quote(symbol)}"
           f"&startTime={floor_ms}&endTime={end}&limit=1000")
    rows = net.get(url, f"binance/fapi_funding/{symbol}.json")
    if not isinstance(rows, list):
        return [], []
    rows = sorted(rows, key=lambda r: r["fundingTime"])
    return [float(r["fundingRate"]) for r in rows], [int(r["fundingTime"]) for r in rows]


def binance_fut_book(net: Net, symbol: str) -> tuple[float | None, float | None]:
    d = net.get(f"https://fapi.binance.com/fapi/v1/depth?symbol={urllib.parse.quote(symbol)}"
                "&limit=100", f"binance/fapi_book/{symbol}.json")
    return sp._half_spread_and_depth({"bids": (d or {}).get("bids", []),
                                      "asks": (d or {}).get("asks", [])}, kind="okx")


def deribit_fut_enumerate(net: Net) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for ccy in ("BTC", "ETH", "SOL", "XRP", "AVAX", "LINK", "DOGE", "MATIC", "BNB"):
        d = net.get(f"https://www.deribit.com/api/v2/public/get_instruments?currency={ccy}"
                    "&kind=future", f"deribit/fut_{ccy}.json")
        summ = net.get("https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
                       f"?currency={ccy}&kind=future", f"deribit/futsum_{ccy}.json")
        volmap = {r["instrument_name"]: _f(r.get("volume_usd"))
                  for r in (summ or {}).get("result", [])}
        for inst in (d or {}).get("result", []):
            if inst.get("settlement_period") != "perpetual" or not inst.get("is_active"):
                continue
            out[ccy] = {
                "symbol": inst["instrument_name"],
                "interval_hours": 8.0,
                "volume_24h_quote_usd": volmap.get(inst["instrument_name"], 0.0),
                "mark_price": None,
                "min_notional_usd": 0.0,
            }
    return out


def deribit_fut_history(net: Net, symbol: str, days: float) -> tuple[list[float], list[int]]:
    end = int(time.time() * 1000)
    start = end - int(days * 86_400_000)
    d = net.get("https://www.deribit.com/api/v2/public/get_funding_rate_history"
                f"?instrument_name={urllib.parse.quote(symbol)}&start_timestamp={start}"
                f"&end_timestamp={end}", f"deribit/funding/{symbol}.json")
    rows = (d or {}).get("result", []) if isinstance(d, dict) else []
    rows = [r for r in rows if start <= int(r["timestamp"]) <= end]
    rows.sort(key=lambda r: r["timestamp"])
    return [float(r["interest_8h"]) for r in rows], [int(r["timestamp"]) for r in rows]


def deribit_fut_book(net: Net, symbol: str) -> tuple[float | None, float | None]:
    d = net.get("https://www.deribit.com/api/v2/public/get_order_book"
                f"?instrument_name={urllib.parse.quote(symbol)}&depth=50",
                f"deribit/fut_book/{symbol}.json")
    res = (d or {}).get("result") if isinstance(d, dict) else None
    if not isinstance(res, dict):
        return None, None
    return sp._half_spread_and_depth({"bids": res.get("bids", []), "asks": res.get("asks", [])},
                                     kind="okx")


def krakenfut_enumerate(net: Net) -> dict[str, dict[str, Any]]:
    d = net.get("https://futures.kraken.com/derivatives/api/v3/instruments",
                "krakenfut/instruments.json")
    tk = net.get("https://futures.kraken.com/derivatives/api/v3/tickers", "krakenfut/tickers.json")
    volmap = {t["symbol"]: _f(t.get("volumeQuote"))
              for t in (tk or {}).get("tickers", [])} if isinstance(tk, dict) else {}
    out: dict[str, dict[str, Any]] = {}
    for inst in (d or {}).get("instruments", []) if isinstance(d, dict) else []:
        if inst.get("type") != "flexible_futures" or not inst.get("tradeable"):
            continue
        sym = inst["symbol"]
        if not sym.startswith("PF_"):
            continue
        base = sym[3:].replace("XBT", "BTC").replace("USD", "")
        out[base] = {
            "symbol": sym,
            "interval_hours": 1.0,
            "volume_24h_quote_usd": volmap.get(sym, 0.0),
            "mark_price": None,
            "min_notional_usd": 0.0,
        }
    return out


def krakenfut_history(net: Net, symbol: str, days: float) -> tuple[list[float], list[int]]:
    d = net.get(f"https://futures.kraken.com/derivatives/api/v3/historical-funding-rates"
                f"?symbol={urllib.parse.quote(symbol)}", f"krakenfut/funding/{symbol}.json")
    rows = (d or {}).get("rates", []) if isinstance(d, dict) else []
    floor_ms = int(time.time() * 1000) - int(days * 86_400_000)
    out_r: list[float] = []
    out_t: list[int] = []
    for r in rows:
        ts = int(datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).timestamp() * 1000)
        if ts < floor_ms:
            continue
        out_r.append(float(r["relativeFundingRate"]))
        out_t.append(ts)
    order = sorted(range(len(out_t)), key=lambda i: out_t[i])
    return [out_r[i] for i in order], [out_t[i] for i in order]


def krakenfut_book(net: Net, symbol: str) -> tuple[float | None, float | None]:
    d = net.get(f"https://futures.kraken.com/derivatives/api/v3/tickers/{urllib.parse.quote(symbol)}",
                f"krakenfut/ticker/{symbol}.json")
    t = (d or {}).get("ticker") if isinstance(d, dict) else None
    if not isinstance(t, dict):
        return None, None
    bid, ask = _f(t.get("bid")), _f(t.get("ask"))
    if bid <= 0 or ask <= 0:
        return None, None
    half = (ask - bid) / ((ask + bid) / 2.0) * BPS / 2.0
    depth = _f(t.get("bidSize")) * bid + _f(t.get("askSize")) * ask
    return round(half, 4), round(depth, 2)


def intx_enumerate(net: Net) -> dict[str, dict[str, Any]]:
    d = net.get("https://api.international.coinbase.com/api/v1/instruments",
                "intx/instruments.json")
    out: dict[str, dict[str, Any]] = {}
    for inst in d if isinstance(d, list) else []:
        if inst.get("type") != "PERP":
            continue
        base = inst.get("base_asset_name")
        out[base] = {
            "symbol": inst["symbol"],
            "interval_hours": 1.0,
            "volume_24h_quote_usd": _f(inst.get("avg_daily_notional")),
            "mark_price": None,
            "min_notional_usd": _f(inst.get("base_increment")) * 0.0,
        }
    return out


def intx_history(net: Net, symbol: str, days: float) -> tuple[list[float], list[int]]:
    floor_ms = int(time.time() * 1000) - int(days * 86_400_000)
    rates: list[float] = []
    times: list[int] = []
    offset = 0
    while offset < 2000:
        d = net.get("https://api.international.coinbase.com/api/v1/instruments/"
                    f"{urllib.parse.quote(symbol)}/funding?result_limit=100&result_offset={offset}",
                    f"intx/funding/{symbol}_{offset}.json")
        rows = (d or {}).get("results", []) if isinstance(d, dict) else []
        if not rows:
            break
        for r in rows:
            ts = int(datetime.fromisoformat(
                r["event_time"].replace("Z", "+00:00")).timestamp() * 1000)
            if ts >= floor_ms:
                rates.append(float(r["funding_rate"]))
                times.append(ts)
        if len(rows) < 100:
            break
        offset += 100
    order = sorted(range(len(times)), key=lambda i: times[i])
    return [rates[i] for i in order], [times[i] for i in order]


def intx_book(net: Net, symbol: str) -> tuple[float | None, float | None]:
    # No public order-book endpoint on Coinbase INTX (404 on /book); half-spread unknown.
    return None, None


NEW_FUNDING_VENUES: dict[str, dict[str, Any]] = {
    "binance": {"enumerate": binance_fut_enumerate, "history": binance_fut_history,
                "book": binance_fut_book},
    "deribit": {"enumerate": deribit_fut_enumerate, "history": deribit_fut_history,
                "book": deribit_fut_book},
    "kraken_futures": {"enumerate": krakenfut_enumerate, "history": krakenfut_history,
                       "book": krakenfut_book},
    "coinbase_intx": {"enumerate": intx_enumerate, "history": intx_history, "book": intx_book},
}


# ===========================================================================
# Runner: triangular
# ===========================================================================


def _tri_worker(args: tuple) -> dict[str, Any]:
    venue, tri, net, notional, taker_bps = args
    books: list[tuple[ac.Book, str]] = []
    for (base, quote), side in tri["legs"]:
        sym = tri["symbols"][(base, quote)]
        book = TRI_VENUES[venue]["book"](net, sym)
        if book is None:
            return {"venue": venue, "triangle": "->".join(tri["path"]),
                    "path": "->".join(tri["path"]),
                    "legs": [f"{b}/{q}" for (b, q), _ in tri["legs"]],
                    "class": "crypto", "min_leg_volume_usd": tri["min_leg_volume_usd"],
                    "samples": 1, "executable_samples": 0, "max_net_bps": None,
                    "mean_net_bps": None, "median_net_bps": None, "p90_net_bps": None,
                    "min_net_bps": None, "pct_samples_positive": 0.0,
                    "longest_positive_run_samples": 0, "longest_positive_run_s": 0.0}
        books.append((book, side))
    edge = triangle_net_bps(books, notional, taker_bps)
    series = [edge]
    summ = summarize_series(series, 1.0)
    summ.update({"venue": venue, "triangle": "->".join(tri["path"]), "path": "->".join(tri["path"]),
                 "legs": [f"{b}/{q}" for (b, q), _ in tri["legs"]], "class": "crypto",
                 "min_leg_volume_usd": tri["min_leg_volume_usd"]})
    return summ


def run_triangular(net: Net, *, venues: list[str], minutes: float, interval: float,
                   notional: float, min_volume: float, top_bases: int) -> dict[str, Any]:
    payload: dict[str, Any] = {"generated_at": datetime.now(UTC).isoformat(),
                              "notional_usd": notional, "venues": {}}
    for venue in venues:
        deadline = time.monotonic() + minutes * 60.0  # each venue gets the full window
        fee = VENUE_SPOT_FEE[venue]
        pairs = TRI_VENUES[venue]["pairs"](net)
        tris = find_triangles(pairs, min_volume_usd=min_volume, top_bases=top_bases)
        for t in tris:
            t["symbols"] = {leg: pairs[leg]["symbol"] for leg, _ in t["legs"]}
        # Rank by the thinner leg's volume so the polled set is the liquid set.
        tris.sort(key=lambda t: -t["min_leg_volume_usd"])
        poll = tris[:12]
        series: list[list[float | None]] = [[] for _ in poll]
        samples = 0
        net.cache_enabled = False  # live books, never replayed from cache
        while time.monotonic() < deadline and poll:
            samples += 1
            with ThreadPoolExecutor(max_workers=4) as pool:
                rows = list(pool.map(_tri_worker, [(venue, t, net, notional, fee["taker_bps"])
                                                   for t in poll]))
            for i, row in enumerate(rows):
                series[i].append(row["max_net_bps"] if row["executable_samples"] else None)
            if time.monotonic() + interval >= deadline:
                break
            time.sleep(interval)
        net.cache_enabled = True
        results = []
        for tri, s in zip(poll, series, strict=False):
            summ = summarize_series(s, interval)
            summ.update({"triangle": "->".join(tri["path"]),
                         "legs": [f"{b}/{q}" for (b, q), _ in tri["legs"]],
                         "min_leg_volume_usd": tri["min_leg_volume_usd"]})
            results.append(summ)
        results.sort(key=lambda r: -(r["max_net_bps"] if r["max_net_bps"] is not None else -1e9))
        payload["venues"][venue] = {
            "fee": fee, "n_triangles_enumerated": len(tris), "n_polled": len(poll),
            "samples": samples, "interval_s": interval,
            "best": results[0] if results else None, "triangles": results,
        }
        print(f"[tri:{venue}] {len(tris)} triangles, polled {len(poll)} x{samples}",
              file=sys.stderr)
    return payload


# ===========================================================================
# Runner: cross-venue funding (extends the existing scanner's registry)
# ===========================================================================


def run_funding(net: Net, *, venues: list[str], days: float, notional: float,
                max_symbols: int, min_volume: float) -> dict[str, Any]:
    # Merge the new adapters into the existing scanner's registry + fee notes.
    sp.VENUES.update(NEW_FUNDING_VENUES)
    sp.VENUE_FEE_NOTE.update(VENUE_PERP_FEE)
    # Net is mv.HttpClient-compatible (same ``request(url, cache_path=..., data=...)``
    # signature), so the existing adapters and collect_venue run on it unchanged.
    client: Any = net

    metas: dict[str, dict[str, dict[str, Any]]] = {}
    for venue in venues:
        try:
            metas[venue] = sp.VENUES[venue]["enumerate"](client)
        except Exception as err:  # noqa: BLE001
            print(f"! {venue} enumerate: {type(err).__name__}: {err}", file=sys.stderr)
            metas[venue] = {}
    listing: dict[str, list[str]] = {}
    for venue, meta in metas.items():
        for base, row in meta.items():
            if _f(row.get("volume_24h_quote_usd")) >= min_volume:
                listing.setdefault(base, []).append(venue)
    need = {b for b, vs in listing.items() if len(vs) >= 2}
    print(f"funding: {({v: len(m) for v, m in metas.items()})}; {len(need)} bases on >=2 venues",
          file=sys.stderr)
    venue_data: dict[str, dict[str, dict[str, Any]]] = {}
    for venue in venues:
        venue_data[venue] = (sp.collect_venue(client, venue, days=days, min_volume_usd=min_volume,
                                              max_symbols=max_symbols, need_history=need)
                             if metas.get(venue) else {})
    results = sp.build_pairs(venue_data, notional=notional, block=5, n_boot=2000)
    order = {"candidate": 0, "no": 1, "unconstructible": 2}
    results.sort(key=lambda r: (order.get(r.verdict, 9), -r.gross_dollars_per_day,
                                -r.abs_mean_spread_bps_8h))
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "notional_usd": notional, "leg_usd": notional / 2.0,
        "bucket_hours": sp.BUCKET_HOURS,
        "fee_model": {"source": "FeeSchedule §10.2 + venue published (unverified)",
                      "futures_taker_bps": FEES.futures_taker_bps},
        "venue_fee_notes": {v: sp.VENUE_FEE_NOTE.get(v, {}) for v in venues},
        "coverage": {v: {"enumerated": len(metas.get(v, {})),
                         "measured": len(venue_data.get(v, {}))} for v in venues},
        "n_pairs": len(results),
        "n_candidates": sum(1 for r in results if r.verdict == "candidate"),
        "pairs": [asdict(r) for r in results],
    }


# ===========================================================================
# Runner: live Binance USDⓈ-M funding + basis (full universe)
# ===========================================================================


def run_binance(net: Net, *, notional: float, top_history: int, days: float) -> dict[str, Any]:
    pi = net.get("https://fapi.binance.com/fapi/v1/premiumIndex", "binance/fapi_premiumindex.json")
    ei = net.get("https://fapi.binance.com/fapi/v1/exchangeInfo", "binance/fapi_exchangeinfo.json")
    tk = net.get("https://fapi.binance.com/fapi/v1/ticker/24hr", "binance/fapi_ticker24.json")
    fi = net.get("https://fapi.binance.com/fapi/v1/fundingInfo", "binance/fapi_fundinginfo.json")
    intervals = {r["symbol"]: _f(r.get("fundingIntervalHours"), 8.0) for r in (fi or [])}
    meta = {s["symbol"]: s for s in (ei or {}).get("symbols", [])
            if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"}
    vols = {t["symbol"]: _f(t.get("quoteVolume")) for t in (tk or [])}
    leg = notional / 2.0
    rows: list[dict[str, Any]] = []
    for p in pi or []:
        sym = p["symbol"]
        if sym not in meta:
            continue
        rate = _f(p.get("lastFundingRate"))
        interval = intervals.get(sym, 8.0)
        mark, index = _f(p.get("markPrice")), _f(p.get("indexPrice"))
        mn = next((_f(f.get("notional")) for f in meta[sym].get("filters", [])
                   if f.get("filterType") == "MIN_NOTIONAL"), 0.0)
        rows.append({
            "symbol": sym, "base": meta[sym]["baseAsset"],
            "funding_rate": rate, "interval_hours": interval,
            "funding_bps_per_settlement": round(rate * BPS, 4),
            "funding_usd_per_day_at_leg": round(leg * rate * (24.0 / interval), 6),
            "annualized_pct": round(rate * (24.0 / interval) * 365 * 100, 2),
            "mark_price": mark, "index_price": index,
            "premium_bps": (None if index == 0 else round((mark - index) / index * BPS, 4)),
            "quote_volume_24h_usd": vols.get(sym, 0.0),
            "min_notional_usd": mn, "permits_50_leg": min_notional_ok(mn, leg),
        })
    # Rank by |funding/day| at the $50 leg, then fetch history for the top names.
    rows.sort(key=lambda r: -abs(r["funding_usd_per_day_at_leg"]))
    bps8 = [r["funding_bps_per_settlement"] * (8.0 / r["interval_hours"]) for r in rows]
    distribution = {
        "n": len(bps8),
        "mean_bps_8h": round(st.fmean(bps8), 4),
        "median_bps_8h": round(st.median(bps8), 4),
        "p10_bps_8h": round(ac.percentile(bps8, 10), 4),
        "p90_bps_8h": round(ac.percentile(bps8, 90), 4),
        "max_abs_bps_8h": round(max(abs(x) for x in bps8), 4),
        "n_abs_ge_30bps_8h": sum(1 for x in bps8 if abs(x) >= 30.0),
    }
    hist: list[dict[str, Any]] = []
    for r in rows[:top_history]:
        rates, times = binance_fut_history(net, r["symbol"], days)
        if len(rates) < 30:
            continue
        gated = sp.evaluate_pair(
            base=r["base"], venue_a="binance", symbol_a=r["symbol"],
            venue_b="binance", symbol_b=r["symbol"],
            a_buckets=sp.bucket_funding(times, rates),
            b_buckets=sp.bucket_funding(times, [0.0] * len(rates)),  # self-spread => signed series
            notional=notional, block=5, n_boot=2000,
            min_notional_a_usd=r["min_notional_usd"], min_notional_b_usd=r["min_notional_usd"],
            depth_a_usd=None, depth_b_usd=None, half_spread_a_bps=None, half_spread_b_bps=None,
        )
        hist.append({"symbol": r["symbol"], "n_events": len(rates),
                     "window_days": round((max(times) - min(times)) / 86_400_000.0, 2),
                     "mean_rate_bps": round(st.fmean(rates) * BPS, 5),
                     "positive_share": round(sum(1 for x in rates if x > 0) / len(rates), 3),
                     "funding_usd_per_day_at_leg": round(funding_per_day(leg, rates, times), 6),
                     "signed_verdict": gated.verdict, "reason": gated.reason})
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "notional_usd": notional, "leg_usd": leg, "universe": "Binance USDⓈ-M USDT perps",
        "n_perps": len(rows),
        "n_permitting_50_leg": sum(1 for r in rows if r["permits_50_leg"]),
        "n_positive_funding": sum(1 for r in rows if r["funding_rate"] > 0),
        "n_negative_funding": sum(1 for r in rows if r["funding_rate"] < 0),
        "instantaneous_distribution_bps_8h": distribution,
        "top_by_abs_funding_per_day": rows[:40],
        "top_by_volume": sorted(rows, key=lambda r: -r["quote_volume_24h_usd"])[:20],
        "history_gated": hist,
        "persistent_tail_check": {
            "bar": "mean >= 30 bps/8h AND sign-persistent >= 14d",
            "n_history_gated": len(hist),
            "n_abs_mean_ge_30bps_8h": sum(1 for h in hist if abs(h["mean_rate_bps"]) >= 30.0),
            "max_abs_mean_bps_8h": round(
                max((abs(h["mean_rate_bps"]) for h in hist), default=0.0), 4),
            "any_passing": any(
                abs(h["mean_rate_bps"]) >= 30.0 and h["signed_verdict"] == "candidate"
                for h in hist),
        },
        "note": ("Live fapi data (first time reachable through WARP). Premium = (mark-index)/index "
                 "uses the index as spot proxy; history gates reuse "
                 "arb_funding_spread.evaluate_pair "
                 "with a zero second leg so the signed funding series itself is gated."),
    }
    return payload


# ===========================================================================
# Runner: spot-perp basis carry on venues with both legs
# ===========================================================================


SPOT_PERP: dict[str, dict[str, Any]] = {
    "binance": {
        "spot_book": ("https://api.binance.com/api/v3/depth?symbol={s}&limit=100",
                      "binance/spb/{s}"),
        "perp_book": ("https://fapi.binance.com/fapi/v1/depth?symbol={s}&limit=100",
                      "binance/pb/{s}"),
        "symbols": {"BTC": ("BTCUSDT", "BTCUSDT"), "ETH": ("ETHUSDT", "ETHUSDT"),
                    "SOL": ("SOLUSDT", "SOLUSDT"), "XRP": ("XRPUSDT", "XRPUSDT"),
                    "DOGE": ("DOGEUSDT", "DOGEUSDT"), "BNB": ("BNBUSDT", "BNBUSDT")},
    },
    "bybit": {
        "spot_book": ("https://api.bybit.com/v5/market/orderbook?category=spot&symbol={s}&limit=100",
                      "bybit/spb/{s}"),
        "perp_book": ("https://api.bybit.com/v5/market/orderbook?category=linear&symbol={s}"
                      "&limit=100", "bybit/pb/{s}"),
        "symbols": {"BTC": ("BTCUSDT", "BTCUSDT"), "ETH": ("ETHUSDT", "ETHUSDT"),
                    "SOL": ("SOLUSDT", "SOLUSDT")},
    },
    "okx": {
        "spot_book": ("https://www.okx.com/api/v5/market/books?instId={s}&sz=100", "okx/spb/{s}"),
        "perp_book": ("https://www.okx.com/api/v5/market/books?instId={s}-SWAP&sz=100",
                      "okx/pb/{s}"),
        "symbols": {"BTC": ("BTC-USDT", "BTC-USDT"), "ETH": ("ETH-USDT", "ETH-USDT")},
    },
    "deribit": {
        "spot_book": ("https://www.deribit.com/api/v2/public/get_order_book?instrument_name={s}"
                      "&depth=50", "deribit/spb/{s}"),
        "perp_book": ("https://www.deribit.com/api/v2/public/get_order_book?instrument_name={s}"
                      "&depth=50", "deribit/pb/{s}"),
        "symbols": {"BTC": ("BTC_USDT", "BTC-PERPETUAL"), "ETH": ("ETH_USDT", "ETH-PERPETUAL")},
    },
}


def _extract_book(venue: str, d: Any) -> ac.Book | None:
    if not isinstance(d, dict):
        return None
    if venue == "binance":
        return _book_from_levels(d.get("bids"), d.get("asks"))
    if venue == "bybit":
        r = d.get("result", {})
        return _book_from_levels(r.get("b"), r.get("a"))
    if venue == "okx":
        data = d.get("data", [])
        return _book_from_levels(data[0].get("bids"), data[0].get("asks")) if data else None
    if venue == "deribit":
        r = d.get("result", {})
        return _book_from_levels(r.get("bids"), r.get("asks"))
    return None


def run_basis(net: Net, *, notional: float, venues: list[str]) -> dict[str, Any]:
    leg = notional / 2.0
    out: dict[str, Any] = {"generated_at": datetime.now(UTC).isoformat(),
                           "notional_usd": notional, "leg_usd": leg, "venues": {}}
    for venue in venues:
        cfg = SPOT_PERP[venue]
        spot_fee = VENUE_SPOT_FEE.get(venue, {}).get("taker_bps", FEES.spot_taker_bps) / BPS
        perp_fee = VENUE_PERP_FEE.get(venue, {}).get("taker_bps", FEES.futures_taker_bps) / BPS
        rows = []
        for base, (spot_sym, perp_sym) in cfg["symbols"].items():
            su = cfg["spot_book"][0].format(s=spot_sym)
            pu = cfg["perp_book"][0].format(s=perp_sym)
            sb = _extract_book(venue, net.get(su, cfg["spot_book"][1].format(s=spot_sym)))
            pb = _extract_book(venue, net.get(pu, cfg["perp_book"][1].format(s=perp_sym)))
            if sb is None or pb is None or sb.empty or pb.empty:
                continue
            spot_bid, spot_ask = sb.best_bid, sb.best_ask
            perp_bid, perp_ask = pb.best_bid, pb.best_ask
            # Carry entry: buy spot at ask, short perp at bid. Basis = perp/spot - 1.
            entry_basis_bps = (perp_bid - spot_ask) / spot_ask * BPS
            # Round-trip cost: 2 spot crossings + 2 perp crossings at each venue's taker.
            cost_bps = 2 * (spot_fee + perp_fee) * BPS
            rows.append({
                "base": base, "spot_symbol": spot_sym, "perp_symbol": perp_sym,
                "spot_bid": spot_bid, "spot_ask": spot_ask,
                "perp_bid": perp_bid, "perp_ask": perp_ask,
                "entry_basis_bps": round(entry_basis_bps, 4),
                "roundtrip_cost_bps": round(cost_bps, 4),
                "net_entry_bps": round(entry_basis_bps - cost_bps, 4),
                "permits_50_leg": min_notional_ok(0.0, leg),
            })
        rows.sort(key=lambda r: -r["net_entry_bps"])
        out["venues"][venue] = {
            "spot_taker_bps": round(spot_fee * BPS, 3), "perp_taker_bps": round(perp_fee * BPS, 3),
            "pairs": rows, "best": rows[0] if rows else None,
        }
        print(f"[basis:{venue}] {len(rows)} pairs", file=sys.stderr)
    return out


# ===========================================================================
# CLI
# ===========================================================================


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("class_", choices=["tri", "funding", "binance", "basis", "probe"])
    ap.add_argument("--venues", default="")
    ap.add_argument("--minutes", type=float, default=25.0)
    ap.add_argument("--interval", type=float, default=7.0)
    ap.add_argument("--notional", type=float, default=100.0)
    ap.add_argument("--min-volume", type=float, default=50_000.0)
    ap.add_argument("--top-bases", type=int, default=25)
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--max-symbols", type=int, default=120)
    ap.add_argument("--top-history", type=int, default=40)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    net = Net(args.out / "raw")
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]

    if args.class_ == "tri":
        payload = run_triangular(net, venues=venues or list(TRI_VENUES), minutes=args.minutes,
                                 interval=args.interval, notional=args.notional,
                                 min_volume=args.min_volume, top_bases=args.top_bases)
        name = "triangular_warp.json"
    elif args.class_ == "funding":
        payload = run_funding(net, venues=venues or list(NEW_FUNDING_VENUES), days=args.days,
                              notional=args.notional, max_symbols=args.max_symbols,
                              min_volume=args.min_volume)
        name = "funding_warp.json"
    elif args.class_ == "binance":
        payload = run_binance(net, notional=args.notional, top_history=args.top_history,
                              days=args.days)
        name = "binance_futures_warp.json"
    elif args.class_ == "basis":
        payload = run_basis(net, notional=args.notional,
                            venues=venues or list(SPOT_PERP))
        name = "basis_warp.json"
    else:  # probe
        payload = {"generated_at": datetime.now(UTC).isoformat(), "http": net.stats}
        name = "probe_warp.json"
    (args.out / name).write_text(json.dumps(payload, indent=2))
    print(f"wrote {args.out / name}  http={net.stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
