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
    assert pos["entry"] == pytest.approx(11 * 1.0025)  # 0.1% costs + 0.15% currency conversion
    assert pos["qty"] == pytest.approx(500 / (11 * 1.0025 / 1.25))  # USD stock bought in EUR
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
    assert score_day(port, market(hist, DATES[1].date())) == 10 + 2  # +1% vs a flat index (capped at 10) + profit bonus
    assert score_day(port, market(hist, DATES[1].date())) is None  # same day twice: no double points
    assert port["points"] == 12


def test_training_mutates_losers_only():
    cfg = {"id": "x", "strategy": "jornalista", "params": {"n": 5, "stop_pct": 0.1}}
    loser = new_portfolio(1000, DATES[0].date())
    loser["history"] = [{"date": "2026-10-01", "equity": 1000, "deposits": 1000},
                        {"date": "2026-10-30", "equity": 900, "deposits": 1000},
                        {"date": "2026-11-02", "equity": 900, "deposits": 1000}]
    winner = {**new_portfolio(1000, DATES[0].date()), "history": [dict(h, equity=1100) for h in loser["history"]]}
    mkt = market({}, date(2026, 11, 2))
    assert train(cfg, loser, mkt) and loser["generation"] == 2 and loser["params"] != cfg["params"]
    assert train(cfg, winner, mkt) is None and winner["params"] == cfg["params"]
    backtestable = {**new_portfolio(1000, DATES[0].date()), "history": loser["history"]}
    assert train({**cfg, "strategy": "foguetao"}, backtestable, mkt) is None  # re-trained by pretrain.py instead


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
    config = {"agents": [{"id": "i", "name": "I", "emoji": "🐢", "strategy": "indexador", "capital": 1000, "params": {},
                          "modo": "diario"}]}
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


def test_deriva_buys_held_gaps_with_stop_under_gap_day():
    hist = {"GAP": frame([100] * 3, [100] * 3, [100] * 3, [100] * 3)}
    m = {"price": 110.0, "sma50": 100.0, "gap_size": 0.06, "gap_low": 104.0, "gap_age": 1,
         "sector": "Energy", "stage2": True, "trend_score": 80}
    filled = {"price": 103.0, "sma50": 100.0, "gap_size": 0.06, "gap_low": 104.0, "gap_age": 1}  # fell back into the gap
    mkt = market(hist, DATES[2].date(), stocks={"GAP": m, "FILL": {**m, **filled}})
    mkt.closes = {"GAP": 110.0, "FILL": 103.0}
    port = new_portfolio(1000, DATES[0].date())
    orders = agents.s_deriva({}, port, mkt, {"n": 5, "salto_pct": 0.04, "max_dias": 20})
    assert [o["symbol"] for o in orders] == ["GAP"]
    assert orders[0]["stop"] == pytest.approx(104 * 0.99) and orders[0]["eur"] == pytest.approx(200)


def test_jornalista_buys_good_news_and_sells_bad_news():
    hist = {"GOOD": frame([100] * 3, [100] * 3, [100] * 3, [100] * 3)}
    stock = {"price": 100.0, "sma50": 90.0, "sector": "Energy", "stage2": True, "trend_score": 80}
    mkt = market(hist, DATES[2].date(), stocks={"GOOD": stock, "BAD": stock, "MEH": stock})
    mkt.closes = {"GOOD": 100.0, "BAD": 100.0}
    mkt.news = {"GOOD": {"score": 6, "importance": 3, "items": [{"resumo": "Contrato enorme"}]},
                "MEH": {"score": 6, "importance": 1, "items": []},
                "BAD": {"score": -5, "importance": 3, "items": []}}
    port = new_portfolio(1000, DATES[0].date())
    port["positions"]["BAD"] = {"qty": 1, "entry": 100, "entry_date": "2026-10-28", "cost_eur": 80, "stop": None,
                                "tp": None, "checked": "2026-10-28"}
    orders = agents.s_jornalista({}, port, mkt, {"n": 5, "limiar": 4, "stop_pct": 0.08, "max_dias": 10})
    assert [(o["side"], o["symbol"]) for o in orders] == [("sell", "BAD"), ("buy", "GOOD")]
    assert orders[1]["reason"] == "Contrato enorme" and orders[1]["stop"] == pytest.approx(92)


