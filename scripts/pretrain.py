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
import csv
import io
import json
import multiprocessing
import os
import random
import statistics
import sys
from datetime import date, timedelta

import pandas as pd
import requests

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
    f["vol20"] = close.pct_change().rolling(20).std()
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


def snapshots(hist: dict[str, pd.DataFrame], days: list[date], extra: dict[str, dict] | None = None,
              member=None) -> dict[date, dict]:
    """{day: {symbol: metrics}} with trend_score ranked within the group that day.

    `member(symbol, day)`, when given, keeps only the symbols in the index on that day.
    """
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
            if i is None or not valid[i] or (member and not member(s, d)):
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


MEMBERSHIP_URL = "https://raw.githubusercontent.com/fja05680/sp500/master/sp500_ticker_start_end.csv"


def load_membership() -> dict[str, list[tuple[date, date | None]]]:
    """{ticker: [(start, end or None)]}: when each ticker was in the S&P 500 (public dataset by fja05680)."""
    resp = requests.get(MEMBERSHIP_URL, timeout=60)
    resp.raise_for_status()
    out: dict[str, list[tuple[date, date | None]]] = {}
    for row in csv.DictReader(io.StringIO(resp.text)):
        ticker = row["ticker"].strip().replace(".", "-")
        start = date.fromisoformat(row["start_date"])
        end = date.fromisoformat(row["end_date"]) if row.get("end_date") else None
        out.setdefault(ticker, []).append((start, end))
    return out


