"""A series of pre-training runs (pretrain.yml with repetir): which strategies the next run trains.

Each strategy trains until its parameters stay the same STABLE runs in a row; the series ends when every strategy
is stable. Strategies out of the league for this profile (pretrain.bench) do not train in a series (the Sunday
run still trains them, so they can come back). The series' state travels from run to run as an input (estado_serie).

    uv run python scripts/series.py --antes antes.json --depois depois.json --perfil equilibrado --estado '{...}'

Prints two lines: the comma-separated strategies of the next run (empty when the series has converged) and the new
state. Strategy names and counts only: the Actions log is public.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import agents

STABLE = 2  # runs in a row without a change


def next_run(before: dict, after: dict, trained: list[str], state: dict, benched: set[str]) -> tuple[list[str], dict]:
    """(strategies for the next run, the updated series state)."""
    quiet = dict(state.get("quiet") or {})
    for s in trained:
        if s == "indexador" or s not in after:
            continue
        quiet[s] = 0 if before.get(s) != after.get(s) else quiet.get(s, 0) + 1
    todo = sorted(s for s, chosen in after.items()  # not the live-only ones (no parameters: skipped by the backtest)
                  if chosen is not None and s != "indexador" and s not in benched and quiet.get(s, 0) < STABLE)
    return todo, {**state, "quiet": quiet, "runs": state.get("runs", 0) + 1}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--antes", required=True)
    parser.add_argument("--depois", required=True)
    parser.add_argument("--perfil", default=agents.DEFAULT_PROFILE)
    parser.add_argument("--estado", default="", help="The series' state so far (JSON, from the previous run's output)")
    a = parser.parse_args()
    before, after = json.loads(Path(a.antes).read_text()), json.loads(Path(a.depois).read_text())
    state = json.loads(a.estado) if a.estado.strip() else {}
    report = json.loads(agents.pretrain_path(a.perfil).read_text())
    trained = report.get("trained") or sorted(after)
    benched = {c["strategy"] for c in agents.load_config()["agents"] if c.get("banco") and agents.profile_of(c) == a.perfil}
    todo, state = next_run(before, after, trained, state, benched)
    print(",".join(todo))
    print(json.dumps(state, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