def test_news_veto_blocks_buys_for_every_agent_but_cetico():
    closes = [100, 101, 102, 103, 104]
    hist = {"SXR8.DE": frame(closes, closes, closes, closes)}
    config = {"agents": [{"id": "i", "name": "I", "emoji": "🐢", "strategy": "indexador", "capital": 1000, "params": {},
                          "modo": "diario"}]}
    state = {"agents": {}}
    mkt = market(hist, DATES[0].date())
    mkt.news = {"SXR8.DE": {"score": -6, "importance": 3, "items": []}}
    _, _, avoided, _ = run_day(config, state, mkt)
    assert not state["agents"]["i"]["pending"] and "notícias negativas" in avoided[0]


def stock(**kw):
    base = {"price": 100.0, "sma50": 90.0, "sma200": 80.0, "sector": "Energy", "stage2": True, "trend_score": 80,
            "ret_1d": 0.02, "vol_ratio": 2.0, "gap_age": -1, "gap_size": 0.0, "low10_prev": 95.0}
    return {**base, **kw}


def test_catalisador_needs_price_confirmation():
    mkt = market({}, DATES[2].date(), stocks={"UP": stock(), "FLAT": stock(ret_1d=-0.01)})
    mkt.closes = {"UP": 100.0, "FLAT": 100.0}
    mkt.news = {"UP": {"score": 4, "importance": 3}, "FLAT": {"score": 6, "importance": 3}}
    orders = agents.s_catalisador({}, new_portfolio(1000, DATES[0].date()), mkt,
                                  {"n": 5, "limiar": 3, "stop_pct": 0.08, "max_dias": 15})
    assert [o["symbol"] for o in orders] == ["UP"] and orders[0]["stop"] == pytest.approx(95.0)


def test_cetico_buys_bad_news_the_market_ignored():
    mkt = market({}, DATES[2].date(), stocks={"HOLD": stock(ret_1d=0.0), "DROP": stock(ret_1d=-0.05)})
    mkt.closes = {"HOLD": 100.0, "DROP": 100.0}
    mkt.news = {"HOLD": {"score": -4, "importance": 3}, "DROP": {"score": -4, "importance": 3}}
    orders = agents.s_cetico({}, new_portfolio(1000, DATES[0].date()), mkt,
                             {"n": 4, "limiar": 3, "stop_pct": 0.06, "alvo_pct": 0.08, "max_dias": 10})
    assert [o["symbol"] for o in orders] == ["HOLD"] and orders[0]["tp"] == pytest.approx(108)


def league_market(holdings: dict[str, list[str]], histories: dict[str, list[float]] | None = None):
    closes = {s: 100.0 for syms in holdings.values() for s in syms}
    mkt = market({}, DATES[4].date(), stocks={s: stock() for s in closes})
    mkt.closes = closes
    for agent_id, syms in holdings.items():
        port = new_portfolio(1000, DATES[0].date())
        port["cash"] = 1000 - 100 * len(syms)
        for s in syms:
            port["positions"][s] = {"qty": 1.0, "entry": 100, "entry_date": "2026-10-26", "cost_eur": 100,
                                    "stop": None, "tp": None, "checked": "2026-10-30"}
        curve = (histories or {}).get(agent_id, [1000] * 6)
        port["history"] = [{"date": f"2026-10-{20 + i}", "equity": e, "deposits": 1000} for i, e in enumerate(curve)]
        mkt.league[agent_id] = ({"id": agent_id, "name": agent_id.upper(), "strategy": "foguetao"}, port)
    return mkt


def test_conselho_follows_consensus():
    mkt = league_market({"a": ["XOM", "NVDA"], "b": ["XOM"], "c": ["AAPL"]})
    orders = agents.s_conselho({"id": "me"}, new_portfolio(1000, DATES[0].date()), mkt,
                               {"n": 6, "min_votos": 2, "stop_pct": 0.1})
    assert [o["symbol"] for o in orders] == ["XOM"] and "2 agentes" in orders[0]["reason"]


def test_maestro_copies_the_recent_winners():
    mkt = league_market({"win": ["NVDA"], "lose": ["XOM"]},
                        {"win": [1000, 1010, 1020, 1030, 1040, 1050], "lose": [1000, 990, 980, 970, 960, 950]})
    port = new_portfolio(1000, DATES[0].date())
    orders = agents.s_maestro({"id": "me"}, port, mkt, {"n": 8, "lideres": 3, "janela": 20, "cada": 5})
    assert [o["symbol"] for o in orders] == ["NVDA"] and "WIN" in orders[0]["reason"]


