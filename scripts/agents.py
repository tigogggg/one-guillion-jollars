"""Simulated trading agents (paper trading) on real market data, via yfinance.

Each agent in agents/config.json runs one strategy built on the channel's own
signals (trend score, quality ranking, breakouts, VCP, market zone) with
virtual euros. Daily run, after the morning tips:

1. Orders decided on the previous run fill at the open of the next session
   (no look-ahead), with FEE per side; US stocks are bought in EUR at the day's
   EUR/USD rate, fractional shares allowed.
2. Stops and targets are checked bar by bar against each session's low/high.
3. Positions are marked to the latest close; points are awarded:
   +10 for a day in profit (equity above money given) and +/-1 per 0.1% daily
   change (capped at +/-10).
   New buys skip stocks reporting earnings within EARNINGS_AVOID_DAYS, unless
   the agent's params set ignora_resultados.
4. On the first run of a month, agents that lost money last month mutate
   their numeric parameters (+/-20%), the "training"; winners keep theirs.
5. The strategy decides tomorrow's orders; the league is posted to Discord.

State lives in agents/state.json, committed by the workflow.

    uv run python scripts/agents.py daily --dry-run
    uv run python scripts/agents.py config '{"acao": "ver"}' --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

import earnings
from charts import lines_chart, returns_chart
from discord_daily import WATCHLIST_DIR
from discord_post import BLUE, GREEN, RED, embed, send
from rankings import (
    SECTORS_PT,
    UCITS_ETFS,
    add_trend_scores,
    download_history,
    is_stage2,
    load_constituents,
    trend_metrics,
)

REPO_AGENTS = WATCHLIST_DIR.parents[1] / "agents"
CONFIG_PATH = REPO_AGENTS / "config.json"
STATE_PATH = REPO_AGENTS / "state.json"

FEE = 0.001
MIN_ORDER_EUR = 20.0
EXPOSURE = {"Alta": 1.0, "Neutra": 0.7, "Cautelosa": 0.4, "Queda": 0.1}
ZONE_ICON = {"Alta": "🟢", "Neutra": "🟡", "Cautelosa": "🟠", "Queda": "🔴"}
MUTATION = 0.2
EARNINGS_AVOID_DAYS = 5
TRADES_KEPT = 200
POINTS_LOG_KEPT = 60

# Numeric parameters the monthly training may change, with their bounds.
PARAM_BOUNDS = {
    "n": (1, 10, int),
    "cada": (3, 30, int),
    "stop_pct": (0.04, 0.30, float),
    "risco_pct": (0.005, 0.04, float),
    "alvo_r": (1.5, 6.0, float),
    "max_pos": (0.10, 0.60, float),
    "qualidade_min": (40, 90, float),
    "aperto_pct": (0.03, 0.12, float),
    "setores": (1, 4, int),
    "por_setor": (1, 4, int),
    "queda_pct": (0.03, 0.15, float),
    "alvo_pct": (0.03, 0.20, float),
    "max_dias": (5, 30, int),
    "folga": (0.05, 0.25, float),
}


@dataclass
class Market:
    hist: dict[str, pd.DataFrame]
    fx: pd.Series
    stocks: dict[str, dict]
    etfs: dict[str, dict]
    as_of: date
    sessions: list[date]
    zone: str
    stage2_share: float
    rankings: list[dict] = field(default_factory=list)
    vcp: set[str] = field(default_factory=set)
    earnings: dict[str, date | None] = field(default_factory=dict)  # looked up once per run
    closes: dict[str, float] | None = None  # as_of closes, precomputed by the backtest for speed


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------


def extra_metrics(df: pd.DataFrame) -> dict:
    before = df.iloc[:-1]
    last = df.iloc[-1]
    close = float(last["Close"])
    return {
        "high20_prev": float(before["High"].iloc[-20:].max()),
        "low10_prev": float(before["Low"].iloc[-10:].min()),
        "vol_ratio": float(last["Volume"]) / max(float(before["Volume"].iloc[-50:].mean()), 1.0),
        "range10": (float(df["High"].iloc[-10:].max()) - float(df["Low"].iloc[-10:].min())) / close,
    }


def zone_for(stage2_share: float) -> str:
    if stage2_share >= 0.45:
        return "Alta"
    if stage2_share >= 0.30:
        return "Neutra"
    if stage2_share >= 0.15:
        return "Cautelosa"
    return "Queda"


def load_market() -> Market:
    constituents = load_constituents()
    sector_of = {c["symbol"]: c["sector"] for c in constituents}
    stock_hist = download_history(list(sector_of) + ["SPY"])
    etf_hist = download_history([t for t, _ in UCITS_ETFS])
    fx_hist = download_history(["EURUSD=X"])
    if "SPY" not in stock_hist or "EURUSD=X" not in fx_hist:
        raise RuntimeError("Sem dados do Yahoo Finance (SPY ou EUR/USD).")

    stocks = {}
    for sym, df in stock_hist.items():
        m = trend_metrics(df) if sym in sector_of else None
        if m:
            stocks[sym] = {**m, **extra_metrics(df), "sector": sector_of[sym], "stage2": is_stage2(m)}
    add_trend_scores(stocks)
    names = dict(UCITS_ETFS)
    etfs = {}
    for sym, df in etf_hist.items():
        m = trend_metrics(df)
        if m:
            etfs[sym] = {**m, "name": names.get(sym, sym)}
    add_trend_scores(etfs)

    share = sum(m["stage2"] for m in stocks.values()) / max(len(stocks), 1)
    spy = stock_hist["SPY"]
    market = Market(
        hist={**stock_hist, **etf_hist},
        fx=fx_hist["EURUSD=X"]["Close"],
        stocks=stocks,
        etfs=etfs,
        as_of=spy.index[-1].date(),
        sessions=[d.date() for d in spy.index],
        zone=zone_for(share),
        stage2_share=share,
    )
    rankings = WATCHLIST_DIR / "rankings-latest.json"
    if rankings.exists():
        data = json.loads(rankings.read_text())
        market.rankings = [r for rows in data["stocks"].values() for r in rows]
    vcp = WATCHLIST_DIR / "vcp-latest.json"
    if vcp.exists():
        market.vcp = {
            r["symbol"]
            for r in json.loads(vcp.read_text()).get("results", [])
            if r.get("entry_ready") and r.get("rating") in ("Textbook VCP", "Strong VCP", "Good VCP")
        }
    return market


def is_eur(symbol: str) -> bool:
    return symbol.endswith((".DE", ".AS", ".PA", ".MI"))


def to_eur(mkt: Market, symbol: str, price: float, on: date) -> float:
    if is_eur(symbol):
        return price
    rate = mkt.fx.asof(pd.Timestamp(on))
    return price / float(mkt.fx.iloc[0] if pd.isna(rate) else rate)


def bars_between(df: pd.DataFrame, after: date, until: date) -> pd.DataFrame:
    """Rows dated after `after` up to and including `until` (index sorted by date)."""
    return df.loc[pd.Timestamp(after) + pd.Timedelta(days=1): pd.Timestamp(until) + pd.Timedelta(hours=23)]


def last_close(mkt: Market, symbol: str) -> float | None:
    if mkt.closes is not None:
        return mkt.closes.get(symbol)
    df = mkt.hist.get(symbol)
    if df is None:
        return None
    rows = df.loc[: pd.Timestamp(mkt.as_of) + pd.Timedelta(hours=23)]
    return float(rows["Close"].iloc[-1]) if len(rows) else None


def metrics(mkt: Market, symbol: str) -> dict | None:
    return mkt.stocks.get(symbol) or mkt.etfs.get(symbol)


# --------------------------------------------------------------------------
# Portfolio
# --------------------------------------------------------------------------


def new_portfolio(capital: float, start: date) -> dict:
    return {
        "cash": capital,
        "deposits": capital,
        "positions": {},
        "pending": [],
        "history": [],
        "points": 0,
        "points_log": [],
        "trades": [],
        "generation": 1,
        "evolution": [],
        "last_rebalance": None,
        "last_decision": None,
        "started": start.isoformat(),
    }


def position_value(mkt: Market, symbol: str, pos: dict) -> float:
    price = last_close(mkt, symbol)
    if price is None:
        return pos["cost_eur"]
    return pos["qty"] * to_eur(mkt, symbol, price, mkt.as_of)


def equity(port: dict, mkt: Market) -> float:
    return port["cash"] + sum(position_value(mkt, s, p) for s, p in port["positions"].items())


def sell(port: dict, mkt: Market, symbol: str, price: float, on: date, reason: str, fraction: float = 1.0) -> dict:
    pos = port["positions"][symbol]
    qty = pos["qty"] * fraction
    fill = price * (1 - FEE)
    proceeds = qty * to_eur(mkt, symbol, fill, on)
    cost = pos["cost_eur"] * fraction
    port["cash"] += proceeds
    if fraction >= 0.999:
        del port["positions"][symbol]
    else:
        pos["qty"] -= qty
        pos["cost_eur"] -= cost
    trade = {"date": on.isoformat(), "side": "venda", "symbol": symbol, "qty": round(qty, 6),
             "price": round(fill, 4), "eur": round(proceeds, 2), "pnl_eur": round(proceeds - cost, 2), "reason": reason}
    port["trades"] = (port["trades"] + [trade])[-TRADES_KEPT:]
    return trade


def buy(port: dict, mkt: Market, order: dict, price: float, on: date) -> dict | None:
    eur = min(order["eur"], port["cash"])
    if eur < MIN_ORDER_EUR:
        return None
    symbol = order["symbol"]
    fill = price * (1 + FEE)
    qty = eur / to_eur(mkt, symbol, fill, on)
    port["cash"] -= eur
    pos = port["positions"].get(symbol)
    if pos:
        pos["entry"] = (pos["entry"] * pos["qty"] + fill * qty) / (pos["qty"] + qty)
        pos["qty"] += qty
        pos["cost_eur"] += eur
    else:
        port["positions"][symbol] = {
            "qty": qty, "entry": fill, "entry_date": on.isoformat(), "cost_eur": eur,
            "stop": order.get("stop"), "tp": order.get("tp"),
            "checked": (on - timedelta(days=1)).isoformat(),
        }
    trade = {"date": on.isoformat(), "side": "compra", "symbol": symbol, "qty": round(qty, 6),
             "price": round(fill, 4), "eur": round(eur, 2), "pnl_eur": 0.0, "reason": order.get("reason", "")}
    port["trades"] = (port["trades"] + [trade])[-TRADES_KEPT:]
    return trade


def fill_pending(port: dict, mkt: Market) -> list[dict]:
    """Execute the previous run's orders at the open of the next session."""
    done, still = [], []
    # sells first, so their cash is available to the buys
    for order in sorted(port["pending"], key=lambda o: o["side"] != "sell"):
        df = mkt.hist.get(order["symbol"])
        if df is None:
            continue
        after = bars_between(df, date.fromisoformat(order["created"]), mkt.as_of)
        if after.empty:
            still.append(order)
            continue
        on, bar = after.index[0].date(), after.iloc[0]
        if order["side"] == "sell":
            if order["symbol"] in port["positions"]:
                done.append(sell(port, mkt, order["symbol"], float(bar["Open"]), on, order.get("reason", ""),
                                 order.get("fraction", 1.0)))
        else:
            t = buy(port, mkt, order, float(bar["Open"]), on)
            if t:
                done.append(t)
    port["pending"] = still
    return done


