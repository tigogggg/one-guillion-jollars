"""How much history should the training look at? The same search run on the last 5, the last 10 or all the years
before each of the WF_FOLDS years before the exam (pretrain.walk_forward with a window), each choice judged on that
unseen year: which horizon picks the parameters that do best afterwards, against the original ones and against the
S&P 500 (the points are already relative to it)? More history means more crises and more years to tell skill from
luck; less means a more recent market. The answer is measured, not assumed.

Results go to the private repo (agents/lab/horizonte/<profile>/<strategy>.json), never to the Actions log.

    uv run python scripts/horizon_lab.py --estrategia quant --contexto ctx/context.csv    # (PRICE_CACHE_DIR)
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import UTC, datetime

import pretrain

import agents

WINDOWS = {"5 anos": 5, "10 anos": 10, "todos": None}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--estrategia", required=True)
    ap.add_argument("--contexto")
    ap.add_argument("--anos", type=int, default=pretrain.YEARS)
    a = ap.parse_args()
    markets = pretrain.build_markets(a.anos, a.contexto)
    pretrain._MARKETS = markets
    pretrain._INNER_POOL = True  # alone in its machine: the candidates on all the cores
    import engine

    if engine.enabled(a.estrategia):
        engine.data_for(markets)
    out = {}
    for name, window in WINDOWS.items():
        wf = pretrain.walk_forward(a.estrategia, window)
        news = [f["new"] for f in wf["folds"]]
        out[name] = {**wf, "mean_points": round(statistics.fmean(news), 1) if news else None,
                     "years_beating_index": sum(x > 0 for x in news)}
        print(f"Horizonte {name}: pronto", flush=True)
    folds = min(len(v["folds"]) for v in out.values())
    best = {f: max(out, key=lambda n: out[n]["folds"][f]["new"]) for f in range(folds)}
    result = {"updated": datetime.now(UTC).isoformat(timespec="seconds"), "strategy": a.estrategia,
              "perfil": pretrain.PROFILE, "windows": out,
              "best_by_year": {out["todos"]["folds"][f]["test"][1][:4]: best[f] for f in range(folds)},
              "wins": {n: sum(b == n for b in best.values()) for n in out}}
    path = agents.REPO_AGENTS / "lab" / "horizonte" / pretrain.PROFILE / f"{a.estrategia}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=1, default=float) + "\n")
    print("Horizonte: guardado no repositório privado", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
