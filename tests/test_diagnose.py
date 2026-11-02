import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import agents  # noqa: E402
import diagnose  # noqa: E402


def m(ret):
    return {"price": 10, "sma50": 9, "sma200": 8, "sma200_rising": True, "pct_from_high": 0.05, "pct_above_low": 0.5,
            "ret_3m": ret, "ret_6m": ret, "stage2": True}


def test_us_only_drops_european_stocks_and_ranks_again():
    stocks = {"AAPL": m(0.1), "SAP.DE": m(0.5), "MSFT": m(0.3)}
    agents.add_trend_scores(stocks)
    mkt = agents.Market(hist={}, fx=pd.Series([1.1]), stocks=stocks, etfs={}, as_of=date(2026, 1, 2), sessions=[],
                        zone="Neutra", stage2_share=0.4)
    out = diagnose.us_only([mkt])[0]
    assert set(out.stocks) == {"AAPL", "MSFT"}
    assert out.stocks["MSFT"]["trend_score"] == 100.0 and out.stocks["AAPL"]["trend_score"] == 70.0
