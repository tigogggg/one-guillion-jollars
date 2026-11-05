import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import repair_european_days as rep  # noqa: E402


class FakeCloses:
    """SXR8.DE: 100 on the 3rd, 110 on the 4th; AAPL (US) never lagged."""
    prices = {"SXR8.DE": {"2026-11-03": 100.0, "2026-11-04": 110.0}}

    def on(self, sym, day):
        return self.prices.get(sym, {}).get(day.isoformat())

    def before(self, sym, day):
        return {"2026-11-04": 100.0, "2026-11-03": 90.0}.get(day.isoformat()) if sym in self.prices else None

    def eur(self, sym, price, day):
        return price


def test_holdings_undo_the_later_trades():
    port = {"positions": {"A.DE": {"qty": 3}}, "trades": [
        {"date": "2026-11-03", "side": "compra", "symbol": "A.DE", "qty": 1},
        {"date": "2026-11-05", "side": "compra", "symbol": "A.DE", "qty": 2},
        {"date": "2026-11-05", "side": "venda", "symbol": "B.DE", "qty": 4}]}
    assert rep.holdings_at(port, "2026-11-04") == {"A.DE": 1.0, "B.DE": 4.0}


def test_a_lagged_point_moves_to_its_own_close_and_its_points_are_redone():
    port = {"positions": {"SXR8.DE": {"qty": 10}}, "trades": [], "points": 5, "peak": 1.0, "max_dd": 0.0,
            "zone_stats": {"Neutra": [0.0, 2]},
            "history": [{"date": "2026-11-03", "equity": 1000.0, "deposits": 1000},
                        {"date": "2026-11-04", "equity": 1000.0, "deposits": 1000}],  # recorded at 100 (the 3rd's close)
            "points_log": [{"date": "2026-11-04", "pts": 0}]}
    plan = {"dates": ["2026-11-04"], "present": {"2026-11-04": []}}
    out = rep.repair_port(port, plan, FakeCloses(), {"2026-11-04": "Neutra"})
    assert out["equity"] == {"2026-11-04": 100.0} and port["history"][1]["equity"] == 1100.0
    # the agent +10% and the index +10% on the day: no excess either way; still above its deposits: the profit bonus
    assert port["points_log"][0]["pts"] == 2 and port["points"] == 7 and out["points"] == {"2026-11-04": 2}
    assert abs(port["zone_stats"]["Neutra"][0] - 0.0) < 1e-9  # old: 0% vs the index's lagged +11.1%; new: 10% vs 10%
