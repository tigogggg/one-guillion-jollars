"""Intraday lab: is reacting during the day worth it? (US stocks, Alpaca's 30-minute bars)

The same strategies, with the parameters the agents trade with, replayed over the last years twice on the same
S&P 500 stocks and the same prices:

- daily: decide once a day with the close, orders fill at the next open (the 20-year pre-training's engine);
- reactive: every 30 minutes of the session (10:00 ... 16:00 New York) the market is rebuilt up to that moment
  (today's bar so far, volume projected to a full session) and the agent runs agents.react, exactly as in the live
  rounds: open markets trade at that moment's price, stops and targets are checked against the day so far.

Data: Alpaca Market Data v2 (free plan: history since 2016, the latest 15 minutes excluded), keys in the GitHub
secrets ALPACA_KEY_ID / ALPACA_SECRET_KEY. Results go to the private repo (agents/lab/) and to Discord, never to
the Actions log (this repo is public).

    uv run python scripts/intraday_lab.py --anos 5
    uv run python scripts/intraday_lab.py --anos 2 --estrategias foguetao,cacador --dry-run

Limits, also in the report: today's list plus the historical S&P 500 membership (companies Alpaca has no bars for
are missing); no ETFs or European stocks (Alpaca covers US listings only); earnings and news rules off (no history);
the market context of each round is the previous close's; valuation ratios (P/E, value score) during the day use the
previous close; a swing-low support only counts once confirmed by the previous day's close.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import pickle
import sys
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

import agents
import pretrain
from agents import Market, new_portfolio

NY = ZoneInfo("America/New_York")
BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
TIMEFRAME = "30Min"
SLOTS = 13  # 09:30-16:00 in 30-minute bars; checkpoint j sees bars 0..j (it is taken at 10:00 + 30 min x j)
CALLS_PER_MIN = 180  # Alpaca's free plan allows 200 historical calls a minute
DOWNLOADS = 6  # chunks of 50 symbols downloaded at the same time
CACHE = Path(__file__).resolve().parents[1] / "out" / "lab" / "bars"
LAB_DIR = agents.REPO_AGENTS / "lab"
# Strategies of US stocks (the ETF ones and the live-only ones are left out)
ETF_STRATEGIES = {"indexador", "guardiao", "rotador", "pendulo"}


# --------------------------------------------------------------------------
# Alpaca bars
# --------------------------------------------------------------------------


class _NoSip(Exception):
    """The account has no access to the SIP feed (use IEX)."""


class _RateLimit:
    """At most `per_min` calls in any minute, across threads."""

    def __init__(self, per_min: int):
        self.per_min, self.calls, self.lock = per_min, [], threading.Lock()

    def wait(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self.calls = [t for t in self.calls if now - t < 60]
                if len(self.calls) < self.per_min:
                    self.calls.append(now)
                    return
                pause = 60 - (now - self.calls[0])
            time.sleep(max(pause, 0.05))


def alpaca_symbol(symbol: str) -> str:
    return symbol.replace("-", ".")  # Yahoo BRK-B is Alpaca BRK.B


def fetch_bars(symbols: list[str], start: date, end: date, feed: str = "sip") -> dict[str, pd.DataFrame]:
    """30-minute bars (split and dividend adjusted) per symbol, by calendar year, cached on disk (out/lab/bars, kept
    by actions/cache): finished years are downloaded once, the current year again each day."""
    parts: dict[str, list[pd.DataFrame]] = {}
    for year in range(start.year, end.year + 1):
        a, b = max(start, date(year, 1, 1)), min(end, date(year, 12, 31))
        tag = str(year) if year < end.year else f"{year}-{end.isoformat()}"
        for s, df in _fetch_chunk(symbols, a, b, tag, feed).items():
            parts.setdefault(s, []).append(df)
    return {s: pd.concat(dfs).sort_index() for s, dfs in parts.items()}


def _fetch_chunk(symbols: list[str], start: date, end: date, tag: str, feed: str) -> dict[str, pd.DataFrame]:
    key, secret = os.environ.get("ALPACA_KEY_ID"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("Faltam os segredos ALPACA_KEY_ID e ALPACA_SECRET_KEY.")
    CACHE.mkdir(parents=True, exist_ok=True)
    out, missing = {}, []
    for s in symbols:
        f = CACHE / f"{s}_{tag}.pkl"
        if f.exists():
            df = pickle.loads(f.read_bytes())
            if df is not None:
                out[s] = df
        else:
            missing.append(s)
    for old in CACHE.glob(f"*_{tag.split('-')[0]}-*.pkl"):  # earlier days' copies of the current year
        if not old.name.endswith(f"_{tag}.pkl"):
            old.unlink()
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    end_ts = min(datetime.combine(end, datetime.min.time(), timezone.utc) + timedelta(days=1),
                 datetime.now(timezone.utc) - timedelta(minutes=20))  # the free plan has no data for the last 15 minutes
    limiter = _RateLimit(CALLS_PER_MIN)

    def download(chunk: list[str]) -> dict[str, pd.DataFrame | None]:
        """One request series (pages) for up to 50 symbols; each symbol's year is cached as soon as it is complete."""
        names = {alpaca_symbol(s): s for s in chunk}
        rows: dict[str, list] = {s: [] for s in chunk}
        token, fails = None, 0
        while True:
            limiter.wait()
            params = {"symbols": ",".join(names), "timeframe": TIMEFRAME, "start": f"{start.isoformat()}T00:00:00Z",
                      "end": end_ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 10000, "adjustment": "all", "feed": feed}
            if token:
                params["page_token"] = token
            try:
                resp = requests.get(BARS_URL, params=params, headers=headers, timeout=120)
            except requests.RequestException:
                resp = None
            if resp is None or resp.status_code == 429 or resp.status_code >= 500:  # rate limit, gateway timeouts
                fails += 1
                if fails > 8:
                    raise RuntimeError(f"Alpaca não responde ({resp.status_code if resp is not None else 'sem resposta'})")
                time.sleep(min(30 * fails, 180))
                continue
            fails = 0
            if resp.status_code == 403 and feed == "sip":
                raise _NoSip
            resp.raise_for_status()
            data = resp.json()
            for sym, bars in (data.get("bars") or {}).items():
                if sym in names:
                    rows[names[sym]] += bars
            token = data.get("next_page_token")
            if not token:
                break
        got = {}
        for s in chunk:
            df = None
            if rows[s]:
                df = pd.DataFrame(rows[s]).rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
                df.index = pd.to_datetime(df["t"], utc=True).dt.tz_convert(NY)
                df = df[["Open", "High", "Low", "Close", "Volume"]].sort_index()
                df = df[~df.index.duplicated(keep="last")]
            (CACHE / f"{s}_{tag}.pkl").write_bytes(pickle.dumps(df))
            got[s] = df
        return got

    # Each request is slow (a page is up to 10 000 bars), far below the rate limit: several chunks at a time.
    chunks = [missing[i: i + 50] for i in range(0, len(missing), 50)]
    done = 0
    try:
        with ThreadPoolExecutor(max_workers=DOWNLOADS) as pool:
            for got in pool.map(download, chunks):
                out.update({s: df for s, df in got.items() if df is not None})
                done += len(got)
                print(f"Alpaca {tag}: {done}/{len(missing)} ações", flush=True)
    except _NoSip:
        print("::notice title=Alpaca::sem acesso ao feed SIP; uso o IEX", flush=True)
        return _fetch_chunk(symbols, start, end, tag, "iex")
    return out


