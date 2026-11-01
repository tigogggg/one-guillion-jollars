import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import agents  # noqa: E402
import context  # noqa: E402


def test_features_align_series_and_never_look_ahead():
    days = pd.bdate_range("2020-01-01", periods=1200)
    raw = {
        "vix": pd.Series(np.linspace(12, 40, len(days)), index=days),
        "epu": pd.Series(np.linspace(50, 300, len(days)), index=pd.date_range("2020-01-01", periods=len(days))),
        "hy": pd.Series(4.0, index=days[::5]),
        "tnx": pd.Series(np.r_[np.full(600, 25.0), np.full(600, 4.0)], index=days),  # old quotes x10
        "fx": pd.Series(np.linspace(1.0, 1.2, len(days)), index=days),
        "spy": pd.Series(np.linspace(100, 200, len(days)), index=days),
    }
    frame = context.features(raw)
    c = context.on(frame, days[800].date())
    assert set(c) == {"vix", "epu_pct", "hy", "rate_chg", "usd_chg", "spy_trend"}
    assert c["vix"] == round(float(raw["vix"].iloc[800]), 5) and c["hy"] == 4.0
    assert c["usd_chg"] > 0 and c["spy_trend"] > 0 and c["epu_pct"] == 1.0
    assert context.on(frame, days[650].date())["rate_chg"] == 1.5  # 25 is an old x10 quote: 2.5% then 4.0%
    assert context.on(frame, pd.Timestamp("2019-12-01").date()) == {}


def test_context_rules_stop_buys_and_explain_why():
    mkt = agents.Market(hist={}, fx=pd.Series(dtype=float), stocks={}, etfs={}, as_of=None, sessions=[], zone="Alta",
                        stage2_share=0.5, context={"vix": 32, "spy_trend": 0.05, "usd_chg": 0.06})
    assert agents.context_gate(mkt, {"vix_max": 30}) == "VIX 32 acima de 30"
    assert agents.context_gate(mkt, {"vix_max": 35, "filtro_spy": 1}) is None
    buy = {"side": "buy", "symbol": "AAA", "eur": 100}
    kept, notes = agents.shared_rules([buy], {"positions": {}}, mkt, {"usd_max": 0.05})
    assert not kept and "dólar caiu" in notes[0]
    kept, _ = agents.shared_rules([buy], {"positions": {}}, mkt, {"vix_meio": 25})
    assert kept[0]["eur"] == 50


def test_saved_context_loads_back_the_same(tmp_path):
    days = pd.bdate_range("2020-01-01", periods=400)
    frame = context.features({"vix": pd.Series(np.linspace(12, 40, len(days)), index=days)})
    context.save(frame, str(tmp_path / "c.csv"))
    back = context.load(str(tmp_path / "c.csv"))
    assert context.on(back, days[300].date()) == context.on(frame, days[300].date())


def test_credit_stress_combines_the_long_and_short_pairs(monkeypatch):
    import rankings

    days = pd.bdate_range("2019-01-01", periods=400)
    falling = pd.Series(np.r_[np.full(300, 100.0), np.linspace(100, 90, 100)], index=days)
    flat = pd.Series(100.0, index=days)
    hist = {"VWEHX": pd.DataFrame({"Close": falling}), "VFITX": pd.DataFrame({"Close": flat}),
            "HYG": pd.DataFrame({"Close": flat[-50:]}), "IEF": pd.DataFrame({"Close": flat[-50:]})}
    monkeypatch.setattr(rankings, "download_history", lambda tickers, period="1y": hist)
    s = context.credit_stress("max")
    assert abs(s.iloc[-1] - 0.10) < 1e-9 and s.iloc[200] == 0


def test_cached_series_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(context, "CACHE_DIR", tmp_path)
    days = pd.date_range("2020-01-01", periods=5)
    pd.Series([1.0, 2, 3, 4, 5], index=days).rename("epu").to_csv(tmp_path / "epu.csv")
    assert context.cached("epu").tolist() == [1, 2, 3, 4, 5] and context.cached("hy") is None
