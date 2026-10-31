import random

import pandas as pd
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agents  # noqa: E402
import pretrain  # noqa: E402


def test_effective_params_layer_shared_defaults_own_and_zone():
    p = {"n": 5, "stop_atr": 2.0, "por_zona": {"Queda": {"n": 2, "max_ext": 0.1}}}
    e = agents.effective(p, "Queda", "foguetao")
    assert e["n"] == 2 and e["max_ext"] == 0.1 and e["stop_atr"] == 2.0 and e["trail_atr"] == 0 and "por_zona" not in e
    assert agents.effective(p, "Alta", "foguetao")["n"] == 5
    assert "stop_atr" not in agents.effective({}, "Alta", "indexador")


def test_mutate_can_switch_shared_rules_on_and_stays_in_bounds():
    switched = set()
    for seed in range(60):
        p = {"n": 5}
        agents.mutate(p, random.Random(seed), "foguetao")
        for k, (lo, hi, _) in agents.PARAM_BOUNDS.items():
            if p.get(k):
                assert lo <= p[k] <= hi
        switched |= {k for k in agents.OFFABLE if p.get(k)}
    assert switched == agents.OFFABLE


def mkt_one(price=110.0, sma50=100.0, atr=2.0):
    m = {"price": price, "sma50": sma50, "sma200": 90.0, "sma200_rising": True, "vol20": 0.012, "pct_from_high": 0.02, "atr14": atr}
    return SimpleNamespace(stocks={"AAA": m}, etfs={}, closes={"AAA": price}, hist={}, sessions=[], as_of=None, context={})


def test_shared_rules_filter_entries_and_set_the_atr_stop():
    buy = {"side": "buy", "symbol": "AAA", "eur": 100, "stop": None}
    port = {"positions": {}}
    kept, notes = agents.shared_rules([buy], port, mkt_one(), {"max_ext": 0.05})
    assert not kept and "esticada" in notes[0]
    kept, _ = agents.shared_rules([buy], port, mkt_one(), {"stop_atr": 2.5})
    assert kept[0]["stop"] == 105.0


def test_trailing_stop_only_goes_up():
    port = {"positions": {"AAA": {"stop": 100.0}}}
    agents.trail_stops(port, mkt_one(price=120.0, atr=2.0), {"trail_atr": 3})
    assert port["positions"]["AAA"]["stop"] == 114.0
    agents.trail_stops(port, mkt_one(price=110.0, atr=2.0), {"trail_atr": 3})
    assert port["positions"]["AAA"]["stop"] == 114.0


def test_recency_weighting_and_neighbours():
    assert pretrain.weighted([0, 100]) > pretrain.weighted([100, 0])
    n = pretrain.nudge({"n": 5, "stop_pct": 0.1, "trail_atr": 0}, random.Random(1))
    assert n["trail_atr"] == 0 and 0.09 <= n["stop_pct"] <= 0.11


def test_train_strategy_runs_end_to_end_on_synthetic_markets(monkeypatch):
    from test_pretrain import synthetic

    hist = {s: synthetic(900, seed=i) for i, s in enumerate(["AAA", "BBB", "CCC", "DDD", "SXR8.DE"])}
    days = [d.date() for d in hist["AAA"].index][pretrain.WARMUP:]
    stocks = pretrain.snapshots({s: df for s, df in hist.items() if s != "SXR8.DE"}, days, {s: {"sector": "Technology"} for s in hist})
    etfs = pretrain.snapshots({"SXR8.DE": hist["SXR8.DE"]}, days, {"SXR8.DE": {"name": "S&P 500"}})
    closes = pd.DataFrame({s: df["Close"] for s, df in hist.items()})
    fx = pd.Series(1.1, index=hist["AAA"].index)
    markets = []
    for d in days:
        share = sum(m["stage2"] for m in stocks[d].values()) / max(len(stocks[d]), 1)
        markets.append(agents.Market(hist=hist, fx=fx, stocks=stocks[d], etfs=etfs[d], as_of=d, sessions=days[: days.index(d) + 1],
                                     zone=agents.zone_for(share), stage2_share=share,
                                     closes={s: float(v) for s, v in closes.loc[:pd.Timestamp(d)].iloc[-1].items()}))
    for name, value in (("VARIANTS", 3), ("EXAM_SESSIONS", 150), ("GEN2_PARENTS", 1), ("NEIGHBOURS", 1), ("ZONE_TRIES", 1),
                        ("SESSIONS_PER_YEAR", 150), ("MIN_TRADES", 0)):
        monkeypatch.setattr(pretrain, name, value)
    monkeypatch.setattr(pretrain, "_MARKETS", markets)
    strategy, r = pretrain.train_strategy("foguetao")
    assert strategy == "foguetao" and "chosen" in r and r["search"]["candidates"] >= 4
    assert {"stop_atr", "trail_atr", "vix_max"} <= set(r["chosen"]) and "trade_stats" in r["year"]
    assert "dias_resultados" not in r["chosen"] and {"stop_atr", "filtro_spy"} <= set(r["importance"])


