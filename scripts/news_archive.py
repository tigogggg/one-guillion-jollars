"""The portal's news tab: 30 days of rated headlines and how prices reacted to them.

news.py rates headlines 3x per weekday; this module keeps them for ARCHIVE_DAYS
(state/news/archive.json, in the news workflow's cache) and builds the portal section:

- now: the last 36 hours, most important first (with filters on the page);
- sectors: the same grouped by sector, with the average sentiment;
- changes: what changed in the last 24 hours (new important news, symbols whose
  news score moved most against the 24 hours before);
- timeline: per stock, every rated headline of the last 30 days, each with the
  price reaction 1 and 5 sessions later, net of the S&P 500 (SPY);
- reaction: per stock and overall, how often the price moved in the direction
  Claude's sentiment said (hit rate) and the average move after important news,
  so you can see which stocks react to news and how good the reading is.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
ARCHIVE_PATH = REPO / "state" / "news" / "archive.json"
ARCHIVE_DAYS = 30
NOW_HOURS = 36
KEEP = ("id", "symbol", "title", "resumo", "publisher", "url", "published", "sentimento", "importancia", "surpresa", "fonte")


def load() -> list[dict]:
    return json.loads(ARCHIVE_PATH.read_text()) if ARCHIVE_PATH.exists() else []


def add(archive: list[dict], rated: list[dict], now: datetime) -> list[dict]:
    """Add newly rated items (by id) and drop those older than ARCHIVE_DAYS."""
    by_id = {i["id"]: i for i in archive}
    for i in rated:
        by_id.setdefault(i["id"], {k: i.get(k) for k in KEEP})
    cutoff = now - timedelta(days=ARCHIVE_DAYS)
    out = [i for i in by_id.values() if datetime.fromisoformat(i["published"]) >= cutoff]
    return sorted(out, key=lambda i: i["published"])


def reactions(archive: list[dict], hist: dict[str, pd.DataFrame]) -> None:
    """Fill r1/r5: the stock's move 1 and 5 sessions after the news, minus the S&P 500's (in %)."""
    spy = hist.get("SPY")
    for i in archive:
        if i.get("r5") is not None:
            continue
        df = hist.get(i["symbol"])
        if df is None or spy is None:
            continue
        day = pd.Timestamp(datetime.fromisoformat(i["published"]).astimezone(timezone.utc).date())
        before = df.loc[:day]
        after = df.loc[day + pd.Timedelta(days=1):]
        sb, sa = spy.loc[:day], spy.loc[day + pd.Timedelta(days=1):]
        if before.empty or sb.empty:
            continue
        base, sbase = float(before["Close"].iloc[-1]), float(sb["Close"].iloc[-1])
        for n, key in ((1, "r1"), (5, "r5")):
            if len(after) >= n and len(sa) >= n:
                move = float(after["Close"].iloc[n - 1]) / base - 1
                market = float(sa["Close"].iloc[n - 1]) / sbase - 1
                i[key] = round((move - market) * 100, 2)


def hit(items: list[dict], key: str = "r1") -> float | None:
    """Share of non-neutral news whose next-session excess move had the sign of the sentiment."""
    rows = [i for i in items if i.get(key) is not None and i.get("sentimento")]
    if not rows:
        return None
    return round(sum((i[key] > 0) == (i["sentimento"] > 0) for i in rows) / len(rows), 3)


def build(archive: list[dict], names: dict[str, str], sectors: dict[str, str], scores: dict[str, float], now: datetime) -> dict:
    def row(i: dict) -> dict:
        return {**{k: i.get(k) for k in KEEP if k != "id"}, "company": names.get(i["symbol"], ""),
                "sector": sectors.get(i["symbol"], ""), "r1": i.get("r1"), "r5": i.get("r5")}

    age = lambda i: now - datetime.fromisoformat(i["published"])  # noqa: E731
    recent = sorted((i for i in archive if age(i) <= timedelta(hours=NOW_HOURS)),
                    key=lambda i: (i.get("importancia", 0), i["published"]), reverse=True)

    by_sector: dict[str, list] = {}
    for i in recent:
        by_sector.setdefault(sectors.get(i["symbol"]) or "Outros", []).append(i)
    sector_rows = sorted(({"sector": s, "count": len(xs), "important": sum(x.get("importancia", 0) >= 2 for x in xs),
                           "sentiment": round(statistics.fmean(x.get("sentimento", 0) for x in xs), 2),
                           "symbols": sorted({x["symbol"] for x in xs})[:12]} for s, xs in by_sector.items()),
                         key=lambda r: r["important"], reverse=True)

    def score(items: list[dict]) -> dict[str, int]:
        out: dict[str, int] = {}
        for i in items:
            out[i["symbol"]] = out.get(i["symbol"], 0) + (i.get("sentimento") or 0) * (i.get("importancia") or 0)
        return out

    last = [i for i in archive if age(i) <= timedelta(hours=24)]
    before = [i for i in archive if timedelta(hours=24) < age(i) <= timedelta(hours=48)]
    s_now, s_before = score(last), score(before)
    moves = sorted(({"symbol": s, "company": names.get(s, ""), "now": s_now.get(s, 0), "before": s_before.get(s, 0),
                     "delta": s_now.get(s, 0) - s_before.get(s, 0)} for s in set(s_now) | set(s_before)),
                   key=lambda r: abs(r["delta"]), reverse=True)

    by_symbol: dict[str, list] = {}
    for i in archive:
        by_symbol.setdefault(i["symbol"], []).append(i)
    timeline, reaction = {}, []
    for s, xs in by_symbol.items():
        xs = sorted(xs, key=lambda i: i["published"], reverse=True)
        timeline[s] = [row(i) for i in xs[:20]]
        important = [i for i in xs if (i.get("importancia") or 0) >= 2 and i.get("r1") is not None]
        if important:
            reaction.append({"symbol": s, "company": names.get(s, ""), "news": len(important),
                             "avg_abs_r1": round(statistics.fmean(abs(i["r1"]) for i in important), 2),
                             "avg_abs_r5": round(statistics.fmean(abs(i["r5"]) for i in important if i.get("r5") is not None), 2)
                             if any(i.get("r5") is not None for i in important) else None,
                             "hit_r1": hit(important), "hit_r5": hit(important, "r5")})
    reaction.sort(key=lambda r: (r["news"], r["avg_abs_r1"]), reverse=True)
    rated = [i for i in archive if (i.get("importancia") or 0) >= 2]
    return {
        "updated": now.isoformat(timespec="minutes"),
        "items": [row(i) for i in recent[:150]],
        "scores": scores,
        "sectors": sector_rows,
        "changes": {"new_important": [row(i) for i in sorted(last, key=lambda i: i["published"], reverse=True)
                                      if (i.get("importancia") or 0) >= 2][:15],
                    "moves": moves[:15]},
        "timeline": dict(sorted(timeline.items(), key=lambda kv: len(kv[1]), reverse=True)[:120]),
        "reaction": reaction[:60],
        "accuracy": {"news": len(rated), "hit_r1": hit(rated), "hit_r5": hit(rated, "r5"),
                     "by_source": {src: hit([i for i in rated if i.get("fonte") == src]) for src in ("Claude", "FinBERT")}},
        "archive_days": ARCHIVE_DAYS,
    }
