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
from dataclasses import dataclass, field
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

import pandas as pd

import discord_bot
import earnings
import interactive
import opportunities
import strategy_docs
from charts import lines_chart, returns_chart
from references import reference_lines
from discord_daily import WATCHLIST_DIR
from discord_post import BLUE, GREEN, RED, embed, route, send
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

FEE = 0.001  # commission and slippage, per side
FX_FEE = 0.0015  # currency conversion at a typical EU broker, per side, for anything not quoted in euros
TAX_RATE = 0.28  # Portuguese tax on a year's net realised gains, paid on the first run of the next year
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
EXPOSURE = {"Alta": 1.0, "Neutra": 0.7, "Cautelosa": 0.4, "Queda": 0.1}
ZONE_ICON = {"Alta": "🟢", "Neutra": "🟡", "Cautelosa": "🟠", "Queda": "🔴"}
MUTATION = 0.2
EARNINGS_AVOID_DAYS = 5
EARNINGS_AVOID_DAYS_DEFAULT = EARNINGS_AVOID_DAYS
NEWS_VETO = 3  # no agent buys a stock whose 36 h news score is at or below -NEWS_VETO (peso_noticias tunes it)
NEWS_VETO_DEFAULT = NEWS_VETO
# Strategies that cannot be replayed on past data (no history of fundamentals, news or other agents):
# they learn by monthly mutation; the rest are re-trained every Sunday by pretrain.py.
# How an agent trades. Reactive (the default): decides in every intraday round with the price of that moment and
# trades at once in the markets that are open, while its plan for the next opens waits as pending orders. Daily: the
# twin of each reactive agent, deciding once a day before the opens (07:30), as in the 20-year pre-training.
REACTIVE, DAILY = "reativo", "diario"
LIVE_ONLY = {"joalheiro", "jornalista", "catalisador", "cetico", "conselho", "maestro"}
META = {"conselho", "maestro"}  # decide after the others, from their portfolios
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
    # Pêndulo: minimum momentum of the best equity ETF to stay in equities
    "limiar_mom": (0.0, 0.08, float),
}
# Variables every strategy (but the Indexador) has, on top of its own. The defaults keep the original
# behaviour; the training (pre-training, monthly mutation and the shadow copies) switches them on and tunes them.
# gap_max is on from the start (8%): a buy is cancelled when the stock opens that far from the close it was decided on.
COMMON = {"stop_atr": 0, "trail_atr": 0, "tempo_max": 0, "min_tecnico": 0, "max_ext": 0, "gap_max": 0.08,
          "peso_noticias": NEWS_VETO_DEFAULT,
          "vix_max": 0, "vix_meio": 0, "epu_max": 0, "credito_max": 0, "juros_max": 0, "usd_max": 0, "filtro_spy": 0,
          "gpr_max": 0, "petroleo_max": 0, "ouro_max": 0, "filtro_setor": 0, "ema_filtro": 0, "stop_suporte": 0,
          "qualidade_fund_min": 0, "valor_fund_min": 0,
          "dias_resultados": EARNINGS_AVOID_DAYS_DEFAULT, "evita_macro": 0, "evita_geo": 0}
OFFABLE = {"stop_atr", "trail_atr", "tempo_max", "min_tecnico", "max_ext", "gap_max", "vix_max", "vix_meio", "epu_max", "credito_max",
           "juros_max", "usd_max", "filtro_spy", "gpr_max", "petroleo_max", "ouro_max", "filtro_setor", "ema_filtro", "stop_suporte",
           "qualidade_fund_min", "valor_fund_min", "evita_macro", "evita_geo"}
CONTEXT_RULES = {"vix_max", "vix_meio", "epu_max", "credito_max", "juros_max", "usd_max", "filtro_spy", "gpr_max", "petroleo_max", "ouro_max"}
LIVE_TUNED = {"peso_noticias", "dias_resultados", "evita_macro", "evita_geo"}  # no history: not in the backtest
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
    closes: dict[str, float] | None = None  # as_of closes, precomputed by the backtest for speed
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
    """Whether the symbol's exchange is in session right now (started, and not over)."""
    from zoneinfo import ZoneInfo

    if exchange_of(symbol) == "=X":
        return True
    local_today = now.astimezone(ZoneInfo(EXCHANGE_CLOSE[exchange_of(symbol)][0])).date()
    return session_started(symbol, now) and last_complete_day(symbol, now) < local_today


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
                cut.iloc[-1, cut.columns.get_loc("Volume")] = (
                    float(cut["Volume"].iloc[-1]) / max(session_fraction(sym, now), LIVE_MIN_FRACTION))
        if len(cut):
            out[sym] = cut
    return out


PRICE_CACHE = Path(__file__).resolve().parents[1] / "state" / "prices"  # kept between rounds by actions/cache


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
        PRICE_CACHE.mkdir(parents=True, exist_ok=True)
        for old in PRICE_CACHE.glob("base-*.pkl"):
            if not old.name.startswith(f"base-{day}-"):
                old.unlink()
        path.write_bytes(pickle.dumps(base))
    fresh = download_history(tickers, period="5d")
    out = {}
    for t in tickers:
        b, f = base.get(t), fresh.get(t)
        if f is None or not len(f):
            if b is not None:
                out[t] = b
            continue
        out[t] = f if b is None else pd.concat([b[b.index < f.index[0]], f])
    return out


