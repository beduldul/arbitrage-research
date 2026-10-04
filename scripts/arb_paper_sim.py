#!/usr/bin/env python3
"""CLI — replay a funding-carry strategy on a $100 paper account (PAPER ONLY).

Consumes the evidence the measurement workstreams publish and prints/writes an auditable
PnL answer. **No network, no keys, no order path.**

Modes, always printed:

* ``--mode real`` (default when evidence exists) — replays measured funding history from
  ``evidence/arbitrage/2026-10-03/``. Output stamped ``REAL MEASURED INPUT``.
* ``--mode synthetic`` — replays the built-in fixture. Output stamped
  ``SYNTHETIC INPUT — NOT EVIDENCE`` and the run states plainly that it proves the
  arithmetic, not the market.

The script refuses to print a profit line without the mode label attached.

Examples
--------
    uv run python scripts/arb_paper_sim.py --strategy funding --capital 100 --pairs top10
    uv run python scripts/arb_paper_sim.py --strategy funding --mode synthetic --days 2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from crypto_brain.arb.simulator import (  # noqa: E402
    DEFAULT_EVIDENCE_DIR,
    REAL_STAMP,
    SYNTHETIC_STAMP,
    SimulationResult,
    fixture_pairs,
    format_report,
    load_evidence,
    simulate_funding,
)


def _select_pairs(pairs: list, pairs_arg: str, top_n: int = 10) -> list:
    """``top10`` takes the ten longest funding histories; ``all`` takes everything."""
    if pairs_arg == "all":
        return list(pairs)
    ranked = sorted(pairs, key=lambda pair: len(pair.rates), reverse=True)
    return ranked[:top_n]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strategy", default="funding", choices=["funding"])
    parser.add_argument("--capital", type=float, default=100.0)
    parser.add_argument("--pairs", default="top10", choices=["top10", "all"])
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--tier", default="vip0_taker", choices=["vip0_taker", "maker"])
    parser.add_argument("--mode", default="auto", choices=["auto", "real", "synthetic"])
    parser.add_argument("--leverage", type=float, default=1.0)
    parser.add_argument(
        "--basis-shock-bps",
        type=float,
        default=0.0,
        help="stated scenario: mark the perp up by this many bps against spot",
    )
    parser.add_argument("--evidence-dir", type=Path, default=REPO_ROOT / DEFAULT_EVIDENCE_DIR)
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / DEFAULT_EVIDENCE_DIR / "sim_funding.json",
    )
    args = parser.parse_args(argv)

    warnings: list[str] = []
    source_files: tuple[str, ...] = ()
    if args.mode in ("auto", "real"):
        real_pairs, source_files = load_evidence(args.evidence_dir)
        if real_pairs:
            pairs = _select_pairs(real_pairs, args.pairs)
            mode = "REAL"
        elif args.mode == "real":
            print(
                "ERROR: --mode real requested but no replayable evidence was found under "
                f"{args.evidence_dir}. Refusing to substitute synthetic data.",
                file=sys.stderr,
            )
            return 2
        else:
            pairs = _select_pairs(fixture_pairs(), args.pairs)
            mode = "SYNTHETIC"
            warnings.append(
                "no measured evidence found; fell back to the synthetic fixture to prove "
                "the arithmetic only"
            )
    else:
        pairs = _select_pairs(fixture_pairs(), args.pairs)
        mode = "SYNTHETIC"

    result = simulate_funding(
        pairs,
        capital=args.capital,
        days=args.days,
        tier=args.tier,
        leverage=args.leverage,
        basis_shock_bps=args.basis_shock_bps,
        mode=mode,
        source_files=source_files,
    )
    result = _with_warnings(result, warnings)

    print(format_report(result))
    if mode == "SYNTHETIC":
        print(
            f"\n[{SYNTHETIC_STAMP}] This run proves the simulator's arithmetic on a fixture. "
            "It makes NO claim about any real market or about profitability."
        )
    else:
        joined = ", ".join(source_files) or "(inline)"
        print(f"\n[{REAL_STAMP}] Replayed from measured evidence: {joined}.")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result.as_dict(), indent=1), encoding="utf-8")
    print(f"\nwrote {args.out}")
    return 0


def _with_warnings(result: SimulationResult, extra: list[str]) -> SimulationResult:
    if not extra:
        return result
    from dataclasses import replace

    return replace(result, warnings=tuple(result.warnings) + tuple(extra))


if __name__ == "__main__":
    raise SystemExit(main())
