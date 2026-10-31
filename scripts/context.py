"""Market context the agents can react to, the same live and over 20 years of backtest.

Each day gets one small dict (Market.context):

- vix: the VIX (fear / implied volatility of the S&P 500), Yahoo ^VIX.
- epu_pct: US Economic Policy Uncertainty (news-based index of political and policy
  uncertainty, FRED USEPUINDXD), 7-day average, as a percentile of the previous 3 years:
  the political-impact gauge.
- hy: US high-yield credit spread in % (FRED BAMLH0A0HYM2): financial stress.
- rate_chg: change of the US 10-year yield over 3 months, in percentage points (Yahoo ^TNX).
- usd_chg: change of EUR/USD over 3 months; positive = the dollar weakened, which costs a
  euro investor in US stocks.
- spy_trend: S&P 500 (SPY) close against its 200-day average.

What has no free 20-year history (news, results dates, central-bank meetings) is not here:
those rules are tuned live only (agents.LIVE_TUNED).
Any series that cannot be downloaded is simply missing, and the rules that need it stay idle.
"""

from __future__ import annotations

import io
import sys

import pandas as pd
import requests

FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_SERIES = {"epu": "USEPUINDXD", "hy": "BAMLH0A0HYM2"}
YAHOO_SERIES = {"vix": "^VIX", "tnx": "^TNX"}
SESSIONS_3M = 63
EPU_WINDOW = 3 * 252


def fred(series: str) -> pd.Series | None:
    try:
        resp = requests.get(FRED, params={"id": series}, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        col = [c for c in df.columns if c.upper() != "DATE" and c.lower() != "observation_date"][0]
        out = pd.Series(pd.to_numeric(df[col], errors="coerce").to_numpy(), index=pd.to_datetime(df.iloc[:, 0]))
        return out.dropna()
    except (requests.RequestException, ValueError, IndexError, KeyError) as e:
        print(f"WARN: FRED {series}: {e}", file=sys.stderr)
        return None


def download(period: str, spy: pd.DataFrame | None, fx: pd.Series | None) -> pd.DataFrame:
    """One row per calendar day with every context field (forward-filled), from all the sources."""
    from rankings import download_history  # heavy import, only when needed

    raw: dict[str, pd.Series] = {}
    hist = download_history(list(YAHOO_SERIES.values()), period=period)
    for name, ticker in YAHOO_SERIES.items():
        if ticker in hist:
            raw[name] = hist[ticker]["Close"]
    for name, series in FRED_SERIES.items():
        s = fred(series)
        if s is not None and len(s):
            raw[name] = s
    if spy is not None:
        raw["spy"] = spy["Close"]
    if fx is not None:
        raw["fx"] = fx
    return features(raw)


def features(raw: dict[str, pd.Series]) -> pd.DataFrame:
    """The context fields from the raw series, each computed on its own dates, then aligned by day."""
    cols = {}
    if "vix" in raw:
        cols["vix"] = raw["vix"]
    if "epu" in raw:
        avg = raw["epu"].rolling(7, min_periods=3).mean()
        cols["epu_pct"] = avg.rolling(EPU_WINDOW, min_periods=250).rank(pct=True)
    if "hy" in raw:
        cols["hy"] = raw["hy"]
    if "tnx" in raw:
        tnx = raw["tnx"].where(raw["tnx"] < 20, raw["tnx"] / 10)  # some old ^TNX quotes are the yield x10
        cols["rate_chg"] = tnx - tnx.shift(SESSIONS_3M)
    if "fx" in raw:
        cols["usd_chg"] = raw["fx"] / raw["fx"].shift(SESSIONS_3M) - 1
    if "spy" in raw:
        cols["spy_trend"] = raw["spy"] / raw["spy"].rolling(200).mean() - 1
    if not cols:
        return pd.DataFrame()
    frame = pd.DataFrame({k: v.groupby(v.index.normalize()).last() for k, v in cols.items()})
    days = pd.date_range(frame.index.min(), frame.index.max(), freq="D")
    return frame.reindex(days).ffill(limit=10)


def on(frame: pd.DataFrame, day) -> dict:
    """The context known at the close of `day` (nothing later)."""
    if frame is None or frame.empty:
        return {}
    rows = frame.loc[: pd.Timestamp(day)]
    if rows.empty:
        return {}
    last = rows.iloc[-1]
    return {k: round(float(v), 5) for k, v in last.items() if pd.notna(v)}


def coverage(frame: pd.DataFrame) -> dict[str, str]:
    """First date with data per field, for the run's log."""
    return {c: frame[c].first_valid_index().date().isoformat() for c in frame.columns if frame[c].first_valid_index() is not None}
