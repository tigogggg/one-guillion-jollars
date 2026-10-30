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
                         {**base, "id": "3", "sentimento": -1, "importancia": 1, "resumo": "Ruído"}], "Claude", NOW,
                        {"AAA": "Alpha Widgets Inc."})
    assert "**AAA** (Alpha Widgets) Grande contrato" in cards[0]["description"]
    assert "Neutra" not in cards[0]["description"]
    assert "Ruído" not in cards[0]["description"]


YAHOO_RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Yahoo! Finance: NVDA News</title>
<item><title>Nvidia beats estimates &amp; raises guidance</title><link>https://finance.yahoo.com/news/a.html</link>
<description>&lt;p&gt;Revenue up 60%&lt;/p&gt;</description><pubDate>Mon, 02 Nov 2026 13:05:00 +0000</pubDate></item>
<item><title>Old</title><link>https://x</link><pubDate>Mon, 26 Oct 2026 13:05:00 +0000</pubDate></item>
</channel></rss>"""
GOOGLE_RSS = """<rss version="2.0"><channel>
<item><title>Everpure surges after earnings - Reuters</title><link>https://news.google.com/rss/articles/x</link>
<pubDate>Mon, 02 Nov 2026 12:00:00 GMT</pubDate><source url="https://www.reuters.com">Reuters</source></item>
</channel></rss>"""


def test_rss_parsing_yahoo_and_google():
    y = news.rss_items(YAHOO_RSS, "NVDA", "Yahoo")
    assert y[0]["title"] == "Nvidia beats estimates & raises guidance" and y[0]["summary"] == "Revenue up 60%"
    g = news.rss_items(GOOGLE_RSS, "P", "Google")
    assert g[0]["title"] == "Everpure surges after earnings" and g[0]["publisher"] == "Reuters"
    assert news.rss_items("not xml", "X", "Yahoo") == []


def test_headlines_fall_back_to_rss_then_google(monkeypatch):
    monkeypatch.setattr(news, "yahoo_api", lambda s, c: [])
    queries = []

    def fake_fetch(url, params):
        queries.append(params)
        return "" if url == news.YAHOO_RSS else GOOGLE_RSS
    monkeypatch.setattr(news, "fetch_rss", fake_fetch)
    items = news.headlines("P", now=NOW, name="Everpure Inc.")
    assert [i["title"] for i in items] == ["Everpure surges after earnings"]
    assert queries[1]["q"] == '"Everpure" when:2d'
    assert news.short_name("Alphabet Inc. (Class A)") == "Alphabet"
