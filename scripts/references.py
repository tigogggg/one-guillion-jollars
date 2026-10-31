"""Inflation and cash reference lines for the agents' return charts.

Inflação: euro area HICP (Eurostat, monthly index). A month's value is used
for every day of that month and held flat until the next one is published.
Depósito: money left at the ECB deposit facility rate, compounded daily and
floored at 0% (savers did not pay the negative rates of 2014-2022).

Both are only worth drawing over long spans: over a few weeks they sit on the
zero line, so reference_lines() returns nothing below MIN_DAYS.
"""

from __future__ import annotations

import csv
import io
import sys
from datetime import date, timedelta

import requests

HICP_URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_minr"
HICP_PARAMS = {"geo": "EA", "coicop18": "TOTAL", "unit": "I25", "format": "JSON"}
DFR_URL = "https://data-api.ecb.europa.eu/service/data/FM/D.U2.EUR.4F.KR.DFR.LEV"
MIN_DAYS = 180
INFLATION = "Inflação"
CASH = "Depósito"


def hicp_index(since: date) -> dict[str, float] | None:
    """Monthly HICP index by "YYYY-MM", or None when Eurostat is unreachable."""
    try:
        resp = requests.get(HICP_URL, params={**HICP_PARAMS, "sinceTimePeriod": f"{since:%Y-%m}"}, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        months = data["dimension"]["time"]["category"]["index"]
        values = data["value"]
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"WARN: inflação (Eurostat): {e}", file=sys.stderr)
        return None
    index = {month: float(values[str(pos)]) for month, pos in months.items() if str(pos) in values}
    return index or None


def deposit_rates(since: date) -> list[tuple[date, float]] | None:
    """ECB deposit facility rate (% a year) on each day it was published, or None."""
    try:
        resp = requests.get(DFR_URL, params={"format": "csvdata", "startPeriod": (since - timedelta(days=60)).isoformat()},
                            timeout=60)
        resp.raise_for_status()
        rows = [(date.fromisoformat(r["TIME_PERIOD"]), float(r["OBS_VALUE"]))
                for r in csv.DictReader(io.StringIO(resp.text)) if r.get("OBS_VALUE")]
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"WARN: taxa de depósito (BCE): {e}", file=sys.stderr)
        return None
    return sorted(rows) or None


def inflation_curve(dates: list[date], index: dict[str, float]) -> list[tuple[str, float]]:
    """Cumulative inflation (%) since dates[0], at each date."""
    months = sorted(index)
    points, last = [], None
    for d in dates:
        key = f"{d:%Y-%m}"
        while months and months[0] <= key:
            last = index[months.pop(0)]
        if last is not None:
            points.append((d, last))
    if not points:
        return []
    base = points[0][1]
    return [(d.isoformat(), (v / base - 1) * 100) for d, v in points]


def cash_curve(dates: list[date], rates: list[tuple[date, float]]) -> list[tuple[str, float]]:
    """Growth (%) of a deposit at the ECB rate (floored at 0) since dates[0], at each date."""
    if not dates or not rates:
        return []
    growth, rate, i, out = 1.0, 0.0, 0, []
    day = dates[0]
    for d in dates:
        while day < d:
            while i < len(rates) and rates[i][0] <= day:
                rate = rates[i][1]
                i += 1
            growth *= 1 + max(rate, 0.0) / 100 / 365
            day += timedelta(days=1)
        out.append((d.isoformat(), (growth - 1) * 100))
    return out


def reference_lines(series: dict[str, list[tuple]]) -> dict[str, list[tuple[str, float]]]:
    """Inflation and deposit lines over the dates of the chart's series; {} if too short or offline."""
    dates = sorted({date.fromisoformat(str(d)[:10]) for points in series.values() for d, _ in points})
    if len(dates) < 2 or (dates[-1] - dates[0]).days < MIN_DAYS:
        return {}
    refs = {}
    index = hicp_index(dates[0] - timedelta(days=31))
    if index:
        refs[INFLATION] = inflation_curve(dates, index)
    rates = deposit_rates(dates[0])
    if rates:
        refs[CASH] = cash_curve(dates, rates)
    return {name: points for name, points in refs.items() if points}
