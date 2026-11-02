import gzip
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import market_archive  # noqa: E402
from agents import Market  # noqa: E402


def test_news_and_decisions_are_added_once(tmp_path, monkeypatch):
    monkeypatch.setattr(market_archive, "ARCHIVE", tmp_path)
    now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    item = {"id": "a1", "symbol": "AAPL", "title": "x", "sentimento": 1, "importancia": 2}
    assert market_archive.news([item], now) == 1 and market_archive.news([item], now) == 0
    config = {"agents": [{"id": "foguetao", "strategy": "foguetao"}]}
    trade = {"date": "2026-11-02", "hora": "10:15", "side": "compra", "symbol": "SAP.DE", "qty": 1.5, "price": 200.0}
    state = {"agents": {"foguetao": {"trades": [trade], "cancelled": []}}}
    assert market_archive.decisions(config, state) == 1 and market_archive.decisions(config, state) == 0
    row = json.loads((tmp_path / "decisoes" / "2026-11.jsonl").read_text().splitlines()[0])
    assert row["mode"] == "reativo" and row["hora"] == "10:15"


def test_closes_keep_only_the_decision_day(tmp_path, monkeypatch):
    monkeypatch.setattr(market_archive, "ARCHIVE", tmp_path)
    idx = pd.to_datetime(["2026-10-29", "2026-10-30"])
    bar = pd.DataFrame({"Open": [1, 2], "High": [1, 2], "Low": [1, 2], "Close": [1, 2], "Volume": [5, 6]}, index=idx)
    stale = bar.iloc[:1]
    fx = pd.Series([1.1, 1.2], index=idx)
    mkt = Market(hist={"AAA": bar, "OLD": stale}, fx=fx, stocks={}, etfs={}, as_of=date(2026, 10, 30), sessions=[],
                 zone="Neutra", stage2_share=0.3, earnings={"AAA": date(2026, 11, 5), "BBB": None})
    assert market_archive.closes(mkt) == 2  # AAA and EUR/USD; OLD has no bar that day
    table = pd.read_csv(tmp_path / "fechos" / "2026-10-30.csv.gz")
    assert set(table["symbol"]) == {"AAA", "EURUSD"} and table.loc[table.symbol == "AAA", "close"].item() == 2
    assert market_archive.earnings(mkt) == 2
    assert json.loads((tmp_path / "resultados" / "2026-10-30.json").read_text())["AAA"] == "2026-11-05"


def test_each_round_saves_the_markets_in_session(tmp_path, monkeypatch):
    monkeypatch.setattr(market_archive, "INTRADAY", tmp_path)
    idx = pd.to_datetime(["2026-10-29", "2026-10-30"])
    bar = pd.DataFrame({"Open": [1, 2], "High": [1, 3], "Low": [1, 1.5], "Close": [1, 2.5], "Volume": [5, 1000]}, index=idx)
    mkt = Market(hist={"SAP.DE": bar, "AAPL": bar}, fx=pd.Series([1.1], index=idx[:1]), stocks={}, etfs={},
                 as_of=date(2026, 10, 30), sessions=[], zone="Neutra", stage2_share=0.3)
    now = datetime(2026, 10, 30, 10, 30, tzinfo=timezone.utc)  # Xetra open, the US not yet
    assert market_archive.intraday(mkt, now) == 1
    table = pd.read_csv(tmp_path / "2026" / "10-30" / "1030.csv.gz")
    assert list(table["symbol"]) == ["SAP.DE"] and table["price"].item() == 2.5
