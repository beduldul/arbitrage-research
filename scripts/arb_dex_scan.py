"""Class C — CEX<->DEX price-gap measurement, with the full real cost stack.

**Measurement only.** Public endpoints, no keys, no wallets, no orders, no account
state, no transactions. Raw responses are cached under
``evidence/arbitrage/2026-10-03/dex/`` with timestamps.

The question: **is the gap between a Solana DEX quote and a CEX book ever larger than
the cost of capturing it, at $100 and at $1,000?**

Design notes that matter for honesty:

* **The DEX quote already contains the pool fee and the price impact.** Jupiter's
  ``outAmount`` is what you actually receive, so ``dex_price = outAmount / inAmount``
  is the *executable* DEX price. Subtracting a pool fee again on top of it would
  double-count. This module therefore decomposes the stack into "embedded in the
  quote" (pool fee + impact) and "added on top" (CEX taker, gas, transfer, slippage
  buffer), and the net formula charges each exactly once.
* **Bid/ask, never mid, for the executable comparison.** A gap is only real if you can
  buy at the ask and sell at the bid. The mid is reported alongside for reference only.
* **Costs are the project's own.** CEX taker is the project's §10.2 base tier
  (10 bps/leg). Gas and transfer fees are cited standard ranges, labelled as such —
  they are not measured here and are not presented as measured.
* **MEV is not modelled away.** On Solana and EVM the gap you can see is the gap a
  colocated searcher with private orderflow has already taken. That is a structural
  fact, stated in the report, not a number in the arithmetic.

Run::

    uv run python scripts/arb_dex_scan.py --probe-only
    uv run python scripts/arb_dex_scan.py --minutes 12 --interval 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics as st
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

EVIDENCE = REPO / "evidence" / "arbitrage" / "2026-10-03" / "dex"

#: Basis points per unit fraction. ``1.0 -> 10_000 bps``.
BPS = 10_000.0

# --------------------------------------------------------------- cost constants
#
# Every number here is either the project's own fee schedule or a cited standard
# range. Nothing in this block is measured by this script; it is the *assumption set*
# and is labelled as such in the report.

#: Project §10.2 base-tier spot taker fee, per leg.
CEX_TAKER_BPS = 10.0
#: A round trip pays the taker fee on entry and on exit (buy the token, sell it back
#: / sell the stable leg). Two legs.
CEX_TAKER_ROUND_TRIP_BPS = 2.0 * CEX_TAKER_BPS

#: Solana transaction fee, USD. Standard range 0.000005-0.0001 SOL plus optional
#: priority fee; 0.005 USD is a realistic retail figure at ~$120 SOL.
SOLANA_GAS_USD = 0.005
#: EVM gas, USD. Deliberately a *range*, because it is highly variable by chain and
#: by congestion. Not measured here.
EVM_GAS_USD_RANGE = (2.0, 50.0)

#: CEX withdrawal fee, USD, for moving the token leg to the DEX chain. Binance does
#: not expose per-network withdrawal fees on public endpoints (verified: the public
#: fee page omits the field), so this is a labelled *range*, not a measurement.
#: ``CEX_WITHDRAWAL_USD`` is the long-standing ~0.01 SOL figure; the low end is the
#: market median ~0.001 SOL. Fixed per withdrawal, so it hurts small notionals hardest.
CEX_WITHDRAWAL_USD = 1.20
TRANSFER_USD_LOW = 0.12

#: Slippage tolerance the quote is requested at, expressed as the buffer we reserve.
#: Jupiter is asked at 50 bps; we reserve a conservative 10 bps of realised slippage
#: on top of the quoted impact.
SLIPPAGE_BUFFER_BPS = 10.0

# ---------------------------------------------------------------- token universe

#: Liquid tokens with a Solana mint that Jupiter routes and a CEX pair to reference.
#: ``binance``/``gate`` are the reference symbols; ``decimals`` is the mint's.
TOKENS: dict[str, dict[str, Any]] = {
    "SOL": {
        "mint": "So11111111111111111111111111111111111111112",
        "decimals": 9,
        "binance": "SOLUSDT",
        "gate": "SOL_USDT",
    },
    "ETH": {
        "mint": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
        "decimals": 8,
        "binance": "ETHUSDT",
        "gate": "ETH_USDT",
    },
    "WBTC": {
        "mint": "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",
        "decimals": 8,
        "binance": "BTCUSDT",
        "gate": "BTC_USDT",
    },
    "ARB": {
        "mint": "ARBzQTYDCW2KnVEjs1Mc81LekB1ibVFZKbSVmorkoT9d",
        "decimals": 8,
        "binance": "ARBUSDT",
        "gate": "ARB_USDT",
    },
    "LINK": {
        "mint": "LinkhB3afbBKb2EQQu7s7umdZceV3wcvAUJhQAfQ23L",
        "decimals": 9,
        "binance": "LINKUSDT",
        "gate": "LINK_USDT",
    },
    "AVAX": {
        "mint": "avaxGHCq3T7hoxd73oY2KY9hJSTaeMibXvHy5KNzh5D",
        "decimals": 9,
        "binance": "AVAXUSDT",
        "gate": "AVAX_USDT",
    },
}

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
USDT_DECIMALS = 6

JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
BINANCE_BOOK = "https://data-api.binance.vision/api/v3/ticker/bookTicker"
GATE_TICKERS = "https://api.gateio.ws/api/v4/spot/tickers"


# ------------------------------------------------------------------ pure math


def ui_amount(raw: int | str, decimals: int) -> float:
    """Convert a base-unit integer amount to a human float."""
    return float(raw) / (10.0**decimals)


def dex_price(in_amount_ui: float, out_amount_ui: float) -> float:
    """Executable DEX price = what you receive per unit sold.

    Because ``out_amount_ui`` is the *quoted output*, this price already includes the
    pool fee and the price impact. Never subtract them again from a spread built on
    this number.
    """
    if in_amount_ui <= 0:
        raise ValueError("in_amount_ui must be positive")
    return out_amount_ui / in_amount_ui


def gross_spread_bps(dex_px: float, cex_ref_px: float) -> float:
    """Signed DEX-vs-CEX gap in bps. Positive => DEX pays more than the CEX reference.

    ``cex_ref_px`` must be an *executable* price (the bid, if you would sell the token
    on the CEX) for the executable comparison; pass the mid only for the reference
    column.
    """
    if cex_ref_px <= 0:
        raise ValueError("cex_ref_px must be positive")
    return (dex_px / cex_ref_px - 1.0) * BPS


def executable_edge_bps(dex_px: float, cex_bid: float, cex_ask: float) -> float:
    """The largest *executable* edge across both directions, in bps.

    There are two ways to close a CEX<->DEX gap, and each pays a different CEX price:

    * **direction A** — buy the token on the CEX at the **ask**, sell it on the DEX:
      edge ``dex_px / cex_ask - 1``.
    * **direction B** — buy the token on the DEX, sell it on the CEX at the **bid**:
      edge ``cex_bid / dex_px - 1``.

    The honest single number is the **maximum** of the two, because a real trader takes
    whichever direction is profitable. Comparing the DEX price only to the bid (or only
    to the mid) understates one direction and overstates the other.
    """
    if dex_px <= 0 or cex_bid <= 0 or cex_ask <= 0:
        raise ValueError("prices must be positive")
    dir_a = (dex_px / cex_ask - 1.0) * BPS
    dir_b = (cex_bid / dex_px - 1.0) * BPS
    return max(dir_a, dir_b)


def impact_bps_from_pct(price_impact_pct: float | str | None) -> float:
    """Jupiter's ``priceImpactPct`` is a *fraction* (0.0001 == 0.01%). Convert to bps.

    A missing field returns 0.0 and is reported as "not exposed" rather than guessed.
    """
    if price_impact_pct is None:
        return 0.0
    return float(price_impact_pct) * BPS


def gas_bps(gas_usd: float, notional_usd: float) -> float:
    """A fixed USD gas cost as bps of a given notional. Small notionals are punished."""
    if notional_usd <= 0:
        raise ValueError("notional_usd must be positive")
    return (gas_usd / notional_usd) * BPS


#: A |gap| beyond this is not a market dislocation, it is a data error (wrong mint,
#: wrong decimals, or a stale/illiquid pool). Real CEX<->DEX gaps on liquid tokens are
#: single-digit to low-double-digit bps; anything past this is discarded and counted.
SANITY_MAX_GAP_BPS = 500.0


def is_sane_gap(gap_bps: float, *, limit_bps: float = SANITY_MAX_GAP_BPS) -> bool:
    """True if ``gap_bps`` is small enough to be a real dislocation rather than a bug.

    This is the guard that catches a wrong mint (a $1.40 "LINK" against a $14 CEX
    LINK, a 9000 bps phantom) before it can be reported as an opportunity.
    """
    return abs(gap_bps) <= limit_bps


@dataclass(frozen=True, slots=True)
class CostStack:
    """The real cost of capturing a CEX<->DEX gap, split by where it is charged.

    ``embedded_*`` is already inside the quoted DEX price and must NOT be subtracted
    from a spread built on that price. ``added_*`` is charged on top and IS subtracted.
    """

    notional_usd: float
    # embedded in the DEX quote (diagnostic only — do not double count):
    dex_pool_fee_bps: float
    dex_impact_bps: float
    # added on top (these are subtracted from the gross spread):
    cex_taker_bps: float
    gas_usd: float
    transfer_usd: float
    slippage_buffer_bps: float

    @property
    def gas_bps(self) -> float:
        return gas_bps(self.gas_usd, self.notional_usd)

    @property
    def transfer_bps(self) -> float:
        return gas_bps(self.transfer_usd, self.notional_usd)

    def added_bps(self) -> float:
        """Total cost charged ON TOP of the quoted DEX price."""
        return (
            self.cex_taker_bps
            + self.gas_bps
            + self.transfer_bps
            + self.slippage_buffer_bps
        )

    def embedded_bps(self) -> float:
        """Diagnostic: cost already reflected in the quoted DEX price."""
        return self.dex_pool_fee_bps + self.dex_impact_bps


def build_cost_stack(
    notional_usd: float,
    *,
    dex_pool_fee_bps: float,
    dex_impact_bps: float,
    transfer_usd: float,
    gas_usd: float = SOLANA_GAS_USD,
    cex_taker_bps: float = CEX_TAKER_ROUND_TRIP_BPS,
    slippage_buffer_bps: float = SLIPPAGE_BUFFER_BPS,
) -> CostStack:
    """Assemble the stack for one notional. Pure; no network, no clock."""
    return CostStack(
        notional_usd=notional_usd,
        dex_pool_fee_bps=dex_pool_fee_bps,
        dex_impact_bps=dex_impact_bps,
        cex_taker_bps=cex_taker_bps,
        gas_usd=gas_usd,
        transfer_usd=transfer_usd,
        slippage_buffer_bps=slippage_buffer_bps,
    )


def net_bps(gross_bps: float, stack: CostStack) -> float:
    """Net edge after the costs charged on top of the quoted price.

    ``gross_bps`` must come from an *executable* DEX price (fee + impact already in
    it). Charging ``stack.embedded_bps()`` here would double-count.
    """
    return gross_bps - stack.added_bps()


# --------------------------------------------------------------- live plumbing


@dataclass
class Sample:
    ts: str
    token: str
    notional_usd: float
    dex_px: float
    cex_mid: float
    cex_bid: float
    cex_ask: float
    price_impact_bps: float
    route_label: str
    gross_vs_mid_bps: float
    gross_vs_bid_bps: float
    #: Max executable edge across BOTH directions (buy-CEX-ask/sell-DEX and
    #: buy-DEX/sell-CEX-bid). This is the honest headline number.
    max_edge_bps: float = 0.0


class Backoff:
    """Bounded backoff that honours ``Retry-After``; never retry-storms.

    Binance's limits are per-IP and shared with sibling workers, so this errs slow.
    """

    def __init__(self, base: float = 0.5, cap: float = 30.0) -> None:
        self.base = base
        self.cap = cap
        self._streak = 0

    def delay(self, retry_after: str | None = None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), self.cap)
            except ValueError:
                pass
        self._streak += 1
        return min(self.base * (2 ** min(self._streak, 6)), self.cap)

    def ok(self) -> None:
        self._streak = 0


class RateLimiter:
    """Async token bucket. Smooths bursts so a shared per-IP limit is not tripped.

    The measured sustainable rate for ``lite-api.jup.ag`` is ~28 requests/minute; a
    burst of 14 near-simultaneous quotes returns 429 and, with a short backoff, keeps
    returning 429 because the limit clears in ~30 s. A bucket sized to stay under the
    sustained rate (default 0.4 req/s) prevents the burst in the first place.
    """

    def __init__(self, rate_per_s: float, capacity: float | None = None) -> None:
        self.rate = rate_per_s
        self.capacity = capacity if capacity is not None else max(1.0, rate_per_s * 3)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._last) * self.rate
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) / self.rate)


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    attempts: int = 3,
    limiter: RateLimiter | None = None,
    stats: dict[str, int] | None = None,
) -> tuple[int, Any | None]:
    """GET with rate limiting and retry/backoff. Returns ``(status, json_or_None)``.

    Never raises. Failures are counted in ``stats`` (``429``, ``http_<code>``,
    ``net_error``) so a silently-empty run cannot be mistaken for "no opportunities".
    """
    backoff = Backoff()
    last_status = 0
    for attempt in range(attempts):
        if limiter is not None:
            await limiter.acquire()
        try:
            r = await client.get(url, params=params)
            last_status = r.status_code
            if r.status_code == 200:
                backoff.ok()
                return 200, r.json()
            if r.status_code in (429, 418, 403):
                if stats is not None:
                    key = "429" if r.status_code == 429 else f"http_{r.status_code}"
                    stats[key] = stats.get(key, 0) + 1
                if attempt < attempts - 1:
                    await asyncio.sleep(backoff.delay(r.headers.get("Retry-After")))
                    continue
                return r.status_code, None
            if stats is not None:
                stats[f"http_{r.status_code}"] = stats.get(f"http_{r.status_code}", 0) + 1
            return r.status_code, None
        except Exception:
            if stats is not None:
                stats["net_error"] = stats.get("net_error", 0) + 1
            if attempt < attempts - 1:
                await asyncio.sleep(backoff.delay())
                continue
            return 0, None
    return last_status, None


#: Jupiter lite-api tolerates ~28 req/min sustained (measured). Stay under it.
JUPITER_RATE_PER_S = 0.4


async def fetch_dex_quote(
    client: httpx.AsyncClient,
    mint: str,
    decimals: int,
    notional_usd: float,
    ref_px: float,
    *,
    limiter: RateLimiter | None = None,
    stats: dict[str, int] | None = None,
) -> dict[str, Any] | None:
    """Quote ``notional`` worth of ``mint`` -> USDC on Jupiter.

    The input amount is derived from the CEX reference so the DEX and CEX legs are the
    same size.
    """
    if ref_px <= 0:
        return None
    in_ui = notional_usd / ref_px
    amount = int(round(in_ui * (10.0**decimals)))
    if amount <= 0:
        return None
    status, payload = await get_json(
        client,
        JUP_QUOTE,
        params={
            "inputMint": mint,
            "outputMint": USDC_MINT,
            "amount": str(amount),
            "slippageBps": "50",
        },
        limiter=limiter,
        stats=stats,
    )
    if status != 200 or not isinstance(payload, dict) or "outAmount" not in payload:
        return None
    return payload


def _route_label(quote: dict[str, Any]) -> str:
    labels = [
        str(step.get("swapInfo", {}).get("label", "?"))
        for step in quote.get("routePlan", [])
    ]
    return "+".join(labels) if labels else "?"


async def fetch_cex_refs(
    client: httpx.AsyncClient,
) -> dict[str, tuple[float, float]]:
    """Return ``{binance_symbol: (bid, ask)}`` from Binance vision, gate as fallback."""
    refs: dict[str, tuple[float, float]] = {}
    symbols = sorted({t["binance"] for t in TOKENS.values()} | {"USDCUSDT"})
    status, payload = await get_json(
        client,
        BINANCE_BOOK,
        params={"symbols": json.dumps(symbols, separators=(",", ":"))},
    )
    if status == 200 and isinstance(payload, list):
        for row in payload:
            try:
                refs[row["symbol"]] = (float(row["bidPrice"]), float(row["askPrice"]))
            except (KeyError, TypeError, ValueError):
                continue
    # Gate fallback for anything Binance did not answer for.
    missing = [s for s in symbols if s not in refs]
    if missing:
        status, payload = await get_json(client, GATE_TICKERS, params={"currency_pair": "SOL_USDT"})
        if status == 200 and isinstance(payload, list):
            for row in payload:
                try:
                    pair = str(row["currency_pair"]).replace("_", "")
                    refs.setdefault(pair, (float(row["highest_bid"]), float(row["lowest_ask"])))
                except (KeyError, TypeError, ValueError):
                    continue
    return refs


async def _sample_once(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    refs: dict[str, tuple[float, float]],
    ts: str,
    notionals: list[float],
    *,
    limiter: RateLimiter | None = None,
    stats: dict[str, int] | None = None,
) -> tuple[list[Sample], list[dict[str, Any]], list[dict[str, Any]]]:
    """Take one full cross-section: every token x notional, plus the stable pair.

    Pure-ish I/O: returns ``(samples, discarded, raw)`` so the caller owns the merge.
    Each DEX call goes through ``sem`` (bounded concurrency), ``limiter`` (token bucket)
    and ``get_json`` retries.
    """
    samples: list[Sample] = []
    discarded: list[dict[str, Any]] = []
    raw: list[dict[str, Any]] = []

    async def one(
        token: str,
        meta: dict[str, Any],
        notional: float,
        refs: dict[str, tuple[float, float]],
        ts: str,
    ) -> None:
        ref = refs.get(meta["binance"])
        if not ref:
            return
        bid, ask = ref
        mid = (bid + ask) / 2.0
        async with sem:
            quote = await fetch_dex_quote(
                client, meta["mint"], meta["decimals"], notional, mid,
                limiter=limiter, stats=stats,
            )
        if not quote:
            return
        try:
            in_ui = ui_amount(quote["inAmount"], meta["decimals"])
            out_ui = ui_amount(quote["outAmount"], USDC_DECIMALS)
        except (KeyError, TypeError, ValueError):
            return
        px = dex_price(in_ui, out_ui)
        impact = impact_bps_from_pct(quote.get("priceImpactPct"))
        g_mid = gross_spread_bps(px, mid)
        g_bid = gross_spread_bps(px, bid)
        if not (is_sane_gap(g_mid) and is_sane_gap(g_bid)):
            discarded.append(
                {"ts": ts, "token": token, "notional_usd": notional,
                 "gap_vs_mid_bps": round(g_mid, 2), "reason": "implausible_gap"}
            )
            return
        samples.append(
            Sample(
                ts=ts,
                token=token,
                notional_usd=notional,
                dex_px=px,
                cex_mid=mid,
                cex_bid=bid,
                cex_ask=ask,
                price_impact_bps=impact,
                route_label=_route_label(quote),
                gross_vs_mid_bps=g_mid,
                gross_vs_bid_bps=g_bid,
                max_edge_bps=executable_edge_bps(px, bid, ask),
            )
        )
        raw.append(
            {
                "ts": ts,
                "token": token,
                "notional_usd": notional,
                "cex": {"bid": bid, "ask": ask, "mid": mid},
                "jupiter": quote,
            }
        )

    async def one_stable(notional: float) -> None:
        stable_ref = refs.get("USDCUSDT")
        if not stable_ref:
            return
        sb, sa = stable_ref
        smid = (sb + sa) / 2.0
        async with sem:
            quote = await fetch_dex_quote(
                client, USDT_MINT, USDT_DECIMALS, notional, smid,
                limiter=limiter, stats=stats,
            )
        if not quote:
            return
        try:
            in_ui = ui_amount(quote["inAmount"], USDT_DECIMALS)
            out_ui = ui_amount(quote["outAmount"], USDC_DECIMALS)
        except (KeyError, TypeError, ValueError):
            return
        px = dex_price(in_ui, out_ui)  # USDC per USDT
        g_mid = gross_spread_bps(px, smid)
        g_bid = gross_spread_bps(px, sb)
        if not (is_sane_gap(g_mid) and is_sane_gap(g_bid)):
            discarded.append(
                {"ts": ts, "token": "USDT/USDC", "notional_usd": notional,
                 "gap_vs_mid_bps": round(g_mid, 2), "reason": "implausible_gap"}
            )
            return
        samples.append(
            Sample(
                ts=ts,
                token="USDT/USDC",
                notional_usd=notional,
                dex_px=px,
                cex_mid=smid,
                cex_bid=sb,
                cex_ask=sa,
                price_impact_bps=impact_bps_from_pct(quote.get("priceImpactPct")),
                route_label=_route_label(quote),
                gross_vs_mid_bps=g_mid,
                gross_vs_bid_bps=g_bid,
                max_edge_bps=executable_edge_bps(px, sb, sa),
            )
        )
        raw.append(
            {
                "ts": ts,
                "token": "USDT/USDC",
                "notional_usd": notional,
                "cex": {"bid": sb, "ask": sa, "mid": smid},
                "jupiter": quote,
            }
        )

    jobs = [one(t, m, n, refs, ts) for t, m in TOKENS.items() for n in notionals]
    jobs += [one_stable(n) for n in notionals]
    await asyncio.gather(*jobs, return_exceptions=True)
    return samples, discarded, raw


#: Hard ceiling for a single sampling iteration. A hung socket must not eat the whole
#: window: the first run lost ~85% of its iterations to one stalled call because the
#: only protection was the per-request timeout, which a stalled body read can escape.
#: The budget is generous because the rate limiter deliberately paces ~14 quotes per
#: iteration at 0.4 req/s, i.e. ~35 s of work before any latency is added.
ITERATION_TIMEOUT_S = 120.0


async def collect(
    minutes: float, interval: float, notionals: list[float]
) -> tuple[list[Sample], list[dict[str, Any]], dict[str, Any]]:
    """Poll DEX quotes and CEX books for a bounded window. Low concurrency."""
    samples: list[Sample] = []
    discarded: list[dict[str, Any]] = []
    raw_cache: list[dict[str, Any]] = []
    stats: dict[str, int] = {}
    iterations = 0
    timeouts = 0
    deadline = time.monotonic() + minutes * 60.0
    sem = asyncio.Semaphore(4)
    limiter = RateLimiter(JUPITER_RATE_PER_S)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(15.0, connect=5.0),
        headers={"User-Agent": "crypto-brain-research/1.0"},
    ) as client:
        refs0 = await fetch_cex_refs(client)
        while time.monotonic() < deadline:
            refs = await fetch_cex_refs(client) or refs0
            ts = datetime.now(UTC).isoformat()
            try:
                s, d, r = await asyncio.wait_for(
                    _sample_once(
                        client, sem, refs, ts, notionals,
                        limiter=limiter, stats=stats,
                    ),
                    timeout=ITERATION_TIMEOUT_S,
                )
                samples.extend(s)
                discarded.extend(d)
                raw_cache.extend(r)
                iterations += 1
            except TimeoutError:
                timeouts += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(interval, remaining))

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    meta = {"iterations": iterations, "timeouts": timeouts, "failures": stats}
    (EVIDENCE / f"raw_quotes_{stamp}.json").write_text(json.dumps(raw_cache, indent=1))
    (EVIDENCE / f"discarded_{stamp}.json").write_text(json.dumps(discarded, indent=1))
    (EVIDENCE / f"collect_meta_{stamp}.json").write_text(json.dumps(meta, indent=1))
    print(f"iterations={iterations} timeouts={timeouts} failures={stats}")
    return samples, discarded, meta


# ------------------------------------------------------------------- reporting


def samples_from_raw(raw: list[dict[str, Any]]) -> list[Sample]:
    """Rebuild ``Sample`` rows from a cached ``raw_quotes_*.json`` payload.

    Lets a finished window be re-scored offline (with a revised cost stack) without
    touching the network again, and lets several windows be pooled.
    """
    out: list[Sample] = []
    for entry in raw:
        try:
            token = str(entry["token"])
            quote = entry["jupiter"]
            cex = entry["cex"]
            bid, ask = float(cex["bid"]), float(cex["ask"])
            mid = float(cex.get("mid", (bid + ask) / 2.0))
            notional = float(entry["notional_usd"])
            if token == "USDT/USDC":
                in_dec, out_dec = USDT_DECIMALS, USDC_DECIMALS
            else:
                in_dec = int(TOKENS[token]["decimals"])
                out_dec = USDC_DECIMALS
            in_ui = ui_amount(quote["inAmount"], in_dec)
            out_ui = ui_amount(quote["outAmount"], out_dec)
            px = dex_price(in_ui, out_ui)
            out.append(
                Sample(
                    ts=str(entry["ts"]),
                    token=token,
                    notional_usd=notional,
                    dex_px=px,
                    cex_mid=mid,
                    cex_bid=bid,
                    cex_ask=ask,
                    price_impact_bps=impact_bps_from_pct(quote.get("priceImpactPct")),
                    route_label=_route_label(quote),
                    gross_vs_mid_bps=gross_spread_bps(px, mid),
                    gross_vs_bid_bps=gross_spread_bps(px, bid),
                    max_edge_bps=executable_edge_bps(px, bid, ask),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def summarize_token(samples: list[Sample]) -> dict[str, Any]:
    """Per-token gap distribution, split by notional."""
    out: dict[str, Any] = {}
    tokens = sorted({s.token for s in samples})
    for token in tokens:
        for notional in sorted({s.notional_usd for s in samples if s.token == token}):
            rows = [s for s in samples if s.token == token and s.notional_usd == notional]
            if not rows:
                continue
            mid_gaps = [r.gross_vs_mid_bps for r in rows]
            bid_gaps = [r.gross_vs_bid_bps for r in rows]
            impacts = [r.price_impact_bps for r in rows]
            out[f"{token}@{int(notional)}"] = {
                "n": len(rows),
                "gross_vs_mid_bps": {
                    "min": round(min(mid_gaps), 3),
                    "median": round(st.median(mid_gaps), 3),
                    "max": round(max(mid_gaps), 3),
                },
                "gross_vs_bid_bps": {
                    "min": round(min(bid_gaps), 3),
                    "median": round(st.median(bid_gaps), 3),
                    "max": round(max(bid_gaps), 3),
                },
                "impact_bps_median": round(st.median(impacts), 4),
                "routes": sorted({r.route_label for r in rows}),
            }
    return out


def build_verdict(samples: list[Sample]) -> dict[str, Any]:
    """Largest observed executable gap vs the cost stack at each notional.

    "Largest" means largest **magnitude**, because either sign is an arbitrage
    direction: a positive gap means the DEX pays more (sell on DEX, buy on CEX), a
    negative gap means the CEX bid is richer (buy on DEX, sell on CEX). A big negative
    gap is just as tradeable as a big positive one, so the verdict ranks by ``abs``.
    """
    verdict: dict[str, Any] = {}
    for notional in sorted({s.notional_usd for s in samples}):
        rows = [s for s in samples if s.notional_usd == notional]
        if not rows:
            continue
        widest = max(rows, key=lambda r: r.max_edge_bps)
        stack = build_cost_stack(
            notional,
            dex_pool_fee_bps=0.0,  # embedded in quote; diagnostic only
            dex_impact_bps=st.median([r.price_impact_bps for r in rows]),
            transfer_usd=CEX_WITHDRAWAL_USD,
        )
        stack_no_transfer = build_cost_stack(
            notional,
            dex_pool_fee_bps=0.0,
            dex_impact_bps=0.0,
            transfer_usd=0.0,
        )
        # Binance does not expose per-network withdrawal fees without an account, so
        # the transfer cost is a labelled range: ~0.001 SOL (market median) to ~0.01 SOL
        # (Binance's long-standing historical figure). The verdict is bounded by both.
        stack_low_transfer = build_cost_stack(
            notional,
            dex_pool_fee_bps=0.0,
            dex_impact_bps=0.0,
            transfer_usd=TRANSFER_USD_LOW,
        )
        widest_abs = widest.max_edge_bps
        verdict[f"notional_{int(notional)}"] = {
            "largest_gap_bps": round(widest.gross_vs_bid_bps, 3),
            "largest_gap_abs_bps": round(abs(widest.gross_vs_bid_bps), 3),
            "largest_max_executable_edge_bps": round(widest_abs, 3),
            "largest_gap_token": widest.token,
            "cost_stack_added_bps": round(stack.added_bps(), 3),
            "cost_stack_components": {
                "cex_taker_round_trip_bps": CEX_TAKER_ROUND_TRIP_BPS,
                "gas_bps": round(stack.gas_bps, 4),
                "transfer_bps": round(stack.transfer_bps, 4),
                "slippage_buffer_bps": SLIPPAGE_BUFFER_BPS,
            },
            "net_of_largest_gap_bps": round(widest_abs - stack.added_bps(), 3),
            "cost_stack_if_inventory_both_sides_bps": round(
                stack_no_transfer.added_bps(), 3
            ),
            "net_if_inventory_both_sides_bps": round(
                widest_abs - stack_no_transfer.added_bps(), 3
            ),
            "net_with_low_transfer_estimate_bps": round(
                widest_abs - stack_low_transfer.added_bps(), 3
            ),
            "gap_exceeds_costs": widest_abs > stack.added_bps(),
            "gap_exceeds_costs_with_low_transfer": (
                widest_abs > stack_low_transfer.added_bps()
            ),
            "gap_exceeds_costs_even_with_inventory": (
                widest_abs > stack_no_transfer.added_bps()
            ),
        }
    return verdict


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--minutes", type=float, default=12.0)
    ap.add_argument("--interval", type=float, default=8.0)
    ap.add_argument("--notional", type=float, nargs="+", default=[100.0, 1000.0])
    ap.add_argument("--probe-only", action="store_true")
    ap.add_argument(
        "--reanalyze",
        action="store_true",
        help="Re-score the newest cached raw_quotes_*.json offline (no network).",
    )
    args = ap.parse_args()

    EVIDENCE.mkdir(parents=True, exist_ok=True)

    if args.reanalyze:
        raws = sorted(EVIDENCE.glob("raw_quotes_*.json"))
        if not raws:
            print("no cached raw_quotes_*.json to reanalyze")
            return 1
        pooled: list[Sample] = []
        for path in raws:
            pooled.extend(samples_from_raw(json.loads(path.read_text())))
        print(f"reanalyzed {len(raws)} file(s) -> {len(pooled)} samples")
        report = {
            "generated_at": datetime.now(UTC).isoformat(),
            "mode": "reanalyze",
            "source_files": [p.name for p in raws],
            "cost_constants": {
                "cex_taker_round_trip_bps": CEX_TAKER_ROUND_TRIP_BPS,
                "solana_gas_usd": SOLANA_GAS_USD,
                "evm_gas_usd_range": list(EVM_GAS_USD_RANGE),
                "cex_withdrawal_usd": CEX_WITHDRAWAL_USD,
                "transfer_usd_low": TRANSFER_USD_LOW,
                "slippage_buffer_bps": SLIPPAGE_BUFFER_BPS,
            },
            "distribution": summarize_token(pooled),
            "verdict": build_verdict(pooled),
        }
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        (EVIDENCE / f"dex_reanalysis_{stamp}.json").write_text(json.dumps(report, indent=1))
        print(json.dumps(report["verdict"], indent=1))
        return 0

    if args.probe_only:
        async def probe() -> None:
            async with httpx.AsyncClient(timeout=15.0) as client:
                refs = await fetch_cex_refs(client)
                print(f"cex_refs: {len(refs)} symbols -> {sorted(refs)[:6]}")
                for token, meta in list(TOKENS.items())[:2]:
                    ref = refs.get(meta["binance"])
                    if not ref:
                        print(f"{token}: no cex ref")
                        continue
                    mid = (ref[0] + ref[1]) / 2
                    q = await fetch_dex_quote(client, meta["mint"], meta["decimals"], 100.0, mid)
                    print(f"{token}: {'OK' if q else 'NO ROUTE'}")
        asyncio.run(probe())
        return 0

    print(f"collecting {args.minutes:.0f} min @ {args.interval:.0f}s ...")
    samples, discarded, meta = asyncio.run(
        collect(args.minutes, args.interval, args.notional)
    )
    print(f"{len(samples)} samples, {len(discarded)} discarded as implausible")
    if not samples:
        print(
            "WARNING: zero samples collected. This is a collection failure, NOT "
            f"evidence of no opportunities. failures={meta.get('failures')}"
        )

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "window_minutes": args.minutes,
        "interval_s": args.interval,
        "notionals_usd": args.notional,
        "discarded_implausible": len(discarded),
        "collection": meta,
        "cost_constants": {
            "cex_taker_round_trip_bps": CEX_TAKER_ROUND_TRIP_BPS,
            "solana_gas_usd": SOLANA_GAS_USD,
            "evm_gas_usd_range": list(EVM_GAS_USD_RANGE),
            "cex_withdrawal_usd": CEX_WITHDRAWAL_USD,
            "slippage_buffer_bps": SLIPPAGE_BUFFER_BPS,
        },
        "distribution": summarize_token(samples),
        "verdict": build_verdict(samples),
    }
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (EVIDENCE / f"dex_scan_{stamp}.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report["verdict"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
