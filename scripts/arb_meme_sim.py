#!/usr/bin/env python3
"""CLI — price a meme cross-venue round trip on a $100 paper account (PAPER ONLY).

Consumes the meme evidence the measurement workstreams publish and prints/writes an
auditable PnL answer. **No network, no keys, no order path.**

Modes, always printed:

* ``--mode real`` — prices measured opportunities from
  ``evidence/arbitrage/2026-10-03/meme/**`` (or ``--window-file``). Output stamped
  ``REAL MEASURED INPUT``.
* ``--mode synthetic`` — prices the built-in fixture. Output stamped
  ``SYNTHETIC INPUT — NOT EVIDENCE`` and the run states plainly that it proves the
  arithmetic, not the market.

The script refuses to print a profit line without the mode label attached.

Examples
--------
    uv run python scripts/arb_meme_sim.py --chain solana --capital 100
    uv run python scripts/arb_meme_sim.py --chain base --mode synthetic --top 3
    uv run python scripts/arb_meme_sim.py --chain solana --window-file path/to/rows.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from crypto_brain.arb.meme import (  # noqa: E402
    DEFAULT_EVIDENCE_DIR,
    MEME_EVIDENCE_SUBDIR,
    REAL_STAMP,
    SYNTHETIC_STAMP,
    fixture_opportunities,
    format_report,
    load_evidence,
    simulate_meme,
    with_warnings,
)

CHAIN_CHOICES = ["solana", "base", "bsc", "eth", "arbitrum", "polygon", "avalanche"]


def _load_window_file(path: Path, chain: str) -> tuple[list, list[str]]:
    """Load opportunities from one explicit evidence file (REAL mode)."""
    from crypto_brain.arb.meme import opportunities_from_payload

    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = opportunities_from_payload(payload, path)
    kept = [row for row in rows if row.chain == chain or row.entry.chain == chain]
    return kept, ([str(path)] if kept else [])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--chain", default="solana", choices=CHAIN_CHOICES)
    parser.add_argument("--capital", type=float, default=100.0)
    parser.add_argument("--top", type=int, default=None, help="price only the top N spreads")
    parser.add_argument(
        "--window-file",
        type=Path,
        default=None,
        help="an explicit evidence file of meme opportunities (implies REAL mode)",
    )
    parser.add_argument("--mode", default="auto", choices=["auto", "real", "synthetic"])
    parser.add_argument("--evidence-dir", type=Path, default=REPO_ROOT / DEFAULT_EVIDENCE_DIR)
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / DEFAULT_EVIDENCE_DIR / MEME_EVIDENCE_SUBDIR / "sim_meme.json",
    )
    args = parser.parse_args(argv)

    warnings: list[str] = []
    source_files: tuple[str, ...] = ()

    if args.window_file is not None:
        if not args.window_file.exists():
            print(
                f"ERROR: --window-file {args.window_file} does not exist. Refusing to "
                "substitute synthetic data.",
                file=sys.stderr,
            )
            return 2
        opportunities, source_files = _load_window_file(args.window_file, args.chain)
        mode = "REAL"
        if not opportunities:
            print(
                f"ERROR: --window-file {args.window_file} contained no opportunities for "
                f"chain {args.chain}. Refusing to substitute synthetic data.",
                file=sys.stderr,
            )
            return 2
    elif args.mode in ("auto", "real"):
        real_rows, source_files = load_evidence(args.chain, args.evidence_dir)
        if real_rows:
            opportunities = real_rows
            mode = "REAL"
        elif args.mode == "real":
            print(
                "ERROR: --mode real requested but no meme evidence for chain "
                f"{args.chain} was found under {args.evidence_dir}. Refusing to substitute "
                "synthetic data.",
                file=sys.stderr,
            )
            return 2
        else:
            opportunities = fixture_opportunities()
            mode = "SYNTHETIC"
            warnings.append(
                "no measured meme evidence found; fell back to the synthetic fixture to "
                "prove the arithmetic only"
            )
    else:
        opportunities = fixture_opportunities()
        mode = "SYNTHETIC"

    result = simulate_meme(
        opportunities,
        chain=args.chain,
        capital=args.capital,
        top_n=args.top,
        mode=mode,
        source_files=source_files,
    )
    result = with_warnings(result, warnings)

    print(format_report(result))
    if mode == "SYNTHETIC":
        print(
            f"\n[{SYNTHETIC_STAMP}] This run proves the simulator's arithmetic on a fixture. "
            "It makes NO claim about any real market or about profitability."
        )
    else:
        joined = ", ".join(source_files) or "(inline)"
        print(f"\n[{REAL_STAMP}] Priced from measured evidence: {joined}.")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result.as_dict(), indent=1), encoding="utf-8")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
