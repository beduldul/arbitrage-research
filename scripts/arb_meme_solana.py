"""Meme-coin cross-venue arbitrage measurement — Solana (primary) + EVM (coverage).

**Measurement only.** Public endpoints, no keys, no wallets, no orders, no account
state, no transactions, and none may be added. Raw responses are cached under
``evidence/arbitrage/2026-10-03/meme/{solana,evm}/``.

The question the earlier programme could not answer: **on a chain where gas is ~$0.001
and there is no CEX taker fee, do meme-pool cross-DEX spreads exceed the real cost
stack at a $100 notional?**

The cost structure is genuinely different from CEX/triangle/funding:

* There is **no CEX taker fee and no withdrawal fee** in an on-chain round trip. The
  binding costs are the **pool fees and price impact**, which are already inside the
  quoted ``outAmount`` of each leg, plus **gas, priority fee and a Jito tip**.
* Therefore the honest net is ``net_bps = gross_round_trip_bps - added_cost_bps``,
  where ``gross`` is built only from *executable* quote outputs (buy leg then sell leg)
  and ``added`` is gas + priority + Jito tip + a labelled slippage buffer. Pool fees
  and impact are **never subtracted again** — they are inside ``gross``.

How the executable cross-venue quotes are obtained (no mid prices anywhere):

* Jupiter's ``lite-api.jup.ag/swap/v1/quote`` accepts a ``dexes=`` constraint. Quoting
  ``USDC -> T`` with ``dexes=Raydium`` returns the executable single-venue route through
  Raydium only; ``T -> USDC`` with ``dexes=Meteora DLMM`` returns the executable
  single-venue sell. Pairing buy-venue A with sell-venue B gives the real round trip.
  (This is *not* a mid-price comparison: every number is an ``outAmount`` you would
  actually receive, at the full $100 size, with that venue's pool fee and impact.)
* On EVM chains the equivalent is a direct ``getAmountsOut`` ``eth_call`` against each
  router's public RPC (Uniswap V2 vs Sushi V2 on Ethereum; Pancake V2 vs Biswap V2 on
  BSC; Uniswap V2 vs Sushi V2 on Base).

Every cost that is an *estimate* is labelled with its source. Every cost that is
*measured* is measured live (priority fee from the Solana RPC, Jito tip from the public
Jito tip-floor endpoint, gas price from each EVM RPC).

Run::

    uv run python scripts/arb_meme_solana.py --probe-only
    uv run python scripts/arb_meme_solana.py --minutes 25 --rate 1.4
    uv run python scripts/arb_meme_solana.py --reanalyze
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics as st
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[1]
EVIDENCE = REPO / "evidence" / "arbitrage" / "2026-10-03" / "meme"
SOLANA_DIR = EVIDENCE / "solana"
EVM_DIR = EVIDENCE / "evm"

#: Basis points per unit fraction. ``1.0 -> 10_000 bps``.
BPS = 10_000.0

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6
SOL_MINT = "So11111111111111111111111111111111111111112"
SOL_DECIMALS = 9

# --------------------------------------------------------------- cost constants
#
# Split deliberately into MEASURED (fetched live each run) and ESTIMATED (cited
# standard ranges). The report labels each. Nothing here is a CEX fee: there is none
# in an on-chain round trip.

#: Base Solana transaction fee: one signature, 5,000 lamports. Cited protocol constant
#: (Solana fee schedule). This is the whole "gas" cost of a meme swap.
SOLANA_BASE_TX_FEE_LAMPORTS = 5_000
#: Compute-unit budget assumed for a two-hop swap transaction. A Jupiter swap consumes
#: roughly 150k-600k CU; 300k is a mid labelled estimate used to convert the measured
#: *per-CU* priority fee into a total.
SOLANA_CU_LIMIT = 300_000

#: Slippage buffer reserved on top of the quoted output. The quote is requested at 50
#: bps tolerance; the quoted ``outAmount`` is the expected landing point, and this is a
#: labelled reserve for realised slippage. Pure assumption, not measured.
SLIPPAGE_BUFFER_BPS = 10.0

#: Jito tip percentile used as the base case (you pay to be included) and the
#: competitive case (you pay to win the bundle auction against other searchers).
JITO_BASE_PERCENTILE = "landed_tips_75th_percentile"
JITO_COMPETITIVE_PERCENTILE = "landed_tips_95th_percentile"

#: Gas assumed for one EVM swap, labelled estimate. V2 router swaps land ~100k-200k.
EVM_SWAP_GAS = 150_000

#: A round-trip quote beyond this magnitude is a data error (wrong mint, wrong
#: decimals, dead pool), not a dislocation. Real cross-DEX meme spreads are <30%.
SANITY_MAX_ROUND_TRIP_BPS = 3_000.0

# ------------------------------------------------------------------- universes
#
# Tokens are the highest-liquidity Solana memes (verified mints, decimals confirmed via
# Jupiter token search; liquidity $3M-$30M). Mid-caps (SOL, WETH-equivalents) are
# deliberately excluded: the prior programme already covered them and they do not carry
# the meme spread this study is testing.

MEME_TOKENS: dict[str, dict[str, Any]] = {
    "BONK": {"mint": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "decimals": 5},
    "WIF": {"mint": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm", "decimals": 6},
    "POPCAT": {"mint": "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr", "decimals": 9},
    "FARTCOIN": {"mint": "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump", "decimals": 6},
    "TRUMP": {"mint": "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN", "decimals": 6},
    "PENGU": {"mint": "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv", "decimals": 6},
    "MEW": {"mint": "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5", "decimals": 5},
}

#: Venue labels as Jupiter reports them (from ``program-id-to-label``). Each is quoted
#: via ``dexes=<label>``; labels that route for too few tokens are dropped at runtime.
CANDIDATE_VENUES: tuple[str, ...] = (
    "Raydium",
    "Raydium CLMM",
    "Whirlpool",  # Orca
    "Meteora",
    "Meteora DLMM",
    "Meteora DAMM v2",
    "Bonkswap",
    "Flux",
    "Scorch",
    "GooseFX GAMMA",
    "Pump.fun Amm",
)
#: A venue must route for at least this share of tokens to enter the matrix; a venue
#: that only ever serves one token would bias the pair statistics.
MIN_VENUE_COVERAGE = 0.5

#: EVM chains that answered a public ``eth_call``. Each entry quotes the token leg in
#: the wrapped native asset (the standard quote asset for these pairs), so a buy leg and
#: a sell leg are directly comparable.
EVM_CHAINS: dict[str, dict[str, Any]] = {
    "ethereum": {
        "rpc": "https://ethereum-rpc.publicnode.com",
        "native": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        "routers": {
            "uniswap_v2": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
            "sushi_v2": "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F",
        },
        "tokens": {
            "PEPE": ("0x6982508145454Ce325dDbE47a25d4ec3d2311933", 18),
            "SHIB": ("0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE", 18),
        },
    },
    "bsc": {
        "rpc": "https://bsc-rpc.publicnode.com",
        "native": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
        "routers": {
            "pancake_v2": "0x10ED43C718714eb63d5aA57B78B54704E256024E",
            "biswap_v2": "0x3a6d8cA21D1CF76F653A67577FA0D27453350dD8",
        },
        "tokens": {
            "SHIB": ("0x2859e4544C4bB03966803b044A93563Bd2D0DD4D", 18),
            "BABYDOGE": ("0xc748673057861a797275CD8A068AbB95A902e8de", 9),
            "CAKE": ("0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82", 18),
        },
    },
    "base": {
        "rpc": "https://base-rpc.publicnode.com",
        "native": "0x4200000000000000000000000000000000000006",
        "routers": {
            "uniswap_v2": "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",
            "sushi_v2": "0x6BDED42c6DA8FBf0d2bA55B2fa120C5e0c8D7891",
        },
        "tokens": {
            "DEGEN": ("0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed", 18),
        },
    },
}

# --------------------------------------------------------------------- endpoints

JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
JUP_PRICE = "https://lite-api.jup.ag/price/v3"
JUP_LABELS = "https://lite-api.jup.ag/swap/v1/program-id-to-label"
JUP_TOKEN_SEARCH = "https://lite-api.jup.ag/tokens/v2/search"
JITO_TIP_FLOOR = "https://bundles.jito.wtf/api/v1/bundles/tip_floor"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
DEXSCREENER_TOKENS = "https://api.dexscreener.com/latest/dex/tokens"

#: Jupiter v6 program id, used as the writable account when sampling priority fees so
#: the distribution reflects swaps, not an idle account.
JUPITER_PROGRAM = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"

#: GET endpoints probed for reachability. ``blocked`` entries are the ones that failed
#: and are reported as such rather than silently dropped.
PROBE_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("jupiter_quote", f"{JUP_QUOTE}?inputMint={SOL_MINT}&outputMint={USDC_MINT}"
                      "&amount=100000000&slippageBps=50"),
    ("jupiter_quote_dexes_raydium",
     f"{JUP_QUOTE}?inputMint={SOL_MINT}&outputMint={USDC_MINT}"
     "&amount=100000000&slippageBps=50&dexes=Raydium"),
    ("jupiter_price", f"{JUP_PRICE}?ids={SOL_MINT}"),
    ("jupiter_labels", JUP_LABELS),
    ("raydium_pools_list",
     "https://api-v3.raydium.io/pools/info/list?poolType=all&poolSortField=volume24h"
     "&sortType=desc&pageSize=10&page=1"),
    ("orca_pools", "https://api.orca.so/v2/solana/pools?size=5"),
    ("geckoterminal_pools", "https://api.geckoterminal.com/api/v2/networks/solana/pools?page=1"),
    ("dexscreener_search", "https://api.dexscreener.com/latest/dex/search?q=SOL/USDC"),
    ("jito_tip_floor", JITO_TIP_FLOOR),
    ("meteora_pair_all", "https://dlmm-api.meteora.ag/pair/all"),
    ("pumpfun_frontend",
     "https://frontend-api.pump.fun/coins/So11111111111111111111111111111111111111112"),
    ("birdeye_price", f"https://public-api.birdeye.so/defi/price?address={SOL_MINT}"),
)


# ------------------------------------------------------------------ pure math


class QuoteError(ValueError):
    """A quote payload that cannot be used as an executable price."""


def ui_amount(raw: int | str, decimals: int) -> float:
    """Convert a base-unit integer amount to a human float."""
    return float(raw) / (10.0**decimals)


def round_trip_gross_bps(usdc_in_ui: float, usdc_out_ui: float) -> float:
    """Gross round-trip edge in bps: what you get back per unit put in, minus one.

    Both numbers are *executable quote outputs*, so the pool fees and price impact of
    both legs are already inside this figure. It must never be reduced by a pool fee
    again.
    """
    if usdc_in_ui <= 0:
        raise ValueError("usdc_in_ui must be positive")
    return (usdc_out_ui / usdc_in_ui - 1.0) * BPS


def scale_leg2_output(q1_raw: int, q1_ref_raw: int, q2_ref_raw: int) -> int:
    """Leg-2 output when you actually hold ``q1_raw`` tokens, from a reference quote.

    The sell leg is quoted once, at the amount produced by the best buy venue
    (``q1_ref_raw``). A buy venue that produced fewer tokens would sell fewer tokens,
    which can only ever be *proportionally better* (less impact), so scaling linearly is
    conservative and never invents an edge. Exact when ``q1_raw == q1_ref_raw``.
    """
    if q1_ref_raw <= 0:
        raise ValueError("q1_ref_raw must be positive")
    return int(q2_ref_raw * q1_raw / q1_ref_raw)


def is_sane_round_trip(gross_bps: float, *, limit_bps: float = SANITY_MAX_ROUND_TRIP_BPS) -> bool:
    """True if a round-trip edge is small enough to be real rather than a data bug."""
    return abs(gross_bps) <= limit_bps


def net_after_pool_fees(
    gross_bps: float,
    *,
    pool_fee_bps_per_leg: float | list[float] | tuple[float, ...],
    n_legs: int = 2,
    impact_bps: float = 0.0,
    other_added_bps: float = 0.0,
) -> float:
    """Net edge when the gross spread comes from NON-executable pool prices.

    The executable path (Jupiter ``outAmount``, EVM ``getAmountsOut``) already contains
    each pool's fee and the price impact, so those must NOT be charged again — that path
    goes through :func:`net_bps`. This helper exists for the *other* case, where a spread
    is read from pool *prices* (e.g. DexScreener mid-ish pool prices) that do not include
    the fee or impact, and each leg's cost must be charged explicitly.

    ``pool_fee_bps_per_leg`` may be a single fee applied to every leg, or an explicit
    per-leg list (e.g. a 0.25% Raydium leg against a 0.60% Meteora leg).
    """
    if isinstance(pool_fee_bps_per_leg, (list, tuple)):
        fees = sum(float(f) for f in pool_fee_bps_per_leg)
    else:
        if n_legs < 0:
            raise ValueError("n_legs must be non-negative")
        fees = n_legs * float(pool_fee_bps_per_leg)
    return gross_bps - fees - impact_bps - other_added_bps


def require_executable_quote(payload: Any) -> tuple[int, int, float]:
    """Return ``(in_raw, out_raw, impact_bps)`` from a quote, or raise.

    **Bid/ask-never-mid rule.** There is no mid-price fallback. A payload without the
    actual ``inAmount``/``outAmount`` you would receive is a data error, not a price, and
    is rejected rather than approximated.
    """
    if not isinstance(payload, dict):
        raise QuoteError("quote payload is not an object")
    for key in ("inAmount", "outAmount"):
        if key not in payload:
            raise QuoteError(f"quote missing {key}; refusing a mid-price fallback")
    try:
        in_raw = int(payload["inAmount"])
        out_raw = int(payload["outAmount"])
    except (TypeError, ValueError) as exc:
        raise QuoteError(f"quote amounts are not integers: {exc}") from exc
    if in_raw <= 0 or out_raw <= 0:
        raise QuoteError("quote amounts must be positive")
    return in_raw, out_raw, impact_bps_from_pct(payload.get("priceImpactPct"))


def impact_bps_from_pct(price_impact_pct: float | str | None) -> float:
    """Jupiter's ``priceImpactPct`` is a fraction (0.0001 == 0.01%). Convert to bps.

    A missing field returns 0.0 and is reported as "not exposed" rather than guessed.
    """
    if price_impact_pct is None:
        return 0.0
    try:
        return float(price_impact_pct) * BPS
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True, slots=True)
class ChainCosts:
    """Costs charged *on top of* the quoted executable prices, in USD.

    ``gas_usd`` and ``priority_usd`` and ``jito_tip_usd`` are each measured live where
    possible (their ``*_source`` fields say so); ``slippage_buffer_bps`` is a labelled
    assumption. Pool fees and impact are **not** here — they are inside the quotes.
    """

    gas_usd: float
    priority_usd: float
    jito_tip_usd: float
    slippage_buffer_bps: float = SLIPPAGE_BUFFER_BPS
    gas_source: str = "protocol constant"
    priority_source: str = "measured"
    jito_source: str = "measured"

    def added_usd(self) -> float:
        return self.gas_usd + self.priority_usd + self.jito_tip_usd

    def added_bps(self, notional_usd: float) -> float:
        """All on-top costs as bps of the notional, plus the labelled slippage buffer."""
        if notional_usd <= 0:
            raise ValueError("notional_usd must be positive")
        return self.added_usd() / notional_usd * BPS + self.slippage_buffer_bps


def net_bps(gross_bps: float, costs: ChainCosts, notional_usd: float) -> float:
    """Net edge after the costs charged on top of the executable quotes.

    ``gross_bps`` must come from a round trip built on quote outputs, so pool fees and
    impact are already inside it and are charged exactly once.
    """
    return gross_bps - costs.added_bps(notional_usd)


def plan_budget(
    n_tokens: int,
    n_venues: int,
    rate_per_s: float,
    window_s: float,
    *,
    extra_per_token: int = 1,
) -> dict[str, Any]:
    """Pure feasibility plan for a sampling window, with no sleeping.

    One token cycle needs one buy quote per venue plus one sell quote per venue (the
    sell leg is quoted once, at the best buy amount), plus ``extra_per_token`` reference
    calls. Returns the achievable cycle count and confirms the plan stays under the
    sustained rate — the bug that once produced silent 429s and a zero-sample run.
    """
    if n_tokens <= 0 or n_venues <= 0 or rate_per_s <= 0:
        raise ValueError("n_tokens, n_venues and rate_per_s must be positive")
    calls_per_cycle = n_tokens * (2 * n_venues + extra_per_token)
    cycle_s = calls_per_cycle / rate_per_s
    cycles = int(window_s / cycle_s) if cycle_s > 0 else 0
    return {
        "calls_per_cycle": calls_per_cycle,
        "cycle_seconds": round(cycle_s, 2),
        "cycles_in_window": cycles,
        "total_calls": calls_per_cycle * cycles,
        "within_rate": calls_per_cycle * cycles <= rate_per_s * window_s,
    }


# --------------------------------------------------------------- rate limiting


class RateLimiter:
    """Async token bucket with an injectable clock and sleep, so it is testable.

    The bucket is sized to the *sustained* rate; a burst that exceeds it is what
    produced the earlier silent-429 run. Injecting ``clock``/``sleep`` lets the unit
    tests exercise the pacing without ever sleeping.
    """

    def __init__(
        self,
        rate_per_s: float,
        capacity: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_s <= 0:
            raise ValueError("rate_per_s must be positive")
        self.rate = rate_per_s
        self.capacity = capacity if capacity is not None else max(1.0, rate_per_s * 2.0)
        self._tokens = self.capacity
        self._last = clock()
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        #: Circuit breaker: a 429 from the server pauses *all* workers until this clock
        #: value. Without it, a short burst that trips the per-minute quota keeps every
        #: in-flight call failing for the rest of the minute and the window yields zero
        #: usable samples — the exact failure the earlier programme hit.
        self._cooldown_until = 0.0
        self.penalties = 0

    def penalize(self, seconds: float) -> None:
        """Pause every worker for ``seconds`` after a 429/418."""
        if seconds <= 0:
            return
        self.penalties += 1
        self._cooldown_until = max(self._cooldown_until, self._clock() + seconds)

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                if now < self._cooldown_until:
                    await self._sleep(self._cooldown_until - now)
                    continue
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await self._sleep((1.0 - self._tokens) / self.rate)


class Backoff:
    """Bounded backoff that honours ``Retry-After``; never retry-storms."""

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


def _retry_after_seconds(header: str | None) -> float:
    """Parse a ``Retry-After`` header to seconds, defaulting to a 60 s quota cooldown.

    Jupiter's lite tier enforces a per-minute quota, so when the header is absent the
    right penalty is a full minute — a shorter pause just burns another burst.
    """
    if header:
        try:
            return min(max(float(header), 0.0), 120.0)
        except ValueError:
            pass
    return 60.0


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    limiter: RateLimiter | None = None,
    stats: dict[str, int] | None = None,
    attempts: int = 3,
) -> tuple[int, Any | None]:
    """GET with rate limiting and retry. Returns ``(status, json_or_None)``; never raises."""
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
            if stats is not None:
                stats[f"http_{r.status_code}"] = stats.get(f"http_{r.status_code}", 0) + 1
            if r.status_code in (429, 418):
                if limiter is not None:
                    limiter.penalize(_retry_after_seconds(r.headers.get("Retry-After")))
            if r.status_code in (429, 418, 403, 503) and attempt < attempts - 1:
                await asyncio.sleep(backoff.delay(r.headers.get("Retry-After")))
                continue
            return r.status_code, None
        except Exception:
            if stats is not None:
                stats["net_error"] = stats.get("net_error", 0) + 1
            if attempt < attempts - 1:
                await asyncio.sleep(backoff.delay())
                continue
            return 0, None
    return last_status, None


async def post_json(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    *,
    stats: dict[str, int] | None = None,
) -> tuple[int, Any | None]:
    """POST JSON-RPC with a single retry. Never raises."""
    for attempt in range(2):
        try:
            r = await client.post(url, json=payload)
            if r.status_code == 200:
                return 200, r.json()
            if stats is not None:
                stats[f"rpc_{r.status_code}"] = stats.get(f"rpc_{r.status_code}", 0) + 1
            if attempt == 0:
                await asyncio.sleep(0.5)
                continue
            return r.status_code, None
        except Exception:
            if stats is not None:
                stats["rpc_net_error"] = stats.get("rpc_net_error", 0) + 1
            if attempt == 0:
                await asyncio.sleep(0.5)
                continue
            return 0, None
    return 0, None


# ------------------------------------------------------------------ Jupiter


def route_label(payload: dict[str, Any]) -> str:
    labels = [
        str(step.get("swapInfo", {}).get("label", "?")) for step in payload.get("routePlan", [])
    ]
    return "+".join(labels) if labels else "?"


async def jup_quote(
    client: httpx.AsyncClient,
    input_mint: str,
    output_mint: str,
    amount_raw: int,
    *,
    dexes: str | None = None,
    limiter: RateLimiter | None = None,
    stats: dict[str, int] | None = None,
    slippage_bps: int = 50,
) -> dict[str, Any] | None:
    """One Jupiter quote, optionally constrained to a single venue via ``dexes=``."""
    if amount_raw <= 0:
        return None
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_raw),
        "slippageBps": str(slippage_bps),
    }
    if dexes:
        params["dexes"] = dexes
    status, payload = await get_json(
        client, JUP_QUOTE, params=params, limiter=limiter, stats=stats
    )
    if status != 200 or not isinstance(payload, dict):
        return None
    return payload


async def fetch_sol_price(
    client: httpx.AsyncClient, stats: dict[str, int] | None = None
) -> float:
    """SOL/USD from Jupiter price v3 (used only to convert lamport costs to USD)."""
    status, payload = await get_json(client, JUP_PRICE, params={"ids": SOL_MINT}, stats=stats)
    if status == 200 and isinstance(payload, dict):
        entry = payload.get(SOL_MINT)
        if isinstance(entry, dict) and entry.get("usdPrice"):
            return float(entry["usdPrice"])
    return 0.0


async def measure_priority_fee(
    client: httpx.AsyncClient, stats: dict[str, int] | None = None
) -> dict[str, Any]:
    """MEASURED Solana priority-fee distribution, in micro-lamports per CU.

    Sampled for the Jupiter program as a writable account so the numbers reflect swap
    contention rather than an idle account. Returns the full distribution; the report
    converts a chosen percentile to USD with the CU assumption.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getRecentPrioritizationFees",
        "params": [[JUPITER_PROGRAM]],
    }
    status, data = await post_json(client, SOLANA_RPC, payload, stats=stats)
    out: dict[str, Any] = {"source": SOLANA_RPC, "cu_limit": SOLANA_CU_LIMIT}
    if status != 200 or not isinstance(data, dict):
        out["error"] = f"status={status}"
        return out
    fees = sorted(
        int(row.get("prioritizationFee", 0))
        for row in data.get("result", [])
        if isinstance(row, dict)
    )
    if not fees:
        out["error"] = "empty"
        return out
    n = len(fees)
    out.update(
        {
            "n": n,
            "nonzero": sum(1 for f in fees if f > 0),
            "p50": fees[n // 2],
            "p75": fees[int(n * 0.75)],
            "p90": fees[int(n * 0.90)],
            "p95": fees[int(n * 0.95)],
            "max": fees[-1],
        }
    )
    return out


async def measure_jito_tips(
    client: httpx.AsyncClient, stats: dict[str, int] | None = None
) -> dict[str, Any]:
    """MEASURED Jito landed-tip percentiles, in SOL (public tip-floor endpoint)."""
    status, data = await get_json(client, JITO_TIP_FLOOR, stats=stats)
    out: dict[str, Any] = {"source": JITO_TIP_FLOOR}
    if status != 200 or not isinstance(data, list) or not data:
        out["error"] = f"status={status}"
        return out
    row = data[-1]
    for key in (
        "landed_tips_25th_percentile",
        "landed_tips_50th_percentile",
        "landed_tips_75th_percentile",
        "landed_tips_95th_percentile",
        "landed_tips_99th_percentile",
    ):
        if key in row:
            out[key] = float(row[key])
    out["time"] = row.get("time")
    return out


def solana_costs(
    sol_price: float, priority: dict[str, Any], jito: dict[str, Any]
) -> ChainCosts:
    """Assemble the Solana on-top cost stack in USD from the live measurements."""
    gas_usd = (SOLANA_BASE_TX_FEE_LAMPORTS / 1e9) * sol_price if sol_price else 0.0
    fee_per_cu = float(priority.get("p75", 0) or 0)
    priority_lamports = fee_per_cu * SOLANA_CU_LIMIT / 1e6
    priority_usd = (priority_lamports / 1e9) * sol_price if sol_price else 0.0
    tip_sol = float(jito.get(JITO_BASE_PERCENTILE, 0) or 0)
    jito_usd = tip_sol * sol_price if sol_price else 0.0
    return ChainCosts(
        gas_usd=gas_usd,
        priority_usd=priority_usd,
        jito_tip_usd=jito_usd,
        gas_source=f"{SOLANA_BASE_TX_FEE_LAMPORTS} lamports @ SOL=${sol_price:.2f}",
        priority_source=(
            f"measured p75 {fee_per_cu:.0f} uLamports/CU x {SOLANA_CU_LIMIT} CU "
            f"({priority.get('source', '?')})"
        ),
        jito_source=f"measured {JITO_BASE_PERCENTILE} ({jito.get('source', '?')})",
    )


# ------------------------------------------------------------- reachability


async def probe_reachability(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Probe every candidate endpoint once and record status/latency/payload sample."""
    results: list[dict[str, Any]] = []
    for name, url in PROBE_ENDPOINTS:
        t0 = time.monotonic()
        try:
            r = await client.get(url, timeout=15.0)
            body = r.text[:400]
            results.append(
                {
                    "name": name,
                    "url": url,
                    "status": r.status_code,
                    "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                    "ok": r.status_code == 200,
                    "payload_sample": body,
                }
            )
        except Exception as exc:  # noqa: BLE001 - probe must never raise
            results.append(
                {
                    "name": name,
                    "url": url,
                    "status": 0,
                    "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                }
            )
    return results


async def discover_venues(
    client: httpx.AsyncClient,
    limiter: RateLimiter,
    stats: dict[str, int],
    tokens: dict[str, dict[str, Any]],
    *,
    max_tokens: int = 3,
    max_venues: int | None = None,
) -> list[str]:
    """Keep the candidate venues that actually route for a majority of the probed tokens.

    Only ``max_tokens`` tokens are probed: every probe spends rate budget, and the
    matrix is built from the routed venues afterwards. Venues are ranked by how many
    tokens they route and trimmed to ``max_venues`` so the per-cycle call count fits the
    sustained rate.
    """
    coverage: dict[str, int] = dict.fromkeys(CANDIDATE_VENUES, 0)
    probe_tokens = list(tokens.items())[:max_tokens]
    for name, meta in probe_tokens:
        for venue in CANDIDATE_VENUES:
            q = await jup_quote(
                client,
                USDC_MINT,
                meta["mint"],
                100 * 10**USDC_DECIMALS,
                dexes=venue,
                limiter=limiter,
                stats=stats,
            )
            if q is not None:
                try:
                    require_executable_quote(q)
                    coverage[venue] += 1
                except QuoteError:
                    pass
    threshold = max(1, int(len(probe_tokens) * MIN_VENUE_COVERAGE))
    routed = [(v, coverage[v]) for v in CANDIDATE_VENUES if coverage[v] >= threshold]
    routed.sort(key=lambda kv: -kv[1])
    out = [v for v, _ in routed]
    return out[:max_venues] if max_venues else out


# ------------------------------------------------------------------ sampling


@dataclass(slots=True)
class VenueQuote:
    """One executable quote at a named venue, at the full $100 size."""

    token: str
    venue: str
    direction: str  # "buy" (USDC -> token) or "sell" (token -> USDC)
    in_raw: int
    out_raw: int
    in_ui: float
    out_ui: float
    impact_bps: float
    route: str
    context_slot: int | None
    ts: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "venue": self.venue,
            "direction": self.direction,
            "in_raw": str(self.in_raw),
            "out_raw": str(self.out_raw),
            "in_ui": self.in_ui,
            "out_ui": self.out_ui,
            "impact_bps": round(self.impact_bps, 4),
            "route": self.route,
            "context_slot": self.context_slot,
            "ts": self.ts,
        }


async def sample_solana_cycle(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    limiter: RateLimiter,
    stats: dict[str, int],
    venues: list[str],
    tokens: dict[str, dict[str, Any]],
    notional_usd: float,
    ts: str,
) -> tuple[list[VenueQuote], dict[str, int]]:
    """One full cross-section: buy leg per venue, then a sell leg per venue.

    The sell leg is quoted at the *best* buy-leg output so that V buy quotes and V sell
    quotes give every ordered venue pair without a V^2 explosion; the linear scaling in
    ``scale_leg2_output`` then charges each buy venue its true (inferior) output.
    Returns ``(quotes, q1_ref_raw_by_token)``.
    """
    quotes: list[VenueQuote] = []
    q1_ref: dict[str, int] = {}
    usdc_in_raw = int(round(notional_usd * 10**USDC_DECIMALS))

    async def buy(token: str, meta: dict[str, Any], venue: str) -> None:
        async with sem:
            q = await jup_quote(
                client, USDC_MINT, meta["mint"], usdc_in_raw,
                dexes=venue, limiter=limiter, stats=stats,
            )
        if q is None:
            return
        try:
            in_raw, out_raw, impact = require_executable_quote(q)
        except QuoteError:
            return
        quotes.append(
            VenueQuote(
                token=token, venue=venue, direction="buy",
                in_raw=in_raw, out_raw=out_raw,
                in_ui=ui_amount(in_raw, USDC_DECIMALS),
                out_ui=ui_amount(out_raw, meta["decimals"]),
                impact_bps=impact, route=route_label(q),
                context_slot=q.get("contextSlot"), ts=ts,
            )
        )

    await asyncio.gather(
        *(buy(t, m, v) for t, m in tokens.items() for v in venues),
        return_exceptions=True,
    )
    for token, meta in tokens.items():
        outs = [q.out_raw for q in quotes if q.token == token and q.direction == "buy"]
        if outs:
            q1_ref[token] = max(outs)

    async def sell(token: str, meta: dict[str, Any], venue: str) -> None:
        amount = q1_ref.get(token)
        if not amount:
            return
        async with sem:
            q = await jup_quote(
                client, meta["mint"], USDC_MINT, amount,
                dexes=venue, limiter=limiter, stats=stats,
            )
        if q is None:
            return
        try:
            in_raw, out_raw, impact = require_executable_quote(q)
        except QuoteError:
            return
        quotes.append(
            VenueQuote(
                token=token, venue=venue, direction="sell",
                in_raw=in_raw, out_raw=out_raw,
                in_ui=ui_amount(in_raw, meta["decimals"]),
                out_ui=ui_amount(out_raw, USDC_DECIMALS),
                impact_bps=impact, route=route_label(q),
                context_slot=q.get("contextSlot"), ts=ts,
            )
        )

    await asyncio.gather(
        *(sell(t, m, v) for t, m in tokens.items() for v in venues),
        return_exceptions=True,
    )
    return quotes, q1_ref


# ------------------------------------------------------------------- EVM


def venue_quote_from_dict(row: dict[str, Any]) -> VenueQuote:
    """Rebuild a cached Solana :class:`VenueQuote` from its ``as_dict`` payload."""
    return VenueQuote(
        token=str(row["token"]),
        venue=str(row["venue"]),
        direction=str(row["direction"]),
        in_raw=int(row["in_raw"]),
        out_raw=int(row["out_raw"]),
        in_ui=float(row["in_ui"]),
        out_ui=float(row["out_ui"]),
        impact_bps=float(row["impact_bps"]),
        route=str(row["route"]),
        context_slot=row.get("context_slot"),
        ts=str(row["ts"]),
    )


def evm_quote_from_dict(row: dict[str, Any]) -> EvmQuote:
    """Rebuild a cached EVM :class:`EvmQuote` from its ``as_dict`` payload."""
    return EvmQuote(
        chain=str(row["chain"]),
        token=str(row["token"]),
        venue=str(row["venue"]),
        direction=str(row["direction"]),
        in_raw=int(row["in_raw"]),
        out_raw=int(row["out_raw"]),
        in_ui=float(row["in_ui"]),
        out_ui=float(row["out_ui"]),
        ts=str(row["ts"]),
    )


def _evm_calldata_get_amounts_out(amount_in: int, path: list[str]) -> str:
    """``getAmountsOut(uint256,address[])`` calldata (selector 0xd06ca61f)."""
    head = "0xd06ca61f" + f"{amount_in:064x}" + f"{0x40:064x}" + f"{len(path):064x}"
    return head + "".join(f"{addr[2:].lower():0>64}" for addr in path)


async def evm_call(
    client: httpx.AsyncClient,
    rpc_url: str,
    to: str,
    data: str,
    stats: dict[str, int] | None = None,
) -> str | None:
    status, payload = await post_json(
        client,
        rpc_url,
        {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": to, "data": data}, "latest"]},
        stats=stats,
    )
    if status != 200 or not isinstance(payload, dict):
        return None
    result = payload.get("result")
    return result if isinstance(result, str) and result.startswith("0x") else None


async def evm_quote(
    client: httpx.AsyncClient,
    rpc_url: str,
    router: str,
    amount_in: int,
    path: list[str],
    stats: dict[str, int] | None = None,
) -> int | None:
    """Executable ``getAmountsOut`` for a router path. ``None`` on any failure."""
    if amount_in <= 0:
        return None
    result = await evm_call(
        client, rpc_url, router, _evm_calldata_get_amounts_out(amount_in, path), stats=stats
    )
    if not result:
        return None
    raw = result[2:]
    try:
        words = [int(raw[i : i + 64], 16) for i in range(0, len(raw), 64)]
    except ValueError:
        return None
    if len(words) < 2:
        return None
    return words[-1]


async def evm_native_price(
    client: httpx.AsyncClient, native: str, stats: dict[str, int] | None = None
) -> float:
    """USD price of the wrapped native asset via DexScreener (highest-liquidity pair)."""
    status, payload = await get_json(client, f"{DEXSCREENER_TOKENS}/{native}", stats=stats)
    if status != 200 or not isinstance(payload, dict):
        return 0.0
    best, best_liq = 0.0, -1.0
    for pair in payload.get("pairs") or []:
        try:
            liq = float((pair.get("liquidity") or {}).get("usd") or 0)
            px = float(pair.get("priceUsd") or 0)
        except (TypeError, ValueError):
            continue
        if px > 0 and liq > best_liq:
            best, best_liq = px, liq
    return best


async def evm_gas_usd(
    client: httpx.AsyncClient,
    rpc_url: str,
    native_price: float,
    stats: dict[str, int] | None = None,
) -> tuple[float, dict[str, Any]]:
    """MEASURED gas cost of one swap: ``eth_gasPrice`` x assumed gas, in USD."""
    status, payload = await post_json(
        client,
        rpc_url,
        {"jsonrpc": "2.0", "id": 1, "method": "eth_gasPrice", "params": []},
        stats=stats,
    )
    meta: dict[str, Any] = {"rpc": rpc_url, "gas_assumed": EVM_SWAP_GAS}
    if status != 200 or not isinstance(payload, dict) or not payload.get("result"):
        meta["error"] = f"status={status}"
        return 0.0, meta
    wei = int(payload["result"], 16)
    meta["gas_price_wei"] = wei
    meta["gas_price_gwei"] = wei / 1e9
    usd = wei * EVM_SWAP_GAS / 1e18 * native_price if native_price else 0.0
    meta["usd"] = usd
    return usd, meta


@dataclass(slots=True)
class EvmQuote:
    chain: str
    token: str
    venue: str
    direction: str
    in_raw: int
    out_raw: int
    in_ui: float
    out_ui: float
    ts: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "chain": self.chain,
            "token": self.token,
            "venue": self.venue,
            "direction": self.direction,
            "in_raw": str(self.in_raw),
            "out_raw": str(self.out_raw),
            "in_ui": self.in_ui,
            "out_ui": self.out_ui,
            "ts": self.ts,
        }


async def sample_evm_cycle(
    client: httpx.AsyncClient,
    notional_usd: float,
    native_prices: dict[str, float],
    ts: str,
    stats: dict[str, int],
) -> list[EvmQuote]:
    """One cross-section per EVM chain: buy leg per router, then a sell leg per router."""
    out: list[EvmQuote] = []
    for chain, cfg in EVM_CHAINS.items():
        native_price = native_prices.get(chain, 0.0)
        if native_price <= 0:
            continue
        native_in = int(round(notional_usd / native_price * 10**18))
        if native_in <= 0:
            continue
        for token, (addr, dec) in cfg["tokens"].items():
            buy_outs: dict[str, int] = {}
            for venue, router in cfg["routers"].items():
                got = await evm_quote(
                    client, cfg["rpc"], router, native_in, [cfg["native"], addr], stats
                )
                if got:
                    buy_outs[venue] = got
                    out.append(
                        EvmQuote(chain, token, venue, "buy", native_in, got,
                                 native_in / 1e18, got / 10**dec, ts)
                    )
            if not buy_outs:
                continue
            ref = max(buy_outs.values())
            for venue, router in cfg["routers"].items():
                got = await evm_quote(
                    client, cfg["rpc"], router, ref, [addr, cfg["native"]], stats
                )
                if got:
                    out.append(
                        EvmQuote(chain, token, venue, "sell", ref, got,
                                 ref / 10**dec, got / 1e18, ts)
                    )
    return out


# ---------------------------------------------------------------- analysis


@dataclass(slots=True)
class PairSample:
    ts: str
    token: str
    chain: str
    buy_venue: str
    sell_venue: str
    gross_bps: float
    added_bps: float
    net_bps: float
    net_usd: float
    impact_bps: float
    buy_route: str
    sell_route: str

    @property
    def key(self) -> str:
        return f"{self.chain}:{self.token}:{self.buy_venue}->{self.sell_venue}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "token": self.token,
            "chain": self.chain,
            "buy_venue": self.buy_venue,
            "sell_venue": self.sell_venue,
            "gross_bps": round(self.gross_bps, 3),
            "added_bps": round(self.added_bps, 3),
            "net_bps": round(self.net_bps, 3),
            "net_usd": round(self.net_usd, 4),
            "impact_bps": round(self.impact_bps, 3),
            "buy_route": self.buy_route,
            "sell_route": self.sell_route,
        }


def pair_edges(
    quotes: list[VenueQuote],
    q1_ref: dict[str, int],
    costs: ChainCosts,
    notional_usd: float,
    ts: str,
    *,
    chain: str = "solana",
) -> tuple[list[PairSample], int]:
    """All ordered (buy venue, sell venue) round trips from one cross-section.

    Every number is an executable quote output: leg 1 is USDC -> token at the buy venue,
    leg 2 is token -> USDC at the sell venue, and leg 2 is scaled to the tokens leg 1
    actually produced. Returns ``(samples, discarded_count)``.
    """
    out: list[PairSample] = []
    discarded = 0
    by_token: dict[str, list[VenueQuote]] = {}
    for q in quotes:
        by_token.setdefault(q.token, []).append(q)

    for token, rows in by_token.items():
        buys = {r.venue: r for r in rows if r.direction == "buy"}
        sells = {r.venue: r for r in rows if r.direction == "sell"}
        ref = q1_ref.get(token)
        if not ref or not buys or not sells:
            continue
        for b_venue, b in buys.items():
            for s_venue, s in sells.items():
                q2_raw = scale_leg2_output(b.out_raw, ref, s.out_raw)
                usdc_out_ui = ui_amount(q2_raw, USDC_DECIMALS)
                gross = round_trip_gross_bps(notional_usd, usdc_out_ui)
                if not is_sane_round_trip(gross):
                    discarded += 1
                    continue
                added = costs.added_bps(notional_usd)
                net = gross - added
                out.append(
                    PairSample(
                        ts=ts, token=token, chain=chain,
                        buy_venue=b_venue, sell_venue=s_venue,
                        gross_bps=gross, added_bps=added, net_bps=net,
                        net_usd=net / BPS * notional_usd,
                        impact_bps=b.impact_bps + s.impact_bps,
                        buy_route=b.route, sell_route=s.route,
                    )
                )
    return out, discarded


def evm_pair_edges(
    quotes: list[EvmQuote],
    costs_by_chain: dict[str, ChainCosts],
    notional_usd: float,
    ts: str,
) -> tuple[list[PairSample], int]:
    """EVM analogue of :func:`pair_edges`, denominated in the wrapped native asset."""
    out: list[PairSample] = []
    discarded = 0
    by_key: dict[tuple[str, str], list[EvmQuote]] = {}
    for q in quotes:
        by_key.setdefault((q.chain, q.token), []).append(q)

    for (chain, token), rows in by_key.items():
        buys = {r.venue: r for r in rows if r.direction == "buy"}
        sells = {r.venue: r for r in rows if r.direction == "sell"}
        costs = costs_by_chain.get(chain)
        if not costs or not buys or not sells:
            continue
        ref = max(b.out_raw for b in buys.values())
        for b_venue, b in buys.items():
            for s_venue, s in sells.items():
                q2_raw = scale_leg2_output(b.out_raw, ref, s.out_raw)
                native_out_ui = ui_amount(q2_raw, 18)
                native_in_ui = ui_amount(b.in_raw, 18)
                gross = round_trip_gross_bps(native_in_ui, native_out_ui)
                if not is_sane_round_trip(gross):
                    discarded += 1
                    continue
                added = costs.added_bps(notional_usd)
                net = gross - added
                out.append(
                    PairSample(
                        ts=ts, token=token, chain=chain,
                        buy_venue=b_venue, sell_venue=s_venue,
                        gross_bps=gross, added_bps=added, net_bps=net,
                        net_usd=net / BPS * notional_usd, impact_bps=0.0,
                        buy_route=b_venue, sell_route=s_venue,
                    )
                )
    return out, discarded


def longest_positive_run(values: list[float]) -> int:
    """Longest consecutive run of strictly positive net values."""
    best = cur = 0
    for v in values:
        cur = cur + 1 if v > 0 else 0
        best = max(best, cur)
    return best


def summarize_pairs(samples: list[PairSample]) -> dict[str, dict[str, Any]]:
    """Per venue-pair: max/mean/p90 net bps, % positive, longest positive run."""
    by_key: dict[str, list[PairSample]] = {}
    for s in samples:
        by_key.setdefault(s.key, []).append(s)
    out: dict[str, dict[str, Any]] = {}
    for key, rows in by_key.items():
        rows.sort(key=lambda r: r.ts)
        nets = [r.net_bps for r in rows]
        pos = [n for n in nets if n > 0]
        out[key] = {
            "n": len(nets),
            "token": rows[0].token,
            "chain": rows[0].chain,
            "buy_venue": rows[0].buy_venue,
            "sell_venue": rows[0].sell_venue,
            "max_net_bps": round(max(nets), 3),
            "mean_net_bps": round(st.fmean(nets), 3),
            "p90_net_bps": round(sorted(nets)[int(len(nets) * 0.9)] if nets else 0.0, 3),
            "min_net_bps": round(min(nets), 3),
            "pct_positive": round(100.0 * len(pos) / len(nets), 2),
            "longest_positive_run": longest_positive_run(nets),
            "mean_positive_net_bps": round(st.fmean(pos), 3) if pos else 0.0,
            "mean_gross_bps": round(st.fmean([r.gross_bps for r in rows]), 3),
            "ever_positive": bool(pos),
        }
    return out


async def size_sweep(
    client: httpx.AsyncClient,
    limiter: RateLimiter,
    stats: dict[str, int],
    token: str,
    meta: dict[str, Any],
    buy_venue: str,
    sell_venue: str,
    costs: ChainCosts,
    sizes: list[float],
) -> dict[str, Any]:
    """Largest $ size at which a given venue pair still nets positive."""
    rows: list[dict[str, Any]] = []
    best_size = 0.0
    for size in sizes:
        usdc_in_raw = int(round(size * 10**USDC_DECIMALS))
        buy = await jup_quote(
            client, USDC_MINT, meta["mint"], usdc_in_raw,
            dexes=buy_venue, limiter=limiter, stats=stats,
        )
        if buy is None:
            rows.append({"size_usd": size, "error": "no buy route"})
            continue
        try:
            _, q1_raw, _ = require_executable_quote(buy)
        except QuoteError:
            rows.append({"size_usd": size, "error": "bad buy quote"})
            continue
        sell = await jup_quote(
            client, meta["mint"], USDC_MINT, q1_raw,
            dexes=sell_venue, limiter=limiter, stats=stats,
        )
        if sell is None:
            rows.append({"size_usd": size, "error": "no sell route"})
            continue
        try:
            _, q2_raw, _ = require_executable_quote(sell)
        except QuoteError:
            rows.append({"size_usd": size, "error": "bad sell quote"})
            continue
        gross = round_trip_gross_bps(size, ui_amount(q2_raw, USDC_DECIMALS))
        net = net_bps(gross, costs, size)
        rows.append(
            {"size_usd": size, "gross_bps": round(gross, 3),
             "added_bps": round(costs.added_bps(size), 3), "net_bps": round(net, 3)}
        )
        if net > 0:
            best_size = size
    return {"max_executable_size_usd": best_size, "sweep": rows}


# ------------------------------------------------------------------- report


def build_report(
    *,
    reachability: list[dict[str, Any]],
    venues: list[str],
    tokens: list[str],
    window_s: float,
    cycles: int,
    samples: list[PairSample],
    costs: ChainCosts,
    sol_price: float,
    priority: dict[str, Any],
    jito: dict[str, Any],
    evm_costs: dict[str, dict[str, Any]],
    evm_cycles: int,
    sweeps: list[dict[str, Any]],
    notional_usd: float,
    stats: dict[str, int],
) -> dict[str, Any]:
    summary = summarize_pairs(samples)
    # The net-edge table is CROSS-VENUE only: a same-venue round trip is the cost
    # calibration (it should net ~ -(2xfee + 2ximpact)) and is not a trade.
    cross_summary = {
        k: v for k, v in summary.items() if v["buy_venue"] != v["sell_venue"]
    }
    ranked = sorted(cross_summary.items(), key=lambda kv: kv[1]["max_net_bps"], reverse=True)
    ever_positive = {k: v for k, v in cross_summary.items() if v["ever_positive"]}
    top = ranked[:15]
    cycle_seconds = window_s / cycles if cycles else 0.0
    per_day_cycles = 86400.0 / cycle_seconds if cycle_seconds else 0.0

    best_usd_per_day = 0.0
    best_key = None
    for key, v in ranked:
        if not v["ever_positive"] or v["mean_positive_net_bps"] <= 0:
            continue
        usd = v["mean_positive_net_bps"] / BPS * notional_usd
        per_day = usd * per_day_cycles * (v["pct_positive"] / 100.0)
        if per_day > best_usd_per_day:
            best_usd_per_day, best_key = per_day, key

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "notional_usd": notional_usd,
        "reachability": reachability,
        "venues_used": venues,
        "tokens_covered": tokens,
        "window_seconds": round(window_s, 1),
        "solana_cycles": cycles,
        "evm_cycles": evm_cycles,
        "effective_cycle_seconds": round(cycle_seconds, 2),
        "pair_samples": len(samples),
        "fetch_failures": stats,
        "cross_venue_verdict": cross_venue_verdict(samples, notional_usd, cycle_seconds),
        "cost_stack_solana": {
            "gas_usd": round(costs.gas_usd, 6),
            "priority_usd": round(costs.priority_usd, 6),
            "jito_tip_usd": round(costs.jito_tip_usd, 6),
            "slippage_buffer_bps": costs.slippage_buffer_bps,
            "added_usd": round(costs.added_usd(), 6),
            "added_bps_at_notional": round(costs.added_bps(notional_usd), 3),
            "gas_source": costs.gas_source,
            "priority_source": costs.priority_source,
            "jito_source": costs.jito_source,
            "sol_price": round(sol_price, 3),
            "priority_distribution": priority,
            "jito_distribution": jito,
        },
        "cost_stack_evm": evm_costs,
        "verdict": {
            "venue_pairs_measured": len(summary),
            "venue_pairs_ever_net_positive": len(ever_positive),
            "venue_pairs_ever_net_positive_pct": round(
                100.0 * len(ever_positive) / len(summary), 2
            ) if summary else 0.0,
            "top_venue_pairs": {k: v for k, v in top},
            "best_pair_by_mean_positive": best_key,
            "best_usd_per_day_at_notional": round(best_usd_per_day, 4),
            "assumed_cycles_per_day": round(per_day_cycles, 1),
            "size_sweeps": sweeps,
        },
    }


def write_text_report(report: dict[str, Any], path: Path) -> None:
    """Numbers-first plain-text report."""
    v = report["verdict"]
    cs = report["cost_stack_solana"]
    lines: list[str] = []
    lines.append("MEME-COIN CROSS-VENUE ARBITRAGE — MEASUREMENT REPORT")
    lines.append(f"generated: {report['generated_at']}")
    lines.append(f"notional basis: ${report['notional_usd']:.0f} (paper, measurement only)")
    lines.append("")
    lines.append("REACHABILITY")
    for r in report["reachability"]:
        state = "OK " if r.get("ok") else "BLOCKED"
        lines.append(
            f"  [{state}] {r['name']:32s} status={r.get('status')} "
            f"latency={r.get('latency_ms')}ms"
        )
    lines.append("")
    lines.append("COVERAGE")
    lines.append(f"  venues: {', '.join(report['venues_used']) or 'none'}")
    lines.append(f"  tokens: {', '.join(report['tokens_covered']) or 'none'}")
    lines.append(
        f"  window: {report['window_seconds']}s  solana_cycles={report['solana_cycles']} "
        f"evm_cycles={report['evm_cycles']}  effective_cycle={report['effective_cycle_seconds']}s"
    )
    lines.append(f"  pair-samples: {report['pair_samples']}")
    lines.append(f"  fetch failures: {report['fetch_failures']}")
    lines.append("")
    lines.append("SOLANA COST STACK (on top of the executable quotes)")
    lines.append(f"  gas      : ${cs['gas_usd']:.6f}   [{cs['gas_source']}]")
    lines.append(f"  priority : ${cs['priority_usd']:.6f}   [{cs['priority_source']}]")
    lines.append(f"  jito tip : ${cs['jito_tip_usd']:.6f}   [{cs['jito_source']}]")
    lines.append(
        f"  slippage buffer: {cs['slippage_buffer_bps']} bps (labelled assumption)"
    )
    lines.append(
        f"  TOTAL ADDED: {cs['added_bps_at_notional']} bps of ${report['notional_usd']:.0f} "
        f"(${cs['added_usd']:.4f})"
    )
    lines.append("")
    lines.append("NET-EDGE TABLE (top 15 venue-pairs by max net bps)")
    hdr = (
        f"  {'pair':52s} {'n':>4s} {'max':>8s} {'mean':>8s} {'p90':>8s} "
        f"{'%pos':>6s} {'run':>4s}"
    )
    lines.append(hdr)
    for key, row in v["top_venue_pairs"].items():
        lines.append(
            f"  {key[:52]:52s} {row['n']:>4d} {row['max_net_bps']:>8.1f} "
            f"{row['mean_net_bps']:>8.1f} {row['p90_net_bps']:>8.1f} "
            f"{row['pct_positive']:>6.1f} {row['longest_positive_run']:>4d}"
        )
    lines.append("")
    lines.append("VERDICT COUNTS")
    lines.append(f"  venue-pairs measured            : {v['venue_pairs_measured']}")
    lines.append(
        f"  venue-pairs EVER net-positive   : {v['venue_pairs_ever_net_positive']} "
        f"({v['venue_pairs_ever_net_positive_pct']}%)"
    )
    lines.append(f"  best pair by mean positive edge : {v['best_pair_by_mean_positive']}")
    lines.append(
        f"  $/day at ${report['notional_usd']:.0f} (best pair): "
        f"${v['best_usd_per_day_at_notional']:.4f} "
        f"(assumes {v['assumed_cycles_per_day']} cycles/day x %positive)"
    )
    cv = report.get("cross_venue_verdict") or {}
    if cv.get("cross_venue_samples"):
        lines.append("")
        lines.append("CROSS-VENUE VERDICT (buy venue != sell venue: the actual trade)")
        lines.append(f"  cross-venue samples            : {cv['cross_venue_samples']}")
        lines.append(f"  same-venue samples (calibration): {cv['same_venue_samples']}")
        lines.append(
            f"  widest gross spread seen       : {cv['widest_gross_bps']} bps "
            f"(mean {cv['mean_gross_bps']}, worst {cv['worst_gross_bps']})"
        )
        lines.append(
            f"  max net edge after cost stack  : {cv['max_net_bps']} bps "
            f"(mean {cv['mean_net_bps']})"
        )
        lines.append(
            f"  cross-venue samples net-positive: {cv['cross_venue_samples_net_positive']} "
            f"({cv['pct_cross_venue_net_positive']}%)"
        )
        lines.append(f"  best pair                      : {cv['best_pair']} "
                     f"(gross {cv['best_pair_gross_bps']} -> net {cv['best_pair_net_bps']} bps)")
        lines.append(
            f"  $/day at ${report['notional_usd']:.0f} for best cross-venue pair: "
            f"${cv['best_usd_per_day_at_notional']:.4f}"
        )
    lines.append("")
    lines.append("SIZE SWEEP (largest $ that still nets positive)")
    for sweep in v["size_sweeps"]:
        lines.append(
            f"  {sweep.get('token')}: {sweep.get('buy_venue')}->{sweep.get('sell_venue')} "
            f"max=${sweep.get('max_executable_size_usd')}"
        )
    path.write_text("\n".join(lines) + "\n")


# --------------------------------------------------------------------- main


async def run(args: argparse.Namespace) -> int:
    SOLANA_DIR.mkdir(parents=True, exist_ok=True)
    EVM_DIR.mkdir(parents=True, exist_ok=True)
    stats: dict[str, int] = {}
    tokens = {k: MEME_TOKENS[k] for k in MEME_TOKENS if k in args.tokens} if args.tokens else MEME_TOKENS

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=8.0),
        headers={"User-Agent": "crypto-brain-research/1.0"},
    ) as client:
        reachability = await probe_reachability(client)
        (EVIDENCE / "reachability.json").write_text(json.dumps(reachability, indent=1))
        for r in reachability:
            print(f"  [{'OK ' if r.get('ok') else 'BLK'}] {r['name']:32s} "
                  f"{r.get('status')} {r.get('latency_ms')}ms")
        if args.probe_only:
            return 0

        sol_price = await fetch_sol_price(client, stats)
        priority = await measure_priority_fee(client, stats)
        jito = await measure_jito_tips(client, stats)
        costs = solana_costs(sol_price, priority, jito)
        print(f"SOL=${sol_price:.2f} priority p75={priority.get('p75')} uLam/CU "
              f"jito75={jito.get(JITO_BASE_PERCENTILE)} SOL")
        print(f"added cost stack at ${args.notional[0]:.0f}: "
              f"{costs.added_bps(args.notional[0]):.2f} bps")

        limiter = RateLimiter(args.rate)
        venues = await discover_venues(
            client, limiter, stats, tokens,
            max_tokens=args.discovery_tokens, max_venues=args.max_venues,
        )
        print(f"venues routing: {venues}")
        if len(venues) < 2:
            print("fewer than 2 venues route; aborting measurement")
            return 1

        plan = plan_budget(len(tokens), len(venues), args.rate, args.minutes * 60.0)
        print(f"plan: {plan}")

        notional = args.notional[0]
        sem = asyncio.Semaphore(args.concurrency)
        all_samples: list[PairSample] = []
        sol_cycles = evm_cycles = 0
        discarded = 0
        evm_costs_usd: dict[str, dict[str, Any]] = {}
        evm_costs_by_chain: dict[str, ChainCosts] = {}
        native_prices: dict[str, float] = {}
        for chain, cfg in EVM_CHAINS.items():
            native_prices[chain] = await evm_native_price(client, cfg["native"], stats)
            gas_usd, gas_meta = await evm_gas_usd(
                client, cfg["rpc"], native_prices[chain], stats
            )
            evm_costs_by_chain[chain] = ChainCosts(
                gas_usd=gas_usd, priority_usd=0.0, jito_tip_usd=0.0,
                gas_source=f"eth_gasPrice x {EVM_SWAP_GAS} gas ({cfg['rpc']})",
                priority_source="included in gasPrice",
                jito_source="n/a",
            )
            evm_costs_usd[chain] = {
                "native_price_usd": round(native_prices[chain], 4),
                "gas_usd": round(gas_usd, 6),
                **gas_meta,
            }

        start = time.monotonic()
        deadline = start + args.minutes * 60.0
        last_evm = 0.0
        evm_every = 60.0
        while time.monotonic() < deadline:
            ts = datetime.now(UTC).isoformat()
            quotes, q1_ref = await sample_solana_cycle(
                client, sem, limiter, stats, venues, tokens, notional, ts
            )
            edges, disc = pair_edges(quotes, q1_ref, costs, notional, ts)
            all_samples.extend(edges)
            discarded += disc
            sol_cycles += 1
            (SOLANA_DIR / f"cycle_{sol_cycles:04d}_{ts.replace(':', '')}.json").write_text(
                json.dumps(
                    {
                        "ts": ts,
                        "quotes": [q.as_dict() for q in quotes],
                        "q1_ref_raw": {k: str(v) for k, v in q1_ref.items()},
                        "edges": [e.as_dict() for e in edges],
                    },
                    indent=1,
                )
            )
            print(f"cycle {sol_cycles}: quotes={len(quotes)} edges={len(edges)} "
                  f"discarded={disc} elapsed={time.monotonic() - start:.0f}s")

            if time.monotonic() - last_evm >= evm_every:
                last_evm = time.monotonic()
                evm_quotes = await sample_evm_cycle(
                    client, notional, native_prices, ts, stats
                )
                evm_edges, edisc = evm_pair_edges(
                    evm_quotes, evm_costs_by_chain, notional, ts
                )
                all_samples.extend(evm_edges)
                discarded += edisc
                if evm_quotes:
                    evm_cycles += 1
                    (EVM_DIR / f"cycle_{evm_cycles:04d}_{ts.replace(':', '')}.json").write_text(
                        json.dumps(
                            {
                                "ts": ts,
                                "quotes": [q.as_dict() for q in evm_quotes],
                                "edges": [e.as_dict() for e in evm_edges],
                            },
                            indent=1,
                        )
                    )
                    print(f"  evm cycle {evm_cycles}: quotes={len(evm_quotes)} "
                          f"edges={len(evm_edges)}")

        window_s = time.monotonic() - start

        # Size sweep on the best few SOLANA cross-venue pairs (buy venue != sell venue):
        # an EVM self-pair or a same-venue pair is a cost calibration, not a trade, and
        # would crowd out the pairs this study is actually about.
        sweeps: list[dict[str, Any]] = []
        summary = summarize_pairs(all_samples)
        ranked = sorted(
            (
                kv
                for kv in summary.items()
                if kv[1]["chain"] == "solana"
                and kv[1]["buy_venue"] != kv[1]["sell_venue"]
                and kv[1]["ever_positive"]
            ),
            key=lambda kv: kv[1]["max_net_bps"],
            reverse=True,
        )[:5]
        if not ranked:
            # Nothing was net-positive; still sweep the least-bad cross-venue pairs so the
            # report can state the size at which the edge dies, rather than omitting it.
            ranked = sorted(
                (
                    kv
                    for kv in summary.items()
                    if kv[1]["chain"] == "solana"
                    and kv[1]["buy_venue"] != kv[1]["sell_venue"]
                ),
                key=lambda kv: kv[1]["mean_net_bps"],
                reverse=True,
            )[:3]
        for key, row in ranked:
            token = row["token"]
            if token not in tokens:
                continue
            sweep = await size_sweep(
                client, limiter, stats, token, tokens[token],
                row["buy_venue"], row["sell_venue"], costs,
                [notional, notional * 5, notional * 10, notional * 50],
            )
            sweep.update(
                {"token": token, "buy_venue": row["buy_venue"],
                 "sell_venue": row["sell_venue"]}
            )
            sweeps.append(sweep)
            print(f"size sweep {token} {row['buy_venue']}->{row['sell_venue']}: "
                  f"max=${sweep['max_executable_size_usd']}")

        (SOLANA_DIR / "all_edges.json").write_text(
            json.dumps([s.as_dict() for s in all_samples], indent=1)
        )
        report = build_report(
            reachability=reachability, venues=venues, tokens=list(tokens),
            window_s=window_s, cycles=sol_cycles, samples=all_samples, costs=costs,
            sol_price=sol_price, priority=priority, jito=jito,
            evm_costs=evm_costs_usd, evm_cycles=evm_cycles, sweeps=sweeps,
            notional_usd=notional, stats=stats,
        )
        report["discarded"] = discarded
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        (EVIDENCE / f"meme_report_{stamp}.json").write_text(json.dumps(report, indent=1))
        write_text_report(report, EVIDENCE / "MEME_REPORT.txt")
        print()
        print(json.dumps(report["verdict"], indent=1)[:4000])
    return 0


def cross_venue_verdict(
    samples: list[PairSample], notional_usd: float, cycle_seconds: float
) -> dict[str, Any]:
    """The verdict restricted to *cross-venue* pairs — the trade this study is about.

    A same-venue round trip (buy and sell at the same DEX) is a cost calibration, not an
    arbitrage; it can read a hair positive purely from quote noise on one pool, and must
    never be counted as an opportunity. This separates the two so the headline is honest.
    """
    cross = [s for s in samples if s.buy_venue != s.sell_venue]
    same = [s for s in samples if s.buy_venue == s.sell_venue]
    if not cross:
        return {"cross_venue_samples": 0, "ever_net_positive": False}
    nets = [s.net_bps for s in cross]
    gross = [s.gross_bps for s in cross]
    pos = [s for s in cross if s.net_bps > 0]
    best = max(cross, key=lambda s: s.net_bps)
    per_day_cycles = 86400.0 / cycle_seconds if cycle_seconds > 0 else 0.0
    # $/day is only meaningful if some cross-venue sample was ever net-positive.
    best_usd_per_day = 0.0
    if pos:
        mean_pos = st.fmean([s.net_bps for s in pos])
        best_usd_per_day = (
            mean_pos / BPS * notional_usd * per_day_cycles * len(pos) / len(cross)
        )
    return {
        "cross_venue_samples": len(cross),
        "same_venue_samples": len(same),
        "widest_gross_bps": round(max(gross), 3),
        "mean_gross_bps": round(st.fmean(gross), 3),
        "worst_gross_bps": round(min(gross), 3),
        "max_net_bps": round(max(nets), 3),
        "mean_net_bps": round(st.fmean(nets), 3),
        "cross_venue_samples_net_positive": len(pos),
        "pct_cross_venue_net_positive": round(100.0 * len(pos) / len(cross), 2),
        "ever_net_positive": bool(pos),
        "best_pair": best.key,
        "best_pair_gross_bps": round(best.gross_bps, 3),
        "best_pair_net_bps": round(best.net_bps, 3),
        "best_usd_per_day_at_notional": round(best_usd_per_day, 4),
        "assumed_cycles_per_day": round(per_day_cycles, 1),
    }


def reanalyze(notional: float) -> int:
    """Re-score cached cycles offline (no network) with the default cost stack.

    The default cost stack uses the Solana constants (gas at the last known SOL price is
    unavailable offline, so the on-top stack is the slippage buffer plus a zero-cost
    floor); this is a *relative* re-score for verifying the netting, and the live report
    carries the measured costs. Edges are rebuilt from the cached executable quotes, so
    nothing here is re-fetched.
    """
    sol_files = sorted(SOLANA_DIR.glob("cycle_*.json"))
    evm_files = sorted(EVM_DIR.glob("cycle_*.json"))
    if not sol_files and not evm_files:
        print("no cached cycles to reanalyze")
        return 1
    costs = ChainCosts(
        gas_usd=0.0, priority_usd=0.0, jito_tip_usd=0.0, slippage_buffer_bps=SLIPPAGE_BUFFER_BPS
    )
    evm_zero = {
        chain: ChainCosts(
            gas_usd=0.0, priority_usd=0.0, jito_tip_usd=0.0,
            slippage_buffer_bps=SLIPPAGE_BUFFER_BPS,
        )
        for chain in EVM_CHAINS
    }
    # Prefer the *measured* Solana cost stack from the newest report so the re-score is
    # faithful rather than understated; fall back to the buffer-only floor.
    reports = sorted(EVIDENCE.glob("meme_report_*.json"))
    if reports:
        try:
            cs = json.loads(reports[-1].read_text())["cost_stack_solana"]
            costs = ChainCosts(
                gas_usd=float(cs["gas_usd"]),
                priority_usd=float(cs["priority_usd"]),
                jito_tip_usd=float(cs["jito_tip_usd"]),
                slippage_buffer_bps=float(cs["slippage_buffer_bps"]),
            )
            print(f"reanalyze: using measured cost stack from {reports[-1].name} "
                  f"(added {costs.added_bps(notional):.2f} bps at ${notional:.0f})")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
    samples: list[PairSample] = []
    for path in sol_files:
        payload = json.loads(path.read_text())
        quotes = [venue_quote_from_dict(r) for r in payload.get("quotes", [])]
        q1_ref = {k: int(v) for k, v in payload.get("q1_ref_raw", {}).items()}
        ts = str(payload.get("ts", "?"))
        edges, _ = pair_edges(quotes, q1_ref, costs, notional, ts)
        samples.extend(edges)
    for path in evm_files:
        payload = json.loads(path.read_text())
        quotes = [evm_quote_from_dict(r) for r in payload.get("quotes", [])]
        ts = str(payload.get("ts", "?"))
        edges, _ = evm_pair_edges(quotes, evm_zero, notional, ts)
        samples.extend(edges)
    summary = summarize_pairs(samples)
    cross = {
        k: v
        for k, v in summary.items()
        if v["buy_venue"] != v["sell_venue"]
    }
    ranked = sorted(cross.items(), key=lambda kv: kv[1]["mean_net_bps"], reverse=True)[:15]
    print(f"reanalyze: {len(sol_files)} solana + {len(evm_files)} evm cycles "
          f"-> {len(samples)} samples, {len(summary)} pairs")
    # Cross-venue only: the trade, not the same-venue calibration.
    cv = cross_venue_verdict(samples, notional, 171.62)
    print("\nCROSS-VENUE VERDICT (buy venue != sell venue):")
    for k, val in cv.items():
        print(f"  {k}: {val}")
    print()
    for key, row in ranked:
        print(f"  {key[:56]:56s} n={row['n']:3d} mean={row['mean_net_bps']:8.1f} "
              f"max={row['max_net_bps']:8.1f} %pos={row['pct_positive']:5.1f}")

    # Rewrite the text report from the cached cycles, keeping the live metadata
    # (reachability, cost stack, coverage, size sweeps) from the newest live report.
    if reports:
        try:
            base = json.loads(reports[-1].read_text())
            ever_pos = {k: v for k, v in summary.items() if v["ever_positive"]}
            ranked_all = sorted(
                summary.items(), key=lambda kv: kv[1]["max_net_bps"], reverse=True
            )
            base["pair_samples"] = len(samples)
            base["cross_venue_verdict"] = cv
            base["reanalyzed_at"] = datetime.now(UTC).isoformat()
            base["verdict"]["top_venue_pairs"] = dict(
                [kv for kv in ranked_all if kv[1]["buy_venue"] != kv[1]["sell_venue"]][:15]
                or ranked_all[:15]
            )
            base["verdict"]["venue_pairs_measured"] = len(cross)
            base["verdict"]["venue_pairs_ever_net_positive"] = len(ever_pos)
            base["verdict"]["venue_pairs_ever_net_positive_pct"] = (
                round(100.0 * len(ever_pos) / len(summary), 2) if summary else 0.0
            )
            write_text_report(base, EVIDENCE / "MEME_REPORT.txt")
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            (EVIDENCE / f"meme_report_reanalyzed_{stamp}.json").write_text(
                json.dumps(base, indent=1)
            )
            print(f"\nrewrote {EVIDENCE / 'MEME_REPORT.txt'} and "
                  f"meme_report_reanalyzed_{stamp}.json")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            print(f"could not rewrite report: {exc}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--minutes", type=float, default=25.0)
    ap.add_argument("--rate", type=float, default=0.5, help="Jupiter sustained req/s (measured ceiling ~0.5)")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--notional", type=float, nargs="+", default=[100.0])
    ap.add_argument("--tokens", nargs="*", default=None)
    ap.add_argument(
        "--discovery-tokens",
        type=int,
        default=3,
        help="tokens used to probe venue routing (kept small: it spends rate budget)",
    )
    ap.add_argument(
        "--max-venues",
        type=int,
        default=6,
        help="cap venues so the per-cycle call count fits the sustained rate",
    )
    ap.add_argument("--probe-only", action="store_true")
    ap.add_argument("--reanalyze", action="store_true")
    args = ap.parse_args()
    if args.reanalyze:
        return reanalyze(args.notional[0])
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
