import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import agents  # noqa: E402


def ny(h, m):  # 2 Nov 2026, New York is UTC-5
    return datetime(2026, 11, 2, h + 5, m, tzinfo=timezone.utc)


def test_reactive_buys_wait_out_the_first_and_last_minutes_of_the_session():
    assert agents.buy_window("AAPL", ny(9, 40), {}) and "abertura" in agents.buy_window("AAPL", ny(9, 40), {})
    assert agents.buy_window("AAPL", ny(10, 0), {}) is None
    assert "últimos" in agents.buy_window("AAPL", ny(15, 40), {})
    assert agents.buy_window("AAPL", ny(15, 40), {"ignora_risco": True}) is None
    # Frankfurt opens at 9:00 Berlin (8:00 UTC in November)
    assert agents.buy_window("SAP.DE", datetime(2026, 11, 2, 8, 5, tzinfo=timezone.utc), {})
    assert agents.buy_window("SAP.DE", datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc), {}) is None


def test_an_agent_down_3_percent_today_stops_buying(monkeypatch):
    mkt = SimpleNamespace(as_of=date(2026, 11, 2))
    port = {"history": [{"date": "2026-10-30", "equity": 1000.0}]}
    monkeypatch.setattr(agents, "equity", lambda p, m: 965.0)
    assert "3%" in agents.round_buy_block(port, mkt, {})
    assert agents.round_buy_block(port, mkt, {"ignora_risco": True}) is None
    monkeypatch.setattr(agents, "equity", lambda p, m: 980.0)
    assert agents.round_buy_block(port, mkt, {}) is None
    port["history"].append({"date": "2026-11-02", "equity": 960.0})  # today's entry already written: compare with the day before
    monkeypatch.setattr(agents, "equity", lambda p, m: 965.0)
    assert agents.round_buy_block(port, mkt, {})
