"""Class M — the execution-cost and MEV-race reality for Solana meme arbitrage.

**Measurement only.** Public endpoints, no keys, no wallets, no orders, no account
state, no transactions. Raw responses are cached under
``evidence/arbitrage/2026-10-03/meme/exec/`` with timestamps.

The question this answers is not "does a spread exist?" but the one that decides the
thesis: **even if a spread exists, can a retail $100 account capture it?** That is a
question about the cost floor and the competitive field, and both are measurable.

What is measured here, and what is not
--------------------------------------
* **Measured:** the live Jito tip floor (``bundles.jito.wtf``), the live Solana
  prioritization-fee distribution (``getRecentPrioritizationFees`` on public RPC),
  the SOL/USD price (Jupiter price v3), and the executable price impact of a $100 and
  a $1,000 quote in real meme pools (Jupiter swap quote).
* **Assumed, and labelled as such:** the compute-unit budget per leg (the fee is per
  CU, so this is the one free parameter), and the ATA rent (~0.002 SOL, refundable).
  Neither is measured by this script and neither is presented as measured.

Cost model (all in lamports, converted at the measured SOL price)
-----------------------------------------------------------------
    total = base_fee(5000 * n_signatures)
          + priority_fee(micro_lamports_per_cu * compute_units / 1e6)
          + jito_tip(measured SOL)
          + rent(new ATA only, refundable)

An atomic two-leg DEX arb is one transaction with two swap instructions, so
``n_signatures = 1`` and ``n_instructions = 2``. The base fee is charged per
signature, not per instruction; the priority fee is charged per CU for the whole
transaction. Both legs' CUs are therefore summed into a single CU budget.

Run::

    uv run python scripts/arb_meme_exec.py --probe-only
    uv run python scripts/arb_meme_exec.py --minutes 10 --interval 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics as st
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

EVIDENCE = REPO / "evidence" / "arbitrage" / "2026-10-03" / "meme" / "exec"

#: Basis points per unit fraction. ``1.0 -> 10_000 bps``.
BPS = 10_000.0

# --------------------------------------------------------------- cost constants
#
# Every number here is either a protocol constant (base fee, lamport scale), a
# measured input (SOL price, tip floor, priority fee), or an *explicitly labelled
# assumption*. Nothing is silently assumed.

#: Solana base fee: 5,000 lamports per signature. A single-signature transaction pays
#: this once regardless of how many instructions it carries.
BASE_FEE_LAMPORTS_PER_SIG = 5_000
#: Lamports per SOL.
LAMPORTS_PER_SOL = 1_000_000_000

#: Signatures in one atomic two-leg arb transaction (one signer).
ARB_SIGNATURES = 1
#: Swap instructions in the transaction (leg A and leg B).
ARB_INSTRUCTIONS = 2

#: Compute units budgeted for the whole two-leg transaction. **Assumption, not a
#: measurement.** A Jupiter route with 2-3 hops per leg plus the arb CPI commonly
#: consumes 150k-400k CU; the fee is linear in this, so it is exposed as a parameter
#: and the report shows the low/central/high band. 200k per leg is the central case.
CU_PER_LEG_LOW = 100_000
CU_PER_LEG_CENTRAL = 200_000
CU_PER_LEG_HIGH = 400_000

#: Rent-exempt minimum for a new SPL token account (ATA). **Assumption, labelled.**
#: This is *refundable*: it is returned when the account is closed, so it is a
#: capital lock-up, not a sunk cost. Reported separately for exactly that reason.
ATA_RENT_SOL = 0.00203928

# --------------------------------------------------------------- endpoints

#: Public Jito tip-floor API. Verified reachable and unauthenticated.
JITO_TIP_FLOOR = "https://bundles.jito.wtf/api/v1/bundles/tip_floor"
#: Public Solana RPC for ``getRecentPrioritizationFees``. Rate-limited but answers.
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
#: Public Solana RPC fallback.
SOLANA_RPC_FALLBACK = "https://solana-rpc.publicnode.com"
#: Jupiter price v3, for SOL/USD and meme spot reference.
JUP_PRICE = "https://lite-api.jup.ag/price/v3"
#: Jupiter swap quote, for the executable slippage-at-size measurement.
JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
#: Orca Whirlpool list. Exposes the pool's own ``lpFeeRate`` — the fee that is
#: *embedded inside every Jupiter quote*, and for meme pools the dominant cost.
ORCA_WHIRLPOOLS = "https://api.mainnet.orca.so/v1/whirlpool/list"

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

#: Accounts whose writable locks are contended. Passing real hot accounts to
#: ``getRecentPrioritizationFees`` is what turns the response from all-zeros (the
#: public RPC's answer for an empty account list) into a real fee distribution.
PRIORITY_ACCOUNTS = [
    SOL_MINT,
    USDC_MINT,
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # pump.fun program
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",  # Jupiter v6 aggregator
    "ComputeBudget111111111111111111111111111111",
]

#: Jupiter lite-api tolerates ~28 req/min sustained (measured by a sibling). Stay
#: well under it: an 8-min window at 0.4 req/s with a slippage pass still drew 30
#: 429s, so the steady-state rate is set lower and the window is kept short.
JUPITER_RATE_PER_S = 0.25
#: The tip-floor and RPC endpoints are more forgiving, but stay polite.
GENERIC_RATE_PER_S = 1.0


# --------------------------------------------------------------- pure computation


def ui_amount(raw: int | str, decimals: int) -> float:
    """Convert base units to UI units. Decimals are part of the token's identity."""
    return int(raw) / (10.0**decimals)


