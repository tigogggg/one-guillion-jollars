"""The Rust engine must trade exactly like the Python reference (same trades, to the cent), strategy by strategy."""

import math
import random
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import agents  # noqa: E402
import engine  # noqa: E402
import pretrain  # noqa: E402

pytestmark = pytest.mark.skipif(engine.ogj_engine is None, reason="Rust engine not built (uv sync builds engine/)")


def prices(n: int, seed: int, start: str = "2015-01-01") -> pd.DataFrame:
    rng = random.Random(seed)
    p, rows = rng.uniform(20, 200), []
    drift = rng.uniform(-0.0006, 0.0015)
    for i in range(n):
        o = p * (1 + rng.uniform(-0.012, 0.012) + (0.05 if i % 97 == 0 else 0))
        p = max(1.0, o * (1 + rng.gauss(drift, 0.021)))
        rows.append((o, max(o, p) * (1 + rng.uniform(0, 0.015)), min(o, p) * (1 - rng.uniform(0, 0.015)), p,
                     rng.randint(300_000, 6_000_000) * (3 if i % 61 == 0 else 1)))
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=pd.bdate_range(start, periods=n))


@pytest.fixture(scope="module")
def markets():
    n = 760
    names = [f"US{i:02d}" for i in range(40)] + [f"EU{i:02d}.DE" for i in range(8)] + [f"UK{i:02d}.L" for i in range(6)]
    hist = {s: prices(n, i) for i, s in enumerate(names)}
    for s in names[-6:]:  # pence quotes
        hist[s] = hist[s] * 100
        hist[s]["Volume"] = hist[s]["Volume"] / 100
    hist["SXR8.DE"] = prices(n, 999)
    sectors = ["Technology", "Health Care", "Energy", "Financials", "Industrials", "Unknown"]
    days = [d.date() for d in hist[names[0]].index][pretrain.WARMUP:]
    stocks = pretrain.snapshots({s: hist[s] for s in names}, days, {s: {"sector": sectors[i % 6]} for i, s in enumerate(names)})
    etfs = pretrain.snapshots({"SXR8.DE": hist["SXR8.DE"]}, days, {"SXR8.DE": {"name": "S&P 500"}})
    idx = hist[names[0]].index
    fx = pd.Series(1.1 + 0.05 * np.sin(np.arange(len(idx)) / 40), index=idx)
    gbp = pd.Series(0.86 + 0.02 * np.cos(np.arange(len(idx)) / 30), index=idx)
    closes = pd.DataFrame({s: df["Close"] for s, df in hist.items()}).ffill()
    cv, cols = closes.to_numpy(dtype=float), {c: k for k, c in enumerate(closes.columns)}
    rng = random.Random(7)
    earnings = {s: sorted(d for d in days if rng.random() < 0.016) for s in names[:30]}
    out = []
    for k, d in enumerate(days):
        share = agents.us_breadth(stocks[d])
        ctx = {"vix": 15 + 20 * abs(math.sin(k / 50)), "credit_dd": 0.02 * abs(math.cos(k / 70)), "spy_trend": math.sin(k / 90),
               "usd_chg": 0.03 * math.sin(k / 33), "sec_XLK_trend": math.sin(k / 45), "sec_XLK_rs": math.cos(k / 45)}
        out.append(agents.Market(hist=hist, fx=fx, fx_rates={"GBP": gbp}, stocks=stocks[d], etfs=etfs[d], as_of=d,
                                 sessions=days[: k + 1], zone=agents.zone_for(share), stage2_share=share,
                                 closes=pretrain.PriceRow(cv[pretrain.WARMUP + k], cols), context=ctx,
                                 earnings_dates=earnings))
    return out


def python_run(strategy, params, mkts, monkeypatch):
    monkeypatch.setattr(agents, "TRADES_KEPT", 10**9)
    r = pretrain.simulate(strategy, params, mkts, keep_port=True)
    side = {"compra": 0, "venda": 1, "imposto": 2}
    trades = [(t["date"], side[t["side"]], None if t["symbol"] == "-" else t["symbol"], t["qty"], t["price"], t["eur"],
               t["pnl_eur"], t["reason"] if t["side"] != "imposto" else "") for t in r["port"]["trades"]]
    return r, trades


def rust_run(strategy, params, mkts):
    r = engine.run(strategy, params, mkts)
    trades = [(engine.from_day(t[0]).isoformat(), t[1], t[2], t[3], t[4], t[5], t[6], t[7] if t[1] != 2 else "")
              for t in r["trades"]]
    return r, trades


PARAMS = [
    {"n": 5, "cada": 5, "stop_pct": 0.1},
    {"n": 3, "cada": 10, "stop_pct": 0.08, "trail_atr": 2.5, "stop_atr": 2.0, "tempo_max": 25, "gap_max": 0.04},
    {"n": 8, "cada": 4, "stop_pct": 0.15, "vix_max": 30, "vix_meio": 22, "max_ext": 0.12, "min_tecnico": 35,
     "credito_max": 0.015, "usd_max": 0.02, "filtro_setor": 2, "regiao": 1, "dias_resultados": 3},
    {"n": 4, "cada": 6, "stop_pct": 0.12, "regiao": 2, "filtro_spy": 1, "gap_max": 0.02,
     "por_zona": {"Queda": {"n": 2, "cada": 3}, "Cautelosa": {"stop_pct": 0.06}}},
]


@pytest.mark.parametrize("params", PARAMS)
def test_foguetao_trades_exactly_like_python(markets, params, monkeypatch):
    params = {**{k: agents.COMMON[k] for k in pretrain.SHARED_IN_BACKTEST}, **params}
    py, py_trades = python_run("foguetao", params, markets, monkeypatch)
    rs, rs_trades = rust_run("foguetao", params, markets)
    assert len(py_trades) > 20
    for a, b in zip(py_trades, rs_trades):
        assert a == b
    assert len(py_trades) == len(rs_trades)
    port = py["port"]
    assert [(h["date"], h["equity"]) for h in port["history"]] == [(engine.from_day(d).isoformat(), e) for d, e in rs["history"]]
    assert port["points"] == rs["points"] and port.get("max_dd", 0.0) == rs["max_dd"]
    assert py["trades"] == rs["n_trades"]
    graded = pretrain.simulate("foguetao", params, markets)  # through the engine
    for k in ("points", "return", "return_after_tax", "max_drawdown", "trades", "trade_stats", "by_zone", "curve"):
        assert graded[k] == py[k], k


def test_a_slice_of_the_days_reuses_the_data_and_matches(markets, monkeypatch):
    params = {**{k: agents.COMMON[k] for k in pretrain.SHARED_IN_BACKTEST}, **PARAMS[1]}
    part = markets[100:350]
    _, py_trades = python_run("foguetao", params, part, monkeypatch)
    _, rs_trades = rust_run("foguetao", params, part)
    assert py_trades == rs_trades
