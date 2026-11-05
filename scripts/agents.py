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

State lives in agents/state.json, committed by the workflow to the private repo checked out at agents/.

    uv run python scripts/agents.py daily --dry-run
    uv run python scripts/agents.py config '{"acao": "ver"}' --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import re
import statistics
import sys
import unicodedata
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

import discord_bot
import earnings
import interactive
import opportunities
import strategy_docs
from charts import lines_chart, returns_chart
from references import reference_lines
from discord_daily import WATCHLIST_DIR
from discord_post import BLUE, GREEN, RED, detail_posts, embed, route, send
from rankings import (
    SECTORS_PT,
    UCITS_ETFS,
    add_trend_scores,
    download_history,
    is_stage2,
    load_constituents,
    trend_metrics,
)

ETF_SYMBOLS = frozenset(t for t, _ in UCITS_ETFS)

REPO_AGENTS = WATCHLIST_DIR.parents[1] / "agents"
CONFIG_PATH = REPO_AGENTS / "config.json"
STATE_PATH = REPO_AGENTS / "state.json"

# Costs per broker, from Portugal (config.json "corretora", or OGJ_CORRETORA; the training, the points and the league
# all use the active one). spread: spread and slippage per side; fx: currency conversion per side for anything not
# quoted in euros; commission per order: in Europe a share of the value with a minimum and a maximum in euros; US
# stocks per share, with a minimum and a maximum share of the value (in dollars).
# - XTB: no commission on stocks and ETFs up to 100 000 EUR a month; 0.5% conversion (0.8% at weekends).
# - Trading 212: no commission; 0.15% conversion.
# - Interactive Brokers (IBKR Pro, tiered): Europe 0.05% (min 1.25 EUR, max 29 EUR; Lisbon too) + ~0.01% exchange and
#   clearing fees; US 0.0035 USD a share (min 0.35 USD, max 1% of the value); conversion ~0.03%; it routes to the
#   exchange itself, so a smaller spread. The minimum is per order: it weighs on small accounts.
BROKERS = {
    "xtb": {"nome": "XTB", "spread": 0.001, "fx": 0.005},
    "trading212": {"nome": "Trading 212", "spread": 0.001, "fx": 0.0015},
    "ibkr": {"nome": "Interactive Brokers", "spread": 0.0005, "fx": 0.0003,
             "eu": {"pct": 0.0006, "min_eur": 1.25, "max_eur": 29.0},
             "us": {"por_acao": 0.0035, "min": 0.35, "max_pct": 0.01}},
}
DEFAULT_BROKER = "xtb"
BROKER = DEFAULT_BROKER
FEE = BROKERS[BROKER]["spread"]
FX_FEE = BROKERS[BROKER]["fx"]


def set_broker(name: str | None) -> str:
    """Use this broker's costs from now on (unknown or empty: the default)."""
    global BROKER, FEE, FX_FEE
    BROKER = name if name in BROKERS else DEFAULT_BROKER
    FEE, FX_FEE = BROKERS[BROKER]["spread"], BROKERS[BROKER]["fx"]
    return BROKER


def broker_name(name: str | None = None) -> str:
    return BROKERS[name or BROKER]["nome"]


def costs_text() -> str:
    """The active broker's costs, for the league card and the portal."""
    b = BROKERS[BROKER]
    parts = [f"spread {FEE:.2%}", f"+{FX_FEE:.2%} de câmbio fora do euro"]
    if b.get("eu"):
        e = b["eu"]
        parts.append(f"comissão na Europa {e['pct']:.2%} (mín. {e['min_eur']:.2f} €, máx. {e['max_eur']:.0f} €)")
    if b.get("us"):
        u = b["us"]
        parts.append(f"nos EUA {u['por_acao']} $ por ação (mín. {u['min']:.2f} $)")
    else:
        parts.append("sem comissão")
    return f"{b['nome']}: " + ", ".join(parts)
TAX_RATE = 0.28  # Portuguese tax on a year's net realised gains (autonomous taxation: a year's losses offset only that year)
# The tax of a year is paid with the IRS of the next one (return by 30 June, payment by 31 August): until then the
# money stays invested.
TAX_DUE = (8, 31)
MIN_ORDER_EUR = 20.0
# Reward: 1 point per 0.1% beaten (or lost) against the S&P 500 that day, a small bonus for being in profit,
# and a daily penalty while the agent is more than DD_FREE below its best level.
BENCHMARK = "SXR8.DE"
PROFIT_BONUS = 2
DD_FREE = 0.10
DD_PENALTY = 50  # points per 100% of drawdown beyond DD_FREE
BANKRUPTCY = 0.5  # an agent that loses half of the money it was given is replaced by a child of the leader
# Risk management for every agent without ignora_risco:
TARGET_VOL = 0.02  # daily volatility a full-size position is meant for; more volatile stocks get smaller
SECTOR_CAP = 0.40  # at most this share of equity in one sector
DD_BRAKE = 0.20  # a fall this deep from the last high pauses new buys…
BRAKE_DAYS = 14  # …for this many calendar days; the brake then re-arms from the current level
# Intraday rounds (react), for every reactive agent without ignora_risco. Exits are never held back.
NO_BUYS_AFTER_OPEN = 15  # minutes: the first prices of a session swing the most
NO_BUYS_BEFORE_CLOSE = 30  # minutes: a buy this late has no time left to work today
DAY_LOSS_STOP = 0.03  # an agent down this much since the last close makes no new buys until tomorrow
# How much a reactive agent buys during the session (exits are always watched in every round). Trained per strategy
# and profile by the intraday lab (scripts/intraday_lab.py --reatividade), not by the 20-year daily pre-training:
#   0 planeado: buys only at the next open (the round's buy orders wait as pending orders, with the gap rule);
#   1 com confirmação: buys during the session only espera_min minutes after the open and while the price is at most
#     esticado_max above the last close (0 = no limit), so it does not chase a morning spike;
#   2 total: buys in every round (after NO_BUYS_AFTER_OPEN minutes).
REACTIVITY = {0: "planeado (compras só à abertura)", 1: "com confirmação", 2: "total"}
INTRADAY_DEFAULTS = {"reatividade": 2, "espera_min": NO_BUYS_AFTER_OPEN, "esticado_max": 0}
EXPOSURE = {"Alta": 1.0, "Neutra": 0.7, "Cautelosa": 0.4, "Queda": 0.1}
ZONE_ICON = {"Alta": "🟢", "Neutra": "🟡", "Cautelosa": "🟠", "Queda": "🔴"}
MUTATION = 0.2
EARNINGS_AVOID_DAYS = 5
EARNINGS_AVOID_DAYS_DEFAULT = EARNINGS_AVOID_DAYS
NEWS_VETO = 3  # no agent buys a stock whose 36 h news score is at or below -NEWS_VETO (peso_noticias tunes it)
NEWS_VETO_DEFAULT = NEWS_VETO
# Strategies that cannot be replayed on past data (no history of fundamentals, news or other agents):
# they learn by monthly mutation; the rest are re-trained every Sunday by pretrain.py.
# Investor profiles. The pre-training picks each strategy's parameters for one profile at a time, scoring every
# training year by the growth of the capital (log of 1 + the year's return): a losing year counts `loss` times, and
# the depth of the fall beyond `dd_cap` costs `dd_cost` per unit. So the conservative profile prefers parameters that
# rarely lose and fall little, the aggressive one the most growth whatever the path. The league has one agent per
# strategy and profile (the balanced one keeps the original name).
PROFILES = {
    "conservador": {"label": "Conservador", "emoji": "🛡️", "loss": 3.0, "dd_cap": 0.15, "dd_cost": 3.0},
    "equilibrado": {"label": "Equilibrado", "emoji": "⚖️", "loss": 2.0, "dd_cap": 0.25, "dd_cost": 2.0},
    "arrojado": {"label": "Arrojado", "emoji": "🔥", "loss": 1.0, "dd_cap": 0.40, "dd_cost": 1.0},
}
DEFAULT_PROFILE = "equilibrado"


def profile_of(cfg: dict) -> str:
    return cfg.get("perfil", DEFAULT_PROFILE)


def pretrain_path(profile: str = DEFAULT_PROFILE) -> Path:
    """The latest pre-training report of a profile (the balanced one keeps pretrain.json)."""
    return REPO_AGENTS / ("pretrain.json" if profile == DEFAULT_PROFILE else f"pretrain-{profile}.json")


def profile_score(ret: float, years: float, max_dd: float, profile: str = DEFAULT_PROFILE) -> float:
    """The profile's grade of a result: yearly log growth (x1000), losses weighted and deep falls penalised."""
    p = PROFILES[profile]
    growth = math.log(max(1 + ret, 0.01)) / max(years, 1 / 252)
    if growth < 0:
        growth *= p["loss"]
    growth -= p["dd_cost"] * max(0.0, max_dd - p["dd_cap"])
    return round(growth * 1000, 1)


# The S&P 500 is the first base: a period's points are the profile's grade minus the index's; above the index there
# is no ceiling, below it every point counts BELOW_INDEX times (beating the index every year is worth more than one
# great year).
BELOW_INDEX = 2.0


def relative_points(own: float, index: float) -> float:
    excess = own - index
    return round(excess if excess >= 0 else BELOW_INDEX * excess, 1)


# Where a strategy buys stocks (a trained parameter, regiao): 0 both, 1 only the US, 2 only Europe. The strategies
# that trade only ETFs or other agents' portfolios have no use for it, so it is neither trained nor shown for them.
REGIONS = {0: "EUA e Europa", 1: "só EUA", 2: "só Europa"}
NO_REGION = {"indexador", "guardiao", "rotador", "pendulo", "joalheiro", "conselho", "maestro", "navegador"}


def uses_param(strategy: str | None, key: str) -> bool:
    return not (key == "regiao" and strategy in NO_REGION)


def in_region(mkt: "Market", region: int, held=()) -> "Market":
    """The market with only the stocks of that region, plus the ones the agent still holds elsewhere (so its exits
    keep seeing them after the region changed). Kept per market, so a backtest builds each view once."""
    if not region:
        return mkt
    inside = is_us if region == 1 else (lambda s: not is_us(s))
    extra = frozenset(s for s in held if s in mkt.stocks and not inside(s))
    cache = mkt.__dict__.setdefault("_regions", {})
    if (region, extra) not in cache:
        from dataclasses import replace

        view = replace(mkt, stocks={s: m for s, m in mkt.stocks.items() if inside(s) or s in extra})
        view.league, view.earnings, view.news = mkt.league, mkt.earnings, mkt.news  # shared, not copied
        cache[(region, extra)] = view
    return cache[(region, extra)]


# How an agent trades. Reactive (the default): decides in every intraday round with the price of that moment and
# trades at once in the markets that are open, while its plan for the next opens waits as pending orders. Daily: the
# twin of each reactive agent, deciding once a day before the opens (07:30), as in the 20-year pre-training.
REACTIVE, DAILY = "reativo", "diario"
LIVE_ONLY = {"joalheiro", "jornalista", "catalisador", "cetico", "conselho", "maestro", "super", "visionario"}
META = {"conselho", "maestro", "super"}  # decide after the others, from their portfolios
INTRADAY_ONLY = {"relampago"}  # trade within the day: the daily backtest skips them, the intraday lab replays them
TRADES_KEPT = 200  # in the state; every trade is also kept for good in agents/arquivo/decisoes (market_archive)
POINTS_LOG_KEPT = 60
# Each agent's changes (training, mutations, renewals): 200 in the state, every one kept in agents/arquivo/evolucao
EVOLUTION_KEPT = 200

# Numeric parameters the monthly training may change, with their bounds.
PARAM_BOUNDS = {
    "banda": (0.01, 0.05, float),
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
    "rompimento_max": (0.002, 0.03, float),
    "salto_pct": (0.03, 0.12, float),
    "limiar": (2, 9, int),
    "min_votos": (2, 4, int),
    "lideres": (2, 5, int),
    "janela": (10, 40, int),
    # shared by every strategy (COMMON); 0 switches a rule off
    "stop_atr": (1.5, 5.0, float),
    "trail_atr": (2.0, 8.0, float),
    "tempo_max": (10, 60, int),
    "min_tecnico": (20.0, 55.0, float),
    "max_ext": (0.05, 0.35, float),
    "gap_max": (0.02, 0.15, float),
    "regiao": (1, 2, int),  # 0 (off) = both regions
    "peso_noticias": (1, 6, int),
    # market context (context.py), shared too; 0 = off
    "vix_max": (18.0, 45.0, float),
    "vix_meio": (15.0, 35.0, float),
    "epu_max": (0.60, 0.98, float),
    "credito_max": (0.02, 0.15, float),
    "juros_max": (0.3, 1.5, float),
    "usd_max": (0.02, 0.10, float),
    "filtro_spy": (1, 1, int),
    "gpr_max": (0.60, 0.98, float),
    "petroleo_max": (0.15, 0.60, float),
    "ouro_max": (0.06, 0.25, float),
    # the stock's sector, technical levels and fundamentals; 0 = off
    "filtro_setor": (1, 2, int),
    "ema_filtro": (1, 1, int),
    "filtro_agenda": (0.1, 1.5, float),  # no buys in a sector the political agenda disfavours by more than this
    "stop_suporte": (0.25, 1.5, float),
    "qualidade_fund_min": (30.0, 75.0, float),
    "valor_fund_min": (20.0, 70.0, float),
    # calendar, live only (no free history to backtest them): tuned by the monthly mutation and the shadow copies
    "dias_resultados": (0, 10, int),
    "evita_macro": (1, 1, int),
    "evita_geo": (1, 1, int),
    # the Analista's own: the minimum value score and how much quality, value and trend weigh in its ranking
    "valor_min": (20.0, 70.0, float),
    "peso_qualidade": (0.2, 2.0, float),
    "peso_valor": (0.2, 2.0, float),
    "peso_tendencia": (0.2, 2.0, float),
    # Quant: weights of each factor (0 = ignored) and whether it only buys stocks in an uptrend
    "peso_momentum": (0.0, 2.0, float),
    "peso_baixa_vol": (0.0, 2.0, float),
    "so_tendencia": (0, 1, int),
    # Abutre: how far below the 52-week high a quality stock must be
    "queda_min": (0.10, 0.45, float),
    # Estratega: the core in the S&P 500, the weights of the political-agenda signals by sector (policy.py)
    "nucleo": (0.0, 0.8, float),
    "peso_gasto": (0.0, 2.0, float),
    "peso_incerteza": (0.0, 2.0, float),
    "peso_capex": (0.0, 2.0, float),
    "segue_zona": (0, 1, int),
    # Navegador: how far a six-month ratio must move to call a regime (context.regime_fields)
    "limiar_conc": (0.0, 0.06, float),
    # Temático: how many themes and stocks per theme; an industry this far above its 200-day average is exhausting
    "temas": (1, 5, int),
    "por_tema": (1, 5, int),
    "esgotado_max": (0.15, 0.60, float),
    "limiar_credito": (0.0, 0.08, float),
    "limiar_ciclo": (0.02, 0.20, float),
    "limiar_corr": (0.0, 0.6, float),
    # Pêndulo: minimum momentum of the best equity ETF to stay in equities
    "limiar_mom": (0.0, 0.08, float),
}
# A strategy's own bounds where they differ (the Estratega rebalances by the month or the quarter: a slow rotation pays
# less tax, see agents/lab/agenda-*.json).
STRATEGY_BOUNDS = {"estratega": {"cada": (21, 126, int)}, "tematico": {"cada": (10, 126, int)}}


def bounds_of(strategy: str | None, key: str) -> tuple:
    return STRATEGY_BOUNDS.get(strategy or "", {}).get(key) or PARAM_BOUNDS[key]


# Variables every strategy (but the Indexador) has, on top of its own. The defaults keep the original
# behaviour; the training (pre-training, monthly mutation and the shadow copies) switches them on and tunes them.
# gap_max is on from the start (8%): a buy is cancelled when the stock opens that far from the close it was decided on.
COMMON = {"stop_atr": 0, "trail_atr": 0, "tempo_max": 0, "min_tecnico": 0, "max_ext": 0, "gap_max": 0.08, "regiao": 0,
          "peso_noticias": NEWS_VETO_DEFAULT,
          "vix_max": 0, "vix_meio": 0, "epu_max": 0, "credito_max": 0, "juros_max": 0, "usd_max": 0, "filtro_spy": 0,
          "gpr_max": 0, "petroleo_max": 0, "ouro_max": 0, "filtro_setor": 0, "ema_filtro": 0, "filtro_agenda": 0,
          "stop_suporte": 0, "qualidade_fund_min": 0, "valor_fund_min": 0,
          "dias_resultados": EARNINGS_AVOID_DAYS_DEFAULT, "evita_macro": 0, "evita_geo": 0}
OFFABLE = {"stop_atr", "trail_atr", "tempo_max", "min_tecnico", "max_ext", "gap_max", "regiao", "vix_max", "vix_meio", "epu_max", "credito_max",
           "juros_max", "usd_max", "filtro_spy", "gpr_max", "petroleo_max", "ouro_max", "filtro_setor", "ema_filtro", "stop_suporte",
           "qualidade_fund_min", "valor_fund_min", "evita_macro", "evita_geo", "filtro_agenda"}
CONTEXT_RULES = {"vix_max", "vix_meio", "epu_max", "credito_max", "juros_max", "usd_max", "filtro_spy", "gpr_max", "petroleo_max", "ouro_max"}
# no history: not in the backtest (the earnings dates have one: data/earnings, from the SEC, so dias_resultados is trained)
LIVE_TUNED = {"peso_noticias", "evita_macro", "evita_geo"}
GEO_RISK_HIGH = 4  # evita_geo: no new buys when Claude rates the geopolitical risk this high (0-5), live only
ZONES = ("Alta", "Neutra", "Cautelosa", "Queda")


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
    # backtest: every company's past earnings dates (earnings.load_history), so the earnings rule runs there too
    earnings_dates: dict[str, list[date]] | None = None
    closes: dict[str, float] | None = None  # as_of closes, precomputed by the backtest for speed
    now: datetime | None = None  # the intraday rounds' clock (set by react), for strategies that trade within the day
    news: dict[str, dict] = field(default_factory=dict)  # news.py signals: {symbol: {score, importance, items}}
    league: dict[str, tuple[dict, dict]] = field(default_factory=dict)  # {agent id: (cfg, portfolio)}, for meta-agents
    priors: dict[str, dict] = field(default_factory=dict)  # pre-training: {strategy: {zone: avg daily excess return}}
    context: dict = field(default_factory=dict)  # context.py: vix, epu_pct, credit_dd, hy, rate_chg, usd_chg, spy_trend; live: macro
    fx_rates: dict[str, pd.Series] = field(default_factory=dict)  # other currencies per euro (GBP, CHF, SEK, DKK, NOK)


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------


