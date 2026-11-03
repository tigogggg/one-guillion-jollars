# The engine (Rust core, Python around it)

One engine trades for every agent, in the pre-training and live: portfolios, orders, fills at the open with the gap
rule, stops and targets, trailing stops, the shared rules (market context, technical and fundamental filters, ATR and
support stops, time stop), the risk rules (volatility sizing, sector cap, drawdown brake), taxes, points, bankruptcy
and the strategies. It is being moved from Python (`scripts/agents.py`) to Rust (`engine/`, Python module
`ogj_engine`), in phases. Everything that waits on the network (prices, news, SEC, FRED, Claude, Discord, charts)
stays in Python.

```
scripts/pretrain.py ──► scripts/engine.py ──► ogj_engine (Rust: engine/src)
  search, scoring         builds the data once       day loop: fills, exits, rules,
  (profiles, exams)       (numpy columns)            strategies, taxes, points
```

## Rules of the migration

- **Same results, to the cent.** A strategy runs in Rust only after `tests/test_engine.py` shows the same trades
  (date, side, symbol, quantity, price, euros, result, reason), the same daily equity, points and deepest fall as the
  Python reference, with several parameter sets (shared rules on and off, regions, zones, currencies, pence quotes,
  earnings). The port reproduces Python's semantics where they matter: `round()` half-even on the exact binary value,
  the compensated float `sum()` of Python 3.12+, `min`/`max` with NaN, truthiness of NaN, missing fields as `None`.
- **One engine.** Python's copy of the engine is deleted once every strategy and the live rounds run on Rust.
- `OGJ_ENGINE=python` forces the Python reference (to compare or to rule the engine out).

## Phases

1. Engine core and the first strategy (Foguetão) — done: ~50x faster per simulation than the Python loop.
2. The other trainable strategies, one at a time (`engine/src/strategies.rs`, `SUPPORTED`).
3. The pre-training's search in Rust too (candidates on all cores with rayon, smarter search).
4. The live rounds (`agents.react`, the live-only strategies), then the Python engine is removed.

## Build

`uv sync` builds `engine/` with maturin (Rust toolchain needed; GitHub's runners have it). `uv` rebuilds it when
`engine/src/**/*.rs` or `Cargo.toml` change. Rust unit tests: `cd engine && cargo test --lib`.
