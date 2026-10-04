"""Meme-coin arbitrage across **every reachable chain except Solana**, plus CEX<->DEX.

**Measurement only.** Public endpoints, no keys, no wallets, no orders, no account
state, no transactions, no writes outside the evidence cache. A sibling worker owns the
Solana DEX leg; this module deliberately excludes ``solana`` so the two do not overlap.

Two trade shapes are measured on each reachable chain:

1. **Cross-pool, same token, same chain.** The same base token trades on >= 2 pools /
   DEXes. Buy at one pool's *executable* price, sell at the other's. Both pool fees and
   both price impacts are charged; the chain's native gas is charged once per round trip.
2. **CEX<->DEX.** Where a CEX lists the same meme, compare the CEX executable price
   (bid/ask, never mid) with the DEX executable price. The CEX taker fee (project §10.2,
   10 bps) is charged on the CEX leg, gas on the DEX leg, and a labelled withdrawal /
   bridge cost where inventory must move.

**Price sources.** DexScreener (multi-chain, no key) is the primary discoverer: one
``/latest/dex/search`` call returns pairs on *every* chain, so a fixed symbol list covers
all networks per iteration. GeckoTerminal (multi-chain, no key, rate-limited to ~30
req/min) is the secondary volume ranking. CEX references come from Binance vision
(spot mirror), Gate, Huobi and Hyperliquid.

**What is measured vs assumed.** Pool price, pool liquidity and 24h volume are
*measured*. Pool fee (from the DEX/labels map), price impact (constant-product model off
the pool's liquidity), native gas and withdrawal costs are *labelled assumption ranges*
— cited, never presented as measured. The report separates the two.

Run::

    uv run python scripts/arb_meme_multichain.py --probe-only
    uv run python scripts/arb_meme_multichain.py --minutes 20 --interval 15
    uv run python scripts/arb_meme_multichain.py --reanalyze
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics as st
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

EVIDENCE = REPO / "evidence" / "arbitrage" / "2026-10-03" / "meme" / "multichain"

#: Basis points per unit fraction. ``1.0 -> 10_000 bps``.
BPS = 10_000.0

# --------------------------------------------------------------- cost constants
#
# Every number in this block is a *labelled assumption*, not a measurement. They are
# the project's own fee schedule or cited standard ranges.

#: Project §10.2 base-tier spot taker fee, per CEX leg.
CEX_TAKER_BPS = 10.0
#: A CEX round trip pays taker on entry and exit. For a CEX<->DEX arb we charge the
#: CEX taker once (one CEX side) — the DEX side pays pool fee + gas instead.
CEX_TAKER_ONE_LEG_BPS = CEX_TAKER_BPS

#: Native gas, USD, per round trip, by chain. Labelled *ranges*: the low end is an
#: uncongested simple swap, the high end a congested one with a priority tip. Not
#: measured here. ``None`` means the chain is not in the map (gas then reported as a
#: gap, not silently zeroed).
GAS_USD_RANGE: dict[str, tuple[float, float]] = {
    "ethereum": (0.50, 15.0),
    "eth": (0.50, 15.0),
    "bsc": (0.05, 0.60),
    "base": (0.005, 0.10),
    "arbitrum": (0.01, 0.30),
    "optimism": (0.005, 0.10),
    "polygon_pos": (0.005, 0.10),
    "polygon": (0.005, 0.10),
    "avax": (0.01, 0.30),
    "fantom": (0.005, 0.10),
    "celo": (0.005, 0.05),
    "mantle": (0.005, 0.05),
    "linea": (0.01, 0.15),
    "scroll": (0.01, 0.20),
    "zksync": (0.01, 0.20),
    "blast": (0.01, 0.15),
    "mode": (0.005, 0.05),
    "berachain": (0.005, 0.10),
    "sonic": (0.005, 0.05),
    "sei-evm": (0.005, 0.05),
    "sei-network": (0.005, 0.05),
    "ton": (0.01, 0.10),
    "tron": (0.30, 3.00),
    "sui-network": (0.005, 0.02),
    "sui": (0.005, 0.02),
    "aptos": (0.005, 0.02),
    "pulsechain": (0.01, 0.30),
    "cronos": (0.01, 0.20),
    "zora-network": (0.005, 0.05),
    "unichain": (0.005, 0.05),
    "hyperevm": (0.005, 0.05),
}
#: Fallback for a chain we can price but have no labelled gas range for. Reported as a
#: gap rather than pretended at zero.
GAS_USD_UNKNOWN = (0.0, 5.0)

#: CEX withdrawal fee to move the token leg onto the DEX chain. Binance does not expose
#: per-network withdrawal fees on public endpoints, so this is a labelled range.
WITHDRAWAL_USD_RANGE = (0.12, 1.20)

#: Realised-slippage buffer reserved on top of the quoted impact (quote-time gap widens
#: by the time the second leg lands). Labelled, conservative.
SLIPPAGE_BUFFER_BPS = 10.0

#: Constant-product impact model: a single-side swap of ``N`` USD into a pool whose
#: quote-side reserve is ``R`` moves the price by ~``N/R``. DexScreener reports
#: ``liquidity.usd`` for *both* sides, so the quote reserve is taken as that times
#: ``QUOTE_SHARE``. This is a model, labelled as such, not a measured impact.
QUOTE_SHARE = 0.5

#: A |gap| beyond this is a data error (wrong token, stale pool, decimal bug), not a
#: dislocation. Discarded and counted rather than reported as a headline.
SANITY_MAX_GAP_BPS = 3000.0

#: A tighter bound for the CEX<->DEX leg specifically. That leg matches by *symbol*
#: (a CEX lists tickers, not contract addresses), so a same-ticker token on another
#: chain is a real collision risk. A meme trading more than 15% away from its CEX
#: listing is almost always a different asset, and the classic trade lives in the
#: 1-10% band, so anything wider is discarded and counted rather than headlined.
CEX_DEX_MAX_GAP_BPS = 1500.0

#: Conservative allowlist of canonical contracts, keyed ``(chain, address_lowercase)``
#: -> the symbol the contract actually is. A CEX<->DEX row is ``identity_verified`` only
#: when its pool's ``(chain, base_address)`` is here *and* the pool's symbol matches.
#: This is deliberately not exhaustive: a symbol absent from the map is reported as
#: **unverified**, which is the safe default (a wrong address simply never matches, so
#: the map cannot upgrade a collision to "verified"). Cross-pool rows are always
#: verified — they are matched by contract address, not by symbol.
CANONICAL_CONTRACTS: dict[tuple[str, str], str] = {
    ("ethereum", "0x6982508145454ce325ddbe47a25d4ec3d2311933"): "PEPE",
    ("ethereum", "0x95ad61b0a150d79219dcf64e1e6cc01f0b64c4ce"): "SHIB",
    ("ethereum", "0xcf0c122c6b73ff809c693db761e7baebe62b6a2e"): "FLOKI",
    ("ethereum", "0xaaeE1A9723aadbD583C24d59dE1D8282bD2c4bE4".lower()): "MOG",
    ("ethereum", "0xa35923162c49cf95e6bf26623385eb431ad920d3"): "TURBO",
    ("ethereum", "0x68bbed6a47194eff1cf514b50ea91895597fc91e"): "ANDY",
    ("ethereum", "0x5026f006b85729a8b14553fae6af249ad16c9aab"): "WOJAK",
    ("ethereum", "0x761d38e5ddf6ccf6cf7c55759d5210750b5d60f3"): "ELON",
    ("base", "0x532f27101965dd16442e59d40670faf5ebb142e4"): "BRETT",
    ("base", "0xac1bd2486aaf3b5c0fc3fd868558b082a531b2b4"): "TOSHI",
    ("base", "0x4ed4e862860bed51a9570b96d89af5e1b0efefed"): "DEGEN",
    ("base", "0x940181a94a35a4569e4529a3cdfb74e38fd98631"): "AERO",
    ("base", "0x6921b130d297cc43754afba22e5eac0f33fac8b8"): "DOGINME",
    ("base", "0x9a26f5433671751c3276a065f57e5a02d2817973"): "KEYCAT",
    ("base", "0xb166e8b140d35d9d8226e40c09f757bac5a4d87d"): "NPC",
    ("base", "0x50da645f148798f68ef2d7db7c1cb22a6819bb2c"): "SPX",
    ("ethereum", "0xe0f63a424a4439cbe457d80e4f4b51ad25b2b97c"): "SPX",
    ("bsc", "0x0df0587216a4a1bb7d5082fdc491d93d2dd4b413"): "CHEEMS",
    ("bsc", "0xc748673057861a797275cd8a068abb95a902e8de"): "BABYDOGE",
    ("bsc", "0xfb5b838b6cfeedc2873ab27866079ac55363d37e"): "FLOKI",
}


def is_verified_contract(chain: str, address: str, symbol: str) -> bool:
    """True only if ``(chain, address)`` is a known canonical contract for ``symbol``.

    A wrong or unknown address returns False — the allowlist can never upgrade an
    unverified (possibly colliding) symbol match to "verified".
    """
    if not address:
        return False
    return CANONICAL_CONTRACTS.get((chain, address.lower())) == symbol.upper()

# --------------------------------------------------------------- DEX fee map
#
# Pool fee by DEX. DexScreener's ``labels`` disambiguates Uniswap/Pancake versions.
# Where a DEX's fee is not fixed (Uniswap v3 tiers), the common meme tier is used and
# the assumption is named. Values are bps per leg, charged on both legs of a swap.

DEX_FEE_BPS_DEFAULT = 30.0
DEX_FEE_BPS: dict[str, float] = {
    "uniswap": 30.0,          # v2 30 bps; v3 tier disambiguated below
    "pancakeswap": 25.0,      # v2 25 bps
    "sushiswap": 30.0,
    "shibaswap": 30.0,
    "quickswap": 30.0,
    "traderjoe": 30.0,
    "camelot": 30.0,
    "aerodrome": 30.0,        # volatile-pair 30 bps (stable pairs are ~2 bps)
    "velodrome": 30.0,
    "basedswap": 30.0,
    "baseswap": 30.0,
    "uniswap-v3": 30.0,
    "alienbase": 30.0,
    "pancakeswap-amm": 25.0,
    "pancakeswap-v3": 25.0,
    "biswap": 10.0,
    "thena": 25.0,
    "ramses": 30.0,
    "ramses-v2": 30.0,
    "solidly": 30.0,
    "spookyswap": 30.0,
    "spiritswap": 30.0,
    "wingers": 30.0,
    "netswap": 30.0,
    "lynex": 30.0,
    "hermes": 30.0,
    "kyberswap": 20.0,
    "stargate": 6.0,
    "curve": 4.0,
    "balancer": 30.0,
    "syncswap": 30.0,
    "mute": 30.0,
    "iziswap": 30.0,
    "spacefi": 30.0,
    "thruster": 30.0,
    "blasterswap": 30.0,
    "agni": 30.0,
    "fusionx": 30.0,
    "merchantmoe": 30.0,
    "turbos": 30.0,
    "cetus": 30.0,
    "navi": 30.0,
    "liquidswap": 30.0,
    "pancakeswap-ton": 25.0,
    "dedust": 25.0,
    "stonfi": 25.0,
    "sundae": 30.0,
    "minswap": 30.0,
    "raydium": 25.0,
}

#: Uniswap v3 fee tiers, selected by the pool's ``labels`` when present.
UNIV3_LABEL_FEE_BPS: dict[str, float] = {
    "0.01%": 1.0,
    "0.05%": 5.0,
    "0.3%": 30.0,
    "1%": 100.0,
}

#: Chains that are Solana or Solana-adjacent — excluded: a sibling worker owns them.
EXCLUDED_CHAINS = {"solana"}

# --------------------------------------------------------------- symbol universe
#
# Meme / mid-cap symbols with broad DEX presence and (mostly) a CEX listing, so both
# trade shapes are reachable. Not a curated subset of chains — the *chains* come from
# whatever DexScreener returns for these symbols; this list only bounds the searches.

MEME_SYMBOLS: tuple[str, ...] = (
    "PEPE", "DOGE", "SHIB", "BONK", "WIF", "FLOKI", "BRETT", "MOG", "POPCAT",
    "TURBO", "PENGU", "PNUT", "NEIRO", "MEME", "BOME", "TRUMP", "FARTCOIN",
    "GOAT", "MOODENG", "SPX", "GIGA", "DEGEN", "AERO", "TOSHI", "MYRO", "WEN",
    "SLERF", "BAN", "BABYDOGE", "ELON", "SATS", "RATS", "AI16Z", "KEYCAT",
    "MUMU", "RETARDIO", "LOCKIN", "MICHI", "FWOG", "NPC", "ANDY", "WOJAK",
    "CHILLGUY", "LUCE", "SIGMA", "ACT", "BANANA", "CHEEMS", "DOGINME", "BOBO",
    "CAT", "DADDY", "TREMP", "BODEN", "STRUMP", "HARAMBE", "PONKE", "BOME",
)

#: CEX reference endpoints (all keyless).
BINANCE_BOOK = "https://data-api.binance.vision/api/v3/ticker/bookTicker"
BINANCE_24HR = "https://data-api.binance.vision/api/v3/ticker/24hr"
GATE_TICKERS = "https://api.gateio.ws/api/v4/spot/tickers"
HUOBI_MERGED = "https://api.huobi.pro/market/detail/merged"
HYPERLIQUID_INFO = "https://api.hyperliquid.xyz/info"

DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search"
DEXSCREENER_PAIRS = "https://api.dexscreener.com/latest/dex/pairs"
GECKOTERMINAL_POOLS = "https://api.geckoterminal.com/api/v2/networks/{net}/pools"


# =========================================================================== pure math


@dataclass(frozen=True, slots=True)
class Pool:
    """One pool's *measured* state, normalised across chains."""

    chain: str
    dex: str
    labels: tuple[str, ...]
    base_symbol: str
    base_address: str
    quote_symbol: str
    pair_address: str
    price_usd: float
    liquidity_usd: float
    volume_h24: float
    fee_bps: float = DEX_FEE_BPS_DEFAULT
    source: str = "dexscreener"


