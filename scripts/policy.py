"""Where the public money and the investment go: per-sector signals of the political agenda, point in time.

Three mechanical signals per GICS sector (no hand-picked laws or themes: the acts that turned out to matter are known
only afterwards), each kept as it could have been known on each day:

- gov: federal contract obligations (USAspending.gov, monthly since October 2007) of the sector's NAICS industries.
  The last 12 months against the 12 before (log growth), minus that growth's own past mean (the surprise), as a
  z-score across sectors. Published with a lag (the Defense Department's with about 90 days): known 3 months after
  the month ends (GOV_LAG_MONTHS). The records are corrected afterwards and there are no vintages: a small leak.
- epub: exposure to policy uncertainty. The sector ETF's monthly return over the S&P 500's, over the last 60 months,
  against the change in five categorical economic policy uncertainty indices (Baker, Bloom & Davis: government
  spending, national security, trade, regulation, health care; FRED, monthly since 1985), one beta per category; the
  score is minus the mean of their z-scores across sectors (Brogaard & Detzel 2015: the most exposed earn less). The
  index of a month is known the next month (EPU_LAG_MONTHS); its revisions are ignored.
- capex: the sector's companies' capital spending over the last 12 months against the same companies' a year before
  (SEC filings, from the day each was filed). High investment growth tends to predict lower returns (Cooper, Gulen &
  Schill 2008; Titman, Wei & Xie 2004): a guard, not a buy signal. A company's sector is today's (S&P 500 list).

The NAICS -> sector map is rough (a contractor's industry is not always its GICS sector): the signal is a growth
rate, so errors dilute it rather than bias it.

    uv run python scripts/policy.py --atualizar      # downloads into data/context/ (Actions: the sites are blocked here)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / "context"
SPENDING_URL = "https://api.usaspending.gov/api/v2/search/spending_over_time/"
SPENDING_START = "2007-10-01"  # the earliest date this endpoint accepts
GOV_LAG_MONTHS = 3
EPU_LAG_MONTHS = 1
BETA_MONTHS = 60
EPU_CATEGORIES = {"EPUGOVTSPEND": "gasto", "EPUNATSEC": "seguranca", "EPUTRADE": "comercio", "EPUREG": "regulacao",
                  "EPUHEALTHCARE": "saude"}
# NAICS prefixes (USAspending matches by prefix) -> the sector's ETF key (context.sector_key). Ambiguous industries
# (scientific R&D, facilities management, finance) are left out.
NAICS = {
    "sec_XLE": ["211", "213", "324", "486"],
    "sec_XLB": ["212", "322", "3251", "3252", "3253", "3255", "3256", "3259", "327", "331", "332"],
    "sec_XLI": ["3364", "3365", "3366", "3369", "333", "335", "23", "481", "482", "483", "484", "488", "492", "493",
                "5413", "5616", "562"],
    "sec_XLK": ["334", "5112", "5132", "518", "5415"],
    "sec_XLV": ["3254", "3391", "621", "622", "623", "4242"],
    "sec_XLY": ["3361", "3362", "3363", "721", "722", "44", "45"],
    "sec_XLP": ["311", "312", "4244", "4245"],
    "sec_XLU": ["2211", "2212", "2213"],
    "sec_XLC": ["517", "516", "519", "515"],
    "sec_XLRE": ["531"],
}
FIELDS = ("gov", "epub", "capex")


def warn(message: str) -> None:
    print(f"::warning::{message}", flush=True)


# --------------------------------------------------------------------------
# downloads (GitHub Actions: context-data workflow)
# --------------------------------------------------------------------------

def spending(key: str, codes: list[str], end: str, tries: int = 4) -> pd.Series | None:
    body = {"group": "month", "spending_level": "transactions",
            "filters": {"time_period": [{"start_date": SPENDING_START, "end_date": end}],
                        "award_type_codes": ["A", "B", "C", "D"], "naics_codes": {"require": codes, "exclude": []}}}
    for attempt in range(tries):
        try:
            resp = requests.post(SPENDING_URL, json=body, timeout=180)
            resp.raise_for_status()
            rows = resp.json()["results"]
            break
        except (requests.RequestException, ValueError, KeyError) as e:
            if attempt == tries - 1:
                warn(f"USAspending {key}: {e}")
                return None
            time.sleep(10 * (attempt + 1))
    out = {}
    for r in rows:
        tp = r["time_period"]  # fiscal year (October-September) and month of the fiscal year (1 = October)
        fy, fm = int(tp["fiscal_year"]), int(tp["month"])
        year, month = (fy - 1, fm + 9) if fm <= 3 else (fy, fm - 3)
        out[pd.Timestamp(year, month, 1) + pd.offsets.MonthEnd(0)] = float(r.get("aggregated_amount") or 0.0)
    return pd.Series(out, name=key).sort_index()


def download_spending() -> pd.DataFrame | None:
    end = (pd.Timestamp.today() - pd.offsets.MonthEnd(1)).date().isoformat()
    cols = {}
    for key, codes in NAICS.items():
        s = spending(key, codes, end)
        if s is not None and len(s):
            cols[key] = s
    return pd.DataFrame(cols) if cols else None


def download_epu() -> pd.DataFrame | None:
    import context

    cols = {}
    for series in EPU_CATEGORIES:
        s = context.fred(series, tries=4, timeout=120)
        if s is not None and len(s):
            cols[series] = s
    return pd.DataFrame(cols) if cols else None


def capex_growth(fund: pd.DataFrame, sector_of: dict[str, str]) -> pd.DataFrame:
    """Month ends x sector keys: the sector's capital spending (last 12 months, from the filings available that day)
    against the same companies' a year before."""
    import context

    fund = fund[fund["symbol"].isin(sector_of) & fund["capex"].notna()].copy()
    fund["key"] = fund["symbol"].map(lambda s: context.sector_key(sector_of[s]))
    fund = fund[fund["key"].notna()]
    months = pd.date_range(fund["available"].min() + pd.offsets.MonthEnd(0), pd.Timestamp.today(), freq="ME")
    out = pd.DataFrame(index=months, columns=sorted(fund["key"].unique()), dtype=float)
    groups = {s: g.sort_values("available") for s, g in fund.groupby("symbol")}
    for t in months:
        now, before = {}, {}
        for s, g in groups.items():
            known = g[g["available"] <= t]
            if not len(known):
                continue
            last = known.iloc[-1]
            if (t - last["available"]).days > 400:
                continue
            target = last["end"] - pd.Timedelta(days=365)
            prev = known[(known["end"] - target).abs() <= pd.Timedelta(days=20)]
            if not len(prev):
                continue
            k = last["key"]
            now[k] = now.get(k, 0.0) + abs(float(last["capex"]))
            before[k] = before.get(k, 0.0) + abs(float(prev.iloc[-1]["capex"]))
        for k in now:
            if before.get(k, 0) > 0:
                out.loc[t, k] = now[k] / before[k] - 1
    return out


