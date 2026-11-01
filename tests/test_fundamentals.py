import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fundamentals as F  # noqa: E402


def fact(start, end, val, filed, form="10-Q"):
    e = {"end": end, "val": val, "filed": filed, "form": form}
    if start:
        e["start"] = start
    return e


def facts():
    usd = lambda xs: {"units": {"USD": xs}}  # noqa: E731
    return {"facts": {
        "us-gaap": {
            "Revenues": usd([
                fact("2021-01-01", "2021-12-31", 360, "2022-02-15", "10-K"),
                fact("2022-01-01", "2022-12-31", 400, "2023-02-15", "10-K"),
                fact("2022-01-01", "2022-03-31", 100, "2022-05-01"),
                fact("2022-01-01", "2022-03-31", 100, "2023-05-01"),   # comparative, filed again later
                fact("2023-01-01", "2023-03-31", 110, "2023-05-01"),
                fact("2022-01-01", "2022-06-30", 210, "2022-08-01"),
                fact("2023-01-01", "2023-06-30", 230, "2023-08-01"),
            ]),
            "OperatingIncomeLoss": usd([fact("2022-01-01", "2022-12-31", 80, "2023-02-15", "10-K")]),
            "NetIncomeLoss": usd([fact("2022-01-01", "2022-12-31", 60, "2023-02-15", "10-K")]),
            "NetCashProvidedByUsedInOperatingActivities": usd([fact("2022-01-01", "2022-12-31", 90, "2023-02-15", "10-K")]),
            "PaymentsToAcquirePropertyPlantAndEquipment": usd([fact("2022-01-01", "2022-12-31", 20, "2023-02-15", "10-K")]),
            "AssetsCurrent": usd([fact(None, "2022-12-31", 300, "2023-02-15", "10-K")]),
            "LiabilitiesCurrent": usd([fact(None, "2022-12-31", 150, "2023-02-15", "10-K")]),
            "CashAndCashEquivalentsAtCarryingValue": usd([fact(None, "2022-12-31", 100, "2023-02-15", "10-K")]),
            "LongTermDebt": usd([fact(None, "2022-12-31", 50, "2023-02-15", "10-K")]),
        },
        "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [fact(None, "2023-02-01", 10, "2023-02-15", "10-K")]}}},
    }}


def test_ttm_from_year_to_date_facts():
    rows = F.with_growth(F.company_rows("ACME", facts())).set_index("end")
    assert rows.loc["2022-12-31", "revenue"] == 400
    assert rows.loc["2023-03-31", "revenue"] == 410  # 110 + 400 - 100
    assert rows.loc["2023-06-30", "revenue"] == 420  # 230 + 400 - 210
    assert abs(rows.loc["2022-12-31", "rev_growth"] - (400 / 360 - 1)) < 1e-9
    assert rows.loc["2023-03-31", "available"] == pd.Timestamp("2023-05-01")


def test_daily_uses_only_filed_numbers_and_scores():
    rows = F.with_growth(F.company_rows("ACME", facts())).set_index("available").sort_index()
    days = pd.bdate_range("2023-02-01", "2023-03-01")
    d = F.daily(rows, pd.Series(50.0, index=days))
    assert d.loc[:"2023-02-14", "fund_q"].isna().all()       # 2022 annual report not filed yet
    after = d.loc["2023-02-15":].iloc[0]
    assert 0 <= after["fund_q"] <= 100 and np.isfinite(after["fund_v"])
    assert abs(after["earn_yield"] - 60 / 500) < 1e-9          # net income / (50 x 10 shares)
    assert abs(after["pe"] - 500 / 60) < 1e-6


def test_dcf_and_missing_data():
    assert F.intrinsic_per_share(-5, 10, 0.1) is None
    v_slow, v_fast = F.intrinsic_per_share(10, 10, 0.0), F.intrinsic_per_share(10, 10, 0.15)
    assert v_fast > v_slow > 10
    assert F.latest(None, 10.0, date(2024, 1, 1)) == {}
