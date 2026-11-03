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


def build(markets: list, symbols: list[str] | None = None, sectors: list[str] | None = None, bars_hist: dict | None = None,
          slots: dict | None = None) -> "ogj_engine.EngineData":
    """The engine's data for these consecutive markets (the days of a backtest, or slots of sessions).

    symbols, sectors: fixed lists, so that agents stepping through several chunks of data (Batch) keep their symbols;
    bars_hist: the complete daily bars (default: the last market's hist); slots: sessions under way, as
    {"partial": [bool per market], "part_open"/"part_low": arrays markets x symbols (today's bar so far, NaN: none),
    "now": [datetime per market]} (see intraday_lab)."""
    hist = bars_hist if bars_hist is not None else markets[-1].hist
    fixed = symbols is not None
    symbols = list(symbols or [])
    sym_ix: dict[str, int] = {s: i for i, s in enumerate(symbols)}

    def add(s: str) -> None:
        if s not in sym_ix:
            if fixed:
                raise ValueError(f"símbolo fora da lista fixa: {s}")
            sym_ix[s] = len(symbols)
            symbols.append(s)

    closes0 = markets[0].closes
    for s in (getattr(closes0, "_cols", None) or {}):
        if not fixed:
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
    sector_ix: dict[str, int] = {x: i for i, x in enumerate(sectors or [])}
    nullable: set[str] = set()
    stocks = _rows(markets, "stocks", sym_ix, fields, field_ix, sector_ix, nullable)
    etfs = _rows(markets, "etfs", sym_ix, fields, field_ix, sector_ix, nullable)
    n_days, n_syms = len(markets), len(symbols)
    closes = np.full((n_days, n_syms), np.nan)
    for k, mkt in enumerate(markets):
        c = mkt.closes
        if c is not None and hasattr(c, "_cols"):
            cols = c._cols
            pairs = [(j, sym_ix[s]) for s, j in cols.items() if s in sym_ix]
            src = np.array([j for j, _ in pairs], dtype=np.int64)
            dst = np.array([i for _, i in pairs], dtype=np.int64)
            closes[k, dst] = np.asarray(c._v)[src]
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
    def context_of(mkt) -> dict:  # numbers only; the live macro calendar as how many events are near
        c = {k: v for k, v in (mkt.context or {}).items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        if (mkt.context or {}).get("macro"):
            c["macro_n"] = len(mkt.context["macro"])
        return c

    contexts = [context_of(m) for m in markets]
    ctx_fields = sorted({k for c in contexts for k in c})
    ctx_val = np.full((n_days, len(ctx_fields)), np.nan)
    for k, c in enumerate(contexts):
        for j, f in enumerate(ctx_fields):
            if f in c:
                ctx_val[k, j] = float(c[f])
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
        "sym_com": np.array([_commission_row(s) for s in symbols], dtype=float).ravel(),
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
    exchanges = sorted({agents.exchange_of(s) for s in symbols})
    data["sym_ex"] = np.array([exchanges.index(agents.exchange_of(s)) for s in symbols], dtype=np.uint16)
    data["n_ex"] = len(exchanges)
    if slots:
        data["partial"] = np.array(slots["partial"], dtype=np.uint8)
        data["part_open"] = np.asarray(slots["part_open"], dtype=float).ravel()
        data["part_low"] = np.asarray(slots["part_low"], dtype=float).ravel()
        ex_open = np.zeros((n_days, len(exchanges)), dtype=np.uint8)
        ex_since, ex_to_close = np.zeros((n_days, len(exchanges))), np.zeros((n_days, len(exchanges)))
        probe = {e: next(s for s in symbols if agents.exchange_of(s) == e) for e in exchanges}
        for k, now in enumerate(slots["now"]):
            if now is None:
                continue
            for j, e in enumerate(exchanges):
                if e == "=X":
                    continue
                ex_open[k, j] = 1 if agents.market_open(probe[e], now) else 0
                ex_since[k, j], ex_to_close[k, j] = agents.session_minutes(probe[e], now)
        data["ex_open"], data["ex_since"], data["ex_to_close"] = ex_open.ravel(), ex_since.ravel(), ex_to_close.ravel()
    last = markets[-1]
    data["news"] = np.array([int((last.news or {}).get(s, {}).get("score", 0) or 0) for s in symbols], dtype=np.int32)
    data["vcp"] = np.array([1 if s in (last.vcp or ()) else 0 for s in symbols], dtype=np.uint8)
    if last.earnings_dates is None and last.earnings:  # live: the next date looked up for each symbol
        off, days = [0], []
        for s in symbols:
            d = last.earnings.get(s)
            if d:
                days.append(day_num(d))
            off.append(len(days))
        data["earn_off"] = np.array(off, dtype=np.int64)
        data["earn_day"] = np.array(days, dtype=np.int32)
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


_OTHER_BROKERS: dict[str, dict] = {}  # data_for's data built with the other brokers' costs (broker_comparison)


def _commission_row(symbol: str) -> list[float]:
    """agents.commission_terms as 6 numbers for the engine (com_pct, com_min, com_max, com_share, its min, max %)."""
    c = agents.commission_terms(symbol)
    if "por_acao" in c:
        return [0.0, 0.0, 0.0, c["por_acao"], c["min"], c["max_pct"]]
    if c:
        return [c["pct"], c["min_eur"], c["max_eur"], 0.0, 0.0, 0.0]
    return [0.0] * 6


def data_for(markets: list):
    """(engine data, first day's index) for a slice of the registered markets, building them on first use."""
    if _DATA and _DATA.get("broker") != agents.BROKER:  # built with another broker's costs: kept, the other built
        _OTHER_BROKERS[_DATA["broker"]] = dict(_DATA)
        _DATA.clear()
        _DATA.update(_OTHER_BROKERS.pop(agents.BROKER, {}))
    if _DATA:
        i = _slice_of(_DATA["markets"], _DATA["ids"], markets)
        if i is not None:
            return _DATA["data"], i
    import pretrain

    full = pretrain._MARKETS or markets
    ids = {id(m): k for k, m in enumerate(full)}
    if _slice_of(full, ids, markets) is None:
        full, ids = markets, {id(m): k for k, m in enumerate(markets)}
    _DATA.update(markets=full, ids=ids, data=build(full), broker=agents.BROKER)
    return _DATA["data"], _slice_of(full, ids, markets)


def zone_params(strategy: str, params: dict) -> list[str]:
    return [json.dumps({k: v for k, v in agents.effective(params, z, strategy).items() if not isinstance(v, (dict, list, str))})
            for z in agents.ZONES]


_ZONE_PARAMS: dict = {}


def run(strategy: str, params: dict, markets: list, capital: float = 1000.0, detail: bool = True,
        signals: bool = False) -> dict:
    """The raw result of the Rust day loop for these markets (detail=False: without the trades and daily history;
    signals=True: also the desk's insights, [(day, symbol, confidence)]: what it wanted to hold on each decision day,
    ranked, before any sizing or risk rule)."""
    data, start = data_for(markets)
    key = (strategy, json.dumps(params, sort_keys=True, default=str))
    zp = _ZONE_PARAMS.get(key)
    if zp is None:
        if len(_ZONE_PARAMS) > 5000:
            _ZONE_PARAMS.clear()
        zp = _ZONE_PARAMS[key] = zone_params(strategy, params)
    return ogj_engine.simulate(data, strategy, zp, start, start + len(markets), capital, detail, signals)


def from_day(n: int) -> date:
    return date.fromordinal(n + EPOCH)


def is_finite(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


# --------------------------------------------------------------------------
# Shadow rounds (phase 4b): the live round again in the engine, compared with what Python did
# --------------------------------------------------------------------------

SHADOW_DIR = agents.REPO_AGENTS / "sombra"


def _day(iso: str | None) -> int | None:
    return day_num(date.fromisoformat(iso[:10])) if iso else None


def port_to_engine(port: dict, ix: dict[str, int], today: date) -> str:
    """A live portfolio as the engine reads it (engine/src/portjson.rs)."""
    t = today.isoformat()
    return json.dumps({
        "cash": port["cash"], "deposits": port["deposits"],
        "positions": [[ix[s], p["qty"], p["entry"], _day(p.get("entry_date")), p["cost_eur"], p.get("stop"), p.get("tp"),
                       _day(p.get("checked"))] for s, p in port["positions"].items()],
        "pending": [[o["side"] == "buy", ix[o["symbol"]], o.get("eur", 0.0), o.get("stop"), o.get("tp"), o.get("fraction", 1.0),
                     o.get("reason", ""), o.get("ref"), o.get("gap_max"), _day(o.get("created"))] for o in port["pending"]],
        "hist": [[_day(h["date"]), h["equity"]] for h in port["history"]],
        "points": port.get("points", 0), "peak": port.get("peak"), "max_dd": port.get("max_dd", 0.0),
        "brake_until": _day(port.get("brake_until")), "brake_peak": port.get("brake_peak"),
        "last_rebalance": _day(port.get("last_rebalance")),
        "realized": [[int(y), g] for y, g in port.get("realized", {}).items()], "taxed": [int(y) for y in port.get("taxed", [])],
        "sold_today": [[ix[x["symbol"]], _day(x["date"])] for x in port["trades"] if x["side"] == "venda" and x["date"] == t
                       and x["symbol"] in ix],
    })


def _python_view(port: dict, ix: dict[str, int]) -> dict:
    """What the engine exports, from a Python portfolio (to compare)."""
    return {
        "cash": port["cash"],
        "positions": sorted([ix[s], p["qty"], p["entry"], _day(p.get("entry_date")), p["cost_eur"], p.get("stop"), p.get("tp"),
                             _day(p.get("checked"))] for s, p in port["positions"].items()),
        "pending": [[o["side"] == "buy", ix[o["symbol"]], o.get("eur", 0.0), o.get("stop"), o.get("tp"), o.get("fraction", 1.0),
                     o.get("reason", ""), o.get("ref"), o.get("gap_max"), _day(o.get("created"))] for o in port["pending"]],
        "last_rebalance": _day(port.get("last_rebalance")),
    }


def shadow_round(config: dict, before: dict, after: dict, mkt, now) -> dict:
    """The round Python just did, again in the engine from the portfolios as they were before it; every difference is
    written to the private repo (agents/sombra/<day>.jsonl). Never changes the agents. Returns counts."""
    import copy

    if ogj_engine is None:
        return {"skipped": "motor Rust não compilado"}
    cfgs = [c for c in agents.active_agents(config) if c["id"] in before["agents"] and c["strategy"] in ogj_engine.SUPPORTED]
    held = {s for c in cfgs for s in list(before["agents"][c["id"]]["positions"]) + [o["symbol"] for o in before["agents"][c["id"]]["pending"]]}
    m = copy.copy(mkt)
    symbols = sorted(set(m.stocks) | set(m.etfs) | held | {s for s, df in (m.hist or {}).items() if df is not None and len(df)})
    ix = {s: i for i, s in enumerate(symbols)}
    part_open = np.full((1, len(symbols)), np.nan)
    part_low = np.full((1, len(symbols)), np.nan)
    for s, i in ix.items():
        df = m.hist.get(s)
        if df is not None and len(df) and df.index[-1].date() == m.as_of:
            part_open[0, i], part_low[0, i] = float(df["Open"].iloc[-1]), float(df["Low"].iloc[-1])
    data = build([m], symbols=symbols, slots={"partial": [True], "part_open": part_open, "part_low": part_low, "now": [now]})
    ports = [before["agents"][c["id"]] for c in cfgs]
    batch = ogj_engine.Batch.from_state([c["strategy"] for c in cfgs], [zone_params(c["strategy"], p.get("params", c["params"])) for c, p in zip(cfgs, ports)],
                                        [port_to_engine(p, ix, m.as_of) for p in ports], [agents.mode_of(c) == agents.REACTIVE for c in cfgs])
    batch.live_round(data, 0)
    diffs = []
    for c, (pj, _) in zip(cfgs, batch.export()):
        rust = json.loads(pj)
        rust["positions"] = sorted(rust["positions"])
        py = json.loads(json.dumps(_python_view(after["agents"][c["id"]], ix)))
        for k in ("cash", "positions", "pending", "last_rebalance"):
            if py[k] != rust[k]:
                diffs.append({"agent": c["id"], "field": k, "python": py[k], "rust": rust[k]})
    SHADOW_DIR.mkdir(parents=True, exist_ok=True)
    with (SHADOW_DIR / f"{m.as_of.isoformat()}.jsonl").open("a") as f:
        f.write(json.dumps({"at": now.isoformat(timespec="minutes"), "agents": len(cfgs), "diffs": diffs}) + "\n")
    return {"agents": len(cfgs), "diffs": len(diffs)}
