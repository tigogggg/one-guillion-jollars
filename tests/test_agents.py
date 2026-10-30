import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import agents  # noqa: E402
from agents import (  # noqa: E402
    Market,
    check_exits,
    configure,
    equity,
    fill_pending,
    new_portfolio,
    run_day,
    score_day,
    train,
)

DATES = pd.bdate_range("2026-10-26", periods=5)  # Mon..Fri


def frame(opens, highs, lows, closes):
    return pd.DataFrame({"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": [1e6] * len(opens)},
                        index=DATES[: len(opens)])


def market(hist, as_of, stocks=None, etfs=None, zone="Neutra"):
    fx = pd.Series([1.25] * len(DATES), index=DATES)
    return Market(hist=hist, fx=fx, stocks=stocks or {}, etfs=etfs or {}, as_of=as_of,
                  sessions=[d.date() for d in DATES if d.date() <= as_of], zone=zone, stage2_share=0.35)


def test_buy_fills_at_next_open_in_eur_with_fee():
    hist = {"AAA": frame([10, 11, 12], [10, 11, 12], [10, 11, 12], [10, 11, 12])}
    port = new_portfolio(1000, DATES[0].date())
    port["pending"] = [{"side": "buy", "symbol": "AAA", "eur": 500, "stop": None, "tp": None, "created": "2026-10-26"}]
    done = fill_pending(port, market(hist, DATES[2].date()))
    pos = port["positions"]["AAA"]
    assert done[0]["date"] == "2026-10-27"  # Tuesday's open, not Monday's close
    assert pos["entry"] == pytest.approx(11 * 1.001)
    assert pos["qty"] == pytest.approx(500 / (11 * 1.001 / 1.25))  # USD stock bought in EUR
    assert port["cash"] == 500


def test_order_waits_when_no_new_session():
    hist = {"AAA": frame([10], [10], [10], [10])}
    port = new_portfolio(1000, DATES[0].date())
    port["pending"] = [{"side": "buy", "symbol": "AAA", "eur": 500, "created": "2026-10-26"}]
    assert fill_pending(port, market(hist, DATES[0].date())) == []
    assert len(port["pending"]) == 1


def test_stop_exits_at_stop_or_gap_open():
    hist = {"AAA.DE": frame([100, 100, 90], [101, 101, 91], [99, 99, 85], [100, 100, 88])}
    port = new_portfolio(1000, DATES[0].date())
    port["cash"] = 0
    port["positions"]["AAA.DE"] = {"qty": 10, "entry": 100, "entry_date": "2026-10-26", "cost_eur": 1000,
                                   "stop": 95, "tp": None, "checked": "2026-10-26"}
    done = check_exits(port, market(hist, DATES[2].date()))
    assert done[0]["reason"] == "stop" and done[0]["date"] == "2026-10-28"
    assert done[0]["price"] == pytest.approx(90 * 0.999)  # gapped below the stop: filled at the open
    assert "AAA.DE" not in port["positions"]


def test_points_for_profit_and_daily_change():
    hist = {"AAA.DE": frame([100] * 3, [100] * 3, [100] * 3, [100, 101, 102])}
    port = new_portfolio(1000, DATES[0].date())
    port["cash"] = 0
    port["positions"]["AAA.DE"] = {"qty": 10, "entry": 100, "entry_date": "2026-10-26", "cost_eur": 1000,
                                   "stop": None, "tp": None, "checked": "2026-10-26"}
    assert score_day(port, market(hist, DATES[0].date())) == 0  # first day: baseline only
    assert score_day(port, market(hist, DATES[1].date())) == 10 + 10  # in profit, +1% capped at +10
    assert score_day(port, market(hist, DATES[1].date())) is None  # same day twice: no double points
    assert port["points"] == 20


def test_training_mutates_losers_only():
    cfg = {"id": "x", "params": {"n": 5, "stop_pct": 0.1}}
    loser = new_portfolio(1000, DATES[0].date())
    loser["history"] = [{"date": "2026-10-01", "equity": 1000, "deposits": 1000},
                        {"date": "2026-10-30", "equity": 900, "deposits": 1000},
                        {"date": "2026-11-02", "equity": 900, "deposits": 1000}]
    winner = {**new_portfolio(1000, DATES[0].date()), "history": [dict(h, equity=1100) for h in loser["history"]]}
    mkt = market({}, date(2026, 11, 2))
    assert train(cfg, loser, mkt) and loser["generation"] == 2 and loser["params"] != cfg["params"]
    assert train(cfg, winner, mkt) is None and winner["params"] == cfg["params"]


def test_configurator_create_and_deposit(tmp_path):
    config, state = {"agents": []}, {"agents": {}}
    _, _, changed = configure({"acao": "criar", "nome": "Teste Ágil", "estrategia": "foguetao", "capital": 500},
                              config, state, market({}, DATES[0].date()))
    assert changed and config["agents"][0]["id"] == "teste-agil"
    configure({"acao": "depositar", "nome": "teste agil", "valor": 250}, config, state, None)
    port = state["agents"]["teste-agil"]
    assert port["cash"] == 750 and port["deposits"] == 750
    _, _, changed = configure({"acao": "criar", "nome": "x", "estrategia": "nope"}, config, state, None)
    assert not changed


def test_run_day_indexador_invests_next_open():
    closes = [100, 101, 102, 103, 104]
    hist = {"SXR8.DE": frame(closes, closes, closes, closes)}
    config = {"agents": [{"id": "i", "name": "I", "emoji": "🐢", "strategy": "indexador", "capital": 1000, "params": {}}]}
    state = {"agents": {}}
    run_day(config, state, market(hist, DATES[0].date()))
    port = state["agents"]["i"]
    assert port["pending"] and not port["positions"]
    run_day(config, state, market(hist, DATES[1].date()))
    assert "SXR8.DE" in port["positions"] and port["cash"] == 0
    assert equity(port, market(hist, DATES[1].date())) == pytest.approx(1000 / 1.001, rel=1e-3)


def test_monthly_report_compares_with_benchmark():
    config = {"agents": [{"id": "i", "name": "I", "emoji": "🐢", "strategy": "indexador", "capital": 1000, "params": {}},
                         {"id": "k", "name": "K", "emoji": "🎲", "strategy": "kamikaze", "capital": 1000, "params": {}}]}
    def port(end):
        p = new_portfolio(1000, date(2026, 10, 1))
        p["history"] = [{"date": "2026-10-01", "equity": 1000, "deposits": 1000},
                        {"date": "2026-10-30", "equity": end, "deposits": 1000},
                        {"date": "2026-11-02", "equity": end, "deposits": 1000}]
        p["trades"] = [{"date": "2026-10-20", "side": "venda", "symbol": "AAA", "pnl_eur": 30.0},
                       {"date": "2026-10-21", "side": "venda", "symbol": "BBB", "pnl_eur": -12.0}]
        return p
    state = {"agents": {"i": port(1020), "k": port(1100)}}
    assert agents.month_changed(state) == "2026-10"
    embeds, files = agents.monthly_report(config, state, "2026-10")
    text = embeds[0]["description"]
    assert text.index("🎲") < text.index("🐢") and "bateu o S&P 500" in text
    assert "melhor **AAA** +30 €" in text and "mes.png" in files