def bps_of(usd: float, notional_usd: float) -> float:
    """Express a USD cost as basis points of a notional. Guards zero notional."""
    if notional_usd <= 0:
        return 0.0
    return (usd / notional_usd) * BPS


def notional_key(notional_usd: float) -> str:
    """Canonical string key for a notional: ``100.0 -> "100"``, ``250.5 -> "250.5"``.

    Every notional-keyed dict in this module uses this, so ``"100"`` cannot appear in
    one table and ``"100.0"`` in another — a mismatch that would silently drop the
    primary-size number from a report.
    """
    n = float(notional_usd)
    return str(int(n)) if n == int(n) else str(n)


def base_fee_lamports(signatures: int = ARB_SIGNATURES) -> int:
    """Base fee: 5,000 lamports per signature."""
    return BASE_FEE_LAMPORTS_PER_SIG * signatures


def priority_fee_lamports(micro_lamports_per_cu: float, compute_units: float) -> float:
    """Priority fee in lamports for a whole-transaction CU budget.

    ``micro_lamports_per_cu`` is the price the RPC reports; the fee is
    ``price * CU / 1e6`` lamports.
    """
    return float(micro_lamports_per_cu) * float(compute_units) / 1_000_000.0


def tip_lamports(tip_sol: float) -> float:
    """Jito tip (a flat SOL amount per bundle) expressed in lamports."""
    return float(tip_sol) * LAMPORTS_PER_SOL


def total_execution_lamports(
    *,
    priority_micro_lamports_per_cu: float,
    compute_units: float,
    tip_sol: float,
    signatures: int = ARB_SIGNATURES,
    new_token_account: bool = False,
) -> dict[str, float]:
    """The full per-transaction cost stack, decomposed.

    Rent is reported separately *and* included in ``total`` only when a new token
    account is required; it is refundable, so the report also exposes
    ``sunk_lamports`` (the part that is actually gone).
    """
    base = float(base_fee_lamports(signatures))
    prio = priority_fee_lamports(priority_micro_lamports_per_cu, compute_units)
    tip = tip_lamports(tip_sol)
    rent = ATA_RENT_SOL * LAMPORTS_PER_SOL if new_token_account else 0.0
    sunk = base + prio + tip
    return {
        "base_lamports": base,
        "priority_lamports": prio,
        "tip_lamports": tip,
        "rent_lamports": rent,
        "sunk_lamports": sunk,
        "total_lamports": sunk + rent,
    }


def lamports_to_usd(lamports: float, sol_usd: float) -> float:
    """Convert lamports to USD at a measured SOL price."""
    return (float(lamports) / LAMPORTS_PER_SOL) * float(sol_usd)


def break_even_spread_bps(
    *,
    cost_lamports: float,
    sol_usd: float,
    notional_usd: float,
    embedded_cost_bps: float = 0.0,
) -> float:
    """The gross spread a trade must show to net zero.

    ``embedded_cost_bps`` is the price impact already inside the quote (measured
    separately); it is added because the quote the trade executes against already
    carries it. Passing 0.0 gives the pure fee/tip break-even.
    """
    fee_bps = bps_of(lamports_to_usd(cost_lamports, sol_usd), notional_usd)
    return fee_bps + float(embedded_cost_bps)


