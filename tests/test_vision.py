import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import agents  # noqa: E402
import vision  # noqa: E402


def test_the_view_keeps_known_sectors_and_industries_with_bounded_tilts():
    raw = {"resumo": "x", "cenarios": [{"nome": "base", "probabilidade": 55}, {"nome": "otimista", "probabilidade": 25},
                                         {"nome": "pessimista", "probabilidade": 20}],
           "setores": {"Industrials": 3, "Utilities": -1, "Nope": 2}, "industrias": {"ind_ITA": 2, "ind_ZZZ": 1, "ind_SMH": "1.4"}}
    v = vision.clean(raw)
    assert v["setores"] == {"Industrials": 2, "Utilities": -1} and v["industrias"] == {"ind_ITA": 2, "ind_SMH": 1}
    assert vision.clean({**raw, "cenarios": [{"probabilidade": 30}]}) is None  # probabilities must add up


def test_the_visionario_follows_the_view(monkeypatch):
    from test_engine import markets as fixture

    m = fixture.__wrapped__()[-1]
    for s, row in m.stocks.items():
        if s.startswith("US"):
            row._extra["industry"] = "Aerospace & Defense" if int(s[2:]) % 2 else "Biotechnology"
    monkeypatch.setitem(agents._VISION, "v", {"at": m.as_of.isoformat(), "industrias": {"ind_ITA": 2, "ind_XBI": -2},
                                               "setores": {}})
    p = dict(agents.STRATEGIES["visionario"][2])
    port = agents.new_portfolio(1000, m.as_of)
    orders = agents.s_visionario({}, port, m, p)
    buys = [o["symbol"] for o in orders if o["side"] == "buy"]
    assert agents.BENCHMARK in buys
    us = [s for s in buys if s.startswith("US")]
    assert us and all(int(s[2:]) % 2 for s in us)  # only the favoured industry
    monkeypatch.setitem(agents._VISION, "v", {"at": "2000-01-01", "industrias": {"ind_ITA": 2}})  # stale view
    port = agents.new_portfolio(1000, m.as_of)
    assert [o["symbol"] for o in agents.s_visionario({}, port, m, p) if o["side"] == "buy"] == [agents.BENCHMARK]
