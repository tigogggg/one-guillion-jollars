import multiprocessing
import random

import pandas as pd
import pytest
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


def _synthetic_training(monkeypatch):
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
    monkeypatch.setattr(pretrain, "other_profiles_choices", lambda strategy: [])  # not the local private reports


def test_train_strategy_runs_end_to_end_on_synthetic_markets(monkeypatch):
    _synthetic_training(monkeypatch)
    strategy, r = pretrain.train_strategy("foguetao")
    assert strategy == "foguetao" and "chosen" in r and r["search"]["candidates"] >= 4
    assert {"stop_atr", "trail_atr", "vix_max"} <= set(r["chosen"]) and "trade_stats" in r["year"]
    assert "dias_resultados" in r["chosen"] and {"stop_atr", "filtro_spy"} <= set(r["importance"])
    assert r["greedy"] and all({"param", "value", "train_gain", "kept"} <= set(t) for t in r["greedy"])


def test_the_report_compares_the_choice_with_each_broker(monkeypatch):
    _synthetic_training(monkeypatch)
    _, r = pretrain.train_strategy("foguetao")
    assert set(r["corretoras"]) == set(agents.BROKERS) and agents.BROKER == agents.DEFAULT_BROKER
    lines = pretrain.broker_lines({"strategies": {"foguetao": r}}, {})
    assert lines[0].startswith("Acima de 0: XTB") and "foguetao" in lines[1]


def test_the_live_parameters_change_only_for_a_clear_gain(monkeypatch):
    _synthetic_training(monkeypatch)
    monkeypatch.setattr(pretrain, "previous_choices", lambda s: [{"chosen": {"n": 3, "stop_pct": 0.2}, "until": None}])
    monkeypatch.setattr(pretrain, "claude_candidates", lambda s, base: [])
    monkeypatch.setattr(pretrain, "DSR_MIN", 1.01)  # no gain can be that certain
    monkeypatch.setitem(pretrain._WF, "foguetao", {"passed": True})  # the walk-forward held: only the DSR decides
    _, r = pretrain.train_strategy("foguetao")
    assert r["chosen"]["n"] == 3 and r["chosen"]["stop_pct"] == 0.2


def test_a_failed_walk_forward_goes_back_to_the_original_parameters(monkeypatch):
    _synthetic_training(monkeypatch)
    monkeypatch.setattr(pretrain, "previous_choices", lambda s: [{"chosen": {"n": 3, "stop_pct": 0.2}, "until": None}])
    monkeypatch.setattr(pretrain, "claude_candidates", lambda s, base: [])
    monkeypatch.setitem(pretrain._WF, "foguetao", {"passed": False})
    _, r = pretrain.train_strategy("foguetao")
    original = pretrain.agents.STRATEGIES["foguetao"][2]
    assert all(r["chosen"].get(k) == v for k, v in original.items())  # not the older search result (n 3)
    assert r["previous_overall"] is not None


def test_a_first_run_must_pass_the_luck_gate_too(monkeypatch):
    _synthetic_training(monkeypatch)
    monkeypatch.setattr(pretrain, "previous_choices", lambda s: [])
    monkeypatch.setattr(pretrain, "claude_candidates", lambda s, base: [])
    monkeypatch.setattr(pretrain, "DSR_MIN", 1.01)
    monkeypatch.setitem(pretrain._WF, "foguetao", {"passed": True})
    _, r = pretrain.train_strategy("foguetao")
    original = pretrain.agents.STRATEGIES["foguetao"][2]
    assert all(r["chosen"].get(k) == v for k, v in original.items())


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="worker processes need fork (Linux)")
def test_training_on_worker_processes_gives_the_same_result(monkeypatch):
    _synthetic_training(monkeypatch)
    _, alone = pretrain.train_strategy("foguetao")
    monkeypatch.setattr(pretrain, "_INNER_POOL", True)
    monkeypatch.setattr(pretrain, "WORKERS", 2)
    _, shared = pretrain.train_strategy("foguetao")
    assert pretrain._POOL is None
    for key in ("chosen", "train", "valid", "search", "greedy", "importance"):
        assert shared[key] == alone[key], key


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
    mkt = SimpleNamespace(hist={"AAA.DE": df}, as_of=idx[1].date(), fx=pd.Series([1.1]), sessions=[])
    assert agents.fill_pending(port, mkt) == [] and len(port["pending"]) == 1
    mkt.as_of = idx[2].date()
    done = agents.fill_pending(port, mkt)
    assert done[0]["price"] > 0 and not port["pending"]