def load_market(now: datetime | None = None, live: bool = False) -> Market:
    """The market for a decision. Daily (live=False): complete sessions only, cut at the last full US session.
    Reactive rounds (live=True): up to this moment, today's sessions under way included (see live_bars)."""
    from zoneinfo import ZoneInfo

    now = now or datetime.now(timezone.utc)
    constituents = load_constituents()
    europe = load_europe()
    sector_of = {c["symbol"]: c["sector"] for c in constituents} | {c["symbol"]: c["sector"] for c in europe}
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
    fx_rates = {c: fx_hist[t]["Close"] for c, t in FX_SYMBOLS.items() if c != "USD" and t in fx_hist}
    missing = [s for s in sector_of if s not in stock_hist]
    if missing:
        print(f"WARN: {len(missing)} ações sem dados do Yahoo: {', '.join(missing[:40])}", file=sys.stderr)
    # Europe: only stocks whose currency can be converted, and only the most traded part of the list.
    probe = Market(hist={}, fx=fx_hist["EURUSD=X"]["Close"], stocks={}, etfs={}, as_of=as_of, sessions=[],
                   zone="Neutra", stage2_share=0.0, fx_rates=fx_rates)
    traded = {}
    for sym, df in stock_hist.items():
        if sym == "SPY" or is_us(sym) or fx_series(probe, currency_of(sym)) is None and not is_eur(sym):
            continue
        tail = df.iloc[-LIQUIDITY_DAYS:]
        traded[sym] = to_eur(probe, sym, float((tail["Close"] * tail["Volume"]).mean()), as_of)
    in_play = liquid_europe(traded)

    stocks = {}
    for sym, df in stock_hist.items():
        if sym not in sector_of or (not is_us(sym) and sym not in in_play):
            continue
        m = trend_metrics(df)
        if m:
            stocks[sym] = {**m, **extra_metrics(df), "sector": sector_of[sym], "name": name_of.get(sym, ""),
                            "stage2": is_stage2(m)}
    add_trend_scores(stocks)
    try:  # fundamentals as of today (SEC filings in data/fundamentals, today's price)
        import fundamentals

        funds = fundamentals.load()
        today = stock_hist["SPY"].index[-1].date()
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
        sessions=sorted({d.date() for d in spy.index} | ({as_of} if live and now.astimezone(ZoneInfo("Europe/Lisbon")).weekday() < 5 else set())),
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
BUY_TAX_EXEMPT = {"STMPA.PA", "AIR.PA", "RACE.MI", "STLAM.MI", "TEN.MI", "FER.MC", "GLEN.L"}


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


def to_eur(mkt: Market, symbol: str, price: float, on: date) -> float:
    ccy = currency_of(symbol)
    if ccy == "EUR":
        return price
    series = fx_series(mkt, ccy)
    if series is None or not len(series):
        raise KeyError(f"sem câmbio para {ccy} ({symbol})")
    rate = series.asof(pd.Timestamp(on))
    eur = price / float(series.iloc[0] if pd.isna(rate) else rate)
    return eur / 100 if ccy == "GBp" else eur


def fix_pence(df: pd.DataFrame) -> pd.DataFrame:
    """London quotes from Yahoo sometimes switch a day between pence and pounds (100x jumps): put them back in pence."""
    close = df["Close"]
    ref = close.rolling(11, center=True, min_periods=3).median()
    ratio = close / ref
    low, high = ratio < 0.02, ratio > 50
    if not (low.any() or high.any()):
        return df
    df = df.copy()
    for col in ("Open", "High", "Low", "Close"):
        df.loc[low, col] = df.loc[low, col] * 100
        df.loc[high, col] = df.loc[high, col] / 100
    return df


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


def fees(symbol: str) -> float:
    """Commission/slippage per side, plus currency conversion for anything not quoted in euros."""
    return FEE + (0.0 if is_eur(symbol) else FX_FEE)


def sell(port: dict, mkt: Market, symbol: str, price: float, on: date, reason: str, fraction: float = 1.0,
         ctx: dict | None = None) -> dict:
    pos = port["positions"][symbol]
    qty = pos["qty"] * fraction
    fill = price * (1 - fees(symbol))
    proceeds = qty * to_eur(mkt, symbol, fill, on)
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
    if order.get("ctx"):
        trade["ctx"] = order["ctx"]
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
        on, price = None, None
        for idx, bar in after.iterrows():  # the first session with a valid price (data can have broken bars)
            price = next((float(bar[c]) for c in ("Open", "Close") if pd.notna(bar[c]) and float(bar[c]) > 0), None)
            if price is not None:
                on = idx.date()
                break
        if price is None:
            still.append(order)
            continue
        if order["side"] == "sell":
            if order["symbol"] in port["positions"]:
                done.append(sell(port, mkt, order["symbol"], price, on, order.get("reason", ""),
                                 order.get("fraction", 1.0), order.get("ctx")))
        else:
            ref, limit = order.get("ref"), order.get("gap_max")
            if ref and limit and abs(price / ref - 1) > limit:  # opened too far from the decision's close: cancel
                port["cancelled"] = (port.get("cancelled", []) + [{
                    "date": on.isoformat(), "symbol": order["symbol"], "gap": round(price / ref - 1, 4),
                    "limit": limit}])[-50:]
                continue
            t = buy(port, mkt, order, price, on)
            if t:
                done.append(t)
    port["pending"] = still
    return done


def check_exits(port: dict, mkt: Market, open_day: date | None = None) -> list[dict]:
    """Stops and targets, bar by bar since each position was last checked.

    open_day: a session still under way (the intraday stop round). Its low and high so far can trigger an exit,
    at the same price the daily check would use, but it is not marked as checked: the full day is checked again.
    """
    done = []
    for symbol, pos in list(port["positions"].items()):
        df = mkt.hist.get(symbol)
        if df is None:
            continue
        bars = bars_between(df, date.fromisoformat(pos["checked"]), mkt.as_of)
        for idx, bar in bars.iterrows():
            on = idx.date()
            if pos.get("stop") and bar["Low"] <= pos["stop"]:
                done.append(sell(port, mkt, symbol, min(float(bar["Open"]), pos["stop"]), on, "stop",
                                 ctx={"stop": round(pos["stop"], 4), "entry": round(pos["entry"], 4), "low": round(float(bar["Low"]), 4)}))
                break
            if pos.get("tp") and bar["High"] >= pos["tp"]:
                done.append(sell(port, mkt, symbol, max(float(bar["Open"]), pos["tp"]), on, "alvo",
                                 ctx={"tp": round(pos["tp"], 4), "entry": round(pos["entry"], 4), "high": round(float(bar["High"]), 4)}))
                break
            if on != open_day:
                pos["checked"] = on.isoformat()
    return done


def pay_taxes(port: dict, mkt: Market) -> dict | None:
    """On the first run of a year, pay TAX_RATE on last year's net realised gains (losses pay nothing)."""
    hist = port["history"]
    if not hist or int(hist[-1]["date"][:4]) >= mkt.as_of.year:
        return None
    year = hist[-1]["date"][:4]
    gain = port.get("realized", {}).get(year, 0.0)
    if gain <= 0 or year in port.setdefault("taxed", []):
        return None
    tax = round(gain * TAX_RATE, 2)
    port["cash"] -= tax
    port["taxed"].append(year)
    trade = {"date": mkt.as_of.isoformat(), "side": "imposto", "symbol": "-", "qty": 0, "price": 0,
             "eur": tax, "pnl_eur": -tax, "reason": f"{TAX_RATE:.0%} sobre {gain:.0f} € de mais-valias de {year}"}
    port["trades"] = (port["trades"] + [trade])[-TRADES_KEPT:]
    return trade


