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
