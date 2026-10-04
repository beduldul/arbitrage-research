# Scripts

The scanners that produced the reports in [`../reports/`](../reports/) and the evidence in
[`../evidence/`](../evidence/). They are copied **verbatim** from the source project.

> **Measurement only.** These scripts place no orders and use no keys, wallets or account
> state. They hit public, unauthenticated endpoints and cache raw responses to disk.

## Requirements

- Python 3.11+ (uses `datetime.UTC`, `X | None` syntax).
- `httpx` for the async scanners (`pip install httpx`).

## Import path

Most scanners import the project's cost model:

```python
from crypto_brain.engine.fees import FeeSchedule
from crypto_brain.engine.cost_model import round_trip_cost_pct
from crypto_brain.engine.slippage import MIN_QUOTE_VOLUME_USD, SymbolCostProfile
```

A **vendored subset** of those three modules is in
[`_vendor/crypto_brain/engine/`](_vendor/crypto_brain/engine/) (cost-model assumptions
only — no strategy, no order path). To run a scanner:

```bash
cd scripts
PYTHONPATH="_vendor:$PYTHONPATH" python arb_funding_spread.py --help
```

Each script's `--help` documents its own subcommands (e.g. `arb_partition_test.py run`).
Some scripts also import each other by bare module name (e.g. `import arb_common`), so run
them from this directory or add it to `PYTHONPATH`.

## Not included

- `arb_triangular_scan.py` imports `crypto_brain.data.sources.binance_spot` (a CEX
  candle loader) — not vendored; the triangular measurement itself uses `arb_common.py`.
- `arb_paper_sim.py` and `arb_meme_sim.py` import `crypto_brain.arb.simulator` /
  `crypto_brain.arb.meme` (the paper-simulator core) — not vendored; the simulator is
  paper-only and its SYNTHETIC-mode output is labelled as arithmetic, not market data.
- Unit tests (`tests/unit/test_arb_*.py` in the source project) are not included; they
  pinned the pure-computation behaviour of these modules.

## Index

See the reproduction index in the top-level [`README.md`](../README.md).