# A gap event: the open is GAP_MIN above the previous close, on GAP_VOLUME x the 50-day volume.
GAP_MIN = 0.03
GAP_VOLUME = 2.0
GAP_LOOKBACK = 5


def last_gap(df: pd.DataFrame) -> dict:
    """The most recent gap event in the last GAP_LOOKBACK sessions (age 0 = today), or zeros."""
    opens, closes, lows, vols = (df[c].to_numpy() for c in ("Open", "Close", "Low", "Volume"))
    for age in range(GAP_LOOKBACK):
        i = len(df) - 1 - age
        if i < 51:
            break
        gap = opens[i] / closes[i - 1] - 1
        avg = max(vols[i - 50:i].mean(), 1.0)
        if gap >= GAP_MIN and vols[i] / avg >= GAP_VOLUME:
            return {"gap_size": float(gap), "gap_low": float(lows[i]), "gap_age": age}
    return {"gap_size": 0.0, "gap_low": 0.0, "gap_age": -1}


def atr14(df: pd.DataFrame) -> float:
    """Average true range of the last 14 sessions (the same formula as pretrain.features)."""
    high, low, close = df["High"], df["Low"], df["Close"]
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    return float(tr.iloc[-14:].mean())


PIVOT = 5  # a swing low is the lowest of PIVOT days on each side; it is only known PIVOT days later


def technical_lines(df: pd.DataFrame) -> pd.DataFrame:
    """Exponential averages (21 and 50 days) and the support: the last confirmed swing low below the price,
    else the lowest low of the previous 60 days. The same for every day at once, live and in the backtest."""
    close, low = df["Close"], df["Low"]
    out = pd.DataFrame(index=df.index)
    out["ema21"] = close.ewm(span=21, adjust=False).mean()
    out["ema50"] = close.ewm(span=50, adjust=False).mean()
    pivot = low.eq(low.rolling(2 * PIVOT + 1, center=True).min())
    swing = low.where(pivot).shift(PIVOT).ffill()  # confirmed PIVOT sessions after the low, no look-ahead
    fallback = low.shift(1).rolling(60, min_periods=10).min()
    out["support"] = swing.where(swing < close, fallback)
    return out


def extra_metrics(df: pd.DataFrame) -> dict:
    before = df.iloc[:-1]
    last = df.iloc[-1]
    close = float(last["Close"])
    return {
        "high20_prev": float(before["High"].iloc[-20:].max()),
        "low10_prev": float(before["Low"].iloc[-10:].min()),
        "vol_ratio": float(last["Volume"]) / max(float(before["Volume"].iloc[-50:].mean()), 1.0),
        "range10": (float(df["High"].iloc[-10:].max()) - float(df["Low"].iloc[-10:].min())) / close,
        "vol20": float(df["Close"].pct_change().iloc[-20:].std()),
        "atr14": atr14(df),
        **last_gap(df),
        **{k: (float(v) if pd.notna(v) else None) for k, v in technical_lines(df.iloc[-300:]).iloc[-1].items()},
    }


def zone_for(stage2_share: float) -> str:
    if stage2_share >= 0.45:
        return "Alta"
    if stage2_share >= 0.30:
        return "Neutra"
    if stage2_share >= 0.15:
        return "Cautelosa"
    return "Queda"


# When each exchange's session ends (local time) and a margin for the closing auction and Yahoo's daily bar.
# During a session Yahoo returns that day as a bar still moving; deciding with it would use information the
# backtest never has (it only sees full days) and the order would still fill at that same day's open.
EXCHANGE_CLOSE = {
    "": ("America/New_York", 16, 0),  # US listings
    ".DE": ("Europe/Berlin", 17, 30), ".F": ("Europe/Berlin", 17, 30),
    ".PA": ("Europe/Paris", 17, 30), ".AS": ("Europe/Amsterdam", 17, 30), ".BR": ("Europe/Brussels", 17, 30),
    ".LS": ("Europe/Lisbon", 16, 30), ".MI": ("Europe/Rome", 17, 30), ".MC": ("Europe/Madrid", 17, 30),
    ".L": ("Europe/London", 16, 30), ".SW": ("Europe/Zurich", 17, 30), ".ST": ("Europe/Stockholm", 17, 30),
    ".CO": ("Europe/Copenhagen", 17, 0), ".HE": ("Europe/Helsinki", 18, 30), ".OL": ("Europe/Oslo", 16, 20),
    ".IR": ("Europe/Dublin", 16, 30), ".VI": ("Europe/Vienna", 17, 30),
}
CLOSE_MARGIN_MIN = 30
# When each exchange's session starts (local time): before it, a bar dated today can only be pre-market noise.
EXCHANGE_OPEN = {"": (9, 30), ".L": (8, 0), ".LS": (8, 0), ".IR": (8, 0), ".HE": (10, 0)}  # others: 9:00 local
OPEN_MARGIN_MIN = 5


def session_started(symbol: str, now: datetime) -> bool:
    """Whether today's session at the symbol's exchange has begun (and its open is known)."""
    from zoneinfo import ZoneInfo

    ex = exchange_of(symbol)
    if ex == "=X":
        return True
    tz = EXCHANGE_CLOSE[ex][0]
    hour, minute = EXCHANGE_OPEN.get(ex, (9, 0))
    local = now.astimezone(ZoneInfo(tz))
    return local >= local.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(minutes=OPEN_MARGIN_MIN)


def exchange_of(symbol: str) -> str:
    """The Yahoo suffix of a listing ('' for US stocks and ETFs, '=X' for currencies)."""
    if symbol.endswith("=X"):
        return "=X"
    dot = symbol.rfind(".")
    return symbol[dot:] if dot > 0 and symbol[dot:] in EXCHANGE_CLOSE else ""


def last_complete_day(symbol: str, now: datetime) -> date:
    """The latest day whose session at the symbol's exchange is over (plus CLOSE_MARGIN_MIN), as of `now`."""
    from zoneinfo import ZoneInfo

    ex = exchange_of(symbol)
    if ex == "=X":  # currencies trade around the clock: a day is complete once it is over in Lisbon
        return now.astimezone(ZoneInfo("Europe/Lisbon")).date() - timedelta(days=1)
    tz, hour, minute = EXCHANGE_CLOSE[ex]
    local = now.astimezone(ZoneInfo(tz))
    closed = local.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(minutes=CLOSE_MARGIN_MIN)
    return local.date() if local >= closed else local.date() - timedelta(days=1)


def complete_bars(hist: dict[str, pd.DataFrame], now: datetime, until: date | None = None) -> dict[str, pd.DataFrame]:
    """Each history without the session still under way at its exchange, and (with `until`) nothing after it."""
    out = {}
    for sym, df in hist.items():
        last = last_complete_day(sym, now)
        if until is not None:
            last = min(last, until)
        cut = df.loc[: pd.Timestamp(last) + pd.Timedelta(hours=23, minutes=59)]
        if len(cut):
            out[sym] = cut
    return out


EUROPE_PATH = Path(__file__).resolve().parents[1] / "data" / "universe" / "europe.csv"
EU_LIQUID_SHARE = 0.75  # European stocks in play: the most traded 75% of the list (60-day traded value in euros)
LIQUIDITY_DAYS = 60


