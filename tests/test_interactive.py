import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import charts  # noqa: E402
import interactive  # noqa: E402


def series(**ends):
    return {name: [("2026-10-28", 0.0), ("2026-10-30", end)] for name, end in ends.items()}


def test_leaders_are_the_top_five_plus_the_benchmark():
    s = series(A=9, B=8, C=7, D=6, E=5, F=4, Indexador=1)
    assert interactive.leaders(s) == {"A", "B", "C", "D", "E", "Indexador"}


def test_publish_without_cloudflare_returns_none(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    assert interactive.publish("t", series(A=1)) is None


def test_lines_chart_with_highlight_draws():
    png = charts.lines_chart(series(A=3, B=2, C=1), "t", highlight={"A"})
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_buttons_go_out_only_with_the_bot_or_an_interaction(monkeypatch):
    import discord_post

    sent = []

    class Resp:
        status_code = 200

        def json(self):
            return {}

    monkeypatch.setattr(discord_post.requests, "request", lambda m, url, **kw: sent.append(kw.get("json")) or Resp())
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/x")
    buttons = [("📈 Ver", "https://w/g/abc")]
    discord_post.post_message([{"title": "t"}], buttons=buttons)  # plain webhook: no components
    discord_post.post_message([{"title": "t"}], url="https://discord.com/api/v10/channels/1/messages",
                              headers={"Authorization": "Bot x"}, buttons=buttons)
    assert "components" not in sent[0]
    assert sent[1]["components"][0]["components"][0] == {"type": 2, "style": 5, "label": "📈 Ver", "url": "https://w/g/abc"}
