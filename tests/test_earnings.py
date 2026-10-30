import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import agents  # noqa: E402
import earnings  # noqa: E402

TODAY = date(2026, 10, 31)  # Saturday


class FakeTicker:
    CAL = {"AAPL": {"Earnings Date": [date(2026, 11, 3), date(2026, 11, 4)]}, "OLD": {"Earnings Date": [date(2026, 10, 1)]}}

    def __init__(self, symbol):
        self.calendar = self.CAL.get(symbol, {})


def test_next_earnings_picks_first_upcoming_date(monkeypatch):
    monkeypatch.setattr(earnings.yf, "Ticker", FakeTicker)
    assert earnings.next_earnings("AAPL", TODAY) == date(2026, 11, 3)
    assert earnings.next_earnings("OLD", TODAY) is None  # only past dates
    assert earnings.next_earnings("VWCE.DE", TODAY) is None  # ETF: not even looked up


def test_labels_and_windows():
    d = date(2026, 11, 3)
    assert earnings.label(d) == "ter 3/11"
    assert earnings.within(d, 5, TODAY) and not earnings.within(d, 2, TODAY)
    assert earnings.warning(d, TODAY) == "resultados ter 3/11 (daqui a 3 dias)"
    assert earnings.next_week(TODAY) == (date(2026, 11, 2), date(2026, 11, 6))
    assert earnings.next_week(date(2026, 11, 2)) == (date(2026, 11, 9), date(2026, 11, 13))


def test_agents_skip_buys_before_earnings(monkeypatch):
    monkeypatch.setattr(earnings.yf, "Ticker", FakeTicker)
    mkt = agents.Market(hist={}, fx=None, stocks={}, etfs={}, as_of=TODAY, sessions=[], zone="Neutra", stage2_share=0.3)
    orders = [agents.order_buy("AAPL", 100, "x"), agents.order_buy("MSFT", 100, "x"),
              agents.order_buy("SXR8.DE", 100, "x"), agents.order_sell("AAPL", "y")]
    kept, skipped = agents.skip_earnings(orders, mkt)
    assert skipped == ["AAPL"]
    assert [(o["side"], o["symbol"]) for o in kept] == [("buy", "MSFT"), ("buy", "SXR8.DE"), ("sell", "AAPL")]
