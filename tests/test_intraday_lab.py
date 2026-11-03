import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import intraday_lab as lab  # noqa: E402
import pretrain  # noqa: E402


def intraday(days: int, seed: int = 0, start="2024-01-02") -> pd.DataFrame:
    """Random-walk 30-minute bars, 13 per regular session, New York time."""
    rng = np.random.default_rng(seed)
    sessions = pd.bdate_range(start, periods=days)
    rows, price = [], 100.0
    for d in sessions:
        for slot in range(lab.SLOTS):
            ts = pd.Timestamp(d.year, d.month, d.day, 9, 30, tz=lab.NY) + pd.Timedelta(minutes=30 * slot)
            o = price
            price = max(1.0, price * (1 + rng.normal(0.0005, 0.004)))
            rows.append((ts, o, max(o, price) * 1.001, min(o, price) * 0.999, price, float(rng.integers(1000, 5000))))
    df = pd.DataFrame(rows, columns=["t", "Open", "High", "Low", "Close", "Volume"]).set_index("t")
    return df


def test_checkpoints_see_the_day_so_far_and_the_last_one_is_the_session():
    df = intraday(3)
    bars = lab.checkpoint_bars(df)
    day = pd.Timestamp("2024-01-03")
    today = df[df.index.tz_localize(None).normalize() == day]
    first = bars[0].loc[day]
    assert first["Open"] == today["Open"].iloc[0] and first["Close"] == today["Close"].iloc[0]
    at3 = bars[3].loc[day]
    assert at3["High"] == today["High"].iloc[:4].max() and at3["Volume"] == today["Volume"].iloc[:4].sum()
    full = bars[-1].loc[day]
    assert full["Low"] == today["Low"].min() and full["Close"] == today["Close"].iloc[-1]


def test_features_at_the_close_match_the_daily_engine():
    df = intraday(320, seed=3)
    bars = lab.checkpoint_bars(df)
    full = bars[-1]
    got = lab.features_at(full, bars[-1], 1.0)
    want = pretrain.features(full)
    cols = [c for c in want.columns if c not in ("support", "valid")]
    tail = slice(260, None)
    for c in cols:
        a, b = got[c].iloc[tail].astype(float).to_numpy(), want[c].iloc[tail].astype(float).to_numpy()
        assert np.allclose(a, b, rtol=1e-6, atol=1e-9, equal_nan=True), c


def test_features_during_the_day_use_only_the_day_so_far():
    df = intraday(320, seed=5)
    bars = lab.checkpoint_bars(df)
    full, part = bars[-1], bars[2]
    got = lab.features_at(full, part, 3 / lab.SLOTS)
    day = full.index[300]
    assert got.loc[day, "price"] == part.loc[day, "Close"]
    expected_sma50 = (full["Close"].iloc[251:300].sum() + part.loc[day, "Close"]) / 50
    assert got.loc[day, "sma50"] == pytest.approx(expected_sma50)
    expected_ratio = (part.loc[day, "Volume"] / (3 / lab.SLOTS)) / full["Volume"].iloc[250:300].mean()
    assert got.loc[day, "vol_ratio"] == pytest.approx(expected_ratio)
    # changing the rest of the day changes nothing seen at that checkpoint
    later = full.copy()
    later.loc[day, ["High", "Close"]] = [999.0, 998.0]
    again = lab.features_at(later, part, 3 / lab.SLOTS)
    assert again.loc[day, "price"] == got.loc[day, "price"] and again.loc[day, "atr14"] == got.loc[day, "atr14"]


def test_reactive_and_daily_engines_run_on_the_same_days(monkeypatch):
    intra = {"SPY": intraday(300, seed=1), "AAA": intraday(300, seed=2), "BBB": intraday(300, seed=4),
             "CCC": intraday(300, seed=6)}
    fx = pd.Series(1.1, index=pd.bdate_range("2023-12-01", periods=400))
    sectors = {"AAA": "Industrials", "BBB": "Health Care", "CCC": "Energy"}
    built = lab.Lab(intra, sectors, None, 40, fx, pd.DataFrame(), {})
    assert len(built.days) == 40 and len(built.snap) == lab.SLOTS
    params = {"n": 3, "cada": 5, "stop_pct": 0.1}  # three sectors: each position stays under the 40% sector cap
    daily = lab.run_daily("foguetao", params, built)
    reactive = lab.run_reactive("foguetao", params, built)
    assert len(daily["curve"]) == len(reactive["curve"]) == 40
    assert reactive["trades"] >= 1 and daily["trades"] >= 1