def build_markets(years: int = YEARS) -> list[Market]:
    """Daily markets for the backtest, with the S&P 500 as it was on each day (survivorship bias reduced).

    Companies that left the index are included while they were members, when Yahoo still has their prices
    (it often has none for acquired or bankrupt ones, so some bias remains).
    """
    constituents = load_constituents()
    sector_of = {c["symbol"]: c["sector"] for c in constituents}
    try:
        membership = load_membership()
    except requests.RequestException as e:
        print(f"WARN: sem composição histórica ({e}); uso a lista atual", file=sys.stderr)
        membership = {}
    since = date.today() - timedelta(days=int((years + 1) * 365.25))
    for ticker, spans in membership.items():
        if any(end is None or end >= since for _, end in spans):
            sector_of.setdefault(ticker, "Unknown")

    def member(symbol: str, day: date) -> bool:
        spans = membership.get(symbol)
        return True if not spans else any(start <= day and (end is None or day <= end) for start, end in spans)
    period = "2y" if years <= 1 else "10y"  # Yahoo periods: enough for the backtest plus the 200-day warm-up
    stock_hist = download_history(list(sector_of) + ["SPY"], period=period)
    etf_hist = download_history([t for t, _ in UCITS_ETFS], period=period)
    fx = download_history(["EURUSD=X"], period=period)["EURUSD=X"]["Close"]
    spy = stock_hist.pop("SPY")
    sessions = [d.date() for d in spy.index]
    days = sessions[WARMUP:][-years * SESSIONS_PER_YEAR:]
    names = dict(UCITS_ETFS)
    stocks = snapshots(stock_hist, days, {s: {"sector": sector_of[s]} for s in stock_hist},
                       member if membership else None)
    left = [s for s in stock_hist if s not in {c["symbol"] for c in constituents}]
    print(f"{len(stock_hist)} ações com preços, {len(left)} já saíram do índice", file=sys.stderr)
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
    """Replay one strategy with the live rules (agents.decide), taxes and bankruptcy, from `capital` euros."""
    cfg = {"id": "bt", "name": strategy, "strategy": strategy, "params": params}
    port = new_portfolio(capital, markets[0].as_of)
    port["params"] = dict(params)
    trades, bankrupt_on = 0, None
    for mkt in markets:
        trades += len(fill_pending(port, mkt)) + len(check_exits(port, mkt))
        agents.pay_taxes(port, mkt)
        score_day(port, mkt)
        if bankrupt_on is None and strategy != "indexador" and agents.is_bankrupt(port, mkt):
            trades += len(agents.liquidate(port, mkt, "falência"))
            bankrupt_on = mkt.as_of.isoformat()
        if bankrupt_on is None and not port["pending"]:
            orders, _ = agents.decide(cfg, port, mkt, port["params"], live=False)
            port["pending"] = [{**o, "created": mkt.as_of.isoformat()} for o in orders]
    final = equity(port, markets[-1])
    equities = pd.Series([h["equity"] for h in port["history"]], dtype=float)
    daily = equities.pct_change().dropna()
    sharpe = float(daily.mean() / daily.std() * SESSIONS_PER_YEAR ** 0.5) if len(daily) > 20 and daily.std() > 0 else 0.0
    return {
        "points": port["points"],
        "return": final / capital - 1,
        "max_drawdown": port.get("max_dd", 0.0),
        "sharpe": round(sharpe, 2),
        "trades": trades,
        "bankrupt": bankrupt_on,
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


def yearly_blocks(markets: list[Market]) -> list[list[Market]]:
    """The training period cut into years; a short remainder joins the last year."""
    blocks = [markets[i:i + SESSIONS_PER_YEAR] for i in range(0, len(markets), SESSIONS_PER_YEAR)]
    if len(blocks) > 1 and len(blocks[-1]) < SESSIONS_PER_YEAR // 2:
        tail = blocks.pop()
        blocks[-1] = blocks[-1] + tail
    return blocks


def train_strategy(strategy: str) -> tuple[str, dict]:
    """Pick parameters that are consistently good year by year in training, then check them on the exam year.

    Each candidate runs every training year separately (fresh 1000 EUR each). It is eligible only if it beats the
    original parameters in at least 3 of every 4 years; among the eligible, the best median year wins. It is
    adopted only if it also scores at least as well as the original parameters on the exam year.
    """
    markets = _MARKETS
    split = max(len(markets) - EXAM_SESSIONS, len(markets) * 2 // 3)
    blocks, valid = yearly_blocks(markets[:split]), markets[split:]
    candidates = variants(strategy, VARIANTS)
    yearly = [[simulate(strategy, p, b)["points"] for b in blocks] for p in candidates]
    base = yearly[0]
    need = -(-3 * len(blocks) // 4)  # ceil(0.75 x years)

    def eligible(i: int) -> bool:
        return i == 0 or sum(a > b for a, b in zip(yearly[i], base)) >= need

    best_i = max((i for i in range(len(candidates)) if eligible(i)), key=lambda i: statistics.median(yearly[i]))
    best = candidates[best_i]
    best_valid = simulate(strategy, best, valid)
    default_valid = best_valid if best_i == 0 else simulate(strategy, candidates[0], valid)
    adopt = best_i != 0 and best_valid["points"] >= default_valid["points"]
    chosen = best if adopt else candidates[0]
    full = simulate(strategy, chosen, markets)
    print(f"{strategy}: anos de treino {yearly[best_i]} vs {base}; exame {best_valid['points']} vs "
          f"{default_valid['points']} → {'adotado' if adopt else 'mantém'}", file=sys.stderr, flush=True)
    return strategy, {
        "default": candidates[0], "best": best, "adopted": adopt, "chosen": chosen,
        "train": {"default": {"points": sum(base), "years": base},
                  "best": {"points": sum(yearly[best_i]), "years": yearly[best_i]}},
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
    """Give every agent of a trained strategy the parameters the exam chose, and let it decide again."""
    config, state = agents.load_config(), agents.load_state()
    changed = []
    for cfg in agents.active_agents(config):
        r = results["strategies"].get(cfg["strategy"], {})
        if "chosen" not in r:  # not trained (live-only) or not part of this run
            continue
        port = state["agents"].setdefault(cfg["id"], new_portfolio(cfg["capital"], today))
        old = port.get("params", cfg["params"])
        changes = {k: v for k, v in r["chosen"].items() if old.get(k) != v}
        if not changes:
            continue
        # The exam's verdict, also when it is "back to the original parameters".
        params = {**old, **r["chosen"]}
        note = "pré-treino: " + ", ".join(f"{k} {old.get(k)}→{v}" for k, v in changes.items())
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
            f" · Sharpe {y.get('sharpe', 0):.1f}" + (f" · 💀 faliu {y['bankrupt']}" if y.get("bankrupt") else "") +
            f" · exame: {v['best']['points']} vs {v['default']['points']} pts · {verdict}"
        )
    fields = [("🧪 Como foi feito",
               f"Treino {results['train'][0]} a {results['train'][1]} · exame {results['valid'][0]} a {results['valid'][1]}.\n"
               f"~{VARIANTS} variações por estratégia; só passam as que batem os parâmetros originais em 3 de cada 4 anos "
               "de treino, e depois no exame. Pontos da liga (face ao S&P 500, com penalização das quedas). Composição "
               "histórica do S&P 500, ordens à abertura seguinte, custos, câmbio e 28% de imposto, sem ver o futuro.\n"
               "*O período inclui os anos em que os parâmetros foram escolhidos, por isso é otimista; "
               "o que conta é o exame (pontos com os parâmetros escolhidos vs os originais).")]
    if applied:
        fields.append(("⚙️ Parâmetros aplicados aos agentes", "\n".join(applied)[:1024]))
    fields.append(("⚠️ Limites",
                   "Empresas que saíram do índice só entram quando o Yahoo ainda tem os preços (muitas falidas ou "
                   "compradas não têm), por isso ainda há algum otimismo. Sem regra de resultados nem notícias no passado."))
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
