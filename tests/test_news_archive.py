import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import news_archive  # noqa: E402

NOW = datetime(2026, 11, 3, 20, 0, tzinfo=timezone.utc)


def item(i, sym, hours, sent, imp=3):
    return {"id": str(i), "symbol": sym, "title": f"t{i}", "resumo": f"r{i}", "publisher": "p", "url": "",
            "published": (NOW - timedelta(hours=hours)).isoformat(), "sentimento": sent, "importancia": imp,
            "surpresa": False, "fonte": "Claude"}


def test_archive_keeps_30_days_and_dedupes():
    old = item(1, "AAA", 24 * 40, 1)
    a = news_archive.add([old], [item(2, "AAA", 2, 1), item(2, "AAA", 2, 1)], NOW)
    assert [i["id"] for i in a] == ["2"]


def test_reactions_are_net_of_the_market_and_the_hit_rate_follows_the_sentiment():
    days = pd.bdate_range("2026-10-20", periods=12)
    stock = pd.DataFrame({"Close": [100] * 5 + [105, 106, 107, 108, 109, 110, 111]}, index=days)
    spy = pd.DataFrame({"Close": [100] * 5 + [101] * 7}, index=days)
    news = {**item(1, "AAA", 0, 2), "published": datetime(2026, 10, 26, 15, tzinfo=timezone.utc).isoformat()}
    archive = [news]
    news_archive.reactions(archive, {"AAA": stock, "SPY": spy})
    assert archive[0]["r1"] == 4.0 and archive[0]["r5"] == 8.0
    assert news_archive.hit(archive) == 1.0


def test_build_groups_sectors_changes_and_timelines():
    archive = [item(1, "AAA", 2, 2), item(2, "BBB", 3, -2), item(3, "AAA", 30, -1, 1)]
    out = news_archive.build(archive, {"AAA": "Acme"}, {"AAA": "Tecnologia", "BBB": "Energia"}, {"AAA": 6}, NOW)
    assert [r["sector"] for r in out["sectors"]] == ["Tecnologia", "Energia"]
    moves = {m["symbol"]: m["delta"] for m in out["changes"]["moves"]}
    assert moves == {"AAA": 6 + 1, "BBB": -6}
    assert len(out["timeline"]["AAA"]) == 2 and out["items"][0]["company"] == "Acme"
