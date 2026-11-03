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

Europe (the same fields for the European sectors, keys eu_<STOXX Europe 600 sector ETF>):
- gov: public procurement notices in the EU's Official Journal (TED), per quarter, by the CPV divisions of the
  sector; the same surprise, known a month after the quarter ends (notices are published as they happen).
- epub: each STOXX Europe 600 sector's monthly return over the STOXX Europe 600 against the European news-based
  policy uncertainty index (Baker, Bloom & Davis; FRED EUEPUINDXM), no categories.
- capex: none (no free point-in-time filings for European companies): the score leaves it out.

The NAICS -> sector map is rough (a contractor's industry is not always its GICS sector): the signal is a growth
rate, so errors dilute it rather than bias it.

    uv run python scripts/policy.py --atualizar      # downloads into data/context/ (Actions: the sites are blocked here)
"""

from __future__ import annotations

import argparse
import io
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
TED_URL = "https://api.ted.europa.eu/v3/notices/search"
TED_FIRST_YEAR = 2008
TED_LAG_MONTHS = 1
EU_EPU = "EUEPUINDXM"
# CPV divisions (the first two digits of the common procurement vocabulary) -> the European sector's key
CPV = {
    "eu_EXH1": ["09"],                                       # petroleum products, fuel, energy
    "eu_EXV6": ["14", "24", "44"],                           # mining and metals, chemicals, construction materials
    "eu_EXH4": ["31", "34", "35", "42", "43", "45", "50", "60", "71", "90"],  # machinery, transport and defence
    "eu_EXV3": ["30", "32", "48", "72"],                      # computers, communication equipment, software, IT
    "eu_EXV4": ["33", "85"],                                 # medical equipment and pharmaceuticals, health services
    "eu_EXV5": ["18", "37", "39", "55"],                     # clothing, sport goods, furniture, hotels and catering
    "eu_EXH3": ["03", "15"],                                 # agriculture, food and beverages
    "eu_EXH9": ["65"],                                       # public utilities
    "eu_EXV2": ["64", "92"],                                 # post and telecommunications, media and culture
    "eu_EXV1": ["66"],                                       # financial and insurance services
    "eu_EXI5": ["70"],                                       # real estate services
}


def warn(message: str) -> None:
    print(f"::warning::{message}", flush=True)


# --------------------------------------------------------------------------
# downloads (GitHub Actions: context-data workflow)
# --------------------------------------------------------------------------

_BODY = 0  # which request form the API accepted (kept for the next requests)


def spending(key: str, codes: list[str], first: str, last: str, tries: int = 3) -> pd.Series | None:
    """Monthly contract obligations of these NAICS codes between two dates (calendar month ends)."""
    global _BODY
    filters = {"time_period": [{"start_date": first, "end_date": last}], "award_type_codes": ["A", "B", "C", "D"],
               "naics_codes": {"require": codes}}
    bodies = [{"group": "month", "filters": filters},  # the API's own defaults
              {"group": "month", "spending_level": "transactions", "filters": filters},
              {"group": "month", "filters": {**filters, "naics_codes": codes}}]  # the legacy list form
    rows = None
    for attempt in range(tries):
        body = bodies[_BODY % len(bodies)]
        try:
            resp = requests.post(SPENDING_URL, json=body, timeout=90)
            if resp.status_code in (400, 422):  # the request's shape: the next form, and say why
                warn(f"USAspending {key} {first}: {resp.status_code} {resp.text[:300]}")
                _BODY += 1
                continue
            resp.raise_for_status()
            rows = resp.json()["results"]
            break
        except (requests.RequestException, ValueError, KeyError) as e:
            if attempt == tries - 1:
                warn(f"USAspending {key} {first}: {e}")
            time.sleep(5 * (attempt + 1))
    if rows is None:
        return None
    out = {}
    for r in rows:
        tp = r["time_period"]  # fiscal year (October-September) and month of the fiscal year (1 = October)
        fy, fm = int(tp["fiscal_year"]), int(tp["month"])
        year, month = (fy - 1, fm + 9) if fm <= 3 else (fy, fm - 3)
        out[pd.Timestamp(year, month, 1) + pd.offsets.MonthEnd(0)] = float(r.get("aggregated_amount") or 0.0)
    return pd.Series(out, name=key, dtype=float).sort_index()


def download_spending(budget_s: float = 1500) -> pd.DataFrame | None:
    """Fiscal year by fiscal year (one large request times out), keeping what is cached: only the missing years and
    the last two (still being corrected) are asked for; at most budget_s seconds, then what was got is kept."""
    old = cached("gasto_publico")
    today = pd.Timestamp.today()
    last_fy = today.year + (1 if today.month >= 10 else 0)
    t0 = time.time()
    cols = {}
    failures = 0
    for key, codes in NAICS.items():
        have = old[key].dropna() if old is not None and key in old else pd.Series(dtype=float, index=pd.DatetimeIndex([]))
        parts = [have]
        for fy in range(2008, last_fy + 1):
            first, last = pd.Timestamp(fy - 1, 10, 1), min(pd.Timestamp(fy, 9, 30), today - pd.offsets.MonthEnd(1))
            if first > last:
                continue
            known = have[(have.index >= first) & (have.index <= last)]
            if len(known) >= 12 and fy < last_fy - 1:
                continue
            if time.time() - t0 > budget_s:
                warn("USAspending: tempo esgotado, fica o que já veio")
                break
            s = spending(key, codes, first.date().isoformat(), last.date().isoformat())
            if s is not None and len(s):
                parts.append(s)
                failures = 0
            else:
                failures += 1
            if failures >= 6:
                break
        if failures >= 6:
            warn("USAspending: 6 pedidos falhados seguidos, desisto por agora")
            break
        merged = pd.concat(parts)
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        if len(merged):
            cols[key] = merged
        print(f"USAspending {key}: {len(merged)} meses ({time.time() - t0:.0f} s)", flush=True)
    if failures >= 6 and old is not None:  # keep the cached sectors not reached
        for key in old:
            cols.setdefault(key, old[key].dropna())
    return pd.DataFrame(cols) if cols else None


_TED_FORM = 0  # which query syntax TED accepted


def ted_count(key: str, divisions: list[str], first: pd.Timestamp, last: pd.Timestamp, tries: int = 3) -> float | None:
    """How many notices TED published between two dates with a CPV code in these divisions."""
    global _TED_FORM
    a, b = first.strftime("%Y%m%d"), last.strftime("%Y%m%d")
    forms = [
        "(" + " OR ".join(f"classification-cpv={d}*" for d in divisions) + f") AND publication-date>={a} AND publication-date<={b}",
        "classification-cpv IN (" + " ".join(f"{d}000000" for d in divisions) + f") AND publication-date>={a}<={b}",
        "PC=[" + " or ".join(f"{d}*" for d in divisions) + f"] AND PD=[{a} <> {b}]",
    ]
    for attempt in range(tries):
        body = {"query": forms[_TED_FORM % len(forms)], "fields": ["publication-number"], "limit": 1, "page": 1}
        try:
            resp = requests.post(TED_URL, json=body, timeout=60)
            if resp.status_code in (400, 422):
                warn(f"TED {key} {a}: {resp.status_code} {resp.text[:300]}")
                _TED_FORM += 1
                continue
            resp.raise_for_status()
            return float(resp.json()["totalNoticeCount"])
        except (requests.RequestException, ValueError, KeyError) as e:
            if attempt == tries - 1:
                warn(f"TED {key} {a}: {e}")
            time.sleep(3 * (attempt + 1))
    return None


def download_procurement(budget_s: float = 900) -> pd.DataFrame | None:
    """Quarter ends x European sectors: notices per quarter, keeping what is cached (only the missing quarters and the
    last two are asked for)."""
    old = cached("gasto_publico_eu")
    today = pd.Timestamp.today()
    quarters = pd.date_range(f"{TED_FIRST_YEAR}-03-31", today - pd.offsets.QuarterEnd(1), freq="QE")
    t0, failures, cols = time.time(), 0, {}
    for key, divisions in CPV.items():
        have = old[key].dropna() if old is not None and key in old else pd.Series(dtype=float, index=pd.DatetimeIndex([]))
        got = dict(have.items())
        for q in quarters:
            if q in got and q < quarters[-2]:
                continue
            if time.time() - t0 > budget_s or failures >= 6:
                break
            n = ted_count(key, divisions, q - pd.offsets.QuarterBegin(1, startingMonth=1), q)
            if n is None:
                failures += 1
            else:
                failures = 0
                got[q] = n
        if got:
            cols[key] = pd.Series(got).sort_index()
        print(f"TED {key}: {len(got)} trimestres ({time.time() - t0:.0f} s)", flush=True)
        if failures >= 6:
            warn("TED: 6 pedidos falhados seguidos, desisto por agora")
            break
    if old is not None:
        for key in old:
            cols.setdefault(key, old[key].dropna())
    return pd.DataFrame(cols) if cols else None


EPU_SITE = {  # the authors' own files, tried before FRED (slow from the cloud): column text -> FRED id
    "https://www.policyuncertainty.com/media/Categorical_EPU_Data.xlsx": {
        "government spending": "EPUGOVTSPEND", "national security": "EPUNATSEC", "trade policy": "EPUTRADE",
        "health care": "EPUHEALTHCARE", "regulation": "EPUREG"},
    "https://www.policyuncertainty.com/media/Europe_Policy_Uncertainty_Data.xlsx": {"european_news_index": EU_EPU},
}


def epu_site() -> dict[str, pd.Series]:
    out = {}
    for url, wanted in EPU_SITE.items():
        try:
            resp = requests.get(url, timeout=90, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            df = pd.read_excel(io.BytesIO(resp.content))
            cols = {str(c).strip().lower(): c for c in df.columns}
            year, month = cols.get("year"), cols.get("month")
            if not year or not month:
                warn(f"EPU {url}: colunas inesperadas {list(df.columns)[:6]}")
                continue
            df = df[pd.to_numeric(df[year], errors="coerce").notna()]
            idx = pd.to_datetime({"year": df[year].astype(int), "month": df[month].astype(int), "day": 1}) + pd.offsets.MonthEnd(0)
            for text, series in wanted.items():
                match = [c for low, c in cols.items() if text in low and not (text == "regulation" and "financial" in low)]
                if match:
                    out[series] = pd.Series(pd.to_numeric(df[match[0]], errors="coerce").to_numpy(), index=idx).dropna()
        except Exception as e:  # noqa: BLE001 - FRED is tried next
            warn(f"EPU {url}: {e!r}")
    return out


def download_epu() -> pd.DataFrame | None:
    import context

    cols = epu_site()
    for series in [*EPU_CATEGORIES, EU_EPU]:
        if series in cols and len(cols[series]) > 200:
            continue
        s = context.fred_in_pieces(series, 1985, years=10)  # one long request times out from the cloud
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
    groups = [g.sort_values("available") for _, g in fund.groupby("symbol")]
    for t in months:
        now, before = {}, {}
        for g in groups:
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
        for k, v in now.items():
            if before.get(k, 0) > 0:
                out.loc[t, k] = v / before[k] - 1
    return out


def refresh() -> dict[str, str]:
    """Each source saved as soon as it is ready (the step may be stopped by its time limit): uncertainty, US spending,
    European procurement, then investment."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    done = {}

    def keep(name: str, frame: pd.DataFrame | None, what: str) -> None:
        if frame is not None and len(frame):
            frame.to_csv(CACHE_DIR / f"{name}.csv")
            done[name] = f"{frame.index.min().date()} a {frame.index.max().date()} ({frame.shape[1]} {what})"
            print(f"{name}: {done[name]}", flush=True)

    keep("epu_categorias", download_epu(), "séries")
    keep("gasto_publico", download_spending(budget_s=900), "setores")
    keep("gasto_publico_eu", download_procurement(budget_s=600), "setores")
    try:
        import fundamentals
        from rankings import load_constituents

        sector_of = {c["symbol"]: c["sector"] for c in load_constituents()}
        keep("capex_setor", capex_growth(pd.concat(fundamentals.load().values()).reset_index(), sector_of), "setores")
    except Exception as e:  # noqa: BLE001 - the other files are kept
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


def gov_signal(obligations: pd.DataFrame, per_year: int = 12, lag_months: int = GOV_LAG_MONTHS) -> pd.DataFrame:
    """Period ends (when known) x sectors: the surprise in public spending growth (the last year against the one
    before, minus that growth's past mean), z-scored across sectors."""
    ttm = obligations.clip(lower=0).rolling(per_year, min_periods=per_year).sum()
    growth = np.log(ttm / ttm.shift(per_year)).replace([np.inf, -np.inf], np.nan)
    surprise = growth - growth.expanding(min_periods=per_year).mean().shift(1)
    return known_from(zscore(surprise), lag_months)


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
    eu_gov = cached("gasto_publico_eu")
    if eu_gov is not None and len(eu_gov):
        for k, s in gov_signal(eu_gov, per_year=4, lag_months=TED_LAG_MONTHS).items():
            cols[f"{k}_gov"] = s
    epu = cached("epu_categorias")

    def month(s: pd.Series) -> pd.Series:
        s = s.copy()
        if s.index.tz is not None:
            s.index = s.index.tz_localize(None)
        return s.resample("ME").last().pct_change()

    spy = raw.get("spy")
    us_cats = [c for c in EPU_CATEGORIES if epu is not None and c in epu]
    if us_cats and spy is not None:
        etfs = {}
        for sector, tickers in context.SECTOR_ETFS.items():
            px = None
            for t in tickers:
                if f"etf:{t}" in raw:
                    px = raw[f"etf:{t}"] if px is None else px.combine_first(raw[f"etf:{t}"])
            if px is not None:
                etfs[context.sector_key(sector)] = month(px)
        for k, s in epu_signal(epu[us_cats].resample("ME").last(), etfs, month(spy)).items():
            cols[f"{k}_epub"] = s
    stoxx = raw.get(f"etf:{context.EU_BENCHMARK}")
    if epu is not None and EU_EPU in epu and stoxx is not None:
        etfs = {context.sector_key(sec, us=False): month(raw[f"etf:{t}"])
                for sec, (t,) in context.EU_SECTOR_ETFS.items() if f"etf:{t}" in raw}
        for k, s in epu_signal(epu[[EU_EPU]].resample("ME").last(), etfs, month(stoxx)).items():
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
