import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import series  # noqa: E402


def test_a_strategy_leaves_the_series_once_stable_and_benched_ones_do_not_train():
    a = {"x": {"n": 1}, "y": {"n": 2}, "z": {"n": 3}, "indexador": {}, "jornalista": None}
    b = {"x": {"n": 5}, "y": {"n": 2}, "z": {"n": 3}, "indexador": {}, "jornalista": None}
    todo, st = series.next_run(a, b, ["x", "y", "z", "indexador"], {}, benched={"z"})
    assert todo == ["x", "y"] and st["quiet"] == {"x": 0, "y": 1, "z": 1} and st["runs"] == 1
    todo, st = series.next_run(b, b, ["x", "y"], st, benched={"z"})
    assert todo == ["x"]  # y unchanged twice in a row: stable
    todo, st = series.next_run(b, b, ["x"], st, benched=set())
    assert todo == ["z"]  # x stable now; z back in the league trains again until stable