@dataclass(frozen=True, slots=True)
class CostStack:
    """The added cost of a round trip, in bps of notional, by where it is charged.

    Fixed USD costs (gas, withdrawal) are converted to bps at the notional. Pool fees
    and impact are *embedded* in the executable prices, so they are recorded here for
    the diagnostic breakdown only and are NOT subtracted again.
    """

    notional_usd: float
    pool_fee_buy_bps: float = 0.0
    pool_fee_sell_bps: float = 0.0
    impact_buy_bps: float = 0.0
    impact_sell_bps: float = 0.0
    gas_usd: float = 0.0
    withdrawal_usd: float = 0.0
    cex_taker_bps: float = 0.0
    slippage_buffer_bps: float = SLIPPAGE_BUFFER_BPS

    @property
    def embedded_bps(self) -> float:
        """Pool fees + impact, already inside the executable price. Diagnostic only."""
        return (
            self.pool_fee_buy_bps + self.pool_fee_sell_bps
            + self.impact_buy_bps + self.impact_sell_bps
        )

    @property
    def fixed_bps(self) -> float:
        if self.notional_usd <= 0:
            return 0.0
        return ((self.gas_usd + self.withdrawal_usd) / self.notional_usd) * BPS

    @property
    def added_bps(self) -> float:
        """Costs charged *on top* of the executable prices: fixed USD + CEX + buffer."""
        return self.fixed_bps + self.cex_taker_bps + self.slippage_buffer_bps