def benchmark_return(mkt: Market, since: date, until: date) -> float:
    df = mkt.hist.get(BENCHMARK)
    if df is None:
        return 0.0
    close = df["Close"]
    a = close.asof(pd.Timestamp(since) + pd.Timedelta(hours=23))
    b = close.asof(pd.Timestamp(until) + pd.Timedelta(hours=23))
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
            params.setdefault(k, v)
    for k in [k for k in params if k in PARAM_BOUNDS]:
        lo, hi, kind = PARAM_BOUNDS[k]
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


def s_analista(cfg, port, mkt, p):
    """Quality + value + trend: solid, reasonably priced companies in an uptrend (point-in-time SEC fundamentals)."""
    if not due(port, mkt, p["cada"]):
        return []
    pool = [s for s, m in mkt.stocks.items() if m["stage2"] and m.get("fund_q") is not None and m.get("fund_v") is not None
            and m["fund_q"] >= p["qualidade_min"] and m["fund_v"] >= p["valor_min"]]
    wq, wv, wt = p.get("peso_qualidade", 1.0), p.get("peso_valor", 1.0), p.get("peso_tendencia", 1.0)
    score = lambda s: wq * mkt.stocks[s]["fund_q"] + wv * mkt.stocks[s]["fund_v"] + wt * mkt.stocks[s]["trend_score"]  # noqa: E731
    ranked = sorted(pool, key=score, reverse=True)
    n = p["n"]
    return rebalance(port, mkt, ranked[:n], exposure(mkt) / n, set(ranked[: 2 * n]), p["stop_pct"], "qualidade, valor e tendência")


def ranks(values: dict[str, float]) -> dict[str, float]:
    """Percentile rank 0-1 of each value within the group (ties share the order they come in)."""
    order = sorted(values, key=values.get)
    n = max(len(order) - 1, 1)
    return {k: i / n for i, k in enumerate(order)}


def s_quant(cfg, port, mkt, p):
    """Multi-factor: momentum, trend, quality, value and low volatility, each ranked within the S&P 500 and weighted
    by trainable weights; the pre-training searches for the mix that worked best."""
    if not due(port, mkt, p["cada"]):
        return []
    pool = {s: m for s, m in mkt.stocks.items() if not p.get("so_tendencia") or m["stage2"]}
    if not pool:
        return rebalance(port, mkt, [], 0, set(), p["stop_pct"], "multi-fator")
    mom = ranks({s: 0.6 * m["ret_6m"] + 0.4 * m["ret_3m"] for s, m in pool.items()})
    low_vol = ranks({s: -(m.get("vol20") or 0.03) for s, m in pool.items()})
    q = {s: (m["fund_q"] / 100 if m.get("fund_q") is not None else 0.5) for s, m in pool.items()}
    v = {s: (m["fund_v"] / 100 if m.get("fund_v") is not None else 0.5) for s, m in pool.items()}
    score = {s: p.get("peso_momentum", 1) * mom[s] + p.get("peso_tendencia", 1) * pool[s]["trend_score"] / 100
             + p.get("peso_qualidade", 1) * q[s] + p.get("peso_valor", 1) * v[s] + p.get("peso_baixa_vol", 1) * low_vol[s]
             for s in pool}
    ranked = sorted(score, key=score.get, reverse=True)
    n = p["n"]
    return rebalance(port, mkt, ranked[:n], exposure(mkt) / n, set(ranked[: 2 * n]), p["stop_pct"], "multi-fator")


def s_monge(cfg, port, mkt, p):
    """Low volatility: the calmest stocks above their 200-day average, with solid fundamentals when known."""
    if not due(port, mkt, p["cada"]):
        return []
    pool = [s for s, m in mkt.stocks.items() if m["price"] > m["sma200"] and m.get("vol20")
            and (m.get("fund_q") is None or m["fund_q"] >= p["qualidade_min"])]
    ranked = sorted(pool, key=lambda s: mkt.stocks[s]["vol20"])
    n = p["n"]
    return rebalance(port, mkt, ranked[:n], exposure(mkt) / n, set(ranked[: 3 * n]), p["stop_pct"], "baixa volatilidade")


def s_abutre(cfg, port, mkt, p):
    """Fallen quality: solid, cheap companies well below their high that start turning up (above the 21-day EMA).
    Sells near the old high, at the stop or after max_dias sessions."""
    orders = []
    for s, pos in port["positions"].items():
        m = mkt.stocks.get(s)
        if sessions_since(mkt, pos["entry_date"]) >= p["max_dias"]:
            orders.append(order_sell(s, f"{p['max_dias']} sessões"))
        elif m and m["pct_from_high"] <= 0.05:
            orders.append(order_sell(s, "alvo: voltou perto do máximo"))
    if mkt.zone == "Queda":
        return orders
    picks = [s for s, m in mkt.stocks.items()
             if s not in port["positions"] and m["pct_from_high"] >= p["queda_min"] and m.get("ema21") and m["price"] > m["ema21"]
             and m.get("fund_q") is not None and m["fund_q"] >= p["qualidade_min"]
             and m.get("fund_v") is not None and m["fund_v"] >= p["valor_min"]]
    eq = equity(port, mkt)
    picks.sort(key=lambda s: mkt.stocks[s]["fund_q"] + mkt.stocks[s]["fund_v"], reverse=True)
    for s in picks[: max(free_slots(port, p["n"]), 0)]:
        price = mkt.stocks[s]["price"]
        orders.append(order_buy(s, eq / p["n"], f"{mkt.stocks[s]['pct_from_high'] * 100:.0f}% abaixo do máximo, qualidade e valor altos",
                                stop=round(price * (1 - p["stop_pct"]), 4)))
    return orders


DEFENSIVE_ETFS = ("EUNH.DE", "VAGF.DE", "4GLD.DE")  # euro government bonds, global bonds, gold


