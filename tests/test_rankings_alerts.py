import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from opportunity_alerts import NEW_YORK, check_breakout, session_fraction  # noqa: E402
from rankings import add_trend_scores, is_stage2, quality_score, trend_metrics  # noqa: E402


def bars(closes, volume=1_000_000, last_volume=None):
    df = pd.DataFrame(
        {
            "Close": closes,
            "High": [c * 1.01 for c in closes],
            "Low": [c * 0.99 for c in closes],
            "Volume": [volume] * len(closes),
        },
        index=pd.date_range("2025-01-01", periods=len(closes), freq="B"),
    )
    if last_volume is not None:
        df.iloc[-1, df.columns.get_loc("Volume")] = last_volume
    return df


def test_uptrend_is_stage2_and_downtrend_is_not():
    up = trend_metrics(bars([50 + i * 0.4 for i in range(260)]))
    down = trend_metrics(bars([150 - i * 0.4 for i in range(260)]))
    assert is_stage2(up) and not is_stage2(down)


def test_short_history_has_no_metrics():
    assert trend_metrics(bars([10.0] * 100)) is None


def test_trend_scores_reward_structure_and_momentum():
    m = {
        "UP": trend_metrics(bars([50 + i * 0.4 for i in range(260)])),
        "FLAT": trend_metrics(bars([100.0 + (i % 2) for i in range(260)])),
        "DOWN": trend_metrics(bars([150 - i * 0.4 for i in range(260)])),
    }
    add_trend_scores(m)
    assert m["UP"]["trend_score"] == 100
    assert m["UP"]["trend_score"] > m["FLAT"]["trend_score"] > m["DOWN"]["trend_score"]


def test_quality_score_bounds():
    best = {"profitMargins": 0.3, "returnOnEquity": 0.4, "revenueGrowth": 0.3, "debtToEquity": 50}
    assert quality_score(best) == 100
    assert quality_score({}) == 5
    assert quality_score({"profitMargins": -0.1, "debtToEquity": 300}) == 0


def base_then(last_close, last_volume):
    closes = [100 + (i % 5) * 0.5 for i in range(80)] + [last_close]
    return bars(closes, last_volume=last_volume)


def test_breakout_with_volume_alerts():
    a = check_breakout(base_then(103.5, 2_000_000), {}, fraction=1.0)
    assert a and a["pivot"] == pytest.approx(102 * 1.01)
    assert a["volume_ratio"] == pytest.approx(2.0)


def test_no_alert_without_volume_or_when_extended():
    assert check_breakout(base_then(103.5, 1_000_000), {}, fraction=1.0) is None
    assert check_breakout(base_then(110.0, 3_000_000), {}, fraction=1.0) is None


def test_intraday_volume_is_projected():
    # 40% of the session gone with 0.7x a full day's volume -> 1.75x projected
    assert check_breakout(base_then(103.5, 700_000), {}, fraction=0.4)


def test_vcp_pivot_and_stop_are_used():
    a = check_breakout(base_then(103.5, 2_000_000), {"pivot": 103.0, "stop": 97.0}, fraction=1.0)
    assert a["pivot"] == 103.0 and a["stop"] == 97.0


def test_session_fraction():
    assert session_fraction(datetime(2026, 10, 30, 12, 45, tzinfo=NEW_YORK)) == pytest.approx(0.5)
    assert session_fraction(datetime(2026, 10, 30, 17, 0, tzinfo=NEW_YORK)) == 1.0
