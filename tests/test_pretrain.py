import random
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pretrain  # noqa: E402
from agents import extra_metrics  # noqa: E402
from rankings import trend_metrics  # noqa: E402


def synthetic(n=400, seed=1):
    rng = random.Random(seed)
    p, rows = 50.0, []
    for i in range(n):
        o = p * (1.06 if i % 37 == 0 else 1.0)  # regular gaps, some on heavy volume
        p = o * (1 + rng.uniform(-0.02, 0.022))
        vol = rng.randint(500_000, 5_000_000) * (4 if i % 74 == 0 else 1)
        rows.append((o, max(o, p) * 1.01, min(o, p) * 0.99, p, vol))
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=pd.bdate_range("2025-01-01", periods=n))


@pytest.mark.parametrize("k", [220, 260, 297, 299, 333, 371, 373, 400])
def test_backtest_features_match_live_metrics_without_lookahead(k):
    df = synthetic()
    live = {**trend_metrics(df.iloc[:k]), **extra_metrics(df.iloc[:k])}  # what the live run sees on day k
    bt = pretrain.features(df).iloc[k - 1]  # computed once over the whole history
    for key in ("price", "sma50", "sma200", "sma200_rising", "pct_from_high", "pct_above_low", "ret_1d", "ret_1w",
                "ret_3m", "ret_6m", "high20_prev", "low10_prev", "vol_ratio", "range10",
                "gap_size", "gap_low", "gap_age", "vol20", "atr14"):
        assert bt[key] == pytest.approx(live[key], rel=1e-9), key


def test_warmup_rows_are_not_valid():
    f = pretrain.features(synthetic())
    assert not f["valid"].iloc[218] and f["valid"].iloc[219]


def test_historical_membership_filters_snapshots(monkeypatch):
    import datetime as dt
    df = synthetic()
    days = [d.date() for d in df.index[-3:]]
    joined = days[1]
    snaps = pretrain.snapshots({"NEW": df, "OLD": df}, days, {"NEW": {"sector": "Energy"}, "OLD": {"sector": "Energy"}},
                               member=lambda s, d: s == "OLD" or d >= joined)
    assert set(snaps[days[0]]) == {"OLD"} and set(snaps[days[1]]) == {"NEW", "OLD"}

    csv_text = "ticker,start_date,end_date\nBRK.B,2010-02-16,\nXYZ,2001-01-02,2022-06-30\n"

    class R:
        text = csv_text

        def raise_for_status(self):
            pass
    monkeypatch.setattr(pretrain.requests, "get", lambda *a, **k: R())
    m = pretrain.load_membership()
    assert m["BRK-B"] == [(dt.date(2010, 2, 16), None)] and m["XYZ"][0][1] == dt.date(2022, 6, 30)


def test_yearly_blocks_merge_a_short_tail():
    blocks = pretrain.yearly_blocks(list(range(252 * 3 + 40)))
    assert [len(b) for b in blocks] == [252, 252, 292]


def test_splice_extends_an_etf_back_with_a_proxy_in_eur():
    idx = pd.bdate_range("2020-01-01", periods=6)
    proxy = pd.DataFrame({"Open": [100.0] * 6, "High": [100.0] * 6, "Low": [100.0] * 6,
                          "Close": [100, 102, 104, 106, 108, 110], "Volume": [1.0] * 6}, index=idx)
    etf = pd.DataFrame({"Open": [50.0, 51.0], "High": [50.0, 51.0], "Low": [50.0, 51.0], "Close": [50.0, 51.0],
                        "Volume": [5.0, 5.0]}, index=idx[4:])
    fx = pd.Series([2.0] * 6, index=idx)  # 1 EUR = 2 USD
    out = pretrain.splice(etf, [proxy], fx)
    assert list(out.index) == list(idx)
    # proxy in EUR is 54 on the day the ETF starts (108/2); scaled so it joins the ETF's 50
    assert out["Close"].iloc[0] == pytest.approx(100 / 2 * 50 / 54)
    assert out["Close"].iloc[-1] == 51.0


def test_monte_carlo_reflects_risk():
    calm = pretrain.monte_carlo([0.0005] * 300)
    wild = pretrain.monte_carlo([0.04, -0.045] * 150)
    assert calm["p_loss"] == 0 and calm["median"] > 0 and calm["p_ruin"] == 0
    assert wild["p_dd30"] > 0.5 and wild["median"] < calm["median"]


def test_crash_rule_rejects_deeper_falls_and_new_bankruptcies():
    original = {"2008": {"max_drawdown": 0.30, "bankrupt": None}}
    assert pretrain.survives({"2008": {"max_drawdown": 0.34, "bankrupt": None}}, original)
    assert not pretrain.survives({"2008": {"max_drawdown": 0.40, "bankrupt": None}}, original)
    assert not pretrain.survives({"2008": {"max_drawdown": 0.50, "bankrupt": "2008-10-10"}}, original)


