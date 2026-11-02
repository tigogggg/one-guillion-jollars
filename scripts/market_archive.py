"""Archive of what cannot be downloaded later, for future training (private repo: agents/arquivo/).

Free sources give today's state but not the past: Claude's reading of the news, the earnings dates as they were
known each day, the agents' every decision, prices of companies that later leave the market, European fundamentals
as they stood each week. Each run adds what it saw:

- noticias/YYYY-MM.jsonl        every rated headline (news.py), and geopolitica/YYYY-MM.jsonl the risk readings
- decisoes/YYYY-MM.jsonl        every trade of every agent (with its context and the round's time)
- fechos/YYYY-MM-DD.csv.gz      the decision day's bar of the whole universe (stocks, ETFs, currencies)
- resultados/YYYY-MM-DD.json    the next earnings date known that day for the stocks the run looked up
- fundamentais-eu/YYYY-MM-DD.json.gz  Yahoo's fundamentals of the European stocks (weekly)

And in a second private repo (one-guillion-jollars-arquivo, checked out at arquivo-intradiario/), each reactive
round's prices of every market in session: YYYY/MM-DD/HHMM.csv.gz, our own intraday history of Europe and the US.

    uv run python scripts/market_archive.py fundamentais     # the weekly European fundamentals
"""

from __future__ import annotations

import gzip
import io
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
ARCHIVE = REPO / "agents" / "arquivo"
INTRADAY = REPO / "arquivo-intradiario"  # checkout of the private repo one-guillion-jollars-arquivo (when configured)
# Yahoo's .info fields worth keeping (valuation, quality, growth, debt, size)
FUNDAMENTAL_FIELDS = (
    "marketCap", "enterpriseValue", "trailingPE", "forwardPE", "priceToBook", "priceToSalesTrailing12Months",
    "enterpriseToEbitda", "profitMargins", "operatingMargins", "grossMargins", "returnOnEquity", "returnOnAssets",
    "revenueGrowth", "earningsGrowth", "debtToEquity", "currentRatio", "freeCashflow", "totalRevenue", "ebitda",
    "dividendYield", "payoutRatio", "beta", "sharesOutstanding", "currency", "financialCurrency", "sector", "industry",
)


def _append(rel: str, rows: list[dict], key) -> int:
    """Add rows to a JSON-lines file, skipping those already there (by key). Returns how many were added."""
    if not rows:
        return 0
    path = ARCHIVE / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(key(json.loads(line)))
            except (ValueError, KeyError, TypeError):
                continue
    new = [r for r in rows if key(r) not in seen]
    if new:
        with path.open("a", encoding="utf-8") as f:
            for r in new:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    return len(new)


def news(rated: list[dict], now: datetime, geo: dict | None = None) -> int:
    """Claude's (or FinBERT's) rating of each new headline, and the geopolitical risk reading."""
    month = now.strftime("%Y-%m")
    rows = [{**item, "arquivado": now.isoformat(timespec="minutes")} for item in rated]
    added = _append(f"noticias/{month}.jsonl", rows, lambda r: (r.get("id"), r.get("symbol")))
    if geo:
        _append(f"geopolitica/{month}.jsonl", [{**geo, "arquivado": now.isoformat(timespec="minutes")}],
                lambda r: r.get("at") or r.get("arquivado"))
    return added


def decisions(config: dict, state: dict) -> int:
    """Every trade of every agent, once, with the agent's strategy and mode (trades kept in the state are capped)."""
    by_id = {c["id"]: c for c in config["agents"]}
    rows: dict[str, list[dict]] = {}
    for agent_id, port in state["agents"].items():
        cfg = by_id.get(agent_id, {})
        for t in port.get("trades", []):
            row = {"agent": agent_id, "strategy": cfg.get("strategy"), "mode": cfg.get("modo", "reativo"), **t}
            rows.setdefault(t["date"][:7], []).append(row)
        for c in port.get("cancelled", []):
            row = {"agent": agent_id, "strategy": cfg.get("strategy"), "mode": cfg.get("modo", "reativo"),
                   "side": "cancelada", **c}
            rows.setdefault(c["date"][:7], []).append(row)

    def key(r):
        return (r.get("agent"), r.get("date"), r.get("hora"), r.get("side"), r.get("symbol"), r.get("qty"), r.get("price"))

    return sum(_append(f"decisoes/{month}.jsonl", rs, key) for month, rs in rows.items())


