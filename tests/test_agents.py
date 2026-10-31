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
    config = {"agents": [{"id": "i", "name": "I", "emoji": "🐢", "strategy": "indexador", "capital": 1000, "params": {}}]}
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
