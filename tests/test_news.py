import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import news  # noqa: E402

NOW = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)


def raw(i, title, hours_ago):
    return {"id": f"n{i}", "content": {"title": title, "summary": "s", "pubDate": (NOW - timedelta(hours=hours_ago)).isoformat(),
                                       "provider": {"displayName": "Reuters"}, "canonicalUrl": {"url": f"https://x/{i}"}}}


def test_headlines_parse_and_drop_old(monkeypatch):
    class T:
        def __init__(self, s):
            pass

        def get_news(self, count):
            return [raw(1, "NVDA beats", 2), raw(2, "Old story", 50), {"ad": [1]}]
    monkeypatch.setattr(news.yf, "Ticker", T)
    items = news.headlines("NVDA", now=NOW)
    assert [i["title"] for i in items] == ["NVDA beats"] and items[0]["publisher"] == "Reuters"


def test_claude_output_is_parsed(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setattr(news.shutil, "which", lambda _: "/usr/bin/claude")
    reply = 'Aqui está:\n[{"id": "n1", "sentimento": 2, "importancia": 3, "surpresa": true, "resumo": "Bateu estimativas"}]'

    class P:
        returncode, stderr = 0, ""
        stdout = json.dumps({"type": "result", "is_error": False, "result": reply})
    calls = []
    monkeypatch.setattr(news.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)) or P())
    ratings = news.analyse_claude([{"id": "n1", "symbol": "NVDA", "title": "NVDA beats", "summary": ""}])
    assert ratings == [{"id": "n1", "sentimento": 2, "importancia": 3, "surpresa": True, "resumo": "Bateu estimativas"}]
    assert calls[0][0][:2] == ["claude", "-p"] and "NVDA beats" in calls[0][1]["input"]


def test_claude_unavailable_without_token(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert news.analyse_claude([{"id": "x", "symbol": "A", "title": "t", "summary": ""}]) is None


def test_signals_sum_and_expire():
    old = {"id": "o", "symbol": "AAA", "sentimento": -2, "importancia": 3, "published": (NOW - timedelta(hours=40)).isoformat()}
    fresh = {"id": "f", "symbol": "AAA", "sentimento": 1, "importancia": 2, "published": (NOW - timedelta(hours=1)).isoformat()}
    signals = news.merge_signals({"AAA": {"items": [old]}}, [fresh, fresh], NOW)
    assert signals["AAA"]["score"] == 2 and signals["AAA"]["importance"] == 2  # old item expired, duplicate ignored


def test_digest_keeps_only_important_moves():
    base = {"symbol": "AAA", "publisher": "R", "url": "u", "surpresa": False}
    cards = news.digest([{**base, "id": "1", "sentimento": 2, "importancia": 3, "resumo": "Grande contrato"},
                         {**base, "id": "2", "sentimento": 0, "importancia": 3, "resumo": "Neutra"},
                         {**base, "id": "3", "sentimento": -1, "importancia": 1, "resumo": "Ruído"}], "Claude", NOW)
    assert "Grande contrato" in cards[0]["description"] and "Neutra" not in cards[0]["description"]
    assert "Ruído" not in cards[0]["description"]