def slippage_bps(spot_out_per_usd: float, sized_out_per_usd: float) -> float:
    """Price impact of executing at size, in bps.

    ``spot_out_per_usd`` is the marginal rate from a tiny (reference) quote;
    ``sized_out_per_usd`` is the rate the size actually receives. A size that
    receives less than the marginal rate has a positive impact.
    """
    if spot_out_per_usd <= 0:
        return 0.0
    return ((spot_out_per_usd - sized_out_per_usd) / spot_out_per_usd) * BPS


def percentile(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile of an already-sorted list. ``q`` in [0, 1]."""
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return float(sorted_vals[idx])


def fee_percentiles(fees: list[float]) -> dict[str, float]:
    """Summarise a prioritization-fee sample into the percentiles that matter.

    The p50 is 0 on Solana at almost all times — which is exactly why a retail
    client cannot use it: the fee that lands a transaction in a contested slot is
    the p90+ tail, not the median.
    """
    s = sorted(float(f) for f in fees)
    return {
        "n": float(len(s)),
        "min": percentile(s, 0.0),
        "p25": percentile(s, 0.25),
        "p50": percentile(s, 0.50),
        "p75": percentile(s, 0.75),
        "p90": percentile(s, 0.90),
        "p95": percentile(s, 0.95),
        "p99": percentile(s, 0.99),
        "max": percentile(s, 1.0),
        "mean": st.mean(s) if s else 0.0,
        "nonzero_frac": (sum(1 for f in s if f > 0) / len(s)) if s else 0.0,
    }


# --------------------------------------------------------------- HTTP helpers


class Backoff:
    """Bounded exponential backoff that honours ``Retry-After``; never retry-storms."""

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
    """Async token bucket. Smooths bursts so a shared per-IP limit is not tripped."""

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

    Never raises. Failures are counted in ``stats`` so an empty run cannot be
    mistaken for a measurement of "no cost".
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


async def post_json(
    client: httpx.AsyncClient,
    url: str,
    body: dict[str, Any],
    *,
    attempts: int = 3,
    limiter: RateLimiter | None = None,
    stats: dict[str, int] | None = None,
) -> tuple[int, Any | None]:
    """POST JSON with the same backoff discipline as ``get_json``."""
    backoff = Backoff()
    last_status = 0
    for attempt in range(attempts):
        if limiter is not None:
            await limiter.acquire()
        try:
            r = await client.post(url, json=body)
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


# --------------------------------------------------------------- endpoint probes


@dataclass
class ProbeResult:
    name: str
    url: str
    status: int
    latency_ms: float
    payload: Any | None
    note: str = ""


async def probe_tip_floor(
    client: httpx.AsyncClient, *, limiter: RateLimiter, stats: dict[str, int]
) -> ProbeResult:
    """Jito tip floor: the price of landing a bundle, in SOL."""
    t0 = time.monotonic()
    status, payload = await get_json(
        client, JITO_TIP_FLOOR, limiter=limiter, stats=stats
    )
    return ProbeResult(
        "jito_tip_floor",
        JITO_TIP_FLOOR,
        status,
        (time.monotonic() - t0) * 1000.0,
        payload,
        "public, unauthenticated" if status == 200 else "unreachable",
    )


async def probe_priority_fees(
    client: httpx.AsyncClient, *, limiter: RateLimiter, stats: dict[str, int]
) -> ProbeResult:
    """getRecentPrioritizationFees with hot locked accounts (non-empty param)."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getRecentPrioritizationFees",
        "params": [PRIORITY_ACCOUNTS],
    }
    t0 = time.monotonic()
    status, payload = await post_json(
        client, SOLANA_RPC, body, limiter=limiter, stats=stats
    )
    if status != 200 or not isinstance(payload, dict) or "result" not in payload:
        status2, payload2 = await post_json(
            client, SOLANA_RPC_FALLBACK, body, limiter=limiter, stats=stats
        )
        if status2 == 200 and isinstance(payload2, dict) and "result" in payload2:
            return ProbeResult(
                "priority_fees",
                SOLANA_RPC_FALLBACK,
                status2,
                (time.monotonic() - t0) * 1000.0,
                payload2,
                "fallback RPC",
            )
    return ProbeResult(
        "priority_fees",
        SOLANA_RPC,
        status,
        (time.monotonic() - t0) * 1000.0,
        payload,
        "hot-account locked-write query" if status == 200 else "unreachable",
    )


async def probe_sol_price(
    client: httpx.AsyncClient, *, limiter: RateLimiter, stats: dict[str, int]
) -> ProbeResult:
    t0 = time.monotonic()
    status, payload = await get_json(
        client, JUP_PRICE, params={"ids": SOL_MINT}, limiter=limiter, stats=stats
    )
    return ProbeResult(
        "sol_price",
        JUP_PRICE,
        status,
        (time.monotonic() - t0) * 1000.0,
        payload,
        "Jupiter price v3",
    )


