import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import agents  # noqa: E402
import pretrain  # noqa: E402
import teams  # noqa: E402


def test_the_picks_fill_the_exposure_with_caps_and_consensus():
    weights = {"a": np.array([[0.3, 0.2, 0.0]]), "b": np.array([[0.3, 0.0, 0.1]])}  # each desk half in cash
    p = {"consenso": 0.0, "max_posicoes": 0, "normalizar": 1, "max_nome": 0.5}
    w = teams.target_weights(p, ["a", "b"], weights, np.array([0.5, 0.5]), 0)
    assert w.sum() == pytest.approx(1.0) and w[0] == pytest.approx(0.5)  # capped; the rest to the others
    w = teams.target_weights({**p, "normalizar": 0}, ["a", "b"], weights, np.array([0.5, 0.5]), 0)
    assert w.sum() == pytest.approx(0.45)  # the desks' cash stays cash
    w = teams.target_weights({**p, "consenso": 1.0, "max_nome": 1.0}, ["a", "b"], weights, np.array([0.5, 0.5]), 0)
    assert w[0] / w[1] == pytest.approx(0.3 * 2 / 0.1)  # held by both desks: double weight
    w = teams.target_weights({**p, "max_posicoes": 1, "max_nome": 1.0}, ["a", "b"], weights, np.array([0.5, 0.5]), 0)
    assert np.count_nonzero(w) == 1


def test_the_manager_spreads_the_capital_by_its_rule():
    eq = np.ones((300, 3))
    eq[:, 0] = np.linspace(1, 2, 300)  # rising
    eq[:, 1] = np.linspace(1, 0.5, 300)  # falling
    assert teams.manager("igual", eq, 299) == pytest.approx([1 / 3] * 3)
    a = teams.manager("momento", eq, 299)
    assert a[0] == pytest.approx(1.0) and a[1] == 0
    assert teams.manager("top3", eq, 299).sum() == pytest.approx(1.0)


def test_teams_run_end_to_end_and_are_graded_like_the_strategies(monkeypatch):
    from test_engine import markets as fixture

    markets = fixture.__wrapped__()
    monkeypatch.setattr(pretrain, "_MARKETS", markets)
    monkeypatch.setattr(teams, "desk_params", lambda s, p: {**{k: agents.COMMON[k] for k in pretrain.SHARED_IN_BACKTEST},
                                                            **agents.STRATEGIES[s][2]})
    r = teams.analyse(markets, "equilibrado", 1000.0, search=3)
    assert r["index"]["overall"] == 0 and set(r["teams"]) == set(teams.TEAMS) and set(r["alone"]) == set(teams.ALL)
    for t in r["teams"].values():
        assert t["configs"] == 4 and len(t["walk_forward"]["folds"]) == 3 and t["membros"]
        assert set(teams.PARAMS) <= set(t["params"]) and 0 <= t["exposure"] <= 1.5
