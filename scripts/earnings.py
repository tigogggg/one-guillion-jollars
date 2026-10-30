"""Next earnings dates of US stocks, from Yahoo Finance (yfinance), one request per symbol.

Yahoo gives one date, or two (a window) when the date is not confirmed; the
first one is used. EUR-listed ETFs have no earnings and are skipped.
"""

from __future__ import annotations

import sys
import time
from datetime import date, timedelta

import yfinance as yf

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