async def probe_quote(
    client: httpx.AsyncClient,
    *,
    limiter: RateLimiter,
    stats: dict[str, int],
    input_mint: str,
    output_mint: str,
    amount: int,
    slippage_bps: int = 50,
) -> tuple[int, Any | None, float]:
    t0 = time.monotonic()
    status, payload = await get_json(
        client,
        JUP_QUOTE,
        params={
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
        },
        limiter=limiter,
        stats=stats,
    )
    return status, payload, (time.monotonic() - t0) * 1000.0


# --------------------------------------------------------------- interpretation


def tip_floor_numbers(payload: Any) -> dict[str, float] | None:
    """Extract the tip-floor percentiles (SOL) from the Jito response.

    The API returns a single-element list with a timestamp and landed-tip
    percentiles. Returned in SOL; the caller converts at the measured SOL price.
    """
    if not isinstance(payload, list) or not payload:
        return None
    row = payload[0]
    if not isinstance(row, dict):
        return None
    keys = {
        "p25": "landed_tips_25th_percentile",
        "p50": "landed_tips_50th_percentile",
        "p75": "landed_tips_75th_percentile",
        "p95": "landed_tips_95th_percentile",
        "p99": "landed_tips_99th_percentile",
        "ema_p50": "ema_landed_tips_50th_percentile",
    }
    out = {k: float(row[v]) for k, v in keys.items() if v in row}
    return out or None


def priority_fee_numbers(payload: Any) -> dict[str, float] | None:
    """Extract the prioritization-fee distribution (micro-lamports/CU)."""
    if not isinstance(payload, dict):
        return None
    rows = payload.get("result")
    if not isinstance(rows, list) or not rows:
        return None
    fees = [
        float(r["prioritizationFee"])
        for r in rows
        if isinstance(r, dict) and "prioritizationFee" in r
    ]
    return fee_percentiles(fees) if fees else None


def sol_price_usd(payload: Any) -> float | None:
    if not isinstance(payload, dict):
        return None
    entry = payload.get(SOL_MINT)
    if isinstance(entry, dict) and entry.get("usdPrice"):
        return float(entry["usdPrice"])
    return None


def cost_scenarios(
    *,
    sol_usd: float,
    tip_sol: float,
    priority_fee_dist: dict[str, float],
    notionals: list[float],
    new_token_account: bool = False,
) -> dict[str, Any]:
    """Build the full cost table for each notional and each CU assumption.

    The headline number is the central CU case at the p90 priority fee (the p50 is
    zero and therefore useless for a transaction that must actually land in a
    contested slot).
    """
    scenarios: dict[str, Any] = {}
    for label, cu_per_leg in (
        ("low_cu", CU_PER_LEG_LOW),
        ("central_cu", CU_PER_LEG_CENTRAL),
        ("high_cu", CU_PER_LEG_HIGH),
    ):
        cu_total = cu_per_leg * ARB_INSTRUCTIONS
        per_fee: dict[str, Any] = {}
        for fee_label, fee in (
            ("p50", priority_fee_dist.get("p50", 0.0)),
            ("p90", priority_fee_dist.get("p90", 0.0)),
            ("p99", priority_fee_dist.get("p99", 0.0)),
        ):
            stack = total_execution_lamports(
                priority_micro_lamports_per_cu=fee,
                compute_units=cu_total,
                tip_sol=tip_sol,
                new_token_account=new_token_account,
            )
            sunk_usd = lamports_to_usd(stack["sunk_lamports"], sol_usd)
            total_usd = lamports_to_usd(stack["total_lamports"], sol_usd)
            per_fee[fee_label] = {
                "priority_fee_micro_lamports_per_cu": fee,
                "compute_units": cu_total,
                "base_usd": lamports_to_usd(stack["base_lamports"], sol_usd),
                "priority_usd": lamports_to_usd(stack["priority_lamports"], sol_usd),
                "tip_usd": lamports_to_usd(stack["tip_lamports"], sol_usd),
                "rent_usd": lamports_to_usd(stack["rent_lamports"], sol_usd),
                "sunk_usd": sunk_usd,
                "total_usd": total_usd,
                "sunk_bps": {notional_key(n): bps_of(sunk_usd, n) for n in notionals},
                "total_bps": {notional_key(n): bps_of(total_usd, n) for n in notionals},
                "break_even_bps": {
                    notional_key(n): break_even_spread_bps(
                        cost_lamports=stack["sunk_lamports"],
                        sol_usd=sol_usd,
                        notional_usd=n,
                    )
                    for n in notionals
                },
            }
        scenarios[label] = per_fee
    return scenarios


