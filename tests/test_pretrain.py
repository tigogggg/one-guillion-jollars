import random
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pretrain  # noqa: E402
from agents import extra_metrics  # noqa: E402
from rankings import trend_metrics  # noqa: E402


def synthetic(n=400, seed=1):
    rng = random.Random(seed)
    p, rows = 50.0, []
    for _ in range(n):
        o = p
        p *= 1 + rng.uniform(-0.02, 0.022)
        rows.append((o, max(o, p) * 1.01, min(o, p) * 0.99, p, rng.randint(500_000, 5_000_000)))
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=pd.bdate_range("2025-01-01", periods=n))


@pytest.mark.parametrize("k", [220, 260, 333, 400])
def test_backtest_features_match_live_metrics_without_lookahead(k):
    df = synthetic()
    live = {**trend_metrics(df.iloc[:k]), **extra_metrics(df.iloc[:k])}  # what the live run sees on day k
    bt = pretrain.features(df).iloc[k - 1]  # computed once over the whole history
    for key in ("price", "sma50", "sma200", "sma200_rising", "pct_from_high", "pct_above_low", "ret_1d", "ret_1w",
                "ret_3m", "ret_6m", "dollar_volume", "high20_prev", "low10_prev", "vol_ratio", "range10"):
        assert bt[key] == pytest.approx(live[key], rel=1e-9), key


def test_warmup_rows_are_not_valid():
    f = pretrain.features(synthetic())
    assert not f["valid"].iloc[218] and f["valid"].iloc[219]
