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
    etf_names = ["SXR8.DE", "VWCE.DE", "EUNH.DE", "4GLD.DE", "EXS1.DE", "IUSN.DE"]
    for i, e in enumerate(etf_names):
        hist[e] = prices(n, 900 + i)
    sectors = ["Technology", "Health Care", "Energy", "Financials", "Industrials", "Unknown"]
    days = [d.date() for d in hist[names[0]].index][pretrain.WARMUP:]
    frng = np.random.default_rng(3)
    frames = {}
    for i, s in enumerate(names[:36]):  # point-in-time fundamentals for most stocks, with gaps
        idx = hist[s].index
        q = pd.Series(np.clip(50 + np.cumsum(frng.normal(0, 2, len(idx))), 0, 100), index=idx)
        v = pd.Series(np.clip(50 + np.cumsum(frng.normal(0, 2, len(idx))), 0, 100), index=idx)
        if i % 5 == 0:
            q[: len(idx) // 2] = np.nan
        frames[s] = pd.DataFrame({"fund_q": q, "fund_v": v})
    stocks = pretrain.snapshots({s: hist[s] for s in names}, days, {s: {"sector": sectors[i % 6]} for i, s in enumerate(names)},
                                frames=frames)
    etfs = pretrain.snapshots({e: hist[e] for e in etf_names}, days, {e: {"name": e} for e in etf_names})
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


def compare(strategy, params, markets, monkeypatch, min_trades=20):
    py, py_trades = python_run(strategy, params, markets, monkeypatch)
    rs, rs_trades = rust_run(strategy, params, markets)
    assert len(py_trades) >= min_trades
    for a, b in zip(py_trades, rs_trades):
        assert a == b
    assert len(py_trades) == len(rs_trades)
    port = py["port"]
    assert [(h["date"], h["equity"]) for h in port["history"]] == [(engine.from_day(d).isoformat(), e) for d, e in rs["history"]]
    assert port["points"] == rs["points"] and port.get("max_dd", 0.0) == rs["max_dd"]
    assert py["trades"] == rs["n_trades"]
    graded = pretrain.simulate(strategy, params, markets)  # through the engine
    for k in ("points", "return", "return_after_tax", "max_drawdown", "trades", "trade_stats", "by_zone", "curve"):
        assert graded[k] == py[k], k


SHARED = {"stop_atr": 2.0, "trail_atr": 3.0, "tempo_max": 30, "gap_max": 0.05, "vix_max": 32, "min_tecnico": 30,
          "qualidade_fund_min": 30, "filtro_setor": 1, "max_ext": 0.25}


@pytest.mark.parametrize("strategy", [s for s in ("indexador", "guardiao", "rotador", "analista", "quant", "monge", "abutre",
                                                  "pendulo", "cacador", "sniper", "campeao", "pescador", "deriva", "kamikaze")])
@pytest.mark.parametrize("variant", ["defaults", "shared rules", "europe"])
def test_every_strategy_trades_exactly_like_python(markets, strategy, variant, monkeypatch):
    params = dict(agents.STRATEGIES[strategy][2])
    if strategy != "indexador":
        params = {**{k: agents.COMMON[k] for k in pretrain.SHARED_IN_BACKTEST}, **params}
    if variant == "shared rules" and strategy != "indexador":
        params.update(SHARED)
    if variant == "europe":
        params.update({"regiao": 2, "por_zona": {"Queda": {"stop_pct": 0.05}}} if strategy != "indexador" else {})
    if strategy == "sniper":
        params["aperto_pct"] = 0.15  # the synthetic prices are wilder than real ones
    if strategy == "abutre":
        params.update(queda_min=0.10, qualidade_min=40, valor_min=40)
    compare(strategy, params, markets, monkeypatch, min_trades=0 if variant == "europe" else 1)


@pytest.mark.parametrize("params", PARAMS)
def test_foguetao_trades_exactly_like_python(markets, params, monkeypatch):
    params = {**{k: agents.COMMON[k] for k in pretrain.SHARED_IN_BACKTEST}, **params}
    compare("foguetao", params, markets, monkeypatch)


@pytest.mark.parametrize("broker", ["ibkr", "trading212"])
@pytest.mark.parametrize("strategy", ["foguetao", "sniper", "rotador", "pescador"])
def test_each_broker_costs_the_same_in_python_and_in_the_engine(markets, strategy, broker, monkeypatch):
    """The commission per order (IBKR: minimum per order, US per share) and the other conversion fees: same trades."""
    agents.set_broker(broker)
    try:
        params = {**{k: agents.COMMON[k] for k in pretrain.SHARED_IN_BACKTEST},
                  **(PARAMS[1] if strategy == "foguetao" else agents.STRATEGIES[strategy][2])}
        if strategy == "sniper":
            params["aperto_pct"] = 0.15
        compare(strategy, params, markets, monkeypatch, min_trades=1)
    finally:
        agents.set_broker(agents.DEFAULT_BROKER)


def test_the_commission_of_an_order():
    agents.set_broker("ibkr")
    try:
        assert agents.commission(None, "SAP.DE", 100.0, 1.0, None) == 1.25  # minimum
        assert agents.commission(None, "SAP.DE", 10_000.0, 1.0, None) == pytest.approx(6.0)  # 0.06%
        assert agents.commission(None, "SAP.DE", 100_000.0, 1.0, None) == 29.0  # maximum
    finally:
        agents.set_broker(agents.DEFAULT_BROKER)
    assert agents.commission(None, "SAP.DE", 100.0, 1.0, None) == 0.0
    assert agents.set_broker("nenhuma") == agents.DEFAULT_BROKER


def test_a_slice_of_the_days_reuses_the_data_and_matches(markets, monkeypatch):
    params = {**{k: agents.COMMON[k] for k in pretrain.SHARED_IN_BACKTEST}, **PARAMS[1]}
    part = markets[100:350]
    _, py_trades = python_run("foguetao", params, part, monkeypatch)
    _, rs_trades = rust_run("foguetao", params, part)
    assert py_trades == rs_trades


def test_a_live_round_in_the_engine_matches_python(markets, monkeypatch, tmp_path):
    """Phase 4b: a reactive round (pending orders at today's open, stops so far, exits and decisions at the price of
    now) on real-looking portfolios, in Python and again in the engine from the same state: no differences."""
    import copy
    from datetime import datetime, timezone

    import earnings as E

    k = 400
    past, today = markets[:k], markets[k]
    names = ("foguetao", "sniper", "pescador", "quant", "abutre", "kamikaze", "guardiao", "rotador")
    config = {"agents": []}
    state = {"agents": {}}
    for i, s in enumerate(names):
        params = {**{x: agents.COMMON[x] for x in pretrain.SHARED_IN_BACKTEST}, **agents.STRATEGIES[s][2]}
        if s == "sniper":
            params["aperto_pct"] = 0.15
        if i % 3 == 1:
            params.update(reatividade=1, espera_min=30, esticado_max=0.02)
        if i % 3 == 2:
            params["reatividade"] = 0
        port = pretrain.simulate(s, params, past, keep_port=True)["port"]
        port["params"] = params
        config["agents"].append({"id": s, "name": s, "emoji": "🤖", "strategy": s, "capital": 1000, "params": params})
        state["agents"][s] = port
    config["agents"].append({"id": "foguetao-diario", "name": "f", "emoji": "🤖", "strategy": "foguetao", "capital": 1000,
                             "params": state["agents"]["foguetao"]["params"], "modo": "diario"})
    state["agents"]["foguetao-diario"] = copy.deepcopy(state["agents"]["foguetao"])
    # the market at 15:00 New York: each stock's bars up to yesterday, plus today's bar so far
    day = today.as_of
    hist = {}
    for s, df in today.hist.items():
        cut = df[df.index.date <= day].copy()
        if len(cut) and cut.index[-1].date() == day:
            cut.iloc[-1, cut.columns.get_loc("Close")] = cut["Open"].iloc[-1] * 1.004
            cut.iloc[-1, cut.columns.get_loc("High")] = max(cut["Open"].iloc[-1], cut["Close"].iloc[-1])
        hist[s] = cut
    live = copy.copy(today)
    live.hist, live.closes, live.earnings_dates, live.earnings = hist, None, None, {}
    live.news = {"US03": {"score": -5}, "US07": {"score": 4}}
    live.vcp = {"US11", "US12"}
    live.league = {}
    monkeypatch.setattr(E, "next_earnings", lambda s, d: d + timedelta(days=(hash(s) % 9) + 1) if s.startswith("US1") else None)
    monkeypatch.setattr(engine, "SHADOW_DIR", tmp_path)
    now = datetime(day.year, day.month, day.day, 20, 0, tzinfo=timezone.utc)
    before = json_copy(state)
    moves, _ = agents.reactive_round(config, state, live, now)
    r = engine.shadow_round(config, before, state, live, now)
    assert r["agents"] == len(names) + 1
    lines = (tmp_path / f"{day.isoformat()}.jsonl").read_text().splitlines()
    import json
    assert json.loads(lines[-1])["diffs"] == [] and r["diffs"] == 0
    assert moves  # the round did trade


def json_copy(x):
    import json

    return json.loads(json.dumps(x, default=str))


def test_the_desks_insights_are_recorded_without_changing_the_run(markets):
    params = {**{k: agents.COMMON[k] for k in pretrain.SHARED_IN_BACKTEST}, **agents.STRATEGIES["foguetao"][2]}
    plain = engine.run("foguetao", params, markets)
    with_signals = engine.run("foguetao", params, markets, signals=True)
    assert plain["trades"] == with_signals["trades"] and plain["history"] == with_signals["history"]
    assert "signals" not in plain and with_signals["signals"]
    by_day = {}
    for day, sym, conf in with_signals["signals"]:
        assert 0.5 <= conf <= 1.0 and isinstance(sym, str)
        by_day.setdefault(day, []).append(conf)
    for confs in by_day.values():  # a ranked list: the first at full confidence, falling
        assert confs[0] == 1.0 and confs == sorted(confs, reverse=True)
    bought = {(t[0], t[2]) for t in plain["trades"] if t[1] == 0}
    signalled = {(d, s) for d, s, _ in with_signals["signals"]}
    days = sorted(by_day)
    for d, s in bought:  # every buy follows an insight on the decision day before (the order fills at the next open)
        prev = max((x for x in days if x < d), default=None)
        assert (prev, s) in signalled


def test_the_estratega_trades_exactly_like_python(markets, monkeypatch):
    import dataclasses

    keys = ["sec_XLV", "sec_XLE", "sec_XLF", "sec_XLI", "eu_EXV4", "eu_EXH1", "eu_EXV1", "eu_EXH4"]
    agenda = []
    for k, m in enumerate(markets):
        ctx = dict(m.context)
        for j, key in enumerate(keys):  # the agenda signals move at different speeds; some missing early on
            if key.startswith("eu_"):
                ctx[f"{key}_trend"] = round(math.sin(k / (40 + 3 * j)), 5)
            if k > 40 * j:
                ctx[f"{key}_gov"] = round(math.sin(k / (30 + 7 * j)), 5)
            ctx[f"{key}_epub"] = round(math.cos(k / (50 + 11 * j)), 5)
            if j != 2:
                ctx[f"{key}_capex"] = round(0.5 * math.sin(k / 80 + j), 5)
            ctx[f"{key}_rs"] = round(0.05 * math.sin(k / (25 + 5 * j)), 5)
        agenda.append(dataclasses.replace(m, context=ctx))
    base = {**{k: agents.COMMON[k] for k in pretrain.SHARED_IN_BACKTEST}, **agents.STRATEGIES["estratega"][2]}
    compare("estratega", base, agenda, monkeypatch, min_trades=10)
    compare("estratega", {**base, "nucleo": 0.2, "setores": 2, "cada": 7, "segue_zona": 1, "stop_pct": 0.1, "peso_capex": 0},
            agenda, monkeypatch, min_trades=10)
    compare("estratega", {**base, **SHARED}, agenda, monkeypatch, min_trades=1)
    eu = [t for t in rust_run("estratega", {**base, "min_acoes_setor": 2, "regiao": 2}, agenda)[1] if "(Europa)" in t[7]]
    assert eu  # Europe's sectors with Europe's signals
    compare("estratega", {**base, "min_acoes_setor": 2, "regiao": 2, "filtro_setor": 2}, agenda, monkeypatch, min_trades=5)
    compare("estratega", {**base, "min_acoes_setor": 2}, agenda, monkeypatch, min_trades=10)
    # every agent can use the agenda as a filter (filtro_agenda): the same trades in both engines, and it does block
    fog = {**{k: agents.COMMON[k] for k in pretrain.SHARED_IN_BACKTEST}, **agents.STRATEGIES["foguetao"][2], "n": 8}
    free = rust_run("foguetao", fog, agenda)[1]
    gated = rust_run("foguetao", {**fog, "filtro_agenda": 0.3}, agenda)[1]
    assert free != gated
    compare("foguetao", {**fog, "filtro_agenda": 0.3}, agenda, monkeypatch, min_trades=10)
    compare("quant", {**{k: agents.COMMON[k] for k in pretrain.SHARED_IN_BACKTEST}, **agents.STRATEGIES["quant"][2],
                      "filtro_agenda": 0.2, "regiao": 2}, agenda, monkeypatch, min_trades=1)


def test_the_navegador_trades_exactly_like_python(markets, monkeypatch):
    import dataclasses

    regimes = []
    for k, m in enumerate(markets):  # the regimes take turns: concentration, broadening, contraction, inflation, none
        ctx = {**m.context, "reg_conc": round(0.05 * math.sin(k / 40), 5), "reg_size": round(0.04 * math.cos(k / 55), 5),
               "reg_credit": round(0.05 * math.sin(k / 70 + 1), 5), "reg_cycle": round(0.1 * math.cos(k / 90), 5)}
        if k % 3:
            ctx["reg_corr"] = round(0.5 * math.sin(k / 33), 5)
        regimes.append(dataclasses.replace(m, context=ctx))
    base = {**{k: agents.COMMON[k] for k in pretrain.SHARED_IN_BACKTEST}, **agents.STRATEGIES["navegador"][2]}
    compare("navegador", base, regimes, monkeypatch, min_trades=10)
    compare("navegador", {**base, "cada": 2, "folga": 0.02, "limiar_conc": 0.0, "limiar_corr": 0.1}, regimes, monkeypatch, min_trades=10)
    reasons = {t[7] for t in rust_run("navegador", base, regimes)[1] if t[7]}
    assert len({r for r in reasons if r.startswith("regime")}) >= 3


def test_the_tematico_trades_exactly_like_python(markets, monkeypatch):
    import copy
    import dataclasses

    subs = ["Semiconductors", "Biotechnology", "Aerospace & Defense", "Regional Banks", None]
    keys = ["ind_SMH", "ind_XBI", "ind_ITA", "ind_KRE"]
    themed = []
    for k, m in enumerate(markets):
        stocks = {}
        for j, (s, row) in enumerate(m.stocks.items()):
            r = copy.copy(row)
            sub = subs[int(s[2:4]) % 5] if s[2:4].isdigit() else None
            if hasattr(r, "_extra"):
                r._extra = {**r._extra, "industry": sub}
            else:
                r = {**r, "industry": sub}
            stocks[s] = r
        ctx = dict(m.context)
        for i, key in enumerate(keys):
            ctx[f"{key}_mom"] = round(0.08 * math.sin(k / (20 + 9 * i) + i), 5)
            if i != 1:
                ctx[f"{key}_trend"] = round(0.3 * math.cos(k / (30 + 5 * i)) + 0.1, 5)
        themed.append(dataclasses.replace(m, stocks=stocks, context=ctx))
    base = {**{k: agents.COMMON[k] for k in pretrain.SHARED_IN_BACKTEST}, **agents.STRATEGIES["tematico"][2], "min_acoes_tema": 2}
    compare("tematico", base, themed, monkeypatch, min_trades=10)
    compare("tematico", {**base, "temas": 2, "por_tema": 1, "cada": 7, "esgotado_max": 0, "nucleo": 0.0, "stop_pct": 0.1},
            themed, monkeypatch, min_trades=10)
    assert any(t[7].startswith("tema: ") and t[7] != "tema: " for t in rust_run("tematico", base, themed)[1])