def slippage_row(
    *,
    label: str,
    mint: str,
    quotes: dict[str, dict[str, Any] | None],
    reference_usd: float,
) -> dict[str, Any]:
    """Compute price impact at each size from Jupiter quotes against a reference.

    The reference is the smallest quote's rate (marginal price). Impact is the bps
    shortfall of a larger size's realised rate versus that marginal rate.
    """
    ref_q = quotes.get(notional_key(reference_usd))
    if not ref_q or "outAmount" not in ref_q or not ref_q.get("inAmount"):
        return {"token": label, "mint": mint, "error": "no reference quote"}
    ref_rate = float(ref_q["outAmount"]) / float(ref_q["inAmount"])
    out: dict[str, Any] = {"token": label, "mint": mint, "reference_usd": reference_usd}
    for size, q in quotes.items():
        if not q or "outAmount" not in q or not q.get("inAmount"):
            out[size] = {"error": "no quote"}
            continue
        rate = float(q["outAmount"]) / float(q["inAmount"])
        out[size] = {
            "impact_bps": slippage_bps(ref_rate, rate),
            "jupiter_price_impact_pct": q.get("priceImpactPct"),
            "route": [s.get("swapInfo", {}).get("label") for s in q.get("routePlan", [])],
            "hops": len(q.get("routePlan", [])),
        }
    return out


async def measure_slippage(
    client: httpx.AsyncClient,
    *,
    limiter: RateLimiter,
    stats: dict[str, int],
    notionals: list[float],
    latencies: dict[str, list[float]],
) -> dict[str, Any]:
    """Quote every meme token at every size and compute the price impact.

    Split out so it can be re-run on its own (``--slippage-only``) when the
    surrounding fee sampling tripped a rate limit, without redoing the window.
    """
    slippage: dict[str, Any] = {}
    for label, mint in MEME_TOKENS.items():
        quotes: dict[str, dict[str, Any] | None] = {}
        for size in notionals:
            amount = int(round(size * 1_000_000))  # USDC in, 6 decimals
            st_, payload, lat = await probe_quote(
                client,
                limiter=limiter,
                stats=stats,
                input_mint=USDC_MINT,
                output_mint=mint,
                amount=amount,
            )
            latencies.setdefault("jup_quote", []).append(lat)
            quotes[notional_key(size)] = payload if st_ == 200 else None
        slippage[label] = slippage_row(
            label=label, mint=mint, quotes=quotes, reference_usd=min(notionals)
        )
    return slippage


def pool_fee_stats(
    whirlpools: Any, meme_mints: dict[str, str]
) -> dict[str, Any]:
    """Extract the measured ``lpFeeRate`` of every meme/SOL and meme/USDC pool.

    This is the fee **inside** every Jupiter quote, and for meme pools it dwarfs
    the on-chain fee stack. A round trip swaps twice, so it pays this twice.
    """
    if not isinstance(whirlpools, list):
        return {}
    out: dict[str, Any] = {}
    for pool in whirlpools:
        if not isinstance(pool, dict):
            continue
        rate = pool.get("lpFeeRate")
        if rate is None:
            continue
        a = pool.get("tokenA") or {}
        b = pool.get("tokenB") or {}
        for tok, quote in ((a, b), (b, a)):
            sym = meme_mints.get(tok.get("mint"))
            if not sym or quote.get("symbol") not in ("SOL", "USDC"):
                continue
            entry = out.setdefault(sym, {"pools": [], "min_fee_bps": 1e9, "max_fee_bps": 0.0})
            fee_bps = float(rate) * BPS
            entry["pools"].append(
                {
                    "quote": quote.get("symbol"),
                    "fee_bps": fee_bps,
                    "tvl_usd": pool.get("tvl"),
                }
            )
            entry["min_fee_bps"] = min(entry["min_fee_bps"], fee_bps)
            entry["max_fee_bps"] = max(entry["max_fee_bps"], fee_bps)
    # The deepest pool is the one a size actually routes through; report its fee as
    # the representative per-leg cost.
    for sym, entry in out.items():
        pools = entry["pools"]
        deepest = max(pools, key=lambda p: p.get("tvl_usd") or 0.0)
        entry["deepest_pool"] = deepest
        entry["deepest_pool_fee_bps"] = deepest["fee_bps"]
        entry["deepest_pool_round_trip_bps"] = 2.0 * deepest["fee_bps"]
        entry["pools"] = sorted(pools, key=lambda p: -(p.get("tvl_usd") or 0.0))[:6]
    return out


