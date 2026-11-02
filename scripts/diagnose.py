"""Why did a strategy's 20-year result change? The parameters each agent trades with, replayed on the same 20 years
in four set-ups: with and without the European stocks, with and without the gap rule (gap_max).

Results go to the private repo (agents/lab/diagnostico-<date>.json) and to Discord, never to the Actions log.

    uv run python scripts/diagnose.py --estrategias sniper,cacador --contexto ctx/context.csv
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import sys
from datetime import date, datetime, timezone

import agents
import pretrain

SETUPS = [("todas", True, True), ("sem_gap", True, False), ("so_eua", False, True), ("so_eua_sem_gap", False, False)]
MARKETS: list = []  # set before forking


def params_of(strategy: str) -> dict:
    """The parameters the reactive agent of this strategy trades with (else the strategy's defaults)."""
    config, state = agents.load_config(), agents.load_state()
    cfg = next((c for c in agents.active_agents(config)
                if c["strategy"] == strategy and agents.mode_of(c) == agents.REACTIVE), None)
    if not cfg:
        return dict(agents.STRATEGIES[strategy][2])
    return dict((state["agents"].get(cfg["id"]) or {}).get("params", cfg["params"]))


def us_only(markets: list) -> list:
    """The same days with only the US stocks (trend scores ranked again within them)."""
    out = []
    for m in markets:
        stocks = {s: r for s, r in m.stocks.items() if agents.is_us(s)}
        agents.add_trend_scores(stocks)
        out.append(agents.Market(hist=m.hist, fx=m.fx, fx_rates=m.fx_rates, stocks=stocks, etfs=m.etfs, as_of=m.as_of,
                                 sessions=m.sessions, zone=m.zone, stage2_share=m.stage2_share, closes=m.closes,
                                 context=m.context))
    return out


def _run(job: tuple[str, dict, str]) -> tuple[str, str, dict]:
    strategy, params, setup = job
    r = pretrain.simulate(strategy, params, MARKETS)
    keep = ("return", "max_drawdown", "sharpe", "trades", "points", "bankrupt")
    return strategy, setup, {k: r[k] for k in keep} | {"trade_stats": r.get("trade_stats", {})}


def run_setups(strategies: list[str], markets: list, with_eu: bool) -> dict:
    """{strategy: {setup: result}} for the set-ups of this universe."""
    global MARKETS
    MARKETS = markets
    jobs = []
    for s in strategies:
        p = params_of(s)
        for name, eu, gap in SETUPS:
            if eu == with_eu:
                jobs.append((s, p if gap else {**p, "gap_max": 0}, name))
    workers = min(len(jobs), os.cpu_count() or 1)
    with multiprocessing.get_context("fork").Pool(workers) as pool:
        done = pool.map(_run, jobs)
    out: dict = {}
    for s, setup, r in done:
        out.setdefault(s, {})[setup] = r
    return out


def report(results: dict) -> dict:
    from discord_post import BLUE, embed

    lines = []
    for s, r in results.items():
        cells = " · ".join(f"{name.replace('_', ' ')} {r[name]['return'] * 100:+.0f}% (queda {r[name]['max_drawdown'] * 100:.0f}%)"
                           for name, _, _ in SETUPS if name in r)
        lines.append(f"**{s}**: {cells}")
    return embed("🔎 Diagnóstico do pré-treino (20 anos, parâmetros atuais)",
                 "O mesmo período e os mesmos parâmetros de cada agente, com e sem ações europeias e com e sem a regra "
                 "dos gaps.\n\n" + "\n".join(lines)[:3800], color=BLUE)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--estrategias", required=True)
    parser.add_argument("--contexto")
    parser.add_argument("--anos", type=int, default=pretrain.YEARS)
    parser.add_argument("--dry-run", action="store_true", help="Do not post to Discord (results still saved)")
    a = parser.parse_args()
    strategies = [s.strip() for s in a.estrategias.split(",") if s.strip() in agents.STRATEGIES]
    markets = pretrain.build_markets(a.anos, a.contexto)
    results = run_setups(strategies, markets, with_eu=True)
    for s, r in run_setups(strategies, us_only(markets), with_eu=False).items():
        results[s].update(r)
    path = agents.REPO_AGENTS / "lab" / f"diagnostico-{date.today().isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "years": a.anos,
                                "strategies": results}, ensure_ascii=False))
    print("Diagnóstico guardado no repositório privado (agents/lab)", flush=True)
    if a.dry_run:
        return 0
    from discord_post import route, send

    return send([report(results)], {}, **(route("agentes") or {}))


if __name__ == "__main__":
    sys.exit(main())