def check_exits(port: dict, mkt: Market) -> list[dict]:
    """Stops and targets, bar by bar since each position was last checked."""
    done = []
    for symbol, pos in list(port["positions"].items()):
        df = mkt.hist.get(symbol)
        if df is None:
            continue
        bars = bars_between(df, date.fromisoformat(pos["checked"]), mkt.as_of)
        for idx, bar in bars.iterrows():
            on = idx.date()
            if pos.get("stop") and bar["Low"] <= pos["stop"]:
                done.append(sell(port, mkt, symbol, min(float(bar["Open"]), pos["stop"]), on, "stop"))
                break
            if pos.get("tp") and bar["High"] >= pos["tp"]:
                done.append(sell(port, mkt, symbol, max(float(bar["Open"]), pos["tp"]), on, "alvo"))
                break
            pos["checked"] = on.isoformat()
    return done


def score_day(port: dict, mkt: Market) -> int | None:
    """Append today's equity and award points; None if already done for this date."""
    eq = equity(port, mkt)
    hist = port["history"]
    if hist and hist[-1]["date"] >= mkt.as_of.isoformat():
        hist[-1].update(equity=round(eq, 2), deposits=port["deposits"])
        return None
    pts = 0
    if hist:
        prev = hist[-1]
        day = (eq - (port["deposits"] - prev["deposits"])) / prev["equity"] - 1 if prev["equity"] else 0.0
        pts = (10 if eq > port["deposits"] else 0) + max(-10, min(10, round(day * 1000)))
        port["points"] += pts
        port["points_log"] = (port["points_log"] + [{"date": mkt.as_of.isoformat(), "pts": pts}])[-POINTS_LOG_KEPT:]
    hist.append({"date": mkt.as_of.isoformat(), "equity": round(eq, 2), "deposits": port["deposits"]})
    return pts