async def probe_pool_fees(
    client: httpx.AsyncClient, *, limiter: RateLimiter, stats: dict[str, int]
) -> tuple[int, Any | None]:
    return await get_json(
        client, ORCA_WHIRLPOOLS, limiter=limiter, stats=stats
    )


# --------------------------------------------------------------- collection


async def collect(
    minutes: float, interval: float, notionals: list[float]
) -> dict[str, Any]:
    """Sample the fee endpoints over a window and measure slippage once."""
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    stats: dict[str, int] = {}
    tip_series: list[dict[str, Any]] = []
    fee_series: list[dict[str, float]] = []
    sol_prices: list[float] = []
    latencies: dict[str, list[float]] = {}
    slippage: dict[str, Any] = {}

    jup_limiter = RateLimiter(JUPITER_RATE_PER_S)
    gen_limiter = RateLimiter(GENERIC_RATE_PER_S)

    async with httpx.AsyncClient(timeout=20.0) as client:
        deadline = time.monotonic() + minutes * 60.0
        first = True
        while first or time.monotonic() < deadline:
            first = False
            tip = await probe_tip_floor(client, limiter=gen_limiter, stats=stats)
            prio = await probe_priority_fees(client, limiter=gen_limiter, stats=stats)
            sol = await probe_sol_price(client, limiter=jup_limiter, stats=stats)
            latencies.setdefault("jito_tip_floor", []).append(tip.latency_ms)
            latencies.setdefault("priority_fees", []).append(prio.latency_ms)
            latencies.setdefault("sol_price", []).append(sol.latency_ms)

            tn = tip_floor_numbers(tip.payload)
            if tn:
                tn = dict(tn)
                tn["time"] = (tip.payload[0] or {}).get("time")
                tip_series.append(tn)
            fn = priority_fee_numbers(prio.payload)
            if fn:
                fee_series.append(fn)
            sp = sol_price_usd(sol.payload)
            if sp:
                sol_prices.append(sp)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(interval, remaining))

        # One slippage pass, paced under the Jupiter limit.
        slippage = await measure_slippage(
            client, limiter=jup_limiter, stats=stats, notionals=notionals, latencies=latencies
        )
        # Pool fees (the quote-embedded cost). Orca's list is one request.
        pf_status, pf_payload = await probe_pool_fees(
            client, limiter=gen_limiter, stats=stats
        )
        pool_fees = (
            pool_fee_stats(
                (pf_payload or {}).get("whirlpools"),
                {mint: sym for sym, mint in MEME_TOKENS.items()},
            )
            if pf_status == 200
            else {}
        )

    return {
        "tip_series": tip_series,
        "fee_series": fee_series,
        "sol_prices": sol_prices,
        "latencies_ms": {k: v for k, v in latencies.items()},
        "slippage": slippage,
        "pool_fees": pool_fees,
        "failures": stats,
    }


#: Top Solana meme tokens by liquidity/volume (GeckoTerminal, 2026-10-03; every mint
#: verified against GeckoTerminal's token endpoint, not recalled). Quotes are
#: USDC -> token, so the measured impact is the buy-side impact.
MEME_TOKENS: dict[str, str] = {
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "POPCAT": "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
    "PENGU": "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv",
    "TRUMP": "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN",
}


