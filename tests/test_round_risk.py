import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import agents  # noqa: E402


def ny(h, m):  # 2 Nov 2026, New York is UTC-5
    return datetime(2026, 11, 2, h + 5, m, tzinfo=timezone.utc)


def test_reactive_buys_wait_out_the_first_and_last_minutes_of_the_session():
    assert agents.buy_window("AAPL", ny(9, 40), {}) and "abertura" in agents.buy_window("AAPL", ny(9, 40), {})
    assert agents.buy_window("AAPL", ny(10, 0), {}) is None
    assert "últimos" in agents.buy_window("AAPL", ny(15, 40), {})
    assert agents.buy_window("AAPL", ny(15, 40), {"ignora_risco": True}) is None
    # Frankfurt opens at 9:00 Berlin (8:00 UTC in November)
    assert agents.buy_window("SAP.DE", datetime(2026, 11, 2, 8, 5, tzinfo=timezone.utc), {})
    assert agents.buy_window("SAP.DE", datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc), {}) is None


def test_an_agent_down_3_percent_today_stops_buying(monkeypatch):
    mkt = SimpleNamespace(as_of=date(2026, 11, 2))
    port = {"history": [{"date": "2026-10-30", "equity": 1000.0}]}
    monkeypatch.setattr(agents, "equity", lambda p, m: 965.0)
    assert "3%" in agents.round_buy_block(port, mkt, {})
    assert agents.round_buy_block(port, mkt, {"ignora_risco": True}) is None
    monkeypatch.setattr(agents, "equity", lambda p, m: 980.0)
    assert agents.round_buy_block(port, mkt, {}) is None
    port["history"].append({"date": "2026-11-02", "equity": 960.0})  # today's entry already written: compare with the day before
    monkeypatch.setattr(agents, "equity", lambda p, m: 965.0)
    assert agents.round_buy_block(port, mkt, {})


def orb_market(now, high, low, price):
    import pandas as pd

    day = pd.Timestamp("2026-11-02")
    df = pd.DataFrame({"Open": [100.0, 100.0], "High": [101.0, high], "Low": [99.0, low], "Close": [100.0, price],
                       "Volume": [1e6, 1e6]}, index=[day - pd.Timedelta(days=3), day])
    return SimpleNamespace(now=now, as_of=date(2026, 11, 2), zone="Alta", hist={"AAPL": df},
                           stocks={"AAPL": {"price": price, "sma50": 90.0}}, etfs={})


def test_relampago_keeps_the_opening_range_then_buys_the_breakout_and_sells_before_the_close(monkeypatch):
    monkeypatch.setattr(agents, "equity", lambda p, m: 1000.0)
    p = {"n": 4, "alvo_r": 1.5, "rompimento_max": 0.01}
    port = {"positions": {}, "pending": [], "cash": 1000.0}
    assert agents.s_relampago({}, port, orb_market(ny(9, 50), 102, 98, 101), p) == []    # inside the first 30 minutes
    assert agents.s_relampago({}, port, orb_market(ny(10, 0), 102, 98, 101), p) == []    # the range is set now
    assert port["orb"]["levels"]["AAPL"] == [102.0, 98.0]
    assert agents.s_relampago({}, port, orb_market(ny(10, 30), 102.5, 98, 102.5), p)[0]["stop"] == 100.0
    order = agents.s_relampago({}, {**port, "orb": port["orb"]}, orb_market(ny(10, 30), 102.5, 98, 102.5), p)[0]
    assert order["side"] == "buy" and order["tp"] == round(102.5 + 1.5 * 2.5, 4)
    assert agents.s_relampago({}, port, orb_market(ny(10, 30), 104, 98, 104), p) == []   # too far above: chasing
    port["positions"] = {"AAPL": {"entry_date": "2026-11-02"}}
    assert [o["side"] for o in agents.s_relampago({}, port, orb_market(ny(15, 50), 104, 98, 103), p)] == ["sell"]
    assert agents.s_relampago({}, port, SimpleNamespace(now=None), p) == []                # the daily run: nothing


def test_reactivity_levels_change_when_a_reactive_agent_buys(monkeypatch):
    import pandas as pd

    idx = pd.to_datetime(["2026-10-30", "2026-11-02"])
    df = pd.DataFrame({"Open": [100.0, 103.0], "High": [101.0, 104.0], "Low": [99.0, 102.0], "Close": [100.0, 103.0],
                       "Volume": [1, 1]}, index=idx)
    mkt = SimpleNamespace(hist={"AAPL": df}, closes=None, as_of=date(2026, 11, 2))
    assert abs(agents.move_today(mkt, "AAPL") - 0.03) < 1e-9
    total, patient = {"reatividade": 2}, {"reatividade": 1, "espera_min": 60, "esticado_max": 0.02}
    assert agents.buy_window("AAPL", ny(10, 0), total, mkt) is None
    assert "60 min" in agents.buy_window("AAPL", ny(10, 0), patient, mkt)
    assert "sobe 3.0%" in agents.buy_window("AAPL", ny(11, 0), patient, mkt)  # too far above yesterday's close
    assert agents.buy_window("AAPL", ny(11, 0), {**patient, "esticado_max": 0.05}, mkt) is None
    # level 0: the buy waits for the next open as a pending order, even with the market open
    port = agents.new_portfolio(1000, date(2026, 10, 1))
    order = {"side": "buy", "symbol": "AAPL", "eur": 200.0, "reason": "teste"}
    monkeypatch.setattr(agents, "decide", lambda cfg, port, mkt, params, live=True: ([order], []))
    monkeypatch.setattr(agents, "trading_now", lambda mkt, s, now: True)
    monkeypatch.setattr(agents, "round_buy_block", lambda port, mkt, params: None)
    mkt.now = None
    done, _ = agents.react({"id": "x", "strategy": "foguetao"}, port, mkt, {"reatividade": 0}, ny(11, 0), live=False)
    assert done == [] and port["pending"][0]["symbol"] == "AAPL" and port["pending"][0]["created"] == "2026-11-02"