def test_reactivity_is_scored_year_by_year_against_the_index_and_kept_unless_clearly_better():
    assert lab.year_blocks(600) == [(0, 96 + 252), (348, 600)] or lab.year_blocks(600) == [(0, 348), (348, 600)]
    assert lab.year_blocks(504) == [(0, 252), (252, 504)]
    flat = [1.0] * 504
    up = [1.0 + i / 1000 for i in range(504)]
    pts = lab.yearly_points(up, flat, "arrojado")
    assert len(pts) == 2 and all(p > 0 for p in pts)
    assert all(p < 0 for p in lab.yearly_points(flat, up, "arrojado"))
    live = {"reatividade": 2}
    runs = [{"config": live, "years": [10, 10, 10, 10, 10, 10, 10]},
            {"config": {"reatividade": 0}, "years": [12, 8, 13, 9, 12, 10, 10]},           # a little better, noisy
            {"config": {"reatividade": 1, "espera_min": 30, "esticado_max": 0.015}, "years": [40, -30, 5, 5, 5, 5, 50]}]
    assert lab.choose(runs, live) == (live, "mantém")
    runs[1]["years"] = [40, 42, 38, 41, 39, 40, -500]  # clearly better every training year; the exam is sealed
    assert lab.choose(runs, live) == ({"reatividade": 0}, "melhor")
    assert lab.reactivity_of({}) == {"reatividade": 2}
    assert lab.reactivity_of({"reatividade": 1}) == {"reatividade": 1, "espera_min": 15, "esticado_max": 0.0}


def test_reactivity_training_runs_every_level_for_each_strategy(monkeypatch):
    intra = {"SPY": intraday(300, seed=1), "AAA": intraday(300, seed=2), "BBB": intraday(300, seed=4),
             "CCC": intraday(300, seed=6)}
    fx = pd.Series(1.1, index=pd.bdate_range("2023-12-01", periods=400))
    built = lab.Lab(intra, {"AAA": "Industrials", "BBB": "Health Care", "CCC": "Energy"}, None, 40, fx, pd.DataFrame(), {})
    monkeypatch.setattr(lab, "LAB", built)
    monkeypatch.setattr(lab, "agent_params", lambda s, p: ("foguetao-arrojado", {"n": 3, "cada": 5, "stop_pct": 0.1, "regiao": 2}))
    out = lab.train_reactivity("arrojado", {"foguetao": {}}, workers=1)
    v = out["foguetao"]
    assert len(v["runs"]) == len(lab.GRID) and v["live"] == {"reatividade": 2} and v["europe_only"]
    assert v["chosen"] in lab.GRID and all(len(r["years"]) >= 1 for r in v["runs"])


@pytest.mark.parametrize("strategy,params", [
    ("foguetao", {"n": 3, "cada": 5, "stop_pct": 0.1}),
    ("foguetao", {"n": 3, "cada": 5, "stop_pct": 0.05, "reatividade": 0}),
    ("foguetao", {"n": 3, "cada": 4, "stop_pct": 0.06, "reatividade": 1, "espera_min": 90, "esticado_max": 0.01}),
    ("sniper", {"n": 3, "risco_pct": 0.02, "aperto_pct": 0.2, "max_pos": 0.3}),
    ("pescador", {"n": 3, "queda_pct": 0.01, "alvo_pct": 0.02, "stop_pct": 0.03, "max_dias": 5}),
    ("kamikaze", {"n": 2, "stop_pct": 0.05, "cada": 3, "ignora_risco": True, "ignora_resultados": True}),
])
def test_the_engine_replays_the_checkpoints_exactly_like_python(strategy, params, monkeypatch):
    import engine

    if engine.ogj_engine is None:
        pytest.skip("Rust engine not built")
    intra = {"SPY": intraday(300, seed=1), "AAA": intraday(300, seed=2), "BBB": intraday(300, seed=4),
             "CCC": intraday(300, seed=6), "DDD": intraday(300, seed=8)}
    fx = pd.Series(1.1, index=pd.bdate_range("2023-12-01", periods=400))
    sectors = {"AAA": "Industrials", "BBB": "Health Care", "CCC": "Energy", "DDD": "Technology"}
    built = lab.Lab(intra, sectors, None, 60, fx, pd.DataFrame(), {})
    monkeypatch.setattr(lab, "CHUNK_DAYS", 7)  # several chunks: the agents carry their state across them
    py = lab.run_reactive(strategy, params, built)
    rs = lab.run_reactive_engine([(strategy, params)], built)[0]
    for k in ("points", "return", "max_drawdown", "trades", "bankrupt", "trade_stats", "curve"):
        assert rs[k] == py[k], k
    assert py["trades"] > 0


def test_finished_years_and_months_are_downloaded_once(tmp_path, monkeypatch):
    from datetime import date

    ps = lab.periods(date(2024, 6, 1), date(2026, 3, 10))
    assert [p[2] for p in ps] == ["2024", "2025", "2026m01", "2026m02", "2026m03-2026-03-10"]
    assert ps[0][0] == date(2024, 6, 1) and ps[-1][:2] == (date(2026, 3, 1), date(2026, 3, 10))
    # the copies a period replaces go; the other periods' files stay
    monkeypatch.setattr(lab, "CACHE", tmp_path)
    for name in ("AAPL_2026-2026-03-09", "AAPL_2026m03-2026-03-09", "AAPL_2026m02", "AAPL_2025"):
        (tmp_path / f"{name}.pkl").write_bytes(b"")
    monkeypatch.setenv("ALPACA_KEY_ID", "x")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "y")
    (tmp_path / "AAPL_2026m03-2026-03-10.pkl").write_bytes(lab.pickle.dumps(None))
    lab._fetch_chunk(["AAPL"], date(2026, 3, 1), date(2026, 3, 10), "2026m03-2026-03-10", "sip", ps[-1][3])
    assert sorted(f.stem for f in tmp_path.glob("*.pkl")) == ["AAPL_2025", "AAPL_2026m02", "AAPL_2026m03-2026-03-10"]