def summarize(
    collected: dict[str, Any], *, notionals: list[float]
) -> dict[str, Any]:
    """Reduce the raw collection into the report's numbers."""
    tips = collected["tip_series"]
    fees = collected["fee_series"]
    sols = collected["sol_prices"]
    sol_usd = st.median(sols) if sols else 0.0

    # Tip floor over the window: median of each percentile.
    tip_summary: dict[str, Any] = {}
    if tips:
        for key in ("p25", "p50", "p75", "p95", "p99", "ema_p50"):
            vals = sorted(t[key] for t in tips if key in t)
            if vals:
                tip_summary[key] = {
                    "sol": st.median(vals),
                    "sol_min": vals[0],
                    "sol_max": vals[-1],
                    "usd": st.median(vals) * sol_usd,
                }

    # Priority fee: median of each percentile across the window.
    fee_summary: dict[str, float] = {}
    if fees:
        for key in ("p25", "p50", "p75", "p90", "p95", "p99", "max", "mean", "nonzero_frac"):
            vals = [f[key] for f in fees if key in f]
            if vals:
                fee_summary[key] = st.median(vals)

    # The headline tip: p50 landed tip is what a bundle of this value would pay to
    # be *competitive*; the p75 is what it pays to be *ahead of half the field*.
    tip_p50 = tip_summary.get("p50", {}).get("sol", 0.0)
    tip_p75 = tip_summary.get("p75", {}).get("sol", 0.0)

    costs = cost_scenarios(
        sol_usd=sol_usd,
        tip_sol=tip_p50,
        priority_fee_dist=fee_summary,
        notionals=notionals,
    )
    costs_p75 = cost_scenarios(
        sol_usd=sol_usd,
        tip_sol=tip_p75,
        priority_fee_dist=fee_summary,
        notionals=notionals,
    )

    lat = collected["latencies_ms"]
    latency_summary = {
        k: {
            "n": len(v),
            "p50_ms": percentile(sorted(v), 0.5),
            "p95_ms": percentile(sorted(v), 0.95),
            "max_ms": max(v) if v else 0.0,
        }
        for k, v in lat.items()
    }

    return {
        "sol_usd": sol_usd,
        "samples": {
            "tip_series": len(tips),
            "fee_series": len(fees),
            "sol_prices": len(sols),
        },
        "tip_floor_sol": tip_summary,
        "priority_fee_micro_lamports_per_cu": fee_summary,
        "latency_ms": latency_summary,
        "costs_at_p50_tip": costs,
        "costs_at_p75_tip": costs_p75,
        "slippage": collected["slippage"],
        "pool_fees": collected.get("pool_fees", {}),
        "failures": collected["failures"],
    }


