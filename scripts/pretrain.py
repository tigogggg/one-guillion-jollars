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
from pathlib import Path

import numpy as np
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
from references import reference_lines
from discord_post import BLUE, embed, route, send
from rankings import UCITS_ETFS, download_history, load_constituents

SESSIONS_PER_YEAR = 252
YEARS = 20
VARIANTS_LONG = 16  # fewer variants above LONG_YEARS, which also run in one process (memory)
LONG_YEARS = 10
# Crash tests: a candidate must not do clearly worse than the original parameters in any of these.
CRISES = [
    ("2008", "2007-10-09", "2009-06-30"),
    ("2011", "2011-07-01", "2011-12-30"),
    ("2020", "2020-02-19", "2020-06-30"),
    ("2022", "2022-01-03", "2022-12-30"),
]
CRASH_SLACK = 0.05  # allowed extra drawdown in a crisis versus the original parameters
MC_PATHS, MC_BLOCK = 1000, 21  # Monte Carlo: alternative years built from one-month blocks of daily returns
MC_RUIN_SLACK = 0.05
# US ETFs standing in for UCITS ETFs before they existed (converted to EUR and spliced onto the real history).
ETF_PROXIES = {
    "SXR8.DE": ["SPY"], "VUAA.DE": ["SPY"], "VWCE.DE": ["ACWI", "SPY"], "EUNL.DE": ["ACWI", "SPY"],
    "IUSQ.DE": ["ACWI", "SPY"], "SXRV.DE": ["QQQ"], "ZPRR.DE": ["IWM"], "IUSN.DE": ["IWM"], "IS3N.DE": ["EEM"],
    "QDV5.DE": ["INDA", "EEM"], "SXRT.DE": ["FEZ"], "EXSA.DE": ["VGK", "IEV"], "EXS1.DE": ["EWG"],
    "SXR7.DE": ["FEZ"], "XDWT.DE": ["XLK"], "XDWH.DE": ["XLV"], "XDWF.DE": ["XLF"], "XDW0.DE": ["XLE"],
    "XDWI.DE": ["XLI"], "XDWC.DE": ["XLY"], "XDWS.DE": ["XLP"], "XDWM.DE": ["XLB"], "XDWU.DE": ["XLU"],
    "XWTS.DE": ["XLC"], "VVSM.DE": ["SMH"], "DFEN.DE": ["ITA"], "IQQH.DE": ["ICLN"], "IS3S.DE": ["IVE"],
    "VHYL.DE": ["DVY"], "TDIV.AS": ["DVY"], "VAGF.DE": ["AGG"], "EUNH.DE": ["IEF"], "4GLD.DE": ["GLD"],
}
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


def splice(target: pd.DataFrame | None, proxies: list[pd.DataFrame], fx: pd.Series) -> pd.DataFrame | None:
    """Extend an EUR-listed ETF's history backwards with US proxies, converted to EUR and scaled to join it."""
    out = target
    for proxy in proxies:
        rate = fx.reindex(proxy.index, method="ffill").bfill().fillna(fx.iloc[0])
        eur = proxy.copy()
        for col in ("Open", "High", "Low", "Close"):
            eur[col] = proxy[col] / rate
        if out is None or out.empty:
            out = eur
            continue
        first = out.index[0]
        before = eur[eur.index < first]
        anchor = eur["Close"].asof(first)
        if before.empty or pd.isna(anchor) or not anchor:
            continue
        ratio = float(out["Close"].iloc[0]) / float(anchor)
        before = before.copy()
        for col in ("Open", "High", "Low", "Close"):
            before[col] = before[col] * ratio
        out = pd.concat([before, out])
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
    # Yahoo periods: enough for the backtest plus the 200-day warm-up
    period = "2y" if years <= 1 else "10y" if years <= 8 else "max"
    stock_hist = download_history(list(sector_of) + ["SPY"], period=period)
    etf_hist = download_history([t for t, _ in UCITS_ETFS], period=period)
    fx = download_history(["EURUSD=X"], period=period)["EURUSD=X"]["Close"]
    proxy_hist = download_history(sorted({p for ps in ETF_PROXIES.values() for p in ps}), period=period)
    for etf, proxies in ETF_PROXIES.items():
        spliced = splice(etf_hist.get(etf), [proxy_hist[p] for p in proxies if p in proxy_hist], fx)
        if spliced is not None:
            etf_hist[etf] = spliced
    spy = stock_hist.pop("SPY")
    sessions = [d.date() for d in spy.index]
    days = sessions[WARMUP:][-years * SESSIONS_PER_YEAR:]
    names = dict(UCITS_ETFS)
    stocks = snapshots(stock_hist, days, {s: {"sector": sector_of[s]} for s in stock_hist},
                       member if membership else None)
    left = [s for s in stock_hist if s not in {c["symbol"] for c in constituents}]
    print(f"{len(stock_hist)} ações com preços, {len(left)} já saíram do índice", file=sys.stderr)
    COVERAGE.clear()
    for name, start, _ in CRISES:
        d = next((x for x in days if x >= date.fromisoformat(start)), None)
        if d and membership:
            in_index = sum(1 for spans in membership.values()
                           if any(a <= d and (b is None or d <= b) for a, b in spans))
            COVERAGE[name] = round(len(stocks[d]) / max(in_index, 1), 3)
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
        "by_zone": {z: round(total / n, 6) for z, (total, n) in port.get("zone_stats", {}).items() if n >= 20},
        "daily": [round(x, 6) for x in daily.tolist()],
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