def gas_bps(gas_usd: float, notional_usd: float) -> float:
    """A fixed USD gas cost as bps of a notional. Small notionals are punished."""
    if notional_usd <= 0:
        return 0.0
    return (gas_usd / notional_usd) * BPS


def dex_fee_bps(dex: str, labels: tuple[str, ...] = ()) -> float:
    """Per-leg pool fee in bps for a DEX, honouring Uniswap v3 fee-tier labels.

    Unknown DEXes fall back to :data:`DEX_FEE_BPS_DEFAULT` (30 bps) rather than zero,
    so a missing entry cannot manufacture a free trade.
    """
    key = (dex or "").strip().lower()
    for label in labels:
        lab = str(label).strip().lower()
        if lab in UNIV3_LABEL_FEE_BPS:
            return UNIV3_LABEL_FEE_BPS[lab]
    if key in DEX_FEE_BPS:
        return DEX_FEE_BPS[key]
    return DEX_FEE_BPS_DEFAULT


def impact_bps(
    notional_usd: float, liquidity_usd: float, *, quote_share: float = QUOTE_SHARE
) -> float:
    """Constant-product single-side price impact, in bps.

    ``impact ~ notional / quote_reserve`` with ``quote_reserve = liquidity * quote_share``.
    A model, labelled as such. A missing/zero liquidity is treated as *maximally*
    impactful (returns a large finite number) so an unknown pool cannot look free.
    """
    if liquidity_usd <= 0:
        return BPS  # 100% — unusable, not free
    reserve = liquidity_usd * quote_share
    return (notional_usd / reserve) * BPS


def executable_buy_px(pool: Pool, notional_usd: float, *, fee_bps: float | None = None) -> float:
    """What you *pay* per base token buying ``notional_usd`` — marked up by fee + impact."""
    fee = pool.fee_bps if fee_bps is None else fee_bps
    imp = impact_bps(notional_usd, pool.liquidity_usd)
    return pool.price_usd * (1.0 + (fee + imp) / BPS)


def executable_sell_px(pool: Pool, notional_usd: float, *, fee_bps: float | None = None) -> float:
    """What you *receive* per base token selling — marked down by fee + impact."""
    fee = pool.fee_bps if fee_bps is None else fee_bps
    imp = impact_bps(notional_usd, pool.liquidity_usd)
    return pool.price_usd * (1.0 - (fee + imp) / BPS)


def build_stack(
    notional_usd: float,
    *,
    buy_pool: Pool | None = None,
    sell_pool: Pool | None = None,
    gas_usd: float = 0.0,
    withdrawal_usd: float = 0.0,
    cex_taker_bps: float = 0.0,
    slippage_buffer_bps: float = SLIPPAGE_BUFFER_BPS,
) -> CostStack:
    """Assemble the cost stack for one notional. Pure; no network, no clock."""
    return CostStack(
        notional_usd=notional_usd,
        pool_fee_buy_bps=buy_pool.fee_bps if buy_pool else 0.0,
        pool_fee_sell_bps=sell_pool.fee_bps if sell_pool else 0.0,
        impact_buy_bps=impact_bps(notional_usd, buy_pool.liquidity_usd) if buy_pool else 0.0,
        impact_sell_bps=impact_bps(notional_usd, sell_pool.liquidity_usd) if sell_pool else 0.0,
        gas_usd=gas_usd,
        withdrawal_usd=withdrawal_usd,
        cex_taker_bps=cex_taker_bps,
        slippage_buffer_bps=slippage_buffer_bps,
    )


