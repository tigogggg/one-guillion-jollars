import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import updates  # noqa: E402


def test_card_has_sections_and_skips_empty():
    c = updates.card({"titulo": "Login com Google", "resumo": "r", "novidades": ["a"], "correcoes": [], "acao": ["b"]}, 3)
    names = [f["name"] for f in c["fields"]]
    assert names == ["✨ Novidades", "👉 O que tens de fazer"]
    assert c["title"].startswith("📢 Login")


def test_bullets_stay_within_discord_limit():
    text = updates.bullets(["x" * 300] * 10)
    assert len(text) <= 1024 and text.endswith("…")


def test_commits_skip_bot_and_data_only(monkeypatch):
    log = ("\x1eaaa\x1ftigogggg\x1fNew portal tab\x1fbody\nCo-Authored-By: X\x1f\n\ndiscord-bot/chart_page.js\n"
           "\x1ebbb\x1fgithub-actions[bot]\x1fAgentes: liga\x1f\x1f\n\nagents/state.json\n"
           "\x1eccc\x1ftigogggg\x1fData refresh\x1f\x1f\n\ndata/context/epu.csv\n")
    monkeypatch.setattr(updates, "git", lambda *a: log)
    items = updates.commits(None, "1 day ago")
    assert [c["sha"] for c in items] == ["aaa"]
    assert items[0]["body"] == "body" and items[0]["files"] == ["discord-bot/chart_page.js"]
