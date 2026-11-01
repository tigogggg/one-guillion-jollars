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
from collections.abc import Mapping, MutableMapping
import csv
import io
import json
import math
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
import interactive
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
# Changes the random candidates of each run (--semente): consecutive runs explore different parameters, and the
# best of the earlier runs stays a candidate (incumbent_candidates), so each run can only keep or improve it.
SEED = os.environ.get("PRETRAIN_SEED", "")
YEARS = 20
VARIANTS_LONG = 32  # fewer variants above LONG_YEARS, which also run in one process (memory)
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
VARIANTS = 40
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
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    f["atr14"] = tr.rolling(14).mean()
    gap = df["Open"] / close.shift(1) - 1
    event = (gap >= agents.GAP_MIN) & (vol / vol.shift(1).rolling(50).mean().clip(lower=1) >= agents.GAP_VOLUME)
    event &= pd.Series(range(len(df)), index=df.index) >= 51
    pos = pd.Series(range(len(df)), index=df.index, dtype=float)
    last_pos = pos.where(event).ffill(limit=agents.GAP_LOOKBACK - 1)
    f["gap_size"] = gap.where(event).ffill(limit=agents.GAP_LOOKBACK - 1).fillna(0.0)
    f["gap_low"] = low.where(event).ffill(limit=agents.GAP_LOOKBACK - 1).fillna(0.0)
    f["gap_age"] = (pos - last_pos).fillna(-1).astype(int)
    f = f.join(agents.technical_lines(df))
    f["valid"] = pd.Series(range(len(df)), index=df.index) >= WARMUP - 1
    return f


class Row(MutableMapping):
    """One stock's metrics on one day, read from a shared numpy table instead of a dict per stock per day
    (a dict of 50 floats costs ~3 KB; 20 years x 500 stocks of them did not fit next to each other in memory).
    Reads like the live dict: floats, bools for sma200_rising, ints for gap_age, None for unknown fundamentals.
    Keys added later (sector, stage2, trend_score) live in a small dict of their own."""

    __slots__ = ("_v", "_cols", "_kinds", "_extra")

    def __init__(self, values, cols: dict[str, int], kinds: dict[str, object], extra: dict | None = None):
        self._v, self._cols, self._kinds, self._extra = values, cols, kinds, dict(extra or {})

    def __getitem__(self, key):
        if key in self._extra:
            return self._extra[key]
        x = float(self._v[self._cols[key]])
        kind = self._kinds.get(key)
        if kind is None:
            return x
        if kind is bool:
            return bool(x)
        if kind is int:
            return int(x)
        return None if x != x else x  # nullable: NaN is unknown

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key) -> bool:
        return key in self._extra or key in self._cols

    def __setitem__(self, key, value) -> None:
        self._extra[key] = value

    def __delitem__(self, key) -> None:
        del self._extra[key]

    def __iter__(self):
        yield from (k for k in self._cols if k not in self._extra)
        yield from self._extra

    def __len__(self) -> int:
        return len(self._cols) + sum(1 for k in self._extra if k not in self._cols)


class PriceRow(Mapping):
    """The closes of every symbol on one day (forward-filled), as a read-only mapping; NaN = no price."""

    __slots__ = ("_v", "_cols")

    def __init__(self, values, cols: dict[str, int]):
        self._v, self._cols = values, cols

    def __getitem__(self, key):
        x = float(self._v[self._cols[key]])
        if x != x:
            raise KeyError(key)
        return x

    def get(self, key, default=None):
        i = self._cols.get(key)
        if i is None:
            return default
        x = float(self._v[i])
        return default if x != x else x

    def __iter__(self):
        return (k for k, i in self._cols.items() if self._v[i] == self._v[i])

    def __len__(self) -> int:
        return sum(1 for _ in self)


def snapshots(hist: dict[str, pd.DataFrame], days: list[date], extra: dict[str, dict] | None = None,
              member=None, frames: dict[str, pd.DataFrame] | None = None) -> dict[date, dict]:
    """{day: {symbol: metrics}} with trend_score ranked within the group that day.

    `member(symbol, day)`, when given, keeps only the symbols in the index on that day. Each symbol's metrics are
    one float32 table holding only the backtest's days, read through Row (much less memory than dicts).
    """
    wanted = set(days)
    tables = {}
    for s, df in hist.items():
        f = features(df)
        nullable = []
        if frames and s in frames:  # point-in-time fundamentals (fundamentals.daily)
            f = f.join(frames[s])
            nullable = list(frames[s].columns)
        valid = f["valid"].to_numpy() & f["sma200"].notna().to_numpy() & f["ret_6m"].notna().to_numpy()
        keep = [i for i, ts in enumerate(f.index) if ts.date() in wanted and valid[i]]
        if not keep:
            continue
        cols = [c for c in f.columns if c != "valid"]
        values = f[cols].to_numpy(dtype=np.float32)[keep]
        rows = {f.index[i].date(): k for k, i in enumerate(keep)}
        kinds = {"sma200_rising": bool, "gap_age": int, **{c: "nullable" for c in nullable}}
        tables[s] = (rows, values, {c: k for k, c in enumerate(cols)}, kinds, (extra or {}).get(s, {}))
    out = {}
    for d in days:
        today = {}
        for s, (rows, values, cols, kinds, base) in tables.items():
            i = rows.get(d)
            if i is None or (member and not member(s, d)):
                continue
            m = Row(values[i], cols, kinds, base)
            if "sector" in base:
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


