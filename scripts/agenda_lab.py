"""What does each piece of the political agenda add? A fixed set of Estratega variants (each signal alone, without the
investment guard, other cores, rebalancing speeds, regions) and the agenda filter on a few agents, replayed on the
pre-training's 20 years and graded the same way (each year on fresh capital against the S&P 500; the last year is the
exam). A diagnosis, not a search: no variant is adopted here; any change still has to pass the pre-training's gates.

Results go to the private repo (agents/lab/agenda-<date>.json), never to the Actions log.

    uv run python scripts/agenda_lab.py --contexto ctx/context.csv        # (PRICE_CACHE_DIR with the prices)
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import sys
from datetime import UTC, date, datetime

import pretrain

import agents

BASE = dict(agents.STRATEGIES["estratega"][2])
OFF = {"peso_gasto": 0.0, "peso_incerteza": 0.0, "peso_momentum": 0.0, "peso_capex": 0.0}
VARIANTS = {
    "base": {},
    "so_gasto": {**OFF, "peso_gasto": 1.0},
    "so_incerteza": {**OFF, "peso_incerteza": 1.0},
    "so_momento": {**OFF, "peso_momentum": 1.0},
    "agenda_sem_momento": {"peso_momentum": 0.0},
    "sem_travao_capex": {"peso_capex": 0.0},
    "segue_capex": {"peso_capex": -0.5},
    "nucleo_0": {"nucleo": 0.0},
    "nucleo_30": {"nucleo": 0.3},
    "nucleo_70": {"nucleo": 0.7},
    "cada_5": {"cada": 5},
    "cada_63": {"cada": 63},
    "setores_1": {"setores": 1},
    "setores_2": {"setores": 2},
    "setores_4": {"setores": 4},
    "por_setor_5": {"por_setor": 5},
    "por_setor_8": {"por_setor": 8},
    "so_eua": {"regiao": 1},
    "so_europa": {"regiao": 2},
    "segue_zona": {"segue_zona": 1},
}
FILTERED = ["quant", "analista", "foguetao", "sniper", "campeao"]
FILTER_LEVELS = [0.0, 0.3, 0.6, 1.0]
_SPLIT: dict = {}


def jobs() -> list[tuple[str, str, dict]]:
    common = {k: agents.COMMON[k] for k in pretrain.SHARED_IN_BACKTEST}
    out = [("estratega", name, {**common, **BASE, **v}) for name, v in VARIANTS.items()]
    for s in FILTERED:
        for level in FILTER_LEVELS:
            out.append((s, f"filtro_agenda_{level:g}", {**common, **agents.STRATEGIES[s][2], "filtro_agenda": level}))
    return out


def _run(job: tuple[str, str, dict]) -> tuple[str, str, dict]:
    strategy, name, params = job
    years = [pretrain.simulate(strategy, params, b, detail=False)["points"] for b in _SPLIT["blocks"]]
    exam = pretrain.simulate(strategy, params, _SPLIT["exam"], detail=False)["points"]
    whole = pretrain.simulate(strategy, params, _SPLIT["all"], detail=False)
    return strategy, name, {
        "overall": round(pretrain.overall(years, exam), 1), "train": round(pretrain.weighted(years), 1),
        "years": [round(y, 1) for y in years], "exam": round(exam, 1),
        "years_beating_index": int(sum(y > 0 for y in years) + (exam > 0)), "years_total": len(years) + 1,
        "return_after_tax": round(whole["return_after_tax"], 4), "max_drawdown": round(whole["max_drawdown"], 4),
        "trades": whole["trades"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--contexto")
    ap.add_argument("--anos", type=int, default=pretrain.YEARS)
    a = ap.parse_args()
    markets = pretrain.build_markets(a.anos, a.contexto)
    pretrain._MARKETS[:] = markets
    import engine

    engine.data_for(markets)  # built before forking: the workers share it
    split = len(markets) - pretrain.EXAM_SESSIONS
    _SPLIT.update(blocks=pretrain.yearly_blocks(markets[:split]), exam=markets[split:], all=markets)
    todo = jobs()
    with multiprocessing.get_context("fork").Pool(os.cpu_count() or 1) as pool:
        done = pool.map(_run, todo, chunksize=1)
    out: dict = {}
    for strategy, name, r in done:
        out.setdefault(strategy, {})[name] = r
    index = pretrain.simulate("indexador", {}, markets, detail=False)
    path = agents.REPO_AGENTS / "lab" / f"agenda-{date.today().isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"updated": datetime.now(UTC).isoformat(timespec="seconds"), "perfil": pretrain.PROFILE,
                                "exam": [markets[split].as_of.isoformat(), markets[-1].as_of.isoformat()],
                                "index": {"return_after_tax": round(index["return_after_tax"], 4),
                                          "max_drawdown": round(index["max_drawdown"], 4)},
                                "results": out}, ensure_ascii=False, indent=1) + "\n")
    print(f"Laboratório da agenda: {len(todo)} variantes guardadas no repositório privado", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
