"""The Rust engine (engine/, Python module ogj_engine): the data it needs, built once from a backtest's markets, and
the simulations it runs instead of the Python day loop of pretrain.simulate.

The Python version (agents.py) stays the reference until every strategy is ported: tests/test_engine.py requires the
same trades, to the cent. pretrain.simulate uses the Rust engine for the strategies in ogj_engine.SUPPORTED when the
module is installed, unless OGJ_ENGINE=python.
"""

from __future__ import annotations

import json
import math
import os
from datetime import date

import numpy as np
import pandas as pd

import agents

try:
    import ogj_engine  # the compiled engine (uv sync builds it from engine/)
except ImportError:  # not built: everything stays in Python
    ogj_engine = None

EPOCH = date(1970, 1, 1).toordinal()
CURRENCIES = ["USD", "GBP", "CHF", "SEK", "DKK", "NOK"]
SEPARATE = ("sector", "name", "stage2", "trend_score")  # kept apart from the float32 metrics


def enabled(strategy: str) -> bool:
    return ogj_engine is not None and os.environ.get("OGJ_ENGINE", "rust") != "python" and strategy in ogj_engine.SUPPORTED


def day_num(d: date) -> int:
    return d.toordinal() - EPOCH


def _rows(markets: list, attr: str, sym_ix: dict, fields: list, field_ix: dict, sector_ix: dict, nullable: set):
    """The day's stocks (or ETFs) in Python's order: offsets, symbols, sectors, the float32 metrics, trend and stage2."""
    off, syms, sectors, trend, stage2, present = [0], [], [], [], [], np.zeros(len(sym_ix), dtype=np.uint64)
    chunks = []
    mapping: dict[int, tuple] = {}
    for mkt in markets:
        table = getattr(mkt, attr)
        n = 0
        for s, m in table.items():
            row = np.full(len(fields), np.nan, dtype=np.float32)
            cols = getattr(m, "_cols", None)
            if cols is not None:  # pretrain.Row: copy its float32 values by column
                key = id(cols)
                if key not in mapping:
                    src = [i for c, i in cols.items() if c in field_ix]
                    dst = [field_ix[c] for c, i in cols.items() if c in field_ix]
                    bits = 0
                    for j in dst:
                        bits |= 1 << j
                    mapping[key] = (np.array(src, dtype=np.int64), np.array(dst, dtype=np.int64), bits, cols)
                src, dst, bits, _ = mapping[key]
                row[dst] = m._v[src]
                for kind_field, kind in m._kinds.items():
                    if kind == "nullable" and kind_field in field_ix:
                        nullable.add(kind_field)
                for c, v in m._extra.items():  # values set on the row later (Row.update) win over the table's
                    if c in field_ix and c not in SEPARATE:
                        if v is None:
                            nullable.add(c)
                            row[field_ix[c]] = np.nan
                        elif isinstance(v, (int, float)):
                            row[field_ix[c]] = float(v)
                        bits |= 1 << field_ix[c]
            else:  # a plain dict (live markets, tests)
                bits = 0
                for c, v in m.items():
                    if c in field_ix and isinstance(v, (int, float, bool)) and not isinstance(v, str):
                        row[field_ix[c]] = float(v)
                        bits |= 1 << field_ix[c]
                    elif c in field_ix and v is None:
                        nullable.add(c)
                        bits |= 1 << field_ix[c]
            i = sym_ix[s]
            present[i] |= np.uint64(bits)
            chunks.append(row)
            syms.append(i)
            sec = m.get("sector")
            sectors.append(sector_ix.setdefault(sec, len(sector_ix)) if sec else -1)
            trend.append(float(m.get("trend_score") or 0.0))
            stage2.append(1 if m.get("stage2") else 0)
            n += 1
        off.append(off[-1] + n)
    val = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    return {"off": np.array(off, dtype=np.int64), "sym": np.array(syms, dtype=np.uint32),
            "sector": np.array(sectors, dtype=np.int32), "val": val.astype(np.float32).ravel(),
            "trend": np.array(trend, dtype=np.float64), "stage2": np.array(stage2, dtype=np.uint8), "present": present}