def test_points_are_relative_to_the_sp500_and_penalise_drawdown():
    hist = {"AAA.DE": frame([100] * 3, [100] * 3, [100] * 3, [100, 101, 80]),
            "SXR8.DE": frame([100] * 3, [100] * 3, [100] * 3, [100, 103, 103])}
    port = new_portfolio(1000, DATES[0].date())
    port["cash"] = 0
    port["positions"]["AAA.DE"] = {"qty": 10, "entry": 100, "entry_date": "2026-10-26", "cost_eur": 1000,
                                   "stop": None, "tp": None, "checked": "2026-10-26"}
    score_day(port, market(hist, DATES[0].date()))
    assert score_day(port, market(hist, DATES[1].date())) == -10 + 2  # +1% while the index made +3% (-20, capped), in profit
    # -20.8% in a day: excess capped at -10, no bonus, drawdown 20.8% -> penalty round(10.8% x 50) = 5
    assert score_day(port, market(hist, DATES[2].date())) == -10 - 5


def test_taxes_on_last_years_gains_only():
    port = new_portfolio(1000, DATES[0].date())
    port["realized"] = {"2025": 400.0}
    port["history"] = [{"date": "2025-12-31", "equity": 1400, "deposits": 1000}]
    port["cash"] = 1400
    t = agents.pay_taxes(port, market({}, date(2026, 1, 2)))
    assert t["eur"] == 112 and port["cash"] == 1288
    port["history"].append({"date": "2026-01-02", "equity": 1288, "deposits": 1000})
    assert agents.pay_taxes(port, market({}, date(2026, 1, 5))) is None  # paid once


def test_risk_filter_sizes_by_volatility_caps_sectors_and_brakes():
    mkt = market({}, DATES[2].date(), stocks={"CALM": stock(vol20=0.01), "WILD": stock(vol20=0.04),
                                              "MORE": stock(vol20=0.01)})
    mkt.closes = {"CALM": 100.0, "WILD": 100.0, "MORE": 100.0}
    port = new_portfolio(1000, DATES[0].date())
    orders = [agents.order_buy("CALM", 200, "x"), agents.order_buy("WILD", 300, "x"), agents.order_buy("MORE", 100, "x")]
    kept, notes = agents.risk_filter(orders, port, mkt)
    assert [(o["symbol"], o["eur"]) for o in kept] == [("CALM", 200), ("WILD", 150)]  # half size for 4% daily vol
    assert "MORE" in notes[0] and "Energia" in notes[0]  # 200 + 150 + 100 > 40% of 1000 in one sector
    port["brake_peak"] = 1.5  # now 33% below its last high: buys pause for two weeks
    kept, notes = agents.risk_filter(orders, port, mkt)
    assert kept == [] and "travão" in notes[0] and port["brake_until"] == "2026-11-11"
    later = market({}, date(2026, 11, 12), stocks=mkt.stocks)
    later.closes = mkt.closes
    kept, _ = agents.risk_filter(orders, port, later)  # pause over: trades again, measured from today's level
    assert kept and port["brake_until"] is None and port["brake_peak"] == pytest.approx(1.0)


def test_bankrupt_agent_is_replaced_by_a_child_of_the_leader():
    closes = [100, 40, 40, 40, 40]
    hist = {"BUST.DE": frame(closes, closes, closes, closes), "SXR8.DE": frame([100] * 5, [100] * 5, [100] * 5, [100] * 5)}
    config = {"agents": [
        {"id": "loser", "name": "Perdedor", "emoji": "🎲", "strategy": "kamikaze", "capital": 1000, "params": {}},
        {"id": "winner", "name": "Vencedor", "emoji": "🚀", "strategy": "foguetao", "capital": 1000,
         "params": {"n": 5, "stop_pct": 0.1, "cada": 5}},
    ]}
    loser, winner = new_portfolio(1000, DATES[0].date()), new_portfolio(1000, DATES[0].date())
    loser["cash"] = 0
    loser["positions"]["BUST.DE"] = {"qty": 10, "entry": 100, "entry_date": "2026-10-26", "cost_eur": 1000,
                                     "stop": None, "tp": None, "checked": "2026-10-27"}
    loser["history"] = winner["history"] = [{"date": "2026-10-26", "equity": 1000, "deposits": 1000}]
    winner["points"], winner["params"] = 50, {"n": 5, "stop_pct": 0.1, "cada": 5}
    state = {"agents": {"loser": loser, "winner": winner}}
    _, _, _, events = run_day(config, state, market(hist, DATES[1].date()))
    assert not config["agents"][0]["active"] and loser["positions"] == {}
    child = config["agents"][2]
    assert child["strategy"] == "foguetao" and child["name"] == "Vencedor II" and child["id"] == "winner-2"
    assert state["agents"]["winner-2"]["cash"] == 1000 and state["agents"]["winner-2"]["generation"] == 2
    assert "faliu" in events[0] and "Vencedor II" in events[0]


