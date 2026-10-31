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
import os
import sys

from pathlib import Path

import pandas as pd
import requests

FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_SERIES = {"epu": "USEPUINDXD", "hy": "BAMLH0A0HYM2"}
EPU_DIRECT = "https://www.policyuncertainty.com/media/All_Daily_Policy_Data.csv"  # the index's own site, if FRED fails
YAHOO_SERIES = {"vix": "^VIX", "tnx": "^TNX"}
SESSIONS_3M = 63
EPU_WINDOW = 3 * 252


def fred(series: str, tries: int = 3) -> pd.Series | None:
    for attempt in range(tries):  # FRED sometimes times out from cloud machines
        try:
            resp = requests.get(FRED, params={"id": series}, timeout=90, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            break
        except requests.RequestException as e:
            if attempt == tries - 1:
                warn(f"FRED {series}: {e}")
                return None
    try:
        df = pd.read_csv(io.StringIO(resp.text))
        col = [c for c in df.columns if c.upper() != "DATE" and c.lower() != "observation_date"][0]
        out = pd.Series(pd.to_numeric(df[col], errors="coerce").to_numpy(), index=pd.to_datetime(df.iloc[:, 0]))
        return out.dropna()
    except (ValueError, IndexError, KeyError) as e:
        warn(f"FRED {series}: {e}")
        return None


def warn(message: str) -> None:
    """stderr, and an annotation on the run page when in GitHub Actions (job logs are not always reachable)."""
    print(f"WARN: {message}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning title=Contexto::{message}", flush=True)


def epu_direct() -> pd.Series | None:
    """US daily policy uncertainty from policyuncertainty.com (columns day, month, year, daily_policy_index)."""
    try:
        resp = requests.get(EPU_DIRECT, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        idx = pd.to_datetime(dict(year=df["year"], month=df["month"], day=df["day"]), errors="coerce")
        return pd.Series(pd.to_numeric(df["daily_policy_index"], errors="coerce").to_numpy(), index=idx).dropna()
    except (requests.RequestException, ValueError, KeyError) as e:
        warn(f"policyuncertainty.com: {e}")
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
    if "epu" not in raw:
        s = epu_direct()
        if s is not None and len(s):
            raw["epu"] = s
    if "tnx" not in raw:
        s = fred("DGS10")
        if s is not None and len(s):
            raw["tnx"] = s
        else:
            warn("juro a 10 anos indisponível (Yahoo ^TNX e FRED DGS10)")
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


def save(frame: pd.DataFrame, path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path)


def load(path: str) -> pd.DataFrame:
    return pd.read_csv(path, index_col=0, parse_dates=True)


def coverage(frame: pd.DataFrame) -> dict[str, str]:
    """First date with data per field, for the run's log."""
    return {c: frame[c].first_valid_index().date().isoformat() for c in frame.columns if frame[c].first_valid_index() is not None}


def main() -> int:
    """Download the context once (e.g. in the pre-training's plan job) so every strategy trains on the same data."""
    import argparse

    from rankings import download_history

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--guardar", required=True, help="CSV file to write")
    parser.add_argument("--periodo", default="max", help="Yahoo period (default max)")
    a = parser.parse_args()
    hist = download_history(["SPY", "EURUSD=X"], period=a.periodo)
    frame = download(a.periodo, hist.get("SPY"), hist["EURUSD=X"]["Close"] if "EURUSD=X" in hist else None)
    save(frame, a.guardar)
    cov = coverage(frame)
    missing = [c for c in ("vix", "epu_pct", "hy", "rate_chg", "usd_chg", "spy_trend") if c not in cov]
    print(f"::notice title=Contexto::dados desde {cov}" + (f"; em falta: {missing}" if missing else ""), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