def build_verdict(summary: dict[str, Any], notionals: list[float]) -> dict[str, Any]:
    """The bottom-line numbers: what spread a retail $100 trade must beat.

    The primary size is **$100** — the retail paper basis. If a $100 size was not
    measured, the smallest measured size is used and the verdict says so.
    """
    sol_usd = summary["sol_usd"]
    central = summary["costs_at_p50_tip"]["central_cu"]["p90"]
    slippage = summary["slippage"]
    if 100.0 in notionals:
        primary = 100.0
    elif notionals:
        primary = min(notionals)
    else:
        primary = 100.0
    prim = notional_key(primary)

    # Slippage at the primary size, averaged across meme pools that quoted.
    impacts = [
        slippage[t][prim]["impact_bps"]
        for t in slippage
        if isinstance(slippage[t], dict)
        and isinstance(slippage[t].get(prim), dict)
        and "impact_bps" in slippage[t][prim]
    ]
    mean_impact = st.mean(impacts) if impacts else 0.0

    # The dominant embedded cost: the pool fee, paid on BOTH legs of the round trip.
    pool_fees = summary.get("pool_fees", {})
    round_trip_pool_fees = [
        pool_fees[t]["deepest_pool_round_trip_bps"]
        for t in pool_fees
        if isinstance(pool_fees.get(t), dict) and "deepest_pool_round_trip_bps" in pool_fees[t]
    ]
    mean_pool_round_trip = st.mean(round_trip_pool_fees) if round_trip_pool_fees else 0.0

    fee_bps_primary = central["break_even_bps"].get(prim, 0.0)
    return {
        "sol_usd": sol_usd,
        "primary_notional_usd": primary,
        "execution_cost_usd": central["sunk_usd"],
        "execution_cost_bps": central["sunk_bps"].get(prim, 0.0),
        "execution_cost_bps_1000": central["sunk_bps"].get("1000", 0.0),
        "break_even_fee_only_bps": fee_bps_primary,
        "slippage_mean_bps_at_primary": mean_impact,
        "pool_fee_round_trip_bps_mean": mean_pool_round_trip,
        "break_even_fee_plus_slippage_bps": fee_bps_primary + mean_impact,
        "break_even_incl_pool_fees_bps": fee_bps_primary + mean_impact + mean_pool_round_trip,
        "note": (
            "break_even_fee_only_bps is the on-chain fee floor the spread must beat. "
            "break_even_incl_pool_fees_bps adds the measured pool fee (paid on both "
            "legs) and the measured price impact — this is the realistic bar. Both "
            "still exclude the MEV race, so the true bar is higher again."
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--notional", type=float, nargs="+", default=[100.0, 1000.0])
    ap.add_argument("--probe-only", action="store_true")
    ap.add_argument(
        "--slippage-only",
        action="store_true",
        help="Only measure the slippage-at-size table (no fee/tip window).",
    )
    ap.add_argument(
        "--pool-fees-only",
        action="store_true",
        help="Only fetch the Orca whirlpool fees (one request).",
    )
    args = ap.parse_args()

    EVIDENCE.mkdir(parents=True, exist_ok=True)

    if args.pool_fees_only:

        async def pool_fee_pass() -> None:
            stats: dict[str, int] = {}
            limiter = RateLimiter(GENERIC_RATE_PER_S)
            async with httpx.AsyncClient(timeout=30.0) as client:
                status, payload = await probe_pool_fees(
                    client, limiter=limiter, stats=stats
                )
            fees = (
                pool_fee_stats(
                    (payload or {}).get("whirlpools"),
                    {mint: sym for sym, mint in MEME_TOKENS.items()},
                )
                if status == 200
                else {}
            )
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            (EVIDENCE / f"raw_pool_fees_{stamp}.json").write_text(
                json.dumps(
                    {
                        "generated_at": datetime.now(UTC).isoformat(),
                        "source": ORCA_WHIRLPOOLS,
                        "status": status,
                        "pool_fees": fees,
                        "failures": stats,
                    },
                    indent=1,
                )
            )
            print(json.dumps(fees, indent=1))
            print("failures:", stats)

        asyncio.run(pool_fee_pass())
        return 0

    if args.slippage_only:

        async def slippage_pass() -> None:
            stats: dict[str, int] = {}
            latencies: dict[str, list[float]] = {}
            limiter = RateLimiter(JUPITER_RATE_PER_S)
            async with httpx.AsyncClient(timeout=30.0) as client:
                slip = await measure_slippage(
                    client,
                    limiter=limiter,
                    stats=stats,
                    notionals=args.notional,
                    latencies=latencies,
                )
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            (EVIDENCE / f"raw_slippage_{stamp}.json").write_text(
                json.dumps(
                    {
                        "generated_at": datetime.now(UTC).isoformat(),
                        "notionals_usd": args.notional,
                        "slippage": slip,
                        "latencies_ms": latencies,
                        "failures": stats,
                    },
                    indent=1,
                )
            )
            print(json.dumps(slip, indent=1))
            print("failures:", stats)

        asyncio.run(slippage_pass())
        return 0

    if args.probe_only:

        async def probe() -> None:
            stats: dict[str, int] = {}
            jup_limiter = RateLimiter(JUPITER_RATE_PER_S)
            gen_limiter = RateLimiter(GENERIC_RATE_PER_S)
            async with httpx.AsyncClient(timeout=20.0) as client:
                tip = await probe_tip_floor(client, limiter=gen_limiter, stats=stats)
                prio = await probe_priority_fees(client, limiter=gen_limiter, stats=stats)
                sol = await probe_sol_price(client, limiter=jup_limiter, stats=stats)
                print(f"{tip.name:18} {tip.status} {tip.latency_ms:6.0f}ms {tip.note}")
                print(f"{prio.name:18} {prio.status} {prio.latency_ms:6.0f}ms {prio.note}")
                print(f"{sol.name:18} {sol.status} {sol.latency_ms:6.0f}ms {sol.note}")
                print("tip_floor:", json.dumps(tip_floor_numbers(tip.payload)))
                print("priority:", json.dumps(priority_fee_numbers(prio.payload)))
                print("sol_usd:", sol_price_usd(sol.payload))
            print("failures:", stats)

        asyncio.run(probe())
        return 0

    print(f"collecting {args.minutes:.0f} min @ {args.interval:.0f}s ...")
    collected = asyncio.run(collect(args.minutes, args.interval, args.notional))
    summary = summarize(collected, notionals=args.notional)
    verdict = build_verdict(summary, args.notional)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (EVIDENCE / f"raw_exec_{stamp}.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "window_minutes": args.minutes,
                "interval_s": args.interval,
                "notionals_usd": args.notional,
                "cost_constants": {
                    "base_fee_lamports_per_sig": BASE_FEE_LAMPORTS_PER_SIG,
                    "signatures": ARB_SIGNATURES,
                    "instructions": ARB_INSTRUCTIONS,
                    "cu_per_leg_low": CU_PER_LEG_LOW,
                    "cu_per_leg_central": CU_PER_LEG_CENTRAL,
                    "cu_per_leg_high": CU_PER_LEG_HIGH,
                    "ata_rent_sol": ATA_RENT_SOL,
                },
                "summary": summary,
                "verdict": verdict,
            },
            indent=1,
        )
    )
    print(f"{summary['samples']} samples; failures={summary['failures']}")
    print(json.dumps(verdict, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
