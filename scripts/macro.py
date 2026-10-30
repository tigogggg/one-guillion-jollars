"""High-impact US macro events (Fed, inflation, jobs, GDP) for the next days.

Source: FMP's economic calendar (one API call). When it is not available on
the key's plan, falls back to the Fed's published 2026 meeting dates and the
usual first-Friday jobs report.
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import requests

from earnings import label

FMP_URL = "https://financialmodelingprep.com/stable/economic-calendar"
# Second day of each FOMC meeting (rate decision), as published by the Fed.
FOMC_2026 = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09"]
NAMES_PT = [
    ("interest rate decision", "Decisão de juros da Fed"),
    ("fomc", "Fed (FOMC)"),
    ("core pce", "Inflação PCE core"),
    ("pce", "Inflação PCE"),
    ("core inflation rate", "Inflação core (CPI)"),
    ("inflation rate", "Inflação (CPI)"),
    ("cpi", "Inflação (CPI)"),
    ("ppi", "Preços no produtor (PPI)"),
    ("non farm payrolls", "Emprego (payrolls)"),
    ("nonfarm payrolls", "Emprego (payrolls)"),
    ("unemployment rate", "Taxa de desemprego"),
    ("gdp", "PIB"),
    ("retail sales", "Vendas a retalho"),
    ("ism", "Índice ISM"),
]


def name_pt(event: str) -> str:
    low = event.lower()
    return next((pt for key, pt in NAMES_PT if key in low), event)


def fmp_events(start: date, end: date) -> list[tuple[date, str]] | None:
    key = os.environ.get("FMP_API_KEY")
    if not key:
        return None
    try:
        resp = requests.get(FMP_URL, params={"from": start.isoformat(), "to": end.isoformat()},
                            headers={"apikey": key}, timeout=30)
        rows = resp.json() if resp.status_code == 200 else None
    except (requests.RequestException, ValueError) as e:
        print(f"WARN: calendário macro FMP: {e}", file=sys.stderr)
        return None
    if not isinstance(rows, list):
        print(f"WARN: calendário macro FMP indisponível ({resp.status_code})", file=sys.stderr)
        return None
    events = []
    for r in rows:
        if r.get("country") != "US" or str(r.get("impact", "")).lower() != "high":
            continue
        try:
            d = date.fromisoformat(str(r["date"])[:10])
        except (KeyError, ValueError):
            continue
        events.append((d, name_pt(r.get("event", ""))))
    return sorted(set(events))


def fallback_events(start: date, end: date) -> list[tuple[date, str]]:
    events = [(date.fromisoformat(d), "Decisão de juros da Fed") for d in FOMC_2026]
    d = start
    while d <= end:
        if d.weekday() == 4 and d.day <= 7:
            events.append((d, "Emprego (payrolls, data habitual)"))
        d += timedelta(days=1)
    return sorted(e for e in events if start <= e[0] <= end)


def events(start: date, end: date) -> tuple[list[tuple[date, str]], bool]:
    """(events, from_fmp) between start and end inclusive."""
    found = fmp_events(start, end)
    return (found, True) if found is not None else (fallback_events(start, end), False)


def by_day_lines(evts: list[tuple[date, str]]) -> list[str]:
    days: dict[date, list[str]] = {}
    for d, name in evts:
        if name not in days.setdefault(d, []):
            days[d].append(name)
    return [f"{label(d)}: " + ", ".join(names) for d, names in sorted(days.items())]


def today_tomorrow_line(today: date | None = None) -> str | None:
    """'🏛️ Agenda macro: hoje …; amanhã …' for the daily tips, or None."""
    today = today or date.today()
    tomorrow = today + timedelta(days=3 if today.weekday() == 4 else 1)
    evts, _ = events(today, tomorrow)
    parts = []
    for d, word in ((today, "hoje"), (tomorrow, "amanhã" if tomorrow == today + timedelta(days=1) else "segunda")):
        names = list(dict.fromkeys(n for e, n in evts if e == d))
        if names:
            parts.append(f"{word} {', '.join(names)}")
    return "🏛️ **Agenda macro:** " + "; ".join(parts) if parts else None
