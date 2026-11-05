import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import agents  # noqa: E402
import jury  # noqa: E402

TODAY = date(2026, 11, 8)


def history(start: date, values: list[float]) -> list[dict]:
    return [{"date": (start + timedelta(days=i)).isoformat(), "equity": v, "deposits": 1000} for i, v in enumerate(values)]


def league(n: int = 8):
    names = ["analista", "quant", "monge", "abutre", "pendulo", "cacador", "sniper", "campeao", "pescador", "deriva"][:n]
    config = {"agents": [{"id": "indexador", "name": "Indexador", "emoji": "🐢", "strategy": "indexador", "capital": 1000, "params": {}}]
              + [{"id": s, "name": s.title(), "emoji": "🤖", "strategy": s, "capital": 1000, "params": dict(agents.STRATEGIES[s][2])}
                 for s in names]}
    start = TODAY - timedelta(days=10)
    state = {"agents": {"indexador": {"history": history(start, [1000, 1010]), "positions": {}, "params": {}}}}
    for k, s in enumerate(names):  # all below the index live, but the 2nd one
        state["agents"][s] = {"history": history(start, [1000, 1020 if k == 1 else 990]), "positions": {}, "params": {}}
    curve = [(f"2010-01-{d:02d}", float(d)) for d in range(1, 29)]
    reps = {"equilibrado": {"strategies": {"indexador": {"curve": curve}, **{
        s: {"overall": 50 - 15 * k, "years_beating_index": 12 - k, "years_total": 20,
            "year": {"max_drawdown": 0.2 + 0.03 * k, "return": 3.0, "return_after_tax": 2.5, "trades": 400 + 50 * k},
            "monte_carlo": {"p_dd30": 0.01 * k, "p_ruin": 0.0}, "crises": {"2008": {"max_drawdown": 0.2 + 0.02 * k}},
            "by_zone": {"Alta": 0.0001, "Queda": -0.0001 * k}, "train": {"default": {"years": [10 - k, 5, -3 * k]}},
            "valid": {"default": {"points": 5 - k}}, "curve": [(d, v * (1 + 0.1 * k) + (k % 3)) for d, v in curve]}
        for k, s in enumerate(names)}}}}
    return config, state, reps


def test_the_jurors_rank_every_agent_and_the_best_lead():
    config, state, reps = league()
    ranked = jury.judge(config, state, reps)
    assert {x["id"] for x in ranked} == {c["id"] for c in config["agents"]} - {"indexador"}
    assert all(0 <= x["scores"][j] <= 100 for x in ranked for j in jury.JURORS)
    assert ranked[0]["id"] == "analista" and ranked[-1]["id"] == "campeao"
    assert ranked[0]["composite"] == pytest.approx(sum(w * ranked[0]["scores"][j] for j, w in jury.JURORS.items()), abs=0.1)


def test_the_fusion_weighs_its_members_equally_or_by_grade_and_place():
    top = [{"id": f"a{i}", "composite": 80.0} for i in range(5)]
    assert jury.super_weights(top) == {f"a{i}": 0.2 for i in range(5)}
    w = jury.super_weights(top, "ranking")
    assert sum(w.values()) == pytest.approx(1.0, abs=1e-3)
    assert w["a0"] == pytest.approx(5 / 15, abs=1e-3) and w["a4"] == pytest.approx(1 / 15, abs=1e-3)


def test_sunday_sets_the_super_agent_and_a_year_out_switches_off():
    config, state, reps = league(8)
    config["agents"].append({"id": "deriva", "name": "Deriva", "emoji": "🌊", "strategy": "deriva", "capital": 1000,
                             "params": {}, "active": False, "banco": {"since": (TODAY - timedelta(days=400)).isoformat()}})
    moves = jury.decide(config, state, jury.judge(config, state, reps), TODAY)
    by_id = {c["id"]: c for c in config["agents"]}
    assert by_id["super"]["membros"] == moves["weights"] and len(moves["weights"]) == jury.TOP
    assert "deriva" in moves["off"] and by_id["deriva"]["desligado"]
    assert all(by_id[s].get("active", True) for s in ("analista", "campeao"))  # nobody leaves on Sundays


