import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import macro  # noqa: E402


def test_fallback_has_fed_and_first_friday_jobs(monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    evts, from_fmp = macro.events(date(2026, 12, 1), date(2026, 12, 11))
    assert not from_fmp
    assert (date(2026, 12, 4), "Emprego (payrolls, data habitual)") in evts
    assert (date(2026, 12, 9), "Decisão de juros da Fed") in evts


def test_names_and_daily_line(monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    assert macro.name_pt("Core Inflation Rate YoY") == "Inflação core (CPI)"
    assert macro.name_pt("Fed Interest Rate Decision") == "Decisão de juros da Fed"
    assert macro.today_tomorrow_line(date(2026, 12, 8)) == "🏛️ **Agenda macro:** amanhã Decisão de juros da Fed"
    assert macro.today_tomorrow_line(date(2026, 12, 3)) == "🏛️ **Agenda macro:** amanhã Emprego (payrolls, data habitual)"
    assert macro.today_tomorrow_line(date(2026, 12, 15)) is None