def refresh() -> dict[str, str]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    done = {}
    gov = download_spending()
    if gov is not None:
        gov.to_csv(CACHE_DIR / "gasto_publico.csv")
        done["gasto_publico"] = f"{gov.index.min().date()} a {gov.index.max().date()} ({gov.shape[1]} setores)"
    epu = download_epu()
    if epu is not None:
        epu.to_csv(CACHE_DIR / "epu_categorias.csv")
        done["epu_categorias"] = f"{epu.index.min().date()} a {epu.index.max().date()} ({epu.shape[1]} categorias)"
    try:
        import fundamentals
        from rankings import load_constituents

        sector_of = {c["symbol"]: c["sector"] for c in load_constituents()}
        cap = capex_growth(pd.concat(fundamentals.load().values()).reset_index(), sector_of)
        cap.to_csv(CACHE_DIR / "capex_setor.csv")
        done["capex_setor"] = f"{cap.index.min().date()} a {cap.index.max().date()}"
    except Exception as e:  # noqa: BLE001 - the other files are still worth keeping
        warn(f"capex por setor: {e}")
    return done


# --------------------------------------------------------------------------
# the daily fields (context.download adds them to the context frame)
# --------------------------------------------------------------------------

def zscore(frame: pd.DataFrame) -> pd.DataFrame:
    """Across sectors (columns), each row."""
    mean = frame.mean(axis=1)
    sd = frame.std(axis=1).replace(0, np.nan)
    return frame.sub(mean, axis=0).div(sd, axis=0)