def test_estrategia_card_explains_rules_params_and_results():
    config = {"agents": [{"id": "f", "name": "Foguetão", "emoji": "🚀", "strategy": "foguetao", "capital": 1000,
                          "params": {"n": 3, "stop_pct": 0.1713, "cada": 11}}]}
    port = new_portfolio(1000, DATES[0].date())
    port["params"] = {"n": 3, "stop_pct": 0.1713, "cada": 11}
    port["history"] = [{"date": "2026-11-03", "equity": 1050, "deposits": 1000}]
    state = {"agents": {"f": port}}
    embeds, _, changed = configure({"acao": "estrategia", "nome": "foguetao"}, config, state, None)
    card = embeds[0]
    fields = {f["name"]: f["value"] for f in card["fields"]}
    assert not changed and "Momentum" in card["description"]
    assert "A cada 11 sessões compra as 3 ações" in fields["🟢 Quando compra"]
    assert "stop de 17.1%" in fields["🔴 Quando vende"]
    assert "`stop_pct` = **17.1%**" in fields["⚙️ Parâmetros atuais"]
    assert "+5.0%" in fields["📊 Ao vivo"]
    index, _, _ = configure({"acao": "estrategia"}, config, state, None)
    assert "Foguetão" in index[0]["description"]


def test_every_strategy_has_an_explanation():
    import strategy_docs
    assert set(strategy_docs.DOCS) == set(agents.STRATEGIES)
    for key, (_, _, default) in agents.STRATEGIES.items():
        assert "«" not in "".join(strategy_docs.describe(key, default)), key  # every placeholder is a real parameter


def test_maestro_follows_the_best_agents_in_the_current_zone():
    mkt = league_market({"bull": ["NVDA"], "bear": ["XLU"]},
                        {"bull": [1000, 1010, 1020, 1030, 1040, 1050], "bear": [1000] * 6})
    mkt.zone = "Queda"
    mkt.league["bull"][1]["zone_stats"] = {"Queda": [-0.02, 20]}  # loses to the index when the market falls
    mkt.league["bear"][1]["zone_stats"] = {"Queda": [0.03, 20]}  # beats it when the market falls
    orders = agents.s_maestro({"id": "me"}, new_portfolio(1000, DATES[0].date()), mkt,
                              {"n": 8, "lideres": 1, "janela": 20, "cada": 5})
    assert [o["symbol"] for o in orders] == ["XLU"] and "zona Queda" in orders[0]["reason"]
    # without enough live days in the zone, the pre-training record decides
    for _, port in mkt.league.values():
        port["zone_stats"] = {}
    mkt.priors = {"foguetao": {"Queda": 0.001}}
    orders = agents.s_maestro({"id": "me"}, new_portfolio(1000, DATES[0].date()), mkt,
                              {"n": 8, "lideres": 2, "janela": 20, "cada": 5})
    assert {o["symbol"] for o in orders} == {"NVDA", "XLU"}


def test_readiness_checks_and_card():
    def port(curve, sells=0, max_dd=0.1):
        p = new_portfolio(1000, DATES[0].date())
        p["history"] = [{"date": f"d{i}", "equity": e, "deposits": 1000} for i, e in enumerate(curve)]
        p["trades"] = [{"side": "venda"}] * sells
        p["max_dd"] = max_dd
        return p
    steady = [1000 * (1.001 ** i) for i in range(200)]
    index = [1000 * (1.0005 ** i) for i in range(200)]
    good = port(steady, sells=40)
    checks = agents.readiness({"strategy": "foguetao"}, good, agents.live_stats(port(index)), {"p5": -0.2})
    assert [ok for _, ok in checks] == [True, True, True, True, True]
    short = agents.readiness({"strategy": "rotador"}, port(steady[:50], max_dd=0.3), None, None)
    assert [ok for _, ok in short] == [False, None, None, None, False]
    config = {"agents": [{"id": "i", "name": "I", "emoji": "🐢", "strategy": "indexador"},
                         {"id": "f", "name": "F", "emoji": "🚀", "strategy": "foguetao"}]}
    card = agents.readiness_card(config, {"agents": {"i": port(index), "f": good}})[0]
    assert "Prontos para a fase 2" in card["description"] and "F" in card["description"]


