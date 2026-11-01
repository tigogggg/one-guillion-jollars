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
    assert score == 96  # 25 + 20 + 15 + 10 + 13.5 (quality 90) + 5 + 4 (no news) + 3 (zone) + 1 (sector unknown) = 96.5
    assert [p["key"] for p in parts] == [c[0] for c in opportunities.CRITERIA]
    assert sum(c[2] for c in opportunities.CRITERIA) == 100
    assert opportunities.level(score) == "Alta"


def test_a_broken_volatile_stock_scores_low():
    m = metrics(price=80.0, sma50=95.0, sma200=100.0, sma200_rising=False, pct_from_high=0.4, vol20=0.05, dollar_volume=5e7)
    score, _ = opportunities.safety(m, quality=None, earnings_days=3, news_score=-4, zone="Queda")
    assert score == 0 + 0 + 0 + 1 + 7 + 0 + 0 + 1  # the last 1: sector unknown
    assert opportunities.level(score) == "Muito baixa"


def market(stocks, rankings=(), vcp=(), news=None, earnings=None, zone="Neutra", etfs=None):
    return SimpleNamespace(as_of=date(2026, 10, 30), zone=zone, stage2_share=0.4, vcp=set(vcp), news=news or {},
                           earnings=earnings or {}, rankings=[{"symbol": s, "quality_score": 70} for s in rankings],
                           stocks=stocks, etfs=etfs or {})


def build(mkt, holders=None):
    return opportunities.build(mkt, holders or {}, fetch_earnings=lambda syms: {s: None for s in syms})


def lists(out):
    return {k: [r["symbol"] for r in out[k]] for k in ("buy", "watch", "sell")}


def test_only_real_setups_are_buys():
    mkt = market({
        "VCP": metrics(),                                   # VCP setup, 10% above the 50-day: buy
        "PULL": metrics(price=102.0),                       # ranking leader back at the 50-day average: buy
        "RANK": metrics(),                                  # ranking leader but 10% up, no entry signal: nothing
        "LATE": metrics(price=130.0, high20_prev=120.0, vol_ratio=2.0),  # breakout 8% above the pivot: too late
        "BRK": metrics(price=111.0, high20_prev=108.0, vol_ratio=2.0),   # fresh breakout on volume: buy
    }, rankings=["PULL", "RANK"], vcp=["VCP"])
    out = lists(build(mkt))
    assert sorted(out["buy"]) == ["BRK", "PULL", "VCP"]
    b = build(mkt)["buy"][0]
    assert b["checks"] and all(c.startswith("✅") for c in b["checks"])


def test_a_stock_is_never_both_a_buy_and_a_warning():
    mkt = market({
        "MSFT": metrics(),                                   # VCP but important negative news
        "AAPL": metrics(),                                   # VCP but results in 3 days
        "HOT": metrics(price=125.0, high20_prev=126.0),     # ranking leader stretched 25% above the 50-day
    }, rankings=["HOT"], vcp=["MSFT", "AAPL"], news={"MSFT": {"score": -4, "items": []}},
        earnings={"AAPL": date(2026, 11, 2)})
    out = lists(build(mkt))
    assert out["buy"] == []
    assert sorted(out["watch"]) == ["AAPL", "HOT", "MSFT"]
    assert not set(out["buy"]) & set(out["watch"]) and not set(out["watch"]) & set(out["sell"])


def test_sell_only_when_the_trend_breaks():
    mkt = market({
        "BRK": metrics(price=85.0, stage2=False),            # below the 200-day average: sell
        "DIP": metrics(price=97.0, pct_from_high=0.06),      # lost the 50-day in an intact uptrend: watch, not sell
        "OK": metrics(),                                     # held, healthy: in no list
    })
    out = lists(build(mkt, {"BRK": ["🚀 Foguetão"], "DIP": ["🛡️ Guardião"], "OK": ["🛡️ Guardião"]}))
    assert out["sell"] == ["BRK"] and out["watch"] == ["DIP"] and "OK" not in out["buy"] + out["watch"] + out["sell"]


def test_no_stock_buys_in_a_falling_market():
    mkt = market({"VCP": metrics()}, vcp=["VCP"], zone="Queda")
    assert build(mkt)["buy"] == []


def test_fundamentals_and_sector_count_in_the_score():
    strong = metrics(fund_q=90.0, fund_v=60.0, pe=18.0, mos=0.2)
    weak = metrics(fund_q=20.0, fund_v=10.0)
    s1, p1 = opportunities.safety(strong, None, 30, None, "Alta", sector_trend=0.05)
    s2, _ = opportunities.safety(weak, None, 30, None, "Alta", sector_trend=-0.05)
    assert s1 - s2 == round(90 * 0.1 + 60 * 0.05 - (20 * 0.1 + 10 * 0.05)) + 2
    assert "P/E 18" in next(p["why"] for p in p1 if p["key"] == "qualidade")


def test_every_list_is_shown_by_score_highest_first():
    mkt = market({
        "VCP": metrics(price=104.0, high20_prev=103.0),
        "PULL": metrics(price=102.0),
        "BRK": metrics(price=111.0, high20_prev=108.0, vol_ratio=2.0),
    }, rankings=["PULL"], vcp=["VCP"])
    out = build(mkt)
    for kind in ("buy", "watch", "sell"):
        scores = [r["safety"] for r in out[kind]]
        assert scores == sorted(scores, reverse=True)