def test_final_phase_keeps_the_rules_that_help_training_and_never_looks_at_the_exam(monkeypatch):
    train, valid = ["train"], ["valid"]

    def fake_sim(strategy, p, markets, detail=True):
        points = 1000
        if markets[0] == "train":  # the training years, one block at a time
            points += 300 if p.get("credito_max") else 0      # helps training…
            points += 200 if p.get("vix_max") else 0          # …this too, but…
            points += 2 if p.get("epu_max") else 0            # too small to count
        else:
            points = 100 - (50 if p.get("vix_max") else 0)    # …it loses on the exam (sealed: reported only)
        return {"points": points}

    monkeypatch.setattr(pretrain, "simulate", fake_sim)
    monkeypatch.setattr(pretrain, "crash_test", lambda s, p, w: {})
    chosen, log, exam = pretrain.greedy_rules("foguetao", {"n": 5}, train, valid, {}, 100)
    kept = {t["param"] for t in log if t["kept"]}
    assert kept == {"credito_max", "vix_max"} and chosen["credito_max"] and chosen["vix_max"]
    assert log[0]["param"] == "credito_max" and log[1]["exam_gain"] == -50  # the exam's verdict, reported


def test_points_are_the_profile_grade_over_the_index(monkeypatch):
    _synthetic_training(monkeypatch)
    markets = pretrain._MARKETS
    r = pretrain.simulate("indexador", dict(agents.STRATEGIES["indexador"][2]), markets)
    years = len(markets) / pretrain.SESSIONS_PER_YEAR
    assert r["points"] == pytest.approx(agents.relative_points(r["profile_score"], pretrain.index_score(markets, years)), abs=0.11)
    assert agents.relative_points(10, 4) == 6 and agents.relative_points(4, 10) == -12  # below the index counts double
    assert abs(r["points"]) < 15  # buying the index scores about what the index does (costs aside)
    # the training years' score: each year on its own, recency-weighted
    blocks = pretrain.yearly_blocks(markets)
    P = dict(agents.STRATEGIES["foguetao"][2])
    each = [pretrain.simulate("foguetao", P, b)["points"] for b in blocks]
    assert pretrain.train_score("foguetao", P, markets) == pytest.approx(pretrain.weighted(each))


def test_a_candidate_stops_once_it_cannot_beat_the_original_in_enough_years(monkeypatch):
    blocks = [[i] for i in range(10)]
    monkeypatch.setitem(pretrain._SEGMENTS, "blocks", blocks)
    calls = []

    def fake(strategy, params, markets, detail=True):
        calls.append(markets[0])
        return {"points": params["v"], "trades": 1}

    monkeypatch.setattr(pretrain, "simulate", fake)
    base = [5.0] * 10  # 65% of 10 years: it must beat 7 of them, so it may lose 3
    years, trades = pretrain._years_vs(("x", {"v": 1.0}, base))
    assert len(calls) == 4 and years == [1.0] * 10 and trades == 4  # stopped after the 4th lost year
    calls.clear()
    years, _ = pretrain._years_vs(("x", {"v": 9.0}, base))
    assert len(calls) == 10 and years == [9.0] * 10


