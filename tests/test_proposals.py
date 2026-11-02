import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import proposals  # noqa: E402


def bars(closes, start="2026-10-01", spread=2.0, opens=None, lows=None, highs=None):
    idx = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({"Open": opens or closes, "High": highs or [c + spread / 2 for c in closes],
                         "Low": lows or [c - spread / 2 for c in closes], "Close": closes, "Volume": [1e6] * len(closes)}, index=idx)


def mkt_with(df, sym="AAA"):
    return SimpleNamespace(hist={sym: df}, closes=None, as_of=df.index[-1].date(), fx=pd.Series([1.10]), zone="Neutra",
                           priors={}, stocks={sym: {}}, etfs={}, rankings=[], earnings={}, news={})


def test_plan_uses_two_atr_stop_and_a_target_at_2_5_r_after_costs_and_tax():
    df = bars([100.0] * 30, spread=2.0)  # ATR 2
    p = proposals.plan(mkt_with(df), "AAA", [])
    assert p["entry_max"] == 100.5 and p["stop"] == 96.0 and p["target"] == 100.5 + 2.5 * 4.5
    assert p["rr"] == 2.5 and p["currency"] == "USD" and abs(p["fx_eur"] - 1 / 1.10) < 1e-4
    gross = (p["target"] / p["entry_max"] - 1)
    assert abs(p["net_target_pct"] - (gross - 2 * (0.001 + 0.0015)) * 0.72 * 100) < 0.01


def test_agent_weight_follows_the_rules():
    port = {"history": [], "trades": []}
    mkt = SimpleNamespace(priors={"foguetao": {"Neutra": 0.0003}}, zone="Neutra")
    pre = {"foguetao": {"adopted": True, "valid": {"best": {"points": 25}}, "monte_carlo": {"p_ruin": 0.2}}}
    w, why = proposals.agent_weight({"strategy": "foguetao"}, port, mkt, pre)
    assert w == round((1 + 0.5 + 1.0) * 0.5, 2) and len(why) == 3


def test_follow_scores_fill_target_and_stop():
    entry = {"date": "2026-10-01", "symbol": "AAA", "entry_max": 101.0, "stop": 96.0, "target": 113.5, "status": "à espera"}
    up = bars([100, 100, 104, 108, 112, 115], spread=1.0)
    e = proposals.follow(entry, up)
    assert e["fill"] == 100 and e["status"] == "alvo" and e["r"] == 3.38  # bought at 100, below the 101 limit: (113.5 - 100) / (100 - 96)
    down = bars([100, 100, 98, 95, 94], spread=1.0)
    assert proposals.follow(entry, down)["status"] == "stop"
    never = bars([100, 105, 106, 107, 108], spread=1.0)
    assert proposals.follow(entry, never)["status"] == "não executada"


def test_scoreboard_counts_by_confidence():
    log = [{"status": "alvo", "fill": 1, "ret": 0.1, "r": 2.5, "bucket": "alta"},
           {"status": "stop", "fill": 1, "ret": -0.04, "r": -1.0, "bucket": "alta"},
           {"status": "não executada", "bucket": "baixa"}]
    sb = proposals.scoreboard(log)
    assert sb["all"]["closed"] == 2 and sb["all"]["win_rate"] == 0.5 and sb["all"]["avg_r"] == 0.75
    assert sb["by_bucket"]["alta"]["targets"] == 1 and sb["by_bucket"]["baixa"]["executed"] == 0


def test_odds_say_what_share_of_wins_the_plan_needs():
    import proposals

    p = {"net_target_pct": 7.2, "net_stop_pct": -3.6}           # wins twice what it loses
    assert proposals.odds(p, {})["needed"] == round(3.6 / 10.8, 3)
    assert "verdict" not in proposals.odds(p, {"trades": 3, "win_rate": 0.6})  # too few trades to compare
    assert proposals.odds(p, {"trades": 40, "win_rate": 0.36})["verdict"] == "perto de empatar"
    assert proposals.odds(p, {"trades": 40, "win_rate": 0.45})["verdict"] == "inclina a favor"
    assert proposals.odds(p, {"trades": 40, "win_rate": 0.10})["verdict"] == "contra"
    assert proposals.odds({"net_target_pct": -0.5, "net_stop_pct": -3.0}, {}) == {}
