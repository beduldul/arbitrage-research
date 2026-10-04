#!/usr/bin/env python3
"""Instrument-identity guard for cross-venue pairs (MEASUREMENT ONLY).

The WARP funding scan derived a Kraken base by *string-stripping* the symbol:

    ``sym[3:].replace('XBT','BTC').replace('USD','')``

which turned Kraken's ``PF_USDTUSD`` (the USDT/USD **stablecoin** perp) into base ``T``,
colliding it with Binance Threshold Network (``TUSDT``, $0.0054). The reported best pair
``T`` binance↔kraken_futures was therefore two *different assets* — the "spread" was
Binance Threshold's own funding, unhedged, with an inert second leg.

This module replaces symbol string-stripping with each venue's OWN instrument metadata
(``base``/``quote``/``category``/``type``), and adds a price-sanity check. A pair is
VALID only when both legs are the same underlying base, quoted in the same fiat/stable
class, both are live perps (not stablecoin/index/dated/quanto), and their prices agree
within a sane basis.

Pure computation (``guard_pair``) is unit-tested; the metadata loaders read the run's
already-cached payloads under ``.../warp/raw/``.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
WARP_RAW = REPO_ROOT / "evidence" / "arbitrage" / "2026-10-03" / "warp" / "raw"

#: Bases that are stablecoins/stable-index, not tradeable token underlyings. A "perp" on
#: one of these is a peg instrument, not comparable to a token perp.
STABLE_BASES = {"USDT", "USDC", "DAI", "TUSD", "USDE", "FDUSD", "BUSD", "USD", "USDP", "PYUSD"}

#: Quote assets treated as the same fiat/stable class (a USD↔USDT quote is a peg, ~1:1).
QUOTE_CLASS = {"USD", "USDT", "USDC"}

#: Dated / quanto / inverse contract types that are NOT a perpetual token perp.
NON_PERP_TYPES = {"futures", "futures_inverse", "dated", "quanto"}

#: Max tolerated price ratio between the two legs. A funding spread is delta-neutral only
#: if both perps track the same underlying; a >10% static divergence is not a hedge.
#: (ONE bitget/okx shows ~18.6% — OKX $0.00212 vs Bitget $0.00249 — and is excluded.)
MAX_PRICE_RATIO = 1.10


@dataclass(frozen=True)
class Instrument:
    """One venue's own view of an instrument."""

    venue: str
    symbol: str
    base: str
    quote: str
    kind: str  # e.g. "PERPETUAL", "flexible_futures", "LinearPerpetual"
    category: str | None = None
    price: float | None = None


def _load(rel: str) -> Any:
    return json.loads((WARP_RAW / rel).read_text())


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def binance_instruments() -> dict[str, Instrument]:
    ei = _load("binance/fapi_exchangeinfo.json")
    tk = {
        t["symbol"]: _f(t.get("lastPrice")) or _f(t.get("bidPrice")) or _f(t.get("askPrice"))
        for t in _load("binance/fapi_ticker24.json")
    }
    out: dict[str, Instrument] = {}
    for s in ei.get("symbols", []):
        if s.get("contractType") != "PERPETUAL" or s.get("status") != "TRADING":
            continue
        out[s["symbol"]] = Instrument(
            venue="binance", symbol=s["symbol"], base=str(s["baseAsset"]).upper(),
            quote=str(s["quoteAsset"]).upper(), kind="PERPETUAL",
            price=tk.get(s["symbol"]),
        )
    return out


def krakenfut_instruments() -> dict[str, Instrument]:
    inst = _load("krakenfut/instruments.json").get("instruments", [])
    tk = {t["symbol"]: _f(t.get("last")) for t in _load("krakenfut/tickers.json")["tickers"]}
    out: dict[str, Instrument] = {}
    for i in inst:
        if not str(i.get("symbol", "")).startswith("PF_") or not i.get("tradeable"):
            continue
        out[i["symbol"]] = Instrument(
            venue="kraken_futures", symbol=i["symbol"],
            base=str(i.get("base") or "").upper(), quote=str(i.get("quote") or "").upper(),
            kind=str(i.get("type") or ""), category=i.get("category"),
            price=tk.get(i["symbol"]),
        )
    return out


def bybit_instruments() -> dict[str, Instrument]:
    inst = _load("bybit/instruments.json")["result"]["list"]
    tk = {
        t["symbol"]: _f(t.get("lastPrice"))
        for t in _load("bybit/tickers.json")["result"]["list"]
    }
    out: dict[str, Instrument] = {}
    for i in inst:
        if i.get("contractType") != "LinearPerpetual" or i.get("status") != "Trading":
            continue
        out[i["symbol"]] = Instrument(
            venue="bybit", symbol=i["symbol"], base=str(i["baseCoin"]).upper(),
            quote=str(i["quoteCoin"]).upper(), kind="LinearPerpetual",
            price=tk.get(i["symbol"]),
        )
    return out


def okx_instruments() -> dict[str, Instrument]:
    inst = _load("okx/instruments.json")["data"]
    tk = {t["instId"]: _f(t.get("last")) for t in _load("okx/tickers.json")["data"]}
    out: dict[str, Instrument] = {}
    for i in inst:
        if i.get("state") != "live" or i.get("ctType") != "linear":
            continue
        if not str(i.get("instId", "")).endswith("-USDT-SWAP"):
            continue
        out[i["instId"]] = Instrument(
            venue="okx", symbol=i["instId"], base=str(i.get("ctValCcy") or "").upper(),
            quote=str(i.get("settleCcy") or "").upper(), kind="linear",
            price=tk.get(i["instId"]),
        )
    return out