def month_return(port: dict, month: str) -> float | None:
    rows = [h for h in port["history"] if h["date"].startswith(month)]
    if len(rows) < 2 or not rows[0]["equity"]:
        return None
    first, last = rows[0], rows[-1]
    return (last["equity"] - first["equity"] - (last["deposits"] - first["deposits"])) / first["equity"]


def train(cfg: dict, port: dict, mkt: Market) -> str | None:
    """First run of a month: an agent that lost money last month mutates its parameters."""
    hist = port["history"]
    if len(hist) < 2 or hist[-1]["date"][:7] == hist[-2]["date"][:7]:
        return None
    last_month = hist[-2]["date"][:7]
    ret = month_return(port, last_month)
    params = port.setdefault("params", dict(cfg["params"]))
    tunable = [k for k in params if k in PARAM_BOUNDS]
    if ret is None or ret >= 0 or not tunable:
        return None
    rng = random.Random(f"{cfg['id']}-{mkt.as_of}")
    changes = []
    for k in tunable:
        lo, hi, kind = PARAM_BOUNDS[k]
        new = min(hi, max(lo, params[k] * rng.uniform(1 - MUTATION, 1 + MUTATION)))
        new = int(round(new)) if kind is int else round(new, 4)
        if new != params[k]:
            changes.append(f"{k} {params[k]}→{new}")
            params[k] = new
    port["generation"] += 1
    note = f"{last_month}: {ret * 100:+.1f}% → geração {port['generation']}: " + (", ".join(changes) or "sem alterações")
    port["evolution"] = (port["evolution"] + [{"date": mkt.as_of.isoformat(), "note": note}])[-24:]
    return note


# --------------------------------------------------------------------------
# Strategies: (cfg, port, mkt, params) -> orders for the next open
# --------------------------------------------------------------------------


def sessions_since(mkt: Market, since: str | None) -> int:
    if not since:
        return 10**6
    return sum(1 for d in mkt.sessions if d > date.fromisoformat(since))