def test_position_history_groups_trades_and_justifies_them():
    trades = [
        {"date": "2026-10-01", "side": "compra", "symbol": "AAA", "qty": 2, "price": 10, "eur": 20, "pnl_eur": 0, "reason": "top tendência",
         "ctx": {"trend": 90, "zone": "Alta"}},
        {"date": "2026-10-03", "side": "compra", "symbol": "AAA", "qty": 1, "price": 11, "eur": 11, "pnl_eur": 0, "reason": "top tendência"},
        {"date": "2026-10-08", "side": "venda", "symbol": "AAA", "qty": 3, "price": 12, "eur": 36, "pnl_eur": 5, "reason": "stop",
         "ctx": {"stop": 11.9, "entry": 10.3, "low": 11.8}, "held": 7},
        {"date": "2026-10-09", "side": "compra", "symbol": "BBB", "qty": 1, "price": 5, "eur": 5, "pnl_eur": 0, "reason": "rompimento"},
        {"date": "2026-10-10", "side": "imposto", "symbol": "-", "qty": 0, "price": 0, "eur": 1, "pnl_eur": -1, "reason": "x"},
    ]
    lots = agents.position_history(trades, "foguetao", {"n": 5, "stop_pct": 0.1, "cada": 5})
    assert [l["symbol"] for l in lots] == ["BBB", "AAA"]
    bbb, aaa = lots
    assert bbb["status"] == "aberta" and aaa["status"] == "fechada" and aaa["days"] == 7
    assert aaa["result"] == 5 and aaa["result_pct"] == round(5 / 31 * 100, 2) and len(aaa["buys"]) == 2
    assert aaa["buys"][0]["why"].startswith("Comprou (top tendência)") and "tendência 90/100" in aaa["buys"][0]["why"]
    assert "tocou no stop" in aaa["sells"][0]["why"] and "mínimo do dia 11.80" in aaa["sells"][0]["why"]


def test_a_broken_bar_does_not_fill_the_order():
    idx = pd.bdate_range("2026-10-01", periods=3)
    df = pd.DataFrame({"Open": [10.0, 0.0, 11.0], "High": [10.5, 0.0, 11.5], "Low": [9.5, 0.0, 10.5], "Close": [10.0, float("nan"), 11.0],
                       "Volume": [1, 1, 1]}, index=idx)
    port = agents.new_portfolio(1000, idx[0].date())
    port["pending"] = [{"side": "buy", "symbol": "AAA.DE", "eur": 100, "created": idx[0].date().isoformat()}]
    mkt = SimpleNamespace(hist={"AAA.DE": df}, as_of=idx[1].date(), fx=pd.Series([1.1]))
    assert agents.fill_pending(port, mkt) == [] and len(port["pending"]) == 1
    mkt.as_of = idx[2].date()
    done = agents.fill_pending(port, mkt)
    assert done[0]["price"] > 0 and not port["pending"]
