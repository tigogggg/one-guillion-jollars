"""Teams: the strategies as signal desks under one portfolio, with a portfolio manager and a risk manager, replayed over
the pre-training years and compared, the same way, with the S&P 500 and with each strategy alone.

A team (TEAMS):
- signal desks (members): strategies with the parameters their agents trade with. Each runs its own day loop (the
  Rust engine) on its own capital; what it holds each day, as weights of its equity, is its recommendation.
  With entrada="sinais" a desk is an alpha model instead (LEAN's insights): run without its own risk rules, it gives
  what it wants to hold, ranked (engine.run(signals=True)), as weights summing to 1; the team sizes them once
  (dimensao: as given or by inverse volatility; max_nome, max_setor) and its risk manager applies the risk once.
- portfolio manager: the team's capital across the desks each day: "igual" (the same for each), "momento" (in
  proportion to each desk's return over the last 12 months, only those above 0; at most MAX_DESK each) or "risco"
  (in inverse proportion to each desk's volatility over the last 3 months).
- risk manager: the team's exposure by the S&P 500's trend (above its 200-session average: all of it; below:
  `queda` of it, the rest in cash), and at most `max_nome` of the team in one stock.
- execution: target of each stock = exposure x sum over desks (desk's capital x the stock's weight in the desk);
  orders at the next session's open, only for a holding more than `banda` of the team away from its target (the desks'
  buys and sells of the same stock cancel out: less turnover and less tax). The active broker's costs, buy-side taxes,
  28% on each year's net realised gains paid on 31 August of the next year.

Grading, the same for teams, desks alone and the index: each training year's profile grade minus the index's
(agents.relative_points), from one continuous run; the exam is the last year; `overall` as in the pre-training. Taxes
are inside the curves (paid when due); the index's, deferred, are not (at the end: after-tax returns of all).
Two modes (EQUIPAS_MODO): "evoluir" (default): a population of POP_SIZE teams with different defaults; each
generation judges them on the training years and on validation (the VAL_YEARS before the exam, which stays sealed),
keeps the KEEP that do well in both and breeds as many new teams from them (mutations, crossings); the population is
saved (agents/lab/equipas_populacao-<profile>.json) and carries on in the next run. "procura": the eight fixed TEAMS,
each searching its parameters at random.

Results go to the private repo (agents/lab/equipas-<profile>-<date>.json, equipas_historico-<profile>.json), never to the Actions log.

    uv run python scripts/teams.py --contexto ctx/context.csv            # (PRICE_CACHE_DIR with the prices)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

import agents

ALL = ["guardiao", "rotador", "foguetao", "analista", "quant", "monge", "abutre", "pendulo", "cacador", "sniper",
       "campeao", "pescador", "deriva", "kamikaze"]
# Diversified teams: who sits at the desks differs; the rest (manager, risk, execution) each team searches in PARAMS.
# "auto:<rule>": members chosen from the training years only (never the exam).
TEAMS = {
    "todos": {"nome": "🏛️ Equipa Total", "membros": ALL, "ideia": "todas as estratégias como mesas"},
    "robusta": {"nome": "🧱 Equipa Robusta", "membros": ["analista", "quant", "pescador", "cacador", "abutre", "campeao"],
                "ideia": "as que melhor aguentam anos que não viram (walk-forward)"},
    "tendencia": {"nome": "🚀 Equipa Tendência", "membros": ["foguetao", "sniper", "cacador", "kamikaze", "deriva", "rotador"],
                  "ideia": "momentum, rompimentos e rotação: seguir o que sobe"},
    "qualidade": {"nome": "💎 Equipa Qualidade e Valor", "membros": ["analista", "quant", "abutre", "monge", "pescador", "campeao"],
                  "ideia": "empresas sólidas, baratas ou em queda passageira"},
    "etf": {"nome": "🌍 Equipa ETF", "membros": ["guardiao", "rotador", "pendulo"],
            "ideia": "só fundos de índice: alocação e rotação entre mercados, obrigações e ouro"},
    "descorrelacionada": {"nome": "🧩 Equipa Descorrelacionada", "membros": "auto:descorrelacao",
                          "ideia": "as 5 que menos se mexem juntas nos anos de treino (a melhor e as mais diferentes dela)"},
    "campeas": {"nome": "🏆 Equipa Campeãs", "membros": "auto:melhores",
                "ideia": "as 4 melhores nos anos de treino"},
    "nucleo": {"nome": "🛰️ Equipa Núcleo e Satélites", "membros": ["analista", "quant", "pescador", "cacador", "abutre", "campeao"],
               "procura": {"nucleo": [0.5, 0.3, 0.6]},
               "ideia": "uma parte fixa no S&P 500 (núcleo, sem filtros) e o resto nas mesas robustas (satélites)"},
}
# What each team searches (one value of each; the template first): the portfolio manager, the risk manager, execution.
PARAMS = {
    # capital across the desks: the same; by 12-month return; inverse 3-month volatility; 12-month Sharpe; the 3 best;
    # a mild tilt (x1.5 above the desks' average return, x0.5 below; Ehsani & Linnainmaa vs Goyal & Wahal)
    # risk parity: each desk the same share of the team's risk (covariance of the last 12 months, shrunk to the diagonal)
    "gestor": ["igual", "momento", "risco", "sharpe", "top3", "inclinacao", "paridade"],
    "corte_mesa": [0, 1],          # pod rule: a desk down more than its yearly volatility gets half, 1.5x: nothing
    "normalizar": [1, 0],          # 1: the desks' picks fill the exposure (no cash left because a desk is half in cash)
    "consenso": [0.0, 0.5, 1.0],   # extra weight for a stock several desks hold (x (1 + c x (desks - 1)))
    "min_mesas": [1, 2],           # only stocks held by at least this many desks
    "max_posicoes": [20, 0, 10, 30],  # at most N stocks (the biggest targets); 0: no limit
    "max_nome": [0.10, 0.05, 0.08, 0.15],  # at most this share of the team in one stock
    "tendencia": [1, 0],           # S&P 500 below its 200-session average: exposure x queda
    "queda": [0.5, 0.0, 0.25, 0.75],
    "refugio": ["", "EUNH.DE", "4GLD.DE"],  # where the exposure the trend takes away goes (euro bonds, gold); "": cash
    "vol_alvo": [0.0, 0.10, 0.15, 0.20],  # yearly volatility aimed at (exposure x target / last 3 months' vol, at most 1)
    "travao_dd": [0.0, 0.15, 0.25],  # the team down this much from its top: half the exposure until it recovers
    "banda": [0.02, 0.01, 0.04],   # trade a stock only when more than this share of the team away from its target
    "cada": [5, 1, 21],            # decide every N sessions (1: daily, 5: weekly, 21: monthly)
    "ajuste": [1.0, 0.5, 0.25],    # move this part of the way to the target each time (Gârleanu & Pedersen)
    "colheita": [0, 1],            # December: realise the losses (sell and buy back) to offset the year's gains
    # what the desks give the team: "posicoes" what each holds (its cash and its own risk sizing included); "sinais"
    # its insights (LEAN's alpha model: what it wants to hold, ranked, run without its own risk rules), sized here once
    "entrada": ["posicoes", "sinais"],
    "dimensao": ["peso", "vol"],   # each stock's size: as the desks give it; by inverse 3-month volatility (same total)
    "max_setor": [0.0, 0.25, 0.35],  # at most this share of the team in one sector (0: no cap)
}
SEARCH = int(os.environ.get("EQUIPAS_PROCURA") or 80)  # random configurations per team, besides the template
TREND_DAYS = 200
MOMENTUM_DAYS = 252
VOL_DAYS = 63
MAX_DESK = 0.4
OUT_DIR = agents.REPO_AGENTS / "lab"


DESKS = os.environ.get("EQUIPAS_MESAS") or "padrao"  # "padrao": the strategies' original parameters (never fitted)


def desk_params(strategy: str, profile: str) -> dict:
    """The parameters this profile's agent of the strategy trades with (as the pre-training simulates them); with
    EQUIPAS_MESAS=padrao the original ones: the trained ones were chosen on these same years, so a team built on them
    inherits that optimism (the walk-forward of the team cannot see it)."""
    import pretrain

    params = {**{k: agents.COMMON[k] for k in pretrain.SHARED_IN_BACKTEST}, **agents.STRATEGIES[strategy][2]}
    if DESKS == "padrao":
        return params
    try:
        report = json.loads(agents.pretrain_path(profile).read_text())
        params.update(report["strategies"][strategy].get("chosen") or {})
    except (OSError, ValueError, KeyError):
        pass
    return params


def eur_matrix(markets: list, symbols: list[str], field: str) -> np.ndarray:
    """Prices in euros, days x symbols (the last known value carried; NaN before a symbol's first price)."""
    hist = markets[-1].hist
    days = pd.DatetimeIndex([pd.Timestamp(m.as_of) for m in markets])
    out = np.full((len(days), len(symbols)), np.nan)
    rates: dict[str, np.ndarray] = {}
    for j, s in enumerate(symbols):
        df = hist.get(s)
        if df is None or not len(df) or field not in df:
            continue
        idx = df.index.tz_localize(None) if getattr(df.index, "tz", None) is not None else df.index
        col = pd.Series(df[field].to_numpy(dtype=float), index=idx.normalize())
        col = col[~col.index.duplicated(keep="last")]
        px = col.reindex(days, method="ffill" if field == "Close" else None).to_numpy()
        ccy = agents.currency_of(s)
        if ccy != "EUR":
            if ccy not in rates:
                fx = agents.fx_series(markets[-1], ccy)
                fx = fx[~fx.index.duplicated(keep="last")] if fx is not None else None
                rates[ccy] = fx.reindex(days, method="ffill").bfill().to_numpy() if fx is not None else np.full(len(days), np.nan)
            px = px / rates[ccy] / (100 if ccy == "GBp" else 1)
        out[:, j] = px
    return out


def desk_run(strategy: str, params: dict, markets: list, capital: float, signals: bool = False) -> dict:
    """One desk alone: its daily equity and, per day, how much of each stock it holds. signals=True: the desk as an
    alpha model (LEAN's insights): without its own risk rules (ignora_risco: no volatility scaling, sector cap or
    brake, the team has its own) and with its insights, what it wants to hold on each decision day, ranked."""
    import engine

    if signals:
        params = {**params, "ignora_risco": True}
    r = engine.run(strategy, params, markets, capital, detail=True, signals=signals)
    days = [engine.day_num(m.as_of) for m in markets]
    pos = {d: i for i, d in enumerate(days)}
    equity = np.full(len(days), np.nan)
    for d, e in r["history"]:
        if d in pos:
            equity[pos[d]] = e
    equity = pd.Series(equity).ffill().fillna(capital).to_numpy()
    changes: dict[int, dict[str, float]] = {}
    for t in r["trades"]:
        day, side, sym, qty = t[0], t[1], t[2], t[3]
        if side not in (0, 1) or sym is None or day not in pos:
            continue
        changes.setdefault(pos[day], {}).setdefault(sym, 0.0)
        changes[pos[day]][sym] += qty if side == 0 else -qty
    out = {"equity": equity, "changes": changes, "final": r["final"], "tax_owed": r["tax_owed"], "trades": r["n_trades"]}
    if signals:
        out["signals"] = {}
        for day, sym, conf in r["signals"]:
            if day in pos:
                out["signals"].setdefault(pos[day], {})[sym] = float(conf)
    return out


def desk_weights(desk: dict, symbols: dict[str, int], close: np.ndarray) -> np.ndarray:
    """Days x symbols: each holding's share of the desk's equity at that day's close (float32: 14 desks x 20 years)."""
    n = len(desk["equity"])
    w = np.zeros((n, len(symbols)), dtype=np.float32)
    held = np.zeros(len(symbols))
    for i in range(n):
        for s, dq in desk["changes"].get(i, {}).items():
            held[symbols[s]] = max(0.0, held[symbols[s]] + dq)
        if desk["equity"][i] > 0:
            w[i] = np.nan_to_num(held * close[i]) / desk["equity"][i]
    return w


SIGNAL_LIFE = 5  # sessions an insight the desk did not act on stays active (a buy it could not afford, a vetoed one)


def desk_insights(desk: dict, symbols: dict[str, int]) -> np.ndarray:
    """Days x symbols: the desk's active insights as weights summing to 1 (or 0: no view, flat). Active: a stock it
    holds, or one it signalled in the last SIGNAL_LIFE sessions and has not sold since; the weight is the latest
    confidence (1 for the first of a ranked list, down to 0.5 for the last). The desk's cash, its exposure by zone and
    the size of each order are left out: sizing and risk are the team's (no risk counted twice)."""
    n = len(desk["equity"])
    k = len(symbols)
    w = np.zeros((n, k), dtype=np.float32)
    held = np.zeros(k)
    conf = np.ones(k)
    seen = np.full(k, -10**9)
    for i in range(n):
        for s, dq in desk["changes"].get(i, {}).items():
            j = symbols[s]
            held[j] = max(0.0, held[j] + dq)
            if held[j] <= 1e-9 and dq < 0:  # sold out: the insight ends
                held[j], seen[j] = 0.0, -10**9
        for s, c in desk["signals"].get(i, {}).items():
            j = symbols[s]
            conf[j], seen[j] = c, i
        active = (held > 1e-9) | (seen >= i - SIGNAL_LIFE)
        if active.any():
            row = np.where(active, conf, 0.0)
            w[i] = row / row.sum()
    return w


def pod_cut(a: np.ndarray, equities: np.ndarray, peaks: np.ndarray, vol: np.ndarray, i: int) -> np.ndarray:
    """Multi-manager rule: a desk down from its top by more than its yearly volatility gets half its capital, by 1.5x
    nothing (until it recovers); the capital taken stays with the team (cash, or the others' picks when filling)."""
    if i < MOMENTUM_DAYS:
        return a
    dd = 1 - equities[i] / peaks[i]
    v = np.where(np.isfinite(vol[i]) & (vol[i] > 0), vol[i], np.inf)
    return a * np.where(dd > 1.5 * v, 0.0, np.where(dd > v, 0.5, 1.0))


def manager(style: str, equities: np.ndarray, i: int) -> np.ndarray:
    """The desks' shares of the team's capital on day i (from their equities up to that day only)."""
    m = equities.shape[1]
    equal = np.full(m, 1.0 / m)
    if style == "inclinacao":
        if i < MOMENTUM_DAYS:
            return equal
        r = equities[i] / equities[i - MOMENTUM_DAYS] - 1
        a = equal * (1 + 0.5 * np.sign(np.nan_to_num(r - np.nanmean(r))))
        a = np.minimum(a / a.sum(), 0.25)
        return a / a.sum()
    if style in ("momento", "sharpe", "top3"):
        if i < MOMENTUM_DAYS:
            return equal
        if style == "sharpe":
            seg = equities[i - MOMENTUM_DAYS:i + 1]
            rets = seg[1:] / seg[:-1] - 1
            sd = rets.std(axis=0)
            r = np.where(sd > 0, rets.mean(axis=0) / np.where(sd > 0, sd, 1), 0.0)
        else:
            r = equities[i] / equities[i - MOMENTUM_DAYS] - 1
        r = np.where(np.isfinite(r), r, -np.inf)
        if style == "top3":
            a = np.zeros(m)
            a[np.argsort(-r)[:3]] = 1.0
            return a / a.sum()
        r = np.where(r > 0, r, 0.0)
        if r.sum() <= 0:
            return equal
        a = r / r.sum()
        for _ in range(5):  # at most MAX_DESK each, the rest spread over the others
            over = a > MAX_DESK + 1e-12
            if not over.any():
                break
            spare = (a[over] - MAX_DESK).sum()
            a[over] = MAX_DESK
            free = ~over & (a > 0)
            if not free.any():
                break
            a[free] += spare * a[free] / a[free].sum()
        return a / a.sum()
    if style == "paridade":
        if i < MOMENTUM_DAYS:
            return equal
        seg = equities[i - MOMENTUM_DAYS:i + 1]
        rets = np.nan_to_num(seg[1:] / seg[:-1] - 1)
        cov = np.cov(rets, rowvar=False)
        cov = 0.7 * cov + 0.3 * np.diag(np.diag(cov)) + np.eye(m) * 1e-8  # shrinkage towards the diagonal
        return risk_parity(cov)
    if style == "risco":
        if i < VOL_DAYS + 1:
            return equal
        seg = equities[i - VOL_DAYS:i + 1]
        vol = np.maximum(np.nan_to_num((seg[1:] / seg[:-1] - 1).std(axis=0), nan=1.0), 0.002)
        a = 1 / vol
        return a / a.sum()
    return equal


def risk_parity(cov: np.ndarray, iters: int = 100) -> np.ndarray:
    """Equal risk contribution weights (w_i (cov w)_i the same for every i), by the fixed point w_i ∝ 1 / (cov w)_i
    (Spinu's cyclical rule simplified), long only, summing to 1."""
    m = cov.shape[0]
    w = np.full(m, 1.0 / m)
    for _ in range(iters):
        mrc = cov @ w
        new = 1 / np.maximum(mrc, 1e-12)
        new /= new.sum()
        new = 0.5 * w + 0.5 * new
        if np.abs(new - w).max() < 1e-9:
            w = new
            break
        w = new
    return w / w.sum()


def drawdown(eq: np.ndarray) -> float:
    peak = np.maximum.accumulate(eq)
    return float(np.max(1 - eq / peak)) if len(eq) else 0.0


def target_weights(p: dict, members: list[str], weights: dict, a: np.ndarray, i: int, vol: np.ndarray | None = None,
                   sectors: np.ndarray | None = None) -> np.ndarray:
    """The picks: the desks' holdings (or insights) weighted by their capital, consensus, the N biggest, sized (by
    volatility, as LEAN's portfolio construction), filled and capped by stock and by sector."""
    w = np.zeros(weights[members[0]].shape[1])
    held_by = np.zeros_like(w)
    for m, s in enumerate(members):
        row = weights[s][i]
        w += a[m] * row
        held_by += row > 0
    if p.get("dimensao") == "vol" and vol is not None and w.sum() > 0:  # inverse volatility, the same total
        total = w.sum()
        v = vol[i]
        inv = np.where(np.isfinite(v) & (v > 0), 1 / np.maximum(v, 0.05), 0.0)
        inv = np.where(inv > 0, inv, np.nanmean(np.where(inv > 0, inv, np.nan)) if (inv > 0).any() else 1.0)
        w = w * inv
        w = w / w.sum() * total if w.sum() > 0 else w
    if p.get("min_mesas", 1) > 1:
        w[held_by < p["min_mesas"]] = 0.0
    if p["consenso"]:
        w *= 1 + p["consenso"] * np.maximum(held_by - 1, 0)
    if p["max_posicoes"] and np.count_nonzero(w) > p["max_posicoes"]:
        w[np.argsort(-w)[p["max_posicoes"]:]] = 0.0
    total = w.sum()
    if total <= 0:
        return w
    if p["normalizar"]:
        w = w / total
    elif p["consenso"]:  # consensus moves weight between stocks, not the desks' cash
        w = w / total * min(1.0, sum(a[m] * float(weights[s][i].sum()) for m, s in enumerate(members)))
    for _ in range(4):  # cap each stock; when filling, what a cap frees goes to the others
        over = w > p["max_nome"]
        if not over.any():
            break
        spare = (w[over] - p["max_nome"]).sum()
        w[over] = p["max_nome"]
        free = (w > 0) & ~over
        if not p["normalizar"] or not free.any():
            break
        w[free] += spare * w[free] / w[free].sum()
    cap = p.get("max_setor", 0.0)
    if cap and sectors is not None:  # at most this share of the team in one sector (ETFs and unknown: no sector)
        for _ in range(4):
            total = w.sum()
            sec = np.bincount(sectors[sectors >= 0], weights=w[sectors >= 0], minlength=int(sectors.max()) + 1)
            over = np.nonzero(sec > cap + 1e-12)[0]
            if not len(over):
                break
            spare = 0.0
            for g in over:
                inside = sectors == g
                spare += sec[g] - cap
                w[inside] *= cap / sec[g]
            free = (w > 0) & ~np.isin(sectors, over)
            if not p["normalizar"] or not free.any():
                break
            w[free] += spare * w[free] / w[free].sum()
            if abs(w.sum() - total) > 1e-9:
                w = w / w.sum() * total
    return w


def run_team(p: dict, members: list[str], desks: dict, weights: dict, markets: list, symbols: list[str],
             open_: np.ndarray, close: np.ndarray, capital: float, lo: int = 0, hi: int | None = None,
             vol: np.ndarray | None = None, sectors: np.ndarray | None = None) -> dict:
    """The team from session lo to hi with fresh capital (the desks keep running from the start: their history feeds
    the manager); at the end, the tax still owed if everything were sold (as the pre-training grades a year).
    entrada="sinais": the desks as alpha models (their insights, run without their own risk rules)."""
    if p.get("entrada") == "sinais" and all(f"sinais:{s}" in desks for s in members):
        members = [f"sinais:{s}" for s in members]
    equities = np.column_stack([desks[s]["equity"] for s in members])
    peaks = np.maximum.accumulate(equities, axis=0)
    desk_vol = (pd.DataFrame(equities).pct_change().rolling(MOMENTUM_DAYS).std() * math.sqrt(252)).to_numpy()
    ix = {s: j for j, s in enumerate(symbols)}
    core = p.get("nucleo", 0.0)
    bench = close[:, ix[agents.BENCHMARK]] if agents.BENCHMARK in ix else None
    refuge = ix.get(p["refugio"]) if p["refugio"] else None
    fee = np.array([agents.fees(s) for s in symbols])
    tax_in = np.array([agents.buy_tax(s) for s in symbols])
    hi = close.shape[0] if hi is None else hi
    k = close.shape[1]
    qty, cost = np.zeros(k), np.zeros(k)
    cash = capital
    realized: dict[int, float] = {}
    paid: set[int] = set()
    target, picks = None, None
    curve, exposure = np.zeros(hi - lo), np.zeros(hi - lo)
    trades, turnover, peak = 0, 0.0, capital
    for i in range(lo, hi):
        day = markets[i].as_of
        if target is not None:  # the decision at today's open: sells first, then buys
            value = np.nan_to_num(qty * close[i - 1])
            eq_prev = cash + value.sum()
            px = np.where(np.isfinite(open_[i]), open_[i], close[i])
            gap = target - value
            exits = (target <= 0) & (qty > 0)
            todo = np.nonzero((exits | (np.abs(gap) >= p["banda"] * eq_prev)) & np.isfinite(px) & (px > 0))[0]
            for j in todo[np.argsort(gap[todo])]:
                g, s = (gap[j] if exits[j] else gap[j] * p["ajuste"]), symbols[j]
                if g < 0 and qty[j] > 0:
                    q = qty[j] if exits[j] else min(qty[j], -g / px[j])
                    gross = q * px[j] * (1 - fee[j])
                    proceeds = gross - agents.commission(markets[i], s, gross, q, day)
                    basis = cost[j] * q / qty[j]
                    realized[day.year] = realized.get(day.year, 0.0) + proceeds - basis
                    cash += proceeds
                    cost[j] -= basis
                    qty[j] -= q
                    if qty[j] < 1e-9:
                        qty[j], cost[j] = 0.0, 0.0
                    trades += 1
                    turnover += gross
                elif g > 0:
                    eur = min(g, cash)
                    if eur < agents.MIN_ORDER_EUR:
                        continue
                    fill = px[j] * (1 + fee[j] + tax_in[j])
                    qty[j] += (eur - agents.commission(markets[i], s, eur, eur / fill, day)) / fill
                    cost[j] += eur
                    cash -= eur
                    trades += 1
                    turnover += eur
            target = None
        if p["colheita"] and day.month == 12 and markets[i - 1].as_of.month == 11 and realized.get(day.year, 0.0) > 0:
            # tax-loss harvesting: sell what is below its cost and buy it back (two spreads), the loss offsets this
            # year's gains (Portugal: a year's losses only offset that year's gains at the 28% rate)
            for j in np.nonzero((qty > 0) & np.isfinite(close[i]))[0]:
                sold = qty[j] * close[i, j] * (1 - fee[j])
                if sold >= cost[j]:
                    continue
                bought = qty[j] * close[i, j] * (1 + fee[j] + tax_in[j])
                realized[day.year] = realized.get(day.year, 0.0) + sold - cost[j]
                cash -= bought - sold
                cost[j] = bought
                trades += 2
        for y in list(realized):  # the tax of a year, with the IRS of the next one
            if y not in paid and day >= date(y + 1, *agents.TAX_DUE):
                cash -= max(0.0, realized[y]) * agents.TAX_RATE
                paid.add(y)
        value = np.nan_to_num(qty * close[i])
        now = curve[i - lo] = cash + value.sum()
        exposure[i - lo] = value.sum() / now if now > 0 else 0.0
        peak = max(peak, now)
        # the picks (manager and desks) every `cada` sessions; the risk manager every day (as LEAN's ManageRisk)
        if (i - lo) % p["cada"] == 0 or picks is None:
            a = manager(p["gestor"], equities, i)
            if p["corte_mesa"]:
                a = pod_cut(a, equities, peaks, desk_vol, i)
            picks = target_weights(p, members, weights, a, i, vol, sectors)
        w = picks.copy()
        invested = float(w.sum())
        refuge_w = 0.0
        if p["tendencia"] and bench is not None and i >= TREND_DAYS:  # the risk manager: the index's trend...
            window = bench[i - TREND_DAYS + 1:i + 1]
            if np.isfinite(window).all() and bench[i] < window.mean():
                w = w * p["queda"]
                if refuge is not None and np.isfinite(close[i, refuge]):
                    refuge_w = invested * (1 - p["queda"])
        scale = 1.0
        if p["vol_alvo"] and i - lo > VOL_DAYS:  # ...the team's own volatility...
            seg = curve[i - lo - VOL_DAYS:i - lo + 1]
            team_vol = float(np.std(seg[1:] / seg[:-1] - 1)) * math.sqrt(252)
            if team_vol > 0:
                scale = min(1.0, p["vol_alvo"] / team_vol)
        if p["travao_dd"] and now < peak * (1 - p["travao_dd"]):  # ...and its fall from the top
            scale *= 0.5
        w = w * scale
        if refuge_w:
            w[refuge] += refuge_w * scale
        if core and bench is not None and np.isfinite(bench[i]):  # the core: the index, without the risk filters
            w = w * (1 - core)
            w[ix[agents.BENCHMARK]] += core
        target = w * now
    end = markets[hi - 1].as_of.year
    owed = sum(max(0.0, v) for y, v in realized.items() if y not in paid and y != end)
    this_year = realized.get(end, 0.0) if end not in paid else 0.0
    owed += max(0.0, this_year + float(np.nan_to_num(qty * close[hi - 1]).sum() - cost.sum()))
    years = max((hi - lo) / 252, 1)
    return {"curve": curve, "tax_owed": owed * agents.TAX_RATE, "trades": trades, "turnover": turnover / years / capital,
            "exposure": float(exposure.mean())}


def blocks_of(markets: list, split: int) -> list[tuple[int, int]]:
    import pretrain

    out, start = [], 0
    for b in pretrain.yearly_blocks(markets[:split]):
        out.append((start, start + len(b)))
        start += len(b)
    return out + [(split, len(markets))]


def year_points(curve: np.ndarray, bench: np.ndarray, blocks: list[tuple[int, int]]) -> list[float]:
    """Each block's profile grade minus the index's (agents.relative_points); the last block is the exam."""
    import pretrain

    def score(c: np.ndarray, a: int, b: int) -> float:
        seg = np.concatenate([[c[a - 1] if a > 0 else c[a]], c[a:b]])
        return agents.profile_score(seg[-1] / seg[0] - 1, (b - a) / pretrain.SESSIONS_PER_YEAR, drawdown(seg), pretrain.PROFILE)

    return [agents.relative_points(score(curve, a, b), score(bench, a, b)) for a, b in blocks]


def team_points(p: dict, members: list[str], desks: dict, weights: dict, markets: list, symbols: list[str],
                open_: np.ndarray, close: np.ndarray, capital: float, blocks: list[tuple[int, int]],
                vol: np.ndarray | None = None, sectors: np.ndarray | None = None) -> list[float]:
    """As the pre-training grades a strategy (pretrain._years and _graded): each year (and the exam) on fresh capital,
    what is left after the tax still owed, the profile's grade minus the index's over the same days."""
    import pretrain

    out = []
    for a, b in blocks:
        r = run_team(p, members, desks, weights, markets, symbols, open_, close, capital, a, b, vol, sectors)
        years = (b - a) / pretrain.SESSIONS_PER_YEAR
        own = agents.profile_score((r["curve"][-1] - r["tax_owed"]) / capital - 1, years, drawdown(r["curve"]), pretrain.PROFILE)
        out.append(agents.relative_points(own, pretrain.index_score(markets[a:b], years)))
    return out


def grade(curve: np.ndarray, bench: np.ndarray, blocks: list, tax_owed: float, trades: int) -> dict:
    """Training years and exam as the pre-training grades them, from one continuous run (see the module's notes)."""
    import pretrain

    pts = year_points(curve, bench, blocks)
    train, exam = pts[:-1], pts[-1]
    daily = np.diff(curve) / curve[:-1]
    capital = curve[0] if curve[0] else 1.0
    return {
        "overall": round(pretrain.overall(train, exam), 1), "train": round(pretrain.weighted(train), 1),
        "years": [round(x, 1) for x in train], "exam": round(exam, 1),
        "years_beating_index": int(sum(x > 0 for x in pts)), "years_total": len(pts),
        "return": round(float(curve[-1] / capital - 1), 4), "return_after_tax": round(float((curve[-1] - tax_owed) / capital - 1), 4),
        "max_drawdown": round(drawdown(curve), 4),
        "sharpe": round(float(daily.mean() / daily.std() * math.sqrt(252)), 2) if daily.std() > 0 else 0.0,
        "trades": trades,
    }


def verdict(pts: list[float]) -> dict:
    """The pre-training's verdict of yearly points (the last one the exam)."""
    import pretrain

    train, exam = pts[:-1], pts[-1]
    return {"overall": round(pretrain.overall(train, exam), 1), "train": round(pretrain.weighted(train), 1),
            "years": [round(x, 1) for x in train], "exam": round(exam, 1),
            "years_beating_index": int(sum(x > 0 for x in pts)), "years_total": len(pts)}


def report_verdicts(profile: str) -> dict:
    """Each strategy's verdict in the profile's latest pre-training report (graded the same way)."""
    try:
        r = json.loads(agents.pretrain_path(profile).read_text())
    except (OSError, ValueError):
        return {}
    return {s: {"overall": v.get("overall"), "years_beating_index": v.get("years_beating_index"),
                "years_total": v.get("years_total")} for s, v in r.get("strategies", {}).items() if "overall" in v}


def pick_members(rule: str, alone: dict, desks: dict, split: int) -> list[str]:
    """Members chosen from the training years only: "melhores" the 4 best; "descorrelacao" the best and then, one by one,
    the desk least correlated (weekly returns) with those already in, up to 5."""
    ranked = sorted(alone, key=lambda s: -alone[s]["train"])
    if rule == "melhores":
        return ranked[:4]
    rets = pd.DataFrame({s: pd.Series(desks[s]["equity"][:split]).pct_change() for s in desks}).iloc[1:]
    weekly = (1 + rets).groupby(np.arange(len(rets)) // 5).prod() - 1
    corr = weekly.corr().fillna(0)
    out = [ranked[0]]
    while len(out) < 5:
        out.append(min((s for s in ranked if s not in out), key=lambda s: corr.loc[s, out].mean()))
    return out


def configs(rng: np.random.Generator, n: int, extra: dict | None = None) -> list[dict]:
    """The template (each parameter's first value), then n random combinations (no repeats)."""
    space = {**PARAMS, **(extra or {})}
    out = [{k: v[0] for k, v in space.items()}]
    seen = {json.dumps(out[0], sort_keys=True)}
    for _ in range(n * 5):
        if len(out) > n:
            break
        c = {k: v[int(rng.integers(len(v)))] for k, v in space.items()}
        key = json.dumps(c, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def prepare(markets: list, profile: str, capital: float) -> dict:
    """The desks' day loops, their weights, prices in euros, the index and the yearly blocks (shared by every team)."""
    import pretrain

    split = max(len(markets) - pretrain.EXAM_SESSIONS, len(markets) * 2 // 3)
    blocks = blocks_of(markets, split)
    desks = {}
    for s in ALL:
        desks[s] = desk_run(s, desk_params(s, profile), markets, capital)
        desks[f"sinais:{s}"] = desk_run(s, desk_params(s, profile), markets, capital, signals=True)
        print(f"Mesa {s}: pronta", flush=True)
    symbols = sorted({sym for d in desks.values() for ch in d["changes"].values() for sym in ch}
                     | {sym for d in desks.values() for day in d.get("signals", {}).values() for sym in day}
                     | {agents.BENCHMARK} | {r for r in PARAMS["refugio"] if r})
    ix = {s: j for j, s in enumerate(symbols)}
    close = eur_matrix(markets, symbols, "Close")
    open_ = eur_matrix(markets, symbols, "Open")
    weights = {s: (desk_insights(d, ix) if "signals" in d else desk_weights(d, ix, close)) for s, d in desks.items()}
    rets = pd.DataFrame(close).pct_change(fill_method=None)
    vol = (rets.rolling(VOL_DAYS, min_periods=20).std() * math.sqrt(252)).to_numpy(dtype=np.float32)
    names = {}
    sectors = np.full(len(symbols), -1, dtype=np.int64)
    sector_of = {}
    for m in markets[::63] + markets[-1:]:  # a stock's sector as the market lists it (also those that left the index)
        sector_of.update({s: v.get("sector") for s, v in m.stocks.items()})
    for j, s in enumerate(symbols):
        sec = sector_of.get(s)
        if sec and sec != "Unknown":
            sectors[j] = names.setdefault(sec, len(names))
    bench = pd.Series(close[:, ix[agents.BENCHMARK]]).ffill().bfill().to_numpy()
    bench = bench / bench[0] * capital
    index = grade(bench, bench, blocks, max(0.0, bench[-1] - capital) * agents.TAX_RATE, 1)
    alone = {s: grade(d["equity"], bench, blocks, d["tax_owed"], d["trades"]) for s, d in desks.items() if ":" not in s}
    reports = report_verdicts(profile)
    for s in alone:  # the verdict as the pre-training gave it (yearly, fresh capital): the one to compare with
        alone[s]["continuous_overall"] = alone[s]["overall"]
        if s in reports:
            alone[s].update({k: v for k, v in reports[s].items() if v is not None})
    return {"split": split, "blocks": blocks, "desks": desks, "symbols": symbols, "close": close, "open": open_,
            "weights": weights, "bench": bench, "index": index, "alone": alone, "vol": vol, "sectors": sectors}


def analyse(markets: list, profile: str, capital: float, search: int = SEARCH, seed: int = 0) -> dict:
    import pretrain

    ctx = prepare(markets, profile, capital)
    split, blocks, desks, symbols = ctx["split"], ctx["blocks"], ctx["desks"], ctx["symbols"]
    close, open_, weights, bench = ctx["close"], ctx["open"], ctx["weights"], ctx["bench"]
    index, alone = ctx["index"], ctx["alone"]
    vs = (ctx["vol"], ctx["sectors"])
    rng = np.random.default_rng(seed)
    teams = {}
    for key, team in TEAMS.items():
        members = team["membros"]
        if isinstance(members, str):
            members = pick_members(members.split(":", 1)[1], alone, desks, split)
        tried = []
        for c in configs(rng, search, team.get("procura")):
            tried.append((c, None, team_points(c, members, desks, weights, markets, symbols, open_, close, capital, blocks, *vs)))
        # the choice on the training years only (recency-weighted, as the pre-training); the exam judges it after
        best = max(range(len(tried)), key=lambda t: pretrain.weighted(tried[t][2][:-1]))
        c, _, pts = tried[best]
        r = run_team(c, members, desks, weights, markets, symbols, open_, close, capital, vol=vs[0], sectors=vs[1])  # 20 years
        # walk-forward: the choice made on the years before each of the last 3 years, judged on that year, against the
        # template (the first configuration)
        folds = []
        for test in range(len(blocks) - 3, len(blocks)):
            pick = max(range(len(tried)), key=lambda t: pretrain.weighted(tried[t][2][:test]))
            folds.append({"test": [markets[blocks[test][0]].as_of.isoformat(), markets[blocks[test][1] - 1].as_of.isoformat()],
                          "chosen": round(tried[pick][2][test], 1), "template": round(tried[0][2][test], 1)})
        gc = grade(r["curve"], bench, blocks, r["tax_owed"], r["trades"])
        g = {**gc, **verdict(pts), "continuous_overall": gc["overall"]}
        g0 = verdict(tried[0][2])
        r0 = run_team(tried[0][0], members, desks, weights, markets, symbols, open_, close, capital, vol=vs[0], sectors=vs[1])
        g0.update(grade(r0["curve"], bench, blocks, r0["tax_owed"], r0["trades"]) | verdict(tried[0][2]))
        teams[key] = {"nome": team["nome"], "ideia": team["ideia"], "membros": members, "params": c, **g,
                      "exposure": round(r["exposure"], 3), "turnover_per_year": round(float(r["turnover"]), 2),
                      "template": {x: g0[x] for x in ("overall", "train", "exam", "return_after_tax", "max_drawdown")},
                      "walk_forward": {"folds": folds, "wins": sum(f["chosen"] > f["template"] for f in folds)},
                      "configs": len(tried),
                      "curve": [(markets[i].as_of.isoformat(), round(float(r["curve"][i] / capital - 1) * 100, 2))
                                for i in range(0, len(markets), 5)]}
        print(f"Equipa {key}: pronta ({len(tried)} configurações)", flush=True)
    return {"at": datetime.now(timezone.utc).isoformat(timespec="minutes"), "profile": profile, "broker": agents.BROKER,
            "capital": capital, "days": [markets[0].as_of.isoformat(), markets[-1].as_of.isoformat()],
            "exam": [markets[split].as_of.isoformat(), markets[-1].as_of.isoformat()],
            "grading": "como o pré-treino: cada ano com capital novo, imposto devido no fim do ano, menos o índice",
            "index": index, "alone": alone, "teams": teams}


# --------------------------------------------------------------------------
# A population of teams that evolves: not a competition, a way to find paths that hold up and grow new teams from them
# --------------------------------------------------------------------------

POP_SIZE = 20
KEEP = 10  # the "correct paths" kept each generation; as many new teams are bred from them
VAL_YEARS = 6  # validation: the years before the exam (the exam is sealed: reported, never used)
GENERATIONS = int(os.environ.get("EQUIPAS_GERACOES") or 5)
FAMILIES = {  # members of the first generation's teams
    "todos": ALL, "robusta": TEAMS["robusta"]["membros"], "tendencia": TEAMS["tendencia"]["membros"],
    "qualidade": TEAMS["qualidade"]["membros"], "etf": TEAMS["etf"]["membros"],
}
# The first generation: 20 different defaults (families x styles from the research: equal weight with a volatility
# cap, inverse volatility, risk parity, mild momentum tilt, pod cuts, trend gate with a refuge, core and satellites,
# consensus, monthly bands...).
STYLES = [
    {"gestor": "igual", "vol_alvo": 0.15, "cada": 21, "banda": 0.04},
    {"gestor": "risco", "max_nome": 0.06, "cada": 21},
    {"gestor": "paridade", "cada": 21, "ajuste": 0.5},
    {"gestor": "inclinacao", "corte_mesa": 1},
    {"gestor": "igual", "tendencia": 1, "queda": 0.5, "refugio": "EUNH.DE", "cada": 21},
    {"gestor": "igual", "consenso": 1.0, "min_mesas": 2, "max_posicoes": 20},
    {"gestor": "momento", "travao_dd": 0.15},
    {"gestor": "sharpe", "colheita": 1, "cada": 5},
]
POPULATION_DIR = OUT_DIR
POP_VERSION = 2  # 2: at most KEEP / families per family, no copies


def first_generation(rng: np.random.Generator) -> list[dict]:
    base = {k: v[0] for k, v in PARAMS.items()}
    out = []
    fams = list(FAMILIES)
    for n in range(POP_SIZE):
        fam = fams[n % len(fams)]
        style = STYLES[(n * 3 + n // len(fams)) % len(STYLES)]
        params = {**base, **style}
        if n in (7, 13):  # two core-and-satellite teams
            params["nucleo"] = 0.5
        out.append({"id": f"g0-{n + 1:02d}", "familia": fam, "membros": list(FAMILIES[fam]), "params": params,
                    "origem": "geração 0"})
    return out


def mutate_team(team: dict, rng: np.random.Generator, gen: int, n: int) -> dict:
    """A child: 1 to 3 parameters changed (any value of PARAMS), sometimes a desk in or out."""
    params = dict(team["params"])
    for k in rng.choice(list(PARAMS), size=int(rng.integers(1, 4)), replace=False):
        params[k] = PARAMS[k][int(rng.integers(len(PARAMS[k])))]
    if "nucleo" in params and rng.random() < 0.3:
        params["nucleo"] = float(rng.choice([0.3, 0.5, 0.6]))
    if rng.random() < 0.25:  # the desks' input (holdings or insights) is the biggest change: tried more often
        params["entrada"] = "sinais" if params.get("entrada", "posicoes") == "posicoes" else "posicoes"
    members = list(team["membros"])
    if rng.random() < 0.4:
        out = [s for s in ALL if s not in members]
        if rng.random() < 0.5 and len(members) > 3:
            members.remove(str(rng.choice(members)))
        elif out:
            members.append(str(rng.choice(out)))
    return {"id": f"g{gen}-{n:02d}", "familia": team["familia"], "membros": members, "params": params,
            "origem": f"mutação de {team['id']}"}


def cross_teams(a: dict, b: dict, rng: np.random.Generator, gen: int, n: int) -> dict:
    """A child of two: each parameter from one of them, the desks of one of them."""
    params = {k: (a["params"] if rng.random() < 0.5 else b["params"]).get(k, PARAMS[k][0]) for k in PARAMS}
    core = [x["params"]["nucleo"] for x in (a, b) if "nucleo" in x["params"]]
    if core and rng.random() < 0.5:
        params["nucleo"] = core[0]
    src = a if rng.random() < 0.5 else b
    return {"id": f"g{gen}-{n:02d}", "familia": src["familia"], "membros": list(src["membros"]), "params": params,
            "origem": f"cruzamento de {a['id']} e {b['id']}"}


def judge(team: dict, ctx: dict, markets: list, capital: float) -> dict:
    """Its yearly points (as the pre-training): training years, validation (the VAL_YEARS before the exam), exam."""
    import pretrain

    members = [s for s in team["membros"] if s in ctx["desks"]]
    pts = team_points(team["params"], members, ctx["desks"], ctx["weights"], markets, ctx["symbols"], ctx["open"],
                      ctx["close"], capital, ctx["blocks"], ctx["vol"], ctx["sectors"])
    train, val, exam = pts[:-1 - VAL_YEARS], pts[-1 - VAL_YEARS:-1], pts[-1]
    return {"train": round(pretrain.weighted(train), 1), "val": round(float(np.mean(val)), 1),
            "val_wins": int(sum(v > 0 for v in val)), "val_years": [round(v, 1) for v in val], "exam": round(exam, 1),
            "overall": round(pretrain.overall(pts[:-1], exam), 1)}


def team_key(t: dict) -> str:
    return json.dumps([sorted(t["membros"]), t["params"]], sort_keys=True)


def select(pop: list[dict]) -> list[dict]:
    """The KEEP teams that do well in the training years and in validation at once (mean of the two ranks): a path
    is "correct" when it is not good in only one of them. Different paths: no copies, and at most KEEP / families
    per family (the rest of the places, if any, to the best left)."""
    unique, seen = [], set()
    for t in pop:
        if team_key(t) not in seen:
            seen.add(team_key(t))
            unique.append(t)
    by_train = sorted(unique, key=lambda t: -t["score"]["train"])
    by_val = sorted(unique, key=lambda t: -t["score"]["val"])
    rank = {t["id"]: (by_train.index(t) + by_val.index(t)) / 2 for t in unique}
    ordered = sorted(unique, key=lambda t: (rank[t["id"]], -t["score"]["val"]))
    quota = max(1, KEEP // len(FAMILIES))
    out, per = [], {}
    for t in ordered:
        if per.get(t["familia"], 0) < quota and len(out) < KEEP:
            out.append(t)
            per[t["familia"]] = per.get(t["familia"], 0) + 1
    out += [t for t in ordered if t not in out][: KEEP - len(out)]
    return out


def evolve(markets: list, profile: str, capital: float, generations: int = GENERATIONS, seed: int = 0) -> dict:
    import pretrain

    path = POPULATION_DIR / f"equipas_populacao-{profile}.json"
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = {}
    if state.get("version") != POP_VERSION:  # a new way of evolving: the population starts over
        state = {"generation": 0, "teams": None, "log": [], "version": POP_VERSION}
    rng = np.random.default_rng(seed + 1000 * state["generation"])
    ctx = prepare(markets, profile, capital)
    pop = state["teams"] or first_generation(rng)
    gen = state["generation"]
    log = state.get("log", [])
    for _ in range(generations):
        for t in pop:  # every generation judges everyone again (the desks may have new parameters)
            t["score"] = judge(t, ctx, markets, capital)
        keep = select(pop)
        log.append({"generation": gen, "at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                    "kept": [t["id"] for t in keep],
                    "best_val": max(t["score"]["val"] for t in pop), "median_val": float(np.median([t["score"]["val"] for t in pop])),
                    "best_train": max(t["score"]["train"] for t in pop)})
        print(f"Geração {gen}: avaliada", flush=True)
        gen += 1
        children, taken = [], {team_key(t) for t in keep}
        for n in range(POP_SIZE - KEEP):
            for _ in range(20):  # a child that is a copy of a team already there is bred again
                if n % 2 == 0:
                    child = mutate_team(keep[n % len(keep)], rng, gen, n + 1)
                else:
                    a, b = rng.choice(len(keep), size=2, replace=False)
                    child = cross_teams(keep[a], keep[b], rng, gen, n + 1)
                if team_key(child) not in taken:
                    break
            taken.add(team_key(child))
            children.append(child)
        pop = [dict(t, sobreviveu=t.get("sobreviveu", 0) + 1) for t in keep] + children
    for t in pop:
        if "score" not in t or t["id"].startswith(f"g{gen}-"):
            t["score"] = judge(t, ctx, markets, capital)
    keep = select(pop)
    # the paths kept, in detail: 20 years in one go, after tax (the exam's numbers are reported, never used)
    detail = []
    for t in keep:
        members = [s for s in t["membros"] if s in ctx["desks"]]
        r = run_team(t["params"], members, ctx["desks"], ctx["weights"], markets, ctx["symbols"], ctx["open"], ctx["close"],
                     capital, vol=ctx["vol"], sectors=ctx["sectors"])
        g = grade(r["curve"], ctx["bench"], ctx["blocks"], r["tax_owed"], r["trades"])
        # the same team with the other input (holdings or insights): does treating the desks as alpha models help?
        other = "posicoes" if t["params"].get("entrada") == "sinais" else "sinais"
        flipped = judge({**t, "params": {**t["params"], "entrada": other}}, ctx, markets, capital)
        detail.append({**{k: t[k] for k in ("id", "familia", "membros", "params", "origem")}, "score": t["score"],
                       "sobreviveu": t.get("sobreviveu", 0), "return_after_tax": g["return_after_tax"],
                       "max_drawdown": g["max_drawdown"], "exposure": round(r["exposure"], 3), "trades": r["trades"],
                       "outra_entrada": {"entrada": other, **flipped}})
    state = {"generation": gen, "profile": profile, "broker": agents.BROKER, "teams": pop, "log": log[-200:],
             "trials": state.get("trials", 0) + generations * POP_SIZE, "version": POP_VERSION}
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1, default=float) + "\n")
    return {"at": datetime.now(timezone.utc).isoformat(timespec="minutes"), "profile": profile, "broker": agents.BROKER,
            "generation": gen, "trials": state["trials"], "days": [markets[0].as_of.isoformat(), markets[-1].as_of.isoformat()],
            "index": ctx["index"], "alone": ctx["alone"], "kept": detail, "log": log[-generations:]}


def save(result: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    profile = result["profile"]
    seed = (f"-s{result['seed']}" if result.get("seed") else "") + ("-padrao" if result.get("desks") == "padrao" else "")
    (OUT_DIR / f"equipas-{profile}-{date.today().isoformat()}{seed}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n")
    hist_path = OUT_DIR / f"equipas_historico-{profile}.json"
    try:
        history = json.loads(hist_path.read_text())
    except (OSError, ValueError):
        history = []
    summary = {k: result.get(k) for k in ("at", "profile", "broker", "days", "exam", "seed", "desks")}
    summary["index"] = {k: result["index"][k] for k in ("return", "return_after_tax", "max_drawdown")}
    summary["teams"] = {k: {x: t[x] for x in ("overall", "exam", "years_beating_index", "return_after_tax", "max_drawdown", "exposure")}
                        for k, t in result["teams"].items()}
    best = max(result["alone"].items(), key=lambda kv: kv[1]["overall"])
    summary["best_alone"] = {"strategy": best[0], **{x: best[1][x] for x in ("overall", "return_after_tax", "max_drawdown")}}
    hist_path.write_text(json.dumps(history + [summary], ensure_ascii=False, indent=1) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--contexto", default=None)
    ap.add_argument("--anos", type=int, default=20)
    ap.add_argument("--perfil", default=os.environ.get("PRETRAIN_PERFIL") or agents.DEFAULT_PROFILE)
    a = ap.parse_args()
    os.environ["PRETRAIN_PERFIL"] = a.perfil
    import pretrain

    markets = pretrain.build_markets(a.anos, a.contexto)
    pretrain._MARKETS[:] = markets  # the engine's data registry
    seed = int(os.environ.get("EQUIPAS_SEMENTE") or 0)
    if (os.environ.get("EQUIPAS_MODO") or "evoluir") == "evoluir":
        result = evolve(markets, a.perfil, agents.real_capital(), seed=seed)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / f"equipas_evolucao-{a.perfil}-{date.today().isoformat()}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1, default=float) + "\n")
        print("Equipas: geração guardada no repositório privado.", flush=True)
        return 0
    result = analyse(markets, a.perfil, agents.real_capital(), seed=seed)
    result["seed"] = seed
    result["desks"] = DESKS
    save(result)
    print("Equipas: resultados guardados no repositório privado.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