def test_session_under_way_is_dropped_per_exchange():
    from datetime import datetime, timezone

    idx = pd.to_datetime(["2026-10-29", "2026-10-30", "2026-11-02"])
    df = pd.DataFrame({"Open": [1, 2, 3], "High": [1, 2, 3], "Low": [1, 2, 3], "Close": [1, 2, 3], "Volume": [1, 1, 1]},
                      index=idx)
    hist = {"SXR8.DE": df, "AAPL": df, "EURUSD=X": df}
    # Monday 12:15 in Lisbon: Xetra is open, the US has not opened: Monday's bars are not complete anywhere
    noon = datetime(2026, 11, 2, 12, 15, tzinfo=timezone.utc)
    out = agents.complete_bars(hist, noon)
    assert all(d.index[-1].date() == date(2026, 10, 30) for d in out.values())
    # Monday 17:00 in Lisbon: Xetra closed at 16:30 (+30 min margin), the US is still trading
    evening = datetime(2026, 11, 2, 17, 5, tzinfo=timezone.utc)
    out = agents.complete_bars(hist, evening)
    assert out["SXR8.DE"].index[-1].date() == date(2026, 11, 2)
    assert out["AAPL"].index[-1].date() == date(2026, 10, 30)
    # and a decision day cuts every market there, even one already closed
    assert agents.complete_bars(hist, evening, date(2026, 10, 30))["SXR8.DE"].index[-1].date() == date(2026, 10, 30)
    assert agents.exchange_of("BRK-B") == "" and agents.exchange_of("ASML.AS") == ".AS"


def test_european_currencies_pence_and_buy_taxes():
    fx_gbp = pd.Series([0.85] * len(DATES), index=DATES)
    mkt = market({}, DATES[0].date())
    mkt.fx_rates = {"GBP": fx_gbp}
    assert agents.currency_of("SHEL.L") == "GBp" and agents.currency_of("NOVO-B.CO") == "DKK"
    assert agents.currency_of("ASML.AS") == "EUR" and agents.currency_of("AAPL") == "USD"
    assert agents.to_eur(mkt, "SHEL.L", 2550.0, DATES[0].date()) == pytest.approx(25.50 / 0.85)  # pence -> pounds -> euros
    with pytest.raises(KeyError):  # never value a stock at a made-up rate
        agents.to_eur(mkt, "NESN.SW", 80.0, DATES[0].date())
    assert agents.buy_tax("BNP.PA") == 0.004 and agents.buy_tax("AIR.PA") == 0.0  # Airbus is a Dutch company
    assert agents.buy_tax("SAP.DE") == 0.0 and agents.buy_tax("SXR8.DE") == 0.0 and agents.buy_tax("RYA.IR") == 0.01
    hist = {"BNP.PA": frame([60, 60], [60, 60], [60, 60], [60, 60])}
    port = new_portfolio(1000, DATES[0].date())
    port["pending"] = [{"side": "buy", "symbol": "BNP.PA", "eur": 600, "created": "2026-10-26"}]
    fill_pending(port, market(hist, DATES[1].date()))
    assert port["positions"]["BNP.PA"]["entry"] == pytest.approx(60 * 1.005)  # 0.1% costs + 0.4% French FTT


def test_pence_glitch_days_are_repaired():
    closes = [2500.0] * 10 + [25.1] + [2510.0] * 10
    df = pd.DataFrame({c: closes for c in ("Open", "High", "Low", "Close")} | {"Volume": [1] * 21},
                      index=pd.bdate_range("2026-01-01", periods=21))
    assert agents.fix_pence(df)["Close"].iloc[10] == pytest.approx(2510.0)


def test_buy_is_cancelled_when_the_open_gaps_past_the_limit():
    hist = {"AAA.DE": frame([100, 89, 100], [100, 90, 100], [100, 88, 100], [100, 89, 100]),
            "BBB.DE": frame([100, 103, 100], [100, 104, 100], [100, 102, 100], [100, 103, 100])}
    port = new_portfolio(1000, DATES[0].date())
    port["pending"] = [{"side": "buy", "symbol": s, "eur": 400, "created": "2026-10-26", "ref": 100.0, "gap_max": 0.08}
                       for s in ("AAA.DE", "BBB.DE")]
    done = fill_pending(port, market(hist, DATES[1].date()))
    assert [t["symbol"] for t in done] == ["BBB.DE"]  # opened 3% up: bought
    assert "AAA.DE" not in port["positions"] and port["cancelled"][-1]["symbol"] == "AAA.DE"  # opened 11% down
    assert port["pending"] == [] and port["cash"] == 600