# --------------------------------------------------------------------------
# Daily bars at each checkpoint
# --------------------------------------------------------------------------


def checkpoint_bars(intraday: pd.DataFrame) -> list[pd.DataFrame]:
    """[daily OHLCV as seen at checkpoint j for j in 0..SLOTS-1]: open of the day, high and low so far, the latest
    close and the volume so far (not projected). The last one is the full regular session."""
    df = intraday
    minutes = df.index.hour * 60 + df.index.minute - (9 * 60 + 30)
    df = df[(minutes >= 0) & (minutes < SLOTS * 30)].copy()
    if df.empty:
        return [pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])] * SLOTS
    df["slot"] = ((df.index.hour * 60 + df.index.minute - (9 * 60 + 30)) // 30).astype(int)
    df["day"] = df.index.tz_localize(None).normalize()
    g = df.groupby("day")
    cum = pd.DataFrame({"Open": g["Open"].transform("first"), "High": g["High"].cummax(), "Low": g["Low"].cummin(),
                        "Close": df["Close"], "Volume": g["Volume"].cumsum(), "slot": df["slot"], "day": df["day"]})
    out = []
    for j in range(SLOTS):
        seen = cum[cum["slot"] <= j].groupby("day").last()
        out.append(seen[["Open", "High", "Low", "Close", "Volume"]].astype(float))
    return out


def features_at(full: pd.DataFrame, part: pd.DataFrame, fraction: float, base: pd.DataFrame | None = None) -> pd.DataFrame:
    """pretrain.features for every day as seen during it: the days before are complete (`full`), the day itself is
    `part` (its bar so far, volume projected by `fraction` of the session). The same fields as agents.load_market
    builds in the live rounds from Yahoo's bar so far."""
    base = pretrain.features(full) if base is None else base
    part = part.reindex(full.index)
    close, high, low, vol = full["Close"], full["High"], full["Low"], full["Volume"]
    c, h, lo, o = part["Close"], part["High"], part["Low"], part["Open"]
    v = part["Volume"] / max(fraction, agents.LIVE_MIN_FRACTION)
    prev = close.shift(1)
    f = pd.DataFrame(index=full.index)
    f["price"] = c
    f["sma50"] = (close.rolling(49).sum().shift(1) + c) / 50
    f["sma200"] = (close.rolling(199).sum().shift(1) + c) / 200
    f["sma200_rising"] = f["sma200"] > close.shift(20).rolling(200).mean()
    high52 = np.fmax(high.rolling(251, min_periods=1).max().shift(1), h)
    low52 = np.fmin(low.rolling(251, min_periods=1).min().shift(1), lo)
    f["pct_from_high"] = (high52 - c) / high52
    f["pct_above_low"] = (c - low52) / low52
    for name, n in (("ret_1d", 1), ("ret_1w", 5), ("ret_3m", 63), ("ret_6m", 126)):
        f[name] = c / close.shift(n) - 1
    f["high20_prev"] = base["high20_prev"]
    f["low10_prev"] = base["low10_prev"]
    avg50 = vol.shift(1).rolling(50).mean().clip(lower=1)
    f["vol_ratio"] = v / avg50
    f["range10"] = (np.fmax(high.rolling(9).max().shift(1), h) - np.fmin(low.rolling(9).min().shift(1), lo)) / c
    r_prev = close.pct_change().shift(1)
    s1 = r_prev.rolling(19).sum() + (c / prev - 1)
    s2 = (r_prev ** 2).rolling(19).sum() + (c / prev - 1) ** 2
    f["vol20"] = np.sqrt(((s2 - s1 ** 2 / 20) / 19).clip(lower=0))
    tr_full = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    tr_now = pd.concat([h - lo, (h - prev).abs(), (lo - prev).abs()], axis=1).max(axis=1)
    f["atr14"] = (tr_full.rolling(13).sum().shift(1) + tr_now) / 14
    pos = pd.Series(range(len(full)), index=full.index)
    gap = o / prev - 1
    event = (gap >= agents.GAP_MIN) & (v / avg50 >= agents.GAP_VOLUME) & (pos >= 51)
    age_prev = base["gap_age"].shift(1)
    carry = (age_prev >= 0) & (age_prev + 1 < agents.GAP_LOOKBACK)
    f["gap_size"] = np.where(event, gap, np.where(carry, base["gap_size"].shift(1), 0.0))
    f["gap_low"] = np.where(event, lo, np.where(carry, base["gap_low"].shift(1), 0.0))
    f["gap_age"] = np.where(event, 0, np.where(carry, age_prev + 1, -1)).astype(int)
    for span, col in ((21, "ema21"), (50, "ema50")):
        a = 2 / (span + 1)
        f[col] = a * c + (1 - a) * base[col].shift(1)
    swing = base["support"].shift(1)  # confirmed by the previous close (see the module's limits)
    fallback = low.shift(1).rolling(60, min_periods=10).min()
    f["support"] = swing.where(swing < c, fallback)
    f["valid"] = base["valid"] & c.notna()
    return f


# --------------------------------------------------------------------------
# The lab's markets
# --------------------------------------------------------------------------


class DayHist(Mapping):
    """mkt.hist for one day at one checkpoint: each symbol's last complete days plus today's bar so far, built only
    for the few symbols the engine asks for (held or pending)."""

    KEEP = 40

    def __init__(self, lab: "Lab", day: date, j: int | None):
        self.lab, self.day, self.j, self._made = lab, day, j, {}

    def _build(self, s: str) -> pd.DataFrame | None:
        full = self.lab.full.get(s)
        if full is None:
            return None
        ts = pd.Timestamp(self.day)
        i = full.index.searchsorted(ts)
        before = full.iloc[max(0, i - self.KEEP): i]
        if self.j is None:  # the complete day
            today = full.iloc[i: i + 1] if i < len(full) and full.index[i] == ts else full.iloc[0:0]
        else:
            part = self.lab.parts[self.j].get(s)
            today = part.loc[[ts]] if part is not None and ts in part.index else full.iloc[0:0]
        return pd.concat([before, today]) if len(today) else before

    def __getitem__(self, s):
        if s not in self._made:
            self._made[s] = self._build(s)
        if self._made[s] is None:
            raise KeyError(s)
        return self._made[s]

    def get(self, s, default=None):
        try:
            return self[s]
        except KeyError:
            return default

    def __contains__(self, s):
        return s in self.lab.full

    def __iter__(self):
        return iter(self.lab.full)

    def __len__(self):
        return len(self.lab.full)


class Lab:
    """Everything both engines need: complete daily bars, the day as seen at each checkpoint, snapshots, context."""

    def __init__(self, intraday: dict[str, pd.DataFrame], sector_of: dict[str, str], member, days_wanted: int,
                 fx: pd.Series, ctx_frame: pd.DataFrame, funds: dict):
        per = {s: checkpoint_bars(df) for s, df in intraday.items()}
        self.full = {s: bars[-1] for s, bars in per.items() if len(bars[-1]) > 0}  # late listings too (valid days only)
        spy = self.full.get("SPY")
        if spy is None:
            raise RuntimeError("Sem barras do SPY no Alpaca.")
        sessions = [d.date() for d in spy.index]
        self.days = sessions[pretrain.WARMUP:][-days_wanted:]
        self.sessions = sessions
        self.parts = [{s: per[s][j] for s in self.full} for j in range(SLOTS)]
        stocks = {s: df for s, df in self.full.items() if s != "SPY"}
        import fundamentals

        first = pd.Timestamp(self.days[0]) - pd.Timedelta(days=10)
        frames = {s: fundamentals.daily(funds[s], df["Close"][df.index >= first]) for s, df in stocks.items() if s in funds}
        extra = {s: {"sector": sector_of.get(s, "Unknown")} for s in stocks}
        base = {s: pretrain.features(df) for s, df in stocks.items()}
        self.daily_snap = pretrain.snapshots(stocks, self.days, extra, member, frames, featured=base, lazy=True)
        # during the day: the valuation ratios of the day before (a known simplification, in the module's limits)
        lagged = {s: fr.shift(1) for s, fr in frames.items()}
        self.snap = []
        for j in range(SLOTS):
            featured = {s: features_at(df, self.parts[j][s], (j + 1) / SLOTS, base[s]) for s, df in stocks.items()}
            self.snap.append(pretrain.snapshots(stocks, self.days, extra, member, lagged, featured=featured, lazy=True))
            print(f"Laboratório: momento {j + 1}/{SLOTS} do dia preparado", flush=True)
        self.fx = fx
        # the benchmark (agents.BENCHMARK, the S&P 500 in euros): SPY converted at each day's rate
        rate = fx.reindex(spy.index, method="ffill").bfill()
        bench = spy.copy()
        for col in ("Open", "High", "Low", "Close"):
            bench[col] = spy[col] / rate
        self.full[agents.BENCHMARK] = bench
        for j in range(SLOTS):
            self.parts[j][agents.BENCHMARK] = bench
        self.ctx_frame = ctx_frame
        closes = pd.DataFrame({s: df["Close"] for s, df in self.full.items()}).sort_index()
        self.full_close = closes.ffill()
        self.part_close = []
        for j in range(SLOTS):
            pc = pd.DataFrame({s: self.parts[j][s]["Close"] for s in stocks}).reindex(closes.index)
            pc[agents.BENCHMARK] = closes[agents.BENCHMARK]
            self.part_close.append(pc.combine_first(self.full_close.shift(1)).reindex(columns=closes.columns))
        self.cols = {c: k for k, c in enumerate(closes.columns)}
        self.full_values = self.full_close.to_numpy(dtype=float)
        self.part_values = [pc.to_numpy(dtype=float) for pc in self.part_close]

    def market(self, day: date, j: int | None) -> Market:
        """The market at checkpoint j of `day`, or (j=None) at its close."""
        import context

        at = self.full_close.index.searchsorted(pd.Timestamp(day))
        stocks = self.daily_snap[day] if j is None else self.snap[j][day]
        values = (self.full_values if j is None else self.part_values[j])[at]
        prev = max((d for d in self.sessions if d < day), default=day)
        share = agents.us_breadth(stocks)
        return Market(hist=DayHist(self, day, j), fx=self.fx, stocks=stocks, etfs={}, as_of=day,
                      sessions=[d for d in self.sessions if d <= day], zone=agents.zone_for(share), stage2_share=share,
                      closes=pretrain.PriceRow(values, self.cols),
                      context=context.on(self.ctx_frame, day if j is None else prev))


def checkpoint_time(day: date, j: int) -> datetime:
    """The UTC moment of checkpoint j (10:00 New York + 30 min x j)."""
    local = datetime(day.year, day.month, day.day, 10, 0, tzinfo=NY) + timedelta(minutes=30 * j)
    return local.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# The two engines
# --------------------------------------------------------------------------

LAB: Lab | None = None  # set before the worker processes fork


def summary(port: dict, capital: float, trades: int, sells: list[dict], bankrupt: str | None) -> dict:
    equities = pd.Series([h["equity"] for h in port["history"]], dtype=float)
    daily = equities.pct_change().dropna()
    sharpe = float(daily.mean() / daily.std() * 252 ** 0.5) if len(daily) > 20 and daily.std() > 0 else 0.0
    final = float(equities.iloc[-1]) if len(equities) else capital
    return {"points": port["points"], "return": round(final / capital - 1, 4),
            "max_drawdown": round(port.get("max_dd", 0.0), 4), "sharpe": round(sharpe, 2), "trades": trades,
            "bankrupt": bankrupt, "trade_stats": pretrain.trade_stats(sells),
            "curve": [(h["date"], round((h["equity"] / capital - 1) * 100, 2)) for h in port["history"]]}


def run_reactive(strategy: str, params: dict, lab: Lab, capital: float = 1000.0) -> dict:
    """The live rounds (agents.fill_pending, check_exits, live_exits, react) at every checkpoint of every day, then the
    daily bookkeeping at the close (stops of the full day, trailing stops, taxes, points)."""
    cfg = {"id": "lab", "name": strategy, "strategy": strategy, "params": params}
    port = new_portfolio(capital, lab.days[0])
    port["params"] = dict(params)
    trades, sells, bankrupt = 0, [], None
    for day in lab.days:
        for j in range(SLOTS):
            if day not in lab.snap[j] or not lab.snap[j][day]:
                continue
            now = checkpoint_time(day, j)
            mkt = lab.market(day, j)
            done = (agents.fill_pending(port, mkt, partial_day=day) + agents.check_exits(port, mkt, open_day=day)
                    + agents.live_exits(port, mkt, now, targets=True))
            if bankrupt is None:
                traded, _ = agents.react(cfg, port, mkt, port["params"], now, live=False)
                done += traded
            trades += len(done)
            sells += [t for t in done if t["side"] == "venda"]
        mkt = lab.market(day, None)
        done = agents.fill_pending(port, mkt) + agents.check_exits(port, mkt)
        trades += len(done)
        sells += [t for t in done if t["side"] == "venda"]
        agents.trail_stops(port, mkt, agents.effective(port["params"], mkt.zone, strategy))
        agents.pay_taxes(port, mkt)
        agents.score_day(port, mkt)
        if bankrupt is None and agents.is_bankrupt(port, mkt):
            trades += len(agents.liquidate(port, mkt, "falência"))
            bankrupt = day.isoformat()
    return summary(port, capital, trades, sells, bankrupt)


def run_daily(strategy: str, params: dict, lab: Lab) -> dict:
    """The pre-training's engine (pretrain.simulate) on the same days and prices (Alpaca's complete sessions)."""
    markets = [lab.market(d, None) for d in lab.days]
    r = pretrain.simulate(strategy, params, markets)
    return {k: r[k] for k in ("points", "return", "max_drawdown", "sharpe", "trades", "bankrupt", "trade_stats", "curve")}


def _job(job: tuple[str, dict]) -> tuple[str, dict]:
    strategy, params = job
    start = time.monotonic()
    out = {"daily": run_daily(strategy, params, LAB), "reactive": run_reactive(strategy, params, LAB)}
    print(f"Laboratório: {strategy} feito em {time.monotonic() - start:.0f} s", flush=True)
    return strategy, out


# --------------------------------------------------------------------------
# Run and report
# --------------------------------------------------------------------------


def lab_strategies(only: set[str] | None) -> dict[str, dict]:
    """{strategy: the parameters its reactive agent trades with (or the strategy's defaults)}."""
    config, state = agents.load_config(), agents.load_state()
    out = {}
    for strategy, (_, _, defaults) in agents.STRATEGIES.items():
        if (strategy in ETF_STRATEGIES or (strategy in pretrain.SKIP and strategy not in agents.INTRADAY_ONLY)
                or (only and strategy not in only)):
            continue
        cfg = next((c for c in agents.active_agents(config)
                    if c["strategy"] == strategy and agents.mode_of(c) == agents.REACTIVE), None)
        params = dict((state["agents"].get(cfg["id"]) or {}).get("params", cfg["params"])) if cfg else dict(defaults)
        out[strategy] = params
    return out


def universe(years: int):
    """(symbols with their sectors, the membership test or None, first day, last day) of the lab."""
    from rankings import load_constituents

    constituents = load_constituents()
    sector_of = {c["symbol"]: c["sector"] for c in constituents}
    try:
        membership = pretrain.load_membership()
    except requests.RequestException:
        membership = {}
    end = date.today()
    start = end - timedelta(days=int(years * 365.25) + 420)  # + the 220-session warm-up
    for ticker, spans in membership.items():
        if any(e is None or e >= start for _, e in spans):
            sector_of.setdefault(ticker, "Unknown")

    def member(symbol: str, day: date) -> bool:
        spans = membership.get(symbol)
        return True if not spans else any(a <= day and (e is None or day <= e) for a, e in spans)

    return sector_of, (member if membership else None), start, end


def build_lab(years: int) -> Lab:
    import context
    import fundamentals
    from rankings import download_history

    sector_of, member, start, end = universe(years)
    intraday = fetch_bars(sorted(sector_of) + ["SPY"], start, end)
    fx = download_history(["EURUSD=X"], period="max")["EURUSD=X"]["Close"]
    spy_daily = download_history(["SPY"], period="max")["SPY"]
    ctx = context.download("max", spy_daily, fx)
    return Lab(intraday, sector_of, member, years * 252, fx, ctx, fundamentals.load())


def report_embed(results: dict, years: int) -> dict:
    from discord_post import BLUE, embed

    lines = []
    for s, r in sorted(results.items(), key=lambda kv: kv[1]["reactive"]["return"] - kv[1]["daily"]["return"], reverse=True):
        d, x = r["daily"], r["reactive"]
        diff = x["return"] - d["return"]
        lines.append(f"{'🟢' if diff > 0 else '🔴'} **{s}**: reativo {x['return'] * 100:+.1f}% (queda máx. "
                     f"{x['max_drawdown'] * 100:.0f}%, {x['trades']} ops.) vs diário {d['return'] * 100:+.1f}% "
                     f"(queda máx. {d['max_drawdown'] * 100:.0f}%, {d['trades']} ops.)")
    better = sum(1 for r in results.values() if r["reactive"]["return"] > r["daily"]["return"])
    return embed("🧪 Laboratório intradiário (Alpaca, ações dos EUA)",
                 f"{years} anos, decisões de 30 em 30 minutos contra uma vez por dia, mesmos parâmetros e preços. "
                 f"O modo reativo ganhou em **{better} de {len(results)}** estratégias.\n\n" + "\n".join(lines)[:3500],
                 color=BLUE)


def save_results(results: dict, years: int) -> Path:
    LAB_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "years": years,
               "timeframe": TIMEFRAME, "strategies": results}
    path = LAB_DIR / f"intraday-{date.today().isoformat()}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False))
    (LAB_DIR / "intraday-latest.json").write_text(json.dumps(payload, ensure_ascii=False))
    return path


