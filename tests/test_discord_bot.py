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


def test_mirror_server_gets_the_same_channels_and_a_copy_of_each_post(monkeypatch, tmp_path):
    import discord_post
    import discord_setup

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    monkeypatch.delenv("DISCORD_TESTES", raising=False)
    mirror_path = tmp_path / "mirror.json"
    monkeypatch.setattr(discord_setup, "MIRROR_PATH", mirror_path)
    monkeypatch.setattr(discord_post, "MIRROR_PATH", mirror_path)
    monkeypatch.setattr(discord_post, "CHANNELS_PATH", tmp_path / "channels.json")
    (tmp_path / "channels.json").write_text(json.dumps({"channels": {"alertas": "11"}}))
    guild_channels = [{"id": "900", "type": 0, "name": "general"}]
    created, posted = [], []

    def fake_request(method, url, **kw):
        path = url.split("/api/v10")[1]
        if path == "/users/@me":
            return Resp({"id": "BOT"})
        if method == "GET":
            return Resp(guild_channels)
        if path == "/guilds/N/channels":
            new = {"id": str(800 + len(guild_channels)), "type": kw["json"]["type"], "name": kw["json"]["name"]}
            guild_channels.append(new)
            created.append(kw["json"])
            return Resp(new)
        posted.append(path)
        return Resp({"id": "M", "channel_id": path.split("/")[2]})
    monkeypatch.setattr(discord_setup.requests, "request", fake_request)
    monkeypatch.setattr(discord_post.requests, "request", fake_request)

    assert discord_setup.create_mirror_channels("N") == 0
    assert [c["name"] for c in created] == ["📈 FINANÇAS", "billions", "📊-mercado", "🚨-alertas-e-notícias", "🤖-agentes", "🧪-testes"]
    assert "permission_overwrites" not in created[1]  # people can talk in #billions
    assert all(int(c["permission_overwrites"][0]["deny"]) & discord_setup.SEND for c in created[2:])
    ids = json.loads(mirror_path.read_text())["channels"]
    assert set(ids) == {"discussao", "mercado", "alertas", "agentes", "testes"}

    created.clear()
    posted.clear()
    assert discord_setup.create_mirror_channels("N") == 0 and not created  # second run reuses everything

    posted.clear()
    message = discord_post.post_message([{"title": "x"}], **discord_post.route("alertas"))
    assert message["channel_id"] == "11"  # callers keep the original server's message
    assert posted == ["/channels/11/messages", f"/channels/{ids['alertas']}/messages"]

    posted.clear()
    discord_post.post_message([{"title": "dm"}], url=f"{discord_post.API}/channels/55/messages")
    assert posted == ["/channels/55/messages"]  # DMs and replies are not copied


def test_delete_older_keeps_the_new_post_pins_and_other_titles(monkeypatch):
    import discord_post

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    bot = {"id": "bot"}
    history = [
        {"id": "9", "author": bot, "embeds": [{"title": "🤖 Liga dos agentes · fecho de 2026-11-03"}]},  # the new one
        {"id": "8", "author": bot, "embeds": [{"title": "🤖 Liga dos agentes · fecho de 2026-11-02"}]},
        {"id": "7", "author": bot, "embeds": [{"title": "📆 Relatório mensal dos agentes · 2026-10"}]},
        {"id": "6", "author": bot, "pinned": True, "embeds": [{"title": "🤖 Liga dos agentes · fixada"}]},
        {"id": "5", "author": {"id": "someone"}, "embeds": [{"title": "🤖 Liga dos agentes · de alguém"}]},
        {"id": "4", "author": bot, "embeds": [{"title": "🤖 Liga dos agentes · fecho de 2026-10-30"}]},
    ]
    deleted = []

    class Resp:
        def __init__(self, status, data=None):
            self.status_code, self._data = status, data

        def json(self):
            return self._data

        def raise_for_status(self):
            pass

    monkeypatch.setattr(discord_post.requests, "get", lambda url, **kw: Resp(200, history))
    monkeypatch.setattr(discord_post.requests, "delete", lambda url, **kw: deleted.append(url.rsplit("/", 1)[1]) or Resp(204))
    new = {"id": "9", "channel_id": "c", "author": bot}
    assert discord_post.delete_older(new, "🤖 Liga dos agentes") == 2
    assert deleted == ["8", "4"]


def test_portal_card_mentions_every_channel_with_the_schedule():
    import discord_setup
    import limits

    channels = {"mercado": "1", "alertas": "2", "agentes": "3", "estado": "4", "discussao": "5"}
    card = discord_setup.portal_card("https://w/p/x", limits.DEFAULT_SCHEDULE, channels)
    where = card["fields"][0]["value"]
    assert all(f"<#{i}>" in where for i in "12345")
    assert "**07:00**" in where and "das 08:05 às 20:55, de 10 em 10 min" in where and "**12:00 · 16:00 · 20:30**" in where and "**23:00**" in where
    assert card["image"]["url"] == "attachment://portal-banner.png"


def test_fit_shortens_the_longest_texts_to_discords_limit():
    import discord_post

    big = {"title": "t", "description": "d" * 3000, "fields": [{"name": "a", "value": "x" * 1000}, {"name": "b", "value": "y" * 1024}] * 3}
    out = discord_post.fit([big])
    total = len(out[0]["description"]) + sum(len(f["name"]) + len(f["value"]) for f in out[0]["fields"]) + 1
    assert total <= discord_post.EMBED_TOTAL and out[0]["description"].endswith("…")
    assert len(big["description"]) == 3000  # the original is untouched
