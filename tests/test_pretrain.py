import random
import sys
from pathlib import Path

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