def due(port: dict, mkt: Market, every: int) -> bool:
    if sessions_since(mkt, port.get("last_rebalance")) >= every:
        port["last_rebalance"] = mkt.as_of.isoformat()
        return True
    return False


def order_buy(symbol: str, eur: float, reason: str, stop: float | None = None, tp: float | None = None) -> dict:
    return {"side": "buy", "symbol": symbol, "eur": round(eur, 2), "stop": stop, "tp": tp, "reason": reason}


def order_sell(symbol: str, reason: str, fraction: float = 1.0) -> dict:
    return {"side": "sell", "symbol": symbol, "fraction": fraction, "reason": reason}


def rebalance(port: dict, mkt: Market, targets: list[str], weight: float, keep: set[str], stop_pct: float | None,
              reason: str) -> list[dict]:
    """Sell what is neither a target nor in `keep`; buy missing targets at `weight` of equity each."""
    orders = [order_sell(s, "saiu da lista") for s in port["positions"] if s not in targets and s not in keep]
    eq = equity(port, mkt)
    for s in targets:
        if s in port["positions"]:
            continue
        price = last_close(mkt, s)
        stop = round(price * (1 - stop_pct), 4) if stop_pct and price else None
        orders.append(order_buy(s, eq * weight, reason, stop=stop))
    return orders


def exposure(mkt: Market) -> float:
    return EXPOSURE[mkt.zone]


def free_slots(port: dict, n: int) -> int:
    return n - len(port["positions"])


def risk_sized(port: dict, mkt: Market, symbol: str, stop: float, risk_pct: float, max_pos: float) -> float:
    price = last_close(mkt, symbol)
    risk = (price - stop) / price if price else 1
    if not 0 < risk <= 0.15:
        return 0.0
    eq = equity(port, mkt)
    return min(eq * risk_pct / risk, eq * max_pos)


def s_indexador(cfg, port, mkt, p):
    return [order_buy("SXR8.DE", port["cash"], "investir")] if port["cash"] >= MIN_ORDER_EUR else []


def s_guardiao(cfg, port, mkt, p):
    if port["positions"] and not due(port, mkt, 5):
        return []
    port["last_rebalance"] = mkt.as_of.isoformat()
    eq = equity(port, mkt)
    target = exposure(mkt) * p["max_exposicao"]
    pos = port["positions"].get("VWCE.DE")
    current = position_value(mkt, "VWCE.DE", pos) / eq if pos else 0.0
    if current < target - p["folga"]:
        return [order_buy("VWCE.DE", (target - current) * eq, f"zona {mkt.zone}")]
    if pos and current > target + p["folga"]:
        return [order_sell("VWCE.DE", f"zona {mkt.zone}", fraction=(current - target) / current)]
    return []


def s_rotador(cfg, port, mkt, p):
    if not due(port, mkt, p["cada"]):
        return []
    ranked = sorted((s for s, m in mkt.etfs.items() if m["price"] > m["sma200"]),
                    key=lambda s: mkt.etfs[s]["trend_score"], reverse=True)
    n = p["n"]
    return rebalance(port, mkt, ranked[:n], exposure(mkt) / n, set(ranked[: n + 1]), None, "top ETFs")


def s_foguetao(cfg, port, mkt, p):
    if not due(port, mkt, p["cada"]):
        return []
    ranked = sorted((s for s, m in mkt.stocks.items() if m["stage2"]),
                    key=lambda s: mkt.stocks[s]["trend_score"], reverse=True)
    n = p["n"]
    return rebalance(port, mkt, ranked[:n], exposure(mkt) / n, set(ranked[: 2 * n]), p["stop_pct"], "top tendência")


def s_joalheiro(cfg, port, mkt, p):
    if not mkt.rankings or not due(port, mkt, p["cada"]):
        return []
    rows = sorted((r for r in mkt.rankings if r.get("quality_score", 0) >= p["qualidade_min"]),
                  key=lambda r: r["score"], reverse=True)
    n = p["n"]
    targets = [r["symbol"] for r in rows[:n]]
    return rebalance(port, mkt, targets, exposure(mkt) / n, set(), p["stop_pct"], "qualidade")


def s_cacador(cfg, port, mkt, p):
    if mkt.zone == "Queda":
        return []
    picks = [
        s for s, m in mkt.stocks.items()
        if m["stage2"] and m["high20_prev"] < m["price"] <= m["high20_prev"] * 1.03 and m["vol_ratio"] >= 1.5
        and s not in port["positions"]
    ]
    orders = []
    for s in sorted(picks, key=lambda s: mkt.stocks[s]["vol_ratio"], reverse=True)[: max(free_slots(port, p["n"]), 0)]:
        m = mkt.stocks[s]
        stop = m["low10_prev"]
        eur = risk_sized(port, mkt, s, stop, p["risco_pct"], p["max_pos"])
        if eur >= MIN_ORDER_EUR:
            tp = m["price"] + p["alvo_r"] * (m["price"] - stop)
            orders.append(order_buy(s, eur, "rompimento", stop=round(stop, 4), tp=round(tp, 4)))
    return orders


