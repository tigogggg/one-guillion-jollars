import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ensemble  # noqa: E402


def curve(yearly: list[float]) -> list:
    out, v = [], 1.0
    for i, r in enumerate(yearly):
        for w in range(52):
            v *= (1 + r) ** (1 / 52)
            out.append(((pd.Timestamp(2010 + i, 1, 1) + pd.Timedelta(weeks=w)).date().isoformat(), (v - 1) * 100))
    return out


def test_the_group_and_the_leaders_are_compared_with_the_index_year_by_year():
    report = {"strategies": {
        "indexador": {"curve": curve([0.10, 0.10, 0.10, 0.10])},
        "a": {"curve": curve([0.30, -0.10, 0.30, -0.10])},
        "b": {"curve": curve([-0.10, 0.30, -0.10, 0.30])},
        "c": {"curve": curve([0.05, 0.05, 0.05, 0.05])},
    }}
    a = ensemble.analyse(report)
    assert a["strategies"] == 3 and a["mean_correlation"] < 0.5
    assert a["alone"]["c"]["beats_index"] == 0 and a["best_alone"]["strategy"] in ("a", "b")
    assert len(ensemble.summary_lines(a)) == 5