def load_europe() -> list[dict]:
    """Large and liquid European companies (data/universe/europe.csv): symbol, name, GICS sector, country."""
    with open(EUROPE_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def is_us(symbol: str) -> bool:
    return exchange_of(symbol) == ""


def liquid_europe(values: dict[str, float], share: float = EU_LIQUID_SHARE) -> set[str]:
    """The most traded `share` of the European stocks, from their traded value in euros."""
    ranked = sorted((s for s, v in values.items() if v == v and v > 0), key=values.get, reverse=True)
    return set(ranked[: math.ceil(len(ranked) * share)])


def us_breadth(stocks: dict[str, dict]) -> float:
    """Share of S&P 500 stocks in a stage 2 uptrend: the market zone, as in the pre-training (US market only)."""
    us = [m["stage2"] for s, m in stocks.items() if is_us(s)]
    return sum(us) / max(len(us), 1)


def market_open(symbol: str, now: datetime) -> bool:
    """Whether the symbol's exchange is in session right now (started, and before its close)."""
    from zoneinfo import ZoneInfo

    ex = exchange_of(symbol)
    if ex == "=X":
        return True
    tz, hour, minute = EXCHANGE_CLOSE[ex]
    local = now.astimezone(ZoneInfo(tz))
    return session_started(symbol, now) and local < local.replace(hour=hour, minute=minute, second=0, microsecond=0)


def trading_now(mkt: Market, symbol: str, now: datetime) -> bool:
    """In session now and with a bar for today already in the data: on an exchange holiday, or while Yahoo has not
    yet published today's first prices (European quotes come with a delay), the latest price is yesterday's close,
    and trading at it would use a price the market no longer offers."""
    from zoneinfo import ZoneInfo

    if not market_open(symbol, now):
        return False
    df = mkt.hist.get(symbol)
    if df is None or not len(df):
        return False
    tz = EXCHANGE_CLOSE[exchange_of(symbol)][0]
    return df.index[-1].date() == now.astimezone(ZoneInfo(tz)).date()


def session_fraction(symbol: str, now: datetime) -> float:
    """How much of today's session at the symbol's exchange has gone by (0-1)."""
    from zoneinfo import ZoneInfo

    ex = exchange_of(symbol)
    tz, ch, cm = EXCHANGE_CLOSE[ex]
    oh, om = EXCHANGE_OPEN.get(ex, (9, 0))
    local = now.astimezone(ZoneInfo(tz))
    start = local.replace(hour=oh, minute=om, second=0, microsecond=0)
    end = local.replace(hour=ch, minute=cm, second=0, microsecond=0)
    return min(max((local - start) / (end - start), 0.0), 1.0)


LIVE_MIN_FRACTION = 0.1  # volume projection: never scale a session's volume by more than 10x


def live_bars(hist: dict[str, pd.DataFrame], now: datetime) -> dict[str, pd.DataFrame]:
    """Each history up to now, for the reactive rounds: today's bar is kept while its session is under way (its close
    is the latest price) with the volume projected to a full session, so volume rules compare like with like; a bar
    dated today before the exchange opens (pre-market noise) is dropped."""
    from zoneinfo import ZoneInfo

    out = {}
    for sym, df in hist.items():
        ex = exchange_of(sym)
        tz = "Europe/Lisbon" if ex == "=X" else EXCHANGE_CLOSE[ex][0]
        today = now.astimezone(ZoneInfo(tz)).date()
        cut = df.loc[: pd.Timestamp(today) + pd.Timedelta(hours=23, minutes=59)]
        if len(cut) and cut.index[-1].date() == today and ex != "=X":
            if not session_started(sym, now):
                cut = cut.iloc[:-1]
            elif market_open(sym, now):
                cut = cut.copy()
                cut["Volume"] = cut["Volume"].astype(float)  # Yahoo gives whole numbers; the projection is not
                cut.iloc[-1, cut.columns.get_loc("Volume")] = (
                    float(cut["Volume"].iloc[-1]) / max(session_fraction(sym, now), LIVE_MIN_FRACTION))
        if len(cut):
            out[sym] = cut
    return out


PRICE_CACHE = Path(__file__).resolve().parents[1] / "state" / "prices"  # kept between rounds by actions/cache


def session_live(symbol: str, now: datetime) -> bool:
    """Whether a round can see new prices of this symbol now: its exchange is open (or closed less than 45 minutes
    ago, for the final bar). Before New York opens, a US stock's last bar is yesterday's (in the day's cached year);
    after Europe closes, a European one's bar no longer moves. Currencies trade all day."""
    from zoneinfo import ZoneInfo

    if symbol.endswith("=X"):
        return True
    zone, opens, closes = (("America/New_York", (9, 25), (16, 45)) if is_us(symbol)
                           else ("Europe/Berlin", (8, 55), (18, 15)))
    t = now.astimezone(ZoneInfo(zone))
    return t.weekday() < 5 and opens <= (t.hour, t.minute) <= closes


def open_clock(symbol: str, day: date) -> str:
    """When the symbol's exchange opens that day, in Lisbon time ("14:30"): the time of a fill at the open."""
    from zoneinfo import ZoneInfo

    zone, opens = ("America/New_York", (9, 30)) if is_us(symbol) else ("Europe/Berlin", (9, 0))
    at = datetime(day.year, day.month, day.day, *opens, tzinfo=ZoneInfo(zone))
    return at.astimezone(ZoneInfo("Europe/Lisbon")).strftime("%H:%M")


def round_history(tickers: list[str], now: datetime) -> dict[str, pd.DataFrame]:
    """A year of daily bars for the rounds: the year is downloaded once a day and kept (PRICE_CACHE); each round
    then only fetches the last 5 days, which carry today's bar so far, and puts them on top."""
    import pickle
    from zoneinfo import ZoneInfo

    day = now.astimezone(ZoneInfo("Europe/Lisbon")).date().isoformat()
    import hashlib

    key = hashlib.sha1(",".join(sorted(tickers)).encode()).hexdigest()[:10]
    path = PRICE_CACHE / f"base-{day}-{key}.pkl"
    base = None
    if path.exists():
        try:
            base = pickle.loads(path.read_bytes())
        except Exception:  # noqa: BLE001 - a broken file is downloaded again
            base = None
    if base is None:
        base = download_history(tickers)
        # the year up to yesterday: today's bar comes fresh every round
        base = {t: df[df.index < pd.Timestamp(day)] for t, df in base.items()}
        PRICE_CACHE.mkdir(parents=True, exist_ok=True)
        for old in PRICE_CACHE.glob("base-*.pkl"):
            if not old.name.startswith(f"base-{day}-"):
                old.unlink()
        if len(base) >= 0.95 * len(tickers):  # a download with holes is used, not kept for the rest of the day
            path.write_bytes(pickle.dumps(base))
    live_now = [t for t in tickers if session_live(t, now)]  # only the markets in session: about half the download
    fresh = download_history(live_now, period="5d") if live_now else {}
    out = {}
    for t in tickers:
        b, f = base.get(t), fresh.get(t)
        if f is None or not len(f):
            if b is not None:  # no fresh prices this round: only the complete days
                out[t] = b[b.index < pd.Timestamp(day)]
            continue
        out[t] = f if b is None else pd.concat([b[b.index < f.index[0]], f])
    return out


STALE_SESSIONS = 3
FX_LAST_PATH = REPO_AGENTS / "fx_last.json"  # the last rate seen per currency, for a day Yahoo misses one


def with_last_rates(rates: dict[str, pd.Series]) -> dict[str, pd.Series]:
    """Every currency of FX_SYMBOLS: Yahoo's series, or (when Yahoo did not answer for one) the last rate seen, so a
    held position can still be valued. The latest rates are remembered for next time."""
    try:
        saved = json.loads(FX_LAST_PATH.read_text()) if FX_LAST_PATH.exists() else {}
    except ValueError:
        saved = {}
    out = dict(rates)
    for ccy in FX_SYMBOLS:
        if ccy == "USD":
            continue
        if ccy in rates and len(rates[ccy]):
            saved[ccy] = float(rates[ccy].iloc[-1])
        elif ccy in saved:
            print(f"WARN: sem câmbio EUR/{ccy} do Yahoo; uso o último conhecido ({saved[ccy]})", file=sys.stderr)
            out[ccy] = pd.Series([saved[ccy]], index=[pd.Timestamp("2000-01-01")])
    try:
        FX_LAST_PATH.parent.mkdir(parents=True, exist_ok=True)
        FX_LAST_PATH.write_text(json.dumps(saved, sort_keys=True))
    except OSError:
        pass
    return out


def load_market(now: datetime | None = None, live: bool = False) -> Market:
    """The market for a decision. Daily (live=False): complete sessions only, cut at the last full US session.
    Reactive rounds (live=True): up to this moment, today's sessions under way included (see live_bars)."""
    from zoneinfo import ZoneInfo

    now = now or datetime.now(timezone.utc)
    constituents = load_constituents()
    europe = load_europe()
    sector_of = {c["symbol"]: c["sector"] for c in constituents} | {c["symbol"]: c["sector"] for c in europe}
    industry_of = {c["symbol"]: c.get("industry") for c in constituents}
    name_of = {c["symbol"]: c["name"] for c in constituents} | {c["symbol"]: c["name"] for c in europe}
    history = (lambda tickers: round_history(tickers, now)) if live else download_history
    raw = history(list(sector_of) + ["SPY"])
    raw = {s: fix_pence(df) if currency_of(s) == "GBp" else df for s, df in raw.items()}
    if live:
        stock_hist = live_bars(raw, now)
        if "SPY" not in stock_hist:
            raise RuntimeError("Sem dados do Yahoo Finance (SPY).")
        as_of = now.astimezone(ZoneInfo("Europe/Lisbon")).date()
        etf_hist = live_bars(history([t for t, _ in UCITS_ETFS]), now)
        fx_hist = live_bars(history(list(FX_SYMBOLS.values())), now)
    else:
        stock_hist = complete_bars(raw, now)
        if "SPY" not in stock_hist:
            raise RuntimeError("Sem dados do Yahoo Finance (SPY).")
        # The decision day: the last full US session. Nothing later is seen, in any market (as in the backtest).
        as_of = stock_hist["SPY"].index[-1].date()
        stock_hist = complete_bars(stock_hist, now, as_of)
        etf_hist = complete_bars(download_history([t for t, _ in UCITS_ETFS]), now, as_of)
        fx_hist = complete_bars(download_history(list(FX_SYMBOLS.values())), now, as_of)
    if "EURUSD=X" not in fx_hist:
        raise RuntimeError("Sem dados do Yahoo Finance (EUR/USD).")
    set_liquidity(stock_hist)
    fx_rates = with_last_rates({c: fx_hist[t]["Close"] for c, t in FX_SYMBOLS.items() if c != "USD" and t in fx_hist})
    missing = [s for s in sector_of if s not in stock_hist]
    if missing:
        print(f"WARN: {len(missing)} ações sem dados do Yahoo: {', '.join(missing[:40])}", file=sys.stderr)
    # Europe: only stocks whose currency can be converted, and only the most traded part of the list.
    probe = Market(hist={}, fx=fx_hist["EURUSD=X"]["Close"], stocks={}, etfs={}, as_of=as_of, sessions=[],
                   zone="Neutra", stage2_share=0.0, fx_rates=fx_rates)
    # A stock with no bar in the last STALE_SESSIONS sessions (stopped trading, delisted) is not bought.
    recent = [d.date() for d in stock_hist["SPY"].index if d.date() <= as_of][-STALE_SESSIONS:]
    fresh = {sym for sym, df in stock_hist.items() if len(df) and df.index[-1].date() >= recent[0]}
    traded = {}
    for sym, df in stock_hist.items():
        if sym == "SPY" or is_us(sym) or sym not in fresh or fx_series(probe, currency_of(sym)) is None and not is_eur(sym):
            continue
        tail = df.iloc[-LIQUIDITY_DAYS:]
        value = tail["Close"] * tail["Volume"]
        if (value > 0).sum() < 20:  # as the backtest: at least 20 days with trading in the window
            continue
        traded[sym] = to_eur(probe, sym, float(value[value > 0].mean()), as_of)
    in_play = liquid_europe(traded)

    stocks = {}
    for sym, df in stock_hist.items():
        if sym not in sector_of or sym not in fresh or (not is_us(sym) and sym not in in_play):
            continue
        m = trend_metrics(df)
        if m:
            stocks[sym] = {**m, **extra_metrics(df), "sector": sector_of[sym], "industry": industry_of.get(sym), "name": name_of.get(sym, ""),
                            "stage2": is_stage2(m), "currency": currency_of(sym)}
            if not is_us(sym):  # trading volume in US dollars, like the US stocks (opportunities, safety score)
                usd = float(probe.fx.iloc[-1]) if len(probe.fx) else 1.0
                stocks[sym]["dollar_volume"] = to_eur(probe, sym, stocks[sym]["dollar_volume"], as_of) * usd
    add_trend_scores(stocks)
    try:  # fundamentals as of today (SEC filings in data/fundamentals, today's price)
        import fundamentals

        funds = fundamentals.load()
        today = as_of
        for sym, m in stocks.items():
            m.update(fundamentals.latest(funds.get(sym), m["price"], today))
    except Exception as e:  # missing fundamentals only switch those rules off
        print(f"WARN: fundamentos: {e!r}", file=sys.stderr)
    names = dict(UCITS_ETFS)
    etfs = {}
    for sym, df in etf_hist.items():
        m = trend_metrics(df)
        if m:
            etfs[sym] = {**m, "name": names.get(sym, sym)}
    add_trend_scores(etfs)

    share = us_breadth(stocks)
    spy = stock_hist["SPY"]
    market = Market(
        hist={**stock_hist, **etf_hist},
        fx=fx_hist["EURUSD=X"]["Close"],
        fx_rates=fx_rates,
        stocks=stocks,
        etfs=etfs,
        as_of=as_of,
        sessions=sorted({d.date() for d in spy.index}
                        | ({as_of} if live and any(len(df) and df.index[-1].date() == as_of for df in stock_hist.values())
                           else set())),
        zone=zone_for(share),
        stage2_share=share,
    )
    rankings = WATCHLIST_DIR / "rankings-latest.json"
    if rankings.exists():
        data = json.loads(rankings.read_text())
        market.rankings = [r for rows in data["stocks"].values() for r in rows]
    signals = WATCHLIST_DIR.parent / "news" / "signals.json"
    if signals.exists():
        market.news = json.loads(signals.read_text())
    pretrained = REPO_AGENTS / "pretrain.json"
    if pretrained.exists():
        market.priors = {s: r["by_zone"] for s, r in json.loads(pretrained.read_text())["strategies"].items()
                         if r.get("by_zone")}
    try:  # market context (VIX, policy uncertainty, credit, rates, dollar, S&P 500 trend) and the macro calendar
        import context
        import macro

        frame = context.download("2y", spy, fx_hist["EURUSD=X"]["Close"])
        market.context = context.on(frame, market.as_of)
        ahead = macro.events(market.as_of + timedelta(days=1), market.as_of + timedelta(days=3))[0]
        nxt = min((d for d, _ in ahead), default=None)
        if nxt:
            market.context["macro"] = [name for d, name in ahead if d == nxt]
    except Exception as e:  # the context only switches rules off when missing
        print(f"WARN: contexto de mercado: {e!r}", file=sys.stderr)
    geo = WATCHLIST_DIR.parent / "news" / "geo.json"  # Claude's geopolitical risk (news.py), live only
    if geo.exists():
        try:
            g = json.loads(geo.read_text())
            if datetime.now(timezone.utc) - datetime.fromisoformat(g["at"]) <= timedelta(hours=36):
                market.context["geo_risk"] = g["risk"]
        except (ValueError, KeyError) as e:
            print(f"WARN: geopolítica: {e!r}", file=sys.stderr)
    vcp = WATCHLIST_DIR / "vcp-latest.json"
    if vcp.exists():
        market.vcp = {
            r["symbol"]
            for r in json.loads(vcp.read_text()).get("results", [])
            if r.get("entry_ready") and r.get("rating") in ("Textbook VCP", "Strong VCP", "Good VCP")
        }
    return market


# Quote currency by exchange (Yahoo quotes London in pence, GBp); every other listed exchange is in euros.
CURRENCY_BY_EXCHANGE = {"": "USD", ".L": "GBp", ".SW": "CHF", ".ST": "SEK", ".CO": "DKK", ".OL": "NOK"}
# Yahoo series of each currency per euro (EURGBP=X: pounds per euro).
FX_SYMBOLS = {"USD": "EURUSD=X", "GBP": "EURGBP=X", "CHF": "EURCHF=X", "SEK": "EURSEK=X", "DKK": "EURDKK=X",
              "NOK": "EURNOK=X"}
CURRENCY_LABEL = {"USD": "USD", "GBp": "GBp", "CHF": "CHF", "SEK": "SEK", "DKK": "DKK", "NOK": "NOK", "EUR": "EUR"}
# Taxes on buying shares (stock exchange / financial transaction taxes), on the amount bought, for companies of
# that country: French FTT 0.4%, Italian FTT 0.1%, Spanish FTT 0.2%, UK stamp duty 0.5%, Irish stamp duty 1%.
# ETFs and companies registered elsewhere (BUY_TAX_EXEMPT) pay none.
BUY_TAX = {".PA": 0.004, ".MI": 0.001, ".MC": 0.002, ".L": 0.005, ".IR": 0.01}
BUY_TAX_EXEMPT = {"STMPA.PA", "AIR.PA", "ERF.PA", "RACE.MI", "STLAM.MI", "TEN.MI", "CPR.MI", "FER.MC", "GLEN.L",
                  "EXPN.L", "WPP.L", "IAG.L"}  # registered in NL, LU, Jersey or Spain


def currency_of(symbol: str) -> str:
    ex = exchange_of(symbol)
    return CURRENCY_BY_EXCHANGE.get(ex, "EUR")


def is_eur(symbol: str) -> bool:
    return currency_of(symbol) == "EUR"


def buy_tax(symbol: str) -> float:
    if symbol in BUY_TAX_EXEMPT or symbol in ETF_SYMBOLS:
        return 0.0
    return BUY_TAX.get(exchange_of(symbol), 0.0)


def fx_series(mkt: Market, currency: str) -> pd.Series | None:
    """Units of `currency` per euro over time (pence quotes use the pound)."""
    if currency == "GBp":
        currency = "GBP"
    return mkt.fx if currency == "USD" else mkt.fx_rates.get(currency)


# numpy copies of price and rate series, kept per object (the backtest asks the same ones millions of times; pandas
# label lookups were most of its time). Weak references: a series that is gone frees its copy.
_NP: dict[tuple, tuple] = {}


def _np_of(obj, build):
    key = (id(obj), build)
    hit = _NP.get(key)
    if hit is not None and hit[0]() is obj:
        return hit[1]
    if len(_NP) > 20000:
        for k in [k for k, v in _NP.items() if v[0]() is None]:
            del _NP[k]
    value = build(obj)
    _NP[key] = (weakref.ref(obj), value)
    return value


def _rate_arrays(series: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    clean = series.dropna()
    return clean.index.values.astype("datetime64[ns]").astype(np.int64), clean.to_numpy(dtype=float)


def to_eur(mkt: Market, symbol: str, price: float, on: date) -> float:
    ccy = currency_of(symbol)
    if ccy == "EUR":
        return price
    series = fx_series(mkt, ccy)
    if series is None or not len(series):
        raise KeyError(f"sem câmbio para {ccy} ({symbol})")
    when, rates = _np_of(series, _rate_arrays)  # = series.asof(on), the first rate before any
    i = np.searchsorted(when, np.datetime64(on, "ns").astype(np.int64), side="right") - 1
    rate = rates[i] if i >= 0 and len(rates) else float(series.iloc[0])
    eur = price / float(rate)
    return eur / 100 if ccy == "GBp" else eur


def fix_pence(df: pd.DataFrame) -> pd.DataFrame:
    """London quotes from Yahoo sometimes switch a value between pence and pounds (100x jumps), a whole day or only
    its high or low: put each one back in pence (checked against the median close around it)."""
    ref = df["Close"].rolling(11, center=True, min_periods=3).median()
    out = None
    for col in ("Open", "High", "Low", "Close"):
        ratio = df[col] / ref
        low, high = ratio < 0.02, ratio > 50
        if low.any() or high.any():
            out = df.copy() if out is None else out
            out.loc[low, col] = df.loc[low, col] * 100
            out.loc[high, col] = df.loc[high, col] / 100
    return df if out is None else out


def bars_between(df: pd.DataFrame, after: date, until: date) -> pd.DataFrame:
    """Rows dated after `after` up to and including `until` (index sorted by date)."""
    return df.loc[pd.Timestamp(after) + pd.Timedelta(days=1): pd.Timestamp(until) + pd.Timedelta(hours=23)]


def _bar_arrays(df: pd.DataFrame) -> tuple:
    days = df.index.values.astype("datetime64[D]")
    cols = tuple(df[c].to_numpy(dtype=float) for c in ("Open", "High", "Low", "Close"))
    return (days, *cols)


def _close_arrays(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    return _rate_arrays(df["Close"])


def bars_after(df: pd.DataFrame, after: date, until: date):
    """(day, open, high, low, close) of the rows dated after `after` up to `until`, as bars_between, without pandas."""
    days, o, h, l, c = _np_of(df, _bar_arrays)
    i0 = np.searchsorted(days, np.datetime64(after, "D"), side="right")
    i1 = np.searchsorted(days, np.datetime64(until, "D"), side="right")
    for i in range(i0, i1):
        yield days[i].astype(date), o[i], h[i], l[i], c[i]


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


# An agent beating the Indexador live stays in the league whatever its backtest (Tiago: never take out an agent that is
# winning against the S&P 500), once its record is three months long (a few days of winning is noise).
LIVE_PROTECT_SESSIONS = 63


def live_vs_index(cfg: dict, state: dict) -> tuple[float, float, int] | None:
    """(agent's return, the Indexador's over the same days, sessions) from the live records, or None."""
    port = state["agents"].get(cfg["id"]) or {}
    idx = state["agents"].get("indexador") or {}
    hist, ih = port.get("history") or [], {h["date"]: h for h in idx.get("history") or []}
    if len(hist) < 2 or not ih:
        return None
    first = next((h for h in hist if h["date"] in ih), None)
    if not first:
        return None
    last = hist[-1]
    i0, i1 = ih[first["date"]], ih.get(last["date"]) or idx["history"][-1]
    agent = (last["equity"] - (last["deposits"] - first["deposits"])) / first["equity"] - 1 if first["equity"] else 0.0
    index = (i1["equity"] - (i1["deposits"] - i0["deposits"])) / i0["equity"] - 1 if i0["equity"] else 0.0
    return agent, index, len(hist)


def beats_index_live(cfg: dict, state: dict) -> bool:
    lv = live_vs_index(cfg, state)
    return bool(lv) and lv[2] >= LIVE_PROTECT_SESSIONS and lv[0] > lv[1]


def retired(profile: str) -> set[str]:
    """Strategies switched off for this profile: their agent of the profile spent a year on the bench (scripts/jury.py)."""
    config = load_config()
    gone = {c["strategy"] for c in config["agents"] if c.get("desligado") and profile_of(c) == profile and mode_of(c) == REACTIVE}
    back = {c["strategy"] for c in config["agents"] if c.get("active", True) and profile_of(c) == profile}
    return gone - back


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


def tax_owed_if_sold(port: dict, mkt: Market) -> float:
    """The tax still to pay if everything were sold now (to compare with buying the index and holding it, whose tax
    is deferred until it is sold): TAX_RATE on this year's realised gains plus the open positions' gains (a year's
    losses offset that year's gains), plus last year's gains when their tax has not been paid yet."""
    this_year = str(mkt.as_of.year)
    realized = port.get("realized", {})
    taxed = port.get("taxed", [])
    open_gain = sum(position_value(mkt, s, p) - p.get("cost_eur", 0.0) for s, p in port["positions"].items())
    owed = max(0.0, realized.get(this_year, 0.0) + open_gain)
    owed += sum(max(0.0, g) for y, g in realized.items() if y < this_year and y not in taxed)  # IRS not yet due
    return owed * TAX_RATE


# The broker's spread is that of a liquid stock: a thinly traded one costs more to get in and out of (wider spread,
# the price moves when the order fills). Extra per side by the median value traded per day (euros, last year);
# ETFs are kept tight by their market makers. Set when the prices load (set_liquidity), read by both engines.
LIQUIDITY_TIERS = ((100e6, 0.0), (20e6, 0.0005), (5e6, 0.0015), (0.0, 0.003))
TO_EUR_APPROX = {"EUR": 1.0, "USD": 0.92, "GBp": 0.0117, "CHF": 1.05, "SEK": 0.087, "DKK": 0.134, "NOK": 0.085}
LIQUIDITY_EXTRA: dict[str, float] = {}


def set_liquidity(hist: dict) -> None:
    """Each stock's extra spread from its median traded value over the last year of `hist` (symbol: daily bars)."""
    for s, df in hist.items():
        if df is None or not len(df) or s in ETF_SYMBOLS or "Volume" not in df:
            continue
        tail = df.iloc[-252:]
        value = float((tail["Close"] * tail["Volume"]).median()) * TO_EUR_APPROX.get(currency_of(s), 1.0)
        if value == value:  # not NaN
            LIQUIDITY_EXTRA[s] = next(extra for floor, extra in LIQUIDITY_TIERS if value >= floor)


def fees(symbol: str) -> float:
    """Spread/slippage per side (wider for thinly traded stocks), plus currency conversion for anything not in euros."""
    return FEE + LIQUIDITY_EXTRA.get(symbol, 0.0) + (0.0 if is_eur(symbol) else FX_FEE)


def commission_terms(symbol: str) -> dict:
    """The active broker's commission for this listing: {} (none), Europe's or the US's terms."""
    b = BROKERS[BROKER]
    return (b.get("us") if is_us(symbol) else b.get("eu")) or {}


def commission(mkt: Market, symbol: str, value_eur: float, qty: float, on: date) -> float:
    """The broker's commission of one order, in euros."""
    c = commission_terms(symbol)
    if not c:
        return 0.0
    if "por_acao" in c:
        return min(max(to_eur(mkt, symbol, c["min"], on), to_eur(mkt, symbol, c["por_acao"], on) * qty),
                   c["max_pct"] * value_eur)
    return min(max(c["pct"] * value_eur, c["min_eur"]), c["max_eur"])


def sell(port: dict, mkt: Market, symbol: str, price: float, on: date, reason: str, fraction: float = 1.0,
         ctx: dict | None = None) -> dict:
    pos = port["positions"][symbol]
    qty = pos["qty"] * fraction
    fill = price * (1 - fees(symbol))
    gross = qty * to_eur(mkt, symbol, fill, on)
    proceeds = gross - commission(mkt, symbol, gross, qty, on)
    cost = pos["cost_eur"] * fraction
    port["cash"] += proceeds
    realized = port.setdefault("realized", {})
    realized[str(on.year)] = round(realized.get(str(on.year), 0.0) + proceeds - cost, 2)
    if fraction >= 0.999:
        del port["positions"][symbol]
    else:
        pos["qty"] -= qty
        pos["cost_eur"] -= cost
    held = (on - date.fromisoformat(pos["entry_date"])).days if pos.get("entry_date") else None
    trade = {"date": on.isoformat(), "side": "venda", "symbol": symbol, "qty": round(qty, 6),
             "price": round(fill, 4), "eur": round(proceeds, 2), "pnl_eur": round(proceeds - cost, 2), "reason": reason,
             "ret": round(proceeds / cost - 1, 4) if cost else 0.0, "held": held}
    if ctx:
        trade["ctx"] = ctx
    port["trades"] = (port["trades"] + [trade])[-TRADES_KEPT:]
    return trade


def buy(port: dict, mkt: Market, order: dict, price: float, on: date) -> dict | None:
    eur = min(order["eur"], port["cash"])
    if eur < MIN_ORDER_EUR:
        return None
    symbol = order["symbol"]
    fill = price * (1 + fees(symbol) + buy_tax(symbol))
    unit = to_eur(mkt, symbol, fill, on)
    qty = (eur - commission(mkt, symbol, eur, eur / unit, on)) / unit
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
    if order.get("ctx"):
        trade["ctx"] = order["ctx"]
    port["trades"] = (port["trades"] + [trade])[-TRADES_KEPT:]
    return trade


PENDING_MAX_SESSIONS = 5  # an order that found no price in this many sessions (stale or delisted stock) expires


def cancel(port: dict, entry: dict) -> None:
    port["cancelled"] = (port.get("cancelled", []) + [entry])[-50:]


def cancel_text(c: dict) -> str:
    gap = f" {c['gap']:+.1%}" if "gap" in c else ""
    return f"{c['symbol']} ({c.get('reason', 'gap na abertura')}{gap})"


def fill_pending(port: dict, mkt: Market, partial_day: date | None = None) -> list[dict]:
    """Execute the previous run's orders at the open of the next session.

    partial_day: a session still under way (the intraday rounds): an order fills there only at a real open, never at
    the price of the moment. An order whose stock has no data stays pending; after PENDING_MAX_SESSIONS it expires.
    """
    done, still = [], []
    # sells first, so their cash is available to the buys
    for order in sorted(port["pending"], key=lambda o: o["side"] != "sell"):
        df = mkt.hist.get(order["symbol"])
        after = () if df is None else bars_after(df, date.fromisoformat(order["created"]), mkt.as_of)
        on, price = None, None
        for day, b_open, _, _, b_close in after:  # the first session with a valid price
            values = (b_open,) if day == partial_day else (b_open, b_close)  # broken bars: the close, on complete days
            price = next((float(v) for v in values if pd.notna(v) and float(v) > 0), None)
            if price is not None:
                on = day
                break
        if price is None:
            if sessions_since(mkt, order["created"]) > PENDING_MAX_SESSIONS:
                cancel(port, {"date": mkt.as_of.isoformat(), "symbol": order["symbol"], "side": order["side"],
                              "reason": "sem preço há mais de 5 sessões"})
            else:
                still.append(order)
            continue
        if order["side"] == "sell":
            if order["symbol"] in port["positions"]:
                done.append(sell(port, mkt, order["symbol"], price, on, order.get("reason", ""),
                                 order.get("fraction", 1.0), order.get("ctx")))
        else:
            ref, limit = order.get("ref"), order.get("gap_max")
            if ref and limit and abs(price / ref - 1) > limit:  # opened too far from the decision's close: cancel
                cancel(port, {"date": on.isoformat(), "symbol": order["symbol"], "gap": round(price / ref - 1, 4),
                              "limit": limit, "reason": "gap na abertura"})
                continue
            t = buy(port, mkt, order, price, on)
            if t:
                done.append(t)
    port["pending"] = still
    return done


def check_exits(port: dict, mkt: Market, open_day: date | None = None) -> list[dict]:
    """Stops and targets, bar by bar since each position was last checked.

    open_day: a session still under way (the intraday rounds). Its low so far can trigger the stop, at the price
    the daily check would use; targets wait for the full day (the backtest checks the stop first when both are hit
    the same day). The day is not marked as checked: the full day is checked again by the daily run.
    """
    done = []
    for symbol, pos in list(port["positions"].items()):
        df = mkt.hist.get(symbol)
        if df is None:
            continue
        for on, b_open, b_high, b_low, _ in bars_after(df, date.fromisoformat(pos["checked"]), mkt.as_of):
            if pos.get("stop") and b_low <= pos["stop"]:
                done.append(sell(port, mkt, symbol, min(float(b_open), pos["stop"]), on, "stop",
                                 ctx={"stop": round(pos["stop"], 4), "entry": round(pos["entry"], 4), "low": round(float(b_low), 4)}))
                break
            if pos.get("tp") and b_high >= pos["tp"] and on != open_day:
                done.append(sell(port, mkt, symbol, max(float(b_open), pos["tp"]), on, "alvo",
                                 ctx={"tp": round(pos["tp"], 4), "entry": round(pos["entry"], 4), "high": round(float(b_high), 4)}))
                break
            if on != open_day:
                pos["checked"] = on.isoformat()
    return done


def pay_taxes(port: dict, mkt: Market) -> dict | None:
    """Pay TAX_RATE on each year's net realised gains (losses pay nothing) once that year's IRS is due (TAX_DUE of the
    next year). Returns the last tax trade, or None."""
    trade = None
    taxed = port.setdefault("taxed", [])
    for year, gain in sorted(port.get("realized", {}).items()):
        if gain <= 0 or year in taxed or mkt.as_of < date(int(year) + 1, *TAX_DUE):
            continue
        tax = round(gain * TAX_RATE, 2)
        port["cash"] -= tax
        taxed.append(year)
        trade = {"date": mkt.as_of.isoformat(), "side": "imposto", "symbol": "-", "qty": 0, "price": 0,
                 "eur": tax, "pnl_eur": -tax, "reason": f"{TAX_RATE:.0%} sobre {gain:.0f} € de mais-valias de {year} (IRS)"}
        port["trades"] = (port["trades"] + [trade])[-TRADES_KEPT:]
    return trade


def benchmark_return(mkt: Market, since: date, until: date) -> float:
    df = mkt.hist.get(BENCHMARK)
    if df is None:
        return 0.0
    when, closes = _np_of(df, _close_arrays)
    def at(d: date):  # = close.asof(the end of that day)
        i = np.searchsorted(when, (np.datetime64(d, "ns") + np.timedelta64(23, "h")).astype(np.int64), side="right") - 1
        return closes[i] if i >= 0 else np.nan
    a, b = at(since), at(until)
    return 0.0 if pd.isna(a) or pd.isna(b) or not a else float(b / a - 1)


def drawdown(port: dict, eq: float) -> float:
    """How far the agent is below its best level (equity per euro given)."""
    ratio = eq / port["deposits"] if port["deposits"] else 1.0
    return max(0.0, 1 - ratio / port.get("peak", 1.0))


def score_day(port: dict, mkt: Market) -> int | None:
    """Append today's equity and award points; None if already done for this date."""
    eq = equity(port, mkt)
    hist = port["history"]
    if hist and hist[-1]["date"] >= mkt.as_of.isoformat():
        hist[-1].update(equity=round(eq, 2), deposits=port["deposits"])
        return None
    ratio = eq / port["deposits"] if port["deposits"] else 1.0
    port["peak"] = max(port.get("peak", 1.0), ratio)
    dd = drawdown(port, eq)
    port["max_dd"] = round(max(port.get("max_dd", 0.0), dd), 4)
    pts = 0
    if hist:
        prev = hist[-1]
        day = (eq - (port["deposits"] - prev["deposits"])) / prev["equity"] - 1 if prev["equity"] else 0.0
        excess = day - benchmark_return(mkt, date.fromisoformat(prev["date"]), mkt.as_of)
        zone = port.setdefault("zone_stats", {}).setdefault(mkt.zone, [0.0, 0])  # [sum of excess, days], for the Maestro
        zone[0], zone[1] = round(zone[0] + excess, 6), zone[1] + 1
        pts = (max(-10, min(10, round(excess * 1000)))
               + (PROFIT_BONUS if eq > port["deposits"] else 0)
               - round(max(0.0, dd - DD_FREE) * DD_PENALTY))
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


def mutate(params: dict, rng: random.Random, strategy: str | None = None) -> list[str]:
    """Change each tunable parameter by up to +/-MUTATION within its bounds, in place; describe the changes.

    The shared rules (COMMON) are tuned too: one that is off switches on with a 25% chance (at a random value
    within its bounds), one that is on switches off with a 10% chance.
    """
    changes = []
    if strategy and strategy != "indexador":
        for k, v in COMMON.items():
            if uses_param(strategy, k):
                params.setdefault(k, v)
    for k in [k for k in params if k in PARAM_BOUNDS and uses_param(strategy, k)]:
        lo, hi, kind = bounds_of(strategy, k)
        if k in OFFABLE and not params[k]:
            if rng.random() < 0.25:
                new = rng.uniform(lo, hi)
                new = int(round(new)) if kind is int else round(new, 4)
                changes.append(f"{k} ligado: {new}")
                params[k] = new
            continue
        if k in OFFABLE and rng.random() < 0.10:
            changes.append(f"{k} desligado")
            params[k] = 0
            continue
        new = min(hi, max(lo, params[k] * rng.uniform(1 - MUTATION, 1 + MUTATION)))
        new = int(round(new)) if kind is int else round(new, 4)
        if new != params[k]:
            changes.append(f"{k} {params[k]}→{new}")
            params[k] = new
    return changes


def train(cfg: dict, port: dict, mkt: Market) -> str | None:
    """First run of a month: a live-only agent that lost money last month mutates its parameters.

    The other strategies are re-trained by pretrain.py every Sunday.
    """
    hist = port["history"]
    if len(hist) < 2 or hist[-1]["date"][:7] == hist[-2]["date"][:7]:
        return None
    last_month = hist[-2]["date"][:7]
    ret = month_return(port, last_month)
    params = port.setdefault("params", dict(cfg["params"]))
    tunable = [k for k in params if k in PARAM_BOUNDS]
    if ret is None or ret >= 0 or not tunable or cfg.get("strategy") not in LIVE_ONLY:
        return None
    changes = mutate(params, random.Random(f"{cfg['id']}-{mkt.as_of}"), cfg["strategy"])
    port["generation"] += 1
    note = f"{last_month}: {ret * 100:+.1f}% → geração {port['generation']}: " + (", ".join(changes) or "sem alterações")
    port["evolution"] = (port["evolution"] + [{"date": mkt.as_of.isoformat(), "note": note}])[-EVOLUTION_KEPT:]
    return note


# --------------------------------------------------------------------------
# Strategies: (cfg, port, mkt, params) -> orders for the next open
# --------------------------------------------------------------------------


def sessions_since(mkt: Market, since: str | None) -> int:
    if not since:
        return 10**6
    from bisect import bisect_right

    return len(mkt.sessions) - bisect_right(mkt.sessions, date.fromisoformat(since))  # sessions are sorted


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


# The strategies themselves live in the private repo (agents/receita/estrategias.py, run in this namespace): this public
# repo keeps only the machinery. Without that checkout only the Indexador exists, and RECEITA says so.
RECEITA_DIR = REPO_AGENTS / "receita"
_RECIPE = RECEITA_DIR / "estrategias.py"
RECEITA = _RECIPE.is_file()
STRATEGIES = {"indexador": (s_indexador, "Compra e mantém o S&P 500 (referência)", {})}
if RECEITA:
    exec(compile(_RECIPE.read_text(encoding="utf-8"), str(_RECIPE), "exec"), globals())  # noqa: S102 - our own private file


def recipe_prompt(name: str) -> str:
    """A Claude prompt from the private recipe (agents/receita/prompts/<name>.md); empty without it (no Claude call)."""
    try:
        return (RECEITA_DIR / "prompts" / f"{name}.md").read_text(encoding="utf-8")
    except OSError:
        return ""


# --------------------------------------------------------------------------
# Config and state
# --------------------------------------------------------------------------


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def _configured_broker() -> str | None:
    try:
        return json.loads(CONFIG_PATH.read_text()).get("corretora")
    except (OSError, ValueError):
        return None


set_broker(os.environ.get("OGJ_CORRETORA") or _configured_broker())


def real_capital() -> float:
    """The money of the real account that will follow the league (config capital_real; default an agent's 1000 EUR)."""
    try:
        return float(json.loads(CONFIG_PATH.read_text()).get("capital_real") or 1000.0)
    except (OSError, ValueError, TypeError):
        return 1000.0


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


def skip_earnings(orders: list[dict], mkt: Market, days: int = EARNINGS_AVOID_DAYS) -> tuple[list[dict], list[str]]:
    """Drop buys of stocks that report within `days` (EARNINGS_AVOID_DAYS by default); return (kept, skipped)."""
    kept, skipped = [], []
    for o in orders:
        if o["side"] == "buy" and o["symbol"] not in ETF_SYMBOLS and o["symbol"] not in mkt.etfs:  # ETFs report no results
            if o["symbol"] not in mkt.earnings:
                mkt.earnings[o["symbol"]] = (earnings.next_from_history(mkt.earnings_dates.get(o["symbol"]), mkt.as_of)
                                             if mkt.earnings_dates is not None else earnings.next_earnings(o["symbol"], mkt.as_of))
            if days and earnings.within(mkt.earnings[o["symbol"]], days, mkt.as_of):
                skipped.append(o["symbol"])
                continue
        kept.append(o)
    return kept, skipped


def skip_bad_news(orders: list[dict], mkt: Market, veto: int = NEWS_VETO) -> tuple[list[dict], list[str]]:
    """Drop buys of stocks with clearly negative news in the last 36 h; return (kept, skipped symbols)."""
    kept, skipped = [], []
    for o in orders:
        if o["side"] == "buy" and news_score(mkt, o["symbol"]) <= -veto:
            skipped.append(o["symbol"])
        else:
            kept.append(o)
    return kept, skipped


def brake(port: dict, mkt: Market, eq: float) -> bool:
    """Whether new buys are paused: a DD_BRAKE fall from the last high pauses them for BRAKE_DAYS.

    The brake has its own high-water mark, reset when a pause ends, so an agent left in cash by its stops can
    trade again instead of staying paused forever.
    """
    if port.get("brake_until") and mkt.as_of.isoformat() <= port["brake_until"]:
        return True
    ratio = eq / port["deposits"] if port["deposits"] else 1.0
    if port.get("brake_until"):  # a pause just ended: measure falls from here
        port["brake_until"], port["brake_peak"] = None, ratio
    port["brake_peak"] = max(port.get("brake_peak", 1.0), ratio)
    if 1 - ratio / port["brake_peak"] >= DD_BRAKE:
        port["brake_until"] = (mkt.as_of + timedelta(days=BRAKE_DAYS)).isoformat()
        return True
    return False


def risk_filter(orders: list[dict], port: dict, mkt: Market) -> tuple[list[dict], list[str]]:
    """Volatility sizing, sector cap and drawdown brake for buy orders; returns (orders, skipped notes)."""
    eq = equity(port, mkt)
    braking = brake(port, mkt, eq)
    sector_value: dict[str, float] = {}
    for s, pos in port["positions"].items():
        m = mkt.stocks.get(s)
        if m:
            sector_value[m["sector"]] = sector_value.get(m["sector"], 0.0) + position_value(mkt, s, pos)
    kept, notes = [], []
    for o in orders:
        if o["side"] != "buy":
            kept.append(o)
            continue
        if braking:
            notes.append(f"{o['symbol']} (travão de risco até {port['brake_until']})")
            continue
        m = mkt.stocks.get(o["symbol"])
        if m:
            if m.get("vol20"):
                o = {**o, "eur": round(o["eur"] * max(0.5, min(1.0, TARGET_VOL / m["vol20"])), 2)}
            if sector_value.get(m["sector"], 0.0) + o["eur"] > SECTOR_CAP * eq and m["sector"] != "Unknown":
                notes.append(f"{o['symbol']} (setor {SECTORS_PT.get(m['sector'], m['sector'])} já com {SECTOR_CAP:.0%})")
                continue
            sector_value[m["sector"]] = sector_value.get(m["sector"], 0.0) + o["eur"]
        kept.append(o)
    return kept, notes


def effective(params: dict, zone: str, strategy: str | None = None) -> dict:
    """The parameters in force today: the shared defaults, the agent's own, then its overrides for this market zone."""
    base = {} if strategy == "indexador" else dict(COMMON)
    return {**base, **{k: v for k, v in params.items() if k != "por_zona"}, **(params.get("por_zona") or {}).get(zone, {})}


def tech_score(m: dict) -> float:
    """0-60: the technical half of the safety score (trend 25, volatility 20, distance to the high 15)."""
    import opportunities

    trend = (10 if m["price"] > m["sma50"] else 0) + (10 if m["sma50"] > m["sma200"] else 0) + (5 if m["sma200_rising"] else 0)
    vol = m.get("vol20")
    return (trend + (opportunities.lerp(vol, 0.015, 0.04, 20) if vol is not None else 10)
            + opportunities.lerp(m["pct_from_high"], 0.05, 0.30, 15))


SUPPORT_MAX_RISK = 0.15  # stop_suporte only when the support is at most 15% below


def sector_state(mkt: Market, sector: str | None, symbol: str | None = None) -> tuple[float | None, float | None]:
    """(sector ETF vs its 200-day average, sector 3-month return minus its index's) from the context, or Nones: a
    European stock (symbol) reads its STOXX Europe 600 sector against the STOXX Europe 600."""
    import context

    key = context.sector_key(sector or "", us=is_us(symbol) if symbol else True)
    if not key:
        return None, None
    return mkt.context.get(f"{key}_trend"), mkt.context.get(f"{key}_rs")


def agenda_level(mkt: Market, sector: str | None, symbol: str | None = None) -> float | None:
    """How the political agenda treats the stock's sector (policy.py, its region's signals): the mean of public
    spending's surprise, the low exposure to policy uncertainty and minus the investment growth (z-scores across
    sectors, 0 = average), or None without any of them."""
    import context

    key = context.sector_key(sector or "", us=is_us(symbol) if symbol else True)
    if not key:
        return None
    gov, epub, capex = (mkt.context.get(f"{key}_{x}") for x in ("gov", "epub", "capex"))
    known = [x for x in (gov, epub, None if capex is None else -capex) if x is not None]
    return sum(known) / len(known) if known else None


def stock_filter(m: dict, mkt: Market, p: dict, symbol: str | None = None) -> str | None:
    """Why a stock fails the sector, moving-average and fundamental rules, or None. Missing data never blocks."""
    if p.get("filtro_setor") and m.get("sector"):
        trend, rs = sector_state(mkt, m["sector"], symbol)
        sector = SECTORS_PT.get(m["sector"], m["sector"])
        if trend is not None and trend < 0:
            return f"setor {sector} abaixo da média de 200 dias"
        if p["filtro_setor"] >= 2 and rs is not None and rs < 0:
            index = "S&P 500" if symbol is None or is_us(symbol) else "STOXX Europe 600"
            return f"setor {sector} mais fraco do que o {index} em 3 meses"
    if p.get("filtro_agenda") and m.get("sector"):
        level = agenda_level(mkt, m["sector"], symbol)
        if level is not None and level < -p["filtro_agenda"]:
            return f"setor {SECTORS_PT.get(m['sector'], m['sector'])} fora da agenda política ({level:+.1f})"
    if p.get("ema_filtro") and m.get("ema21") and m.get("ema50") and not (m["price"] > m["ema21"] > m["ema50"]):
        return "abaixo das médias exponenciais de 21 e 50 dias"
    if p.get("qualidade_fund_min") and m.get("fund_q") is not None and m["fund_q"] < p["qualidade_fund_min"]:
        return f"fundamentos fracos: qualidade {m['fund_q']:.0f} < {p['qualidade_fund_min']:.0f}"
    if p.get("valor_fund_min") and m.get("fund_v") is not None and m["fund_v"] < p["valor_fund_min"]:
        return f"cara: valor {m['fund_v']:.0f} < {p['valor_fund_min']:.0f}"
    return None


def sector_table(mkt: Market) -> list[dict]:
    """Each S&P 500 sector's health for the portal: ETF vs its 200-day average, 3-month strength vs the index,
    and the share of its stocks in Stage 2."""
    import context

    rows = []
    for sector in context.SECTOR_ETFS:
        trend, rs = sector_state(mkt, sector)
        members = [m for m in mkt.stocks.values() if m.get("sector") == sector]
        rows.append({"sector": SECTORS_PT.get(sector, sector), "etf": context.SECTOR_ETFS[sector][0],
                     "trend": None if trend is None else round(trend * 100, 1), "rs": None if rs is None else round(rs * 100, 1),
                     "stage2": round(sum(m["stage2"] for m in members) / len(members) * 100) if members else None})
    return sorted(rows, key=lambda r: (r["rs"] is None, -(r["rs"] or 0)))


def fundamentals_table(mkt: Market) -> dict:
    """{symbol: SEC fundamentals as of today} for the portal and Claude's portfolio analysis."""
    keys = ("fund_q", "fund_v", "mos", "pe", "fcf_yield", "earn_yield", "rev_growth", "op_margin", "current_ratio")
    return {s: {k: round(m[k], 3) for k in keys if m.get(k) is not None} for s, m in mkt.stocks.items() if m.get("fund_q") is not None}


def context_gate(mkt: Market, p: dict) -> str | None:
    """Why no new buys today, from the market context and the calendar, or None."""
    c = mkt.context
    if p.get("vix_max") and c.get("vix", 0) > p["vix_max"]:
        return f"VIX {c['vix']:.0f} acima de {p['vix_max']:.0f}"
    if p.get("epu_max") and c.get("epu_pct", 0) > p["epu_max"]:
        return f"incerteza política no percentil {c['epu_pct']:.0%}"
    if p.get("credito_max") and c.get("credit_dd", 0) > p["credito_max"]:
        return f"crédito de alto risco {c['credit_dd']:.0%} abaixo do melhor do ano face ao Tesouro"
    if p.get("juros_max") and c.get("rate_chg", 0) > p["juros_max"]:
        return f"juro a 10 anos subiu {c['rate_chg']:.2f} p.p. em 3 meses"
    if p.get("filtro_spy") and c.get("spy_trend", 0) < 0:
        return "S&P 500 abaixo da média de 200 dias"
    if p.get("gpr_max") and c.get("gpr_pct", 0) > p["gpr_max"]:
        return f"risco geopolítico no percentil {c['gpr_pct']:.0%} (guerras e tensões nas notícias)"
    if p.get("petroleo_max") and c.get("oil_chg", 0) > p["petroleo_max"]:
        return f"choque do petróleo: +{c['oil_chg']:.0%} em 3 meses"
    if p.get("ouro_max") and c.get("gold_chg", 0) > p["ouro_max"]:
        return f"fuga para o ouro: +{c['gold_chg']:.0%} em 3 meses"
    if p.get("evita_geo") and (c.get("geo_risk") or 0) >= GEO_RISK_HIGH:
        return f"risco geopolítico alto segundo o Claude ({c['geo_risk']}/5)"
    if p.get("evita_macro") and c.get("macro"):
        return "evento macro: " + ", ".join(c["macro"][:2])
    return None


def shared_rules(orders: list[dict], port: dict, mkt: Market, p: dict) -> tuple[list[dict], list[str]]:
    """The tunable rules every strategy shares: entry filters, ATR stop and the time stop."""
    kept, notes = [], []
    gate = context_gate(mkt, p)
    for o in orders:
        if o["side"] != "buy":
            kept.append(o)
            continue
        if gate:
            notes.append(f"{o['symbol']} ({gate})")
            continue
        c = mkt.context
        if p.get("usd_max") and currency_of(o["symbol"]) == "USD" and c.get("usd_chg", 0) > p["usd_max"]:
            notes.append(f"{o['symbol']} (dólar caiu {c['usd_chg']:.1%} em 3 meses)")
            continue
        if p.get("vix_meio") and c.get("vix", 0) > p["vix_meio"]:
            o = {**o, "eur": round(o["eur"] * 0.5, 2)}
        m = mkt.stocks.get(o["symbol"]) or mkt.etfs.get(o["symbol"])
        if m:
            if p.get("max_ext") and m["price"] > m["sma50"] * (1 + p["max_ext"]):
                notes.append(f"{o['symbol']} (esticada: {m['price'] / m['sma50'] - 1:.0%} acima da média de 50 dias)")
                continue
            if p.get("min_tecnico") and tech_score(m) < p["min_tecnico"]:
                notes.append(f"{o['symbol']} (score técnico {tech_score(m):.0f} < {p['min_tecnico']:.0f})")
                continue
            why = stock_filter(m, mkt, p, o["symbol"])
            if why:
                notes.append(f"{o['symbol']} ({why})")
                continue
            if p.get("stop_atr") and m.get("atr14"):
                atr_stop = round(m["price"] - p["stop_atr"] * m["atr14"], 4)
                if atr_stop > 0 and (not o.get("stop") or atr_stop > o["stop"]):
                    o = {**o, "stop": atr_stop}
            if p.get("stop_suporte") and m.get("support") and m.get("atr14"):
                level = round(m["support"] - p["stop_suporte"] * m["atr14"], 4)
                if 0 < level < m["price"] and (m["price"] - level) / m["price"] <= SUPPORT_MAX_RISK and (not o.get("stop") or level > o["stop"]):
                    o = {**o, "stop": level}
        kept.append(o)
    if p.get("tempo_max"):
        selling = {o["symbol"] for o in kept if o["side"] == "sell"}
        for s, pos in port["positions"].items():
            if s in selling or not pos.get("entry_date"):
                continue
            price = last_close(mkt, s)
            if price and sessions_since(mkt, pos["entry_date"]) >= p["tempo_max"] and price < pos["entry"]:
                kept.append(order_sell(s, f"não andou em {p['tempo_max']} sessões"))
    return kept, notes


def trail_stops(port: dict, mkt: Market, p: dict) -> None:
    """Raise each position's stop to the close minus trail_atr x ATR (never lower it)."""
    k = p.get("trail_atr")
    if not k:
        return
    for s, pos in port["positions"].items():
        m = mkt.stocks.get(s) or mkt.etfs.get(s)
        if m and m.get("atr14"):
            level = round(m["price"] - k * m["atr14"], 4)
            if level > (pos.get("stop") or 0):
                pos["stop"] = level


def decide(cfg: dict, port: dict, mkt: Market, params: dict, live: bool = True) -> tuple[list[dict], list[str]]:
    """The strategy's orders after the shared rules: earnings, bad news, risk. Same live and in backtests.

    The earnings rule runs live, and in a backtest whose markets carry the past earnings dates (earnings_dates).
    """
    params = effective(params, mkt.zone, cfg["strategy"])
    rebalanced_before = port.get("last_rebalance")
    region = int(params.get("regiao") or 0) if uses_param(cfg["strategy"], "regiao") else 0
    orders = STRATEGIES[cfg["strategy"]][0](cfg, port, in_region(mkt, region, port["positions"]), params)
    notes = []
    if cfg["strategy"] != "indexador":
        orders, skipped = shared_rules(orders, port, mkt, params)
        notes += skipped
    if (live or mkt.earnings_dates is not None) and not params.get("ignora_resultados"):
        orders, skipped = skip_earnings(orders, mkt, int(params.get("dias_resultados", EARNINGS_AVOID_DAYS)))
        notes += [f"{s} (resultados {earnings.label(mkt.earnings[s])})" for s in skipped]
    if not params.get("ignora_noticias") and cfg["strategy"] != "cetico":
        orders, skipped = skip_bad_news(orders, mkt, int(params.get("peso_noticias") or NEWS_VETO))
        notes += [f"{s} (notícias negativas)" for s in skipped]
    if not params.get("ignora_risco"):
        orders, skipped = risk_filter(orders, port, mkt)
        notes += skipped
    orders = [o for o in orders if o["side"] == "sell" or o["eur"] >= MIN_ORDER_EUR]
    if notes and not any(o["side"] == "buy" for o in orders) and port.get("last_rebalance") != rebalanced_before:
        # every buy of this rebalance was blocked by a rule: it is not counted as done, so the agent tries again at the
        # next decision instead of waiting a whole cycle ("cada" sessions) in cash
        port["last_rebalance"] = rebalanced_before
    gap = params.get("gap_max") if cfg["strategy"] != "indexador" else 0
    if gap:  # the close the buy was decided on, to cancel it if the next open is too far from it (fill_pending)
        orders = [{**o, "ref": last_close(mkt, o["symbol"]), "gap_max": gap} if o["side"] == "buy" else o for o in orders]
    if live:
        orders = [{**o, "ctx": decision_context(o, port, mkt)} for o in orders]
    return orders, notes


def decision_context(order: dict, port: dict, mkt: Market) -> dict:
    """The numbers behind an order when it was decided, for the agent's position history."""
    s = order["symbol"]
    m = mkt.stocks.get(s) or mkt.etfs.get(s) or {}
    ctx = {"zone": mkt.zone, "date": mkt.as_of.isoformat(),
           **{k: v for k, v in mkt.context.items() if k in ("vix", "epu_pct", "credit_dd", "usd_chg", "spy_trend", "gpr_pct", "oil_chg", "geo_risk")}}
    if m:
        ctx.update(trend=round(m.get("trend_score", 0)), ret_3m=round(m.get("ret_3m", 0) * 100, 1),
                   from_high=round(m.get("pct_from_high", 0) * 100, 1), vs_sma50=round((m["price"] / m["sma50"] - 1) * 100, 1),
                   vol_ratio=round(m.get("vol_ratio", 0), 2), tech=round(tech_score(m)), price=round(m["price"], 2))
        for k in ("fund_q", "fund_v", "pe", "mos"):
            if m.get(k) is not None:
                ctx[k] = round(m[k], 2)
        trend, _ = sector_state(mkt, m.get("sector"), s)
        if trend is not None:
            ctx["sector_trend"] = round(trend * 100, 1)
    score = news_score(mkt, s)
    if score:
        ctx["news"] = score
    if order["side"] == "buy" and order.get("stop"):
        ctx["stop"] = order["stop"]
    pos = port["positions"].get(s)
    if order["side"] == "sell" and pos and m:
        ctx.update(entry=round(pos["entry"], 4), ret=round((m["price"] / pos["entry"] - 1) * 100, 1),
                   held=sessions_since(mkt, pos.get("entry_date")))
    return ctx


def is_bankrupt(port: dict, mkt: Market) -> bool:
    return bool(port["history"]) and equity(port, mkt) < port["deposits"] * (1 - BANKRUPTCY)


def liquidate(port: dict, mkt: Market, reason: str) -> list[dict]:
    """Sell everything at the latest close and cancel pending orders."""
    port["pending"] = []
    done = []
    for s in list(port["positions"]):
        price = last_close(mkt, s)
        if price:
            done.append(sell(port, mkt, s, price, mkt.as_of, reason))
    return done


ROMAN = ["", "", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]


def spawn_child(config: dict, state: dict, leader: dict, replaced: dict, mkt: Market) -> dict:
    """A new agent with the leader's strategy and mutated parameters, in place of a bankrupt one."""
    base_id, base_name = leader.get("lineage", leader["id"]), leader.get("lineage_name", leader["name"])
    n = 1 + sum(1 for a in config["agents"] if a.get("lineage", a["id"]) == base_id)
    params = dict(state["agents"][leader["id"]].get("params", leader["params"]))
    changes = mutate(params, random.Random(f"{base_id}-{n}-{mkt.as_of}"), leader["strategy"])
    child = {
        "id": f"{base_id}-{n}", "name": f"{base_name} {ROMAN[n] if n < len(ROMAN) else n}", "emoji": leader["emoji"],
        "strategy": leader["strategy"], "capital": 1000, "params": params, "lineage": base_id, "lineage_name": base_name,
        **({"modo": mode_of(replaced)} if mode_of(replaced) != REACTIVE else {}),
        **({"perfil": profile_of(replaced)} if profile_of(replaced) != DEFAULT_PROFILE else {}),
        "description": f"Filho de {leader['name']}, nasceu a {mkt.as_of.isoformat()} no lugar de {replaced['name']} "
                       f"(falência). Mutação: {', '.join(changes) or 'nenhuma'}.",
    }
    config["agents"].append(child)
    port = new_portfolio(1000, mkt.as_of)
    port["params"] = params
    port["generation"] = state["agents"][leader["id"]]["generation"] + 1
    state["agents"][child["id"]] = port
    return child


SHADOWS = 2  # paper copies of each agent with slightly different parameters
SHADOW_MIN = 20  # sessions before a copy can replace its agent's parameters…
SHADOW_MAX = 60  # …and after which a copy that never did is replaced by a fresh one
PROMOTE_MARGIN = 0.02  # the copy must have made 2 percentage points more than its agent over the same sessions


def shadow_day(cfg: dict, port: dict, state: dict, mkt: Market) -> str | None:
    """Run the agent's shadow copies for one day; promote one that clearly beats it. Returns a training note."""
    if cfg["strategy"] in META | {"indexador"}:
        return None
    shadows = state.setdefault("shadows", {}).setdefault(cfg["id"], [])
    ratio = equity(port, mkt) / port["deposits"] if port["deposits"] else 1.0
    while len(shadows) < SHADOWS:
        params = {k: v for k, v in port.get("params", cfg["params"]).items()}
        for k, v in COMMON.items():
            params.setdefault(k, v)
        changes = mutate(params, random.Random(f"shadow-{cfg['id']}-{mkt.as_of}-{len(shadows)}"), cfg["strategy"])
        if not changes:
            break
        shadow = new_portfolio(1000, mkt.as_of)
        shadow["params"] = params
        shadows.append({"port": shadow, "born": mkt.as_of.isoformat(), "changes": changes,
                        "parent_ratio": round(ratio, 6), "parent_points": port["points"]})
    promoted = None
    for sh in list(shadows):
        sp = sh["port"]
        fill_pending(sp, mkt)
        check_exits(sp, mkt)
        trail_stops(sp, mkt, effective(sp["params"], mkt.zone, cfg["strategy"]))
        pay_taxes(sp, mkt)
        score_day(sp, mkt)
        if sp["last_decision"] != mkt.as_of.isoformat() and not sp["pending"]:
            orders, _ = decide(cfg, sp, mkt, sp["params"])
            sp["pending"] = [{**o, "created": mkt.as_of.isoformat()} for o in orders]
            sp["last_decision"] = mkt.as_of.isoformat()
        sessions = sessions_since(mkt, sh["born"])
        mine = equity(sp, mkt) / sp["deposits"] - 1
        theirs = ratio / sh["parent_ratio"] - 1 if sh["parent_ratio"] else 0.0
        sh.update(sessions=sessions, ret=round(mine, 4), parent_ret=round(theirs, 4))
        if (promoted is None and sessions >= SHADOW_MIN and mine - theirs >= PROMOTE_MARGIN
                and sp["points"] > port["points"] - sh["parent_points"]):
            promoted = sh
        elif sessions >= SHADOW_MAX:
            shadows.remove(sh)
    if promoted is None:
        return None
    port["params"] = promoted["port"]["params"]
    port["generation"] += 1
    note = (f"fantasma promovido ({', '.join(promoted['changes'])}): {promoted['ret'] * 100:+.1f}% contra "
            f"{promoted['parent_ret'] * 100:+.1f}% em {promoted['sessions']} sessões → geração {port['generation']}")
    port["evolution"] = (port["evolution"] + [{"date": mkt.as_of.isoformat(), "note": note}])[-EVOLUTION_KEPT:]
    state["shadows"][cfg["id"]] = []  # new copies tomorrow, from the new parameters
    return note


def run_day(config: dict, state: dict, mkt: Market) -> tuple[list[tuple[dict, dict]], list[str], list[str], list[str]]:
    """One day for every active agent; returns (moves, training notes, avoided buys, bankruptcies and births)."""
    moves, training, avoided, events = [], [], [], []
    agents_today = sorted(active_agents(config), key=lambda c: c["strategy"] in META)  # meta-agents decide last
    for cfg in agents_today:
        port = state["agents"].setdefault(cfg["id"], new_portfolio(cfg["capital"], mkt.as_of))
        mkt.league[cfg["id"]] = (cfg, port)
    bankrupt = []
    for cfg in agents_today:
        port = state["agents"][cfg["id"]]
        params = port.setdefault("params", dict(cfg["params"]))
        cancelled = list(port.get("cancelled", []))
        filled = fill_pending(port, mkt)
        for t in filled:  # filled at the session's open price: that is when, in Lisbon time
            t.setdefault("hora", open_clock(t["symbol"], date.fromisoformat(t["date"])))
        for t in filled + check_exits(port, mkt) + [x for x in [pay_taxes(port, mkt)] if x]:
            moves.append((cfg, t))
        gone = [c for c in port.get("cancelled", []) if c not in cancelled]
        if gone:
            avoided.append(f"{cfg['emoji']} {cfg['name']}: ordens canceladas: " + ", ".join(map(cancel_text, gone)))
        trail_stops(port, mkt, effective(params, mkt.zone, cfg["strategy"]))
        score_day(port, mkt)
        if cfg["strategy"] != "indexador" and is_bankrupt(port, mkt):
            moves += [(cfg, t) for t in liquidate(port, mkt, "falência")]
            cfg["active"], cfg["retired"] = False, mkt.as_of.isoformat()
            state.get("shadows", {}).pop(cfg["id"], None)
            for twin in config["agents"]:  # its daily twin compared with it: it goes too (the child gets a new one)
                if twin.get("twin_of") == cfg["id"] and twin.get("active", True):
                    twin["active"], twin["retired"] = False, mkt.as_of.isoformat()
            mkt.league.pop(cfg["id"], None)
            bankrupt.append(cfg)
            continue
        note = train(cfg, port, mkt)
        if note:
            training.append(f"{cfg['emoji']} **{cfg['name']}** {note}")
        if mode_of(cfg) == REACTIVE:  # it decides in the intraday rounds (reactive_round)
            # shadow copies replay the daily engine: they tune the daily twin, whose parameters the pre-training shares
            state.get("shadows", {}).pop(cfg["id"], None)
            continue
        if port["last_decision"] != mkt.as_of.isoformat() and not port["pending"]:
            orders, notes = decide(cfg, port, mkt, params)
            if notes:
                avoided.append(f"{cfg['emoji']} {cfg['name']}: " + ", ".join(notes))
            port["pending"] = [{**o, "created": mkt.as_of.isoformat()} for o in orders]
            port["last_decision"] = mkt.as_of.isoformat()
        note = None if cfg.get("congelado") else shadow_day(cfg, port, state, mkt)  # frozen: no change for a year
        if note:
            training.append(f"{cfg['emoji']} **{cfg['name']}** {note}")
    for dead in bankrupt:
        alive = [(c, state["agents"][c["id"]]) for c in active_agents(config)
                 if c["strategy"] not in META | {"indexador"} and mode_of(c) == mode_of(dead)
                 and profile_of(c) == profile_of(dead)]
        if not alive:
            continue
        leader, _ = max(alive, key=lambda cp: cp[1]["points"])
        child = spawn_child(config, state, leader, dead, mkt)
        events.append(f"💀 {dead['emoji']} **{dead['name']}** faliu (perdeu mais de {BANKRUPTCY:.0%}) · "
                      f"🧬 nasceu {child['emoji']} **{child['name']}**, filho do líder {leader['name']}")
    return moves, training, avoided, events


def eur(x: float) -> str:
    return f"{x:,.0f} €".replace(",", " ")


def pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def day_change(port: dict, eq: float | None = None, mkt: Market | None = None) -> float:
    """Today's change: during the rounds, the value now against the last close; else the last day of the history."""
    h = port["history"]
    if eq is not None and mkt is not None and h and h[-1]["date"] < mkt.as_of.isoformat() and h[-1]["equity"]:
        return (eq - (port["deposits"] - h[-1]["deposits"])) / h[-1]["equity"] - 1
    if len(h) < 2 or not h[-2]["equity"]:
        return 0.0
    return (h[-1]["equity"] - (h[-1]["deposits"] - h[-2]["deposits"])) / h[-2]["equity"] - 1


def with_now(port: dict, eq: float, mkt: Market) -> list[tuple[str, float]]:
    """The agent's return per day (history, recorded at each day's close), plus the value of this moment when the
    market is newer than the last close (the intraday rounds), so the chart and the periods show today's moves."""
    points = [(h["date"], (h["equity"] / h["deposits"] - 1) * 100) for h in port["history"]]
    now = mkt.as_of.isoformat()
    if port.get("deposits") and (not points or points[-1][0] < now):
        points.append((now, (eq / port["deposits"] - 1) * 100))
    return points


def best_by_profile(rows: list[tuple[dict, dict, float]]) -> list[str]:
    """For each investor profile, the reactive agent of that profile with the best grade so far (its return since it
    started, losses and the deepest fall weighted the profile's way)."""
    out = []
    for name, p in PROFILES.items():
        graded = [(profile_score(eq / port["deposits"] - 1, 1, port.get("max_dd", 0.0), name), cfg, eq / port["deposits"] - 1)
                  for cfg, port, eq in rows
                  if port.get("deposits") and mode_of(cfg) == REACTIVE and profile_of(cfg) == name
                  and cfg["strategy"] != "indexador" and port.get("history")]
        if graded:
            _, cfg, ret = max(graded, key=lambda g: g[0])
            out.append(f"{p['emoji']} {p['label']}: {cfg['emoji']} **{cfg['name']}** ({pct(ret)})")
    return out


def league(config: dict, state: dict, mkt: Market, title: str, moves=(), training=(), avoided=(), events=()) -> tuple[list[dict], dict]:
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
            f" · hoje {pct(day_change(port, eq, mkt))} · ⭐ **{port['points']}** · {len(port['positions'])} pos."
        )
    text, shown = "", 0  # a Discord message holds 6000 characters with the fields: the rest of the table is on the portal
    for line in lines:
        if len(text) + len(line) + 60 > 3000:
            break
        text, shown = text + ("\n" if text else "") + line, shown + 1
    if shown < len(lines):
        text += f"\n… e mais {len(lines) - shown} agentes (tabela completa no portal)"
    lines = [text]
    fields = [("🧭 Mercado", f"{ZONE_ICON[mkt.zone]} zona **{mkt.zone}** · {mkt.stage2_share * 100:.0f}% das ações do S&P 500 em tendência de alta")]
    best = best_by_profile(rows)
    if best:
        fields.append(("🎯 Melhor agente por perfil de investidor", "\n".join(best)))
    if moves:
        move_lines = []
        for cfg, t in list(moves)[:15]:
            if t["side"] == "imposto":
                move_lines.append(f"🧾 {cfg['emoji']} {cfg['name']} pagou {eur(t['eur'])} de imposto ({t['reason']})")
            elif t["side"] == "compra":
                move_lines.append(f"🟢 {cfg['emoji']} {cfg['name']} comprou **{t['symbol']}** · {eur(t['eur'])} ({t['reason']})")
            else:
                move_lines.append(f"🔴 {cfg['emoji']} {cfg['name']} vendeu **{t['symbol']}** · {t['pnl_eur']:+.0f} € ({t['reason']})")
        fields.append(("📥 Movimentos", "\n".join(move_lines)))
    if events:
        fields.append(("💀🧬 Falências e nascimentos", "\n".join(events)[:1024]))
    if avoided:
        fields.append(("🚫 Compras evitadas (resultados próximos ou notícias negativas)", "\n".join(avoided)[:1024]))
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

    series = {cfg["name"]: with_now(port, eq, mkt) for cfg, port, eq in rows}
    refs = reference_lines(series)
    top = interactive.leaders(series)
    files = {"agentes.png": lines_chart(series, "Rentabilidade desde o início (a cores: os 5 melhores e o Indexador)",
                                        refs, top)}
    bench = [{"name": cfg["name"], "emoji": cfg["emoji"], "since": cfg["banco"].get("since"),
              "points": [(h["date"], (h["equity"] / h["deposits"] - 1) * 100) for h in port["history"]]}
             for cfg in config["agents"] if cfg.get("banco") and (port := state["agents"].get(cfg["id"])) and port.get("history")]
    link = interactive.publish(title, series, refs, top, subtitle="Rentabilidade desde o início, sobre o dinheiro depositado",
                               agents=agent_details(config, state, mkt), kind="liga", bench=bench)
    leader = rows[0][2] / rows[0][1]["deposits"] - 1 if rows else 0
    card = embed(
        title,
        "\n".join(lines),
        color=GREEN if leader >= 0 else RED,
        fields=fields,
        image="agentes.png",
        footer="Simulação com dinheiro fictício e preços reais: os agentes reativos negoceiam ao preço de cada ronda nos "
        "mercados abertos; os diários, à abertura seguinte. 0,1% de custos (+0,5% câmbio fora do euro, como na XTB; imposto de bolsa "
        "na compra em FR, IT, ES, UK e IE), 28% de imposto sobre as mais-valias de cada ano. Pontos: ±1 por cada 0,1% acima/abaixo do "
        "S&P 500 no dia, +2 por dia em lucro, penalização acima de 10% de queda desde o máximo. Falência a -50%: o lugar "
        "passa para um filho do líder. Não é recomendação de compra.",
    )
    if link:
        card["url"] = link  # the title opens the interactive chart too
    return [card], files


READY_SESSIONS = 126  # about 6 months live
READY_TRADES = 30
READY_MAX_DD = 0.25
LOW_TURNOVER = {"indexador", "guardiao", "rotador"}  # few trades by design: the trade-count test does not apply


def live_stats(port: dict) -> dict:
    """Return, annualised Sharpe and session count from the live history (deposits excluded)."""
    h = port["history"]
    daily = [(b["equity"] - (b["deposits"] - a["deposits"])) / a["equity"] - 1 for a, b in zip(h, h[1:]) if a["equity"]]
    total = 1.0
    for r in daily:
        total *= 1 + r
    sd = statistics.pstdev(daily) if len(daily) > 1 else 0.0
    sharpe = statistics.fmean(daily) / sd * 252 ** 0.5 if sd else 0.0
    return {"sessions": len(daily), "return": total - 1, "sharpe": sharpe}


def readiness(cfg: dict, port: dict, bench: dict | None, mc: dict | None) -> list[tuple[str, bool | None]]:
    """The five checks before an agent's ideas are worth real (small) money; None = not applicable."""
    st = live_stats(port)
    sells = sum(1 for t in port["trades"] if t["side"] == "venda")
    beats = None if bench is None else st["return"] > bench["return"] and st["sharpe"] >= bench["sharpe"]
    in_range = None
    if mc and mc.get("p5") is not None and st["sessions"]:
        floor = mc["p5"] * min(1.0, st["sessions"] / 252)  # worst 1 in 20 years, prorated to the time live
        in_range = st["return"] >= floor
    return [
        (f"{st['sessions']}/{READY_SESSIONS} sessões ao vivo", st["sessions"] >= READY_SESSIONS),
        (f"vs S&P 500: {pct(st['return'])} (Sharpe {st['sharpe']:.1f})"
         + (f" contra {pct(bench['return'])} (Sharpe {bench['sharpe']:.1f})" if bench else ""), beats),
        ("dentro do Monte Carlo do backtest" if mc else "sem backtest (só se avalia ao vivo)", in_range),
        (f"{sells}/{READY_TRADES} vendas fechadas", None if cfg["strategy"] in LOW_TURNOVER else sells >= READY_TRADES),
        (f"queda máx. {pct(-port.get('max_dd', 0.0))} (limite {READY_MAX_DD:.0%})", port.get("max_dd", 0.0) <= READY_MAX_DD),
    ]


def pretrain_report(profile: str = DEFAULT_PROFILE) -> dict:
    """The latest pre-training of a profile ({} before its first run)."""
    path = pretrain_path(profile)
    return json.loads(path.read_text()) if path.exists() else {}


def readiness_card(config: dict, state: dict) -> list[dict]:
    reports = {p: pretrain_report(p).get("strategies", {}) for p in PROFILES}
    bench_cfg = next((c for c in active_agents(config) if c["strategy"] == "indexador"), None)
    bench = live_stats(state["agents"][bench_cfg["id"]]) if bench_cfg and bench_cfg["id"] in state["agents"] else None
    icon = {True: "✅", False: "❌", None: "➖"}
    lines, ready = [], []
    for cfg in active_agents(config):
        port = state["agents"].get(cfg["id"])
        if not port or cfg["strategy"] == "indexador":
            continue
        checks = readiness(cfg, port, bench, reports[profile_of(cfg)].get(cfg["strategy"], {}).get("monte_carlo"))
        passed = all(ok is not False for _, ok in checks) and any(ok for _, ok in checks)
        if passed:
            ready.append(cfg["name"])
        lines.append(f"{'🟢' if passed else '⚪'} {cfg['emoji']} **{cfg['name']}** "
                     + " ".join(icon[ok] for _, ok in checks) + f" · {checks[1][0].split(' contra')[0]}")
    summary = (f"**Prontos para a fase 2** (ideias com pouco dinheiro, decisão humana): {', '.join(ready)}"
               if ready else "**Nenhum agente pronto ainda.** A liga continua só com dinheiro fictício.")
    legend = ("Critérios, por ordem: 1) 6 meses ao vivo · 2) bate o S&P 500 com Sharpe igual ou melhor · 3) dentro do "
              "Monte Carlo do backtest · 4) 30 vendas (➖ não se aplica) · 5) queda máx. ≤ 25%")
    return [embed("🚦 Prontidão dos agentes", summary + "\n\n" + "\n".join(lines)[:3500], color=BLUE,
                  footer=legend + ". Mesmo prontos, nunca em piloto automático. Não é recomendação de compra.")]


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


def common_rules(params: dict, strategy: str) -> str:
    rules = []
    rules.append("❌ ignora resultados próximos" if params.get("ignora_resultados")
                 else f"✅ não compra ações com resultados nos próximos {EARNINGS_AVOID_DAYS} dias")
    if strategy == "cetico" or params.get("ignora_noticias"):
        rules.append("❌ não evita notícias negativas (são o seu sinal)" if strategy == "cetico" else "❌ ignora notícias")
    else:
        rules.append("✅ não compra ações com notícias claramente negativas")
    if params.get("ignora_risco"):
        rules.append("❌ ignora as regras de risco (volatilidade, setor, travão)")
    else:
        rules.append(f"✅ posições menores em ações voláteis, máx. {SECTOR_CAP:.0%} num setor, pausa de {BRAKE_DAYS} dias "
                     f"após cair {DD_BRAKE:.0%}")
    if strategy != "indexador":
        p = effective(params, "", strategy)
        on = [f"stop a {p['stop_atr']}× ATR" if p.get("stop_atr") else None,
              f"stop que sobe a {p['trail_atr']}× ATR do fecho" if p.get("trail_atr") else None,
              f"vende o que não andou em {p['tempo_max']} sessões" if p.get("tempo_max") else None,
              f"só compra com score técnico ≥ {p['min_tecnico']:.0f}/60" if p.get("min_tecnico") else None,
              f"não compra mais de {p['max_ext']:.0%} acima da média de 50 dias" if p.get("max_ext") else None]
        on = [x for x in on if x]
        rules.append("🎛️ regras afinadas pelo treino: " + ("; ".join(on) if on else "nenhuma ligada por agora")
                     + f"; veto de notícias a -{p.get('peso_noticias', NEWS_VETO)}")
        if uses_param(strategy, "regiao"):
            rules.append(f"🌍 compra ações: {REGIONS.get(int(p.get('regiao') or 0), REGIONS[0])}")
        level = int(params.get("reatividade", 2))
        how = REACTIVITY.get(level, REACTIVITY[2])
        if level == 1:
            how += f" (espera {int(params.get('espera_min') or NO_BUYS_AFTER_OPEN)} min após a abertura" + (
                f", não compra mais de {float(params['esticado_max']):.1%} acima do fecho anterior)"
                if params.get("esticado_max") else ")")
        rules.append(f"⚡ reatividade durante o dia: {how}; stops e alvos vigiados em todas as rondas")
        if params.get("por_zona"):
            rules.append("🧭 parâmetros próprios por zona de mercado: " + "; ".join(
                f"{z}: " + ", ".join(f"{k} {v}" for k, v in d.items()) for z, d in params["por_zona"].items()))
    rules.append(f"💶 custos {costs_text()} (na compra, impostos de bolsa de França, Itália, Espanha, "
                 f"Reino Unido e Irlanda), {TAX_RATE:.0%} de imposto sobre mais-valias anuais")
    if strategy != "indexador":
        rules.append(f"💀 falência se perder {BANKRUPTCY:.0%}: o lugar passa para um filho do líder")
    return "\n".join(rules)


def pretrain_text(strategy: str, profile: str = DEFAULT_PROFILE) -> str:
    saved = pretrain_report(profile)
    r = saved.get("strategies", {}).get(strategy)
    if not r:
        return ("Ainda não pré-treinado para este perfil: usa os parâmetros do perfil equilibrado."
                if profile != DEFAULT_PROFILE else "Ainda não pré-treinado.")
    if "skipped" in r:
        return f"Não testável no passado: {r['skipped']}. Aprende ao vivo (ajusta parâmetros nos meses em que perde)."
    y, v = r["year"], r["valid"]
    span = saved.get("train", [None])[0], saved.get("valid", [None, None])[1]
    years = (round((date.fromisoformat(span[1]) - date.fromisoformat(span[0])).days / 365.25)
             if span[0] and span[1] else None)
    period = f"{years} anos" if years else "Período"
    verdict = "✅ adotou os parâmetros do treino" if r["adopted"] else "↩️ mantém os parâmetros originais"
    return (f"{period}: {pct(y['return'])} · queda máx. {pct(-y['max_drawdown'])} · Sharpe {y.get('sharpe', 0):.1f} · "
            f"{y['trades']} operações\nExame (último ano, nota do perfil {PROFILES[profile]['label'].lower()} face ao S&P 500): "
            f"{v['best']['points']:+g} vs {v['default']['points']:+g} pts "
            f"({pct(v['best']['return'])} vs {pct(v['default']['return'])}) · {verdict}\n"
            f"_Os {period.lower()} incluem os anos usados para escolher os parâmetros; o exame é o teste justo._")


def strategy_card(cfg: dict, port: dict | None) -> list[dict]:
    """Everything about how one agent decides, for /estrategia."""
    params = (port or {}).get("params", cfg["params"])
    idea, buys, sells, size = strategy_docs.describe(cfg["strategy"], params)
    fields = [
        ("🟢 Quando compra", buys), ("🔴 Quando vende", sells), ("⚖️ Tamanho das posições", size),
        ("🛡️ Regras comuns", common_rules(params, cfg["strategy"])),
        ("⚙️ Parâmetros atuais", "\n".join(strategy_docs.params_lines(params)) or "nenhum"),
        ("🧠 Pré-treino", pretrain_text(cfg["strategy"], profile_of(cfg))),
    ]
    if port and port["history"]:
        last = port["history"][-1]
        family = f" · filho de {cfg['lineage_name']}" if cfg.get("lineage") else ""
        fields.append(("📊 Ao vivo", (
            f"{eur(last['equity'])} ({pct(last['equity'] / last['deposits'] - 1)}) · ⭐ {port['points']} pontos · "
            f"queda máx. {pct(-port.get('max_dd', 0.0))} · {len(port['positions'])} posições · "
            f"geração {port['generation']}{family}"
            + (f"\nÚltimo treino: {port['evolution'][-1]['note']}" if port["evolution"] else ""))))
    status = "" if cfg.get("active", True) else f" (💀 retirado {cfg.get('retired', '')})"
    return [embed(f"{cfg['emoji']} {cfg['name']}{status} · como pensa", f"💡 {idea}", color=BLUE, fields=fields,
                  footer="Simulação com dinheiro fictício e preços reais. Não é recomendação de compra.")]


def strategy_info(cfg: dict, params: dict) -> dict:
    """How an agent decides, as plain fields for the interactive page."""
    idea, buys, sells, size = strategy_docs.describe(cfg["strategy"], params)
    return {
        "label": STRATEGIES[cfg["strategy"]][1], "description": cfg.get("description", ""),
        "idea": idea, "buys": buys, "sells": sells, "size": size, "rules": common_rules(params, cfg["strategy"]),
        "params": [{"key": k, "value": strategy_docs.fmt_param(k, v), "meaning": strategy_docs.PARAMS.get(k, "")}
                   for k, v in params.items()],
        "pretrain": pretrain_text(cfg["strategy"], profile_of(cfg)),
    }


def _when(trade: dict) -> str:
    """A trade's date with its Lisbon time when the round recorded one ("2026-11-03 14:20")."""
    return trade["date"] + (" " + trade["hora"] if trade.get("hora") else "")


def position_history(trades: list[dict], strategy: str, params: dict, kept: int = 60) -> list[dict]:
    """The trades grouped into positions (first buy to last sell), newest first, each trade with its justification."""
    lots, open_lot = [], {}
    for t in trades:
        if t["side"] not in ("compra", "venda"):
            continue
        t = {**t, "why": strategy_docs.justify(strategy, params, t)}
        lot = open_lot.get(t["symbol"])
        if lot is None:
            lot = {"symbol": t["symbol"], "opened": t["date"], "opened_at": _when(t), "closed": None, "closed_at": None,
                   "buys": [], "sells": [], "qty": 0.0, "invested": 0.0, "result": 0.0, "partial": t["side"] == "venda"}
            open_lot[t["symbol"]] = lot
            lots.append(lot)
        if t["side"] == "compra":
            lot["buys"].append(t)
            lot["qty"] += t["qty"]
            lot["invested"] += t["eur"]
        else:
            lot["sells"].append(t)
            lot["qty"] -= t["qty"]
            lot["result"] += t["pnl_eur"]
            if lot["qty"] <= 1e-6:
                lot["closed"], lot["closed_at"] = t["date"], _when(t)
                open_lot.pop(t["symbol"], None)
    for lot in lots:
        lot["status"] = "fechada" if lot["closed"] else "aberta"
        lot["result"] = round(lot["result"], 2)
        lot["result_pct"] = round(lot["result"] / lot["invested"] * 100, 2) if lot["invested"] else None
        end = date.fromisoformat(lot["closed"]) if lot["closed"] else None
        lot["days"] = (end - date.fromisoformat(lot["opened"])).days if end else None
        lot["qty"] = round(max(lot["qty"], 0.0), 6)
    return list(reversed(lots))[:kept]


def agent_details(config: dict, state: dict, mkt: Market) -> list[dict]:
    """Everything about each active agent for the interactive page: holdings, orders, trades, stats, strategy."""
    out = []
    for cfg in active_agents(config):
        port = state["agents"].get(cfg["id"])
        if not port:
            continue
        params = port.get("params", cfg["params"])
        eq = equity(port, mkt)
        positions = []
        for symbol, pos in sorted(port["positions"].items()):
            price = last_close(mkt, symbol)
            value = position_value(mkt, symbol, pos)
            info = mkt.stocks.get(symbol) or mkt.etfs.get(symbol) or {}
            positions.append({
                "symbol": symbol, "name": info.get("name", ""), "sector": info.get("sector", ""),
                "currency": currency_of(symbol), "qty": round(pos["qty"], 6),
                "entry": round(pos["entry"], 4), "entry_date": pos.get("entry_date"), "price": round(price, 4) if price else None,
                "cost_eur": round(pos["cost_eur"], 2), "value_eur": round(value, 2),
                "pnl_eur": round(value - pos["cost_eur"], 2),
                "pnl_pct": round((value / pos["cost_eur"] - 1) * 100, 2) if pos["cost_eur"] else 0.0,
                "weight": round(value / eq * 100, 2) if eq else 0.0,
                "stop": pos.get("stop"), "tp": pos.get("tp"),
                "earnings": (mkt.earnings.get(symbol).isoformat() if mkt.earnings.get(symbol) else None),
            })
        trades = port["trades"]
        history = position_history(trades, cfg["strategy"], params)
        why_open = {lot["symbol"]: (lot["buys"][-1] if lot["buys"] else {}) for lot in history if lot["status"] == "aberta"}
        for row in positions:
            buy_t = why_open.get(row["symbol"], {})
            row["reason"], row["why"] = buy_t.get("reason", ""), buy_t.get("why", "")
            lot = next((x for x in history if x["symbol"] == row["symbol"] and x["status"] == "aberta"), None)
            row["since"] = lot["opened_at"] if lot and lot["opened"] == row["entry_date"] else row["entry_date"]
        stats = live_stats(port)
        hist = port["history"]
        peak, max_dd = 0.0, 0.0
        for h in hist:
            ratio = h["equity"] / h["deposits"] if h["deposits"] else 1.0
            peak = max(peak, ratio)
            max_dd = max(max_dd, 1 - ratio / peak if peak else 0.0)
        out.append({
            "id": cfg["id"], "name": cfg["name"], "emoji": cfg["emoji"], "strategy": strategy_info(cfg, params),
            "perfil": profile_of(cfg), "modo": mode_of(cfg),
            "started": port.get("started"), "generation": port.get("generation", 1),
            "lineage": cfg.get("lineage_name"), "points": port["points"],
            "deposits": round(port["deposits"], 2), "cash": round(port["cash"], 2), "equity": round(eq, 2),
            "return_pct": round((eq / port["deposits"] - 1) * 100, 2) if port["deposits"] else 0.0,
            "day_pct": round(day_change(port, eq, mkt) * 100, 2), "max_dd_pct": round(max_dd * 100, 2),
            "sharpe": round(stats["sharpe"], 2), "sessions": stats["sessions"],
            "invested_pct": round((eq - port["cash"]) / eq * 100, 2) if eq else 0.0,
            "realized": port.get("realized", {}),
            "taxes": round(sum(t["eur"] for t in trades if t["side"] == "imposto"), 2),
            "fees_note": f"{costs_text()}, + imposto de bolsa na compra em FR 0,4%, "
                         "IT 0,1%, ES 0,2%, UK 0,5% e IE 1%",
            "buys": sum(t["side"] == "compra" for t in trades), "sells": sum(t["side"] == "venda" for t in trades),
            "wins": sum(t["side"] == "venda" and t["pnl_eur"] > 0 for t in trades),
            "positions": positions, "pending": [{**o, "why": strategy_docs.context_text(o.get("ctx"))} for o in port["pending"]],
            "trades": list(reversed(trades)), "history": history,
            "evolution": list(reversed(port.get("evolution", [])))[:20],
            "shadows": [{"changes": sh["changes"], "born": sh["born"], "sessions": sh.get("sessions", 0),
                         "ret_pct": round(sh.get("ret", 0) * 100, 2), "parent_ret_pct": round(sh.get("parent_ret", 0) * 100, 2)}
                        for sh in state.get("shadows", {}).get(cfg["id"], [])],
            "por_zona": params.get("por_zona") or {},
            "genealogia": cfg.get("genealogia"),  # a fusion's family tree and the analysis behind it (scripts/jury.py)
            "dividendos": {k: v for k, v in (port.get("dividendos") or {}).items() if k != "ate"},  # credit_dividends
        })
    return out


def strategies_index(config: dict) -> list[dict]:
    lines = [f"{c['emoji']} **{c['name']}** · {strategy_docs.DOCS[c['strategy']][0]}" for c in active_agents(config)]
    return [embed("🧭 Estratégias dos agentes", "\n".join(lines)[:4096], color=BLUE,
                  footer="Usa /estrategia nome para ver tudo sobre um agente: regras, parâmetros, pré-treino e resultados.")]


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
    if action == "prontidao":
        return readiness_card(config, state), {}, False
    if action == "estrategia":
        if not name:
            return strategies_index(config), {}, False
        if not cfg:
            return [embed("⚠️ Agente não encontrado", f"`{name}` · usa /estrategia sem nome para ver a lista", color=RED)], {}, False
        return strategy_card(cfg, state["agents"].get(cfg["id"])), {}, False
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


def publish_agent_list(config: dict) -> None:
    """The agents a member can follow in the profile (portal:agentes, read by the Worker): the active league."""
    rows = [{"id": c["id"], "name": c["name"], "emoji": c["emoji"], "perfil": profile_of(c), "modo": mode_of(c),
             "congelado": bool(c.get("congelado"))} for c in active_agents(config)]
    try:
        import discord_bot

        discord_bot.kv_put("portal:agentes", rows)
    except Exception as e:  # noqa: BLE001
        print(f"WARN: lista de agentes: {e!r}", file=sys.stderr)


def publish_calendar(config: dict, state: dict, mkt: Market, extra: set[str] = frozenset()) -> None:
    """The portal's calendar: macro events for 14 days and results dates of the followed stocks for 21 days."""
    import macro

    holders: dict[str, list[str]] = {}
    for cfg in active_agents(config):
        for sym in (state["agents"].get(cfg["id"]) or {}).get("positions", {}):
            holders.setdefault(sym, []).append(f"{cfg['emoji']} {cfg['name']}")
    ranked = {r["symbol"] for r in mkt.rankings}
    syms = [x for x in sorted(set(holders) | ranked | set(extra)) if x in mkt.stocks]
    missing = [x for x in syms if x not in mkt.earnings]
    if missing:
        mkt.earnings.update(earnings.next_earnings_many(missing))
    events, from_fmp = macro.events(mkt.as_of, mkt.as_of + timedelta(days=14))
    rows = sorted(({"symbol": x, "name": mkt.stocks[x].get("name", ""), "date": mkt.earnings[x].isoformat(),
                    "held_by": holders.get(x, []), "ranking": x in ranked}
                   for x in syms if isinstance(mkt.earnings.get(x), date) and 0 <= (mkt.earnings[x] - mkt.as_of).days <= 21),
                  key=lambda r: r["date"])
    interactive.publish_section("calendar", {
        "as_of": mkt.as_of.isoformat(), "source": "FMP" if from_fmp else "calendário de reserva (reuniões da Fed)",
        "macro": [{"date": d.isoformat(), "name": n} for d, n in events], "earnings": rows,
        "avoid_days": EARNINGS_AVOID_DAYS,
    })


LEAGUE_TITLE = "🤖 Liga dos agentes"
MONTHLY_TITLE = "📆 Relatório mensal dos agentes"


# --------------------------------------------------------------------------
# Intraday stop round
# --------------------------------------------------------------------------


DIVIDENDS_KEPT = 30  # last dividends listed per agent


def fetch_dividends(symbols: list[str], since: date) -> dict[str, list[tuple[date, float]]]:
    """Each symbol's dividends (ex-date, amount per share in its quote currency) since that day, from Yahoo."""
    if not symbols:
        return {}
    import yfinance as yf

    out: dict[str, list[tuple[date, float]]] = {}
    try:
        df = yf.download(symbols, start=since.isoformat(), actions=True, auto_adjust=False, progress=False,
                         group_by="ticker", threads=True)
    except Exception as e:  # noqa: BLE001 - no dividends today, the rest of the run goes on
        print(f"WARN: dividendos: {e!r}", file=sys.stderr)
        return {}
    for s in symbols:
        try:
            col = df[s]["Dividends"] if len(symbols) > 1 else df["Dividends"]
        except (KeyError, TypeError):
            continue
        col = col.dropna()
        out[s] = [(ix.date(), float(v)) for ix, v in col.items() if v and v > 0]
    return out


def credit_dividends(config: dict, state: dict, mkt: Market, fetch=fetch_dividends) -> list[str]:
    """The dividends of the stocks each agent held on their ex-date, paid into its cash after the Portuguese tax (28%,
    the US 15% withheld at source is credited against it). The prices are not adjusted live: on the ex-date the price
    falls by the dividend, and until now the agent never received it. Totals in port["dividendos"]."""
    agents_with = [(c, state["agents"][c["id"]]) for c in config["agents"]
                   if not c.get("desligado") and (state["agents"].get(c["id"]) or {}).get("positions")]
    held = sorted({s for _, p in agents_with for s in p["positions"]})
    if not held:
        return []
    since = min(date.fromisoformat((p.get("dividendos") or {}).get("ate") or (mkt.as_of - timedelta(days=1)).isoformat())
                for _, p in agents_with)
    divs = fetch(held, since + timedelta(days=1))
    notes = []
    for cfg, port in agents_with:
        book = port.setdefault("dividendos", {"bruto_eur": 0.0, "imposto_eur": 0.0, "liquido_eur": 0.0, "ultimos": []})
        last = date.fromisoformat(book.get("ate") or (mkt.as_of - timedelta(days=1)).isoformat())
        for sym, pos in port["positions"].items():
            entry = date.fromisoformat(pos["entry_date"]) if pos.get("entry_date") else None
            for ex, amount in divs.get(sym, []):
                if not (last < ex <= mkt.as_of) or (entry and entry >= ex):  # bought on or after the ex-date: not paid
                    continue
                try:
                    gross = pos["qty"] * to_eur(mkt, sym, amount, ex)
                except KeyError:
                    continue
                tax = gross * TAX_RATE
                port["cash"] += gross - tax
                book["bruto_eur"] = round(book["bruto_eur"] + gross, 2)
                book["imposto_eur"] = round(book["imposto_eur"] + tax, 2)
                book["liquido_eur"] = round(book["liquido_eur"] + gross - tax, 2)
                book["ultimos"] = (book["ultimos"] + [{"date": ex.isoformat(), "symbol": sym, "bruto_eur": round(gross, 2),
                                                       "liquido_eur": round(gross - tax, 2)}])[-DIVIDENDS_KEPT:]
                notes.append(f"{cfg['emoji']} {cfg['name']}: dividendo de {sym} {eur(gross - tax)} líquido")
        book["ate"] = mkt.as_of.isoformat()
    return notes


FROZEN_PATH = REPO_AGENTS / "lab" / "congelados.json"


def frozen_report(config: dict, state: dict, mkt: Market) -> dict:
    """The frozen candidates (config "congelado": parameters fixed for a year, never moved by the training, the jury
    or the renewal) against the Indexador live, as a real account would see them: what is left after selling
    everything today and paying the tax still owed (the index's own tax is deferred until then too)."""
    def after_tax(cfg: dict) -> tuple[float, float] | None:
        port = state["agents"].get(cfg["id"])
        if not port or not port.get("history"):
            return None
        eq = equity(port, mkt)
        return eq, eq - tax_owed_if_sold(port, mkt)

    index_cfg = next((c for c in config["agents"] if c["strategy"] == "indexador"), None)
    out = {"updated": mkt.as_of.isoformat(), "agents": {}}
    for cfg in [c for c in config["agents"] if c.get("congelado")]:
        mine, cmp = after_tax(cfg), live_vs_index(cfg, state)
        if not mine:
            continue
        port = state["agents"][cfg["id"]]
        deposits = port.get("deposits", cfg["capital"])
        row = {"desde": cfg["congelado"].get("desde"), "ate": cfg["congelado"].get("ate"),
               "bruto": round(mine[0] / deposits - 1, 4), "liquido": round(mine[1] / deposits - 1, 4)}
        if cmp and index_cfg:  # the index over the same days, sold today: 28% on its gain
            row["indice_bruto"] = round(cmp[1], 4)
            row["indice_liquido"] = round(cmp[1] - TAX_RATE * max(cmp[1], 0.0), 4)
            row["diferenca_liquida"] = round(row["liquido"] - row["indice_liquido"], 4)
            row["sessoes"] = cmp[2]
        try:  # the same agent in a virtual IBKR account (whole shares, IBKR's costs and order rules)
            import ibkr

            if (v := ibkr.virtual_summary(cfg["id"])):
                row["ibkr"] = v
        except Exception:  # noqa: BLE001
            pass
        out["agents"][cfg["id"]] = row
    try:
        FROZEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        FROZEN_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    except OSError:
        pass
    return out


# The rule written before the results (Tiago, 2026-11-05): at the end of the frozen year a candidate only moves to real
# money if, after the tax owed when sold, it beat the S&P 500 by DECISION_MARGIN and fell no deeper than it.
DECISION_MARGIN = 0.02


def decision_page(config: dict, state: dict, mkt: Market, frozen: dict) -> dict:
    """The portal's 🎯 Decisão: the frozen candidates against the S&P 500 after tax, the rule, and today's orders of each
    scaled to the real account (capital_real), to copy by hand if a candidate is ever chosen. Nothing is sent to a
    broker."""
    capital = real_capital()
    by_id = {c["id"]: c for c in config["agents"]}
    index_port = state["agents"].get("indexador") or {}

    def deepest(port: dict) -> float:
        peak, dd = 0.0, 0.0
        for h in port.get("history") or []:
            r = h["equity"] / h["deposits"] if h.get("deposits") else 1.0
            peak = max(peak, r)
            dd = max(dd, 1 - r / peak if peak else 0.0)
        return dd

    idx_dd = deepest(index_port)
    rows, orders = [], []
    for aid, row in frozen.get("agents", {}).items():
        cfg, port = by_id.get(aid), state["agents"].get(aid) or {}
        if not cfg:
            continue
        dd = deepest(port)
        diff = row.get("diferenca_liquida")
        verdict = ("ainda sem dias suficientes" if diff is None else
                   "a cumprir a regra" if diff >= DECISION_MARGIN and dd <= idx_dd + 1e-9 else "ainda não cumpre a regra")
        rows.append({"id": aid, "name": cfg["name"], "emoji": cfg["emoji"], "estrategia": cfg["strategy"], **row,
                     "queda_max": round(dd, 4), "indice_queda_max": round(idx_dd, 4), "veredicto": verdict})
        scale = capital / (port.get("deposits") or cfg.get("capital") or 1000)
        for o in port.get("pending") or []:
            orders.append({"agent": cfg["name"], "emoji": cfg["emoji"], "symbol": o["symbol"], "side": o["side"],
                           "eur": round((o.get("eur") or 0) * scale, 2) if o["side"] == "buy" else None,
                           "fraction": o.get("fraction", 1.0), "reason": o.get("reason", "")})
    page = {"as_of": mkt.as_of.isoformat(), "capital_real": capital, "margem": DECISION_MARGIN, "candidatos": rows,
            "ordens": orders,
            "regra": f"Ao fim do ano congelado, um candidato só passa a dinheiro real se, depois do imposto que pagaria ao "
                     f"vender, bater o S&P 500 por pelo menos {DECISION_MARGIN * 100:.0f} pontos percentuais e não tiver "
                     "caído mais do que ele. Regra escrita antes dos resultados."}
    try:
        import interactive

        interactive.publish_section("decisao", page)
    except Exception as e:  # noqa: BLE001 - the portal never stops the day
        print(f"WARN: decisão: {e!r}", file=sys.stderr)
    return page


def ensure_twins(config: dict, state: dict, start: date) -> list[dict]:
    """A daily twin ("· diário") for every reactive agent that has none (the Indexador needs none): same strategy and
    parameters, from the same capital, so the league shows what reacting during the day is worth."""
    twinned = {a.get("twin_of") for a in config["agents"] if mode_of(a) == DAILY}
    created = []
    for cfg in list(active_agents(config)):
        if (mode_of(cfg) == DAILY or cfg["strategy"] in ("indexador", "super") or cfg["id"] in twinned
                or profile_of(cfg) != DEFAULT_PROFILE):  # one twin per strategy is enough for the comparison
            continue
        twin = {**{k: v for k, v in cfg.items() if k not in ("id", "name", "description", "lineage", "lineage_name")},
                "id": f"{cfg['id']}-diario", "name": f"{cfg['name']} · diário", "modo": DAILY, "twin_of": cfg["id"],
                "description": f"Gémeo diário de {cfg['name']}: a mesma estratégia, mas decide uma vez por dia antes "
                               "das aberturas (07:30), como no pré-treino. Serve de comparação com o modo reativo."}
        port = new_portfolio(cfg["capital"], start)
        port["params"] = dict((state["agents"].get(cfg["id"]) or {}).get("params", cfg["params"]))
        config["agents"].append(twin)
        state["agents"][twin["id"]] = port
        created.append(twin)
    return created


def ensure_profiles(config: dict, state: dict, start: date) -> list[dict]:
    """For every trainable strategy, a reactive agent per investor profile (PROFILES): the balanced one is the
    original agent; the conservative and aggressive ones start with its parameters until their own pre-training."""
    have = {(a.get("profile_base"), profile_of(a)) for a in config["agents"]}
    created = []
    for cfg in list(active_agents(config)):
        if (mode_of(cfg) != REACTIVE or profile_of(cfg) != DEFAULT_PROFILE
                or cfg["strategy"] in LIVE_ONLY | INTRADAY_ONLY  # no pre-training: a profile copy would be identical
                or cfg["strategy"] == "indexador" or cfg.get("lineage") or cfg["id"] != cfg["strategy"]):
            continue
        for name, p in PROFILES.items():
            if name == DEFAULT_PROFILE or (cfg["id"], name) in have:
                continue
            agent = {**{k: v for k, v in cfg.items() if k not in ("id", "name", "description", "lineage", "lineage_name")},
                     "id": f"{cfg['id']}-{name}", "name": f"{cfg['name']} {p['label']}", "perfil": name,
                     "profile_base": cfg["id"],
                     "description": f"{cfg['name']} para um investidor {p['label'].lower()}: a mesma estratégia, com os "
                                    f"parâmetros que o pré-treino escolhe para esse perfil."}
            port = new_portfolio(cfg["capital"], start)
            port["params"] = dict((state["agents"].get(cfg["id"]) or {}).get("params", cfg["params"]))
            config["agents"].append(agent)
            state["agents"][agent["id"]] = port
            created.append(agent)
    return created


def live_exits(port: dict, mkt: Market, now: datetime, targets: bool = False) -> list[dict]:
    """Exits at the price of the round (and sold at it). Stops of positions bought during today's session (today's
    low may come from before the purchase); with `targets` (reactive agents), every target the price has reached
    now: the price is really there, so a reactive agent takes it the same day."""
    done = []
    today = mkt.as_of.isoformat()
    for symbol, pos in list(port["positions"].items()):
        bought_today = pos.get("checked", "") >= today
        if not (bought_today or targets) or not trading_now(mkt, symbol, now):
            continue
        price = last_close(mkt, symbol)
        if price is None:
            continue
        if bought_today and pos.get("stop") and price <= pos["stop"]:
            done.append(sell(port, mkt, symbol, price, mkt.as_of, "stop",
                             ctx={"stop": round(pos["stop"], 4), "entry": round(pos["entry"], 4), "price": round(price, 4)}))
        elif pos.get("tp") and price >= pos["tp"]:
            done.append(sell(port, mkt, symbol, price, mkt.as_of, "alvo",
                             ctx={"tp": round(pos["tp"], 4), "entry": round(pos["entry"], 4), "price": round(price, 4)}))
    return done


def session_minutes(symbol: str, now: datetime) -> tuple[float, float]:
    """Minutes since today's open and until today's close at the symbol's exchange."""
    from zoneinfo import ZoneInfo

    ex = exchange_of(symbol)
    tz, ch, cm = EXCHANGE_CLOSE[ex]
    oh, om = EXCHANGE_OPEN.get(ex, (9, 0))
    local = now.astimezone(ZoneInfo(tz))
    start = local.replace(hour=oh, minute=om, second=0, microsecond=0)
    end = local.replace(hour=ch, minute=cm, second=0, microsecond=0)
    return (local - start).total_seconds() / 60, (end - local).total_seconds() / 60


def move_today(mkt: "Market", symbol: str) -> float | None:
    """The price now against the last complete session's close (today's bar is the last one while the session runs)."""
    df = mkt.hist.get(symbol)
    price = last_close(mkt, symbol)
    if df is None or price is None or len(df) < 2:
        return None
    today = df.index[-1].date() == mkt.as_of
    prev = float(df["Close"].iloc[-2] if today else df["Close"].iloc[-1])
    return price / prev - 1 if prev > 0 else None


def round_buy_block(port: dict, mkt: Market, params: dict) -> str | None:
    """Why this reactive agent makes no new buys in this round at all (the day's loss stop), or None."""
    if params.get("ignora_risco") or not port.get("history"):
        return None
    before = port["history"][-1]["equity"] if port["history"][-1]["date"] < mkt.as_of.isoformat() else (
        port["history"][-2]["equity"] if len(port["history"]) > 1 else None)
    if before and equity(port, mkt) / before - 1 <= -DAY_LOSS_STOP:
        return f"já perdeu {DAY_LOSS_STOP:.0%} hoje: sem compras novas até amanhã"
    return None


def buy_window(symbol: str, now: datetime, params: dict, mkt: "Market | None" = None) -> str | None:
    """Why a reactive buy of this symbol waits (too close to its exchange's open or close, or, with reatividade 1,
    before espera_min or with the price stretched more than esticado_max above the last close), or None."""
    level = int(params.get("reatividade", 2))
    if params.get("ignora_risco") and level == 2:
        return None
    since_open, to_close = session_minutes(symbol, now)
    wait = NO_BUYS_AFTER_OPEN if level == 2 else max(NO_BUYS_AFTER_OPEN, int(params.get("espera_min") or NO_BUYS_AFTER_OPEN))
    if since_open < wait:
        return f"{symbol} (só compra {wait} min depois da abertura)"
    stretch = float(params.get("esticado_max") or 0) if level == 1 else 0.0
    if stretch and mkt is not None:
        move = move_today(mkt, symbol)
        if move is not None and move > stretch:
            return f"{symbol} (sobe {move:.1%} hoje: só compra até {stretch:.1%} acima do fecho anterior)"
    if to_close < NO_BUYS_BEFORE_CLOSE:
        return f"{symbol} (não compra nos últimos {NO_BUYS_BEFORE_CLOSE} min da sessão)"
    return None


def react(cfg: dict, port: dict, mkt: Market, params: dict, now: datetime, live: bool = True) -> tuple[list[dict], list[str]]:
    """One reactive decision: the strategy runs with the prices of this moment. Orders in markets that are open are
    traded now, at the current price; the others join the plan of pending orders for the next open (at the open
    price, with the gap rule). The money of pending buys stays reserved, a symbol already pending is left to its
    order, and a stock sold today is not bought back the same day. Buys in open markets also wait out the first
    and last minutes of the session (buy_window), and stop for the day after a DAY_LOSS_STOP loss (round_buy_block).
    With reatividade 0 the buys always join the plan for the next open; with 1 they also wait for confirmation."""
    today = mkt.as_of.isoformat()
    if not port["positions"] and not port["pending"]:
        # all in cash: a reactive agent looks for entries in every round, not only every "cada" sessions
        port["last_rebalance"] = None
    sold = {t["symbol"] for t in port["trades"] if t["side"] == "venda" and t["date"] == today}
    pending = {o["symbol"] for o in port["pending"]}
    reserved = sum(o["eur"] for o in port["pending"] if o["side"] == "buy")
    mkt.now = now
    port["cash"] -= reserved
    try:
        orders, notes = decide(cfg, port, mkt, params, live=live)
    finally:
        port["cash"] += reserved
    done = []
    day_block = round_buy_block(port, mkt, params)
    if day_block:
        notes = notes + [day_block]
    for o in sorted(orders, key=lambda o: o["side"] != "sell"):  # sells first, so their cash is there for the buys
        sym = o["symbol"]
        if sym in pending or (o["side"] == "buy" and sym in sold):
            continue
        if o["side"] == "buy" and day_block:
            continue
        planned = o["side"] == "buy" and int(params.get("reatividade", 2)) == 0
        if trading_now(mkt, sym, now) and not planned:
            if o["side"] == "buy":
                wait = buy_window(sym, now, params, mkt)
                if wait:
                    notes = notes + [wait]
                    continue
            price = last_close(mkt, sym)
            if price is None:
                continue
            if o["side"] == "sell":
                if sym in port["positions"]:
                    done.append(sell(port, mkt, sym, price, mkt.as_of, o.get("reason", ""), o.get("fraction", 1.0), o.get("ctx")))
            else:
                t = buy(port, mkt, o, price, mkt.as_of)
                if t:
                    port["positions"][sym]["checked"] = today  # today's low/high before the buy do not count
                    done.append(t)
        else:
            df = mkt.hist.get(sym)
            if df is not None and len(df):  # fills at the first open after its last bar (later today or tomorrow)
                port["pending"].append({**o, "created": df.index[-1].date().isoformat()})
                pending.add(sym)
    return done, notes


def reactive_round(config: dict, state: dict, mkt: Market, now: datetime,
                   live: bool = True, skip: set[str] | frozenset = frozenset()) -> tuple[list[tuple[dict, dict]], list[str]]:
    """An intraday round (every 30 minutes during the sessions), with the market up to now (load_market(live=True)).
    Every agent: pending orders whose market opened today fill at that open, stops and targets reached so far are
    executed. Reactive agents then decide again with the prices of this moment (react). Daily twins only do the
    first part, at exactly the prices their daily run would use."""
    from zoneinfo import ZoneInfo

    clock = now.astimezone(ZoneInfo("Europe/Lisbon")).strftime("%H:%M")
    moves, avoided = [], []
    order = sorted(active_agents(config), key=lambda c: c["strategy"] in META)  # meta-agents decide last
    for cfg in order:
        if cfg["id"] in state["agents"]:
            mkt.league[cfg["id"]] = (cfg, state["agents"][cfg["id"]])
    for cfg in order:
        port = state["agents"].get(cfg["id"])
        if not port or cfg["id"] in skip:  # skip: already done by the Rust engine (engine.live_round)
            continue
        params = port.setdefault("params", dict(cfg["params"]))
        cancelled = list(port.get("cancelled", []))
        reactive = mode_of(cfg) == REACTIVE
        done = (fill_pending(port, mkt, partial_day=mkt.as_of) + check_exits(port, mkt, open_day=mkt.as_of)
                + live_exits(port, mkt, now, targets=reactive))
        gone = [c for c in port.get("cancelled", []) if c not in cancelled]
        if gone:
            avoided.append(f"{cfg['emoji']} {cfg['name']}: ordens canceladas: " + ", ".join(map(cancel_text, gone)))
        if reactive:
            try:
                traded, notes = react(cfg, port, mkt, params, now, live=live)
            except Exception as e:  # one agent's error never stops the others
                print(f"WARN: {cfg['id']}: {e!r}", file=sys.stderr)
                traded, notes = [], []
            done += traded
            if notes:
                avoided.append(f"{cfg['emoji']} {cfg['name']}: " + ", ".join(notes))
        for t in done:
            t["hora"] = clock
            moves.append((cfg, t))
    return moves, avoided


def archive(config: dict, state: dict, mkt: Market | None = None) -> None:
    """Keep what cannot be downloaded later (market_archive: decisions; with a daily market, closes and earnings)."""
    try:
        import market_archive

        market_archive.decisions(config, state)
        if mkt is not None:
            market_archive.closes(mkt)
            market_archive.earnings(mkt)
    except Exception as e:  # the archive never stops the league
        print(f"WARN: arquivo: {e!r}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["daily", "config", "portal", "stops"])
    parser.add_argument("args", nargs="?", default="{}", help="config: JSON with acao, nome, estrategia, capital, valor, emoji")
    parser.add_argument("--dry-run", action="store_true", help="Print instead of posting; state is still saved")
    a = parser.parse_args()

    config, state = load_config(), load_state()
    target = reply_target()
    follow_up = ([], {})
    if a.mode == "stops":  # intraday round: the agents react to the market of this moment; post only when they trade
        now = datetime.now(timezone.utc)
        mkt = load_market(now, live=True)
        print(f"Ronda: mercado carregado em {(datetime.now(timezone.utc) - now).total_seconds():.0f} s")
        try:  # our own intraday history (private repo, branch arquivo-intradiario), when it is checked out
            import market_archive

            market_archive.intraday(mkt, now)
        except Exception as e:  # noqa: BLE001
            print(f"WARN: arquivo intradiário: {e!r}", file=sys.stderr)
        before = json.dumps(state, sort_keys=True, default=str)
        twins = ensure_twins(config, state, mkt.as_of) + ensure_profiles(config, state, mkt.as_of)
        earnings_cache = PRICE_CACHE / f"earnings-{mkt.as_of.isoformat()}.json"
        try:  # earnings dates looked up once a day, not every round
            mkt.earnings = {s: (date.fromisoformat(d) if d else None)
                            for s, d in json.loads(earnings_cache.read_text()).items()}
        except (OSError, ValueError):
            pass
        # Phase 4c: the Rust engine trades every agent whose strategy it runs; Python the rest (live-only and meta
        # strategies, which read the others' portfolios: after them). OGJ_LIVE_ENGINE=python goes back to Python.
        import engine

        snapshot = json.loads(json.dumps(state, default=str))
        rust_ids, moves = set(), []
        if os.environ.get("OGJ_LIVE_ENGINE", "rust") != "python":
            try:
                rust_ids, moves = engine.live_round(config, state, mkt, now)
            except Exception as e:  # noqa: BLE001 - the round goes on in Python
                print(f"WARN: ronda no motor Rust falhou, a ronda segue em Python: {e!r}", file=sys.stderr)
                state.clear()
                state.update(json.loads(json.dumps(snapshot)))
                rust_ids, moves = set(), []
        from zoneinfo import ZoneInfo

        clock = now.astimezone(ZoneInfo("Europe/Lisbon")).strftime("%H:%M")
        for _, t in moves:
            t["hora"] = clock
        py_moves, avoided = reactive_round(config, state, mkt, now, skip=rust_ids)
        moves += py_moves
        if rust_ids and os.environ.get("OGJ_SHADOW", "1") != "0":  # the Python reference in the shadow, compared
            try:
                ref = json.loads(json.dumps(snapshot))
                only = {c["id"] for c in config["agents"]} - rust_ids
                reactive_round(config, ref, mkt, now, skip=only)
                print(f"Ronda-sombra (Python): {engine.compare_round(rust_ids, ref, state, mkt.as_of)}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"WARN: ronda-sombra: {e!r}", file=sys.stderr)
            for cfg in active_agents(config):  # the shadow registered its copies: the real portfolios again
                if cfg["id"] in state["agents"]:
                    mkt.league[cfg["id"]] = (cfg, state["agents"][cfg["id"]])
        if engine.ENGINE_SHADOW and os.environ.get("OGJ_SHADOW", "1") != "0":  # strategies being ported, in the shadow
            try:
                print(f"Ronda-sombra (Rust, em observação): "
                      f"{engine.shadow_round(config, snapshot, state, mkt, now, only=engine.ENGINE_SHADOW)}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"WARN: ronda-sombra nova: {e!r}", file=sys.stderr)
        try:
            PRICE_CACHE.mkdir(parents=True, exist_ok=True)
            earnings_cache.write_text(json.dumps({s: (d.isoformat() if d else None) for s, d in mkt.earnings.items()}))
        except OSError:
            pass
        changed = json.dumps(state, sort_keys=True, default=str) != before
        if changed:  # the agents under test, as if traded at Interactive Brokers (scripts/ibkr.py)
            try:
                import ibkr

                ibkr.virtual_round(config, state, mkt, now)
            except Exception as e:  # noqa: BLE001
                print(f"WARN: IBKR simulada: {e!r}", file=sys.stderr)
        print(f"Ronda: decisões feitas em {(datetime.now(timezone.utc) - now).total_seconds():.0f} s desde o início")
        if not changed and not twins:
            return 0
        save(config, state)
        archive(config, state)
        if moves and not a.dry_run:  # the members who follow an agent hear of its trades in the round it decides
            try:
                import copy_alerts

                copy_alerts.notify_round(config, snapshot, state, moves, mkt, clock)
            except Exception as e:  # noqa: BLE001
                print(f"WARN: avisos a quem segue: {e!r}", file=sys.stderr)
        try:  # the portal at the prices of this moment
            league(config, state, mkt, f"🤖 Liga dos agentes · {mkt.as_of.isoformat()} às {now.astimezone(timezone.utc):%H:%M} UTC")
        except Exception as e:
            print(f"WARN: portal: {e!r}", file=sys.stderr)
        if not moves and not avoided or not detail_posts():  # the trades are in the portal
            return 0
        lines = [f"{cfg['emoji']} **{cfg['name']}** {'comprou' if t['side'] == 'compra' else 'vendeu'} {t['symbol']} "
                 f"({eur(t['eur'])}{', ' + t['reason'] if t['side'] == 'venda' and t.get('reason') else ''}) · às {t['hora']}"
                 for cfg, t in moves] + [f"⛔ {a}" for a in avoided]
        where = route("agentes") or {"thread_id": discord_bot.todays_thread()}
        return send([embed("⏱️ Ronda dos agentes", "\n".join(lines)[:4000], color=BLUE)], {}, dry_run=a.dry_run, **where)
    if a.mode == "portal":  # the portal's league page from the current state: no trading, no post
        mkt = load_market()
        embeds, _ = league(config, state, mkt, f"🤖 Liga dos agentes · fecho de {mkt.as_of.isoformat()}")
        print(f"Portal atualizado: {embeds[0].get('url', 'sem credenciais da Cloudflare')}")
        publish_calendar(config, state, mkt)
        publish_agent_list(config)
        holders: dict[str, list[str]] = {}
        for cfg in active_agents(config):
            for sym in (state["agents"].get(cfg["id"]) or {}).get("positions", {}):
                holders.setdefault(sym, []).append(f"{cfg['emoji']} {cfg['name']}")
        interactive.publish_section("market", {**opportunities.build(mkt, holders, earnings.next_earnings_many),
                                               "context": dict(mkt.context), "sectors": sector_table(mkt)})
        interactive.publish_section("fundamentals", fundamentals_table(mkt))
        import evolution

        interactive.publish_section("evolution", evolution.compact(evolution.load()))
        return 0
    try:
        if a.mode == "daily":
            mkt = load_market()
            ensure_twins(config, state, mkt.as_of)
            ensure_profiles(config, state, mkt.as_of)
            try:  # before the day: the dividends of yesterday's ex-dates are cash today
                paid = credit_dividends(config, state, mkt)
                print(f"Dividendos: {len(paid)} pagamentos creditados", flush=True)  # the details stay private
            except Exception as e:  # noqa: BLE001 - never stops the day
                print(f"WARN: dividendos: {e!r}", file=sys.stderr)
            moves, training, avoided, events = run_day(config, state, mkt)
            try:  # the agents under test, as if traded at Interactive Brokers (scripts/ibkr.py)
                import ibkr

                ibkr.virtual_round(config, state, mkt)
            except Exception as e:  # noqa: BLE001
                print(f"WARN: IBKR simulada: {e!r}", file=sys.stderr)
            save(config, state)  # bankruptcies retire agents and add their replacements
            decision_page(config, state, mkt, frozen_report(config, state, mkt))
            if not a.dry_run:
                try:  # the orders for the next open, to the members who follow each agent
                    import copy_alerts

                    copy_alerts.notify_orders(config, state, mkt)
                except Exception as e:  # noqa: BLE001
                    print(f"WARN: avisos a quem segue: {e!r}", file=sys.stderr)
            archive(config, state, mkt)
            embeds, files = league(config, state, mkt, f"🤖 Liga dos agentes · fecho de {mkt.as_of.isoformat()}",
                                   moves, training, avoided, events)
            ended = month_changed(state)
            if ended:
                month_embeds, month_files = monthly_report(config, state, ended)
                follow_up = (month_embeds + readiness_card(config, state), month_files)
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
    replace = {}
    if a.mode == "daily" and not target:
        target = route("agentes") or {"thread_id": discord_bot.todays_thread()}
        replace = {"replace": LEAGUE_TITLE}  # only the latest league chart stays in the channel
    buttons = [(interactive.BUTTON, embeds[0]["url"])] if embeds and embeds[0].get("url") else []
    if a.mode == "daily" and not a.dry_run:
        try:  # the portal's opportunities tab, from the market this run already loaded
            holders: dict[str, list[str]] = {}
            for cfg in active_agents(config):
                for sym in (state["agents"].get(cfg["id"]) or {}).get("positions", {}):
                    holders.setdefault(sym, []).append(f"{cfg['emoji']} {cfg['name']}")
            interactive.publish_section("market", {**opportunities.build(mkt, holders, earnings.next_earnings_many),
                                                   "context": {k: v for k, v in mkt.context.items()}, "sectors": sector_table(mkt)})
            interactive.publish_section("fundamentals", fundamentals_table(mkt))
            import proposals  # imports this module

            props = proposals.build(config, state, mkt)
            props["scoreboard"] = proposals.scoreboard(proposals.update_log(props, mkt))
            interactive.publish_section("proposals", props)
            publish_calendar(config, state, mkt, {b["symbol"] for b in props["buys"]})
            publish_agent_list(config)
            portal = interactive.portal_url()
            if portal:
                buttons.append(("🏟️ Abrir o portal", portal))
        except Exception as e:  # the league post matters more than the portal
            print(f"WARN: portal: {e!r}", file=sys.stderr)
    if a.mode == "daily" and replace and not detail_posts():  # the league and the month are in the portal
        print("Discord: liga do dia só no portal", flush=True)
        return 0
    status = send(embeds, files, dry_run=a.dry_run, buttons=buttons, **target, **replace)
    if follow_up[0]:
        status = status or send(*follow_up, dry_run=a.dry_run, **target, **({"replace": MONTHLY_TITLE} if replace else {}))
    return status


if __name__ == "__main__":
    sys.exit(main())