def s_sniper(cfg, port, mkt, p):
    orders = [order_sell(s, "perdeu a média 50d") for s in port["positions"]
              if (m := metrics(mkt, s)) and m["price"] < m["sma50"]]
    if mkt.zone == "Queda":
        return orders
    own = {
        s for s, m in mkt.stocks.items()
        if m["stage2"] and m["range10"] <= p["aperto_pct"] and m["price"] >= m["high20_prev"] * 0.97 and m["trend_score"] >= 80
    }
    picks = [s for s in (mkt.vcp & set(mkt.stocks)) | own if s not in port["positions"]]
    for s in sorted(picks, key=lambda s: mkt.stocks[s]["trend_score"], reverse=True)[: max(free_slots(port, p["n"]), 0)]:
        stop = mkt.stocks[s]["low10_prev"]
        eur = risk_sized(port, mkt, s, stop, p["risco_pct"], p["max_pos"])
        if eur >= MIN_ORDER_EUR:
            orders.append(order_buy(s, eur, "VCP" if s in mkt.vcp else "contração", stop=round(stop, 4)))
    return orders


def s_campeao(cfg, port, mkt, p):
    if not due(port, mkt, p["cada"]):
        return []
    by_sector: dict[str, list[str]] = {}
    for s, m in mkt.stocks.items():
        by_sector.setdefault(m["sector"], []).append(s)
    strength = {sec: sum(mkt.stocks[s]["stage2"] for s in syms) / len(syms) for sec, syms in by_sector.items() if len(syms) >= 5}
    top = sorted(strength, key=strength.get, reverse=True)[: p["setores"]]
    targets = []
    for sec in top:
        ranked = sorted((s for s in by_sector[sec] if mkt.stocks[s]["stage2"]),
                        key=lambda s: mkt.stocks[s]["trend_score"], reverse=True)
        targets += ranked[: p["por_setor"]]
    if not targets:
        return rebalance(port, mkt, [], 0, set(), None, "")
    return rebalance(port, mkt, targets, exposure(mkt) / len(targets), set(), p["stop_pct"],
                     "setor forte: " + ", ".join(SECTORS_PT.get(s, s) for s in top))


def s_pescador(cfg, port, mkt, p):
    orders = [order_sell(s, f"{p['max_dias']} sessões") for s, pos in port["positions"].items()
              if sessions_since(mkt, pos["entry_date"]) >= p["max_dias"]]
    if mkt.zone == "Queda":
        return orders
    picks = [
        s for s, m in mkt.stocks.items()
        if m["price"] > m["sma200"] and m["sma50"] > m["sma200"] and m["ret_1w"] <= -p["queda_pct"]
        and m["trend_score"] >= 60 and s not in port["positions"]
    ]
    eq = equity(port, mkt)
    for s in sorted(picks, key=lambda s: mkt.stocks[s]["ret_1w"])[: max(free_slots(port, p["n"]), 0)]:
        price = mkt.stocks[s]["price"]
        orders.append(order_buy(s, eq / p["n"], f"caiu {mkt.stocks[s]['ret_1w'] * 100:.0f}% na semana",
                                stop=round(price * (1 - p["stop_pct"]), 4), tp=round(price * (1 + p["alvo_pct"]), 4)))
    return orders


def s_kamikaze(cfg, port, mkt, p):
    if not due(port, mkt, p["cada"]):
        return []
    ranked = sorted((s for s, m in mkt.stocks.items() if m["price"] > m["sma50"]),
                    key=lambda s: mkt.stocks[s]["ret_3m"], reverse=True)
    n = p["n"]
    return rebalance(port, mkt, ranked[:n], 1.0 / n, set(), p["stop_pct"], "maior subida a 3 meses")


STRATEGIES = {
    "indexador": (s_indexador, "Compra e mantém o S&P 500 (referência)", {}),
    "guardiao": (s_guardiao, "ETF mundial com peso pela zona do mercado", {"max_exposicao": 1.0, "folga": 0.1}),
    "rotador": (s_rotador, "Roda entre os melhores ETFs UCITS", {"n": 3, "cada": 5}),
    "foguetao": (s_foguetao, "Top ações por score de tendência", {"n": 5, "stop_pct": 0.10, "cada": 5}),
    "joalheiro": (s_joalheiro, "Melhores do ranking com qualidade alta", {"n": 6, "qualidade_min": 70, "stop_pct": 0.15, "cada": 20}),
    "cacador": (s_cacador, "Rompimentos com volume", {"n": 5, "risco_pct": 0.02, "alvo_r": 3.0, "max_pos": 0.30}),
    "sniper": (s_sniper, "Contração de volatilidade (VCP)", {"n": 4, "risco_pct": 0.01, "aperto_pct": 0.06, "max_pos": 0.30}),
    "campeao": (s_campeao, "Melhores ações dos setores mais fortes", {"setores": 2, "por_setor": 2, "stop_pct": 0.12, "cada": 5}),
    "pescador": (s_pescador, "Compra quedas em ações em tendência", {"n": 5, "queda_pct": 0.05, "alvo_pct": 0.06, "stop_pct": 0.07, "max_dias": 10}),
    "kamikaze": (s_kamikaze, "Tudo nas maiores subidas a 3 meses", {"n": 2, "stop_pct": 0.20, "cada": 5, "ignora_resultados": True}),
}