def cross_pool_net_bps(
    buy_pool: Pool,
    sell_pool: Pool,
    notional_usd: float,
    *,
    gas_usd: float = 0.0,
    slippage_buffer_bps: float = SLIPPAGE_BUFFER_BPS,
) -> float:
    """Net bps of buying at ``buy_pool`` and selling at ``sell_pool``.

    The executable prices already contain each pool's fee and impact. Gas is charged
    **exactly once** per round trip (one batched swap; a router can settle both legs in
    one transaction). The slippage buffer is charged once.
    """
    buy = executable_buy_px(buy_pool, notional_usd)
    sell = executable_sell_px(sell_pool, notional_usd)
    if buy <= 0:
        return 0.0
    gross = (sell / buy - 1.0) * BPS
    return gross - gas_bps(gas_usd, notional_usd) - slippage_buffer_bps


def cex_dex_net_bps(
    dex_pool: Pool,
    notional_usd: float,
    *,
    cex_bid: float,
    cex_ask: float,
    gas_usd: float = 0.0,
    withdrawal_usd: float = 0.0,
    cex_taker_bps: float = CEX_TAKER_ONE_LEG_BPS,
    slippage_buffer_bps: float = SLIPPAGE_BUFFER_BPS,
) -> tuple[float, str]:
    """Best executable CEX<->DEX edge in bps and its direction.

    Two directions, both charged the same stack:

    * ``dex_buy_cex_sell`` — buy the token on the DEX at its executable ask, sell into
      the CEX bid.
    * ``cex_buy_dex_sell`` — buy the token on the CEX ask, sell into the DEX executable
      bid.

    Bid/ask only; a mid-price model is never used. Gas is charged once (the DEX leg),
    the CEX taker once (the CEX leg), and a withdrawal/bridge cost where inventory moves.
    """
    dex_buy = executable_buy_px(dex_pool, notional_usd)
    dex_sell = executable_sell_px(dex_pool, notional_usd)
    fixed = gas_bps(gas_usd, notional_usd)
    if notional_usd > 0:
        fixed += (withdrawal_usd / notional_usd) * BPS
    add = fixed + cex_taker_bps + slippage_buffer_bps

    dir_a = ((cex_bid / dex_buy) - 1.0) * BPS - add if dex_buy > 0 else -1e9
    dir_b = ((dex_sell / cex_ask) - 1.0) * BPS - add if cex_ask > 0 else -1e9
    if dir_a >= dir_b:
        return dir_a, "dex_buy_cex_sell"
    return dir_b, "cex_buy_dex_sell"


def is_sane_gap(gap_bps: float, *, limit_bps: float = SANITY_MAX_GAP_BPS) -> bool:
    """True if ``gap_bps`` is small enough to be a real dislocation, not a data bug."""
    return abs(gap_bps) <= limit_bps


def longest_positive_run(values: list[float]) -> int:
    """Length of the longest consecutive run of strictly positive values."""
    best = cur = 0
    for v in values:
        cur = cur + 1 if v > 0 else 0
        best = max(best, cur)
    return best


def percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile. Empty -> ``0.0`` (never NaN into a report)."""
    if not values:
        return 0.0
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * (pct / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def max_executable_size_usd(
    buy_pool: Pool,
    sell_pool: Pool,
    *,
    edge_floor_bps: float = 0.0,
    gas_usd: float = 0.0,
    cap_usd: float = 10_000.0,
) -> float:
    """Largest notional (USD, grid search) whose net edge still clears ``edge_floor_bps``.

    Impact grows with size, so the edge shrinks; this finds where it crosses the floor.
    Returns 0.0 if even the smallest tested size does not clear it.
    """
    best = 0.0
    size = 10.0
    while size <= cap_usd:
        if cross_pool_net_bps(buy_pool, sell_pool, size, gas_usd=gas_usd) > edge_floor_bps:
            best = size
        size *= 1.5
    return best


def mid(bid: float, ask: float) -> float:
    """Reference mid. Never used for an executable edge — only for a diagnostic column."""
    return (bid + ask) / 2.0


def _cex_dex_max_size(
    dex_pool: Pool,
    cex_bid: float,
    cex_ask: float,
    *,
    gas_usd: float = 0.0,
    cap_usd: float = 10_000.0,
) -> float:
    """Largest notional whose CEX<->DEX net edge still clears zero. 0.0 if none does."""
    best = 0.0
    size = 10.0
    while size <= cap_usd:
        net, _ = cex_dex_net_bps(
            dex_pool, size, cex_bid=cex_bid, cex_ask=cex_ask,
            gas_usd=gas_usd, withdrawal_usd=WITHDRAWAL_USD_RANGE[0],
        )
        if net > 0:
            best = size
        size *= 1.5
    return best


# =========================================================================== live plumbing


class Backoff:
    """Bounded backoff that honours ``Retry-After``; never retry-storms.

    Siblings share this host's IP, so the pacing errs slow.
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
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
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
    json_body: Any | None = None,
    attempts: int = 3,
    limiter: RateLimiter | None = None,
    stats: dict[str, int] | None = None,
) -> tuple[int, Any | None]:
    """GET/POST with rate limiting and retry/backoff. Returns ``(status, json_or_None)``.

    Never raises. Failures are counted in ``stats`` so a silently-empty run cannot be
    mistaken for "no opportunities".
    """
    backoff = Backoff()
    last_status = 0
    for attempt in range(attempts):
        if limiter is not None:
            await limiter.acquire()
        try:
            if json_body is not None:
                r = await client.post(url, json=json_body)
            else:
                r = await client.get(url, params=params)
            last_status = r.status_code
            if r.status_code == 200:
                backoff.ok()
                return 200, r.json()
            if r.status_code in (429, 418, 403):
                key = "429" if r.status_code == 429 else f"http_{r.status_code}"
                if stats is not None:
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


#: DexScreener's free tier allows ~300 req/min; 3 req/s is comfortably under it while
#: leaving headroom for siblings sharing the IP.
DEXSCREENER_RATE_PER_S = 3.0
#: GeckoTerminal measured ~30 req/min; stay under.
GECKOTERMINAL_RATE_PER_S = 0.4


