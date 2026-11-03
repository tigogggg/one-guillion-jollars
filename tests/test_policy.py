import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import policy  # noqa: E402


def months(n, start="2008-01-31"):
    return pd.date_range(start, periods=n, freq="ME")


def test_public_spending_is_a_cross_sector_surprise_known_three_months_later():
    idx = months(60)
    base = pd.Series(100.0, index=idx)
    boom = base.copy()
    boom.iloc[36:] = 200.0  # spending doubles from month 37
    sig = policy.gov_signal(pd.DataFrame({"sec_A": base, "sec_B": base * 1.1, "sec_C": boom}))
    assert sig.index[0] == idx[0] + pd.offsets.MonthEnd(policy.GOV_LAG_MONTHS)  # known only 3 months after
    after = sig.loc[idx[40] + pd.offsets.MonthEnd(3)]
    assert after["sec_C"] > 1 and after["sec_A"] < 0
    assert np.nanmean(after) == pytest.approx(0, abs=1e-9)


def test_the_most_exposed_to_policy_uncertainty_score_lowest():
    rng = np.random.default_rng(1)
    idx = months(90, "2001-01-31")
    epu = pd.DataFrame({c: np.exp(np.cumsum(rng.normal(0, 0.1, len(idx)))) for c in policy.EPU_CATEGORIES}, index=idx)
    d = np.log(epu).diff().mean(axis=1).fillna(0)
    spy = pd.Series(rng.normal(0.005, 0.03, len(idx)), index=idx)
    noise = lambda: rng.normal(0, 0.01, len(idx))  # noqa: E731
    etfs = {"sec_A": spy + 0.5 * d + noise(), "sec_B": spy + noise(), "sec_C": spy - 0.5 * d + noise()}
    sig = policy.epu_signal(epu, etfs, spy)
    last = sig.iloc[-1]
    assert last["sec_C"] > last["sec_B"] > last["sec_A"]
    assert sig.index[0] == idx[policy.BETA_MONTHS - 1] + pd.offsets.MonthEnd(policy.EPU_LAG_MONTHS)


def test_capex_growth_uses_only_the_filings_known_that_day():
    fund = pd.DataFrame({
        "symbol": ["X", "X", "Y", "Y"],
        "end": pd.to_datetime(["2019-12-31", "2020-12-31", "2019-12-31", "2020-12-31"]),
        "available": pd.to_datetime(["2020-02-15", "2021-02-15", "2020-02-15", "2021-02-15"]),
        "capex": [100.0, 150.0, 50.0, 50.0],
    })
    out = policy.capex_growth(fund, {"X": "Energy", "Y": "Energy"})
    assert np.isnan(out.loc["2021-01-31", "sec_XLE"])  # the 2020 filings not out yet
    assert out.loc["2021-02-28", "sec_XLE"] == pytest.approx(200 / 150 - 1)


def test_federal_spending_is_asked_year_by_year_and_kept(monkeypatch, tmp_path):
    calls = []

    class Resp:
        status_code = 200

        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            pass

        def json(self):
            first = pd.Timestamp(self.body["filters"]["time_period"][0]["start_date"])
            fy = first.year + 1
            return {"results": [{"time_period": {"fiscal_year": str(fy), "month": str(m)}, "aggregated_amount": 10.0 * m}
                                for m in range(1, 13)]}

    def post(url, json, timeout):
        calls.append(json)
        return Resp(json)

    monkeypatch.setattr(policy.requests, "post", post)
    monkeypatch.setattr(policy, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(policy, "NAICS", {"sec_XLU": ["2211"]})
    gov = policy.download_spending()
    assert gov.index[0] == pd.Timestamp("2007-10-31") and gov.loc["2007-10-31", "sec_XLU"] == 10.0
    assert gov.loc["2008-09-30", "sec_XLU"] == 120.0  # month 12 of FY2008 is September 2008
    gov.to_csv(tmp_path / "gasto_publico.csv")
    n = len(calls)
    policy.download_spending()
    assert len(calls) - n == 2  # cached: only the last two fiscal years asked again


def test_the_uncertainty_categories_are_read_from_the_authors_files(monkeypatch):
    import io

    def xlsx(frame):
        buf = io.BytesIO()
        frame.to_excel(buf, index=False)
        return buf.getvalue()

    cat = pd.DataFrame({"Year": [2020, 2020, None], "Month": [1, 2, None], "1. Economic Policy Uncertainty": [100, 110, None],
                        "4. Government spending": [80, 90, None], "8. Regulation": [50, 55, None],
                        "Financial Regulation": [10, 11, None], "6. National security": [70, 60, None]})
    eu = pd.DataFrame({"Year": [2020, 2020], "Month": [1, 2], "European_News_Index": [200.0, 210.0]})

    class Resp:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            pass

    monkeypatch.setattr(policy.requests, "get", lambda url, timeout, headers: Resp(xlsx(cat if "Categorical" in url else eu)))
    out = policy.epu_site()
    assert out["EPUGOVTSPEND"].tolist() == [80, 90] and out["EPUREG"].tolist() == [50, 55]
    assert out["EUEPUINDXM"].index[1] == pd.Timestamp("2020-02-29") and "EPUTRADE" not in out


def test_naics_prefixes_are_two_four_or_six_digits():
    out = policy.naics_filter(["23", "324", "3364", "5413"])
    assert "23" in out and "3241" in out and "324" not in out and "3364" in out
    assert all(len(c) in (2, 4, 6) for codes in policy.NAICS.values() for c in policy.naics_filter(codes))