# --------------------------------------------------------------------------
# Config and state
# --------------------------------------------------------------------------


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def load_state() -> dict:
    return json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {"agents": {}}


def save(config: dict | None, state: dict | None) -> None:
    if config is not None:
        CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    if state is not None:
        STATE_PATH.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n")


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def find_agent(config: dict, name: str) -> dict | None:
    key = slug(name or "")
    return next((a for a in config["agents"] if a["id"] == key or slug(a["name"]) == key), None)


def active_agents(config: dict) -> list[dict]:
    return [a for a in config["agents"] if a.get("active", True)]


# --------------------------------------------------------------------------
# Daily run and report
# --------------------------------------------------------------------------


def skip_earnings(orders: list[dict], mkt: Market) -> tuple[list[dict], list[str]]:
    """Drop buys of stocks that report within EARNINGS_AVOID_DAYS; return (kept, skipped symbols)."""
    kept, skipped = [], []
    for o in orders:
        if o["side"] == "buy" and not is_eur(o["symbol"]):
            if o["symbol"] not in mkt.earnings:
                mkt.earnings[o["symbol"]] = earnings.next_earnings(o["symbol"], mkt.as_of)
            if earnings.within(mkt.earnings[o["symbol"]], EARNINGS_AVOID_DAYS, mkt.as_of):
                skipped.append(o["symbol"])
                continue
        kept.append(o)
    return kept, skipped


def run_day(config: dict, state: dict, mkt: Market) -> tuple[list[tuple[dict, dict]], list[str], list[str]]:
    moves, training, avoided = [], [], []
    for cfg in active_agents(config):
        port = state["agents"].setdefault(cfg["id"], new_portfolio(cfg["capital"], mkt.as_of))
        params = port.setdefault("params", dict(cfg["params"]))
        for t in fill_pending(port, mkt) + check_exits(port, mkt):
            moves.append((cfg, t))
        score_day(port, mkt)
        note = train(cfg, port, mkt)
        if note:
            training.append(f"{cfg['emoji']} **{cfg['name']}** {note}")
        if port["last_decision"] != mkt.as_of.isoformat() and not port["pending"]:
            orders = STRATEGIES[cfg["strategy"]][0](cfg, port, mkt, params)
            if not params.get("ignora_resultados"):
                orders, skipped = skip_earnings(orders, mkt)
                if skipped:
                    avoided.append(f"{cfg['emoji']} {cfg['name']}: " + ", ".join(
                        f"{s} ({earnings.label(mkt.earnings[s])})" for s in skipped))
            port["pending"] = [{**o, "created": mkt.as_of.isoformat()} for o in orders if o["side"] == "sell" or o["eur"] >= MIN_ORDER_EUR]
            port["last_decision"] = mkt.as_of.isoformat()
    return moves, training, avoided


def eur(x: float) -> str:
    return f"{x:,.0f} €".replace(",", " ")


def pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def day_change(port: dict) -> float:
    h = port["history"]
    if len(h) < 2 or not h[-2]["equity"]:
        return 0.0
    return (h[-1]["equity"] - (h[-1]["deposits"] - h[-2]["deposits"])) / h[-2]["equity"] - 1


def league(config: dict, state: dict, mkt: Market, title: str, moves=(), training=(), avoided=()) -> tuple[list[dict], dict]:
    rows = []
    for cfg in active_agents(config):
        port = state["agents"].get(cfg["id"])
        if port:
            rows.append((cfg, port, equity(port, mkt)))
    rows.sort(key=lambda r: (r[1]["points"], r[2] / r[1]["deposits"]), reverse=True)

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (cfg, port, eq) in enumerate(rows):
        total = eq / port["deposits"] - 1
        lines.append(
            f"{medals[i] if i < 3 else f'`{i + 1:>2}`'} {cfg['emoji']} **{cfg['name']}** · {eur(eq)} ({pct(total)})"
            f" · hoje {pct(day_change(port))} · ⭐ **{port['points']}** · {len(port['positions'])} pos."
        )
    fields = [("🧭 Mercado", f"{ZONE_ICON[mkt.zone]} zona **{mkt.zone}** · {mkt.stage2_share * 100:.0f}% das ações do S&P 500 em tendência de alta")]
    if moves:
        move_lines = []
        for cfg, t in list(moves)[:15]:
            if t["side"] == "compra":
                move_lines.append(f"🟢 {cfg['emoji']} {cfg['name']} comprou **{t['symbol']}** · {eur(t['eur'])} ({t['reason']})")
            else:
                move_lines.append(f"🔴 {cfg['emoji']} {cfg['name']} vendeu **{t['symbol']}** · {t['pnl_eur']:+.0f} € ({t['reason']})")
        fields.append(("📥 Movimentos", "\n".join(move_lines)))
    if avoided:
        fields.append((f"📅 Compras evitadas: resultados nos próximos {EARNINGS_AVOID_DAYS} dias", "\n".join(avoided)[:1024]))
    if training:
        fields.append(("🧬 Treino do mês (agentes que perderam dinheiro mudam de parâmetros)", "\n".join(training)[:1024]))
    pending = sum(len(p["pending"]) for _, p, _ in rows)
    if pending:
        fields.append(("⏳ Ordens para a próxima abertura", f"{pending} ordens à espera de execução"))
    if rows and rows[0][1]["pending"]:
        cfg, port, _ = rows[0]
        signals = [f"{'🟢 compra' if o['side'] == 'buy' else '🔴 vende'} **{o['symbol']}**"
                   + (f" ({eur(o['eur'])})" if o["side"] == "buy" else "") for o in port["pending"]]
        fields.append((f"📣 Sinais do líder: {cfg['emoji']} {cfg['name']} na próxima abertura", " · ".join(signals)[:1024]))

    series = {
        cfg["name"]: [(h["date"], (h["equity"] / h["deposits"] - 1) * 100) for h in port["history"]]
        for cfg, port, _ in rows
    }
    files = {"agentes.png": lines_chart(series, "Rentabilidade desde o início")}
    leader = rows[0][2] / rows[0][1]["deposits"] - 1 if rows else 0
    card = embed(
        title,
        "\n".join(lines),
        color=GREEN if leader >= 0 else RED,
        fields=fields,
        image="agentes.png",
        footer="Simulação com dinheiro fictício e preços reais: ordens à abertura seguinte, 0,1% de custos. "
        "Pontos: +10 por dia em lucro, ±1 por cada 0,1% do dia. Não é recomendação de compra.",
    )
    return [card], files