def s_pendulo(cfg, port, mkt, p):
    """Dual momentum: the strongest equity ETFs while they keep rising, otherwise the strongest of bonds and gold."""
    if not due(port, mkt, p["cada"]):
        return []
    mom = {s: 0.5 * m["ret_6m"] + 0.5 * m["ret_3m"] for s, m in mkt.etfs.items()}
    equity_etfs = sorted((s for s in mom if s not in DEFENSIVE_ETFS), key=mom.get, reverse=True)
    defensive = sorted((s for s in mom if s in DEFENSIVE_ETFS), key=mom.get, reverse=True)
    n = p["n"]
    if equity_etfs and mom[equity_etfs[0]] > p["limiar_mom"]:
        targets, why = equity_etfs[:n], "momentum: ações"
    elif defensive:
        targets, why = defensive[:1], "momentum: refúgio (obrigações ou ouro)"
    else:
        targets, why = [], "momentum: dinheiro"
    return rebalance(port, mkt, targets, 1.0 / max(len(targets), 1), set(), None, why)


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
    strength = {sec: sum(mkt.stocks[s]["stage2"] for s in syms) / len(syms)
                for sec, syms in by_sector.items() if len(syms) >= 5 and sec != "Unknown"}
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


def s_deriva(cfg, port, mkt, p):
    """Post-gap drift: buy stocks that gapped up on heavy volume in the last days and held the gap."""
    orders = [order_sell(s, f"{p['max_dias']} sessões") for s, pos in port["positions"].items()
              if sessions_since(mkt, pos["entry_date"]) >= p["max_dias"]]
    if mkt.zone == "Queda":
        return orders
    picks = [
        s for s, m in mkt.stocks.items()
        if m["gap_size"] >= p["salto_pct"] and 0 <= m["gap_age"] <= 3 and m["price"] > m["gap_low"]
        and m["price"] > m["sma50"] and s not in port["positions"]
    ]
    eq = equity(port, mkt)
    for s in sorted(picks, key=lambda s: mkt.stocks[s]["gap_size"], reverse=True)[: max(free_slots(port, p["n"]), 0)]:
        m = mkt.stocks[s]
        orders.append(order_buy(s, eq / p["n"], f"salto de {m['gap_size'] * 100:.0f}% com volume",
                                stop=round(m["gap_low"] * 0.99, 4)))
    return orders


def s_jornalista(cfg, port, mkt, p):
    """Trade the news: buy strong positive, important news; sell on negative news or after max_dias."""
    orders = [order_sell(s, "notícias negativas") for s in port["positions"]
              if mkt.news.get(s, {}).get("score", 0) <= -p["limiar"] / 2]
    orders += [order_sell(s, f"{p['max_dias']} sessões") for s, pos in port["positions"].items()
               if sessions_since(mkt, pos["entry_date"]) >= p["max_dias"] and s not in {o["symbol"] for o in orders}]
    if mkt.zone == "Queda":
        return orders
    picks = [s for s, sig in mkt.news.items()
             if sig.get("score", 0) >= p["limiar"] and sig.get("importance", 0) >= 2
             and s in mkt.stocks and s not in port["positions"]]
    eq = equity(port, mkt)
    for s in sorted(picks, key=lambda s: mkt.news[s]["score"], reverse=True)[: max(free_slots(port, p["n"]), 0)]:
        price = mkt.stocks[s]["price"]
        headline = mkt.news[s]["items"][0]["resumo"] if mkt.news[s].get("items") else "notícias positivas"
        orders.append(order_buy(s, eq / p["n"], headline[:80], stop=round(price * (1 - p["stop_pct"]), 4)))
    return orders


def news_score(mkt: Market, symbol: str) -> int:
    return mkt.news.get(symbol, {}).get("score", 0)


def s_catalisador(cfg, port, mkt, p):
    """News confirmed by the market: important positive news plus a price reaction on volume."""
    orders = [order_sell(s, "notícias negativas") for s in port["positions"] if news_score(mkt, s) <= -NEWS_VETO]
    orders += [order_sell(s, f"{p['max_dias']} sessões") for s, pos in port["positions"].items()
               if sessions_since(mkt, pos["entry_date"]) >= p["max_dias"] and s not in {o["symbol"] for o in orders}]
    if mkt.zone == "Queda":
        return orders
    picks = []
    for s, sig in mkt.news.items():
        m = mkt.stocks.get(s)
        if not m or s in port["positions"] or sig.get("score", 0) < p["limiar"] or sig.get("importance", 0) < 2:
            continue
        reacted = m["ret_1d"] > 0 and (m["vol_ratio"] >= 1.5 or (m["gap_age"] == 0 and m["gap_size"] > 0))
        if reacted and m["price"] > m["sma50"]:
            picks.append(s)
    eq = equity(port, mkt)
    for s in sorted(picks, key=lambda s: mkt.news[s]["score"], reverse=True)[: max(free_slots(port, p["n"]), 0)]:
        m = mkt.stocks[s]
        stop = max(m["low10_prev"], m["price"] * (1 - p["stop_pct"]))
        orders.append(order_buy(s, eq / p["n"], "notícia confirmada pelo preço", stop=round(stop, 4)))
    return orders


def s_cetico(cfg, port, mkt, p):
    """Adversarial: important bad news that the market shrugged off, in a stock still in an uptrend."""
    orders = [order_sell(s, f"{p['max_dias']} sessões") for s, pos in port["positions"].items()
              if sessions_since(mkt, pos["entry_date"]) >= p["max_dias"]]
    if mkt.zone == "Queda":
        return orders
    picks = [s for s, sig in mkt.news.items()
             if sig.get("score", 0) <= -p["limiar"] and sig.get("importance", 0) >= 2
             and (m := mkt.stocks.get(s)) and m["stage2"] and m["ret_1d"] >= 0 and s not in port["positions"]]
    eq = equity(port, mkt)
    for s in sorted(picks, key=lambda s: mkt.news[s]["score"])[: max(free_slots(port, p["n"]), 0)]:
        price = mkt.stocks[s]["price"]
        orders.append(order_buy(s, eq / p["n"], "o mercado ignorou a má notícia",
                                stop=round(price * (1 - p["stop_pct"]), 4), tp=round(price * (1 + p["alvo_pct"]), 4)))
    return orders


def mode_of(cfg: dict) -> str:
    return cfg.get("modo", REACTIVE)


def peers(mkt: Market, cfg: dict) -> list[tuple[dict, dict]]:
    """The other agents of the same mode that decide on their own (not meta-agents, not this one)."""
    return [(c, port) for agent_id, (c, port) in mkt.league.items()
            if agent_id != cfg.get("id") and c["strategy"] not in META and mode_of(c) == mode_of(cfg)]