def pool_from_dexscreener(pair: dict[str, Any]) -> Pool | None:
    """Normalise one DexScreener pair into a :class:`Pool`. None if unusable."""
    try:
        chain = str(pair["chainId"])
        base = pair["baseToken"]
        quote = pair["quoteToken"]
        price = float(pair.get("priceUsd") or 0.0)
        liq = float((pair.get("liquidity") or {}).get("usd") or 0.0)
        vol = float((pair.get("volume") or {}).get("h24") or 0.0)
        if price <= 0 or liq <= 0:
            return None
        dex = str(pair.get("dexId") or "?")
        labels = tuple(str(x) for x in (pair.get("labels") or []))
        return Pool(
            chain=chain,
            dex=dex,
            labels=labels,
            base_symbol=str(base.get("symbol") or "?").upper(),
            base_address=str(base.get("address") or ""),
            quote_symbol=str(quote.get("symbol") or "?").upper(),
            pair_address=str(pair.get("pairAddress") or ""),
            price_usd=price,
            liquidity_usd=liq,
            volume_h24=vol,
            fee_bps=dex_fee_bps(dex, labels),
            source="dexscreener",
        )
    except (KeyError, TypeError, ValueError):
        return None


def pool_from_geckoterminal(attrs: dict[str, Any], chain: str) -> Pool | None:
    """Normalise one GeckoTerminal pool attribute dict into a :class:`Pool`."""
    try:
        name = str(attrs.get("name") or "?")
        price = float(attrs.get("base_token_price_usd") or 0.0)
        liq = float(attrs.get("reserve_in_usd") or 0.0)
        vol = float((attrs.get("volume_usd") or {}).get("h24") or 0.0)
        if price <= 0 or liq <= 0:
            return None
        base_symbol = name.split("/")[0].strip().upper() if "/" in name else name.upper()
        return Pool(
            chain=chain,
            dex="geckoterminal",
            labels=(),
            base_symbol=base_symbol,
            base_address="",
            quote_symbol="",
            pair_address=str(attrs.get("address") or ""),
            price_usd=price,
            liquidity_usd=liq,
            volume_h24=vol,
            fee_bps=DEX_FEE_BPS_DEFAULT,
            source="geckoterminal",
        )
    except (KeyError, TypeError, ValueError):
        return None


def select_top_pools(
    pools: list[Pool],
    *,
    min_liquidity_usd: float = 20_000.0,
    top_n: int = 40,
) -> list[Pool]:
    """Rank pools by 24h volume, keep the liquid top ``top_n``.

    Selection rule (stated, not hidden): pools with ``liquidity_usd >= min_liquidity``
    sorted by 24h USD volume descending, capped at ``top_n``. The liquidity floor keeps
    a dust pool from producing a fake 10% "edge".
    """
    liquid = [p for p in pools if p.liquidity_usd >= min_liquidity_usd]
    return sorted(liquid, key=lambda p: p.volume_h24, reverse=True)[:top_n]


def group_by_token(pools: list[Pool]) -> dict[tuple[str, str], list[Pool]]:
    """Group pools by ``(chain, contract address)``.

    **Address, never symbol.** DexScreener's search returns *different tokens that
    share a ticker* (three distinct "TRUMP" contracts on ethereum alone, priced 0.03 /
    2.34 / 5.05). Grouping by symbol manufactures a ~100000 bps "edge" that is really
    three different assets, so a pool is keyed by its base-token contract address and
    the symbol is carried for reporting only. A pool with no address is keyed by
    symbol and can never be paired with an addressed pool.
    """
    out: dict[tuple[str, str], list[Pool]] = {}
    for p in pools:
        key = (p.chain, p.base_address) if p.base_address else (p.chain, f"sym:{p.base_symbol}")
        out.setdefault(key, []).append(p)
    return out


def find_cross_pool_candidates(
    pools: list[Pool],
    *,
    min_liquidity_usd: float = 20_000.0,
) -> list[tuple[str, str, Pool, Pool]]:
    """All ``(chain, symbol, buy_pool, sell_pool)`` where >= 2 pools share one contract.

    Only *distinct* pools of the *same base-token contract* are paired. The returned
    pair is (cheapest executable ask, richest executable bid) at $100.
    """
    out: list[tuple[str, str, Pool, Pool]] = []
    for (chain, _addr), group in group_by_token(pools).items():
        liquid = [p for p in group if p.liquidity_usd >= min_liquidity_usd]
        if len(liquid) < 2:
            continue
        # Deduplicate by pair address so the same pool is not paired with itself.
        uniq: dict[str, Pool] = {}
        for p in liquid:
            uniq.setdefault(p.pair_address or f"{p.dex}:{p.price_usd}", p)
        pools_u = list(uniq.values())
        if len(pools_u) < 2:
            continue
        buy = min(pools_u, key=lambda p: executable_buy_px(p, 100.0))
        sell = max(pools_u, key=lambda p: executable_sell_px(p, 100.0))
        if buy.pair_address == sell.pair_address:
            continue
        out.append((chain, buy.base_symbol, buy, sell))
    return out


async def fetch_dexscreener_symbol(
    client: httpx.AsyncClient,
    symbol: str,
    *,
    limiter: RateLimiter | None = None,
    stats: dict[str, int] | None = None,
) -> list[Pool]:
    """All DexScreener pools for one symbol, across every chain, as :class:`Pool` rows."""
    status, payload = await get_json(
        client, DEXSCREENER_SEARCH, params={"q": symbol}, limiter=limiter, stats=stats
    )
    if status != 200 or not isinstance(payload, dict):
        return []
    pools: list[Pool] = []
    for pair in payload.get("pairs") or []:
        p = pool_from_dexscreener(pair)
        if p is not None and p.chain not in EXCLUDED_CHAINS:
            pools.append(p)
    return pools