def month_changed(state: dict) -> str | None:
    """The month that just ended, if the latest run is the first of a new month."""
    for port in state["agents"].values():
        h = port["history"]
        if len(h) >= 2:
            return h[-2]["date"][:7] if h[-1]["date"][:7] != h[-2]["date"][:7] else None
    return None


def monthly_report(config: dict, state: dict, month: str) -> tuple[list[dict], dict]:
    rows = []
    for cfg in active_agents(config):
        port = state["agents"].get(cfg["id"])
        ret = month_return(port, month) if port else None
        if ret is None:
            continue
        sells = [t for t in port["trades"] if t["side"] == "venda" and t["date"].startswith(month)]
        best = max(sells, key=lambda t: t["pnl_eur"], default=None)
        worst = min(sells, key=lambda t: t["pnl_eur"], default=None)
        pts = sum(p["pts"] for p in port["points_log"] if p["date"].startswith(month))
        rows.append((cfg, ret, pts, best, worst, len(sells)))
    if not rows:
        return [], {}
    rows.sort(key=lambda r: r[1], reverse=True)
    bench = next((r[1] for r in rows if r[0]["strategy"] == "indexador"), None)
    lines = []
    for cfg, ret, pts, best, worst, n in rows:
        vs = "" if bench is None or cfg["strategy"] == "indexador" else (" · ✅ bateu o S&P 500" if ret > bench else " · ❌ abaixo do S&P 500")
        extra = ""
        if best:
            extra = f"\n   melhor **{best['symbol']}** {best['pnl_eur']:+.0f} € · pior **{worst['symbol']}** {worst['pnl_eur']:+.0f} € · {n} vendas"
        lines.append(f"{cfg['emoji']} **{cfg['name']}** {pct(ret)} · ⭐ {pts:+d}{vs}{extra}")
    files = {"mes.png": returns_chart([(r[0]["name"], r[1] * 100) for r in rows], f"Rentabilidade em {month}")}
    card = embed(f"📆 Relatório mensal dos agentes · {month}", "\n".join(lines)[:4096], color=BLUE, image="mes.png",
                 footer="Agentes que perderam dinheiro no mês ajustam os parâmetros (treino). Simulação com dinheiro fictício.")
    return [card], files


def agent_card(cfg: dict, port: dict, mkt: Market) -> list[dict]:
    eq = equity(port, mkt)
    params = port.get("params", cfg["params"])
    pos_lines = []
    for s, pos in sorted(port["positions"].items()):
        value = position_value(mkt, s, pos)
        stop = f" · stop {pos['stop']:.2f}" if pos.get("stop") else ""
        pos_lines.append(f"**{s}** {eur(value)} ({pct(value / pos['cost_eur'] - 1)}){stop}")
    trades = [f"{t['date'][5:]} {t['side']} **{t['symbol']}** {eur(t['eur'])}"
              + (f" ({t['pnl_eur']:+.0f} €)" if t["side"] == "venda" else "") for t in port["trades"][-6:]]
    fields = [
        ("📊 Estado", f"Património **{eur(eq)}** · recebido {eur(port['deposits'])} · resultado {pct(eq / port['deposits'] - 1)}\n"
                     f"Dinheiro livre {eur(port['cash'])} · ⭐ {port['points']} pontos · geração {port['generation']}"),
        ("📁 Posições", "\n".join(pos_lines) or "sem posições"),
        ("⚙️ Parâmetros", ", ".join(f"{k}={v}" for k, v in params.items()) or "nenhum"),
    ]
    if trades:
        fields.append(("🧾 Últimas operações", "\n".join(trades)))
    if port["evolution"]:
        fields.append(("🧬 Treino", port["evolution"][-1]["note"]))
    return [embed(f"{cfg['emoji']} {cfg['name']} · {STRATEGIES[cfg['strategy']][1]}", cfg.get("description", ""),
                  color=GREEN if eq >= port["deposits"] else RED, fields=fields)]


