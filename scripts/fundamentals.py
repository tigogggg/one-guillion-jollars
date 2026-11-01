"""Company fundamentals as they were known on each day, from the SEC's XBRL filings (EDGAR).

Why EDGAR: it is free, goes back to about 2009, includes companies that later left the index, and every
number carries the date it was filed, so the backtest only uses what was public that day (no look-ahead).
Yahoo's fundamentals are today's snapshot only, which would leak the future into 20 years of training.

For each company and each reported quarter-end: trailing-12-month revenue, operating income, net income,
operating cash flow and capital expenditure, plus current assets and liabilities, cash, debt, equity and
shares outstanding, available from the filing date. From these and the day's price:

- fund_q (0-100, quality): operating margin, revenue growth, free-cash-flow margin, liquidity (current
  ratio) and leverage (net debt / operating cash flow). QUALITY_RULES.
- fund_v (0-100, value): earnings yield, free-cash-flow yield and the margin of safety against a simple
  discounted-cash-flow value. VALUE_RULES.
- mos: margin of safety = 1 - price / intrinsic value per share (DCF: free cash flow per share growing
  with revenue for 5 years, capped at 15%, fading to 3% by year 10, discounted at 9%, 2.5% after).
- rev_growth, op_margin, fcf_yield, earn_yield, current_ratio, pe for the portal and Claude.

Missing data never blocks a stock (banks have no current ratio, some filers miss concepts): a score is
computed from the parts available, and is None when too little is known.

    uv run python scripts/fundamentals.py --atualizar   # downloads EDGAR into data/fundamentals/ (Actions)
"""

from __future__ import annotations

import argparse
import gzip
import io
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

DATA = Path(__file__).resolve().parents[1] / "data" / "fundamentals" / "fundamentals.csv.gz"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
# The SEC asks automated clients to identify themselves (contact in SEC_USER_AGENT when set).
USER_AGENT = os.environ.get("SEC_USER_AGENT") or "OneGuillionJollars research github.com/tigogggg/one-guillion-jollars"
STALE_DAYS = 400  # older filings than this no longer count

