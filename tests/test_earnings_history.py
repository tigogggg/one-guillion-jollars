import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import agents  # noqa: E402
import earnings  # noqa: E402


def test_only_8k_item_202_since_2004_are_earnings_releases():
    filings = {
        "form": ["8-K", "8-K", "10-Q", "8-K", "8-K/A", "8-K"],
        "filingDate": ["2026-07-30", "2026-06-02", "2026-07-31", "2004-05-01", "2026-08-01", "2026-04-30"],
        "items": ["2.02,9.01", "5.02", "", "2.02", "2.02", "7.01,2.02"],
    }
    assert earnings.report_dates(filings) == {"2026-07-30", "2026-04-30"}


def test_the_history_is_saved_and_gives_the_next_date(tmp_path):
    path = tmp_path / "dates.csv.gz"
    earnings.save_history({"AAPL": ["2026-01-29", "2026-04-30", "2026-07-30"]}, path)
    dates = earnings.load_history(path)["AAPL"]
    assert earnings.next_from_history(dates, date(2026, 4, 28)) == date(2026, 4, 30)
    assert earnings.next_from_history(dates, date(2026, 4, 30)) == date(2026, 4, 30)
    assert earnings.next_from_history(dates, date(2026, 8, 1)) is None
    assert earnings.next_from_history(None, date(2026, 8, 1)) is None


def test_the_backtest_skips_a_buy_right_before_the_results_without_asking_yahoo(monkeypatch):
    monkeypatch.setattr(earnings, "next_earnings", lambda *a: (_ for _ in ()).throw(AssertionError("no live lookup")))
    mkt = SimpleNamespace(as_of=date(2026, 4, 27), etfs={}, earnings={},
                          earnings_dates={"AAPL": [date(2026, 4, 30)], "MSFT": [date(2026, 7, 28)]})
    orders = [{"side": "buy", "symbol": "AAPL"}, {"side": "buy", "symbol": "MSFT"}, {"side": "buy", "symbol": "SAP.DE"}]
    kept, skipped = agents.skip_earnings(orders, mkt, 5)
    assert skipped == ["AAPL"] and [o["symbol"] for o in kept] == ["MSFT", "SAP.DE"]
