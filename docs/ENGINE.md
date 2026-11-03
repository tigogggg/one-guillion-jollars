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
2. Every trainable strategy (15) — done. A training on the test markets: 64 s in Python, 1.3 s with the engine,
   same choices; on GitHub a strategy's job went from 20-45 min to about 3 min (mostly loading the data).
3. Use the speed for better training — started: 3x more candidates in Actions, and a walk-forward check of the
   search itself (the same search on the data up to each of the last 3 years, judged on that unseen year).
4. The live rounds, then the Python engine is removed. Plan:
   - **What moves:** everything that decides and trades — `react` (reactivity levels, buy window, the day's loss
     stop), `live_exits`, fills at a real open during a session (`fill_pending(partial_day)`), stops on the day so far
     (`check_exits(open_day)`), the news veto, the daily mode (`run_day`) and the live-only strategies (Joalheiro,
     Jornalista, Catalisador, Cético, Conselho, Maestro, Relâmpago), with their inputs (rankings, news signals, the
     league's portfolios, the round's clock) added to the engine's data.
   - **What stays in Python:** collecting data (prices, news, SEC, FRED, Claude), the agents' lifecycle (twins,
     profiles, bankruptcies and children, monthly mutation), Discord, the portal and charts. The live state stays the
     JSON in the private repo; the engine reads the fields it trades with and Python writes them back.
   - **4a — intraday lab on the engine (done):** the lab's 30-minute checkpoints (each a market with the day so far)
     run `react`, `live_exits` and the fills and stops of a session under way in Rust (`engine/src/live.rs`), many
     agents at once through chunks of days (`ogj_engine.Batch`). Same results as Python (tests/test_intraday_lab.py);
     12 runs over 250 days: 124 s in Python, 2.2 s in Rust.
   - **4b — shadow rounds:** every live round also runs the engine on the same state and market, without saving;
     any difference from Python is written to the private repo. A week without differences, on real rounds.
   - **4c — switch:** the rounds trade with the engine; the Python copy of the engine is deleted.

## Build

`uv sync` builds `engine/` with maturin (Rust toolchain needed; GitHub's runners have it). `uv` rebuilds it when
`engine/src/**/*.rs` or `Cargo.toml` change. Rust unit tests: `cd engine && cargo test --lib`.