def test_walk_forward_checks_the_search_on_years_it_did_not_see(monkeypatch):
    days = list(range(10 * 150))
    monkeypatch.setattr(pretrain, "_MARKETS", days)
    monkeypatch.setattr(pretrain, "EXAM_SESSIONS", 150)
    seen = []

    def fake_search(strategy, markets, fold=False, keep_live=False):
        seen.append((len(markets), fold))
        return strategy, {"default": {"n": 5}, "chosen": {"n": 3}}

    gains = iter([5.0, -2.0, 4.0, 1.0, 3.0, -1.0])  # per unseen year: new minus the original

    def fake_sim(strategy, params, markets, detail=True):
        return {"points": next(gains) if params == {"n": 3} else 0.0}

    monkeypatch.setattr(pretrain, "_search", fake_search)
    monkeypatch.setattr(pretrain, "simulate", lambda s, p, m, detail=True: {"points": 0.0} if p == {"n": 5} else fake_sim(s, p, m))
    monkeypatch.setattr(pretrain, "_MARKETS", [type("M", (), {"as_of": __import__("datetime").date(2020, 1, 1)})()] * 1500)
    wf = pretrain.walk_forward("foguetao")
    # the exam year (the last 150) is never a test year; each fold stops before its test year; 6 folds while 4 years remain
    assert [n for n, _ in seen] == [1200, 1050, 900, 750, 600] and all(f for _, f in seen)
    assert wf["wins"] == 4 and wf["losses"] == 1 and wf["passed"] and wf["against"] == "originais"


def test_a_series_inside_one_job_stops_when_the_choice_stays(monkeypatch):
    chosen = iter([{"n": 3}, {"n": 4}, {"n": 4}, {"n": 4}, {"n": 9}])
    seeds, histories = [], []

    def fake_pretrain(markets, only=None, processes=None):
        seeds.append(pretrain.SEED)
        histories.append([h["chosen"] for h in pretrain.previous_choices("foguetao")])
        return {"valid": ["2025-01-01", "2026-01-01"], "strategies": {"foguetao": {"chosen": next(chosen)}}}

    monkeypatch.setattr(pretrain, "pretrain", fake_pretrain)
    monkeypatch.setattr(pretrain, "REPORT_PATH", Path("/nonexistent/pretrain.json"))
    monkeypatch.setattr(pretrain, "_SERIES_CHOICES", {})
    r = pretrain.series([], {"foguetao"}, 10)
    assert r["series_runs"] == 4 and r["strategies"]["foguetao"]["chosen"] == {"n": 4}  # 3 → 4 → 4 → 4: stable twice
    assert len(set(seeds)) == 4 and histories[1] == [{"n": 3}] and histories[3][0] == {"n": 4}