COVERAGE: dict[str, float] = {}  # share of the index with prices at the start of each crisis
_MARKETS: list[Market] = []  # shared with the worker processes (fork), not pickled


def monte_carlo(daily: list[float], paths: int = MC_PATHS, block: int = MC_BLOCK, seed: int = 0) -> dict:
    """Alternative years from random one-month blocks of the strategy's own daily returns."""
    if len(daily) < 2 * block:
        return {}
    rng = np.random.default_rng(seed)
    r = np.asarray(daily, dtype=float)
    starts = rng.integers(0, len(r) - block, size=(paths, -(-SESSIONS_PER_YEAR // block)))
    years = np.stack([np.concatenate([r[s:s + block] for s in row])[:SESSIONS_PER_YEAR] for row in starts])
    curves = np.cumprod(1 + years, axis=1)
    final = curves[:, -1] - 1
    drawdown = 1 - (curves / np.maximum.accumulate(curves, axis=1)).min(axis=1)
    return {
        "median": round(float(np.median(final)), 4),
        "p5": round(float(np.percentile(final, 5)), 4),
        "p_loss": round(float((final < 0).mean()), 3),
        "p_dd30": round(float((drawdown > 0.30).mean()), 3),
        "p_ruin": round(float((curves.min(axis=1) < 1 - agents.BANKRUPTCY).mean()), 3),
    }


def crisis_windows(markets: list[Market]) -> dict[str, list[Market]]:
    out = {}
    for name, start, end in CRISES:
        window = [m for m in markets if date.fromisoformat(start) <= m.as_of <= date.fromisoformat(end)]
        if len(window) >= 40:
            out[name] = window
    return out


def crash_test(strategy: str, params: dict, windows: dict[str, list[Market]]) -> dict[str, dict]:
    return {name: {k: v for k, v in strip(simulate(strategy, params, w)).items() if k in ("return", "max_drawdown", "bankrupt")}
            for name, w in windows.items()}


def survives(candidate: dict[str, dict], original: dict[str, dict]) -> bool:
    """No crisis where the candidate goes bankrupt or falls clearly deeper than the original parameters."""
    for name, c in candidate.items():
        o = original[name]
        if c["bankrupt"] and not o["bankrupt"]:
            return False
        if c["max_drawdown"] > o["max_drawdown"] + CRASH_SLACK:
            return False
    return True


def yearly_blocks(markets: list[Market]) -> list[list[Market]]:
    """The training period cut into years; a short remainder joins the last year."""
    blocks = [markets[i:i + SESSIONS_PER_YEAR] for i in range(0, len(markets), SESSIONS_PER_YEAR)]
    if len(blocks) > 1 and len(blocks[-1]) < SESSIONS_PER_YEAR // 2:
        tail = blocks.pop()
        blocks[-1] = blocks[-1] + tail
    return blocks


def train_strategy(strategy: str) -> tuple[str, dict]:
    """Pick parameters that are consistently good, survive the crises and hold up on the exam year.

    1. Each candidate runs every training year separately (fresh 1000 EUR each) and must beat the original
       parameters in at least 3 of every 4 years.
    2. It must not go bankrupt, or fall more than CRASH_SLACK deeper than the original, in any crisis window.
    3. Among those, the best median year wins; it is adopted only if it scores at least as well as the
       original on the exam year and its Monte Carlo risk of ruin is not clearly higher.
    """
    markets = _MARKETS
    split = max(len(markets) - EXAM_SESSIONS, len(markets) * 2 // 3)
    blocks, valid = yearly_blocks(markets[:split]), markets[split:]
    windows = crisis_windows(markets[:split])
    candidates = variants(strategy, VARIANTS_LONG if len(blocks) > LONG_YEARS else VARIANTS)
    yearly = [[simulate(strategy, p, b)["points"] for b in blocks] for p in candidates]
    base = yearly[0]
    need = -(-3 * len(blocks) // 4)  # ceil(0.75 x years)
    consistent = [i for i in range(len(candidates)) if i == 0 or sum(a > b for a, b in zip(yearly[i], base)) >= need]
    ranked = sorted(consistent, key=lambda i: statistics.median(yearly[i]), reverse=True)
    crashes = {0: crash_test(strategy, candidates[0], windows)}
    best_i = 0
    for i in ranked:  # the best consistent candidate that also survives the crises
        if i == 0:
            break
        crashes[i] = crash_test(strategy, candidates[i], windows)
        if survives(crashes[i], crashes[0]):
            best_i = i
            break
    best = candidates[best_i]
    best_valid = simulate(strategy, best, valid)
    default_valid = best_valid if best_i == 0 else simulate(strategy, candidates[0], valid)
    full_best = simulate(strategy, best, markets)
    full_default = full_best if best_i == 0 else simulate(strategy, candidates[0], markets)
    mc_best, mc_default = monte_carlo(full_best["daily"]), monte_carlo(full_default["daily"])
    riskier = mc_best.get("p_ruin", 0) > mc_default.get("p_ruin", 0) + MC_RUIN_SLACK
    adopt = best_i != 0 and best_valid["points"] >= default_valid["points"] and not riskier
    chosen, full, mc = (best, full_best, mc_best) if adopt else (candidates[0], full_default, mc_default)
    print(f"{strategy}: anos {yearly[best_i]} vs {base}; crises {crashes.get(best_i)}; exame {best_valid['points']} vs "
          f"{default_valid['points']}; MC ruína {mc_best.get('p_ruin')} vs {mc_default.get('p_ruin')} → "
          f"{'adotado' if adopt else 'mantém'}", file=sys.stderr, flush=True)
    return strategy, {
        "default": candidates[0], "best": best, "adopted": adopt, "chosen": chosen,
        "train": {"default": {"points": sum(base), "years": base},
                  "best": {"points": sum(yearly[best_i]), "years": yearly[best_i]}},
        "valid": {"default": strip(default_valid), "best": strip(best_valid)},
        "crises": crashes.get(best_i if adopt else 0, crashes[0]),
        "monte_carlo": mc, "by_zone": full["by_zone"],
        "year": strip(full), "curve": full["curve"][::5],  # weekly points are enough for the chart
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
    long_run = len(markets) > LONG_YEARS * SESSIONS_PER_YEAR  # forked copies of years of snapshots would not fit
    processes = processes or (1 if long_run else min(len(trainable), os.cpu_count() or 1))
    if processes > 1:
        with multiprocessing.get_context("fork").Pool(processes) as pool:
            done = pool.map(train_strategy, trainable, chunksize=1)
    else:
        done = [train_strategy(s) for s in trainable]
    results["strategies"].update(done)
    results["coverage"] = dict(COVERAGE)
    return results


def strip(r: dict) -> dict:
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()
            if k not in ("curve", "daily", "by_zone")}


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
               "Variações por estratégia; só passam as que batem os parâmetros originais em 3 de cada 4 anos de treino, "
               "não caem mais do que eles nas crises (2008, 2011, 2020, 2022) e passam o exame. Pontos da liga (face ao S&P 500, com penalização das quedas). Composição "
               "histórica do S&P 500, ordens à abertura seguinte, custos, câmbio e 28% de imposto, sem ver o futuro.\n"
               "*O período inclui os anos em que os parâmetros foram escolhidos, por isso é otimista; "
               "o que conta é o exame (pontos com os parâmetros escolhidos vs os originais).")]
    crash_lines, mc_lines = [], []
    for strategy, r in results["strategies"].items():
        cfg = by_strategy.get(strategy, {"emoji": "🤖", "name": strategy})
        if r.get("crises"):
            crash_lines.append(f"{cfg['emoji']} {cfg['name']}: " + " · ".join(
                f"{name} {'💀' if c['bankrupt'] else pct(-c['max_drawdown'])}" for name, c in r["crises"].items()))
        mc = r.get("monte_carlo") or {}
        if mc:
            mc_lines.append(f"{cfg['emoji']} {cfg['name']}: mediana {pct(mc['median'])} · pior 5% {pct(mc['p5'])} · "
                            f"perda {mc['p_loss']:.0%} · falência {mc['p_ruin']:.0%}")
    if crash_lines:
        coverage = results.get("coverage") or {}
        note = ("\n_Ações com dados no início de cada crise: " + ", ".join(f"{k} {v:.0%}" for k, v in coverage.items())
                + " (as que faliram muitas vezes já não têm preços)._") if coverage else ""
        fields.append(("💥 Teste de colisão: queda máxima em cada crise", ("\n".join(crash_lines) + note)[:1024]))
    if mc_lines:
        fields.append((f"🎲 Monte Carlo: {MC_PATHS} anos alternativos", "\n".join(mc_lines)[:1024]))
    if applied:
        fields.append(("⚙️ Parâmetros aplicados aos agentes", "\n".join(applied)[:1024]))
    fields.append(("⚠️ Limites",
                   "Empresas que saíram do índice só entram quando o Yahoo ainda tem os preços (muitas falidas ou "
                   "compradas não têm), por isso ainda há algum otimismo. Sem regra de resultados nem notícias no passado."))
    if bench:
        fields.append(("🐢 Referência", f"S&P 500 (Indexador) no período: {pct(bench['return'])}, queda máx. {pct(-bench['max_drawdown'])}"))
    series = {by_strategy.get(s, {"name": s})["name"]: r["curve"] for s, r in results["strategies"].items() if "curve" in r}
    first = next(iter(series.values()), [])
    years = round((date.fromisoformat(first[-1][0]) - date.fromisoformat(first[0][0])).days / 365.25) if first else YEARS
    files = {"pretreino.png": lines_chart(series, f"Pré-treino: rentabilidade em {years:g} anos (parâmetros escolhidos)",
                                          reference_lines(series))}
    card = embed(f"🧠 Pré-treino dos agentes: {years:g} anos, dia a dia", "\n".join(lines), color=BLUE,
                 fields=fields, image="pretreino.png",
                 footer="Backtest: resultados passados simulados, não garantem resultados futuros.")
    return [card], files


def finish(results: dict, today: date, apply_params: bool, dry_run: bool, partial: bool) -> int:
    """Save the results (keeping other strategies on a partial run), apply them and post the report."""
    if partial and REPORT_PATH.exists():
        previous = json.loads(REPORT_PATH.read_text())
        results = {**results, "strategies": {**previous.get("strategies", {}), **results["strategies"]}}
    REPORT_PATH.write_text(json.dumps(results, indent=1, default=str) + "\n")
    applied = apply(results, today) if apply_params else None
    return send(*report(results, applied), dry_run=dry_run, **route("agentes"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Set the agents' parameters in agents/state.json")
    parser.add_argument("--dry-run", action="store_true", help="Print the report instead of posting")
    parser.add_argument("--estrategias", default="", help="Comma-separated strategies to train (default: all)")
    parser.add_argument("--anos", type=int, default=YEARS, help=f"Years of history to replay (default {YEARS})")
    parser.add_argument("--saida", help="Only train and write these strategies' results to this JSON file "
                                        "(one job of a parallel run)")
    parser.add_argument("--juntar", help="Merge the JSON results in this folder, then save, apply and report")
    parser.add_argument("--republicar", action="store_true", help="Only post the report of the saved results again")
    args = parser.parse_args()

    if args.republicar:
        return send(*report(json.loads(REPORT_PATH.read_text()), None), dry_run=args.dry_run, **route("agentes"))

    if args.juntar:
        parts = [json.loads(f.read_text()) for f in sorted(Path(args.juntar).rglob("*.json"))]
        if not parts:
            print("Nenhum resultado para juntar.", file=sys.stderr)
            return 1
        results = {**parts[0], "strategies": {k: v for part in parts for k, v in part["strategies"].items()}}
        return finish(results, date.fromisoformat(results["valid"][1]), args.apply, args.dry_run, partial=True)

    markets = build_markets(args.anos)
    print(f"{len(markets)} dias de {markets[0].as_of} a {markets[-1].as_of}", file=sys.stderr)
    only = {s.strip() for s in args.estrategias.split(",") if s.strip()} or None
    if args.saida:
        results = pretrain(markets, only)
        Path(args.saida).parent.mkdir(parents=True, exist_ok=True)
        Path(args.saida).write_text(json.dumps(results, default=str))
        return 0
    results = pretrain(markets, only | {"indexador"} if only else None)
    return finish(results, markets[-1].as_of, args.apply, args.dry_run, partial=bool(only))


if __name__ == "__main__":
    sys.exit(main())