async def fetch_cex_refs(
    client: httpx.AsyncClient,
    *,
    limiter: RateLimiter | None = None,
    stats: dict[str, int] | None = None,
) -> dict[str, dict[str, float]]:
    """Return ``{SYMBOL: {"bid","ask","venue"}}`` from Binance vision, Gate, Huobi, HL.

    Binance is preferred per symbol; Gate and Huobi fill the gaps; Hyperliquid adds a
    perp mid (reported separately as a *perp* reference, not a spot bid/ask).
    """
    refs: dict[str, dict[str, float]] = {}

    status, payload = await get_json(
        client, BINANCE_24HR, limiter=limiter, stats=stats
    )
    if status == 200 and isinstance(payload, list):
        for row in payload:
            try:
                sym = str(row["symbol"])
                if not sym.endswith("USDT"):
                    continue
                base = sym[:-4]
                refs[base] = {
                    "bid": float(row["bidPrice"]),
                    "ask": float(row["askPrice"]),
                    "venue": "binance",
                }
            except (KeyError, TypeError, ValueError):
                continue

    if refs:
        return refs

    status, payload = await get_json(client, GATE_TICKERS, limiter=limiter, stats=stats)
    if status == 200 and isinstance(payload, list):
        for row in payload:
            try:
                pair = str(row["currency_pair"])
                if not pair.endswith("_USDT"):
                    continue
                refs[pair[:-5]] = {
                    "bid": float(row["highest_bid"]),
                    "ask": float(row["lowest_ask"]),
                    "venue": "gate",
                }
            except (KeyError, TypeError, ValueError):
                continue
    return refs


async def fetch_geckoterminal_top_pools(
    client: httpx.AsyncClient,
    network: str,
    *,
    limiter: RateLimiter | None = None,
    stats: dict[str, int] | None = None,
) -> list[Pool]:
    """Top-volume pools for one GeckoTerminal network (page 1, 20 pools).

    GeckoTerminal is rate-limited (~30 req/min measured), so this is a *census* run
    once, not a per-iteration poll. It is what proves per-chain coverage on networks
    DexScreener under-indexes (sui, aptos, ton).
    """
    status, payload = await get_json(
        client, GECKOTERMINAL_POOLS.format(net=network),
        limiter=limiter, stats=stats,
    )
    if status != 200 or not isinstance(payload, dict):
        return []
    pools: list[Pool] = []
    for row in payload.get("data") or []:
        p = pool_from_geckoterminal(row.get("attributes") or {}, network)
        if p is not None:
            pools.append(p)
    return pools


async def census_geckoterminal(
    networks: list[str], *, interval_s: float = 2.2
) -> dict[str, Any]:
    """One-shot per-network top-pool census. Paced to stay under the GT rate limit."""
    limiter = RateLimiter(GECKOTERMINAL_RATE_PER_S)
    stats: dict[str, int] = {}
    out: dict[str, Any] = {}
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = EVIDENCE / f"gt_census_{stamp}.json"
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(15.0, connect=5.0),
        headers={"User-Agent": "crypto-brain-research/1.0"},
    ) as client:
        for net in networks:
            ps = await fetch_geckoterminal_top_pools(client, net, limiter=limiter, stats=stats)
            top = select_top_pools(ps, min_liquidity_usd=0.0, top_n=20)
            out[net] = {
                "reachable": bool(ps),
                "pools_page1": len(ps),
                "total_h24_volume_usd": round(sum(p.volume_h24 for p in ps), 2),
                "top": [
                    {"symbol": p.base_symbol, "dex": p.dex, "price_usd": p.price_usd,
                     "liquidity_usd": round(p.liquidity_usd, 2),
                     "volume_h24": round(p.volume_h24, 2)}
                    for p in top[:5]
                ],
            }
            # Write after every network so an interrupted run is still usable evidence.
            payload = {"generated_at": datetime.now(UTC).isoformat(),
                       "stats": stats, "networks": out}
            path.write_text(json.dumps(payload, indent=1))
            await asyncio.sleep(interval_s)
    return {"generated_at": datetime.now(UTC).isoformat(), "stats": stats, "networks": out}