def s_conselho(cfg, port, mkt, p):
    """Collective intelligence: hold what at least min_votos agents hold or are buying, without bad news."""
    votes: dict[str, int] = {}
    for _, other in peers(mkt, cfg):
        for s in set(other["positions"]) | {o["symbol"] for o in other["pending"] if o["side"] == "buy"}:
            votes[s] = votes.get(s, 0) + 1
    orders = [order_sell(s, "o conselho deixou de a apoiar") for s in port["positions"]
              if votes.get(s, 0) < 1 or news_score(mkt, s) <= -NEWS_VETO]
    picks = [s for s, v in votes.items() if v >= p["min_votos"] and s not in port["positions"]
             and news_score(mkt, s) > -NEWS_VETO and metrics(mkt, s)]
    picks.sort(key=lambda s: (votes[s], metrics(mkt, s).get("trend_score", 0)), reverse=True)
    eq = equity(port, mkt)
    for s in picks[: max(free_slots(port, p["n"]), 0)]:
        price = last_close(mkt, s)
        orders.append(order_buy(s, eq / p["n"], f"{votes[s]} agentes de acordo",
                                stop=round(price * (1 - p["stop_pct"]), 4) if price else None))
    return orders


def recent_return(port: dict, sessions: int) -> float | None:
    h = port["history"][-(sessions + 1):]
    if len(h) < 5 or not h[0]["equity"]:
        return None
    return (h[-1]["equity"] - h[0]["equity"] - (h[-1]["deposits"] - h[0]["deposits"])) / h[0]["equity"]


ZONE_MIN_DAYS = 15  # live days in a zone before the Maestro trusts an agent's record there over its pre-training


def regime_score(mkt: Market, cfg: dict, port: dict, window: int) -> float | None:
    """How well an agent does in the current market zone: average daily return above the S&P 500.

    Live record in this zone when it has ZONE_MIN_DAYS, else the pre-training average for its strategy in this
    zone, else its recent return.
    """
    total, days = port.get("zone_stats", {}).get(mkt.zone, [0.0, 0])
    if days >= ZONE_MIN_DAYS:
        return total / days
    prior = mkt.priors.get(cfg["strategy"], {}).get(mkt.zone)
    if prior is not None:
        return prior
    recent = recent_return(port, window)
    return None if recent is None else recent / max(window, 1)


def s_maestro(cfg, port, mkt, p):
    """Central coordinator: copy the agents that do best in the current market regime."""
    if not due(port, mkt, p["cada"]):
        return []
    ranked = sorted(((regime_score(mkt, c, o, p["janela"]), c, o) for c, o in peers(mkt, cfg)),
                    key=lambda x: -999 if x[0] is None else x[0], reverse=True)
    leaders = [(c, o) for r, c, o in ranked[: p["lideres"]] if r is not None and r > 0 and o["positions"]]
    if not leaders:
        return [order_sell(s, f"nenhum agente bate o S&P 500 em zona {mkt.zone}") for s in port["positions"]]
    weights: dict[str, float] = {}
    for _, other in leaders:
        other_eq = equity(other, mkt)
        for s, pos in other["positions"].items():
            weights[s] = weights.get(s, 0.0) + position_value(mkt, s, pos) / other_eq / len(leaders)
    targets = sorted(weights, key=weights.get, reverse=True)[: p["n"]]
    total = sum(weights[s] for s in targets)
    scale = 1.0 / total if total > 1 else 1.0
    orders = [order_sell(s, "saiu das carteiras dos líderes") for s in port["positions"] if s not in targets]
    eq = equity(port, mkt)
    names = ", ".join(c["name"] for c, _ in leaders)
    for s in targets:
        if s not in port["positions"]:
            orders.append(order_buy(s, eq * weights[s] * scale, f"zona {mkt.zone}: segue {names}"))
    return orders