def main() -> int:
    global LAB
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--anos", type=int, default=5)
    parser.add_argument("--estrategias", default="")
    parser.add_argument("--dry-run", action="store_true",
                        help="Save the report to out/lab/report.json instead of posting it (never printed: public logs)")
    parser.add_argument("--so-barras", action="store_true",
                        help="Only download the 30-minute bars into the cache (a job of its own: the download takes hours)")
    a = parser.parse_args()
    if a.so_barras:
        sector_of, _, start, end = universe(a.anos)
        fetch_bars(sorted(sector_of) + ["SPY"], start, end)
        return 0
    only = {s.strip() for s in a.estrategias.split(",") if s.strip()} or None
    jobs = list(lab_strategies(only).items())
    if not jobs:
        print("Nenhuma estratégia para testar.", file=sys.stderr)
        return 1
    LAB = build_lab(a.anos)
    workers = min(len(jobs), os.cpu_count() or 1, 3)  # the tables are shared by fork; each worker adds its own days
    results = {}

    def keep(done) -> None:  # saved after each strategy: a run stopped by the time limit keeps what it finished
        for s, r in done:
            results[s] = r
            save_results(results, a.anos)
            print(f"Laboratório: {len(results)}/{len(jobs)} estratégias", flush=True)

    if workers > 1:
        with multiprocessing.get_context("fork").Pool(workers) as pool:
            keep(pool.imap_unordered(_job, jobs))
    else:
        keep(map(_job, jobs))
    print("Laboratório: resultados guardados no repositório privado (agents/lab)", flush=True)
    if a.dry_run:
        out = CACHE.parent / "report.json"
        out.write_text(json.dumps(report_embed(results, a.anos), ensure_ascii=False, indent=1))
        print(f"Relatório em {out}")
        return 0
    from discord_post import route, send

    where = route("agentes") or {}
    return send([report_embed(results, a.anos)], {}, **where)


if __name__ == "__main__":
    sys.exit(main())
