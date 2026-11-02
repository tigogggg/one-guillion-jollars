"""Earnings dates of US stocks: the next one live (Yahoo Finance), the past ones for the backtest (SEC EDGAR).

Live: Yahoo gives one date, or two (a window) when the date is not confirmed; the first one is used.
EUR-listed ETFs have no earnings and are skipped.

Backtest: every 8-K a company filed with item 2.02 ("Results of Operations and Financial Condition") is an
earnings release, and the SEC lists them all with their dates since 2004. data/earnings/earnings_dates.csv.gz
keeps them (refreshed weekly by context-data.yml), so the backtest applies the same "no buys just before the
results" rule as the live agents. It knows each date exactly, while a live agent sees the announced date a few
weeks ahead: close enough for a rule that looks at most dias_resultados (10) days ahead.

    uv run python scripts/earnings.py --atualizar     # downloads the dates into data/earnings/ (Actions)
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import io
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import yfinance as yf

HISTORY = Path(__file__).resolve().parents[1] / "data" / "earnings" / "earnings_dates.csv.gz"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
OLDER_URL = "https://data.sec.gov/submissions/{name}"
SINCE = "2004-08-23"  # item 2.02 exists since the 8-K rules of August 2004

WEEKDAYS_PT = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
NO_EARNINGS_SUFFIXES = (".DE", ".AS", ".PA", ".MI", ".L", ".SW")


def next_earnings(symbol: str, today: date | None = None) -> date | None:
    if symbol.endswith(NO_EARNINGS_SUFFIXES) or "=" in symbol:
        return None
    today = today or date.today()
    try:
        cal = yf.Ticker(symbol).calendar or {}
    except Exception as e:  # yfinance raises many types, incl. rate limits
        print(f"WARN: resultados {symbol}: {e}", file=sys.stderr)
        return None
    dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
    upcoming = sorted(d for d in dates or [] if isinstance(d, date) and d >= today)
    return upcoming[0] if upcoming else None


def next_earnings_many(symbols, today: date | None = None, pause: float = 0.2) -> dict[str, date | None]:
    out = {}
    for s in dict.fromkeys(symbols):
        out[s] = next_earnings(s, today)
        time.sleep(pause)
    return out


def days_until(d: date | None, today: date | None = None) -> int | None:
    return (d - (today or date.today())).days if d else None


def within(d: date | None, days: int, today: date | None = None) -> bool:
    n = days_until(d, today)
    return n is not None and 0 <= n <= days


def label(d: date) -> str:
    """e.g. 'qui 5/11'."""
    return f"{WEEKDAYS_PT[d.weekday()]} {d.day}/{d.month}"


def warning(d: date | None, today: date | None = None) -> str:
    """Human line for a card, e.g. 'resultados qui 5/11 (daqui a 3 dias)'."""
    n = days_until(d, today)
    if n is None:
        return ""
    when = "hoje" if n == 0 else "amanhã" if n == 1 else f"daqui a {n} dias"
    return f"resultados {label(d)} ({when})"


def next_week(today: date | None = None) -> tuple[date, date]:
    """Monday to Friday of the next calendar week."""
    today = today or date.today()
    monday = today + timedelta(days=7 - today.weekday())
    return monday, monday + timedelta(days=4)


# --------------------------------------------------------------------------
# Past earnings dates (SEC EDGAR), for the backtest
# --------------------------------------------------------------------------


def report_dates(filings: dict) -> set[str]:
    """Filing dates of the 8-Ks with item 2.02 in one block of an EDGAR submissions file (parallel columns)."""
    forms, dates, items = filings.get("form", []), filings.get("filingDate", []), filings.get("items", [])
    return {d for f, d, it in zip(forms, dates, items) if f == "8-K" and "2.02" in str(it or "").split(",") and d >= SINCE}


def company_dates(session, cik: int) -> set[str]:
    """Every earnings release date of one company: the recent filings plus the older pages back to SINCE."""
    sub = session.get(SUBMISSIONS_URL.format(cik=cik), timeout=60)
    if sub.status_code == 404:
        return set()
    sub.raise_for_status()
    sub = sub.json()
    out = report_dates(sub.get("filings", {}).get("recent", {}))
    for page in sub.get("filings", {}).get("files", []):
        if page.get("filingTo", "9999") < SINCE:
            continue
        time.sleep(0.12)
        resp = session.get(OLDER_URL.format(name=page["name"]), timeout=60)
        resp.raise_for_status()
        out |= report_dates(resp.json())
    return out


def download_history(symbols: list[str], pause: float = 0.12) -> dict[str, list[str]]:
    """{symbol: sorted dates} for every symbol the SEC can map to a CIK (SEC limit: 10 requests a second)."""
    import fundamentals
    import requests

    session, cik_of = fundamentals.sec_session()
    out, missing, failed = {}, [], []
    for i, sym in enumerate(sorted(set(symbols))):
        cik = cik_of.get(sym.upper())
        if not cik:
            missing.append(sym)
            continue
        for attempt in range(3):
            try:
                dates = company_dates(session, cik)
                if dates:
                    out[sym] = sorted(dates)
                break
            except (requests.RequestException, ValueError) as e:
                if attempt == 2:
                    failed.append(f"{sym}: {e}")
                time.sleep(2 * (attempt + 1))
        time.sleep(pause)
        if i % 100 == 0:
            print(f"{i} empresas...", file=sys.stderr, flush=True)
    print(f"::notice title=Resultados::{len(out)} empresas com datas de resultados; {len(missing)} sem CIK; "
          f"{len(failed)} falharam", flush=True)
    if failed:
        print("::warning title=Resultados::" + "; ".join(failed[:10]), flush=True)
    return out


def save_history(dates: dict[str, list[str]], path: Path = HISTORY) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = "".join(f"{s},{d}\n" for s in sorted(dates) for d in dates[s])
    path.write_bytes(gzip.compress(("symbol,date\n" + rows).encode(), mtime=0))


_HISTORY: dict[str, list[date]] | None = None


def load_history(path: Path = HISTORY) -> dict[str, list[date]]:
    """{symbol: sorted past earnings dates} from the saved file; empty without it."""
    global _HISTORY
    if _HISTORY is not None and path == HISTORY:
        return _HISTORY
    out: dict[str, list[date]] = {}
    if path.exists():
        for line in io.StringIO(gzip.decompress(path.read_bytes()).decode()).readlines()[1:]:
            sym, d = line.strip().split(",")
            out.setdefault(sym, []).append(date.fromisoformat(d))
    for v in out.values():
        v.sort()
    if path == HISTORY:
        _HISTORY = out
    return out


def next_from_history(dates: list[date] | None, today: date) -> date | None:
    """The first earnings date on or after `today` in a sorted list, or None."""
    if not dates:
        return None
    i = bisect.bisect_left(dates, today)
    return dates[i] if i < len(dates) else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--atualizar", action="store_true", help="Download the past earnings dates into data/earnings/")
    parser.add_argument("--simbolos", default="", help="Only these symbols (comma-separated), for a test")
    a = parser.parse_args()
    if not a.atualizar:
        parser.print_help()
        return 0
    import fundamentals

    symbols = [s.strip().upper() for s in a.simbolos.split(",") if s.strip()] or fundamentals.universe()
    try:
        dates = download_history(symbols)
    except RuntimeError as e:
        print(f"::error title=Resultados::{e}")
        return 1
    if not dates:
        print("::error title=Resultados::nada descarregado")
        return 1
    save_history(dates)
    return 0


if __name__ == "__main__":
    sys.exit(main())