def known_from(frame: pd.DataFrame, lag_months: int) -> pd.DataFrame:
    """A monthly frame indexed by the day each value became known (the month's end plus the lag)."""
    out = frame.copy()
    out.index = out.index + pd.offsets.MonthEnd(lag_months)
    return out


def gov_signal(obligations: pd.DataFrame) -> pd.DataFrame:
    """Month ends (when known) x sectors: the surprise in federal contract spending growth, z-scored across sectors."""
    ttm = obligations.clip(lower=0).rolling(12, min_periods=12).sum()
    growth = np.log(ttm / ttm.shift(12)).replace([np.inf, -np.inf], np.nan)
    surprise = growth - growth.expanding(min_periods=12).mean().shift(1)
    return known_from(zscore(surprise), GOV_LAG_MONTHS)


def epu_signal(epu: pd.DataFrame, etf_monthly: dict[str, pd.Series], spy_monthly: pd.Series) -> pd.DataFrame:
    """Month ends (when known) x sectors: minus the mean z-score of the sector's betas to the uncertainty categories."""
    d_epu = np.log(epu.where(epu > 0)).diff()
    betas = {c: {} for c in d_epu.columns}
    excess = pd.DataFrame({k: s - spy_monthly.reindex(s.index) for k, s in etf_monthly.items()})
    common = excess.index.intersection(d_epu.index)
    excess, d_epu = excess.loc[common], d_epu.loc[common]
    for i in range(BETA_MONTHS, len(common) + 1):
        x_all, y_all = d_epu.iloc[i - BETA_MONTHS:i], excess.iloc[i - BETA_MONTHS:i]
        t = common[i - 1]
        for c in d_epu.columns:
            x = x_all[c]
            for k in excess.columns:
                ok = x.notna() & y_all[k].notna()
                if ok.sum() < 36 or x[ok].var() == 0:
                    continue
                betas[c].setdefault(k, {})[t] = float(np.cov(x[ok], y_all[k][ok])[0, 1] / x[ok].var())
    zs = [zscore(pd.DataFrame(b)) for b in betas.values() if b]
    if not zs:
        return pd.DataFrame()
    score = -pd.concat(zs).groupby(level=0).mean()
    return known_from(score, EPU_LAG_MONTHS)


def cached(name: str) -> pd.DataFrame | None:
    path = CACHE_DIR / f"{name}.csv"
    if not path.exists():
        return None
    return pd.read_csv(path, index_col=0, parse_dates=True)


def fields(raw: dict[str, pd.Series], days: pd.DatetimeIndex) -> pd.DataFrame:
    """Daily columns {sector key}_{gov,epub,capex} on these days (each value from the day it was known)."""
    import context

    cols: dict[str, pd.Series] = {}
    gov = cached("gasto_publico")
    if gov is not None and len(gov):
        for k, s in gov_signal(gov).items():
            cols[f"{k}_gov"] = s
    epu = cached("epu_categorias")
    spy = raw.get("spy")
    if epu is not None and len(epu) and spy is not None:
        month = lambda s: s.resample("ME").last().pct_change()  # noqa: E731
        etfs = {}
        for sector, tickers in context.SECTOR_ETFS.items():
            px = None
            for t in tickers:
                if f"etf:{t}" in raw:
                    px = raw[f"etf:{t}"] if px is None else px.combine_first(raw[f"etf:{t}"])
            if px is not None:
                etfs[context.sector_key(sector)] = month(px)
        for k, s in epu_signal(epu.resample("ME").last(), etfs, month(spy)).items():
            cols[f"{k}_epub"] = s
    cap = cached("capex_setor")
    if cap is not None and len(cap):
        for k, s in zscore(cap).items():
            cols[f"{k}_capex"] = s
    if not cols:
        return pd.DataFrame(index=days)
    frame = pd.DataFrame(cols).sort_index()
    if frame.index.tz is not None:
        frame.index = frame.index.tz_localize(None)
    return frame.reindex(frame.index.union(days)).ffill(limit=150).reindex(days)  # a source that stops: gone after 5 months


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--atualizar", action="store_true", help="Download into data/context/")
    a = ap.parse_args()
    if a.atualizar:
        done = refresh()
        print(f"::notice title=Agenda política::{done or 'nada descarregado'}", flush=True)
        return 0 if done else 1
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
