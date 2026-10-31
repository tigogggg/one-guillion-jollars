import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import opportunities  # noqa: E402


def metrics(**kw):
    m = {"price": 110.0, "sma50": 100.0, "sma200": 90.0, "sma200_rising": True, "pct_from_high": 0.03, "vol20": 0.012,
         "dollar_volume": 2e9, "ret_1d": 0.01, "ret_1w": 0.02, "ret_3m": 0.1, "trend_score": 80, "stage2": True,
         "high20_prev": 120.0, "vol_ratio": 1.0, "low10_prev": 104.0, "name": "Acme", "sector": "Technology"}
    return {**m, **kw}


def test_a_calm_strong_stock_scores_high_and_every_criterion_is_explained():
    score, parts = opportunities.safety(metrics(), quality=90, earnings_days=30, news_score=None, zone="Alta")
    assert score == 98  # 25 + 20 + 15 + 10 + 13.5 (quality 90) + 5 + 4 (no news) + 5 = 97.5
    assert [p["key"] for p in parts] == [c[0] for c in opportunities.CRITERIA]
    assert sum(c[2] for c in opportunities.CRITERIA) == 100
    assert opportunities.level(score) == "Alta"


def test_a_broken_volatile_stock_scores_low():
    m = metrics(price=80.0, sma50=95.0, sma200=100.0, sma200_rising=False, pct_from_high=0.4, vol20=0.05, dollar_volume=5e7)
    score, _ = opportunities.safety(m, quality=None, earnings_days=3, news_score=-4, zone="Queda")
    assert score == 0 + 0 + 0 + 1 + 7 + 0 + 0 + 0
    assert opportunities.level(score) == "Muito baixa"


def test_build_splits_buy_ideas_and_warnings():
    mkt = SimpleNamespace(
        as_of=date(2026, 10, 30), zone="Neutra", stage2_share=0.4, vcp={"BBB"}, news={}, earnings={},
        rankings=[{"symbol": "AAA", "quality_score": 70}, {"symbol": "CCC", "quality_score": 50}],
        stocks={"AAA": metrics(), "BBB": metrics(), "CCC": metrics(price=85.0, stage2=False), "DDD": metrics()},
        etfs={},
    )
    out = opportunities.build(mkt, {"CCC": ["🚀 Foguetão"]}, fetch_earnings=lambda syms: {s: None for s in syms})
    assert {r["symbol"] for r in out["buy"]} == {"AAA", "BBB"}  # DDD has no reason, CCC is not Stage 2
    assert [r["symbol"] for r in out["sell"]] == ["CCC"]
    assert out["sell"][0]["held_by"] == ["🚀 Foguetão"] and "📉 abaixo da média de 50 dias" in out["sell"][0]["warnings"]