def test_decisions_carry_the_close_and_gap_limit():
    hist = {"AAA.DE": frame([10, 10], [10, 10], [10, 10], [10, 12])}
    mkt = market(hist, DATES[1].date(), etfs={"AAA.DE": {"price": 12, "sma200": 10, "trend_score": 90, "ret_3m": 0.1,
                                                         "ret_6m": 0.1}})
    cfg = {"id": "r", "strategy": "rotador", "params": {"n": 1, "cada": 5}}
    port = new_portfolio(1000, DATES[0].date())
    orders, _ = agents.decide(cfg, port, mkt, cfg["params"], live=False)
    assert orders and orders[0]["ref"] == 12 and orders[0]["gap_max"] == agents.COMMON["gap_max"]


def test_only_the_most_traded_european_stocks_are_in_play():
    values = {f"S{i}.DE": float(i) for i in range(1, 9)} | {"Z.PA": 0.0}
    assert agents.liquid_europe(values) == {f"S{i}.DE" for i in range(3, 9)}  # top 75% of the 8 that trade


def test_europe_universe_file_is_well_formed():
    rows = agents.load_europe()
    assert len(rows) > 250 and len({r["symbol"] for r in rows}) == len(rows)
    sectors = set(agents.SECTORS_PT)
    assert all(r["sector"] in sectors and agents.exchange_of(r["symbol"]) != "" for r in rows)


def test_stop_round_fills_todays_opens_and_stops_without_closing_the_day():
    from datetime import datetime, timezone

    # Thursday: bought at Wednesday's open earlier; today (Friday) so far the low touched the stop
    hist = {"AAA.DE": frame([100, 100, 100, 100, 95], [101, 101, 101, 101, 96], [99, 99, 99, 99, 89],
                            [100, 100, 100, 100, 90]),
            "BBB.DE": frame([50, 50, 50, 50, 52], [50, 50, 50, 50, 53], [50, 50, 50, 50, 51], [50, 50, 50, 50, 52])}
    port = new_portfolio(1000, DATES[0].date())
    port["cash"] = 500
    port["positions"]["AAA.DE"] = {"qty": 5, "entry": 100, "entry_date": "2026-10-28", "cost_eur": 500,
                                   "stop": 90.0, "tp": None, "checked": "2026-10-29"}
    port["pending"] = [{"side": "buy", "symbol": "BBB.DE", "eur": 200, "created": "2026-10-29"}]
    state = {"agents": {"a": port}}
    config = {"agents": [{"id": "a", "name": "A", "emoji": "🅰️", "strategy": "rotador", "active": True, "params": {},
                          "modo": "diario"}]}
    friday = DATES[4].date()
    mkt = market(hist, friday)
    now = datetime(2026, 10, 30, 15, 40, tzinfo=timezone.utc)
    moves, _ = agents.reactive_round(config, state, mkt, now)
    sides = {t["symbol"]: t for _, t in moves}
    assert sides["BBB.DE"]["side"] == "compra" and sides["BBB.DE"]["date"] == "2026-10-30" and sides["BBB.DE"]["hora"] == "15:40"
    assert sides["AAA.DE"]["side"] == "venda" and sides["AAA.DE"]["reason"] == "stop"
    assert sides["AAA.DE"]["price"] == pytest.approx(90 * (1 - 0.001))  # at the stop, as the daily check would
    # the position bought today is still open and today is not marked as checked
    assert port["positions"]["BBB.DE"]["checked"] == "2026-10-29"
    assert not agents.session_started("AAPL", now.replace(hour=13, minute=0)) and agents.session_started("AAPL", now)


def test_reactive_agent_trades_open_markets_now_and_plans_the_closed_ones():
    from datetime import datetime, timezone

    # Friday 10:30 in Lisbon: Xetra is open (today's bar so far), the US opens at 13:30 UTC that week
    hist = {"EU1.DE": frame([10, 10, 10, 10, 10], [10, 10, 10, 10, 11], [10, 10, 10, 10, 9.5], [10, 10, 10, 10, 10.8]),
            "US1": frame([20, 20, 20, 20], [20, 20, 20, 20], [20, 20, 20, 20], [20, 20, 20, 20])}
    mkt = market(hist, DATES[4].date())
    now = datetime(2026, 10, 30, 10, 30, tzinfo=timezone.utc)
    port = new_portfolio(1000, DATES[0].date())
    port["pending"] = [{"side": "buy", "symbol": "OLD.DE", "eur": 300, "created": "2026-10-30"}]
    cfg = {"id": "r", "name": "R", "emoji": "R", "strategy": "x", "params": {}}
    seen_cash = []

    def fake_decide(c, p, m, params, live=True):  # noqa: ARG001
        seen_cash.append(p["cash"])
        return [agents.order_buy("EU1.DE", 200, "sinal"), agents.order_buy("US1", 200, "sinal")], []

    orig = agents.decide
    agents.decide = fake_decide
    try:
        done, _ = agents.react(cfg, port, mkt, {}, now)
    finally:
        agents.decide = orig
    assert seen_cash == [700]  # the pending buy's money is reserved while it deciding again
    assert [t["symbol"] for t in done] == ["EU1.DE"] and done[0]["price"] == pytest.approx(10.8 * 1.001)  # now, at 10.8
    assert port["positions"]["EU1.DE"]["checked"] == "2026-10-30"  # today's earlier low (9.5) does not stop it
    planned = [o for o in port["pending"] if o["symbol"] == "US1"]
    assert planned and planned[0]["created"] == "2026-10-29"  # fills at the US open later today
    assert agents.live_exits(port, mkt, now) == []