def test_merge_job_combines_partial_results(monkeypatch, tmp_path):
    import json

    part = {"train": ["2006-01-02", "2025-10-28"], "valid": ["2025-10-29", "2026-10-29"], "coverage": {"2008": 0.9}}
    out = tmp_path / "out"
    for name in ("foguetao", "indexador"):
        (out / f"pretrain-{name}").mkdir(parents=True)
        (out / f"pretrain-{name}" / f"{name}.json").write_text(
            json.dumps({**part, "strategies": {name: {"adopted": False, "chosen": {}}}}))
    report = tmp_path / "pretrain.json"
    report.write_text(json.dumps({"strategies": {"joalheiro": {"skipped": "x"}, "foguetao": {"old": True}}}))
    monkeypatch.setattr(pretrain, "REPORT_PATH", report)
    sent = {}
    monkeypatch.setattr(pretrain, "report", lambda results, applied: (sent.setdefault("r", results), [[], {}])[1])
    monkeypatch.setattr(pretrain, "send", lambda *a, **k: 0)
    monkeypatch.setattr(sys, "argv", ["pretrain.py", "--juntar", str(out), "--dry-run"])
    assert pretrain.main() == 0
    merged = json.loads(report.read_text())
    assert set(merged["strategies"]) == {"joalheiro", "foguetao", "indexador"}
    assert "old" not in merged["strategies"]["foguetao"] and merged["coverage"] == {"2008": 0.9}


def test_new_rules_and_analista_run_in_the_backtest():
    import fundamentals as F

    hist = {s: synthetic(600, seed=i) for i, s in enumerate(["AAA", "BBB", "CCC"])}
    idx = hist["AAA"].index
    days = [d.date() for d in idx[pretrain.WARMUP:]]
    rows = pd.DataFrame({"symbol": "AAA", "end": idx[100], "revenue": 1000.0, "op_income": 250.0, "net_income": 200.0,
                         "ocf": 260.0, "capex": 40.0, "assets_cur": 500.0, "liab_cur": 200.0, "cash": 300.0, "debt": 100.0,
                         "debt_cur": 0.0, "equity": 800.0, "shares": 20.0, "rev_growth": 0.12}, index=pd.DatetimeIndex([idx[450]]))
    frames = {"AAA": F.daily(rows, hist["AAA"]["Close"])}
    snaps = pretrain.snapshots(hist, days, {s: {"sector": "Energy"} for s in hist}, frames=frames)
    some = snaps[days[-1]]
    assert some["AAA"]["fund_q"] is not None and "fund_q" not in some["BBB"]
    assert all(k in some["BBB"] for k in ("ema21", "ema50", "support"))
    from agents import Market, zone_for
    fx = pd.Series(1.1, index=idx)
    ctx = {"sec_XLE_trend": -0.05, "sec_XLE_rs": -0.02, "gpr_pct": 0.5, "oil_chg": 0.1, "gold_chg": 0.02}
    markets = [Market(hist=hist, fx=fx, stocks=snaps[d], etfs={}, as_of=d, sessions=[x for x in days if x <= d],
                      zone=zone_for(0.5), stage2_share=0.5, closes={s: float(h["Close"].asof(pd.Timestamp(d))) for s, h in hist.items()},
                      context=ctx) for d in days]
    base = dict(pretrain.STRATEGIES["analista"][2], qualidade_min=0, valor_min=0)
    r = pretrain.simulate("analista", base, markets)
    assert "points" in r
    blocked = pretrain.simulate("foguetao", {**pretrain.STRATEGIES["foguetao"][2], "filtro_setor": 1}, markets)
    assert blocked["trades"] == 0  # the sector is below its 200-day average every day


def test_new_strategies_trade_in_the_backtest():
    hist = {s: synthetic(600, seed=i) for i, s in enumerate(["AAA", "BBB", "CCC", "DDD", "EUNH.DE", "SXR8.DE"])}
    idx = hist["AAA"].index
    days = [d.date() for d in idx[pretrain.WARMUP:]]
    stocks = pretrain.snapshots({k: v for k, v in hist.items() if "." not in k}, days, {s: {"sector": "Energy"} for s in hist})
    etfs = pretrain.snapshots({k: v for k, v in hist.items() if "." in k}, days, {s: {"name": s} for s in hist})
    for d in days:  # fundamentals known for every stock
        for i, m in enumerate(stocks[d].values()):
            m.update(fund_q=60.0 + 10 * i, fund_v=55.0)
    from agents import Market, zone_for
    fx = pd.Series(1.1, index=idx)
    markets = [Market(hist=hist, fx=fx, stocks=stocks[d], etfs=etfs[d], as_of=d, sessions=[x for x in days if x <= d],
                      zone=zone_for(0.5), stage2_share=0.5, closes={s: float(h["Close"].asof(pd.Timestamp(d))) for s, h in hist.items()},
                      context={}) for d in days]
    for strategy in ("quant", "monge", "abutre", "pendulo"):
        params = dict(pretrain.STRATEGIES[strategy][2])
        if strategy == "quant":
            params["so_tendencia"] = 0
        if strategy == "abutre":
            params.update(queda_min=0.05, qualidade_min=0, valor_min=0)
        r = pretrain.simulate(strategy, params, markets)
        assert r["trades"] > 0, strategy


def test_rows_read_like_the_live_dicts():
    hist = {"AAA": synthetic(400, seed=3)}
    days = [d.date() for d in hist["AAA"].index[pretrain.WARMUP:]]
    m = pretrain.snapshots(hist, days, {"AAA": {"sector": "Energy"}})[days[-1]]["AAA"]
    assert isinstance(m["price"], float) and isinstance(m["sma200_rising"], bool) and isinstance(m["gap_age"], int)
    assert m["sector"] == "Energy" and "stage2" in m and "trend_score" in m and m.get("nao_existe") is None
    assert dict(m)["price"] == m["price"] and {**m}["sector"] == "Energy"
    closes = pretrain.PriceRow(np.array([1.5, np.nan]), {"A": 0, "B": 1})
    assert closes.get("A") == 1.5 and closes.get("B") is None and closes.get("C", 7) == 7 and list(closes) == ["A"]