def gate_instruments() -> dict[str, Instrument]:
    contracts = _load("gate/contracts.json")
    tk = {t["contract"]: _f(t.get("last")) for t in _load("gate/tickers.json")}
    out: dict[str, Instrument] = {}
    for c in contracts:
        name = str(c.get("name") or "")
        # Gate publishes no separate base field; its own contract name is ``BASE_USDT``.
        if not name.endswith("_USDT") or c.get("contract_type") not in (None, ""):
            continue
        out[name] = Instrument(
            venue="gate", symbol=name, base=name[: -len("_USDT")].upper(),
            quote="USDT", kind="perp", price=tk.get(name),
        )
    return out


def bitget_instruments() -> dict[str, Instrument]:
    inst = _load("bitget/contracts.json")["data"]
    tk = {t["symbol"]: _f(t.get("lastPr")) for t in _load("bitget/tickers.json")["data"]}
    out: dict[str, Instrument] = {}
    for i in inst:
        if i.get("symbolType") != "perpetual" or i.get("symbolStatus") != "normal":
            continue
        out[i["symbol"]] = Instrument(
            venue="bitget", symbol=i["symbol"], base=str(i.get("baseCoin")).upper(),
            quote=str(i.get("quoteCoin")).upper(), kind="perpetual",
            price=tk.get(i["symbol"]),
        )
    return out


def hyperliquid_instruments() -> dict[str, Instrument]:
    meta = _load("hyperliquid/meta.json")
    out: dict[str, Instrument] = {}
    if not isinstance(meta, list) or len(meta) < 2:
        return out
    universe = meta[0].get("universe", []) if isinstance(meta[0], dict) else []
    for u in universe:
        name = str(u.get("name") or "")
        if not name:
            continue
        out[name] = Instrument(
            venue="hyperliquid", symbol=name, base=name.upper(), quote="USD",
            kind="perp", price=None,
        )
    return out


_LOADERS = {
    "binance": binance_instruments,
    "kraken_futures": krakenfut_instruments,
    "bybit": bybit_instruments,
    "okx": okx_instruments,
    "gate": gate_instruments,
    "bitget": bitget_instruments,
    "hyperliquid": hyperliquid_instruments,
}


def load_instruments(venue: str) -> dict[str, Instrument]:
    """Venue -> its own instrument metadata (cached payloads; no network)."""
    loader = _LOADERS.get(venue)
    if loader is None:
        return {}
    try:
        return loader()
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return {}


def guard_pair(
    a: Instrument | None,
    b: Instrument | None,
    *,
    max_price_ratio: float = MAX_PRICE_RATIO,
) -> dict[str, Any]:
    """Pure identity verdict for one cross-venue pair of instruments.

    ``valid`` is True only when both legs exist, share the same non-stable base, sit in
    the same fiat/stable quote class, are live perps (not dated/stablecoin/index), and
    their prices agree within ``max_price_ratio``. Any failure is named in ``reasons``.
    """
    reasons: list[str] = []
    if a is None or b is None:
        return {"valid": False, "reasons": ["missing_instrument_metadata"],
                "same_base": None, "price_ratio": None}

    same_base = a.base == b.base
    if not same_base:
        reasons.append(f"base_mismatch:{a.base}!={b.base}")
    if a.base in STABLE_BASES or b.base in STABLE_BASES:
        reasons.append(f"stablecoin_base:{a.base}/{b.base}")
    if a.quote not in QUOTE_CLASS or b.quote not in QUOTE_CLASS:
        reasons.append(f"quote_class:{a.quote}/{b.quote}")
    if a.kind.lower() in NON_PERP_TYPES or b.kind.lower() in NON_PERP_TYPES:
        reasons.append(f"non_perp_type:{a.kind}/{b.kind}")

    ratio: float | None = None
    if a.price and b.price:
        ratio = max(a.price, b.price) / min(a.price, b.price)
        if ratio >= max_price_ratio:
            reasons.append(f"price_divergence:{ratio:.3f}x")
    else:
        reasons.append("missing_price")

    return {
        "valid": not reasons,
        "reasons": reasons,
        "same_base": same_base,
        "price_ratio": ratio,
        "base_a": a.base, "base_b": b.base,
        "kind_a": a.kind, "kind_b": b.kind,
        "category_a": a.category, "category_b": b.category,
        "price_a": a.price, "price_b": b.price,
    }


def audit_pairs(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate each funding-scan pair with an identity verdict (no network)."""
    cache: dict[str, dict[str, Instrument]] = {}
    out: list[dict[str, Any]] = []
    for p in pairs:
        for venue in (p["venue_a"], p["venue_b"]):
            if venue not in cache:
                cache[venue] = load_instruments(venue)
        a = cache[p["venue_a"]].get(p["symbol_a"])
        b = cache[p["venue_b"]].get(p["symbol_b"])
        verdict = guard_pair(a, b)
        out.append({**p, "identity": verdict})
    return out


def main(argv: list[str] | None = None) -> int:
    """Print the identity audit for the top executable pairs."""
    funding_path = (
        REPO_ROOT / "evidence" / "arbitrage" / "2026-10-03" / "warp" / "funding_warp.json"
    )
    pairs = json.loads(funding_path.read_text())["pairs"]
    audited = audit_pairs(pairs)
    invalid = [p for p in audited if not p["identity"]["valid"]]
    print(f"{len(pairs)} pairs, {len(invalid)} fail the identity guard")
    for p in invalid[:40]:
        print(f"  INVALID {p['base']:>8} {p['venue_a']}/{p['venue_b']}: "
              f"{p['identity']['reasons']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