def build(markets: list) -> "ogj_engine.EngineData":
    """The engine's data for these consecutive markets (the days of a backtest)."""
    hist = markets[-1].hist
    symbols: list[str] = []
    sym_ix: dict[str, int] = {}

    def add(s: str) -> None:
        if s not in sym_ix:
            sym_ix[s] = len(symbols)
            symbols.append(s)

    closes0 = markets[0].closes
    for s in (getattr(closes0, "_cols", None) or {}):
        add(s)
    for mkt in markets:
        for s in list(mkt.stocks) + list(mkt.etfs):
            add(s)
    fields: list[str] = []
    field_ix: dict[str, int] = {}
    for mkt in markets[:1] + markets[len(markets) // 2: len(markets) // 2 + 1] + markets[-1:]:
        for table in (mkt.stocks, mkt.etfs):
            for m in list(table.values())[:50]:
                for c in list(getattr(m, "_cols", None) or m) + list(getattr(m, "_extra", {})):
                    if c not in field_ix and c not in SEPARATE:
                        field_ix[c] = len(fields)
                        fields.append(c)
    if len(fields) > 64:
        raise ValueError(f"demasiados campos para a máscara: {len(fields)}")
    sector_ix: dict[str, int] = {}
    nullable: set[str] = set()
    stocks = _rows(markets, "stocks", sym_ix, fields, field_ix, sector_ix, nullable)
    etfs = _rows(markets, "etfs", sym_ix, fields, field_ix, sector_ix, nullable)
    n_days, n_syms = len(markets), len(symbols)
    closes = np.full((n_days, n_syms), np.nan)
    for k, mkt in enumerate(markets):
        c = mkt.closes
        if c is not None and hasattr(c, "_cols"):
            cols = c._cols
            idx = np.array([sym_ix[s] for s in cols], dtype=np.int64)
            closes[k, idx] = c._v
        else:
            for s in symbols:
                v = agents.last_close(mkt, s)
                if v is not None:
                    closes[k, sym_ix[s]] = v
    bar_off, bar = [0], {k: [] for k in ("day", "open", "high", "low", "close")}
    for s in symbols:
        df = hist.get(s)
        if df is not None and len(df):
            bar["day"].append(df.index.values.astype("datetime64[D]").astype(np.int64).astype(np.int32))
            for k, c in (("open", "Open"), ("high", "High"), ("low", "Low"), ("close", "Close")):
                bar[k].append(df[c].to_numpy(dtype=float))
            bar_off.append(bar_off[-1] + len(df))
        else:
            bar_off.append(bar_off[-1])

    def cat(parts, dtype):
        return np.concatenate(parts).astype(dtype) if parts else np.zeros(0, dtype=dtype)

    fx_off, fx_ns, fx_rate, fx_first = [0], [], [], []
    for c in CURRENCIES:
        series = markets[-1].fx if c == "USD" else (markets[-1].fx_rates or {}).get(c)
        if series is None or not len(series):
            fx_first.append(float("nan"))
            fx_off.append(fx_off[-1])
            continue
        clean = series.dropna()
        fx_ns.append(clean.index.values.astype("datetime64[ns]").astype(np.int64))
        fx_rate.append(clean.to_numpy(dtype=float))
        fx_first.append(float(series.iloc[0]))
        fx_off.append(fx_off[-1] + len(clean))
    ccy, pence, usd = [], [], []
    for s in symbols:
        cur = agents.currency_of(s)
        pence.append(1 if cur == "GBp" else 0)
        usd.append(1 if cur == "USD" else 0)
        cur = "GBP" if cur == "GBp" else cur
        ccy.append(CURRENCIES.index(cur) if cur in CURRENCIES else -1)
    bench_df = hist.get(agents.BENCHMARK)
    bench = np.full(n_days, np.nan)
    if bench_df is not None and len(bench_df):
        close = bench_df["Close"]
        for k, mkt in enumerate(markets):
            v = close.asof(pd.Timestamp(mkt.as_of) + pd.Timedelta(hours=23))
            bench[k] = float(v) if pd.notna(v) else np.nan
    ctx_fields = sorted({k for mkt in markets for k, v in (mkt.context or {}).items() if isinstance(v, (int, float))})
    ctx_val = np.full((n_days, len(ctx_fields)), np.nan)
    for k, mkt in enumerate(markets):
        for j, f in enumerate(ctx_fields):
            v = (mkt.context or {}).get(f)
            if isinstance(v, (int, float)) and v is not None:
                ctx_val[k, j] = float(v)
    import context

    sectors = sorted(sector_ix, key=sector_ix.get)
    trend_ix, rs_ix = [], []
    for sec in sectors:
        key = context.sector_key(sec)
        trend_ix.append(ctx_fields.index(f"{key}_trend") if key and f"{key}_trend" in ctx_fields else -1)
        rs_ix.append(ctx_fields.index(f"{key}_rs") if key and f"{key}_rs" in ctx_fields else -1)
    zones = {z: i for i, z in enumerate(agents.ZONES)}
    data = {
        "fields": fields, "nullable": np.array([1 if f in nullable else 0 for f in fields], dtype=np.uint8),
        "sym_names": symbols, "sym_ccy": np.array(ccy, dtype=np.int32), "sym_pence": np.array(pence, dtype=np.uint8),
        "sym_usd": np.array(usd, dtype=np.uint8),
        "sym_fee": np.array([agents.fees(s) for s in symbols], dtype=float),
        "sym_buy_tax": np.array([agents.buy_tax(s) for s in symbols], dtype=float),
        "sym_is_us": np.array([1 if agents.is_us(s) else 0 for s in symbols], dtype=np.uint8),
        "sym_is_etf": np.array([1 if s in agents.ETF_SYMBOLS else 0 for s in symbols], dtype=np.uint8),
        "sym_defensive": np.array([1 if s in agents.DEFENSIVE_ETFS else 0 for s in symbols], dtype=np.uint8),
        "sectors": sectors, "sectors_pt": [agents.SECTORS_PT.get(x, x) for x in sectors], "sector_unknown": sector_ix.get("Unknown", -1),
        "sector_trend": np.array(trend_ix, dtype=np.int32), "sector_rs": np.array(rs_ix, dtype=np.int32),
        "days": np.array([day_num(m.as_of) for m in markets], dtype=np.int32),
        "zone": np.array([zones[m.zone] for m in markets], dtype=np.uint8),
        "sessions": np.array([day_num(d) for d in markets[-1].sessions], dtype=np.int32),
        "closes": closes.ravel(), "bench": bench,
        "bar_off": np.array(bar_off, dtype=np.int64), "bar_day": cat(bar["day"], np.int32),
        "bar_open": cat(bar["open"], float), "bar_high": cat(bar["high"], float), "bar_low": cat(bar["low"], float),
        "bar_close": cat(bar["close"], float),
        "fx_off": np.array(fx_off, dtype=np.int64), "fx_ns": cat(fx_ns, np.int64), "fx_rate": cat(fx_rate, float),
        "fx_first": np.array(fx_first, dtype=float),
        "ctx_fields": ctx_fields, "ctx_val": ctx_val.ravel(),
    }
    for prefix, rows in (("stock", stocks), ("etf", etfs)):
        for k, v in rows.items():
            data[f"{prefix}_{k}"] = v
    if markets[-1].earnings_dates is not None:
        dates = markets[-1].earnings_dates
        off, days = [0], []
        for s in symbols:
            ds = sorted(day_num(d) for d in (dates.get(s) or []))
            days += ds
            off.append(off[-1] + len(ds))
        data["earn_off"] = np.array(off, dtype=np.int64)
        data["earn_day"] = np.array(days, dtype=np.int32)
    return ogj_engine.EngineData(data)


# The data of the backtest being trained (pretrain._MARKETS): every simulation on a slice of it reuses it.
_DATA: dict = {}


def _slice_of(full: list, ids: dict, markets: list) -> int | None:
    i = ids.get(id(markets[0]))
    if i is None or i + len(markets) > len(full) or full[i + len(markets) - 1] is not markets[-1]:
        return None
    return i


def data_for(markets: list):
    """(engine data, first day's index) for a slice of the registered markets, building them on first use."""
    if _DATA:
        i = _slice_of(_DATA["markets"], _DATA["ids"], markets)
        if i is not None:
            return _DATA["data"], i
    import pretrain

    full = pretrain._MARKETS or markets
    ids = {id(m): k for k, m in enumerate(full)}
    if _slice_of(full, ids, markets) is None:
        full, ids = markets, {id(m): k for k, m in enumerate(markets)}
    _DATA.update(markets=full, ids=ids, data=build(full))
    return _DATA["data"], _slice_of(full, ids, markets)


def zone_params(strategy: str, params: dict) -> list[str]:
    return [json.dumps({k: v for k, v in agents.effective(params, z, strategy).items() if not isinstance(v, (dict, list, str))})
            for z in agents.ZONES]


def run(strategy: str, params: dict, markets: list, capital: float = 1000.0) -> dict:
    """The raw result of the Rust day loop for these markets."""
    data, start = data_for(markets)
    return ogj_engine.simulate(data, strategy, zone_params(strategy, params), start, start + len(markets), capital)


def from_day(n: int) -> date:
    return date.fromordinal(n + EPOCH)


def is_finite(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))
