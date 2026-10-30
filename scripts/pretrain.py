"""Pre-train the agents on the last YEARS years of real data (walk-forward backtest).

Every strategy is replayed day by day with the live engine (agents.py): on day
D it only sees data up to D's close, its orders fill at D+1's open, then stops
and targets are checked. For each strategy about VARIANTS parameter sets are
scored with the league's own points on all but the last year (training); the
best one is then run on the last year (validation, never seen in training) and adopted only if it scores at least as well there as the
original parameters.

Limits, also stated in the Discord report: today's S&P 500 list is used for
the whole year (survivorship bias, results a bit optimistic); Joalheiro is not
tested because Yahoo only has today's fundamentals; the earnings rule is off
because past earnings dates are not available.

    uv run python scripts/pretrain.py --dry-run          # report only
    uv run python scripts/pretrain.py --apply            # also set the agents' parameters
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import random
import sys
from datetime import date

import pandas as pd

import agents
from agents import (
    PARAM_BOUNDS,
    STRATEGIES,
    Market,
    check_exits,
    equity,
    fill_pending,
    new_portfolio,
    score_day,
    zone_for,
)
from charts import lines_chart
from discord_post import BLUE, embed, route, send
from rankings import UCITS_ETFS, download_history, load_constituents

SESSIONS_PER_YEAR = 252
YEARS = 5
EXAM_SESSIONS = SESSIONS_PER_YEAR  # the last year is the exam; the years before it are for training
VARIANTS = 24
WARMUP = 220
SKIP = {
    "joalheiro": "precisa dos fundamentais do passado, que o Yahoo não tem",
    "jornalista": "não há histórico gratuito de notícias; só se avalia ao vivo",
    "catalisador": "não há histórico gratuito de notícias; só se avalia ao vivo",
    "cetico": "não há histórico gratuito de notícias; só se avalia ao vivo",
    "conselho": "depende das carteiras dos outros agentes ao vivo",
    "maestro": "depende dos resultados dos outros agentes ao vivo",
}
REPORT_PATH = agents.REPO_AGENTS / "pretrain.json"


# --------------------------------------------------------------------------
# Daily snapshots of the market, computed once with rolling windows
# --------------------------------------------------------------------------


def features(df: pd.DataFrame) -> pd.DataFrame:
    """The same fields as agents.load_market, for every day at once."""
    close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"]
    f = pd.DataFrame(index=df.index)
    f["price"] = close
    f["sma50"] = close.rolling(50).mean()
    f["sma200"] = close.rolling(200).mean()
    f["sma200_rising"] = f["sma200"] > close.shift(20).rolling(200).mean()
    high52, low52 = high.rolling(252, min_periods=1).max(), low.rolling(252, min_periods=1).min()
    f["pct_from_high"] = (high52 - close) / high52
    f["pct_above_low"] = (close - low52) / low52
    for name, n in (("ret_1d", 1), ("ret_1w", 5), ("ret_3m", 63), ("ret_6m", 126)):
        f[name] = close / close.shift(n) - 1
    f["dollar_volume"] = vol.rolling(50).mean() * close
    f["high20_prev"] = high.shift(1).rolling(20).max()
    f["low10_prev"] = low.shift(1).rolling(10).min()
    f["vol_ratio"] = vol / vol.shift(1).rolling(50).mean().clip(lower=1)
    f["range10"] = (high.rolling(10).max() - low.rolling(10).min()) / close
    gap = df["Open"] / close.shift(1) - 1
    event = (gap >= agents.GAP_MIN) & (vol / vol.shift(1).rolling(50).mean().clip(lower=1) >= agents.GAP_VOLUME)
    event &= pd.Series(range(len(df)), index=df.index) >= 51
    pos = pd.Series(range(len(df)), index=df.index, dtype=float)
    last_pos = pos.where(event).ffill(limit=agents.GAP_LOOKBACK - 1)
    f["gap_size"] = gap.where(event).ffill(limit=agents.GAP_LOOKBACK - 1).fillna(0.0)
    f["gap_low"] = low.where(event).ffill(limit=agents.GAP_LOOKBACK - 1).fillna(0.0)
    f["gap_age"] = (pos - last_pos).fillna(-1).astype(int)
    f["valid"] = pd.Series(range(len(df)), index=df.index) >= WARMUP - 1
    return f


def snapshots(hist: dict[str, pd.DataFrame], days: list[date], extra: dict[str, dict] | None = None) -> dict[date, dict]:
    """{day: {symbol: metrics}} with trend_score ranked within the group that day."""
    # Plain numpy rows per symbol: pandas row lookups are too slow for years x hundreds of symbols.
    tables = {}
    for s, df in hist.items():
        f = features(df)
        valid = f["valid"].to_numpy() & f["sma200"].notna().to_numpy() & f["ret_6m"].notna().to_numpy()
        cols = [c for c in f.columns if c != "valid"]
        rows = {ts.date(): i for i, ts in enumerate(f.index)}
        tables[s] = (rows, f[cols].to_numpy(dtype=float), cols, valid)
    out = {}
    for d in days:
        today = {}
        for s, (rows, values, cols, valid) in tables.items():
            i = rows.get(d)
            if i is None or not valid[i]:
                continue
            m = dict(zip(cols, values[i].tolist()))
            m["sma200_rising"] = bool(m["sma200_rising"])
            m["gap_age"] = int(m["gap_age"])
            m.update((extra or {}).get(s, {}))
            if "sector" in m:
                m["stage2"] = agents.is_stage2(m)
            today[s] = m
        agents.add_trend_scores(today)
        out[d] = today
    return out


def build_markets(years: int = YEARS) -> list[Market]:
    constituents = load_constituents()
    sector_of = {c["symbol"]: c["sector"] for c in constituents}
    period = "2y" if years <= 1 else "10y"  # Yahoo periods: enough for the backtest plus the 200-day warm-up
    stock_hist = download_history(list(sector_of) + ["SPY"], period=period)
    etf_hist = download_history([t for t, _ in UCITS_ETFS], period=period)
    fx = download_history(["EURUSD=X"], period=period)["EURUSD=X"]["Close"]
    spy = stock_hist.pop("SPY")
    sessions = [d.date() for d in spy.index]
    days = sessions[WARMUP:][-years * SESSIONS_PER_YEAR:]
    names = dict(UCITS_ETFS)
    stocks = snapshots(stock_hist, days, {s: {"sector": sector_of[s]} for s in stock_hist})
    etfs = snapshots(etf_hist, days, {s: {"name": names.get(s, s)} for s in etf_hist})
    hist = {**stock_hist, **etf_hist}
    closes = pd.DataFrame({s: df["Close"] for s, df in hist.items()}).sort_index().ffill()
    markets = []
    for d in days:
        share = sum(m["stage2"] for m in stocks[d].values()) / max(len(stocks[d]), 1)
        row = closes.loc[: pd.Timestamp(d)].iloc[-1]
        markets.append(Market(
            hist=hist, fx=fx, stocks=stocks[d], etfs=etfs[d], as_of=d,
            sessions=[x for x in sessions if x <= d], zone=zone_for(share), stage2_share=share,
            closes={s: float(v) for s, v in row.items() if pd.notna(v)},
        ))
    return markets


# --------------------------------------------------------------------------
# Simulation and search
# --------------------------------------------------------------------------


def simulate(strategy: str, params: dict, markets: list[Market], capital: float = 1000.0) -> dict:
    cfg = {"id": "bt", "name": strategy, "strategy": strategy, "params": params}
    port = new_portfolio(capital, markets[0].as_of)
    port["params"] = dict(params)
    peak, max_dd, trades = capital, 0.0, 0
    for mkt in markets:
        trades += len(fill_pending(port, mkt)) + len(check_exits(port, mkt))
        score_day(port, mkt)
        eq = equity(port, mkt)
        peak = max(peak, eq)
        max_dd = max(max_dd, 1 - eq / peak)
        if not port["pending"]:
            orders = STRATEGIES[strategy][0](cfg, port, mkt, port["params"])
            port["pending"] = [{**o, "created": mkt.as_of.isoformat()} for o in orders
                               if o["side"] == "sell" or o["eur"] >= agents.MIN_ORDER_EUR]
    final = equity(port, markets[-1])
    return {
        "points": port["points"],
        "return": final / capital - 1,
        "max_drawdown": max_dd,
        "trades": trades,
        "curve": [(h["date"], (h["equity"] / capital - 1) * 100) for h in port["history"]],
    }


def variants(strategy: str, n: int) -> list[dict]:
    base = STRATEGIES[strategy][2]
    tunable = [k for k in base if k in PARAM_BOUNDS]
    rng = random.Random(f"pretrain-{strategy}")
    out = [dict(base)]
    for _ in range(n if tunable else 0):
        p = dict(base)
        for k in tunable:
            lo, hi, kind = PARAM_BOUNDS[k]
            v = rng.uniform(lo, hi)
            p[k] = int(round(v)) if kind is int else round(v, 4)
        out.append(p)
    return out


_MARKETS: list[Market] = []  # shared with the worker processes (fork), not pickled


def train_strategy(strategy: str) -> tuple[str, dict]:
    """Search parameters on the training years, then check the best set on the exam year."""
    markets = _MARKETS
    split = max(len(markets) - EXAM_SESSIONS, len(markets) * 2 // 3)
    train, valid = markets[:split], markets[split:]
    candidates = variants(strategy, VARIANTS)
    scored = [(simulate(strategy, p, train), p) for p in candidates]
    best_train, best = max(scored, key=lambda x: x[0]["points"])
    default_train = scored[0][0]
    best_valid = simulate(strategy, best, valid)
    default_valid = best_valid if best == candidates[0] else simulate(strategy, candidates[0], valid)
    adopt = best != candidates[0] and best_valid["points"] >= default_valid["points"]
    chosen = best if adopt else candidates[0]
    full = simulate(strategy, chosen, markets)
    print(f"{strategy}: treino {best_train['points']} vs {default_train['points']} pts; "
          f"validação {best_valid['points']} vs {default_valid['points']} → {'adotado' if adopt else 'mantém'}",
          file=sys.stderr, flush=True)
    return strategy, {
        "default": candidates[0], "best": best, "adopted": adopt, "chosen": chosen,
        "train": {"default": strip(default_train), "best": strip(best_train)},
        "valid": {"default": strip(default_valid), "best": strip(best_valid)},
        "year": strip(full), "curve": full["curve"],
    }


def pretrain(markets: list[Market], only: set[str] | None = None, processes: int | None = None) -> dict:
    """Train every strategy (in parallel processes) and report training and exam results."""
    global _MARKETS
    _MARKETS = markets
    split = max(len(markets) - EXAM_SESSIONS, len(markets) * 2 // 3)
    results = {"train": [markets[0].as_of.isoformat(), markets[split - 1].as_of.isoformat()],
               "valid": [markets[split].as_of.isoformat(), markets[-1].as_of.isoformat()], "strategies": {}}
    todo = [s for s in STRATEGIES if not only or s in only]
    for s in todo:
        if s in SKIP:
            results["strategies"][s] = {"skipped": SKIP[s]}
    trainable = [s for s in todo if s not in SKIP]
    processes = processes or min(len(trainable), os.cpu_count() or 1)
    if processes > 1:
        with multiprocessing.get_context("fork").Pool(processes) as pool:
            done = pool.map(train_strategy, trainable, chunksize=1)
    else:
        done = [train_strategy(s) for s in trainable]
    results["strategies"].update(done)
    return results


def strip(r: dict) -> dict:
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items() if k != "curve"}


# --------------------------------------------------------------------------
# Apply and report
# --------------------------------------------------------------------------


def apply(results: dict, today: date) -> list[str]:
    """Give every agent of a tuned strategy its chosen parameters and let it decide again."""
    config, state = agents.load_config(), agents.load_state()
    changed = []
    for cfg in agents.active_agents(config):
        r = results["strategies"].get(cfg["strategy"], {})
        if not r.get("adopted"):
            continue
        port = state["agents"].setdefault(cfg["id"], new_portfolio(cfg["capital"], today))
        old = port.get("params", cfg["params"])
        params = {**old, **r["chosen"]}
        note = "pré-treino: " + ", ".join(f"{k} {old.get(k)}→{v}" for k, v in r["chosen"].items() if old.get(k) != v)
        port["params"] = params
        port["pending"], port["last_decision"], port["last_rebalance"] = [], None, None
        port["evolution"] = (port["evolution"] + [{"date": today.isoformat(), "note": note}])[-24:]
        changed.append(f"{cfg['emoji']} {cfg['name']}: {note}")
    agents.save(None, state)
    return changed


def pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def report(results: dict, applied: list[str] | None) -> tuple[list[dict], dict]:
    config = agents.load_config()
    by_strategy = {}
    for cfg in agents.active_agents(config):
        by_strategy.setdefault(cfg["strategy"], cfg)
    bench = results["strategies"].get("indexador", {}).get("year", {})
    lines = []
    for strategy, r in sorted(results["strategies"].items(), key=lambda kv: -kv[1].get("year", {}).get("return", -9)):
        cfg = by_strategy.get(strategy, {"emoji": "🤖", "name": strategy})
        if "skipped" in r:
            lines.append(f"{cfg['emoji']} **{cfg['name']}** · não testado: {r['skipped']}")
            continue
        y, v = r["year"], r["valid"]
        verdict = "✅ parâmetros novos" if r["adopted"] else "↩️ mantém os originais"
        lines.append(
            f"{cfg['emoji']} **{cfg['name']}** · período* {pct(y['return'])} · queda máx. {pct(-y['max_drawdown'])}"
            f" · exame: {v['best']['points']} vs {v['default']['points']} pts · {verdict}"
        )
    fields = [("🧪 Como foi feito",
               f"Treino {results['train'][0]} a {results['train'][1]} · exame {results['valid'][0]} a {results['valid'][1]}.\n"
               f"~{VARIANTS} variações por estratégia, avaliadas pelos pontos da liga. Ordens à abertura do dia seguinte, "
               "0,1% de custos, sem ver o futuro.\n"
               "*O período inclui os anos em que os parâmetros foram escolhidos, por isso é otimista; "
               "o que conta é o exame (pontos com os parâmetros escolhidos vs os originais).")]
    if applied:
        fields.append(("⚙️ Parâmetros aplicados aos agentes", "\n".join(applied)[:1024]))
    fields.append(("⚠️ Limites",
                   "Lista atual do S&P 500 (empresas que saíram do índice não entram: resultados otimistas). "
                   "Sem regra de resultados no passado. Um ano é um só tipo de mercado."))
    if bench:
        fields.append(("🐢 Referência", f"S&P 500 (Indexador) no período: {pct(bench['return'])}, queda máx. {pct(-bench['max_drawdown'])}"))
    series = {by_strategy.get(s, {"name": s})["name"]: r["curve"] for s, r in results["strategies"].items() if "curve" in r}
    years = round(len(next(iter(series.values()), [])) / SESSIONS_PER_YEAR, 1) if series else YEARS
    files = {"pretreino.png": lines_chart(series, f"Pré-treino: rentabilidade em {years:g} anos (parâmetros escolhidos)")}
    card = embed(f"🧠 Pré-treino dos agentes: {years:g} anos, dia a dia", "\n".join(lines), color=BLUE,
                 fields=fields, image="pretreino.png",
                 footer="Backtest: resultados passados simulados, não garantem resultados futuros.")
    return [card], files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Set the agents' parameters in agents/state.json")
    parser.add_argument("--dry-run", action="store_true", help="Print the report instead of posting")
    parser.add_argument("--estrategias", default="", help="Comma-separated strategies to train (default: all)")
    parser.add_argument("--anos", type=int, default=YEARS, help=f"Years of history to replay (default {YEARS})")
    args = parser.parse_args()

    markets = build_markets(args.anos)
    print(f"{len(markets)} dias de {markets[0].as_of} a {markets[-1].as_of}", file=sys.stderr)
    only = {s.strip() for s in args.estrategias.split(",") if s.strip()} or None
    results = pretrain(markets, only | {"indexador"} if only else None)
    if only and REPORT_PATH.exists():  # a partial run updates its strategies and keeps the others
        previous = json.loads(REPORT_PATH.read_text())
        results = {**results, "strategies": {**previous.get("strategies", {}), **results["strategies"]}}
    REPORT_PATH.write_text(json.dumps(results, indent=1, default=str) + "\n")
    applied = apply(results, markets[-1].as_of) if args.apply else None
    return send(*report(results, applied), dry_run=args.dry_run, **route("agentes"))


if __name__ == "__main__":
    sys.exit(main())