STRATEGIES = {
    "indexador": (s_indexador, "Compra e mantém o S&P 500 (referência)", {}),
    "guardiao": (s_guardiao, "ETF mundial com peso pela zona do mercado", {"max_exposicao": 1.0, "folga": 0.1}),
    "rotador": (s_rotador, "Roda entre os melhores ETFs UCITS", {"n": 3, "cada": 5}),
    "foguetao": (s_foguetao, "Top ações por score de tendência", {"n": 5, "stop_pct": 0.10, "cada": 5}),
    "joalheiro": (s_joalheiro, "Melhores do ranking com qualidade alta", {"n": 6, "qualidade_min": 70, "stop_pct": 0.15, "cada": 20}),
    "analista": (s_analista, "Empresas sólidas e baratas em tendência de alta (fundamentos da SEC)",
                 {"n": 6, "qualidade_min": 60, "valor_min": 40, "stop_pct": 0.15, "cada": 10,
                  "peso_qualidade": 1.0, "peso_valor": 1.0, "peso_tendencia": 1.0}),
    "quant": (s_quant, "Multi-fator: momentum, tendência, qualidade, valor e baixa volatilidade com pesos treinados",
              {"n": 8, "cada": 10, "stop_pct": 0.15, "so_tendencia": 1, "peso_momentum": 1.0, "peso_tendencia": 1.0,
               "peso_qualidade": 1.0, "peso_valor": 1.0, "peso_baixa_vol": 1.0}),
    "monge": (s_monge, "Baixa volatilidade: as ações mais calmas acima da média de 200 dias, com bons fundamentos",
              {"n": 10, "cada": 20, "stop_pct": 0.12, "qualidade_min": 50}),
    "abutre": (s_abutre, "Quedas em empresas sólidas e baratas que começam a recuperar",
               {"n": 5, "queda_min": 0.25, "qualidade_min": 60, "valor_min": 50, "stop_pct": 0.12, "max_dias": 60}),
    "pendulo": (s_pendulo, "Momentum duplo em ETFs: ações enquanto sobem, obrigações ou ouro quando não",
                {"n": 2, "cada": 20, "limiar_mom": 0.0}),
    "cacador": (s_cacador, "Rompimentos com volume", {"n": 5, "risco_pct": 0.02, "alvo_r": 3.0, "max_pos": 0.30}),
    "sniper": (s_sniper, "Contração de volatilidade (VCP)", {"n": 4, "risco_pct": 0.01, "aperto_pct": 0.06, "max_pos": 0.30}),
    "campeao": (s_campeao, "Melhores ações dos setores mais fortes", {"setores": 2, "por_setor": 2, "stop_pct": 0.12, "cada": 5}),
    "pescador": (s_pescador, "Compra quedas em ações em tendência", {"n": 5, "queda_pct": 0.05, "alvo_pct": 0.06, "stop_pct": 0.07, "max_dias": 10}),
    "jornalista": (s_jornalista, "Negoceia notícias analisadas pelo Claude", {"n": 5, "limiar": 4, "stop_pct": 0.08, "max_dias": 10}),
    "deriva": (s_deriva, "Segue saltos com volume (deriva pós-notícia/resultados)", {"n": 5, "salto_pct": 0.04, "max_dias": 20}),
    "catalisador": (s_catalisador, "Notícias positivas confirmadas pelo preço", {"n": 5, "limiar": 3, "stop_pct": 0.08, "max_dias": 15}),
    "cetico": (s_cetico, "Más notícias que o mercado ignorou", {"n": 4, "limiar": 3, "stop_pct": 0.06, "alvo_pct": 0.08, "max_dias": 10}),
    "conselho": (s_conselho, "Compra o que vários agentes apoiam", {"n": 6, "min_votos": 2, "stop_pct": 0.10}),
    "maestro": (s_maestro, "Copia os agentes que melhor se portam no regime de mercado atual", {"n": 8, "lideres": 3, "janela": 20, "cada": 5}),
    "kamikaze": (s_kamikaze, "Tudo nas maiores subidas a 3 meses", {"n": 2, "stop_pct": 0.20, "cada": 5, "ignora_resultados": True, "ignora_risco": True}),
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


def skip_earnings(orders: list[dict], mkt: Market, days: int = EARNINGS_AVOID_DAYS) -> tuple[list[dict], list[str]]:
    """Drop buys of stocks that report within `days` (EARNINGS_AVOID_DAYS by default); return (kept, skipped)."""
    kept, skipped = [], []
    for o in orders:
        if o["side"] == "buy" and o["symbol"] not in ETF_SYMBOLS and o["symbol"] not in mkt.etfs:  # ETFs report no results
            if o["symbol"] not in mkt.earnings:
                mkt.earnings[o["symbol"]] = earnings.next_earnings(o["symbol"], mkt.as_of)
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


def sector_state(mkt: Market, sector: str | None) -> tuple[float | None, float | None]:
    """(sector ETF vs its 200-day average, sector 3-month return minus the S&P 500's) from the context, or Nones."""
    import context

    key = context.sector_key(sector or "")
    if not key:
        return None, None
    return mkt.context.get(f"{key}_trend"), mkt.context.get(f"{key}_rs")


def stock_filter(m: dict, mkt: Market, p: dict) -> str | None:
    """Why a stock fails the sector, moving-average and fundamental rules, or None. Missing data never blocks."""
    if p.get("filtro_setor") and m.get("sector"):
        trend, rs = sector_state(mkt, m["sector"])
        sector = SECTORS_PT.get(m["sector"], m["sector"])
        if trend is not None and trend < 0:
            return f"setor {sector} abaixo da média de 200 dias"
        if p["filtro_setor"] >= 2 and rs is not None and rs < 0:
            return f"setor {sector} mais fraco do que o S&P 500 em 3 meses"
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
            why = stock_filter(m, mkt, p)
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
    """The strategy's orders after the shared rules: earnings (live only), bad news, risk. Same live and in backtests."""
    params = effective(params, mkt.zone, cfg["strategy"])
    orders = STRATEGIES[cfg["strategy"]][0](cfg, port, mkt, params)
    notes = []
    if cfg["strategy"] != "indexador":
        orders, skipped = shared_rules(orders, port, mkt, params)
        notes += skipped
    if live and not params.get("ignora_resultados"):
        orders, skipped = skip_earnings(orders, mkt, int(params.get("dias_resultados", EARNINGS_AVOID_DAYS)))
        notes += [f"{s} (resultados {earnings.label(mkt.earnings[s])})" for s in skipped]
    if not params.get("ignora_noticias") and cfg["strategy"] != "cetico":
        orders, skipped = skip_bad_news(orders, mkt, int(params.get("peso_noticias") or NEWS_VETO))
        notes += [f"{s} (notícias negativas)" for s in skipped]
    if not params.get("ignora_risco"):
        orders, skipped = risk_filter(orders, port, mkt)
        notes += skipped
    orders = [o for o in orders if o["side"] == "sell" or o["eur"] >= MIN_ORDER_EUR]
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
        trend, _ = sector_state(mkt, m.get("sector"))
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
    port["evolution"] = (port["evolution"] + [{"date": mkt.as_of.isoformat(), "note": note}])[-24:]
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
        cancelled = len(port.get("cancelled", []))
        for t in fill_pending(port, mkt) + check_exits(port, mkt) + [x for x in [pay_taxes(port, mkt)] if x]:
            moves.append((cfg, t))
        gaps = port.get("cancelled", [])[cancelled:]
        if gaps:
            avoided.append(f"{cfg['emoji']} {cfg['name']}: compra cancelada por gap na abertura: "
                           + ", ".join(f"{g['symbol']} ({g['gap']:+.1%})" for g in gaps))
        trail_stops(port, mkt, effective(params, mkt.zone, cfg["strategy"]))
        score_day(port, mkt)
        if cfg["strategy"] != "indexador" and is_bankrupt(port, mkt):
            moves += [(cfg, t) for t in liquidate(port, mkt, "falência")]
            cfg["active"], cfg["retired"] = False, mkt.as_of.isoformat()
            state.get("shadows", {}).pop(cfg["id"], None)
            mkt.league.pop(cfg["id"], None)
            bankrupt.append(cfg)
            continue
        note = train(cfg, port, mkt)
        if note:
            training.append(f"{cfg['emoji']} **{cfg['name']}** {note}")
        if mode_of(cfg) == REACTIVE:  # it decides in the intraday rounds (reactive_round)
            continue
        if port["last_decision"] != mkt.as_of.isoformat() and not port["pending"]:
            orders, notes = decide(cfg, port, mkt, params)
            if notes:
                avoided.append(f"{cfg['emoji']} {cfg['name']}: " + ", ".join(notes))
            port["pending"] = [{**o, "created": mkt.as_of.isoformat()} for o in orders]
            port["last_decision"] = mkt.as_of.isoformat()
        note = shadow_day(cfg, port, state, mkt)
        if note:
            training.append(f"{cfg['emoji']} **{cfg['name']}** {note}")
    for dead in bankrupt:
        alive = [(c, state["agents"][c["id"]]) for c in active_agents(config)
                 if c["strategy"] not in META | {"indexador"} and mode_of(c) == mode_of(dead)]
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


def day_change(port: dict) -> float:
    h = port["history"]
    if len(h) < 2 or not h[-2]["equity"]:
        return 0.0
    return (h[-1]["equity"] - (h[-1]["deposits"] - h[-2]["deposits"])) / h[-2]["equity"] - 1


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
            f" · hoje {pct(day_change(port))} · ⭐ **{port['points']}** · {len(port['positions'])} pos."
        )
    fields = [("🧭 Mercado", f"{ZONE_ICON[mkt.zone]} zona **{mkt.zone}** · {mkt.stage2_share * 100:.0f}% das ações do S&P 500 em tendência de alta")]
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

    series = {
        cfg["name"]: [(h["date"], (h["equity"] / h["deposits"] - 1) * 100) for h in port["history"]]
        for cfg, port, _ in rows
    }
    refs = reference_lines(series)
    top = interactive.leaders(series)
    files = {"agentes.png": lines_chart(series, "Rentabilidade desde o início (a cores: os 5 melhores e o Indexador)",
                                        refs, top)}
    link = interactive.publish(title, series, refs, top, subtitle="Rentabilidade desde o início, sobre o dinheiro depositado",
                               agents=agent_details(config, state, mkt), kind="liga")
    leader = rows[0][2] / rows[0][1]["deposits"] - 1 if rows else 0
    card = embed(
        title,
        "\n".join(lines),
        color=GREEN if leader >= 0 else RED,
        fields=fields,
        image="agentes.png",
        footer="Simulação com dinheiro fictício e preços reais: ordens à abertura seguinte, 0,1% de custos (+0,15% câmbio "
        "nas ações dos EUA), 28% de imposto sobre as mais-valias de cada ano. Pontos: ±1 por cada 0,1% acima/abaixo do "
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


def readiness_card(config: dict, state: dict) -> list[dict]:
    path = REPO_AGENTS / "pretrain.json"
    pre = json.loads(path.read_text())["strategies"] if path.exists() else {}
    bench_cfg = next((c for c in active_agents(config) if c["strategy"] == "indexador"), None)
    bench = live_stats(state["agents"][bench_cfg["id"]]) if bench_cfg and bench_cfg["id"] in state["agents"] else None
    icon = {True: "✅", False: "❌", None: "➖"}
    lines, ready = [], []
    for cfg in active_agents(config):
        port = state["agents"].get(cfg["id"])
        if not port or cfg["strategy"] == "indexador":
            continue
        checks = readiness(cfg, port, bench, pre.get(cfg["strategy"], {}).get("monte_carlo"))
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
        if params.get("por_zona"):
            rules.append("🧭 parâmetros próprios por zona de mercado: " + "; ".join(
                f"{z}: " + ", ".join(f"{k} {v}" for k, v in d.items()) for z, d in params["por_zona"].items()))
    rules.append(f"💶 custos {FEE:.1%} (+{FX_FEE:.2%} câmbio fora do euro; na compra, impostos de bolsa de França, Itália, Espanha, "
                 f"Reino Unido e Irlanda), {TAX_RATE:.0%} de imposto sobre mais-valias anuais")
    if strategy != "indexador":
        rules.append(f"💀 falência se perder {BANKRUPTCY:.0%}: o lugar passa para um filho do líder")
    return "\n".join(rules)


def pretrain_text(strategy: str) -> str:
    path = REPO_AGENTS / "pretrain.json"
    saved = json.loads(path.read_text()) if path.exists() else {}
    r = saved.get("strategies", {}).get(strategy)
    if not r:
        return "Ainda não pré-treinado."
    if "skipped" in r:
        return f"Não testável no passado: {r['skipped']}. Aprende ao vivo (ajusta parâmetros nos meses em que perde)."
    y, v = r["year"], r["valid"]
    span = saved.get("train", [None])[0], saved.get("valid", [None, None])[1]
    years = (round((date.fromisoformat(span[1]) - date.fromisoformat(span[0])).days / 365.25)
             if span[0] and span[1] else None)
    period = f"{years} anos" if years else "Período"
    verdict = "✅ adotou os parâmetros do treino" if r["adopted"] else "↩️ mantém os parâmetros originais"
    return (f"{period}: {pct(y['return'])} · queda máx. {pct(-y['max_drawdown'])} · Sharpe {y.get('sharpe', 0):.1f} · "
            f"{y['trades']} operações\nExame (último ano): {v['best']['points']} vs {v['default']['points']} pts "
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
        ("🧠 Pré-treino", pretrain_text(cfg["strategy"])),
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
        "pretrain": pretrain_text(cfg["strategy"]),
    }


def position_history(trades: list[dict], strategy: str, params: dict, kept: int = 60) -> list[dict]:
    """The trades grouped into positions (first buy to last sell), newest first, each trade with its justification."""
    lots, open_lot = [], {}
    for t in trades:
        if t["side"] not in ("compra", "venda"):
            continue
        t = {**t, "why": strategy_docs.justify(strategy, params, t)}
        lot = open_lot.get(t["symbol"])
        if lot is None:
            lot = {"symbol": t["symbol"], "opened": t["date"], "closed": None, "buys": [], "sells": [], "qty": 0.0,
                   "invested": 0.0, "result": 0.0, "partial": t["side"] == "venda"}
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
                lot["closed"] = t["date"]
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
        stats = live_stats(port)
        hist = port["history"]
        peak, max_dd = 0.0, 0.0
        for h in hist:
            ratio = h["equity"] / h["deposits"] if h["deposits"] else 1.0
            peak = max(peak, ratio)
            max_dd = max(max_dd, 1 - ratio / peak if peak else 0.0)
        out.append({
            "id": cfg["id"], "name": cfg["name"], "emoji": cfg["emoji"], "strategy": strategy_info(cfg, params),
            "started": port.get("started"), "generation": port.get("generation", 1),
            "lineage": cfg.get("lineage_name"), "points": port["points"],
            "deposits": round(port["deposits"], 2), "cash": round(port["cash"], 2), "equity": round(eq, 2),
            "return_pct": round((eq / port["deposits"] - 1) * 100, 2) if port["deposits"] else 0.0,
            "day_pct": round(day_change(port) * 100, 2), "max_dd_pct": round(max_dd * 100, 2),
            "sharpe": round(stats["sharpe"], 2), "sessions": stats["sessions"],
            "invested_pct": round((eq - port["cash"]) / eq * 100, 2) if eq else 0.0,
            "realized": port.get("realized", {}),
            "taxes": round(sum(t["eur"] for t in trades if t["side"] == "imposto"), 2),
            "fees_note": f"{FEE:.1%} por ordem, +{FX_FEE:.2%} de câmbio fora do euro, + imposto de bolsa na compra em FR 0,4%, "
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


def ensure_twins(config: dict, state: dict, start: date) -> list[dict]:
    """A daily twin ("· diário") for every reactive agent that has none (the Indexador needs none): same strategy and
    parameters, from the same capital, so the league shows what reacting during the day is worth."""
    twinned = {a.get("twin_of") for a in config["agents"] if mode_of(a) == DAILY}
    created = []
    for cfg in list(active_agents(config)):
        if mode_of(cfg) == DAILY or cfg["strategy"] == "indexador" or cfg["id"] in twinned:
            continue
        twin = {**{k: v for k, v in cfg.items() if k not in ("id", "name", "description")},
                "id": f"{cfg['id']}-diario", "name": f"{cfg['name']} · diário", "modo": DAILY, "twin_of": cfg["id"],
                "description": f"Gémeo diário de {cfg['name']}: a mesma estratégia, mas decide uma vez por dia antes "
                               "das aberturas (07:30), como no pré-treino. Serve de comparação com o modo reativo."}
        port = new_portfolio(cfg["capital"], start)
        port["params"] = dict((state["agents"].get(cfg["id"]) or {}).get("params", cfg["params"]))
        config["agents"].append(twin)
        state["agents"][twin["id"]] = port
        created.append(twin)
    return created


def live_exits(port: dict, mkt: Market, now: datetime) -> list[dict]:
    """Stops and targets of positions bought during today's session: today's low and high may come from before the
    purchase, so these are checked against the price of each round instead (and sold at it)."""
    done = []
    for symbol, pos in list(port["positions"].items()):
        if pos.get("checked", "") < mkt.as_of.isoformat() or not market_open(symbol, now):
            continue
        price = last_close(mkt, symbol)
        if price is None:
            continue
        if pos.get("stop") and price <= pos["stop"]:
            done.append(sell(port, mkt, symbol, price, mkt.as_of, "stop",
                             ctx={"stop": round(pos["stop"], 4), "entry": round(pos["entry"], 4), "price": round(price, 4)}))
        elif pos.get("tp") and price >= pos["tp"]:
            done.append(sell(port, mkt, symbol, price, mkt.as_of, "alvo",
                             ctx={"tp": round(pos["tp"], 4), "entry": round(pos["entry"], 4), "price": round(price, 4)}))
    return done


def react(cfg: dict, port: dict, mkt: Market, params: dict, now: datetime) -> tuple[list[dict], list[str]]:
    """One reactive decision: the strategy runs with the prices of this moment. Orders in markets that are open are
    traded now, at the current price; the others join the plan of pending orders for the next open (at the open
    price, with the gap rule). The money of pending buys stays reserved, a symbol already pending is left to its
    order, and a stock sold today is not bought back the same day."""
    today = mkt.as_of.isoformat()
    sold = {t["symbol"] for t in port["trades"] if t["side"] == "venda" and t["date"] == today}
    pending = {o["symbol"] for o in port["pending"]}
    reserved = sum(o["eur"] for o in port["pending"] if o["side"] == "buy")
    port["cash"] -= reserved
    try:
        orders, notes = decide(cfg, port, mkt, params)
    finally:
        port["cash"] += reserved
    done = []
    for o in sorted(orders, key=lambda o: o["side"] != "sell"):  # sells first, so their cash is there for the buys
        sym = o["symbol"]
        if sym in pending or (o["side"] == "buy" and sym in sold):
            continue
        if market_open(sym, now):
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
    port["last_decision"] = now.isoformat(timespec="minutes")
    return done, notes


def reactive_round(config: dict, state: dict, mkt: Market, now: datetime) -> tuple[list[tuple[dict, dict]], list[str]]:
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
        if not port:
            continue
        params = port.setdefault("params", dict(cfg["params"]))
        done = fill_pending(port, mkt) + check_exits(port, mkt, open_day=mkt.as_of) + live_exits(port, mkt, now)
        if mode_of(cfg) == REACTIVE:
            traded, notes = react(cfg, port, mkt, params, now)
            done += traded
            if notes:
                avoided.append(f"{cfg['emoji']} {cfg['name']}: " + ", ".join(notes))
        for t in done:
            t["hora"] = clock
            moves.append((cfg, t))
    return moves, avoided


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
        twins = ensure_twins(config, state, mkt.as_of)
        moves, _ = reactive_round(config, state, mkt, now)
        print(f"Ronda: decisões feitas em {(datetime.now(timezone.utc) - now).total_seconds():.0f} s desde o início")
        if not moves and not twins:
            return 0
        save(config, state)
        try:  # the portal at the prices of this moment
            league(config, state, mkt, f"🤖 Liga dos agentes · {mkt.as_of.isoformat()} às {now.astimezone(timezone.utc):%H:%M} UTC")
        except Exception as e:
            print(f"WARN: portal: {e!r}", file=sys.stderr)
        if not moves:
            return 0
        lines = [f"{cfg['emoji']} **{cfg['name']}** {'comprou' if t['side'] == 'compra' else 'vendeu'} {t['symbol']} "
                 f"({eur(t['eur'])}{', ' + t['reason'] if t['side'] == 'venda' and t.get('reason') else ''}) · às {t['hora']}"
                 for cfg, t in moves]
        where = route("agentes") or {"thread_id": discord_bot.todays_thread()}
        return send([embed("⏱️ Ronda dos agentes", "\n".join(lines)[:4000], color=BLUE)], {}, dry_run=a.dry_run, **where)
    if a.mode == "portal":  # the portal's league page from the current state: no trading, no post
        mkt = load_market()
        embeds, _ = league(config, state, mkt, f"🤖 Liga dos agentes · fecho de {mkt.as_of.isoformat()}")
        print(f"Portal atualizado: {embeds[0].get('url', 'sem credenciais da Cloudflare')}")
        publish_calendar(config, state, mkt)
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
            moves, training, avoided, events = run_day(config, state, mkt)
            save(config, state)  # bankruptcies retire agents and add their replacements
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
            portal = interactive.portal_url()
            if portal:
                buttons.append(("🏟️ Abrir o portal", portal))
        except Exception as e:  # the league post matters more than the portal
            print(f"WARN: portal: {e!r}", file=sys.stderr)
    status = send(embeds, files, dry_run=a.dry_run, buttons=buttons, **target, **replace)
    if follow_up[0]:
        status = status or send(*follow_up, dry_run=a.dry_run, **target, **({"replace": MONTHLY_TITLE} if replace else {}))
    return status


if __name__ == "__main__":
    sys.exit(main())
