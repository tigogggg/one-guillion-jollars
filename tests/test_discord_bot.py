import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import discord_bot  # noqa: E402


class Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.text = data, status, json.dumps(data)

    def json(self):
        return self._data


def test_votes_exclude_the_bots_own_reactions(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    msg = {"reactions": [{"emoji": {"name": "👍"}, "count": 4, "me": True}, {"emoji": {"name": "👎"}, "count": 1, "me": True},
                         {"emoji": {"name": "🔥"}, "count": 2, "me": False}]}
    monkeypatch.setattr(discord_bot.requests, "request", lambda *a, **k: Resp(msg))
    assert discord_bot.count_votes("c", "m") == {"👍": 3, "👎": 0}


def test_no_token_means_no_calls(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setattr(discord_bot.requests, "request", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert discord_bot.count_votes("c", "m") is None
    assert discord_bot.start_daily_thread({"id": "1", "channel_id": "2"}, "x", date(2026, 11, 2)) is None


def test_daily_thread_is_remembered_for_the_day(monkeypatch, tmp_path):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    monkeypatch.setattr(discord_bot, "THREAD_PATH", tmp_path / "thread.json")
    monkeypatch.setattr(discord_bot.requests, "request", lambda *a, **k: Resp({"id": "999"}))
    assert discord_bot.start_daily_thread({"id": "1", "channel_id": "2"}, "📅 seg 2/11", date(2026, 11, 2)) == "999"
    assert discord_bot.todays_thread(date(2026, 11, 2)) == "999"
    assert discord_bot.todays_thread(date(2026, 11, 3)) is None


def test_load_follows_reads_kv(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")

    def fake_get(url, **kw):
        if url.endswith("/namespaces"):
            return Resp({"result": [{"title": "other", "id": "x"}, {"title": "coutotrading-config", "id": "ns"}]})
        if url.endswith("/ns/keys"):
            assert kw["params"]["prefix"] == "follows:"
            return Resp({"result": [{"name": "follows:42"}]})
        assert "/ns/values/follows%3A42" in url
        return Resp(["NVDA", "KDP"])
    monkeypatch.setattr(discord_bot.requests, "get", fake_get)
    assert discord_bot.load_follows() == {"42": ["NVDA", "KDP"]}