def test_the_monthly_renewal_keeps_the_best_twenty_live_winners_first(monkeypatch):
    monkeypatch.setattr(jury, "LEAGUE_SIZE", 5)
    monkeypatch.setattr(agents, "LIVE_PROTECT_SESSIONS", 2)  # the test's live records are two days long
    config, state, reps = league(8)
    state["agents"]["campeao"]["history"][-1]["equity"] = 1100  # the jury's last, but beating the index live
    config["agents"].append({"id": "campeao-diario", "name": "Campeao · diário", "emoji": "🤖", "strategy": "campeao",
                             "capital": 1000, "params": {}, "modo": agents.DAILY, "twin_of": "campeao"})
    config["agents"].append({"id": "monge-diario", "name": "Monge · diário", "emoji": "🤖", "strategy": "monge",
                             "capital": 1000, "params": {}, "modo": agents.DAILY, "twin_of": "monge"})
    by_id = {c["id"]: c for c in config["agents"]}
    by_id["sniper"]["active"] = False
    by_id["sniper"]["banco"] = {"since": (TODAY - timedelta(days=30)).isoformat()}
    ranked = jury.judge(config, state, reps)
    moves = jury.renew(config, state, ranked, TODAY)
    active = {c["id"] for c in config["agents"] if c.get("active", True) and c["strategy"] != "indexador" and not c.get("twin_of")}
    assert len(active) == 5 and "campeao" in active and moves["kept"][0] in ("campeao", "quant")
    assert {"campeao-diario"} <= {c["id"] for c in config["agents"] if c.get("active", True)}  # the twin follows
    assert not by_id["monge-diario"].get("active", True) or "monge" in active
    assert by_id["indexador"].get("active", True)


def test_claude_helps_the_jurors_within_bounds(monkeypatch):
    import json as _json

    config, state, reps = league(8)
    reply = {"agentes": {"campeao": {"risco": {"ajuste": 40, "porque": "quedas fortes nas crises"}},
                         "nobody": {"risco": {"ajuste": 5, "porque": "x"}}, "quant": {"inventado": {"ajuste": 3}}}}

    class Proc:
        returncode = 0
        stdout = _json.dumps({"result": _json.dumps(reply)})
        stderr = ""

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setattr(jury.shutil, "which", lambda _: "/bin/claude")
    monkeypatch.setattr(jury.subprocess, "run", lambda *a, **k: Proc())
    plain = {x["id"]: x for x in jury.judge(config, state, reps)}
    helped = {x["id"]: x for x in jury.judge(config, state, reps, use_claude=True)}
    assert helped["campeao"]["claude"]["risco"]["ajuste"] == jury.CLAUDE_MAX  # clamped
    assert helped["campeao"]["scores"]["risco"] == min(100, plain["campeao"]["scores"]["risco"] + jury.CLAUDE_MAX)
    assert helped["quant"]["claude"] == {} and "nobody" not in helped


def test_each_sunday_with_a_new_top_five_makes_a_new_super_agent_and_the_cap_replaces_the_worst_live(monkeypatch):
    config, state, reps = league(8)
    ranked = jury.judge(config, state, reps)
    first = jury.decide(config, state, ranked, TODAY)
    assert first["created"] == "super"
    again = jury.decide(config, state, ranked, TODAY + timedelta(days=7))
    assert "created" not in again  # the same top 5: no copy
    by_id = {c["id"]: c for c in config["agents"]}
    by_id["analista"]["active"] = False  # the top changes
    assert "created" not in jury.decide(config, state, jury.judge(config, state, reps), TODAY + timedelta(days=14))  # a month
    monkeypatch.setattr(jury, "MAX_AGENTS", len(config["agents"]))
    monkeypatch.setattr(jury, "LIVE_MIN_SESSIONS", 2)
    state["agents"]["campeao"]["history"][-1]["equity"] = 900  # the worst live
    third = jury.decide(config, state, jury.judge(config, state, reps), TODAY + timedelta(days=30))
    assert third["created"] == "super-2" and third["replaced"] == "campeao"
    assert by_id["campeao"]["desligado"]["motivo"] == "substituido" and by_id["super"]["membros"] != third["weights"]


def test_the_luck_test_counts_every_agent_as_a_try():
    config, state, reps = league(8)
    ranked = jury.judge(config, state, reps)
    few, many = jury.league_luck(ranked, 8), jury.league_luck(ranked, 200)
    assert few["agent"] == ranked[0]["id"] and many["trials"] == 200 and many["dsr"] <= few["dsr"]


def test_a_frozen_candidate_is_never_replaced(monkeypatch):
    config, state, reps = league(8)
    for c in config["agents"]:
        if c["id"] != "campeao":
            c["congelado"] = {"desde": "2026-11-05", "ate": "2027-11-05"}
    monkeypatch.setattr(jury, "MAX_AGENTS", len(config["agents"]))
    monkeypatch.setattr(jury, "LIVE_MIN_SESSIONS", 2)
    state["agents"]["analista"]["history"][-1]["equity"] = 500  # the worst live, but frozen
    assert jury.make_room(config, state, TODAY, "novo") == "campeao"