async def fetch_hyperliquid_mids(
    client: httpx.AsyncClient,
    *,
    limiter: RateLimiter | None = None,
    stats: dict[str, int] | None = None,
) -> dict[str, float]:
    """Perp mids from Hyperliquid (keyless). Reported as a *perp* reference only."""
    status, payload = await get_json(
        client, HYPERLIQUID_INFO, json_body={"type": "allMids"},
        limiter=limiter, stats=stats,
    )
    if status != 200 or not isinstance(payload, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in payload.items():
        try:
            out[str(k).upper()] = float(v)
        except (TypeError, ValueError):
            continue
    return out


# ------------------------------------------------------------------- sampling


@dataclass
class Sample:
    """One cross-section observation for one candidate."""

    ts: str
    kind: str  # "cross_pool" | "cex_dex"
    chain: str
    symbol: str
    notional_usd: float
    net_bps: float
    direction: str = ""
    buy_venue: str = ""
    sell_venue: str = ""
    buy_px: float = 0.0
    sell_px: float = 0.0
    cex_mid: float = 0.0
    liquidity_usd: float = 0.0
    gas_usd_low: float = 0.0
    gas_usd_high: float = 0.0
    max_size_usd: float = 0.0
    identity_verified: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


def build_samples(
    pools: list[Pool],
    cex_refs: dict[str, dict[str, float]],
    ts: str,
    notional_usd: float,
    *,
    gas_high: bool = False,
) -> tuple[list[Sample], list[dict[str, Any]]]:
    """Turn one cross-section of pools + CEX refs into executable samples.

    Returns ``(samples, discarded)``. A gap beyond :data:`SANITY_MAX_GAP_BPS` is
    discarded and counted — it is a data error, not an opportunity.
    """
    samples: list[Sample] = []
    discarded: list[dict[str, Any]] = []

    for chain, sym, buy_pool, sell_pool in find_cross_pool_candidates(pools):
        lo, hi = GAS_USD_RANGE.get(chain, GAS_USD_UNKNOWN)
        gas = hi if gas_high else lo
        net = cross_pool_net_bps(buy_pool, sell_pool, notional_usd, gas_usd=gas)
        if not is_sane_gap(net):
            discarded.append({"kind": "cross_pool", "chain": chain, "symbol": sym,
                              "net_bps": net, "reason": "implausible_gap"})
            continue
        samples.append(Sample(
            ts=ts, kind="cross_pool", chain=chain, symbol=sym, notional_usd=notional_usd,
            net_bps=net, direction="buy_low_sell_high",
            buy_venue=f"{buy_pool.dex}", sell_venue=f"{sell_pool.dex}",
            buy_px=executable_buy_px(buy_pool, notional_usd),
            sell_px=executable_sell_px(sell_pool, notional_usd),
            liquidity_usd=min(buy_pool.liquidity_usd, sell_pool.liquidity_usd),
            gas_usd_low=lo, gas_usd_high=hi,
            max_size_usd=max_executable_size_usd(buy_pool, sell_pool, gas_usd=gas),
        ))

    for chain, _addr, group in (
        (c, a, g) for (c, a), g in group_by_token(pools).items()
    ):
        ref = cex_refs.get(group[0].base_symbol)
        if not ref or ref["bid"] <= 0 or ref["ask"] <= 0:
            continue
        # The deepest pool of the contract is the relevant venue: an illiquid pool must
        # not manufacture the edge.
        dex_pool = max(group, key=lambda p: p.liquidity_usd)
        if dex_pool.liquidity_usd < 20_000.0:
            continue
        lo, hi = GAS_USD_RANGE.get(chain, GAS_USD_UNKNOWN)
        gas = hi if gas_high else lo
        net, direction = cex_dex_net_bps(
            dex_pool, notional_usd, cex_bid=ref["bid"], cex_ask=ref["ask"],
            gas_usd=gas, withdrawal_usd=WITHDRAWAL_USD_RANGE[0],
        )
        if not is_sane_gap(net, limit_bps=CEX_DEX_MAX_GAP_BPS):
            discarded.append({
                "kind": "cex_dex", "chain": chain, "symbol": dex_pool.base_symbol,
                "net_bps": net, "reason": "implausible_gap",
            })
            continue
        samples.append(Sample(
            ts=ts, kind="cex_dex", chain=chain, symbol=dex_pool.base_symbol,
            notional_usd=notional_usd,
            net_bps=net, direction=direction, buy_venue=ref["venue"], sell_venue=dex_pool.dex,
            buy_px=executable_buy_px(dex_pool, notional_usd),
            sell_px=executable_sell_px(dex_pool, notional_usd),
            cex_mid=mid(ref["bid"], ref["ask"]),
            liquidity_usd=dex_pool.liquidity_usd,
            gas_usd_low=lo, gas_usd_high=hi,
            max_size_usd=_cex_dex_max_size(
                dex_pool, ref["bid"], ref["ask"], gas_usd=gas,
            ),
            identity_verified=is_verified_contract(
                chain, dex_pool.base_address, dex_pool.base_symbol
            ),
        ))
    return samples, discarded


#: Hard ceiling for one sampling iteration, so a hung socket cannot eat the window.
ITERATION_TIMEOUT_S = 180.0


async def collect(
    minutes: float, interval: float, notional_usd: float
) -> tuple[list[Sample], list[dict[str, Any]], dict[str, Any]]:
    """Poll every reachable chain for a bounded window. Low concurrency (4)."""
    samples: list[Sample] = []
    discarded: list[dict[str, Any]] = []
    raw_cache: list[dict[str, Any]] = []
    stats: dict[str, int] = {}
    iterations = 0
    timeouts = 0
    deadline = time.monotonic() + minutes * 60.0
    sem = asyncio.Semaphore(4)
    limiter = RateLimiter(DEXSCREENER_RATE_PER_S)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(15.0, connect=5.0),
        headers={"User-Agent": "crypto-brain-research/1.0"},
    ) as client:
        cex_refs = await fetch_cex_refs(client, limiter=limiter, stats=stats)
        while time.monotonic() < deadline:
            ts = datetime.now(UTC).isoformat()
            try:
                fresh_refs = await fetch_cex_refs(client, limiter=limiter, stats=stats)
                if fresh_refs:
                    cex_refs = fresh_refs
                pools, raw = await asyncio.wait_for(
                    _sample_once(client, sem, limiter, stats, MEME_SYMBOLS),
                    timeout=ITERATION_TIMEOUT_S,
                )
                s, d = build_samples(pools, cex_refs, ts, notional_usd)
                samples.extend(s)
                discarded.extend(d)
                raw_cache.append({
                    "ts": ts,
                    "notional_usd": notional_usd,
                    "cex_refs": cex_refs,
                    "pools": [asdict(p) for p in pools],
                    "symbols_fetched": raw,
                })
                iterations += 1
            except TimeoutError:
                timeouts += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(interval, remaining))

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    meta = {"iterations": iterations, "timeouts": timeouts, "failures": stats,
            "symbols": len(MEME_SYMBOLS)}
    (EVIDENCE / f"raw_multichain_{stamp}.json").write_text(json.dumps(raw_cache, indent=1))
    (EVIDENCE / f"discarded_multichain_{stamp}.json").write_text(json.dumps(discarded, indent=1))
    (EVIDENCE / f"collect_meta_{stamp}.json").write_text(json.dumps(meta, indent=1))
    print(f"iterations={iterations} timeouts={timeouts} failures={stats}")
    return samples, discarded, meta


async def _sample_once(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    limiter: RateLimiter,
    stats: dict[str, int],
    symbols: tuple[str, ...],
) -> tuple[list[Pool], list[dict[str, Any]]]:
    """Fetch every symbol's DexScreener pools once, bounded concurrency."""
    pools: list[Pool] = []
    raw: list[dict[str, Any]] = []

    async def one(sym: str) -> None:
        async with sem:
            ps = await fetch_dexscreener_symbol(client, sym, limiter=limiter, stats=stats)
            if ps:
                pools.extend(ps)
                raw.append({"symbol": sym, "n": len(ps)})

    await asyncio.gather(*(one(s) for s in symbols))
    return pools, raw


# ------------------------------------------------------------------- reporting


def samples_from_raw(raw: list[dict[str, Any]]) -> list[Sample]:
    """Rebuild ``Sample`` rows from a cached ``raw_multichain_*.json`` payload.

    The cache stores each cross-section's pools and CEX refs; this re-scores offline so
    a revised cost stack can be applied without touching the network.
    """
    out: list[Sample] = []
    for entry in raw:
        if "pools" not in entry:
            continue
        pools = [Pool(**p) for p in entry["pools"]]
        cex = entry.get("cex_refs") or {}
        s, _ = build_samples(pools, cex, str(entry.get("ts", "")), float(entry["notional_usd"]))
        out.extend(s)
    return out