def test_daily_twins_are_created_once_with_the_same_parameters():
    config = {"agents": [{"id": "foguetao", "name": "Foguetão", "emoji": "🚀", "strategy": "foguetao", "capital": 1000,
                          "params": {"n": 5}},
                         {"id": "indexador", "name": "Indexador", "emoji": "🐢", "strategy": "indexador", "capital": 1000,
                          "params": {}}]}
    state = {"agents": {"foguetao": {**new_portfolio(1000, DATES[0].date()), "params": {"n": 7}}}}
    made = agents.ensure_twins(config, state, DATES[0].date())
    assert [t["id"] for t in made] == ["foguetao-diario"]
    assert state["agents"]["foguetao-diario"]["params"] == {"n": 7} and made[0]["modo"] == "diario"
    assert agents.ensure_twins(config, state, DATES[0].date()) == []


def test_rounds_download_the_year_once_a_day_and_add_the_latest_days(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    calls = []
    year = frame([1, 2, 3, 4], [1, 2, 3, 4], [1, 2, 3, 4], [1, 2, 3, 4])
    latest = frame([4, 5, 6, 7, 8], [4, 5, 6, 7, 9], [4, 5, 6, 7, 7], [4, 5, 6, 7, 8.5]).iloc[3:]

    def fake(tickers, period="1y"):
        calls.append(period)
        return {"AAA": year if period == "1y" else latest}

    monkeypatch.setattr(agents, "download_history", fake)
    monkeypatch.setattr(agents, "PRICE_CACHE", tmp_path)
    now = datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)
    out = agents.round_history(["AAA"], now)
    assert list(out["AAA"]["Close"]) == [1, 2, 3, 7, 8.5]  # the fresh days replace the cached ones from their start
    agents.round_history(["AAA"], now)
    assert calls == ["1y", "5d", "5d"]


def test_orders_without_data_wait_and_then_expire():
    hist = {"AAA.DE": frame([10, 10], [10, 10], [10, 10], [10, 10])}
    port = new_portfolio(1000, DATES[0].date())
    port["pending"] = [{"side": "buy", "symbol": "GONE.DE", "eur": 100, "created": "2026-10-26"}]
    mkt = market(hist, DATES[1].date())
    assert fill_pending(port, mkt) == [] and len(port["pending"]) == 1  # no data today: it waits
    old = market(hist, DATES[4].date())
    old.sessions = [d.date() for d in pd.bdate_range("2026-10-26", periods=12)]
    fill_pending(port, old)
    assert port["pending"] == [] and port["cancelled"][-1]["symbol"] == "GONE.DE"


def test_rounds_fill_only_at_a_real_open():
    hist = {"AAA.DE": frame([10, float("nan")], [10, 11], [10, 9], [10, 10.5])}
    port = new_portfolio(1000, DATES[0].date())
    port["pending"] = [{"side": "buy", "symbol": "AAA.DE", "eur": 100, "created": "2026-10-26"}]
    mkt = market(hist, DATES[1].date())
    assert fill_pending(port, mkt, partial_day=DATES[1].date()) == [] and port["pending"]  # no open yet: waits
    assert fill_pending(port, mkt)[0]["price"] == pytest.approx(10.5 * 1.001)  # a complete day: its close


def test_a_missing_rate_uses_the_last_one_seen(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "FX_LAST_PATH", tmp_path / "fx.json")
    idx = pd.to_datetime(["2026-10-30"])
    agents.with_last_rates({"GBP": pd.Series([0.85], index=idx)})
    rates = agents.with_last_rates({})
    assert float(rates["GBP"].iloc[-1]) == 0.85 and "SEK" not in rates


