import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import references  # noqa: E402


def test_inflation_holds_the_last_published_month():
    index = {"2025-12": 100.0, "2026-01": 101.0, "2026-02": 102.0}
    dates = [date(2025, 12, 15), date(2026, 1, 10), date(2026, 2, 27), date(2026, 3, 20)]
    curve = references.inflation_curve(dates, index)
    assert [d for d, _ in curve] == ["2025-12-15", "2026-01-10", "2026-02-27", "2026-03-20"]
    assert [round(v, 6) for _, v in curve] == [0.0, 1.0, 2.0, 2.0]  # March not out yet: flat


def test_cash_compounds_daily_and_ignores_negative_rates():
    rates = [(date(2019, 1, 1), -0.5), (date(2023, 1, 1), 3.65)]
    year = [date(2023, 1, 1) + timedelta(days=n) for n in range(0, 366, 7)] + [date(2024, 1, 1)]
    curve = references.cash_curve([date(2022, 1, 1), *year], rates)
    assert curve[1][1] == 0.0  # all of 2022 below zero: the deposit just stays flat
    assert abs(curve[-1][1] - ((1 + 0.0365 / 365) ** 365 - 1) * 100) < 1e-9


def test_reference_lines_skip_short_charts_and_tolerate_failures(monkeypatch):
    called = []
    monkeypatch.setattr(references, "hicp_index", lambda since: called.append("hicp") or None)
    monkeypatch.setattr(references, "deposit_rates", lambda since: called.append("dfr") or [(date(2020, 1, 1), 2.0)])
    short = {"A": [("2026-10-01", 0.0), ("2026-10-30", 1.0)]}
    assert references.reference_lines(short) == {} and not called  # no download for a few weeks
    long = {"A": [("2025-01-02", 0.0), ("2026-10-30", 5.0)], "B": [("2025-06-02", 0.0)]}
    refs = references.reference_lines(long)
    assert list(refs) == [references.CASH]  # Eurostat down: only the deposit line
    assert [d for d, _ in refs[references.CASH]] == ["2025-01-02", "2025-06-02", "2026-10-30"]


def test_chart_draws_references():
    from charts import lines_chart

    series = {"A": [("2025-01-02", 0.0), ("2026-10-30", 5.0)]}
    refs = {references.INFLATION: [("2025-01-02", 0.0), ("2026-10-30", 3.8)]}
    assert lines_chart(series, "t", refs)[:8] == b"\x89PNG\r\n\x1a\n"