FLOWS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "RevenuesNetOfInterestExpense"],
    "op_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
}
INSTANTS = {
    "assets_cur": ["AssetsCurrent"],
    "liab_cur": ["LiabilitiesCurrent"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "debt": ["LongTermDebt", "LongTermDebtNoncurrent", "DebtInstrumentCarryingAmount"],
    "debt_cur": ["LongTermDebtCurrent", "DebtCurrent"],
    "equity": ["StockholdersEquity"],
}
COLUMNS = ["symbol", "end", "available", *FLOWS, *INSTANTS, "shares"]

QUALITY_RULES = [
    "Margem operacional: 20% ou mais = 25 pontos; 0% ou menos = 0.",
    "Crescimento das receitas (12 meses face aos 12 anteriores): 15% ou mais = 20; -5% ou pior = 0.",
    "Margem de cash flow livre (cash flow operacional menos investimento, face às receitas): 15% ou mais = 20; 0% ou menos = 0.",
    "Liquidez (ativo corrente / passivo corrente): 1,5 ou mais = 15; 0,8 ou menos = 0.",
    "Endividamento (dívida líquida / cash flow operacional): sem dívida líquida = 20; 4 anos ou mais = 0.",
]
VALUE_RULES = [
    "Rendimento dos lucros (lucro / capitalização, o inverso do P/E): 7% ou mais = 35 pontos; 0% ou menos = 0.",
    "Rendimento do cash flow livre (FCF / capitalização): 6% ou mais = 35; 0% ou menos = 0.",
    "Margem de segurança face ao valor intrínseco (DCF): 30% ou mais abaixo do valor = 30; 50% ou mais acima = 0.",
]
DCF = {"years_high": 5, "growth_cap": 0.15, "growth_floor": 0.0, "fade_to": 0.03, "discount": 0.09, "terminal": 0.025}


# --------------------------------------------------------------------------
# Scores (pure functions, the same live and in the backtest)
# --------------------------------------------------------------------------


def lerp(x, best: float, worst: float, points: float):
    """points at `best` or better, 0 at `worst` or worse (works on numbers and numpy arrays)."""
    x = np.asarray(x, dtype=float)
    frac = (x - worst) / (best - worst)
    return np.clip(frac, 0.0, 1.0) * points


def intrinsic_per_share(fcf: float, shares: float, growth: float | None) -> float | None:
    """A plain two-stage DCF of free cash flow per share; None without positive free cash flow."""
    if not fcf or not shares or fcf <= 0 or shares <= 0:
        return None
    g1 = min(DCF["growth_cap"], max(DCF["growth_floor"], growth if growth is not None and np.isfinite(growth) else DCF["fade_to"]))
    r, cash, value = DCF["discount"], fcf / shares, 0.0
    for year in range(1, 11):
        g = g1 if year <= DCF["years_high"] else g1 + (DCF["fade_to"] - g1) * (year - DCF["years_high"]) / (10 - DCF["years_high"])
        cash *= 1 + g
        value += cash / (1 + r) ** year
    value += cash * (1 + DCF["terminal"]) / (r - DCF["terminal"]) / (1 + r) ** 10
    return value


def _combine(parts: list[tuple[np.ndarray, float]], min_max: float) -> np.ndarray:
    """Sum of the available parts' points, scaled to 0-100 by the maximum of those available; NaN when
    the available maximum is below min_max."""
    pts = np.zeros_like(parts[0][0], dtype=float)
    mx = np.zeros_like(parts[0][0], dtype=float)
    for values, top in parts:
        ok = np.isfinite(values)
        pts += np.where(ok, values, 0.0)
        mx += np.where(ok, top, 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(mx >= min_max, pts / np.where(mx > 0, mx, 1) * 100, np.nan)


def ratios(rows: pd.DataFrame) -> pd.DataFrame:
    """Per filing: the price-independent ratios, the quality score and the intrinsic value per share."""
    r = pd.DataFrame(index=rows.index)
    rev, ocf, capex = rows["revenue"], rows["ocf"], rows["capex"].fillna(0)
    with np.errstate(invalid="ignore", divide="ignore"):
        r["op_margin"] = rows["op_income"] / rev.where(rev > 0)
        r["rev_growth"] = rows["rev_growth"]
        r["fcf"] = ocf - capex
        r["fcf_margin"] = r["fcf"] / rev.where(rev > 0)
        r["current_ratio"] = rows["assets_cur"] / rows["liab_cur"].where(rows["liab_cur"] > 0)
        net_debt = rows["debt"].fillna(0) + rows["debt_cur"].fillna(0) - rows["cash"].fillna(0)
        has_debt_info = rows[["debt", "debt_cur", "cash"]].notna().any(axis=1)
        lev = np.where(net_debt <= 0, -1.0, net_debt / ocf.where(ocf > 0))
        lev = np.where(has_debt_info, lev, np.nan)
        lev = np.where((net_debt > 0) & ~(ocf > 0) & has_debt_info, 99.0, lev)
    r["leverage"] = lev
    r["net_income"] = rows["net_income"]
    r["shares"] = rows["shares"]
    q_parts = [
        (np.where(np.isfinite(r["op_margin"]), lerp(r["op_margin"], 0.20, 0.0, 25), np.nan), 25),
        (np.where(np.isfinite(r["rev_growth"]), lerp(r["rev_growth"], 0.15, -0.05, 20), np.nan), 20),
        (np.where(np.isfinite(r["fcf_margin"]), lerp(r["fcf_margin"], 0.15, 0.0, 20), np.nan), 20),
        (np.where(np.isfinite(r["current_ratio"]), lerp(r["current_ratio"], 1.5, 0.8, 15), np.nan), 15),
        (np.where(np.isfinite(r["leverage"]), np.where(r["leverage"] < 0, 20.0, lerp(r["leverage"], 0.0, 4.0, 20)), np.nan), 20),
    ]
    r["fund_q"] = _combine(q_parts, 40)
    r["intrinsic"] = [intrinsic_per_share(f, s, g) for f, s, g in zip(r["fcf"], rows["shares"], rows["rev_growth"])]
    return r


def valuation(r: pd.DataFrame, price) -> dict[str, np.ndarray]:
    """Price-dependent fields for ratios `r` (aligned rows) at `price` (array or number)."""
    price = np.asarray(price, dtype=float)
    shares = np.asarray(r["shares"], dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        mcap = np.where(shares > 0, price * shares, np.nan)
        earn_yield = np.asarray(r["net_income"], dtype=float) / mcap
        fcf_yield = np.asarray(r["fcf"], dtype=float) / mcap
        intrinsic = np.asarray(r["intrinsic"], dtype=float)
        mos = np.where(intrinsic > 0, 1 - price / intrinsic, np.nan)
        pe = np.where(earn_yield > 0, 1 / earn_yield, np.nan)
    v_parts = [
        (np.where(np.isfinite(earn_yield), lerp(earn_yield, 0.07, 0.0, 35), np.nan), 35),
        (np.where(np.isfinite(fcf_yield), lerp(fcf_yield, 0.06, 0.0, 35), np.nan), 35),
        (np.where(np.isfinite(mos), lerp(mos, 0.30, -0.50, 30), np.nan), 30),
    ]
    return {"fund_v": _combine(v_parts, 60), "mos": mos, "earn_yield": earn_yield, "fcf_yield": fcf_yield, "pe": pe}


DAILY_COLS = ["fund_q", "fund_v", "mos", "rev_growth", "op_margin", "fcf_yield", "earn_yield", "current_ratio", "pe"]


def daily(rows: pd.DataFrame, close: pd.Series) -> pd.DataFrame:
    """DAILY_COLS for every day of `close`, from the filings known that day (rows indexed by availability)."""
    if rows is None or rows.empty:
        return pd.DataFrame(index=close.index, columns=DAILY_COLS, dtype=float)
    r = ratios(rows).sort_index()
    r = r[~r.index.duplicated(keep="last")]
    idx = close.index
    pos = r.index.searchsorted(idx, side="right") - 1  # last filing available on or before each day
    ok = pos >= 0
    take = r.iloc[np.where(ok, pos, 0)].reset_index(drop=True)
    age = (idx - r.index[np.where(ok, pos, 0)]).days.to_numpy()
    fresh = ok & (age <= STALE_DAYS)
    out = pd.DataFrame(index=idx)
    v = valuation(take, close.to_numpy())
    for c in ("fund_q", "rev_growth", "op_margin", "current_ratio"):
        out[c] = np.where(fresh, take[c].to_numpy(dtype=float), np.nan)
    for c in ("fund_v", "mos", "fcf_yield", "earn_yield", "pe"):
        out[c] = np.where(fresh, v[c], np.nan)
    return out


def latest(rows: pd.DataFrame | None, price: float, today: date) -> dict:
    """DAILY_COLS for today's price from the latest filing (live), as plain floats or None."""
    if rows is None or rows.empty:
        return {}
    d = daily(rows, pd.Series([price], index=pd.DatetimeIndex([pd.Timestamp(today)])))
    return {k: (round(float(v), 4) if pd.notna(v) else None) for k, v in d.iloc[0].items()}


# --------------------------------------------------------------------------
# EDGAR: download and turn companyfacts into point-in-time rows
# --------------------------------------------------------------------------


def _entries(facts: dict, concepts: list[str], unit: str = "USD") -> list[dict]:
    out = []
    for taxonomy in ("us-gaap", "dei"):
        for concept in concepts:
            units = (facts.get(taxonomy, {}).get(concept) or {}).get("units", {})
            for e in units.get(unit, []):
                if e.get("form", "").startswith(("10-Q", "10-K")) and e.get("filed") and e.get("end"):
                    out.append(e)
    return out


def _first_filed(entries: list[dict], key) -> dict:
    """{key(entry): (value, filed)} keeping the earliest filing of each fact (what was known first)."""
    out = {}
    for e in entries:
        k = key(e)
        if k not in out or e["filed"] < out[k][1]:
            out[k] = (float(e["val"]), e["filed"])
    return out


def _ttm(entries: list[dict]) -> dict[str, tuple[float, str]]:
    """{period end: (trailing-12-month value, filed)} from annual, quarterly and year-to-date facts."""
    dur = _first_filed([e for e in entries if e.get("start")], lambda e: (e["start"], e["end"]))
    by_end: dict[str, list] = {}
    for (start, end), (val, filed) in dur.items():
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days
        by_end.setdefault(end, []).append((days, start, val, filed))
    annual = {end: (v, f) for end, xs in by_end.items() for d, s, v, f in xs if 350 <= d <= 380}

    def near(target: date, pool, tol: int = 12):
        best = None
        for end in pool:
            gap = abs((date.fromisoformat(end) - target).days)
            if gap <= tol and (best is None or gap < best[0]):
                best = (gap, end)
        return best[1] if best else None

    out = {}
    for end, xs in by_end.items():
        if end in annual:
            out[end] = annual[end]
            continue
        ytd = max((x for x in xs if 80 <= x[0] < 350), default=None, key=lambda x: x[0])  # longest period ending here
        if not ytd:
            continue
        days, start, val, filed = ytd
        prev_annual = near(date.fromisoformat(start) - timedelta(days=1), annual)
        prev_end = near(date.fromisoformat(end) - timedelta(days=365), by_end)
        if not prev_annual or not prev_end:
            continue
        prev_ytd = next((x for x in by_end[prev_end] if abs(x[0] - days) <= 15), None)
        if not prev_ytd:
            continue
        out[end] = (val + annual[prev_annual][0] - prev_ytd[2], max(filed, prev_ytd[3]))
    return out


def company_rows(symbol: str, facts: dict) -> pd.DataFrame:
    """Point-in-time rows of one company (COLUMNS, plus rev_growth) from its companyfacts JSON."""
    facts = facts.get("facts", {})
    table: dict[str, dict] = {}
    for name, concepts in FLOWS.items():
        series: dict[str, tuple[float, str]] = {}
        for concept in concepts:  # companies switch concepts over the years: fill the gaps
            for end, v in _ttm(_entries(facts, [concept])).items():
                series.setdefault(end, v)
        for end, (val, filed) in series.items():
            row = table.setdefault(end, {"end": end, "filed": filed})
            row[name] = val
            row["filed"] = max(row["filed"], filed)
    for name, concepts in INSTANTS.items():
        found: dict[str, tuple[float, str]] = {}
        for concept in concepts:
            for end, v in _first_filed([e for e in _entries(facts, [concept]) if not e.get("start")], lambda e: e["end"]).items():
                found.setdefault(end, v)
        for end, (val, filed) in found.items():
            if end in table:
                table[end][name] = val
    shares = _first_filed([e for e in _entries(facts, ["EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"], "shares")],
                          lambda e: e["end"])
    if not table:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.DataFrame(table.values())
    df["end"] = pd.to_datetime(df["end"])
    df["available"] = pd.to_datetime(df["filed"])
    df = df.drop(columns="filed").sort_values("end")
    sh = pd.Series({pd.Timestamp(k): v[0] for k, v in shares.items()}).sort_index() if shares else pd.Series(dtype=float)
    # shares: the latest count reported by the filing date (cover-page counts are dated near the filing)
    df["shares"] = [sh[:a].iloc[-1] if len(sh[:a]) else np.nan for a in df["available"]] if len(sh) else np.nan
    for c in COLUMNS:
        if c not in df:
            df[c] = np.nan
    df["symbol"] = symbol
    return df[COLUMNS].reset_index(drop=True)


def with_growth(df: pd.DataFrame) -> pd.DataFrame:
    """Add rev_growth: TTM revenue against the TTM revenue of the period ending about a year earlier."""
    out = []
    for _, g in df.groupby("symbol", sort=False):
        g = g.sort_values("end").copy()
        ends = g["end"].to_numpy()
        revs = g["revenue"].to_numpy(dtype=float)
        growth = []
        for e, rv in zip(ends, revs):
            target = e - np.timedelta64(365, "D")
            j = np.argmin(np.abs(ends - target)) if len(ends) else None
            ok = j is not None and abs((ends[j] - target) / np.timedelta64(1, "D")) <= 20 and revs[j] > 0
            growth.append(rv / revs[j] - 1 if ok and np.isfinite(rv) else np.nan)
        g["rev_growth"] = growth
        out.append(g)
    return pd.concat(out) if out else df.assign(rev_growth=np.nan)


def download(symbols: list[str], pause: float = 0.12) -> pd.DataFrame:
    """Companyfacts for every symbol the SEC can map to a CIK; one request each (SEC limit: 10 per second)."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
    tickers = session.get(TICKERS_URL, timeout=60).json()
    cik_of = {v["ticker"].upper().replace(".", "-"): int(v["cik_str"]) for v in tickers.values()}
    frames, missing, failed = [], [], []
    for i, sym in enumerate(sorted(set(symbols))):
        cik = cik_of.get(sym.upper())
        if not cik:
            missing.append(sym)
            continue
        for attempt in range(3):
            try:
                resp = session.get(FACTS_URL.format(cik=cik), timeout=60)
                if resp.status_code == 404:
                    break
                resp.raise_for_status()
                rows = company_rows(sym, resp.json())
                if not rows.empty:
                    frames.append(rows)
                break
            except (requests.RequestException, ValueError) as e:
                if attempt == 2:
                    failed.append(f"{sym}: {e}")
                time.sleep(2 * (attempt + 1))
        time.sleep(pause)
        if i % 100 == 0:
            print(f"{i} empresas...", file=sys.stderr, flush=True)
    print(f"::notice title=Fundamentos::{len(frames)} empresas com dados; {len(missing)} sem CIK (saíram da bolsa ou mudaram de "
          f"ticker); {len(failed)} falharam", flush=True)
    if failed:
        print("::warning title=Fundamentos::" + "; ".join(failed[:10]), flush=True)
    return with_growth(pd.concat(frames, ignore_index=True)) if frames else pd.DataFrame(columns=COLUMNS + ["rev_growth"])


def save(df: pd.DataFrame, path: Path = DATA) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df = df.copy()
    for c in ("end", "available"):
        df[c] = pd.to_datetime(df[c]).dt.strftime("%Y-%m-%d")
    path.write_bytes(gzip.compress(df.to_csv(index=False, float_format="%.6g").encode(), mtime=0))


_CACHE: dict[str, pd.DataFrame] | None = None


def load(path: Path = DATA) -> dict[str, pd.DataFrame]:
    """{symbol: rows indexed by availability date} from the saved file; empty without it."""
    global _CACHE
    if _CACHE is not None and path == DATA:
        return _CACHE
    if not path.exists():
        return {}
    df = pd.read_csv(io.BytesIO(gzip.decompress(path.read_bytes())), parse_dates=["end", "available"])
    out = {s: g.set_index("available").sort_index() for s, g in df.groupby("symbol")}
    if path == DATA:
        _CACHE = out
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--atualizar", action="store_true", help="Download EDGAR fundamentals into data/fundamentals/")
    parser.add_argument("--simbolos", default="", help="Only these symbols (comma-separated), for a test")
    a = parser.parse_args()
    if not a.atualizar:
        parser.print_help()
        return 0
    if a.simbolos:
        symbols = [s.strip().upper() for s in a.simbolos.split(",") if s.strip()]
    else:
        from pretrain import load_membership
        from rankings import load_constituents

        symbols = {c["symbol"] for c in load_constituents()}
        try:
            since = date(date.today().year - 21, 1, 1)
            symbols |= {t for t, spans in load_membership().items() if any(e is None or e >= since for _, e in spans)}
        except requests.RequestException as e:
            print(f"::warning title=Fundamentos::sem composição histórica ({e})")
        symbols = sorted(symbols)
    df = download(symbols)
    if df.empty:
        print("::error title=Fundamentos::nada descarregado")
        return 1
    save(df)
    cover = df.groupby("symbol")["end"].min()
    print(f"::notice title=Fundamentos::{df['symbol'].nunique()} empresas, {len(df)} trimestres; primeiro trimestre mediano "
          f"{pd.to_datetime(cover).median().date()}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
