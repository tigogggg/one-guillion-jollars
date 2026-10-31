import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import discord_bot  # noqa: E402


class Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.text = data, status, json.dumps(data)
        self.content = self.text.encode()

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


def test_create_channels_is_idempotent_and_read_only(monkeypatch, tmp_path):
    import discord_post
    import discord_setup

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/x")
    monkeypatch.setattr(discord_setup, "CHANNELS_PATH", tmp_path / "channels.json")
    guild_channels = [{"id": "500", "type": 0, "name": "billions"}]
    calls = []

    def fake_request(method, url, **kw):
        path = url.split("/api/v10")[1]
        calls.append((method, path, kw.get("json")))
        if method == "GET" and path == "/users/@me":
            return Resp({"id": "BOT"})
        if method == "GET" and path.endswith("/channels"):
            return Resp(guild_channels)
        if method == "POST" and path == "/guilds/G/channels":
            new = {"id": str(600 + len(guild_channels)), "type": kw["json"]["type"], "name": kw["json"]["name"]}
            guild_channels.append(new)
            return Resp(new)
        return Resp({"id": "M"})
    monkeypatch.setattr(discord_setup.requests, "request", fake_request)
    monkeypatch.setattr(discord_setup.requests, "get", lambda url, **kw: Resp({"guild_id": "G", "channel_id": "500"}))

    assert discord_setup.create_channels() == 0
    created = [c for c in calls if c[0] == "POST" and c[1] == "/guilds/G/channels"]
    assert [c[2]["name"] for c in created] == ["📈 FINANÇAS", "📊-mercado", "🚨-alertas-e-notícias", "🤖-agentes"]
    everyone = created[1][2]["permission_overwrites"][0]
    assert everyone["id"] == "G" and int(everyone["deny"]) & discord_setup.SEND
    category_id = guild_channels[1]["id"]  # the first channel created is the category
    assert ("PATCH", "/channels/500", {"parent_id": category_id, "lock_permissions": False}) in calls
    saved = json.loads((tmp_path / "channels.json").read_text())
    assert saved["channels"]["discussao"] == "500" and set(saved["channels"]) == {"mercado", "alertas", "agentes", "discussao"}

    calls.clear()
    assert discord_setup.create_channels() == 0  # second run reuses everything
    assert not [c for c in calls if c[0] == "POST" and c[1] == "/guilds/G/channels"]

    monkeypatch.setattr(discord_post, "CHANNELS_PATH", tmp_path / "channels.json")
    assert discord_post.route("alertas")["url"].endswith(f"/channels/{saved['channels']['alertas']}/messages")
    monkeypatch.delenv("DISCORD_BOT_TOKEN")
    assert discord_post.route("alertas") == {}


def test_test_channel_is_created_once_and_takes_every_post(monkeypatch, tmp_path):
    import discord_post
    import discord_setup
    import pytest

    path = tmp_path / "channels.json"
    path.write_text(json.dumps({"guild_id": "G", "category_id": "C", "channels": {"agentes": "601"}}))
    monkeypatch.setattr(discord_setup, "CHANNELS_PATH", path)
    monkeypatch.setattr(discord_post, "CHANNELS_PATH", path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    guild_channels = [{"id": "601", "type": 0, "name": "🤖-agentes"}]
    calls = []

    def fake_request(method, url, **kw):
        path_ = url.split("/api/v10")[1]
        calls.append((method, path_))
        if path_ == "/users/@me":
            return Resp({"id": "BOT"})
        if method == "GET":
            return Resp(guild_channels)
        if path_ == "/guilds/G/channels":
            guild_channels.append({"id": "700", "type": 0, "name": kw["json"]["name"]})
            assert kw["json"]["parent_id"] == "C"
            return Resp(guild_channels[-1])
        return Resp({"id": "M"})
    monkeypatch.setattr(discord_setup.requests, "request", fake_request)

    assert discord_setup.create_test_channel() == 0
    assert discord_setup.create_test_channel() == 0
    assert [c for c in calls if c == ("POST", "/guilds/G/channels")] == [("POST", "/guilds/G/channels")]
    assert json.loads(path.read_text())["channels"] == {"agentes": "601", "testes": "700"}

    assert discord_post.route("agentes")["url"].endswith("/channels/601/messages")
    monkeypatch.setenv("DISCORD_TESTES", "1")
    assert discord_post.route("agentes")["url"].endswith("/channels/700/messages")
    path.write_text(json.dumps({"guild_id": "G", "category_id": "C", "channels": {"agentes": "601"}}))
    with pytest.raises(RuntimeError):  # never fall back to the real channel
        discord_post.route("agentes")