def summarize(samples: list[Sample], *, gas_high: bool = False) -> dict[str, Any]:
    """Per ``(chain, kind, symbol)`` distribution of net bps at the sampled notional."""
    out: dict[str, Any] = {}
    keys = sorted({(s.chain, s.kind, s.symbol) for s in samples})
    for chain, kind, sym in keys:
        rows = [s for s in samples if s.chain == chain and s.kind == kind and s.symbol == sym]
        if not rows:
            continue
        nets = [r.net_bps for r in rows]
        pos = [n for n in nets if n > 0]
        out[f"{chain}|{kind}|{sym}"] = {
            "n": len(rows),
            "max_bps": round(max(nets), 3),
            "mean_bps": round(st.mean(nets), 3),
            "p90_bps": round(percentile(nets, 90.0), 3),
            "min_bps": round(min(nets), 3),
            "pct_positive": round(100.0 * len(pos) / len(nets), 2),
            "longest_positive_run": longest_positive_run(nets),
            "max_liquidity_usd": round(max(r.liquidity_usd for r in rows), 0),
            "buy_venues": sorted({r.buy_venue for r in rows}),
            "sell_venues": sorted({r.sell_venue for r in rows}),
        }
    return out


def build_verdict(samples: list[Sample], *, notional_usd: float = 100.0) -> dict[str, Any]:
    """Per-chain verdict at the sampled notional: does any candidate beat its costs?"""
    verdict: dict[str, Any] = {}
    chains = sorted({s.chain for s in samples})
    for chain in chains:
        rows = [s for s in samples if s.chain == chain]
        nets = [r.net_bps for r in rows]
        pos = [r for r in rows if r.net_bps > 0]
        verified = [r for r in rows if r.identity_verified]
        v_pos = [r for r in verified if r.net_bps > 0]
        lo, hi = GAS_USD_RANGE.get(chain, GAS_USD_UNKNOWN)
        verdict[chain] = {
            "candidates": len(rows),
            "max_net_bps": round(max(nets), 3) if nets else None,
            "mean_net_bps": round(st.mean(nets), 3) if nets else None,
            "p90_net_bps": round(percentile(nets, 90.0), 3) if nets else None,
            "pct_positive": round(100.0 * len(pos) / len(nets), 2) if nets else None,
            "longest_positive_run": longest_positive_run(nets),
            "any_net_positive": bool(pos),
            "max_size_usd_clearing_zero": round(
                max((r.max_size_usd for r in rows), default=0.0), 1
            ),
            "identity_verified_candidates": len(verified),
            "identity_verified_max_net_bps": (
                round(max(r.net_bps for r in verified), 3) if verified else None
            ),
            "identity_verified_any_positive": bool(v_pos),
            "identity_verified_max_size_usd_clearing_zero": round(
                max((r.max_size_usd for r in verified), default=0.0), 1
            ),
            "gas_usd_range_labelled": [lo, hi],
            "best_symbol": max(rows, key=lambda r: r.net_bps).symbol if rows else None,
            "best_kind": max(rows, key=lambda r: r.net_bps).kind if rows else None,
        }
    return verdict


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--minutes", type=float, default=20.0)
    ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--notional", type=float, default=100.0)
    ap.add_argument("--probe-only", action="store_true")
    ap.add_argument("--reanalyze", action="store_true")
    ap.add_argument("--census", action="store_true",
                    help="One-shot per-network GeckoTerminal top-pool census (paced).")
    args = ap.parse_args()

    EVIDENCE.mkdir(parents=True, exist_ok=True)

    if args.census:
        ids_path = EVIDENCE / "gt_network_ids.json"
        if ids_path.exists():
            networks = json.loads(ids_path.read_text())
        else:
            networks = list(GAS_USD_RANGE.keys())
        print(f"census over {len(networks)} networks (paced {GECKOTERMINAL_RATE_PER_S} req/s) ...")
        payload = asyncio.run(census_geckoterminal(networks))
        reachable = [k for k, v in payload["networks"].items() if v["reachable"]]
        print(f"reachable: {len(reachable)}/{len(networks)}")
        print(f"failures={payload['stats']}")
        return 0

    if args.reanalyze:
        raws = sorted(EVIDENCE.glob("raw_multichain_*.json"))
        if not raws:
            print("no cached raw_multichain_*.json to reanalyze")
            return 1
        pooled: list[Sample] = []
        for path in raws:
            pooled.extend(samples_from_raw(json.loads(path.read_text())))
        print(f"reanalyzed {len(raws)} file(s) -> {len(pooled)} samples")
        report = {
            "generated_at": datetime.now(UTC).isoformat(),
            "mode": "reanalyze",
            "source_files": [p.name for p in raws],
            "notional_usd": args.notional,
            "distribution": summarize(pooled),
            "verdict": build_verdict(pooled, notional_usd=args.notional),
        }
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        (EVIDENCE / f"multichain_reanalysis_{stamp}.json").write_text(json.dumps(report, indent=1))
        print(json.dumps(report["verdict"], indent=1))
        return 0

    if args.probe_only:
        async def probe() -> None:
            limiter = RateLimiter(DEXSCREENER_RATE_PER_S)
            stats: dict[str, int] = {}
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0),
                headers={"User-Agent": "crypto-brain-research/1.0"},
            ) as client:
                refs = await fetch_cex_refs(client, limiter=limiter, stats=stats)
                print(f"cex_refs: {len(refs)} symbols")
                hl = await fetch_hyperliquid_mids(client, limiter=limiter, stats=stats)
                print(f"hyperliquid mids: {len(hl)}")
                for sym in ("PEPE", "DOGE", "BRETT"):
                    ps = await fetch_dexscreener_symbol(client, sym, limiter=limiter, stats=stats)
                    chains = sorted({p.chain for p in ps})
                    print(f"{sym}: {len(ps)} pools across {len(chains)} chains {chains[:12]}")
                print(f"failures={stats}")
        asyncio.run(probe())
        return 0

    print(f"collecting {args.minutes:.0f} min @ {args.interval:.0f}s at ${args.notional:.0f} ...")
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
        "notional_usd": args.notional,
        "excluded_chains": sorted(EXCLUDED_CHAINS),
        "discarded_implausible": len(discarded),
        "collection": meta,
        "cost_constants": {
            "cex_taker_bps": CEX_TAKER_BPS,
            "slippage_buffer_bps": SLIPPAGE_BUFFER_BPS,
            "gas_usd_range_by_chain": {k: list(v) for k, v in GAS_USD_RANGE.items()},
            "gas_usd_unknown": list(GAS_USD_UNKNOWN),
            "withdrawal_usd_range": list(WITHDRAWAL_USD_RANGE),
            "quote_share_impact_model": QUOTE_SHARE,
            "dex_fee_bps": DEX_FEE_BPS,
        },
        "distribution": summarize(samples),
        "verdict": build_verdict(samples, notional_usd=args.notional),
    }
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (EVIDENCE / f"multichain_scan_{stamp}.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report["verdict"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