def build_markets(years: int = YEARS, context_path: str | None = None) -> list[Market]:
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
    import context

    ctx_frame = context.load(context_path) if context_path and Path(context_path).exists() else context.download(period, spy, fx)
    print(f"::notice title=Contexto::dados desde {context.coverage(ctx_frame)}", flush=True)
    sessions = [d.date() for d in spy.index]
    days = sessions[WARMUP:][-years * SESSIONS_PER_YEAR:]
    names = dict(UCITS_ETFS)
    import fundamentals

    funds = fundamentals.load()
    first = pd.Timestamp(days[0]) - pd.Timedelta(days=10)
    frames = {s: fundamentals.daily(funds[s], df["Close"][df.index >= first]) for s, df in stock_hist.items() if s in funds}
    print(f"::notice title=Fundamentos::{len(frames)} de {len(stock_hist)} ações com fundamentos da SEC", flush=True)
    stocks = snapshots(stock_hist, days, {s: {"sector": sector_of[s]} for s in stock_hist},
                       member if membership else None, frames)
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
    close_cols = {c: k for k, c in enumerate(closes.columns)}
    close_values = closes.to_numpy(dtype=float)
    markets = []
    for d in days:
        share = sum(m["stage2"] for m in stocks[d].values()) / max(len(stocks[d]), 1)
        at = closes.index.searchsorted(pd.Timestamp(d), side="right") - 1
        markets.append(Market(
            hist=hist, fx=fx, stocks=stocks[d], etfs=etfs[d], as_of=d,
            sessions=[x for x in sessions if x <= d], zone=zone_for(share), stage2_share=share,
            closes=PriceRow(close_values[at], close_cols),
            context=context.on(ctx_frame, d),
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
    trades, bankrupt_on, sells = 0, None, []
    for mkt in markets:
        done = fill_pending(port, mkt) + check_exits(port, mkt)
        agents.trail_stops(port, mkt, agents.effective(port["params"], mkt.zone, strategy))
        trades += len(done)
        sells += [t for t in done if t["side"] == "venda"]
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
        "trade_stats": trade_stats(sells),
        "by_zone": {z: round(total / n, 6) for z, (total, n) in port.get("zone_stats", {}).items() if n >= 20},
        "daily": [round(x, 6) for x in daily.tolist()],
        "curve": [(h["date"], (h["equity"] / capital - 1) * 100) for h in port["history"]],
    }


def trade_stats(sells: list[dict]) -> dict:
    """Win rate, average win and loss, and typical holding time of the closed trades (after costs)."""
    rets = [t["ret"] for t in sells if t.get("ret") is not None]
    if not rets:
        return {}
    wins, losses = [r for r in rets if r > 0], [r for r in rets if r <= 0]
    held = sorted(t["held"] for t in sells if t.get("held") is not None)
    return {
        "trades": len(rets), "win_rate": round(len(wins) / len(rets), 3),
        "avg_win": round(statistics.fmean(wins), 4) if wins else 0.0,
        "avg_loss": round(statistics.fmean(losses), 4) if losses else 0.0,
        "expectancy": round(statistics.fmean(rets), 4),
        "median_days": held[len(held) // 2] if held else None,
    }


SHARED_IN_BACKTEST = [k for k in agents.COMMON if k not in agents.LIVE_TUNED]  # no news or calendar history
OFF_SHARE = 0.4  # a variant leaves each shared rule off this often


def variants(strategy: str, n: int) -> list[dict]:
    """The original parameters, then n random draws within the bounds (shared rules included, often off)."""
    base = dict(STRATEGIES[strategy][2])
    if strategy != "indexador":
        base.update({k: agents.COMMON[k] for k in SHARED_IN_BACKTEST})
    tunable = [k for k in base if k in PARAM_BOUNDS]
    rng = random.Random(f"pretrain-{strategy}{SEED}")
    out = [dict(base)]
    for _ in range(n if tunable else 0):
        p = dict(base)
        for k in tunable:
            if k in agents.OFFABLE and rng.random() < OFF_SHARE:
                p[k] = 0
                continue
            lo, hi, kind = PARAM_BOUNDS[k]
            v = rng.uniform(lo, hi)
            p[k] = int(round(v)) if kind is int else round(v, 4)
        out.append(p)
    return out


REVIEW_PATH = agents.REPO_AGENTS / "claude_review.json"
HISTORY_KEEP = 3  # earlier choices kept per strategy, each one a candidate in the next pre-training


def previous_choices(strategy: str) -> list[dict]:
    """The parameters chosen in the last pre-trainings ({"chosen", "until"}), newest first."""
    try:
        report = json.loads(REPORT_PATH.read_text())
    except (OSError, ValueError):
        return []
    r = report.get("strategies", {}).get(strategy) or {}
    out = ([{"chosen": r["chosen"], "until": (report.get("valid") or [None, None])[1]}] if r.get("chosen") else []) + list(r.get("history") or [])
    seen, kept = [], []
    for h in out:
        if h.get("chosen") and h["chosen"] not in seen:
            seen.append(h["chosen"])
            kept.append(h)
    return kept[:HISTORY_KEEP]


def incumbent_candidates(strategy: str, base: dict) -> list[dict]:
    """Earlier choices as candidates (zone parameters included), so a retrain only replaces them with something better."""
    out = []
    for h in previous_choices(strategy):
        cand = {**base, **{k: v for k, v in h["chosen"].items() if k in base or k == "por_zona"}}
        if cand != base and cand not in out:
            out.append(cand)
    return out


def claude_candidates(strategy: str, base: dict) -> list[dict]:
    """Claude's proposals for this strategy (agent_review.py), on top of the parameters chosen last time.

    They only join the candidates: they pass or fail the same tests as the random ones.
    """
    try:
        review = json.loads(REVIEW_PATH.read_text())
        previous = json.loads(REPORT_PATH.read_text()).get("strategies", {}).get(strategy, {}).get("chosen") or {}
    except (OSError, ValueError):
        return []
    start = {**base, **{k: v for k, v in previous.items() if k in base}}
    out = []
    for prop in (review.get("estrategias", {}).get(strategy) or {}).get("propostas") or []:
        params = {k: v for k, v in (prop.get("params") or {}).items() if k in base and k in PARAM_BOUNDS}
        cand = {**start, **params}
        if params and cand != start and all(cand != c["params"] for c in out):
            out.append({"params": cand, "changes": params, "hipotese": prop.get("hipotese", ""), "espera": prop.get("espera", "")})
    return out


def nudge(params: dict, rng: random.Random, size: float = 0.10) -> dict:
    """A neighbour: every numeric parameter that is on moves by up to +/-size, within its bounds."""
    p = dict(params)
    for k, v in params.items():
        if k in PARAM_BOUNDS and v:
            lo, hi, kind = PARAM_BOUNDS[k]
            new = min(hi, max(lo, v * rng.uniform(1 - size, 1 + size)))
            p[k] = int(round(new)) if kind is int else round(new, 4)
    return p


RECENCY = 0.85  # each older training year counts 15% less
GEN2_PARENTS, GEN2_CHILDREN = 6, 3
GENERATIONS = 2  # rounds of mutations after the random variants


def search_scale() -> float:
    """agents/search.json: more candidates on a dedicated computer, fewer in GitHub Actions (minutes)."""
    try:
        cfg = json.loads((agents.REPO_AGENTS / "search.json").read_text())
    except (OSError, ValueError):
        return 1.0
    where = "actions" if os.environ.get("GITHUB_ACTIONS") else "local"
    return float(os.environ.get("PRETRAIN_SCALE") or (cfg.get(where) or {}).get("scale") or 1.0)


SCALE = search_scale()
VARIANTS, VARIANTS_LONG = max(8, round(VARIANTS * SCALE)), max(8, round(VARIANTS_LONG * SCALE))
GEN2_PARENTS, GEN2_CHILDREN = max(3, round(GEN2_PARENTS * min(SCALE, 2) ** 0.5)), max(2, round(GEN2_CHILDREN * min(SCALE, 2) ** 0.5))
# A candidate must beat the original parameters in this share of the training years (was 3 of 4: too strict to
# let the search climb; the plateau, crisis, exam and Monte Carlo tests still guard against luck).
CONSISTENCY = 0.65
NEIGHBOURS, PLATEAU = 3, 0.7
MIN_TRADES = 20
ZONE_TRIES, ZONE_MIN_SHARE = 3, 0.10
GREEDY_LEVELS = (0.25, 0.5, 0.75)  # where in each rule's range the final phase tries it
GREEDY_MAX = 4  # rules the final phase may switch on
GREEDY_GAIN = 0.02  # each must add 2% to the training points (at least 20 points)


def greedy_rules(strategy: str, chosen: dict, train_mk: list[Market], valid: list[Market], windows: dict,
                 exam: int) -> tuple[dict, list[dict], dict | None]:
    """The final phase: switch on, one at a time, the shared rules that help this strategy most.

    Each rule that is off is tried at GREEDY_LEVELS of its range on the training years; the best level of
    each is kept as a candidate. Candidates are added in order of gain, each only if it adds GREEDY_GAIN to
    the training points, does not lose on the exam year and does not fall deeper in the crises.
    Returns (parameters, the log of what was tried, the new exam result or None).
    """
    base = simulate(strategy, chosen, train_mk)["points"]
    tries = []
    for k in [k for k in SHARED_IN_BACKTEST if k in agents.OFFABLE and not chosen.get(k)]:
        lo, hi, kind = PARAM_BOUNDS[k]
        levels = sorted({int(round(lo + f * (hi - lo))) if kind is int else round(lo + f * (hi - lo), 4) for f in GREEDY_LEVELS})
        best = max(((simulate(strategy, {**chosen, k: v}, train_mk)["points"], v) for v in levels), key=lambda t: t[0])
        tries.append({"param": k, "value": best[1], "train_gain": best[0] - base})
    tries.sort(key=lambda t: t["train_gain"], reverse=True)
    crashes = crash_test(strategy, chosen, windows)
    exam_result, added = None, 0
    for t in tries:
        need = max(abs(base) * GREEDY_GAIN, 20)
        t["kept"] = False
        if added >= GREEDY_MAX or t["train_gain"] < need:
            continue
        cand = {**chosen, t["param"]: t["value"]}
        points = simulate(strategy, cand, train_mk)["points"]  # on top of what was added before
        if points < base + need:
            t["train_gain"] = points - base
            continue
        ex = simulate(strategy, cand, valid)
        cr = crash_test(strategy, cand, windows)
        t.update(exam_gain=ex["points"] - exam, train_gain=points - base)
        if ex["points"] >= exam and survives(cr, crashes):
            chosen, base, exam, crashes, exam_result, added = cand, points, ex["points"], cr, ex, added + 1
            t["kept"] = True
    return chosen, tries, exam_result


def overall(years: list[float], exam: float) -> float:
    """What a run tries to raise: the recency-weighted training years and the exam year, equally."""
    return 0.5 * weighted(years) + 0.5 * exam


def weighted(years: list[float]) -> float:
    """Recency-weighted mean of the yearly points: the latest year weighs 1, the one before 0.85, ..."""
    w = [RECENCY ** (len(years) - 1 - i) for i in range(len(years))]
    return sum(a * b for a, b in zip(w, years)) / sum(w) if years else 0.0


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
    """Pick parameters that are consistently good, robust, survive the crises and hold up on the exam year.

    1. Generation 1: the original and random variants (shared rules included). Each runs every training year
       separately (fresh 1000 EUR each) and must beat the original in at least CONSISTENCY (65%) of the years, with at
       least MIN_TRADES trades (not for low-turnover strategies). Ranked by recency-weighted points.
    2. Generation 2: the best GEN2_PARENTS get GEN2_CHILDREN mutations each (agents.mutate), judged the same way.
    3. The best candidate must be on a plateau (NEIGHBOURS nearby values keep PLATEAU of its score, so it is
       not a lucky spike) and must not go bankrupt, or fall CRASH_SLACK deeper than the original, in a crisis.
    4. It is adopted only if it scores at least as well as the original on the exam year and its Monte Carlo
       risk of ruin is not clearly higher.
    5. Market zones: for each zone with ZONE_MIN_SHARE of the training days, ZONE_TRIES mutations of the chosen
       parameters are tried in that zone only; the best is kept if it adds 2% to the training points and does
       not lose on the exam.
    """
    markets = _MARKETS
    split = max(len(markets) - EXAM_SESSIONS, len(markets) * 2 // 3)
    train_mk, blocks, valid = markets[:split], yearly_blocks(markets[:split]), markets[split:]
    windows = crisis_windows(train_mk)
    low_turnover = strategy in agents.LOW_TURNOVER
    rng = random.Random(f"evolve-{strategy}{SEED}")

    def run_years(p: dict) -> tuple[list[int], int]:
        runs = [simulate(strategy, p, b) for b in blocks]
        return [r["points"] for r in runs], sum(r["trades"] for r in runs)

    pool = variants(strategy, VARIANTS_LONG if len(blocks) > LONG_YEARS else VARIANTS)
    n_random = len(pool)
    claude = claude_candidates(strategy, pool[0]) if strategy != "indexador" else []
    pool += [c["params"] for c in claude]
    history = previous_choices(strategy) if strategy != "indexador" else []
    incumbents = incumbent_candidates(strategy, pool[0]) if strategy != "indexador" else []
    first_incumbent = len(pool)
    pool += incumbents
    scored = [run_years(p) for p in pool]
    base = scored[0][0]
    need = math.ceil(CONSISTENCY * len(blocks))  # years a candidate must beat the original in

    def ok(i: int) -> bool:
        years, trades = scored[i]
        if first_incumbent <= i < first_incumbent + len(incumbents):  # passed before: still faces plateau, crises, exam and Monte Carlo
            return True
        return i == 0 or (sum(a > b for a, b in zip(years, base)) >= need and (low_turnover or trades >= MIN_TRADES))

    def ranked() -> list[int]:
        return sorted((i for i in range(len(pool)) if ok(i)), key=lambda i: weighted(scored[i][0]), reverse=True)

    def parents() -> list[int]:
        """The consistent candidates first, then the best scores of the rest, the incumbent and the original: a local
        search keeps going even when no random variant beats the original in enough years."""
        out = [i for i in ranked() if i != 0][:GEN2_PARENTS]
        rest = sorted((i for i in range(1, len(pool)) if i not in out), key=lambda i: weighted(scored[i][0]), reverse=True)
        out += rest[: max(GEN2_PARENTS - len(out), 2)]
        if incumbents and first_incumbent not in out:
            out.append(first_incumbent)
        return out + [0]

    if len(pool) > 1:  # generations 2 and 3: mutations of the best so far
        for _generation in range(GENERATIONS):
            for i in parents():
                for _ in range(GEN2_CHILDREN):
                    child = dict(pool[i])
                    agents.mutate(child, rng, strategy)
                    child = {k: v for k, v in child.items() if k in pool[0]}
                    if child in pool:
                        continue
                    pool.append(child)
                    scored.append(run_years(child))
    crashes = {0: crash_test(strategy, pool[0], windows)}
    plateau = {}
    best_i = 0
    for i in ranked():  # the best consistent candidate that is robust and survives the crises
        if i == 0:
            break
        mine = weighted(scored[i][0])
        near = [weighted(run_years(nudge(pool[i], rng))[0]) for _ in range(NEIGHBOURS)]
        plateau[i] = near
        if mine > 0 and sum(n >= PLATEAU * mine for n in near) < NEIGHBOURS - 1:
            continue
        crashes[i] = crash_test(strategy, pool[i], windows)
        if survives(crashes[i], crashes[0]):
            best_i = i
            break
    best = pool[best_i]
    best_valid = simulate(strategy, best, valid)
    default_valid = best_valid if best_i == 0 else simulate(strategy, pool[0], valid)
    full_best = simulate(strategy, best, markets)
    full_default = full_best if best_i == 0 else simulate(strategy, pool[0], markets)
    mc_best, mc_default = monte_carlo(full_best["daily"]), monte_carlo(full_default["daily"])
    riskier = mc_best.get("p_ruin", 0) > mc_default.get("p_ruin", 0) + MC_RUIN_SLACK
    adopt = best_i != 0 and best_valid["points"] >= default_valid["points"] and not riskier
    chosen, full, mc = (best, full_best, mc_best) if adopt else (pool[0], full_default, mc_default)
    chosen_valid = best_valid if adopt else default_valid

    # Final phase: switch on the shared rules that help this strategy most (credit, VIX, …).
    greedy_log = []
    if strategy != "indexador":
        chosen, greedy_log, greedy_exam = greedy_rules(strategy, chosen, train_mk, valid, windows, chosen_valid["points"])
        if greedy_exam is not None:
            chosen_valid, adopt = greedy_exam, True
            full = simulate(strategy, chosen, markets)
            mc = monte_carlo(full["daily"])

    # Market zones: parameters for one zone at a time, on top of the chosen ones.
    zones_tried = {}
    if strategy != "indexador" and any(k in PARAM_BOUNDS for k in chosen):
        counts = {z: sum(m.zone == z for m in train_mk) for z in agents.ZONES}
        train_points = simulate(strategy, chosen, train_mk)["points"]
        for zone, n in counts.items():
            if n < ZONE_MIN_SHARE * len(train_mk):
                continue
            tries = []
            for _ in range(ZONE_TRIES):
                alt = {k: v for k, v in chosen.items() if k != "por_zona"}
                agents.mutate(alt, rng, strategy)
                diff = {k: v for k, v in alt.items() if k in PARAM_BOUNDS and k not in agents.LIVE_TUNED and chosen.get(k) != v}
                if not diff:
                    continue
                cand = {**chosen, "por_zona": {**(chosen.get("por_zona") or {}), zone: diff}}
                tries.append((simulate(strategy, cand, train_mk)["points"], cand, diff))
            if not tries:
                continue
            pts, cand, diff = max(tries, key=lambda t: t[0])
            gain_needed = abs(train_points) * 0.02 if train_points else 20
            exam = simulate(strategy, cand, valid) if pts >= train_points + gain_needed else None
            kept = exam is not None and exam["points"] >= chosen_valid["points"]
            zones_tried[zone] = {"train": pts, "base": train_points, "exam": exam["points"] if exam else None,
                                 "exam_base": chosen_valid["points"], "kept": kept, "diff": diff}
            if kept:
                chosen, train_points, chosen_valid = cand, pts, exam
        if any(z["kept"] for z in zones_tried.values()):
            full = simulate(strategy, chosen, markets)
            mc = monte_carlo(full["daily"])
            adopt = True

    # Keep the last choice unless the new one is better overall (training years and exam together), so consecutive
    # runs can only keep or improve each strategy.
    kept_previous = None
    new_obj = overall(run_years(chosen)[0], chosen_valid["points"]) if history and strategy != "indexador" else None
    for h in history if new_obj is not None else []:  # the last choices (up to HISTORY_KEEP): the best one stays
        previous = {**pool[0], **h["chosen"]}
        if previous != chosen:
            prev_exam = simulate(strategy, previous, valid)
            prev_obj = overall(run_years(previous)[0], prev_exam["points"])
            if prev_obj > new_obj:
                kept_previous = {"previous": round(prev_obj, 1), "new": round((kept_previous or {}).get("new", new_obj), 1)}
                new_obj = prev_obj
                chosen, chosen_valid, adopt = previous, prev_exam, True
                full = simulate(strategy, chosen, markets)
                mc = monte_carlo(full["daily"])
                greedy_log = [dict(t, kept=False) for t in greedy_log]
                zones_tried = {z: dict(v, kept=False) for z, v in zones_tried.items()}
                crashes["kept_previous"] = crash_test(strategy, chosen, windows)

    importance = parameter_importance(strategy, chosen, train_mk, valid, chosen_valid["points"])
    claude_log = []
    for j, c in enumerate(claude):
        i = n_random + j
        years, trades = scored[i]
        claude_log.append({
            "changes": c["changes"], "hipotese": c["hipotese"], "espera": c["espera"],
            "beats_years": sum(a > b for a, b in zip(years, base)), "years": len(years), "trades": trades,
            "score": round(weighted(years), 1), "score_original": round(weighted(base), 1),
            "consistent": ok(i), "picked": best_i == i, "adopted": adopt and best_i == i,
        })

    print(f"{strategy}: {len(pool)} candidatos; anos {scored[best_i][0]} vs {base}; vizinhos {plateau.get(best_i)}; "
          f"crises {crashes.get(best_i)}; exame {best_valid['points']} vs {default_valid['points']}; "
          f"fase final {[t['param'] for t in greedy_log if t.get('kept')]}; "
          f"zonas {[(z, v['kept']) for z, v in zones_tried.items()]} → {'adotado' if adopt else 'mantém'}",
          file=sys.stderr, flush=True)
    return strategy, {
        "default": pool[0], "best": best, "adopted": adopt, "chosen": chosen,
        "train": {"default": {"points": sum(base), "years": base},
                  "best": {"points": sum(scored[best_i][0]), "years": scored[best_i][0]}},
        "valid": {"default": strip(default_valid), "best": strip(chosen_valid if adopt else best_valid)},
        "crises": crashes.get("kept_previous") or crashes.get(best_i if adopt else 0, crashes[0]),
        "monte_carlo": mc, "by_zone": full["by_zone"], "zones": zones_tried, "importance": importance, "greedy": greedy_log,
        "claude": claude_log,
        "history": history,
        "kept_previous": kept_previous,
        "incumbents": [{"score": round(weighted(scored[first_incumbent + j][0]), 1), "picked": best_i == first_incumbent + j,
                        "adopted": adopt and best_i == first_incumbent + j} for j in range(len(incumbents))],
        "search": {"candidates": len(pool), "generation2": len(pool) - n_random - len(claude) - len(incumbents), "claude": len(claude), "incumbents": len(incumbents),
                   "plateau": plateau.get(best_i), "recency": RECENCY, "min_trades": MIN_TRADES},
        "year": strip(full), "curve": light_curve(full["curve"]),
    }


def parameter_importance(strategy: str, chosen: dict, train_mk: list[Market], valid: list[Market], exam: int) -> dict:
    """What each parameter is worth to this strategy, everything else equal.

    A parameter in use is reset to its default (its own strategy's, or off for a shared rule): the points lost
    are what it adds. A shared rule that is off is switched on at the middle of its range: the points gained
    are what it would add. Both on the training years and on the exam year.
    """
    if strategy == "indexador":
        return {}
    defaults = {**{k: agents.COMMON[k] for k in SHARED_IN_BACKTEST}, **STRATEGIES[strategy][2]}
    base = simulate(strategy, chosen, train_mk)["points"]
    out = {}
    for k in sorted(k for k in set(chosen) | set(SHARED_IN_BACKTEST) if k in PARAM_BOUNDS and k not in agents.LIVE_TUNED):
        value = chosen.get(k, 0)
        if value and value != defaults.get(k):
            alt, used = {**chosen, k: defaults.get(k, 0)}, True
        elif not value and k in agents.OFFABLE:
            lo, hi, kind = PARAM_BOUNDS[k]
            mid = int(round((lo + hi) / 2)) if kind is int else round((lo + hi) / 2, 4)
            alt, used = {**chosen, k: mid}, False
        else:
            continue
        t = simulate(strategy, alt, train_mk)["points"]
        e = simulate(strategy, alt, valid)["points"]
        out[k] = {"used": used, "value": value if used else alt[k],
                  "train": (base - t) if used else (t - base), "exam": (exam - e) if used else (e - exam)}
    return out


def importance_summary(results: dict) -> list[dict]:
    """Across strategies: in how many each parameter helps (training and exam both up), and by how much."""
    rows: dict[str, dict] = {}
    for strategy, r in results.get("strategies", {}).items():
        for k, v in (r.get("importance") or {}).items():
            row = rows.setdefault(k, {"param": k, "tested": 0, "helps": 0, "hurts": 0, "train": 0.0, "exam": 0.0, "where": []})
            row["tested"] += 1
            row["train"] += v["train"]
            row["exam"] += v["exam"]
            if v["train"] > 0 and v["exam"] >= 0:
                row["helps"] += 1
                row["where"].append(strategy)
            elif v["train"] < 0 and v["exam"] <= 0:
                row["hurts"] += 1
    for row in rows.values():
        row["train"] = round(row["train"] / row["tested"])
        row["exam"] = round(row["exam"] / row["tested"])
    return sorted(rows.values(), key=lambda r: (r["helps"] - r["hurts"], r["train"]), reverse=True)


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


DAILY_TAIL = SESSIONS_PER_YEAR  # the last year day by day (for 1D, 1W and 1M on the portal), weekly before


def light_curve(curve: list) -> list:
    """Weekly points for the older years and daily points for the last one: light for the page, exact for short periods."""
    return curve[:-DAILY_TAIL][::5] + curve[-DAILY_TAIL:] if len(curve) > DAILY_TAIL else curve


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
        if "por_zona" not in r["chosen"]:
            params.pop("por_zona", None)
        state.setdefault("shadows", {}).pop(cfg["id"], None)  # the shadow copies restart from the new parameters
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
               "Variações por estratégia; só passam as que batem os parâmetros originais em 65% dos anos de treino, "
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
    turned_on = []
    for strategy, r in results["strategies"].items():
        kept = [f"{t['param']} {t['value']}" for t in r.get("greedy") or [] if t.get("kept")]
        if kept:
            cfg = by_strategy.get(strategy, {"emoji": "🤖", "name": strategy})
            turned_on.append(f"{cfg['emoji']} {cfg['name']}: {', '.join(kept)}")
    fields.append(("🎛️ Fase final: regras ligadas (só se melhoram o treino sem piorar o exame nem as crises)",
                   "\n".join(turned_on) or "nenhuma passou os três testes"))
    reviewed = []
    for strategy, r in results["strategies"].items():
        props = r.get("claude") or []
        if props:
            cfg = by_strategy.get(strategy, {"emoji": "🤖", "name": strategy})
            marks = " ".join("✅" if p["adopted"] else "🟡" if p["consistent"] else "❌" for p in props)
            reviewed.append(f"{cfg['emoji']} {cfg['name']}: {marks}")
    if reviewed:
        fields.append(("🤖 Propostas do Claude (✅ adotada · 🟡 consistente mas não escolhida · ❌ falhou os anos)",
                       "\n".join(reviewed)[:1024]))
    summary = importance_summary(results)
    if summary:
        import strategy_docs

        top = [f"`{r['param']}` · ajuda em {r['helps']} de {r['tested']} ({', '.join(r['where'][:4])}) · "
               f"treino {r['train']:+d} · exame {r['exam']:+d} pts" for r in summary if r["helps"]][:8]
        flop = [f"`{r['param']}`" for r in summary if not r["helps"]]
        fields.append(("🔬 Que parâmetros contam (cada um ligado/desligado, o resto igual)",
                       ("\n".join(top) or "nenhum ajuda de forma consistente")
                       + (f"\nSem efeito útil: {', '.join(flop[:10])}" if flop else ""))[:1024])
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
    refs = reference_lines(series)
    top = interactive.leaders(series)
    files = {"pretreino.png": lines_chart(series, f"Pré-treino: {years:g} anos (a cores: os 5 melhores e o Indexador)",
                                          refs, top)}
    details = []
    for strategy, r in results["strategies"].items():
        if "year" not in r:
            continue
        cfg = by_strategy.get(strategy, {"id": strategy, "name": strategy, "emoji": "🤖", "strategy": strategy, "params": {}})
        params = r.get("chosen") or cfg.get("params", {})
        y, v = r["year"], r.get("valid", {})
        details.append({
            "id": cfg.get("id", strategy), "name": cfg["name"], "emoji": cfg["emoji"],
            "strategy": agents.strategy_info({**cfg, "strategy": strategy}, params),
            "backtest": {
                "return_pct": round(y["return"] * 100, 1), "max_dd_pct": round(y["max_drawdown"] * 100, 1),
                "sharpe": y.get("sharpe"), "trades": y.get("trades"), "bankrupt": y.get("bankrupt"),
                "adopted": r.get("adopted"), "chosen": r.get("chosen") or {},
                "exam": {k: {**x, "return_pct": round(x.get("return", 0) * 100, 1),
                             "max_dd_pct": round(x.get("max_drawdown", 0) * 100, 1)} for k, x in v.items()},
                "years": (r.get("train", {}).get("best") or {}).get("years", []),
                "train_start": int(results["train"][0][:4]) if results.get("train") else None,
                "crises": {k: {"return_pct": round(c["return"] * 100, 1), "max_dd_pct": round(c["max_drawdown"] * 100, 1),
                               "bankrupt": c.get("bankrupt")} for k, c in (r.get("crises") or {}).items()},
                "monte_carlo": r.get("monte_carlo") or {},
                "importance": r.get("importance") or {},
                "zones": {z: {"kept": v["kept"], "diff": v["diff"]} for z, v in (r.get("zones") or {}).items()},
                "greedy": r.get("greedy") or [],
                "claude": r.get("claude") or [],
                "review": review_for(strategy),
            },
        })
    link = interactive.publish(f"🧠 Pré-treino dos agentes: {years:g} anos", series, refs, top, agents=details, kind="pretreino",
                               subtitle=f"Treino {results['train'][0]} a {results['train'][1]} · exame {results['valid'][0]} "
                                        f"a {results['valid'][1]} · parâmetros escolhidos, custos e impostos incluídos")
    card = embed(f"🧠 Pré-treino dos agentes: {years:g} anos, dia a dia", "\n".join(lines), color=BLUE,
                 fields=fields, image="pretreino.png",
                 footer="Backtest: resultados passados simulados, não garantem resultados futuros.")
    if link:
        card["url"] = link  # the title opens the interactive chart too
    return [card], files


def review_for(strategy: str) -> dict:
    """Claude's assessment of this strategy, for the portal."""
    try:
        review = json.loads(REVIEW_PATH.read_text())
    except (OSError, ValueError):
        return {}
    r = review.get("estrategias", {}).get(strategy) or {}
    return {k: r[k] for k in ("parecer", "fortes", "fracos", "manter", "regras_novas") if k in r} | (
        {"date": review.get("date")} if r else {})


REPORT_TITLE = "🧠 Pré-treino dos agentes"  # only the latest report stays in the channel


def post_report(results: dict, applied: list[str] | None, dry_run: bool, channel: str) -> int:
    """The report, with a button to its interactive chart, replacing the previous report in that channel."""
    embeds, files = report(results, applied)
    buttons = [(interactive.BUTTON, embeds[0]["url"])] if embeds and embeds[0].get("url") else None
    return send(embeds, files, dry_run=dry_run, **route(channel), replace=REPORT_TITLE, buttons=buttons)


def finish(results: dict, today: date, apply_params: bool, dry_run: bool, partial: bool) -> int:
    """Save the results (keeping other strategies on a partial run), apply them and post the report."""
    if partial and REPORT_PATH.exists():
        previous = json.loads(REPORT_PATH.read_text())
        results = {**results, "strategies": {**previous.get("strategies", {}), **results["strategies"]}}
    REPORT_PATH.write_text(json.dumps(results, indent=1, default=str) + "\n")
    applied = apply(results, today) if apply_params else None
    return post_report(results, applied, dry_run, "agentes")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Set the agents' parameters in agents/state.json")
    parser.add_argument("--dry-run", action="store_true", help="Print the report instead of posting")
    parser.add_argument("--estrategias", default="", help="Comma-separated strategies to train (default: all)")
    parser.add_argument("--anos", type=int, default=YEARS, help=f"Years of history to replay (default {YEARS})")
    parser.add_argument("--semente", default="", help="Seed for the random candidates (a different one explores other parameters)")
    parser.add_argument("--contexto", help="CSV with the market context (context.py --guardar), shared by every strategy")
    parser.add_argument("--saida", help="Only train and write these strategies' results to this JSON file "
                                        "(one job of a parallel run)")
    parser.add_argument("--juntar", help="Merge the JSON results in this folder, then save, apply and report")
    parser.add_argument("--republicar", action="store_true", help="Only post the report of the saved results again")
    parser.add_argument("--canal", default="agentes", choices=["agentes", "estado", "mercado"],
                        help="Channel for --republicar (default: agentes)")
    args = parser.parse_args()
    global SEED
    if args.semente:
        SEED = f"-{args.semente}"
        os.environ["PRETRAIN_SEED"] = SEED  # the worker processes read it too

    if args.republicar:
        return post_report(json.loads(REPORT_PATH.read_text()), None, args.dry_run, args.canal)

    if args.juntar:
        parts = [json.loads(f.read_text()) for f in sorted(Path(args.juntar).rglob("*.json"))]
        if not parts:
            print("Nenhum resultado para juntar.", file=sys.stderr)
            return 1
        results = {**parts[0], "strategies": {k: v for part in parts for k, v in part["strategies"].items()}}
        return finish(results, date.fromisoformat(results["valid"][1]), args.apply, args.dry_run, partial=True)

    markets = build_markets(args.anos, args.contexto)
    print(f"{len(markets)} dias de {markets[0].as_of} a {markets[-1].as_of}", file=sys.stderr)
    only = {s.strip() for s in args.estrategias.split(",") if s.strip()} or None
    if args.saida:
        try:
            results = pretrain(markets, only)
        except Exception as e:  # an annotation is readable on the run page even when the job log is not
            import traceback

            where = " ← ".join(f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(traceback.extract_tb(e.__traceback__)[-4:]))
            print(f"::error title=Treino {args.estrategias}::{e!r} em {where}", flush=True)
            raise
        Path(args.saida).parent.mkdir(parents=True, exist_ok=True)
        Path(args.saida).write_text(json.dumps(results, default=str))
        return 0
    results = pretrain(markets, only | {"indexador"} if only else None)
    return finish(results, markets[-1].as_of, args.apply, args.dry_run, partial=bool(only))


if __name__ == "__main__":
    sys.exit(main())