# --------------------------------------------------------------------------
# Configurator (/agente) and CLI
# --------------------------------------------------------------------------


def configure(args: dict, config: dict, state: dict, mkt: Market | None) -> tuple[list[dict], dict, bool]:
    """Apply one /agente action. Returns (embeds, files, changed)."""
    action = args.get("acao", "ver")
    name = (args.get("nome") or "").strip()
    if action == "criar":
        strategy = args.get("estrategia", "")
        if strategy not in STRATEGIES:
            return [embed("⚠️ Estratégia desconhecida", ", ".join(STRATEGIES), color=RED)], {}, False
        capital = float(args.get("capital") or 1000)
        if not name or find_agent(config, name) or capital <= 0:
            return [embed("⚠️ Nome em falta, repetido ou capital inválido", f"`{name}` · {capital}", color=RED)], {}, False
        cfg = {"id": slug(name), "name": name[:40], "emoji": (args.get("emoji") or "🤖")[:4], "strategy": strategy,
               "capital": capital, "params": dict(STRATEGIES[strategy][2]), "description": STRATEGIES[strategy][1]}
        config["agents"].append(cfg)
        start = mkt.as_of if mkt else date.today()
        state["agents"][cfg["id"]] = new_portfolio(capital, start)
        return [embed(f"✅ Agente criado: {cfg['emoji']} {cfg['name']}",
                      f"Estratégia **{strategy}** ({STRATEGIES[strategy][1]}) com {eur(capital)}. Começa a investir na próxima execução.",
                      color=GREEN)], {}, True
    cfg = find_agent(config, name) if name else None
    if action in ("depositar", "remover") and not cfg:
        return [embed("⚠️ Agente não encontrado", f"`{name}`", color=RED)], {}, False
    if action == "depositar":
        amount = float(args.get("valor") or 0)
        if amount <= 0 or amount > 1_000_000:
            return [embed("⚠️ Valor inválido", str(amount), color=RED)], {}, False
        port = state["agents"].setdefault(cfg["id"], new_portfolio(0, date.today()))
        port["cash"] += amount
        port["deposits"] += amount
        return [embed(f"💶 {cfg['emoji']} {cfg['name']} recebeu {eur(amount)}",
                      f"Dinheiro livre: {eur(port['cash'])}. Total recebido: {eur(port['deposits'])}.", color=GREEN)], {}, True
    if action == "remover":
        cfg["active"] = False
        return [embed(f"🗑️ {cfg['emoji']} {cfg['name']} desativado",
                      "Deixa de negociar e de aparecer na liga. O histórico fica guardado.", color=BLUE)], {}, True
    if mkt is None:
        return [embed("⚠️ Sem dados de mercado", "Tenta mais tarde.", color=RED)], {}, False
    if cfg:
        port = state["agents"].get(cfg["id"])
        if not port:
            return [embed(f"{cfg['emoji']} {cfg['name']}", "Ainda não começou a investir.", color=BLUE)], {}, False
        return agent_card(cfg, port, mkt), {}, False
    embeds, files = league(config, state, mkt, f"🤖 Liga dos agentes · fecho de {mkt.as_of.isoformat()}")
    return embeds, files, False


def reply_target() -> dict:
    app, token = os.environ.get("APPLICATION_ID"), os.environ.get("INTERACTION_TOKEN")
    if app and token:
        return {"url": f"https://discord.com/api/v10/webhooks/{app}/{token}/messages/@original", "method": "PATCH"}
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["daily", "config"])
    parser.add_argument("args", nargs="?", default="{}", help="config: JSON with acao, nome, estrategia, capital, valor, emoji")
    parser.add_argument("--dry-run", action="store_true", help="Print instead of posting; state is still saved")
    a = parser.parse_args()

    config, state = load_config(), load_state()
    target = reply_target()
    follow_up = ([], {})
    try:
        if a.mode == "daily":
            mkt = load_market()
            moves, training, avoided = run_day(config, state, mkt)
            save(None, state)
            embeds, files = league(config, state, mkt, f"🤖 Liga dos agentes · fecho de {mkt.as_of.isoformat()}",
                                   moves, training, avoided)
            ended = month_changed(state)
            if ended:
                follow_up = monthly_report(config, state, ended)
        else:
            args = json.loads(a.args or "{}")
            needs_market = args.get("acao", "ver") in ("ver", "criar")
            mkt = load_market() if needs_market else None
            embeds, files, changed = configure(args, config, state, mkt)
            if changed:
                save(config, state)
    except Exception as e:  # always answer a slash command
        if not target:
            raise
        print(f"ERROR: {e!r}", file=sys.stderr)
        embeds, files = [embed("⚠️ Erro nos agentes", str(e)[:300], color=RED)], {}
    status = send(embeds, files, dry_run=a.dry_run, **target)
    if follow_up[0]:
        status = status or send(*follow_up, dry_run=a.dry_run)
    return status


if __name__ == "__main__":
    sys.exit(main())