def closes(mkt) -> int:
    """The decision day's bar of every symbol the run loaded (complete sessions only)."""
    rows = []
    day = pd.Timestamp(mkt.as_of)
    for sym, df in mkt.hist.items():
        if df is None or not len(df) or df.index[-1].normalize() != day:
            continue
        bar = df.iloc[-1]
        rows.append({"symbol": sym, "date": mkt.as_of.isoformat(),
                     **{c.lower(): float(bar[c]) for c in ("Open", "High", "Low", "Close", "Volume") if c in bar}})
    fx = {"EURUSD": mkt.fx, **{f"EUR{c}": s for c, s in getattr(mkt, "fx_rates", {}).items()}}
    for name, series in fx.items():
        if series is not None and len(series) and series.index[-1].normalize() == day:
            rows.append({"symbol": name, "date": mkt.as_of.isoformat(), "close": float(series.iloc[-1])})
    if not rows:
        return 0
    path = ARCHIVE / "fechos" / f"{mkt.as_of.isoformat()}.csv.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    pd.DataFrame(rows).to_csv(buf, index=False)
    path.write_bytes(gzip.compress(buf.getvalue().encode(), mtime=0))
    return len(rows)


def earnings(mkt) -> int:
    """The next earnings dates as known on the decision day (only what the run looked up)."""
    known = {s: (d.isoformat() if isinstance(d, date) else None) for s, d in (mkt.earnings or {}).items()}
    if not known:
        return 0
    path = ARCHIVE / "resultados" / f"{mkt.as_of.isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    old = json.loads(path.read_text()) if path.exists() else {}
    path.write_text(json.dumps({**old, **known}, ensure_ascii=False, sort_keys=True))
    return len(known)


def intraday(mkt, now: datetime) -> int:
    """This round's bar so far of every symbol in session (one small file per round, never rewritten)."""
    if not INTRADAY.is_dir():
        return 0
    import agents
    from zoneinfo import ZoneInfo

    rows = []
    for sym, df in mkt.hist.items():
        if df is None or not len(df) or not agents.market_open(sym, now):
            continue
        bar = df.iloc[-1]
        if bar.name.date() != mkt.as_of:
            continue
        frac = agents.session_fraction(sym, now)
        rows.append({"symbol": sym, "open": float(bar["Open"]), "high": float(bar["High"]), "low": float(bar["Low"]),
                     "price": float(bar["Close"]),
                     # Yahoo's volume so far (the rounds project it to a full session; undone here)
                     "volume": round(float(bar["Volume"]) * max(frac, agents.LIVE_MIN_FRACTION)), "session": round(frac, 4)})
    if not rows:
        return 0
    local = now.astimezone(ZoneInfo("Europe/Lisbon"))
    path = INTRADAY / local.strftime("%Y") / local.strftime("%m-%d") / f"{local:%H%M}.csv.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    pd.DataFrame(rows).to_csv(buf, index=False)
    path.write_bytes(gzip.compress(buf.getvalue().encode(), mtime=0))
    return len(rows)


def european_fundamentals(symbols: list[str], today: date | None = None) -> int:
    """Yahoo's current fundamentals of the European stocks (point-in-time history builds up week by week)."""
    import yfinance as yf

    today = today or datetime.now(timezone.utc).date()
    out = {}
    for sym in symbols:
        try:
            info = yf.Ticker(sym).info or {}
        except Exception as e:  # noqa: BLE001 - one failed ticker never stops the archive
            print(f"WARN: {sym}: {e!r}", file=sys.stderr)
            continue
        kept = {k: info[k] for k in FUNDAMENTAL_FIELDS if info.get(k) is not None}
        if kept:
            out[sym] = kept
    if not out:
        return 0
    path = ARCHIVE / "fundamentais-eu" / f"{today.isoformat()}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps(out, ensure_ascii=False, sort_keys=True).encode(), mtime=0))
    return len(out)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "fundamentais":
        import agents

        n = european_fundamentals([c["symbol"] for c in agents.load_europe()])
        print(f"Arquivo: fundamentais de {n} ações europeias")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