def test_live_bars_project_whole_number_volumes():
    from datetime import datetime, timezone

    idx = pd.to_datetime(["2026-10-29", "2026-10-30"])
    df = pd.DataFrame({"Open": [1.0, 2.0], "High": [1.0, 2.0], "Low": [1.0, 2.0], "Close": [1.0, 2.0],
                       "Volume": [100, 50]}, index=idx)  # int64, as Yahoo returns it
    now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)  # Xetra half-way through its session
    out = agents.live_bars({"SAP.DE": df}, now)
    assert out["SAP.DE"]["Volume"].iloc[-1] > 50


def test_a_rebalance_whose_buys_were_all_blocked_is_tried_again():
    hist = {"AAA.DE": frame([10, 10], [10, 10], [10, 10], [10, 12])}
    mkt = market(hist, DATES[1].date(), etfs={"AAA.DE": {"price": 12, "sma200": 10, "trend_score": 90, "ret_3m": 0.1,
                                                         "ret_6m": 0.1}})
    mkt.context = {"vix": 40}
    cfg = {"id": "r", "strategy": "rotador", "params": {"n": 1, "cada": 5, "vix_max": 30}}
    port = new_portfolio(1000, DATES[0].date())
    orders, notes = agents.decide(cfg, port, mkt, cfg["params"], live=False)
    assert orders == [] and notes and port["last_rebalance"] is None  # VIX too high: blocked, not "done for 5 sessions"
    mkt.context = {"vix": 20}
    orders, _ = agents.decide(cfg, port, mkt, cfg["params"], live=False)
    assert orders and port["last_rebalance"] == DATES[1].date().isoformat()


def test_league_series_and_today_include_the_value_of_this_round():
    port = new_portfolio(1000, DATES[0].date())
    port["history"] = [{"date": "2026-10-29", "equity": 1000.0, "deposits": 1000}]
    mkt = market({}, DATES[4].date())
    assert agents.with_now(port, 1050.0, mkt)[-1] == ("2026-10-30", pytest.approx(5.0))
    assert agents.day_change(port, 1050.0, mkt) == pytest.approx(0.05)
    port["history"].append({"date": "2026-10-30", "equity": 1050.0, "deposits": 1000})
    assert len(agents.with_now(port, 1050.0, mkt)) == 2  # the close of today is already there


def test_profiles_grade_losses_and_falls_differently():
    calm = agents.profile_score(0.08, 1, 0.10, "conservador")
    wild = agents.profile_score(0.25, 1, 0.35, "conservador")
    assert calm > wild  # a conservative investor prefers +8% with a 10% fall to +25% with a 35% fall
    assert agents.profile_score(0.25, 1, 0.35, "arrojado") > agents.profile_score(0.08, 1, 0.10, "arrojado")
    loss = agents.profile_score(-0.10, 1, 0.12, "conservador")
    assert loss == pytest.approx(3 * 1000 * __import__("math").log(0.9), rel=1e-3)  # losses weigh 3x


def test_region_parameter_limits_where_a_strategy_buys():
    stocks = {"AAPL": {"x": 1}, "SAP.DE": {"x": 2}}
    mkt = market({}, DATES[0].date(), stocks=stocks)
    assert set(agents.in_region(mkt, 1).stocks) == {"AAPL"} and set(agents.in_region(mkt, 2).stocks) == {"SAP.DE"}
    assert agents.in_region(mkt, 0) is mkt and agents.in_region(mkt, 1) is agents.in_region(mkt, 1)  # built once


def test_each_trainable_strategy_gets_a_conservative_and_an_aggressive_agent():
    config = {"agents": [
        {"id": "foguetao", "name": "Foguetão", "emoji": "🚀", "strategy": "foguetao", "capital": 1000, "params": {"n": 5}},
        {"id": "jornalista", "name": "Jornalista", "emoji": "📰", "strategy": "jornalista", "capital": 1000, "params": {}},
        {"id": "foguetao-diario", "name": "Foguetão · diário", "emoji": "🚀", "strategy": "foguetao", "capital": 1000,
         "params": {}, "modo": "diario", "twin_of": "foguetao"}]}
    state = {"agents": {"foguetao": {**new_portfolio(1000, DATES[0].date()), "params": {"n": 7}}}}
    made = agents.ensure_profiles(config, state, DATES[0].date())
    assert sorted(a["id"] for a in made) == ["foguetao-arrojado", "foguetao-conservador"]
    assert state["agents"]["foguetao-conservador"]["params"] == {"n": 7}
    assert agents.ensure_profiles(config, state, DATES[0].date()) == []
    twins = agents.ensure_twins(config, state, DATES[0].date())
    assert [t["twin_of"] for t in twins] == ["jornalista"]  # none for the profile agents