def test_each_profile_also_tries_the_other_profiles_choices(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(agents, "REPO_AGENTS", tmp_path)
    monkeypatch.setattr(pretrain, "PROFILE", "equilibrado")
    (tmp_path / "pretrain-conservador.json").write_text(json.dumps({"strategies": {"foguetao": {"chosen": {"n": 3}}}}))
    (tmp_path / "pretrain-arrojado.json").write_text(json.dumps({"strategies": {"foguetao": {"chosen": {"n": 9, "zzz": 1}}}}))
    (tmp_path / "pretrain.json").write_text(json.dumps({"strategies": {"foguetao": {"chosen": {"n": 5}}}}))  # its own: not here
    assert [c["from"] for c in pretrain.other_profiles_choices("foguetao")] == ["conservador", "arrojado"]
    assert pretrain.cross_candidates("foguetao", {"n": 5, "cada": 5}) == [{"n": 3, "cada": 5}, {"n": 9, "cada": 5}]
    assert pretrain.other_profiles_choices("sniper") == []


def test_the_deflated_sharpe_discounts_the_best_of_many_tries():
    steady = [10, 12, 9, 11, 10, 12, 11, 9, 10, 11]
    assert pretrain.deflated_sharpe(steady, []) > 0.99  # always better: certain
    noisy = [30, -25, 10, -15, 20, -10, 5, -20, 25, 0]
    assert pretrain.deflated_sharpe(noisy, []) < 0.8
    tries = [pretrain.sharpe_of([random.gauss(0, 10) for _ in range(10)]) for _ in range(200)]
    random.seed(1)
    lucky = max(([random.gauss(0, 10) for _ in range(10)] for _ in range(200)), key=pretrain.sharpe_of)
    assert pretrain.deflated_sharpe(lucky, tries) < pretrain.deflated_sharpe(lucky, [])  # many tries: less convincing


def test_the_walk_forward_can_look_at_only_the_last_years(monkeypatch):
    from datetime import date as _d, timedelta as _td
    from types import SimpleNamespace

    days = [SimpleNamespace(as_of=_d(2000, 1, 1) + _td(days=i)) for i in range(12 * pretrain.EXAM_SESSIONS)]
    seen = []
    monkeypatch.setattr(pretrain, "_MARKETS", days)
    monkeypatch.setattr(pretrain, "_search", lambda s, past, fold=False: (seen.append(len(past)), (s, {"chosen": {"a": 1}, "default": {}}))[1])
    monkeypatch.setattr(pretrain, "simulate", lambda s, p, test, detail=False: {"points": 5.0 if p else 0.0})
    wf = pretrain.walk_forward("x", window=5)
    assert wf["passed"] and len(wf["folds"]) == pretrain.WF_FOLDS
    assert set(seen) == {5 * pretrain.EXAM_SESSIONS}
    seen.clear()
    pretrain.walk_forward("x")
    assert seen[0] == 10 * pretrain.EXAM_SESSIONS  # all the years before the tested one


def test_discord_hears_only_of_agents_that_improved(monkeypatch):
    import pretrain

    curve = [("2010-01-04", 0.0), ("2020-01-02", 120.0)]
    results = {"strategies": {
        "quant": {"previous_overall": -40.0, "overall": 12.0, "years_beating_index": 11, "years_total": 20,
                  "year": {"max_drawdown": 0.3, "return_after_tax": 4.1}, "curve": curve},
        "sniper": {"previous_overall": 5.0, "overall": -3.0, "curve": curve},  # changed but worse: not news
        "monge": {"previous_overall": -9.0, "overall": 30.0, "curve": curve},  # better but did not change
        "indexador": {"year": {"return_after_tax": 4.4}, "curve": curve}}}
    better = pretrain.improvements(results, {"quant", "sniper"})
    assert [b["strategy"] for b in better] == ["quant"]
    monkeypatch.setattr(pretrain.agents, "load_config", lambda: {"agents": [
        {"id": "quant", "strategy": "quant", "name": "Quant", "emoji": "🧮", "params": {}}]})
    embeds, files = pretrain.improvement_card(results, better, "https://portal")
    assert "1 agente melhorou" in embeds[0]["title"] and "−40" not in embeds[0]["description"]
    assert "-40 → **+12**" in embeds[0]["description"] and "acima do S&P 500" in embeds[0]["description"]
    assert set(files) == {"melhorias.png"} and files["melhorias.png"][:4] == b"\x89PNG"
    sent = []
    monkeypatch.setattr(pretrain, "report", lambda r, a: ([{"url": "https://portal"}], {}))
    monkeypatch.setattr(pretrain, "send", lambda *a, **k: sent.append(a) or 0)
    monkeypatch.setattr(pretrain, "route", lambda c: {})
    assert pretrain.post_report(results, [], False, "agentes", {"sniper"}) == 0 and not sent  # nothing improved
    pretrain.post_report(results, [], False, "agentes", {"quant"})
    assert len(sent) == 1


def test_a_year_with_half_the_index_missing_counts_half_for_stock_pickers(monkeypatch):
    from datetime import date

    import pretrain

    monkeypatch.setattr(pretrain, "COVERAGE_DAYS", {date(2008, 1, 2): 0.5, date(2020, 1, 2): 1.0})
    m = type("M", (), {"as_of": date(2008, 6, 1)})()
    assert pretrain.coverage_scaled(-40.0, "quant", [m], 1.0) == -20.0
    assert pretrain.coverage_scaled(-40.0, "rotador", [m], 1.0) == -40.0  # ETFs only: no survivorship
    assert pretrain.coverage_scaled(-40.0, "quant", [m], 20.0) == -40.0  # whole periods are reported as they are


def test_the_verdict_holds_five_and_ten_year_stretches(monkeypatch):
    from datetime import date, timedelta

    import pretrain

    per = pretrain.SESSIONS_PER_YEAR
    days = [date(2006, 1, 2) + timedelta(days=i * 365 / per) for i in range(12 * per)]
    markets = [type("M", (), {"as_of": d})() for d in days]
    seen = []

    def fake(strategy, params, seg, detail=True):
        seen.append(len(seg))
        return {"points": 10.0}

    monkeypatch.setattr(pretrain, "simulate", fake)
    monkeypatch.setattr(pretrain, "COVERAGE_DAYS", {date(2006, 1, 2): 0.5, date(2009, 1, 2): 1.0})
    w = pretrain.window_points("quant", {}, markets)
    assert len(w["5a"]) == 8 and len(w["10a"]) == 3 and set(seen) == {5 * per, 10 * per}
    assert w["5a"][-1] == 10.0 and w["5a"][0] < 10.0  # the oldest stretch had part of the index missing
    assert pretrain.window_points("rotador", {}, markets)["5a"][0] == 10.0  # ETFs: no survivorship
    assert abs(pretrain.window_verdict({"5a": [10.0] * 8, "10a": [-10.0] * 3})) < 1e-9


def test_holding_stocks_pays_the_dividend_tax_each_year():
    import pretrain

    ten = pretrain.dividend_taxed(1.0, 10, "quant")
    assert 0.9 < ten < 1.0 and abs(ten - (2 * (1 - pretrain.DIVIDEND_TAX_DRAG) ** 10 - 1)) < 1e-12
    assert pretrain.dividend_taxed(1.0, 10, "rotador") == 1.0  # accumulating ETFs: no tax until sold


def test_the_training_never_changes_a_frozen_candidate(monkeypatch, tmp_path):
    from datetime import date

    import pretrain

    config = {"agents": [{"id": "quant-congelado", "strategy": "quant", "name": "Q", "emoji": "🧊", "capital": 1000,
                          "params": {"n": 8}, "modo": pretrain.agents.DAILY, "congelado": {"desde": "2026-11-05"}}]}
    state = {"agents": {"quant-congelado": {"params": {"n": 8}, "evolution": []}}}
    saved = {}
    monkeypatch.setattr(pretrain.agents, "load_config", lambda: config)
    monkeypatch.setattr(pretrain.agents, "load_state", lambda: state)
    monkeypatch.setattr(pretrain.agents, "save", lambda c, s: saved.update(s or {}))
    assert pretrain.apply({"strategies": {"quant": {"chosen": {"n": 3}}}}, date(2026, 11, 8)) == []
    assert state["agents"]["quant-congelado"]["params"] == {"n": 8}


def test_dividends_are_paid_after_tax_to_the_agents_that_held_on_the_ex_date():
    from datetime import date

    import agents

    day = date(2026, 11, 6)
    mkt = type("M", (), {"as_of": day})()
    config = {"agents": [{"id": "a", "name": "A", "emoji": "🤖"}, {"id": "b", "name": "B", "emoji": "🤖"}]}
    state = {"agents": {
        "a": {"cash": 100.0, "positions": {"SAP.DE": {"qty": 10, "entry_date": "2026-10-01"}},
              "dividendos": {"ate": "2026-11-04", "bruto_eur": 0.0, "imposto_eur": 0.0, "liquido_eur": 0.0, "ultimos": []}},
        "b": {"cash": 100.0, "positions": {"SAP.DE": {"qty": 10, "entry_date": "2026-11-06"}}}}}  # bought on the ex-date
    paid = agents.credit_dividends(config, state, mkt, fetch=lambda syms, since: {"SAP.DE": [(date(2026, 11, 5), 2.0),
                                                                                         (date(2026, 1, 5), 9.0)]})
    a, b = state["agents"]["a"], state["agents"]["b"]
    assert len(paid) == 1 and abs(a["cash"] - (100 + 20 * 0.72)) < 1e-9 and a["dividendos"]["bruto_eur"] == 20.0
    assert a["dividendos"]["imposto_eur"] == 5.6 and a["dividendos"]["ate"] == "2026-11-06"
    assert b["cash"] == 100.0 and b["dividendos"]["ate"] == "2026-11-06"
    again = agents.credit_dividends(config, state, mkt, fetch=lambda syms, since: {"SAP.DE": [(date(2026, 11, 5), 2.0)]})
    assert again == [] and abs(a["cash"] - 114.4) < 1e-9  # never paid twice
