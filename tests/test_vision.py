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


def test_a_grave_event_asks_for_a_view_but_not_twice_a_day(monkeypatch, tmp_path):
    import json
    from datetime import datetime, timedelta, timezone

    import news

    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(vision, "VISION_PATH", tmp_path / "visao.json")
    monkeypatch.setattr(news, "GEO_PATH", tmp_path / "geo.json")
    monkeypatch.setattr("rankings.download_history", lambda *a, **k: {})
    (tmp_path / "visao.json").write_text(json.dumps({"at": (now - timedelta(days=2)).isoformat()}))
    (tmp_path / "geo.json").write_text(json.dumps({"at": (now - timedelta(hours=1)).isoformat(), "risk": 4, "summary": "escalada"}))
    assert "risco geopolítico 4/5" in vision.shock(now)
    (tmp_path / "geo.json").write_text(json.dumps({"at": now.isoformat(), "risk": 2}))
    assert vision.shock(now) is None
    (tmp_path / "geo.json").write_text(json.dumps({"at": now.isoformat(), "risk": 5}))
    (tmp_path / "visao.json").write_text(json.dumps({"at": (now - timedelta(hours=3)).isoformat()}))
    assert vision.shock(now) is None  # a view from 3 hours ago is recent enough


def test_the_view_keeps_tilts_per_horizon_and_factor():
    raw = {"resumo": "x", "cenarios": [{"nome": "base", "probabilidade": 100}], "setores": {"Energy": 1},
           "fatores": {"valor": 2, "magia": 1}, "horizontes": {"3m": {"setores": {"Energy": -1}, "fatores": {"momento": 5}},
                                                              "24m": {"setores": {"Energy": 2}}}}
    v = vision.clean(raw)
    assert v["fatores"] == {"valor": 2} and v["horizontes"] == {"3m": {"setores": {"Energy": -1}, "fatores": {"momento": 2}}}
    assert "⏱️ Por horizonte" in str(vision.card(v))


def test_past_views_are_scored_against_the_market_per_horizon():
    from datetime import date

    import pandas as pd

    days = pd.bdate_range("2025-01-01", "2026-03-01")
    up = pd.Series([100 * 1.001 ** i for i in range(len(days))], index=days)
    flat = pd.Series(100.0, index=days)
    down = pd.Series([100 * 0.999 ** i for i in range(len(days))], index=days)
    closes = {agents.BENCHMARK: flat, "XLE": up, "XLK": down, "VLUE": up}
    hist = [{"at": "2025-01-02T05:41+00:00", "horizontes": {
        "3m": {"setores": {"Energy": 2, "Information Technology": -1}, "fatores": {"valor": -1}},
        "18m": {"setores": {"Energy": 1}, "fatores": {}}}}]
    out = vision.evaluate(hist, closes, date(2025, 6, 1))
    rows = {(r["horizonte"], r["tipo"]): r for r in out["avaliacoes"]}
    assert rows[("3m", "setores")]["maduro"] and rows[("3m", "setores")]["acertos"] == 1.0
    assert rows[("3m", "setores")]["excesso"] > 0 and rows[("3m", "fatores")]["excesso"] < 0
    assert not rows[("18m", "setores")]["maduro"]  # still running: kept apart
    assert out["resumo"]["3m"]["setores"]["maduras"]["visoes"] == 1 and "em_curso" in out["resumo"]["18m"]["setores"]
